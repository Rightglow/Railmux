from __future__ import annotations

import os
import shlex
import socket
import struct
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from railmux import __version__, windows_attach_relay
from railmux.tmux_server import TmuxServerTarget


def test_frame_decoder_preserves_partial_and_multiple_frames():
    first = windows_attach_relay._frame(windows_attach_relay._TYPE_OUTPUT, b"first")
    second = windows_attach_relay._frame(windows_attach_relay._TYPE_EXIT, b"done")
    decoder = windows_attach_relay._FrameDecoder()

    assert decoder.feed(first[:3]) == []
    assert decoder.feed(first[3:] + second) == [
        (windows_attach_relay._TYPE_OUTPUT, b"first"),
        (windows_attach_relay._TYPE_EXIT, b"done"),
    ]


def test_frame_decoder_rejects_oversized_payload():
    decoder = windows_attach_relay._FrameDecoder()
    header = windows_attach_relay._HEADER.pack(
        windows_attach_relay._TYPE_OUTPUT,
        windows_attach_relay._MAX_FRAME_BYTES + 1,
    )

    with pytest.raises(windows_attach_relay.WindowsAttachRelayError):
        decoder.feed(header)


def test_terminal_capability_rejects_control_and_oversized_values():
    assert (
        windows_attach_relay._terminal_capability("xterm-256color", "fallback", 32)
        == "xterm-256color"
    )
    assert (
        windows_attach_relay._terminal_capability("bad\nvalue", "fallback", 32)
        == "fallback"
    )
    assert (
        windows_attach_relay._terminal_capability("x" * 33, "fallback", 32)
        == "fallback"
    )


def test_cursor_coalescer_preserves_text_and_partial_sequences():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)

    assert cursor.feed(b"left\033[?2", 1.0) == b"left"
    assert cursor.feed(b"5lright", 1.02) == b"\033[?25lright"
    assert cursor.feed(b"\033[?25h", 1.04) == b""
    assert cursor.flush_due(1.11) == b""
    assert cursor.flush_due(1.15) == b"\033[?25h"


def test_cursor_coalescer_keeps_cursor_hidden_across_an_output_burst():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)

    first = cursor.feed(b"A\033[?25l", 1.0)
    second = cursor.feed(b"\033[2;2HB\033[?25h", 1.08)

    assert first == b"A\033[?25l"
    assert second == b"\033[2;2HB"
    assert cursor.flush_due(1.15) == b""
    assert cursor.flush_due(1.19) == b"\033[?25h"


def test_cursor_coalescer_retains_a_final_hidden_state():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)

    assert cursor.feed(b"frame\033[?25l", 1.0) == b"frame\033[?25l"
    assert cursor.flush_due(1.2) == b""


def test_cursor_coalescer_restores_visible_anchor_after_flicker_signature():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)

    rendered = cursor.feed(
        b"\033[?25l\033[55;3H\033[?25h\033[57;1H\033[?25l",
        1.0,
    )

    assert rendered == b"\033[?25l\033[55;3H\033[57;1H"
    assert cursor.flush_due(1.2) == b"\033[55;3H\033[?25h"


def test_cursor_coalescer_keeps_ime_anchor_inside_synchronized_repaints():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)

    # Establish the quiet prompt cursor. A redundant SHOW is still useful
    # semantic evidence even though it need not reach the terminal.
    assert cursor.feed(b"\033[55;3H\033[?25h", 0.9) == b"\033[55;3H"
    rendered = cursor.feed(
        b"\033[?25l\033[?2026h\033[52;1Hworking\033[57;1H\033[?2026l\033[?25h",
        1.0,
    )

    assert rendered == (
        b"\033[?25l\033[?2026h\033[52;1Hworking\033[57;1H\033[55;3H\033[?2026l"
    )
    # Once output becomes quiet, restore the provider's latest authoritative
    # frame cursor rather than pinning the prompt forever.
    assert cursor.flush_due(1.2) == b"\033[57;1H\033[?25h"


