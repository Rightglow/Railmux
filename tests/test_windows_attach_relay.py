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
from railmux.fast_display_history import LocalHistoryView, PeriodicPrefetchGate
from railmux.fast_display_input import ClickTarget, LocalTextSelection, SgrMouseEvent
from railmux.fast_display_protocol import (
    HistoryBatch,
    HistorySnapshot,
    PathKind,
    PathOpenResult,
    PathResult,
    InputFrameDecoder,
)


def _set_local_history_state(client) -> None:
    history = LocalHistoryView(wheel_lines=3)
    history.visible_routes = tuple(getattr(client, "_routes", ()))
    history._routes_ready = True
    client._history = history
    client._history_worker = None
    client._history_request_decoder = InputFrameDecoder()
    client._history_prefetch = PeriodicPrefetchGate()
    client._next_history_prefetch = 0.0
    client._routes_force_refresh = False
    client._claude_history_override = None
    client._claude_history_prompt_input = None
    client._claude_history_prompt_mouse_button = None


def _install_local_history_snapshot(client, snapshot: HistorySnapshot) -> None:
    frame = client._history.begin_prefetch(time.monotonic(), force=True)
    assert frame
    request_id = client._history.prefetch_pending_id
    assert request_id is not None
    snapshot = HistorySnapshot(
        request_id,
        snapshot.pane_id,
        snapshot.x,
        snapshot.y,
        snapshot.width,
        snapshot.height,
        lines=snapshot.lines,
        mouse_forwardable=snapshot.mouse_forwardable,
        transcript_backed=snapshot.transcript_backed,
        transcript_available=snapshot.transcript_available,
        history_choice_required=snapshot.history_choice_required,
        more_available=snapshot.more_available,
        generation=snapshot.generation,
        timeline_start=snapshot.timeline_start,
        timeline_end=snapshot.timeline_end,
    )
    client._history.accept_prefetch(HistoryBatch(request_id, (snapshot,)))
    client._routes = client._history.visible_routes


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


def test_local_proxy_uses_default_history_if_config_changes_after_validation(
    monkeypatch,
):
    input_read, input_write = os.pipe()
    output_fd = os.open(os.devnull, os.O_WRONLY)
    proxy_socket, child_socket = socket.socketpair()
    monkeypatch.setattr(windows_attach_relay, "_terminal_size", lambda _fd: (80, 24))
    monkeypatch.setattr(windows_attach_relay, "_child_status", lambda _pid: None)
    monkeypatch.setattr(windows_attach_relay, "_stop_child", lambda _pid: 0)
    monkeypatch.setattr(
        windows_attach_relay,
        "load_config",
        MagicMock(side_effect=windows_attach_relay.ConfigError("changed")),
    )
    client = windows_attach_relay.LocalPtyClient(
        999999,
        proxy_socket.detach(),
        stdin_fd=input_read,
        stdout_fd=output_fd,
    )
    try:
        assert client._history.history_limit == 10000
    finally:
        client.close()
        child_socket.close()
        os.close(input_read)
        os.close(input_write)
        os.close(output_fd)


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


