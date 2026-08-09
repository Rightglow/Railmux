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
    first = windows_attach_relay._frame(
        windows_attach_relay._TYPE_OUTPUT, b"first")
    second = windows_attach_relay._frame(
        windows_attach_relay._TYPE_EXIT, b"done")
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
    assert windows_attach_relay._terminal_capability(
        "xterm-256color", "fallback", 32) == "xterm-256color"
    assert windows_attach_relay._terminal_capability(
        "bad\nvalue", "fallback", 32) == "fallback"
    assert windows_attach_relay._terminal_capability(
        "x" * 33, "fallback", 32) == "fallback"


@pytest.mark.parametrize("with_wt_marker", [True, False])
def test_client_uses_identity_pinned_run_shell_and_cleans_endpoint(
    monkeypatch, tmp_path, with_wt_marker,
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
    monkeypatch.setattr(
        windows_attach_relay, "_terminal_size", lambda _fd: (100, 35))
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
            peer.sendall(windows_attach_relay._challenge_response(
                token, challenge))
            peer.recv(4096)
            peer.close()

        thread = threading.Thread(target=connect)
        thread.start()
        peer_threads.append(thread)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(windows_attach_relay.subprocess, "run", run)
    monkeypatch.setattr(
        windows_attach_relay, "_peer_is_same_user", lambda _connection: True)
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
            "tmux", "-S", "/tmp/private/railmux", "run-shell",
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
        assert observed["kwargs"]["env"]["RAILMUX_TMUX_LABEL"] == (
            "railmux-test"
        )
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

    assert windows_attach_relay.relay_server_main([
        "--endpoint", "/tmp/railmux-1/railmux/windows-attach-0123456789abcdef.sock",
        "--token", "00" * windows_attach_relay._TOKEN_BYTES,
        "--label", "railmux",
        "--runtime-id", "msys2-test",
        "--app-id", f"railmux-{__version__}",
        "--socket-path", "/tmp/tmux-1/railmux",
        "--tmux-path", "/usr/bin/tmux",
        "--server-pid", "123",
        "--session-id", "$1",
        "--width", "80",
        "--height", "24",
        "--term", "xterm-256color",
    ]) == 2


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
        "/usr/bin/tmux", "-S", target.socket_path, "-T", "sync",
        "attach-session", "-t", "$5",
    ]


def test_pty_input_write_has_a_deadline(monkeypatch):
    monkeypatch.setattr(
        windows_attach_relay.os,
        "write",
        MagicMock(side_effect=BlockingIOError),
    )
    times = iter((0.0, windows_attach_relay._PTY_INPUT_TIMEOUT + 1))
    monkeypatch.setattr(
        windows_attach_relay.time, "monotonic", lambda: next(times))

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
    monkeypatch.setattr(
        windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
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
            raw[:windows_attach_relay._HEADER.size])
        assert kind == windows_attach_relay._TYPE_CLOSE
        assert size == 0
        assert raw[windows_attach_relay._HEADER.size:] == b""
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
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(
        windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
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