def test_cursor_coalescer_leaves_visible_synchronized_output_byte_exact():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)
    payload = b"\033[?2026h\033[4;2Hframe\033[4;7H\033[?2026l"

    assert cursor.feed(payload, 1.0) == payload
    assert cursor.flush_due(2.0) == b""


def test_cursor_coalescer_does_not_guess_after_relative_frame_output():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)
    cursor.feed(b"\033[55;3H\033[?25h", 0.9)
    payload = b"\033[?25l\033[?2026hrelative text\033[?2026l\033[?25h"

    assert cursor.feed(payload, 1.0) == (
        b"\033[?25l\033[?2026hrelative text\033[?2026l"
    )


def test_cursor_coalescer_relearns_anchor_after_explicit_input():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)
    cursor.feed(b"\033[55;3H\033[?25h", 0.9)
    cursor.note_input("中".encode())

    assert cursor.feed(b"\033[55;5H\033[?25h", 1.0) == b"\033[55;5H"
    assert cursor._stable_cursor_position == b"\033[55;5H"


def test_cursor_coalescer_does_not_override_one_intentional_hide():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)

    assert cursor.feed(b"\033[4;7H\033[?25l", 1.0) == (b"\033[4;7H\033[?25l")
    assert cursor.flush_due(1.2) == b""


def test_cursor_coalescer_invalidates_stale_visible_anchor_after_text():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)

    cursor.feed(
        b"\033[?25l\033[55;3Htext\033[?25h\033[57;1H\033[?25l",
        1.0,
    )

    assert cursor.flush_due(1.2) == b"\033[?25h"


def test_cursor_coalescer_leaves_ordinary_output_byte_exact():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)
    payload = b"typing and ordinary \033[31mterminal output\033[0m"

    assert cursor.feed(payload, 1.0) == payload
    assert cursor.flush_due(2.0) == b""


def test_cursor_coalescer_suppresses_a_redundant_show_without_blinking():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)

    assert cursor.feed(b"before\033[?25hafter", 1.0) == b"beforeafter"
    assert cursor.flush_due(2.0) == b""


def test_cursor_coalescer_does_not_parse_dectcem_inside_osc_payload():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)
    payload = b"\033]0;opaque-\033[?25h-payload\007after"

    assert cursor.feed(payload, 1.0) == payload
    assert cursor.flush_due(2.0) == b""


def test_cursor_coalescer_never_injects_inside_a_split_control_sequence():
    cursor = windows_attach_relay._CursorVisibilityCoalescer(quiet_interval=0.1)

    assert cursor.feed(b"\033[?25l", 1.0) == b"\033[?25l"
    assert cursor.feed(b"\033[?25h", 1.01) == b""
    assert cursor.feed(b"\033[38;2;12", 1.02) == b""
    assert cursor.flush_due(1.2) == b""
    assert cursor.next_timeout(0.25, 1.2) == 0.25
    assert cursor.feed(b"0;30mtext", 1.21) == b"\033[38;2;120;30mtext"
    assert cursor.flush_due(1.21) == b"\033[?25h"


def test_local_proxy_close_restores_a_visible_terminal_cursor(monkeypatch):
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    master_read, master_write = os.pipe()
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    client = windows_attach_relay.LocalPtyClient(
        999999,
        master_read,
        stdin_fd=input_read,
        stdout_fd=output_write,
    )
    try:
        client._cursor._physical_visible = False
        client._cursor._desired_visible = False
        client.close()
        os.close(output_write)
        output_write = -1
        assert os.read(output_read, 4096) == b"\033[?25h"
    finally:
        client.close()
        for fd in (
            input_read,
            input_write,
            output_read,
            output_write,
            master_write,
        ):
            if fd >= 0:
                os.close(fd)


def test_local_proxy_requires_managed_windows(monkeypatch):
    monkeypatch.setattr(
        windows_attach_relay,
        "running_in_managed_windows_wrapper",
        lambda _environ=None: False,
    )

    with pytest.raises(
        windows_attach_relay.WindowsAttachRelayError,
        match="unavailable",
    ):
        windows_attach_relay.start_local_pty_client(
            ["tmux", "attach"],
            environ={},
            stdin_fd=10,
            stdout_fd=11,
        )