def test_local_semantic_click_opens_url_but_replays_an_ordinary_click(monkeypatch):
    proxy_socket, child_socket = socket.socketpair()
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    client.master_fd = proxy_socket.detach()
    client._session_id = "$4"
    client._selection = LocalTextSelection()
    client._routes = (HistorySnapshot(0, "%8", 0, 0, 80, 1),)
    client._routes_checked_at = time.monotonic()
    client._path_prompt = None
    client._path_prompt_mouse_button = None
    _set_local_history_state(client)
    screen = SimpleNamespace(
        width=80,
        height=1,
        cursor_x=10,
        cursor_y=0,
        rows=(b"See https://example.test/docs and ordinary text",),
    )
    renderer = MagicMock()
    renderer.presentation_stable = True
    renderer.screen = screen
    renderer.surface.translate_mouse_event.side_effect = lambda event, **_kwargs: event
    client._renderer = renderer
    opened = MagicMock(
        return_value=windows_attach_relay.local_open.OpenResult(
            True, "opened", "success"
        )
    )
    monkeypatch.setattr(windows_attach_relay.local_open, "open_url", opened)
    child_socket.setblocking(False)
    try:
        client._forward_input_part(SgrMouseEvent(b"url-down", 0, 8, 1, True))
        client._forward_input_part(SgrMouseEvent(b"url-up", 0, 8, 1, False))
        opened.assert_called_once_with("https://example.test/docs")
        with pytest.raises(BlockingIOError):
            child_socket.recv(64)

        client._forward_input_part(SgrMouseEvent(b"plain-down", 0, 40, 1, True))
        client._forward_input_part(SgrMouseEvent(b"plain-up", 0, 40, 1, False))
        assert child_socket.recv(64) == b"plain-downplain-up"
    finally:
        os.close(client.master_fd)
        child_socket.close()


def test_local_semantic_drag_copies_without_forwarding_mouse(monkeypatch):
    proxy_socket, child_socket = socket.socketpair()
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    client.master_fd = proxy_socket.detach()
    client._session_id = "$4"
    client._selection = LocalTextSelection()
    client._routes = (HistorySnapshot(0, "%8", 0, 0, 20, 1),)
    client._routes_checked_at = time.monotonic()
    client._path_prompt = None
    client._path_prompt_mouse_button = None
    _set_local_history_state(client)
    renderer = MagicMock()
    renderer.presentation_stable = True
    renderer.screen = SimpleNamespace(
        width=20, height=1, cursor_x=1, cursor_y=0, rows=(b"select me",)
    )
    renderer.surface.translate_mouse_event.side_effect = lambda event, **_kwargs: event
    client._renderer = renderer
    child_socket.setblocking(False)
    try:
        client._forward_input_part(SgrMouseEvent(b"down", 0, 1, 1, True))
        client._forward_input_part(SgrMouseEvent(b"drag", 32, 6, 1, True))
        client._forward_input_part(SgrMouseEvent(b"up", 0, 6, 1, False))

        renderer.copy_to_clipboard.assert_called_once_with(b"select")
        with pytest.raises(BlockingIOError):
            child_socket.recv(64)
    finally:
        os.close(client.master_fd)
        child_socket.close()


def test_local_windows_wheel_uses_shared_managed_history_without_tmux_copy_mode():
    proxy_socket, child_socket = socket.socketpair()
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    client.master_fd = proxy_socket.detach()
    client._session_id = "$4"
    client._selection = LocalTextSelection()
    client._routes = ()
    client._routes_checked_at = time.monotonic()
    client._path_prompt = None
    client._path_prompt_mouse_button = None
    _set_local_history_state(client)
    _install_local_history_snapshot(
        client,
        HistorySnapshot(
            0,
            "%8",
            0,
            0,
            20,
            2,
            lines=tuple(f"row-{index}".encode() for index in range(8)),
        ),
    )
    renderer = MagicMock()
    renderer.presentation_stable = True
    renderer.screen = SimpleNamespace(
        width=20,
        height=3,
        cursor_x=1,
        cursor_y=0,
        rows=(b"live-0", b"live-1", b"status"),
    )
    renderer.surface.translate_mouse_event.side_effect = lambda event, **_kwargs: event
    client._renderer = renderer
    child_socket.setblocking(False)
    try:
        client._forward_input_part(
            SgrMouseEvent(b"wheel-up", 64, 2, 1, True)
        )

        overlays = client._history.overlays()
        assert overlays[0][1] == (b"row-3", b"row-4")
        renderer.set_history_overlays.assert_called_with(overlays)
        with pytest.raises(BlockingIOError):
            child_socket.recv(64)
    finally:
        os.close(client.master_fd)
        child_socket.close()


