from __future__ import annotations

import os
import re
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


def test_terminal_selector_batch_processes_input_before_output():
    output = (SimpleNamespace(data="tmux"), 1)
    terminal = (SimpleNamespace(data="terminal"), 1)
    relay = (SimpleNamespace(data="relay"), 1)

    assert windows_attach_relay._terminal_events_first(
        [output, relay, terminal]
    ) == [terminal, output, relay]


def test_local_windows_ctrl_c_signal_is_forwarded_to_the_active_pty(monkeypatch):
    installed = []
    restored = []
    previous = object()
    monkeypatch.setattr(
        windows_attach_relay.signal,
        "getsignal",
        lambda _sig: previous,
    )

    def set_handler(_sig, handler):
        if handler is previous:
            restored.append(handler)
        else:
            installed.append(handler)

    monkeypatch.setattr(windows_attach_relay.signal, "signal", set_handler)
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    monkeypatch.setattr(windows_attach_relay, "_child_status", lambda _pid: None)
    monkeypatch.setattr(windows_attach_relay, "_stop_child", lambda _pid: 0)
    input_read, input_write = os.pipe()
    output_fd = os.open(os.devnull, os.O_WRONLY)
    proxy_socket, child_socket = socket.socketpair()
    proxy_fd = proxy_socket.detach()
    os.set_blocking(proxy_fd, False)
    client = windows_attach_relay.LocalPtyClient(
        999999,
        proxy_fd,
        stdin_fd=input_read,
        stdout_fd=output_fd,
        forward_interrupts=True,
    )
    child_socket.settimeout(0.5)
    try:
        assert installed
        installed[0](windows_attach_relay.signal.SIGINT, None)

        client.pump(0.0)

        assert child_socket.recv(16) == b"\x03"
        assert client.returncode is None
    finally:
        client.close()
        child_socket.close()
        os.close(input_read)
        os.close(input_write)
        os.close(output_fd)

    assert restored == [previous]


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
        client._renderer.surface.start()
        client.close()
        os.close(output_write)
        output_write = -1
        rendered = os.read(output_read, 4096)
        assert rendered.endswith(b"\033[?1049l")
        assert b"\033[?25h" in rendered
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
    punctuation = "A“B”C‘D’E「F」G".encode()
    payload = (
        punctuation
        + b"\033[200~"
        + ("本地“粘贴”‘line’「保留」\n" * 9000).encode()
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
    client_type.assert_called_once_with(
        77,
        12,
        stdin_fd=10,
        stdout_fd=11,
        forward_interrupts=True,
    )


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

    assert b"leftmidright" in rendered
    assert rendered.endswith(b"\033[?1049l")


def test_semantic_renderer_does_not_repaint_unchanged_prompt_for_working_tick():
    output_read, output_write = os.pipe()
    renderer = windows_attach_relay._SemanticTerminalRenderer(output_write, 30, 4)
    try:
        renderer.feed(b"\033[2J\033[1;1HWorking\033[4;1HPROMPT\033[4;7H")
        renderer.paint_due(1.0, force=True)
        assert renderer.writer.wait_idle(1.0)
        os.set_blocking(output_read, False)
        while True:
            try:
                os.read(output_read, 65536)
            except BlockingIOError:
                break

        renderer.feed(
            b"\033[?2026h\033[1;1H\033[2KWorking."
            b"\033[4;7H\033[?2026l"
        )
        renderer.paint_due(2.0)
        assert renderer.writer.wait_idle(1.0)
        rendered = os.read(output_read, 65536)
    finally:
        renderer.close()
        os.close(output_read)
        os.close(output_write)

    assert b"Working." in rendered
    assert b"PROMPT" not in rendered
    assert b"\033[4;1H\033[2K" not in rendered


def test_semantic_renderer_never_paints_a_partial_synchronized_frame():
    output_read, output_write = os.pipe()
    renderer = windows_attach_relay._SemanticTerminalRenderer(output_write, 30, 4)
    try:
        renderer.feed(b"\033[2J\033[4;1HPROMPT\033[4;7H")
        renderer.paint_due(1.0, force=True)
        assert renderer.writer.wait_idle(1.0)
        os.set_blocking(output_read, False)
        while True:
            try:
                os.read(output_read, 65536)
            except BlockingIOError:
                break

        renderer.feed(b"\033[?2026h\033[4;1H\033[2K")
        assert renderer.next_timeout(0.5, 2.0) == (
            windows_attach_relay._SYNCHRONIZED_UPDATE_MAX_HOLD
        )
        renderer.paint_due(2.0)
        with pytest.raises(BlockingIOError):
            os.read(output_read, 65536)

        renderer.feed(b"PROMPT\033[4;7H\033[?2026l")
        renderer.paint_due(2.1)
        with pytest.raises(BlockingIOError):
            os.read(output_read, 65536)
    finally:
        renderer.close()
        os.close(output_read)
        os.close(output_write)

    # The completed frame restored the exact prior prompt and cursor, so the
    # semantic differ has nothing at all to send to the physical terminal.


def test_semantic_renderer_bounds_an_unclosed_synchronized_frame():
    output_read, output_write = os.pipe()
    renderer = windows_attach_relay._SemanticTerminalRenderer(output_write, 30, 4)
    try:
        renderer.feed(b"\033[2J\033[4;1HPROMPT\033[4;7H")
        renderer.paint_due(1.0, force=True)
        assert renderer.writer.wait_idle(1.0)
        os.set_blocking(output_read, False)
        while True:
            try:
                os.read(output_read, 65536)
            except BlockingIOError:
                break

        renderer.feed(b"\033[?2026h\033[1;1HWorking")
        renderer.paint_due(2.0)
        with pytest.raises(BlockingIOError):
            os.read(output_read, 65536)

        renderer.paint_due(
            2.0 + windows_attach_relay._SYNCHRONIZED_UPDATE_MAX_HOLD
        )
        assert renderer.writer.wait_idle(1.0)
        rendered = os.read(output_read, 65536)
    finally:
        renderer.close()
        os.close(output_read)
        os.close(output_write)

    assert b"Working" in rendered
    assert b"PROMPT" not in rendered


def test_semantic_renderer_preserves_split_osc52_clipboard_requests():
    output_fd = os.open(os.devnull, os.O_WRONLY)
    renderer = windows_attach_relay._SemanticTerminalRenderer(output_fd, 30, 4)
    copy = MagicMock()
    renderer.surface.copy_to_clipboard = copy
    try:
        renderer.feed(b"before\033]52;c;dGV")
        renderer.feed(b"zdA==\007after")
        renderer.paint_due(1.0, force=True)
        assert renderer.writer.wait_idle(1.0)
    finally:
        renderer.close()
        os.close(output_fd)

    copy.assert_called_once_with(b"test")


def test_semantic_renderer_retains_only_latest_clipboard_during_backpressure(
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()

    def blocked_write(_fd, _payload):
        started.set()
        assert release.wait(2.0)

    monkeypatch.setattr(
        windows_attach_relay,
        "_write_terminal_output",
        blocked_write,
    )
    output_fd = os.open(os.devnull, os.O_WRONLY)
    renderer = windows_attach_relay._SemanticTerminalRenderer(output_fd, 30, 4)
    copy = MagicMock()
    renderer.surface.copy_to_clipboard = copy
    try:
        renderer.feed(b"SCREEN")
        renderer.paint_due(1.0, force=True)
        assert started.wait(1.0)

        renderer.feed(b"\033]52;c;Zmlyc3Q=\007")
        renderer.feed(b"\033]52;c;c2Vjb25k\007")
        assert renderer._pending_clipboard == b"second"
        assert copy.call_count == 0

        release.set()
        assert renderer.writer.wait_idle(1.0)
        renderer.paint_due(2.0, force=True)
        assert renderer.writer.wait_idle(1.0)
        copy.assert_called_once_with(b"second")
    finally:
        release.set()
        renderer.close()
        os.close(output_fd)


def test_semantic_renderer_does_not_block_or_queue_stale_frames_for_slow_terminal(
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    writes = []

    def slow_first_write(_fd, payload):
        writes.append(payload)
        if len(writes) == 1:
            started.set()
            assert release.wait(2.0)

    monkeypatch.setattr(
        windows_attach_relay,
        "_write_terminal_output",
        slow_first_write,
    )
    output_fd = os.open(os.devnull, os.O_WRONLY)
    renderer = windows_attach_relay._SemanticTerminalRenderer(output_fd, 40, 4)
    try:
        renderer.feed(b"\033[2J\033[1;1HINITIAL")
        before = time.monotonic()
        renderer.paint_due(1.0, force=True)
        assert time.monotonic() - before < 0.1
        assert started.wait(1.0)
        assert not renderer.writer.idle

        renderer.feed(b"\033[1;1H\033[2KINTERMEDIATE")
        renderer.paint_due(2.0)
        renderer.feed(b"\033[1;1H\033[2KFINAL")
        renderer.paint_due(3.0)
        assert renderer.producer.delivered is not None
        assert renderer.producer.delivered.sequence == 1
        assert renderer.next_timeout(0.25, 3.0) <= (
            windows_attach_relay._TERMINAL_WRITER_POLL
        )

        release.set()
        assert renderer.writer.wait_idle(1.0)
        renderer.paint_due(4.0)
        assert renderer.writer.wait_idle(1.0)
    finally:
        release.set()
        renderer.close()
        os.close(output_fd)

    rendered = b"".join(writes)
    assert b"INITIAL" in rendered
    assert b"FINAL" in rendered
    assert b"INTERMEDIATE" not in rendered


def test_physical_terminal_writer_queue_is_bounded(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocked_write(_fd, _payload):
        started.set()
        assert release.wait(2.0)

    monkeypatch.setattr(
        windows_attach_relay,
        "_write_terminal_output",
        blocked_write,
    )
    output_fd = os.open(os.devnull, os.O_WRONLY)
    writer = windows_attach_relay._TerminalFdWriter(output_fd)
    try:
        writer.write(b"current")
        assert started.wait(1.0)
        for _index in range(windows_attach_relay._TERMINAL_WRITER_MAX_QUEUE):
            writer.write(b"queued")
        with pytest.raises(
            windows_attach_relay.WindowsAttachRelayError,
            match="exceeded",
        ):
            writer.write(b"overflow")
    finally:
        release.set()
        writer.close()
        os.close(output_fd)


def test_semantic_renderer_blocked_shutdown_discards_stale_queue_and_orders_restore(
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    writes = []

    def blocked_write(_fd, payload):
        writes.append(payload)
        started.set()
        assert release.wait(2.0)

    monkeypatch.setattr(
        windows_attach_relay,
        "_write_terminal_output",
        blocked_write,
    )
    monkeypatch.setattr(
        windows_attach_relay,
        "_TERMINAL_WRITER_CLOSE_TIMEOUT",
        0.02,
    )
    output_fd = os.open(os.devnull, os.O_WRONLY)
    renderer = windows_attach_relay._SemanticTerminalRenderer(output_fd, 40, 4)
    try:
        renderer.feed(b"\033[2J\033[HFINAL")
        renderer.paint_due(1.0, force=True)
        assert started.wait(1.0)

        before = time.monotonic()
        assert renderer.close()
        assert time.monotonic() - before < 0.2
        with renderer.writer._condition:
            assert renderer.writer._closed
            assert renderer.writer._queue == [renderer.surface.close_payload()]
    finally:
        release.set()
        renderer.writer.close(1.0)
        renderer.writer._thread.join(1.0)
        os.close(output_fd)

    assert not renderer.writer._thread.is_alive()
    assert len(writes) == 2
    assert writes[-1].endswith(b"\033[?1049l")


def test_semantic_renderer_surfaces_write_failure_and_cleanup_does_not_raise(
    monkeypatch,
):
    failed = threading.Event()

    def failed_write(_fd, _payload):
        failed.set()
        raise OSError("terminal unavailable")

    monkeypatch.setattr(
        windows_attach_relay,
        "_write_terminal_output",
        failed_write,
    )
    output_fd = os.open(os.devnull, os.O_WRONLY)
    renderer = windows_attach_relay._SemanticTerminalRenderer(output_fd, 40, 4)
    renderer.feed(b"\033[2J\033[HSCREEN")
    renderer.paint_due(1.0, force=True)
    assert failed.wait(1.0)

    with pytest.raises(
        windows_attach_relay.WindowsAttachRelayError,
        match="stopped accepting",
    ):
        renderer.next_timeout(0.1, 2.0)

    renderer.close()
    os.close(output_fd)


def test_local_proxy_forwards_input_while_physical_output_is_blocked(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocked_write(_fd, _payload):
        started.set()
        assert release.wait(2.0)

    monkeypatch.setattr(
        windows_attach_relay,
        "_write_terminal_output",
        blocked_write,
    )
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    monkeypatch.setattr(windows_attach_relay, "_child_status", lambda _pid: None)
    monkeypatch.setattr(windows_attach_relay, "_stop_child", lambda _pid: 0)
    input_read, input_write = os.pipe()
    output_fd = os.open(os.devnull, os.O_WRONLY)
    proxy_socket, child_socket = socket.socketpair()
    proxy_fd = proxy_socket.detach()
    os.set_blocking(proxy_fd, False)
    client = windows_attach_relay.LocalPtyClient(
        999999,
        proxy_fd,
        stdin_fd=input_read,
        stdout_fd=output_fd,
    )
    child_socket.settimeout(1.0)
    try:
        child_socket.sendall(b"\033[2J\033[HSCREEN")
        paint_deadline = time.monotonic() + 1.0
        while not started.is_set() and time.monotonic() < paint_deadline:
            client.pump(0.05)
        assert started.wait(1.0)

        os.write(input_write, b"typed while painting")
        before = time.monotonic()
        client.pump(0.1)

        assert time.monotonic() - before < 0.5
        assert child_socket.recv(1024) == b"typed while painting"
        assert not client._renderer.writer.idle
    finally:
        release.set()
        client.close()
        child_socket.close()
        os.close(input_read)
        os.close(input_write)
        os.close(output_fd)


def test_local_proxy_coalesces_a_large_restore_backlog_to_its_latest_screen(
    monkeypatch,
):
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    proxy_socket, child_socket = socket.socketpair()
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    monkeypatch.setattr(windows_attach_relay, "_child_status", lambda _pid: None)
    monkeypatch.setattr(windows_attach_relay, "_stop_child", lambda _pid: 0)
    proxy_fd = proxy_socket.detach()
    os.set_blocking(proxy_fd, False)
    client = windows_attach_relay.LocalPtyClient(
        999999,
        proxy_fd,
        stdin_fd=input_read,
        stdout_fd=output_write,
    )
    frames = []
    for index in range(160):
        label = f"STATE-{index:04d}".encode()
        body = b"\n".join(label + b"-" * 68 for _row in range(24))
        frames.append(b"\033[?2026h\033[2J\033[H" + body + b"\033[?2026l")
    frames.append(b"\033[?2026h\033[2J\033[HFINAL\033[?2026l")
    payload = b"".join(frames)

    def write_payload() -> None:
        pending = memoryview(payload)
        while pending:
            pending = pending[child_socket.send(pending) :]

    writer = threading.Thread(target=write_payload, daemon=True)
    writer.start()
    os.set_blocking(output_read, False)
    rendered = bytearray()
    try:
        deadline = time.monotonic() + 5.0
        while (writer.is_alive() or client._pty_backlog_pending) and time.monotonic() < deadline:
            client.pump(0.01)
            try:
                rendered.extend(os.read(output_read, 65536))
            except BlockingIOError:
                pass
        writer.join(timeout=1.0)
        settle_deadline = time.monotonic() + 1.0
        while b"FINAL" not in rendered and time.monotonic() < settle_deadline:
            client.pump(0.05)
            try:
                rendered.extend(os.read(output_read, 65536))
            except BlockingIOError:
                pass
        assert client._renderer.writer.wait_idle(1.0)
        while True:
            try:
                rendered.extend(os.read(output_read, 65536))
            except BlockingIOError:
                break
    finally:
        client.close()
        child_socket.close()
        os.close(input_read)
        os.close(input_write)
        os.close(output_read)
        os.close(output_write)

    assert not writer.is_alive()
    assert b"FINAL" in rendered
    # A restore which exceeds the staleness deadline may publish a periodic
    # newest sample, but it must not serialize the 160 source states.
    assert len(set(re.findall(rb"STATE-[0-9]{4}", rendered))) < 5


def test_local_proxy_coalesces_microgapped_screen_replay_after_settling(
    monkeypatch,
):
    writes = []
    terminal_size = [80, 24]
    monkeypatch.setattr(
        windows_attach_relay,
        "_write_terminal_output",
        lambda _fd, payload: writes.append(payload),
    )
    monkeypatch.setattr(
        windows_attach_relay,
        "_terminal_size",
        lambda _fd: tuple(terminal_size),
    )
    monkeypatch.setattr(windows_attach_relay, "_set_winsize", lambda *_args: None)
    monkeypatch.setattr(windows_attach_relay.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(windows_attach_relay, "_child_status", lambda _pid: None)
    monkeypatch.setattr(windows_attach_relay, "_stop_child", lambda _pid: 0)
    input_read, input_write = os.pipe()
    output_fd = os.open(os.devnull, os.O_WRONLY)
    proxy_socket, child_socket = socket.socketpair()
    proxy_fd = proxy_socket.detach()
    os.set_blocking(proxy_fd, False)
    client = windows_attach_relay.LocalPtyClient(
        999999,
        proxy_fd,
        stdin_fd=input_read,
        stdout_fd=output_fd,
    )

    def pump_until_painted(label: bytes) -> None:
        deadline = time.monotonic() + 1.0
        while label not in b"".join(writes) and time.monotonic() < deadline:
            client.pump(0.02)
        assert client._renderer.writer.wait_idle(1.0)
        assert label in b"".join(writes)

    try:
        child_socket.sendall(b"\033[2J\033[HREADY")
        pump_until_painted(b"READY")
        assert not client._pty_catchup_active
        writes.clear()

        terminal_size[:] = [100, 30]
        client._resize_if_needed()
        assert client._pty_catchup_active

        for index in range(8):
            label = f"REPLAY-{index:02d}".encode()
            body = b"\n".join(label + b"-" * 68 for _row in range(24))
            child_socket.sendall(
                b"\033[?2026h\033[2J\033[H"
                + body
                + b"\033[?2026l"
            )
            client.pump(0.0)
            time.sleep(0.02)
        child_socket.sendall(b"\033[?2026h\033[2J\033[HFINAL\033[?2026l")
        pump_until_painted(b"FINAL")
    finally:
        client.close()
        child_socket.close()
        os.close(input_read)
        os.close(input_write)
        os.close(output_fd)

    replayed = set(re.findall(rb"REPLAY-[0-9]{2}", b"".join(writes)))
    assert replayed == set()


def test_local_proxy_keeps_small_settled_updates_immediate(monkeypatch):
    writes = []
    monkeypatch.setattr(windows_attach_relay, "_LOCAL_FRAME_INTERVAL", 0.0)
    monkeypatch.setattr(
        windows_attach_relay,
        "_write_terminal_output",
        lambda _fd, payload: writes.append(payload),
    )
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    monkeypatch.setattr(windows_attach_relay, "_child_status", lambda _pid: None)
    monkeypatch.setattr(windows_attach_relay, "_stop_child", lambda _pid: 0)
    input_read, input_write = os.pipe()
    output_fd = os.open(os.devnull, os.O_WRONLY)
    proxy_socket, child_socket = socket.socketpair()
    proxy_fd = proxy_socket.detach()
    os.set_blocking(proxy_fd, False)
    client = windows_attach_relay.LocalPtyClient(
        999999,
        proxy_fd,
        stdin_fd=input_read,
        stdout_fd=output_fd,
    )
    try:
        # A slow tmux startup must begin its staleness ceiling at the first
        # output byte, not at LocalPtyClient construction.
        client._last_pty_paint -= 10.0
        child_socket.sendall(b"\033[2J\033[HREADY")
        client.pump(0.0)
        assert b"READY" not in b"".join(writes)
        deadline = time.monotonic() + 1.0
        while b"READY" not in b"".join(writes) and time.monotonic() < deadline:
            client.pump(0.02)
        assert client._renderer.writer.wait_idle(1.0)
        assert not client._pty_catchup_active
        writes.clear()

        child_socket.sendall(b"\033[Htick")
        client.pump(0.1)
        assert client._renderer.writer.wait_idle(1.0)
        assert b"tick" in b"".join(writes)
        assert not client._pty_catchup_active
    finally:
        client.close()
        child_socket.close()
        os.close(input_read)
        os.close(input_write)
        os.close(output_fd)


def test_local_proxy_sustained_backlog_still_paints_bounded_progress(monkeypatch):
    writes = []
    received_input = bytearray()
    sent_frames = 0

    monkeypatch.setattr(
        windows_attach_relay,
        "_write_terminal_output",
        lambda _fd, payload: writes.append(payload),
    )
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    monkeypatch.setattr(windows_attach_relay, "_child_status", lambda _pid: None)
    monkeypatch.setattr(windows_attach_relay, "_stop_child", lambda _pid: 0)
    input_read, input_write = os.pipe()
    output_fd = os.open(os.devnull, os.O_WRONLY)
    proxy_socket, child_socket = socket.socketpair()
    proxy_fd = proxy_socket.detach()
    os.set_blocking(proxy_fd, False)
    client = windows_attach_relay.LocalPtyClient(
        999999,
        proxy_fd,
        stdin_fd=input_read,
        stdout_fd=output_fd,
    )
    child_socket.setblocking(False)
    stop = threading.Event()

    def produce_continuously() -> None:
        nonlocal sent_frames
        pending = memoryview(b"")
        while not stop.is_set():
            if not pending:
                sent_frames += 1
                label = f"STREAM-{sent_frames:06d}".encode()
                body = b"\n".join(label + b"-" * 64 for _row in range(24))
                pending = memoryview(
                    b"\033[?2026h\033[2J\033[H"
                    + body
                    + b"\033[?2026l"
                )
            try:
                pending = pending[child_socket.send(pending) :]
            except BlockingIOError:
                pass
            try:
                received_input.extend(child_socket.recv(65536))
            except BlockingIOError:
                pass

    producer = threading.Thread(target=produce_continuously, daemon=True)
    producer.start()
    backlog_seen = False
    try:
        os.write(input_write, b"input during steady output")
        # Older supported pyte/Python combinations can spend most of the first
        # catch-up staleness window consuming this deliberately saturated
        # stream. Keep the source busy for more than three complete production
        # windows so every interpreter must expose two independent bounded
        # publications rather than weakening the assertion.
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            client.pump(0.01)
            backlog_seen = backlog_seen or client._pty_backlog_pending
        assert client._renderer.writer.wait_idle(1.0)
    finally:
        stop.set()
        producer.join(1.0)
        client._pty_backlog_pending = False
        client.close()
        child_socket.close()
        os.close(input_read)
        os.close(input_write)
        os.close(output_fd)

    rendered = b"".join(writes)
    labels = set(re.findall(rb"STREAM-[0-9]{6}", rendered))
    assert not producer.is_alive()
    assert backlog_seen
    assert received_input == b"input during steady output"
    assert len(labels) >= 2
    assert len(labels) < sent_frames


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
    assert not client._renderer.surface.active


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
    read_fd, input_write = os.pipe()
    write_fd = os.open(os.devnull, os.O_WRONLY)
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
        assert (client._renderer is not None) is with_wt_marker
        client.close()
        assert not endpoint.exists()
    finally:
        os.close(read_fd)
        os.close(input_write)
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


def test_relay_close_cleans_endpoint_after_renderer_cleanup_failure(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    unlink = MagicMock()
    monkeypatch.setattr(windows_attach_relay, "_unlink_owned_endpoint", unlink)
    client_socket, peer_socket = socket.socketpair()
    listener = socket.socket()
    read_fd, write_fd = os.pipe()
    output_fd = os.open(os.devnull, os.O_WRONLY)
    endpoint = tmp_path / "windows-attach-0123456789abcdef.sock"
    identity = windows_attach_relay._EndpointIdentity(1, 2)
    client = windows_attach_relay.RelayClient(
        client_socket,
        listener,
        endpoint,
        identity,
        stdin_fd=read_fd,
        stdout_fd=output_fd,
    )
    renderer = MagicMock()
    renderer.close.side_effect = windows_attach_relay.WindowsAttachRelayError(
        "cleanup failed"
    )
    client._renderer = renderer

    client.close()

    renderer.close.assert_called_once_with()
    unlink.assert_called_once_with(endpoint, identity)
    assert client_socket.fileno() == -1
    assert listener.fileno() == -1
    peer_socket.close()
    os.close(read_fd)
    os.close(write_fd)
    os.close(output_fd)