def test_local_proxy_preserves_large_bracketed_paste_byte_exact(monkeypatch):
    input_read, input_write = os.pipe()
    output_fd = os.open(os.devnull, os.O_WRONLY)
    proxy_socket, child_socket = socket.socketpair()
    payload = (
        b"\033[200~"
        + ("本地粘贴-line\n" * 9000).encode()
        + b"\x1d\033[5~\033[<0;4;5M\033[201~"
    )
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    monkeypatch.setattr(windows_attach_relay, "_child_status", lambda _pid: None)
    monkeypatch.setattr(windows_attach_relay, "_stop_child", lambda _pid: 0)
    proxy_fd = proxy_socket.detach()
    client = windows_attach_relay.LocalPtyClient(
        999999,
        proxy_fd,
        stdin_fd=input_read,
        stdout_fd=output_fd,
    )

    def write_payload() -> None:
        pending = memoryview(payload)
        while pending:
            pending = pending[os.write(input_write, pending) :]

    writer = threading.Thread(target=write_payload, daemon=True)
    writer.start()
    received = bytearray()
    child_socket.setblocking(False)
    try:
        deadline = time.monotonic() + 3.0
        while len(received) < len(payload) and time.monotonic() < deadline:
            client.pump(0.01)
            try:
                received.extend(child_socket.recv(65536))
            except BlockingIOError:
                pass
        writer.join(timeout=1.0)
        assert not writer.is_alive()
        assert bytes(received) == payload
    finally:
        client.close()
        child_socket.close()
        os.close(input_read)
        os.close(input_write)
        os.close(output_fd)


def test_local_proxy_starts_at_exact_entry_geometry(monkeypatch):
    monkeypatch.setattr(
        windows_attach_relay,
        "running_in_managed_windows_wrapper",
        lambda _environ=None: True,
    )
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (164, 46))
    spawn = MagicMock(return_value=(77, 12))
    client = MagicMock()
    client_type = MagicMock(return_value=client)
    monkeypatch.setattr(windows_attach_relay, "_spawn_local_pty_process", spawn)
    monkeypatch.setattr(windows_attach_relay, "LocalPtyClient", client_type)
    env = {"RAILMUX_WINDOWS_RUNTIME": "msys2"}

    assert (
        windows_attach_relay.start_local_pty_client(
            ["tmux", "-L", "railmux"],
            environ=env,
            stdin_fd=10,
            stdout_fd=11,
        )
        is client
    )

    spawn.assert_called_once_with(
        ["tmux", "-L", "railmux"],
        env,
        width=164,
        height=46,
        suppress_stderr=False,
    )
    client_type.assert_called_once_with(77, 12, stdin_fd=10, stdout_fd=11)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires a POSIX PTY")
def test_local_proxy_forwards_real_pty_output_and_settles_cursor(monkeypatch):
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    pid, master_fd = windows_attach_relay._spawn_local_pty_process(
        [
            "/bin/sh",
            "-c",
            "printf 'left\\033[?25lmid\\033[?25hright'; sleep 0.15",
        ],
        os.environ,
        width=80,
        height=24,
    )
    client = windows_attach_relay.LocalPtyClient(
        pid,
        master_fd,
        stdin_fd=input_read,
        stdout_fd=output_write,
    )
    try:
        assert client.wait(timeout=2.0) == 0
        client.close()
        os.close(output_write)
        output_write = -1
        rendered = os.read(output_read, 4096)
    finally:
        client.close()
        for fd in (input_read, input_write, output_read, output_write):
            if fd >= 0:
                os.close(fd)

    assert rendered == b"left\033[?25lmidright\033[?25h"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires a POSIX PTY")