def test_local_windows_sidebar_wheel_stays_tmux_owned():
    proxy_socket, child_socket = socket.socketpair()
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    client.master_fd = proxy_socket.detach()
    client._session_id = "$4"
    client._selection = LocalTextSelection()
    client._routes = ()
    client._routes_checked_at = time.monotonic()
    client._path_prompt = None
    client._path_prompt_mouse_button = None
    _set_local_history_state(client)
    _install_local_history_snapshot(
        client,
        HistorySnapshot(0, "%8", 10, 0, 10, 2, lines=(b"a", b"b")),
    )
    renderer = MagicMock()
    renderer.presentation_stable = True
    renderer.screen = SimpleNamespace(
        width=20,
        height=3,
        cursor_x=11,
        cursor_y=0,
        rows=(b"sidebar".ljust(10) + b"agent", b"", b"status"),
    )
    renderer.surface.translate_mouse_event.side_effect = lambda event, **_kwargs: event
    client._renderer = renderer
    child_socket.settimeout(0.5)
    try:
        client._forward_input_part(
            SgrMouseEvent(b"sidebar-wheel", 64, 2, 1, True)
        )
        assert child_socket.recv(64) == b"sidebar-wheel"
        renderer.set_history_overlays.assert_not_called()
    finally:
        os.close(client.master_fd)
        child_socket.close()


def test_local_windows_page_up_uses_managed_history_for_focused_agent():
    proxy_socket, child_socket = socket.socketpair()
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    client.master_fd = proxy_socket.detach()
    client._session_id = "$4"
    client._selection = LocalTextSelection()
    client._routes = ()
    client._routes_checked_at = time.monotonic()
    client._path_prompt = None
    client._path_prompt_mouse_button = None
    _set_local_history_state(client)
    _install_local_history_snapshot(
        client,
        HistorySnapshot(
            0,
            "%8",
            0,
            0,
            20,
            3,
            lines=tuple(f"row-{index}".encode() for index in range(12)),
        ),
    )
    renderer = MagicMock()
    renderer.screen = SimpleNamespace(
        width=20,
        height=4,
        cursor_x=1,
        cursor_y=1,
        rows=(b"live-0", b"live-1", b"live-2", b"status"),
    )
    client._renderer = renderer
    child_socket.setblocking(False)
    try:
        client._forward_input_part(b"\x1b[5~")

        assert client._history.active
        renderer.set_history_overlays.assert_called_with(
            client._history.overlays()
        )
        with pytest.raises(BlockingIOError):
            child_socket.recv(64)
    finally:
        os.close(client.master_fd)
        child_socket.close()


def test_local_windows_claude_history_choice_uses_shared_setting(monkeypatch):
    proxy_socket, child_socket = socket.socketpair()
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    client.master_fd = proxy_socket.detach()
    client._session_id = "$4"
    client._selection = LocalTextSelection()
    client._routes = ()
    client._routes_checked_at = time.monotonic()
    client._routes_force_refresh = False
    client._path_prompt = None
    client._path_prompt_mouse_button = None
    _set_local_history_state(client)
    client._claude_history_prompt_input = b"wheel-up"
    renderer = MagicMock()
    client._renderer = renderer
    settings = MagicMock()
    settings.set_claude_history_policy.return_value = True
    monkeypatch.setattr("railmux.settings.Settings", lambda: settings)
    child_socket.setblocking(False)
    try:
        client._forward_input_part(b"1")

        settings.set_claude_history_policy.assert_called_once_with("local")
        assert client._claude_history_override is None
        assert client._claude_history_prompt_input is None
        renderer.clear_claude_history_prompt.assert_called_once_with()
        with pytest.raises(BlockingIOError):
            child_socket.recv(64)
    finally:
        os.close(client.master_fd)
        child_socket.close()


def test_local_windows_one_time_native_claude_history_replays_wheel():
    proxy_socket, child_socket = socket.socketpair()
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    client.master_fd = proxy_socket.detach()
    client._session_id = "$4"
    client._selection = LocalTextSelection()
    client._routes = ()
    client._routes_checked_at = time.monotonic()
    client._routes_force_refresh = False
    client._path_prompt = None
    client._path_prompt_mouse_button = None
    _set_local_history_state(client)
    client._claude_history_prompt_input = b"wheel-up"
    client._renderer = MagicMock()
    child_socket.settimeout(0.5)
    try:
        client._forward_input_part(b"4")

        assert client._claude_history_override == "native"
        assert child_socket.recv(64) == b"wheel-up"
    finally:
        os.close(client.master_fd)
        child_socket.close()


def test_local_semantic_drag_keeps_ownership_during_frame_transition():
    proxy_socket, child_socket = socket.socketpair()
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    client.master_fd = proxy_socket.detach()
    client._session_id = "$4"
    client._selection = LocalTextSelection()
    client._routes = (HistorySnapshot(0, "%8", 0, 0, 20, 1),)
    client._routes_checked_at = time.monotonic()
    client._path_prompt = None
    client._path_prompt_mouse_button = None
    _set_local_history_state(client)
    client._size = (20, 1)
    renderer = MagicMock()
    renderer.presentation_stable = True
    renderer.screen = SimpleNamespace(
        width=20, height=1, cursor_x=1, cursor_y=0, rows=(b"select me",)
    )
    renderer.surface.translate_mouse_event.side_effect = lambda event, **_kwargs: event
    client._renderer = renderer
    child_socket.setblocking(False)
    try:
        client._forward_input_part(SgrMouseEvent(b"down", 0, 1, 1, True))
        renderer.presentation_stable = False
        client._forward_input_part(SgrMouseEvent(b"drag", 32, 6, 1, True))
        client._forward_input_part(SgrMouseEvent(b"up", 0, 6, 1, False))

        renderer.copy_to_clipboard.assert_called_once_with(b"select")
        with pytest.raises(BlockingIOError):
            child_socket.recv(64)
    finally:
        os.close(client.master_fd)
        child_socket.close()


def test_local_semantic_url_in_unfocused_agent_replays_focus_click():
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    proxy_socket, child_socket = socket.socketpair()
    client.master_fd = proxy_socket.detach()
    client._session_id = "$4"
    client._selection = LocalTextSelection()
    client._routes = (
        HistorySnapshot(0, "%8", 0, 0, 20, 1),
        HistorySnapshot(0, "%9", 20, 0, 30, 1),
    )
    client._routes_checked_at = time.monotonic()
    client._path_prompt = None
    client._path_prompt_mouse_button = None
    _set_local_history_state(client)
    renderer = MagicMock()
    renderer.presentation_stable = True
    renderer.screen = SimpleNamespace(
        width=50,
        height=1,
        cursor_x=2,
        cursor_y=0,
        rows=(b"focused pane".ljust(20) + b"https://example.test",),
    )
    renderer.surface.translate_mouse_event.side_effect = lambda event, **_kwargs: event
    client._renderer = renderer
    child_socket.settimeout(0.5)
    try:
        client._forward_input_part(SgrMouseEvent(b"down", 0, 25, 1, True))
        client._forward_input_part(SgrMouseEvent(b"up", 0, 25, 1, False))

        assert child_socket.recv(32) == b"downup"
        renderer.copy_to_clipboard.assert_not_called()
    finally:
        os.close(client.master_fd)
        child_socket.close()