def test_local_proxy_restores_ime_anchor_after_noisy_final_hide(monkeypatch):
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    pid, master_fd = windows_attach_relay._spawn_local_pty_process(
        [
            "/bin/sh",
            "-c",
            "printf '\033[?25l\033[20;4H\033[?25h\033[24;1H\033[?25l'; sleep 0.15",
        ],
        os.environ,
        width=80,
        height=24,
    )
    client = windows_attach_relay.LocalPtyClient(
        pid,
        master_fd,
        stdin_fd=input_read,
        stdout_fd=output_write,
    )
    try:
        assert client.wait(timeout=2.0) == 0
        client.close()
        os.close(output_write)
        output_write = -1
        rendered = os.read(output_read, 4096)
    finally:
        client.close()
        for fd in (input_read, input_write, output_read, output_write):
            if fd >= 0:
                os.close(fd)

    assert rendered == (b"\033[?25l\033[20;4H\033[24;1H\033[20;4H\033[?25h")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires a POSIX PTY")
def test_local_proxy_stabilizes_ime_anchor_inside_real_sync_frame(monkeypatch):
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    pid, master_fd = windows_attach_relay._spawn_local_pty_process(
        [
            "/bin/sh",
            "-c",
            "printf '\033[20;4H\033[?25h'; sleep 0.02; "
            "printf '\033[?25l\033[?2026h\033[18;1Hworking"
            "\033[24;1H\033[?2026l\033[?25h'; sleep 0.15",
        ],
        os.environ,
        width=80,
        height=24,
    )
    client = windows_attach_relay.LocalPtyClient(
        pid,
        master_fd,
        stdin_fd=input_read,
        stdout_fd=output_write,
    )
    try:
        assert client.wait(timeout=2.0) == 0
        client.close()
        os.close(output_write)
        output_write = -1
        rendered = os.read(output_read, 4096)
    finally:
        client.close()
        for fd in (input_read, input_write, output_read, output_write):
            if fd >= 0:
                os.close(fd)

    assert (b"\033[?2026h\033[18;1Hworking\033[24;1H\033[20;4H\033[?2026l") in rendered
    assert rendered.endswith(b"\033[24;1H\033[?25h")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires a POSIX PTY")
def test_local_proxy_drains_terminal_restore_tail_when_terminated(monkeypatch):
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    pid, master_fd = windows_attach_relay._spawn_local_pty_process(
        [
            "/bin/sh",
            "-c",
            "trap 'printf \"\\033[?1049l\\033[?25h\"; exit 0' TERM; "
            "printf ready; while :; do sleep 1; done",
        ],
        os.environ,
        width=80,
        height=24,
    )
    client = windows_attach_relay.LocalPtyClient(
        pid,
        master_fd,
        stdin_fd=input_read,
        stdout_fd=output_write,
    )
    try:
        client.pump(0.2)
        client.terminate()
        client.close()
        os.close(output_write)
        output_write = -1
        rendered = os.read(output_read, 4096)
    finally:
        client.close()
        for fd in (input_read, input_write, output_read, output_write):
            if fd >= 0:
                os.close(fd)

    assert b"ready" in rendered
    assert b"\033[?1049l" in rendered
    assert client._cursor._physical_visible