def test_local_path_choice_completes_outside_input_loop(monkeypatch):
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    client._session_id = "$4"
    client._renderer = MagicMock()
    client._selection = MagicMock()
    client._routes = (HistorySnapshot(0, "%8", 0, 0, 20, 1),)
    client._routes_checked_at = 10.0
    client._routes_force_refresh = False
    _set_local_history_state(client)
    client._pending_path_open = None
    worker = MagicMock()
    worker.submit.return_value = True
    worker.drain.return_value = ()
    client._path_worker = worker
    target = ClickTarget("path", "/c/work", "%8")
    resolved = PathResult(9, PathKind.DIRECTORY, "/c/work", "external")

    client._apply_path_choice(target, resolved, "external", persistent=False)

    worker.submit.assert_called_once_with(
        "$4", 9, "%8", "/c/work", "external", False, None, None
    )
    assert client._pending_path_open == (target, resolved, "external")
    client._renderer.show_status.assert_called_once_with("Opening path…")

    worker.drain.return_value = (
        PathOpenResult(9, True, "success", "validated"),
    )
    opened = MagicMock(
        return_value=windows_attach_relay.local_open.OpenResult(
            True, "opened", "success"
        )
    )
    monkeypatch.setattr(
        windows_attach_relay.local_open, "open_windows_path", opened
    )

    client._drain_path_action_results()

    opened.assert_called_once_with("/c/work", directory=True)
    assert client._pending_path_open is None
    assert client._routes == ()


def test_local_internal_path_open_invalidates_old_visible_topology():
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    client._session_id = "$4"
    client._renderer = MagicMock()
    client._selection = MagicMock()
    client._routes = (HistorySnapshot(0, "%8", 0, 0, 20, 1),)
    client._routes_checked_at = 10.0
    client._routes_force_refresh = False
    _set_local_history_state(client)
    target = ClickTarget("path", "~", "%8")
    resolved = PathResult(11, PathKind.DIRECTORY, "/home/user", "internal")
    worker = MagicMock()
    worker.submit.return_value = True
    client._path_worker = worker

    client._apply_path_choice(
        target,
        resolved,
        "internal",
        persistent=False,
    )

    client._renderer.require_fresh_presentation.assert_called_once_with()

    worker.drain.return_value = (
        PathOpenResult(11, False, "warning", "Could not open terminal"),
    )
    client._drain_path_action_results()
    client._renderer.cancel_fresh_presentation_requirement.assert_called_once_with()


def test_local_route_refresh_uses_async_shared_history_source():
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    client._session_id = "$4"
    client._selection = MagicMock()
    client._selection.validate_routes.return_value = False
    client._renderer = MagicMock()
    client._routes = ()
    client._routes_checked_at = time.monotonic()
    client._routes_force_refresh = True
    _set_local_history_state(client)
    client._routes_force_refresh = True
    worker = MagicMock()
    worker.submit.return_value = True
    client._history_worker = worker

    client._refresh_routes()

    job = worker.submit.call_args.args[0]
    assert job.kind == "batch"
    assert job.session_id == "$4"
    assert job.request[1] == 300
    assert client._routes == ()
    assert client._history.prefetch_pending_id == job.request[0]


def test_local_path_resolution_completes_outside_input_loop():
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    client._session_id = "$4"
    client._renderer = MagicMock()
    client._pending_path_open = None
    client._pending_path_resolve = None
    client._next_path_request_id = 4
    worker = MagicMock()
    worker.submit_resolve.return_value = True
    worker.drain.return_value = ()
    client._path_worker = worker
    target = ClickTarget("path", r"C:\Users\user\.railmux\windows", "%8")

    client._open_target(target)

    worker.submit_resolve.assert_called_once_with(
        "$4", 4, "%8", r"C:\Users\user\.railmux\windows"
    )
    assert client._pending_path_resolve == (4, target)
    client._renderer.show_status.assert_called_once_with("Checking path…")

    resolved = PathResult(
        4,
        PathKind.DIRECTORY,
        "/c/Users/user/.railmux/windows",
        "ask",
    )
    worker.drain.return_value = (resolved,)
    client._drain_path_action_results()

    assert client._pending_path_resolve is None
    assert client._path_prompt == (target, resolved)
    client._renderer.show_path_prompt.assert_called_once_with()