@pytest.mark.parametrize("with_wt_marker", [True, False])
def test_client_uses_identity_pinned_run_shell_and_cleans_endpoint(
    monkeypatch,
    tmp_path,
    with_wt_marker,
):
    runtime_root = tmp_path.parent / f"rx-{os.getpid()}"
    runtime_root.mkdir(mode=0o700)
    monkeypatch.setattr(
        windows_attach_relay.restart_state,
        "runtime_state_dir",
        lambda: runtime_root,
    )
    monkeypatch.setattr(
        windows_attach_relay,
        "running_in_managed_windows_wrapper",
        lambda _environ=None: True,
    )
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (100, 35))
    observed = {}
    peer_threads = []

    def run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        helper = shlex.split(argv[-1])
        endpoint = helper[helper.index("--endpoint") + 1]
        token = bytes.fromhex(helper[helper.index("--token") + 1])
        observed["endpoint"] = endpoint
        observed["token_hex"] = token.hex()

        def connect():
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            peer.connect(endpoint)
            peer.sendall(windows_attach_relay._PROTOCOL_MAGIC)
            challenge = peer.recv(4096)
            peer.sendall(windows_attach_relay._challenge_response(token, challenge))
            peer.recv(4096)
            peer.close()

        thread = threading.Thread(target=connect)
        thread.start()
        peer_threads.append(thread)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(windows_attach_relay.subprocess, "run", run)
    monkeypatch.setattr(
        windows_attach_relay, "_peer_is_same_user", lambda _connection: True
    )
    read_fd, write_fd = os.pipe()
    target = TmuxServerTarget("/tmp/private/railmux", 77)

    try:
        environ = {
            "RAILMUX_WINDOWS_RUNTIME": "msys2",
            "RAILMUX_TMUX_LABEL": "railmux-test",
            "TERM": "xterm-256color",
        }
        if with_wt_marker:
            environ["WT_SESSION"] = "opaque-and-never-forwarded"
        client = windows_attach_relay.start_relay_client(
            target=target,
            session_id="$5",
            environ=environ,
            stdin_fd=read_fd,
            stdout_fd=write_fd,
        )
        endpoint = client.endpoint
        assert endpoint.exists()
        assert observed["argv"][:4] == [
            "tmux",
            "-S",
            "/tmp/private/railmux",
            "run-shell",
        ]
        assert observed["argv"][4] == "-b"
        assert observed["argv"][-1].startswith("exec env -u PYTHONPATH ")
        assert observed["argv"][-1].endswith(" >/dev/null 2>&1")
        assert observed["token_hex"] not in observed["endpoint"]
        helper = shlex.split(observed["argv"][-1])
        assert helper[helper.index("--socket-path") + 1] == target.socket_path
        assert os.path.isabs(helper[helper.index("--tmux-path") + 1])
        assert ("--synchronized-output" in helper) is with_wt_marker
        assert "opaque-and-never-forwarded" not in helper
        assert observed["kwargs"]["env"]["RAILMUX_TMUX_LABEL"] == ("railmux-test")
        assert (client._cursor is not None) is with_wt_marker
        client.close()
        assert not endpoint.exists()
    finally:
        os.close(read_fd)
        os.close(write_fd)
        for thread in peer_threads:
            thread.join(timeout=2)
        runtime_root.rmdir()


def test_relay_server_rejects_non_managed_runtime(monkeypatch):
    monkeypatch.setattr(
        windows_attach_relay,
        "running_in_managed_windows_wrapper",
        lambda: False,
    )

    assert (
        windows_attach_relay.relay_server_main(
            [
                "--endpoint",
                "/tmp/railmux-1/railmux/windows-attach-0123456789abcdef.sock",
                "--token",
                "00" * windows_attach_relay._TOKEN_BYTES,
                "--label",
                "railmux",
                "--runtime-id",
                "msys2-test",
                "--app-id",
                f"railmux-{__version__}",
                "--socket-path",
                "/tmp/tmux-1/railmux",
                "--tmux-path",
                "/usr/bin/tmux",
                "--server-pid",
                "123",
                "--session-id",
                "$1",
                "--width",
                "80",
                "--height",
                "24",
                "--term",
                "xterm-256color",
            ]
        )
        == 2
    )


def test_normalized_wait_status_uses_shell_signal_convention():
    assert windows_attach_relay._normalized_wait_status(15) == 143


def test_relay_tmux_client_adds_sync_before_attach(monkeypatch):
    target = TmuxServerTarget("/tmp/private/railmux", 77)
    monkeypatch.setattr(windows_attach_relay.os, "openpty", lambda: (10, 11))
    monkeypatch.setattr(windows_attach_relay, "_set_winsize", lambda *_a: None)
    monkeypatch.setattr(windows_attach_relay.os, "fork", lambda: 0)
    monkeypatch.setattr(windows_attach_relay.os, "close", lambda _fd: None)
    monkeypatch.setattr(windows_attach_relay.os, "setsid", lambda: None)
    monkeypatch.setattr(windows_attach_relay.fcntl, "ioctl", lambda *_a: None)
    monkeypatch.setattr(windows_attach_relay.os, "dup2", lambda *_a: None)
    observed = MagicMock(side_effect=RuntimeError("stop before exec"))
    monkeypatch.setattr(windows_attach_relay.os, "execve", observed)
    monkeypatch.setattr(
        windows_attach_relay.os,
        "_exit",
        MagicMock(side_effect=RuntimeError("child stopped")),
    )

    with pytest.raises(RuntimeError, match="child stopped"):
        windows_attach_relay._spawn_tmux_client(
            target,
            "$5",
            tmux_path="/usr/bin/tmux",
            width=100,
            height=35,
            term="xterm-256color",
            colorterm="truecolor",
            synchronized_output=True,
        )

    argv = observed.call_args.args[1]
    assert argv == [
        "/usr/bin/tmux",
        "-S",
        target.socket_path,
        "-T",
        "sync",
        "attach-session",
        "-t",
        "$5",
    ]