def test_slow_local_path_resolution_does_not_stop_pty_drain(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def resolve(_session, request_id, _pane, _raw_path):
        started.set()
        assert release.wait(1.0)
        return PathResult(request_id, PathKind.UNAVAILABLE)

    monkeypatch.setattr(
        "railmux.fast_display_server.resolve_path_result", resolve
    )
    monkeypatch.setattr(
        windows_attach_relay, "_terminal_size", lambda _fd: (80, 24)
    )
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
        session_id="$4",
    )
    try:
        client._open_target(ClickTarget("path", "/c/work", "%8"))
        assert started.wait(0.5)

        child_socket.sendall(b"provider output while path lookup waits\r\n")
        client.pump(0.1)

        assert client._pty_seen_output is True
        assert client.returncode is None
    finally:
        release.set()
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


def test_semantic_renderer_keeps_history_overlay_while_live_screen_advances():
    output_fd = os.open(os.devnull, os.O_WRONLY)
    renderer = windows_attach_relay._SemanticTerminalRenderer(output_fd, 30, 4)
    paint = MagicMock()
    renderer.surface.paint = paint
    snapshot = HistorySnapshot(
        1,
        "%7",
        0,
        0,
        30,
        3,
        lines=(b"frozen-history",),
    )
    overlay = ((snapshot, (b"frozen-history",)),)
    try:
        renderer.set_history_overlays(overlay)
        renderer.feed(b"\033[2J\033[Hlive-one")
        assert renderer.paint_due(1.0, force=True)
        assert renderer.writer.wait_idle(1.0)

        renderer.feed(b"\033[H\033[2Klive-two")
        assert renderer.paint_due(2.0, force=True)
        assert renderer.writer.wait_idle(1.0)
    finally:
        renderer.close()
        os.close(output_fd)

    assert paint.call_count == 2
    assert paint.call_args_list[0].args[1] == overlay
    assert paint.call_args_list[1].args[1] == overlay
    assert b"live-two" in paint.call_args_list[1].args[0].rows[0]


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


def test_semantic_renderer_hit_testing_waits_for_physical_frame(monkeypatch):
    blocked = False
    started = threading.Event()
    release = threading.Event()

    def controllable_write(_fd, _payload):
        if blocked:
            started.set()
            assert release.wait(2.0)

    monkeypatch.setattr(
        windows_attach_relay,
        "_write_terminal_output",
        controllable_write,
    )
    output_fd = os.open(os.devnull, os.O_WRONLY)
    renderer = windows_attach_relay._SemanticTerminalRenderer(output_fd, 40, 4)
    try:
        renderer.feed(b"\033[2J\033[HOLD-VISIBLE")
        renderer.paint_due(1.0, force=True)
        assert renderer.writer.wait_idle(1.0)
        old_screen = renderer.screen
        old_serial = renderer.presentation_serial
        assert old_screen is not None

        blocked = True
        renderer.feed(b"\033[H\033[2KNEW-NOT-YET-VISIBLE")
        renderer.paint_due(2.0, force=True)
        assert started.wait(1.0)

        assert renderer.screen is old_screen
        assert renderer.presentation_serial == old_serial
        assert renderer.presentation_stable is False

        # A topology mutation requested while this already-queued frame is
        # writing must wait for one still-newer authoritative frame.
        renderer.require_fresh_presentation()
        release.set()
        assert renderer.writer.wait_idle(1.0)
        assert renderer.presentation_stable is False
        assert renderer.presentation_serial == old_serial + 1
        renderer.feed(b"\033[H\033[2KNEW-TOPOLOGY-VISIBLE")
        renderer.paint_due(3.0, force=True)
        assert renderer.writer.wait_idle(1.0)
        assert renderer.presentation_stable is True
        assert b"NEW-TOPOLOGY-VISIBLE" in renderer.screen.rows[0]
    finally:
        release.set()
        renderer.close()
        os.close(output_fd)


def test_semantic_renderer_eventually_paints_path_prompt_after_backpressure(
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
    renderer = windows_attach_relay._SemanticTerminalRenderer(output_fd, 40, 4)
    prompt = MagicMock()
    renderer.surface.show_path_open_prompt = prompt
    try:
        renderer.writer.write(b"busy")
        assert started.wait(1.0)

        renderer.show_path_prompt()
        assert renderer.paint_due(1.0, force=True) is False
        prompt.assert_not_called()

        release.set()
        assert renderer.writer.wait_idle(1.0)
        assert renderer.next_timeout(1.0, 2.0) == 0.0
        assert renderer.paint_due(2.0, force=True) is True
        prompt.assert_called_once_with()
    finally:
        release.set()
        renderer.close()
        os.close(output_fd)


def test_local_pointer_is_dropped_while_visible_frame_is_transitioning():
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    proxy_socket, child_socket = socket.socketpair()
    client.master_fd = proxy_socket.detach()
    client._selection = LocalTextSelection()
    client._path_prompt = None
    client._path_prompt_mouse_button = None
    client._routes = ()
    _set_local_history_state(client)
    client._size = (80, 24)
    renderer = MagicMock()
    renderer.presentation_stable = False
    renderer.screen = SimpleNamespace(
        height=24,
        cursor_x=0,
        cursor_y=0,
        rows=(b"",) * 24,
    )
    renderer.surface.translate_mouse_event.side_effect = (
        lambda event, **_kwargs: event
    )
    client._renderer = renderer
    child_socket.setblocking(False)
    try:
        client._forward_input_part(SgrMouseEvent(b"stale-click", 0, 4, 5, True))
        renderer.presentation_stable = True
        client._forward_input_part(SgrMouseEvent(b"orphan-drag", 32, 8, 5, True))
        client._forward_input_part(SgrMouseEvent(b"orphan-up", 0, 8, 5, False))
        with pytest.raises(BlockingIOError):
            child_socket.recv(64)
    finally:
        os.close(client.master_fd)
        child_socket.close()


def test_local_left_gesture_outside_agent_route_remains_tmux_owned():
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    proxy_socket, child_socket = socket.socketpair()
    client.master_fd = proxy_socket.detach()
    client._session_id = "$4"
    client._selection = LocalTextSelection()
    client._routes = (HistorySnapshot(0, "%8", 0, 0, 20, 1),)
    client._routes_checked_at = time.monotonic()
    client._path_prompt = None
    client._path_prompt_mouse_button = None
    _set_local_history_state(client)
    client._size = (20, 2)
    renderer = MagicMock()
    renderer.presentation_stable = True
    renderer.screen = SimpleNamespace(
        width=20,
        height=2,
        cursor_x=1,
        cursor_y=0,
        rows=(b"agent", b"status"),
    )
    renderer.surface.translate_mouse_event.side_effect = lambda event, **_kwargs: event
    client._renderer = renderer
    child_socket.settimeout(0.5)
    try:
        client._forward_input_part(SgrMouseEvent(b"down", 0, 2, 2, True))
        client._forward_input_part(SgrMouseEvent(b"drag", 32, 4, 2, True))
        client._forward_input_part(SgrMouseEvent(b"up", 0, 4, 2, False))

        assert child_socket.recv(64) == b"downdragup"
    finally:
        os.close(client.master_fd)
        child_socket.close()


def test_local_explicit_tmux_copy_mode_key_remains_byte_exact():
    client = windows_attach_relay.LocalPtyClient.__new__(
        windows_attach_relay.LocalPtyClient
    )
    proxy_socket, child_socket = socket.socketpair()
    client.master_fd = proxy_socket.detach()
    client._selection = LocalTextSelection()
    client._path_prompt = None
    client._path_prompt_mouse_button = None
    client._renderer = MagicMock()
    client._routes = ()
    _set_local_history_state(client)
    child_socket.settimeout(0.5)
    try:
        client._forward_input_part(b"\x02[")
        assert child_socket.recv(8) == b"\x02["
    finally:
        os.close(client.master_fd)
        child_socket.close()


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