def test_pty_input_write_has_a_deadline(monkeypatch):
    monkeypatch.setattr(
        windows_attach_relay.os,
        "write",
        MagicMock(side_effect=BlockingIOError),
    )
    times = iter((0.0, windows_attach_relay._PTY_INPUT_TIMEOUT + 1))
    monkeypatch.setattr(windows_attach_relay.time, "monotonic", lambda: next(times))

    with pytest.raises(
        windows_attach_relay.WindowsAttachRelayError,
        match="remained blocked",
    ):
        windows_attach_relay._write_pty_input(10, b"input")


def test_stale_endpoint_cleanup_preserves_live_listener(tmp_path):
    stale = tmp_path / "windows-attach-0123456789abcdef.sock"
    live = tmp_path / "windows-attach-fedcba9876543210.sock"
    stale_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale_socket.bind(str(stale))
    stale_socket.close()
    live_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live_socket.bind(str(live))
    live_socket.listen(1)
    old = time.time() - windows_attach_relay._STALE_ENDPOINT_AGE - 1
    os.utime(stale, (old, old))
    os.utime(live, (old, old))

    try:
        windows_attach_relay._cleanup_stale_endpoints(tmp_path)
        assert not stale.exists()
        assert live.exists()
    finally:
        live_socket.close()
        if live.exists():
            live.unlink()


def test_terminal_eof_sends_close_once(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    client_socket, peer_socket = socket.socketpair()
    listener = socket.socket()
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    output_fd = os.open(os.devnull, os.O_WRONLY)
    endpoint = tmp_path / "absent.sock"
    client = windows_attach_relay.RelayClient(
        client_socket,
        listener,
        endpoint,
        windows_attach_relay._EndpointIdentity(0, 0),
        stdin_fd=read_fd,
        stdout_fd=output_fd,
    )
    try:
        client.pump(0.1)
        raw = peer_socket.recv(1024)
        kind, size = windows_attach_relay._HEADER.unpack(
            raw[: windows_attach_relay._HEADER.size]
        )
        assert kind == windows_attach_relay._TYPE_CLOSE
        assert size == 0
        assert raw[windows_attach_relay._HEADER.size :] == b""
        # The EOF descriptor was unregistered, so a second pump emits nothing.
        peer_socket.settimeout(0.05)
        client.pump(0.01)
        with pytest.raises(socket.timeout):
            peer_socket.recv(struct.calcsize(">BI"))
    finally:
        client.close()
        peer_socket.close()
        os.close(read_fd)
        os.close(output_fd)


def test_relay_socket_eof_is_an_explicit_transport_error(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    client_socket, peer_socket = socket.socketpair()
    listener = socket.socket()
    read_fd, write_fd = os.pipe()
    output_fd = os.open(os.devnull, os.O_WRONLY)
    client = windows_attach_relay.RelayClient(
        client_socket,
        listener,
        tmp_path / "absent.sock",
        windows_attach_relay._EndpointIdentity(0, 0),
        stdin_fd=read_fd,
        stdout_fd=output_fd,
    )
    peer_socket.close()
    try:
        with pytest.raises(
            windows_attach_relay.WindowsAttachRelayError,
            match="ended unexpectedly",
        ):
            client.pump(0.1)
    finally:
        client.close()
        os.close(read_fd)
        os.close(write_fd)
        os.close(output_fd)
