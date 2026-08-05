from __future__ import annotations

import inspect
import io
import os
import base64
import shlex
import signal
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from unittest.mock import MagicMock, patch

import pytest

from railmux.config import Config
from railmux.fast_display_protocol import (
    ClipboardCopy,
    ClaudeHistoryPolicyResult,
    DISPLAY_MAGIC,
    HistoryBatch,
    HistorySnapshot,
    InputFrameDecoder,
    InputKind,
    PathKind,
    PathOpenResult,
    PathResult,
    PROTOCOL_VERSION,
    REMOTE_CONFIG_PROTOCOL,
    REMOTE_ATTACH_ACCEPTED,
    REMOTE_ATTACH_BUSY,
    REMOTE_HELLO_PREFIX,
    REMOTE_START,
    RemoteExit,
    ScreenUpdate,
    ScreenUpdateDecoder as ClientScreenUpdateDecoder,
    ServerMessageDecoder,
    TerminalMode,
    UpdateKind,
    decode_history_prefetch,
    decode_history_request,
    decode_path_request,
    decode_path_open_request,
    decode_claude_history_policy,
    decode_claude_history_choice,
    encode_history_batch,
    encode_history_prefetch,
    encode_history_request,
    encode_history_snapshot,
    encode_path_request,
    encode_path_open_request,
    encode_path_open_result,
    encode_path_result,
    encode_claude_history_policy,
    encode_claude_history_policy_result,
    encode_clipboard_copy,
    encode_heartbeat,
    encode_update,
)
from railmux.fast_display_server import parse_args as parse_server_args
from railmux.fast_display_server import render_rows
from railmux.fast_display_server import terminal_modes_for_screen
from railmux import fast_display_client, fast_display_server, tmux_ctl
from railmux.fast_display_client import (
    AppliedScreen,
    LOCAL_ESCAPE,
    RemoteHello,
    RemoteAttachKind,
    RemoteLaunchMode,
    RemoteStartKind,
    RemoteStartup,
    ScreenModel,
    TerminalSurface,
    UpdateKind as ClientUpdateKind,
    build_remote_command_argv,
    build_ssh_argv,
    build_ssh_install_argv,
    build_ssh_private_venv_install_argv,
    await_remote_startup,
    encode_input as encode_client_input,
    encode_keyframe_request as encode_client_keyframe_request,
    encode_resize as encode_client_resize,
    focus_in_frame_for_screen,
    parse_args as parse_client_args,
    parse_remote_hello,
    prepare_remote_process,
    remote_install_help,
    screen_input_may_change_routes,
    split_local_escape,
    termux_prompt_touch_action,
)
from railmux import fast_display_history
from railmux.fast_display_history import (
    HistoryAction,
    LocalHistoryView,
    PeriodicPrefetchGate,
    input_may_change_routes,
)
from railmux.fast_display_input import (
    ClickTarget,
    LocalTextSelection,
    SelectionAction,
    SelectionSource,
    SgrMouseEvent,
    TermuxTouchKeyboard,
    TerminalInputDecoder,
    is_termux_environment,
    page_key_direction,
    split_page_key_input,
)


def _keyframe(
    sequence: int = 1,
    width: int = 4,
    height: int = 2,
    terminal_modes: TerminalMode = TerminalMode.NONE,
) -> ScreenUpdate:
    return ScreenUpdate(
        kind=UpdateKind.KEYFRAME,
        sequence=sequence,
        width=width,
        height=height,
        cursor_x=1,
        cursor_y=0,
        cursor_visible=True,
        rows=tuple((index, f"row-{index}".encode()) for index in range(height)),
        terminal_modes=terminal_modes,
    )


def test_compressed_keyframe_crosses_standalone_client_decoder_in_parts():
    packet = encode_update(_keyframe())
    decoder = ClientScreenUpdateDecoder()

    assert decoder.feed(b"login banner\n" + packet[:8]) == []
    assert decoder.feed(packet[8:-1]) == []
    updates = decoder.feed(packet[-1:])

    assert len(updates) == 1
    update = updates[0]
    assert update.kind is ClientUpdateKind.KEYFRAME
    assert update.sequence == 1
    assert (update.width, update.height) == (4, 2)
    assert update.rows == ((0, b"row-0"), (1, b"row-1"))
    assert update.terminal_modes is TerminalMode.NONE


def test_v10_wire_round_trips_allowlisted_terminal_modes_and_rejects_unknown():
    modes = TerminalMode.BRACKETED_PASTE | TerminalMode.FOCUS_EVENTS
    update = ClientScreenUpdateDecoder().feed(
        encode_update(_keyframe(terminal_modes=modes))
    )[0]

    assert update.terminal_modes == modes
    with pytest.raises(ValueError, match="unknown terminal mode"):
        encode_update(_keyframe(terminal_modes=TerminalMode(1 << 8)))

    malformed = bytearray(encode_update(_keyframe()))
    modes_offset = len(DISPLAY_MAGIC) + 4 + 1 + 14
    malformed[modes_offset : modes_offset + 2] = struct.pack(">H", 1 << 8)
    assert ClientScreenUpdateDecoder().feed(malformed) == []


def test_v10_decoder_does_not_accept_a_v9_packet_prefix():
    old_packet = b"RMUXD9\x00" + struct.pack(">I", 32) + bytes(32)

    assert ClientScreenUpdateDecoder().feed(old_packet) == []


def test_v10_unified_decoder_round_trips_history_capabilities():
    snapshot = HistorySnapshot(
        request_id=7,
        pane_id="%42",
        x=30,
        y=1,
        width=50,
        height=2,
        lines=(b"old-1", b"old-2", b"visible-1", b"visible-2"),
        mouse_forwardable=True,
        more_available=True,
        transcript_available=True,
        history_choice_required=True,
        generation=42,
    )
    local_snapshot = replace(
        snapshot,
        request_id=8,
        transcript_backed=True,
        history_choice_required=False,
    )
    packet = b"".join(
        (
            encode_update(_keyframe()),
            encode_history_snapshot(snapshot),
            encode_history_snapshot(local_snapshot),
            encode_update(_keyframe(sequence=2)),
        )
    )
    decoder = ServerMessageDecoder()

    assert decoder.feed(packet[:11]) == []
    messages = decoder.feed(packet[11:])

    assert messages == [
        _keyframe(),
        snapshot,
        local_snapshot,
        _keyframe(sequence=2),
    ]


def test_v10_history_snapshot_round_trips_the_maximum_line_count():
    snapshot = HistorySnapshot(
        request_id=8,
        pane_id="%42",
        x=30,
        y=1,
        width=50,
        height=2,
        lines=(b"x",) * 20000,
    )

    assert ServerMessageDecoder().feed(encode_history_snapshot(snapshot)) == [snapshot]


def test_rejected_history_response_is_bounded_and_screen_decoder_ignores_it():
    rejected = HistorySnapshot(9, None)
    packet = encode_history_snapshot(rejected) + encode_update(_keyframe())

    assert ServerMessageDecoder().feed(packet)[0] == rejected
    assert ClientScreenUpdateDecoder().feed(packet) == [_keyframe()]


def test_history_request_round_trip_validates_pointer_and_line_limit():
    decoder = InputFrameDecoder()
    message = decoder.feed(encode_history_request(12, 80, 24, 1500))[0]

    assert message.kind is InputKind.REQUEST_HISTORY
    assert decode_history_request(message.data) == (12, 80, 24, 1500)
    with pytest.raises(ValueError):
        encode_history_request(1, 0, 24)
    assert decode_history_request(
        decoder.feed(encode_history_request(1, 80, 24, 20000))[0].data
    ) == (1, 80, 24, 20000)
    with pytest.raises(ValueError):
        encode_history_request(1, 80, 24, 20001)


def test_path_request_and_result_round_trip_are_bounded():
    request = InputFrameDecoder().feed(
        encode_path_request(17, "%42", "src/main.py")
    )[0]

    assert request.kind is InputKind.RESOLVE_PATH
    assert decode_path_request(request.data) == (17, "%42", "src/main.py")
    result = PathResult(17, PathKind.FILE, "/workspace/src/main.py")
    assert ServerMessageDecoder().feed(encode_path_result(result)) == [result]
    rejected = PathResult(18, PathKind.UNAVAILABLE)
    assert ServerMessageDecoder().feed(encode_path_result(rejected)) == [rejected]

    with pytest.raises(ValueError):
        encode_path_request(1, "42", "main.py")
    with pytest.raises(ValueError):
        encode_path_request(1, "%42", "x" * 4097)
    with pytest.raises(ValueError):
        encode_path_result(PathResult(1, PathKind.UNAVAILABLE, "/leak"))


def test_path_open_request_and_result_round_trip_are_bounded():
    decoder = InputFrameDecoder()
    message = decoder.feed(encode_path_open_request(
        19,
        "%42",
        "src/main.py",
        policy="internal",
        persistent=True,
        line=123,
        column=7,
    ))[0]

    assert message.kind is InputKind.OPEN_PATH
    assert decode_path_open_request(message.data) == (
        19,
        "%42",
        "src/main.py",
        "internal",
        True,
        123,
        7,
    )
    result = PathOpenResult(19, True, "success", "Opened inside Railmux")
    assert ServerMessageDecoder().feed(encode_path_open_result(result)) == [result]

    with pytest.raises(ValueError):
        encode_path_open_request(
            1, "%42", "main.py", policy="ask", persistent=False
        )
    with pytest.raises(ValueError):
        encode_path_open_request(
            1,
            "%42",
            "main.py",
            policy="external",
            persistent=False,
            column=3,
        )
    with pytest.raises(ValueError):
        encode_path_open_result(PathOpenResult(1, False, "debug", "no"))


def test_claude_history_policy_input_round_trip_is_bounded():
    decoder = InputFrameDecoder()
    encoded = encode_claude_history_policy("local")
    message = decoder.feed(encoded)[0]

    assert message.kind is InputKind.SET_CLAUDE_HISTORY
    assert decode_claude_history_choice(message.data) == ("local", True)
    temporary = decoder.feed(encode_claude_history_policy("native", persistent=False))[
        0
    ]
    assert decode_claude_history_choice(temporary.data) == ("native", False)
    assert decoder.feed(encoded[:-1] + b"\x05") == []
    with pytest.raises(ValueError):
        encode_claude_history_policy("ask")
    with pytest.raises(ValueError):
        decode_claude_history_policy(b"\x03")
    with pytest.raises(ValueError):
        decode_claude_history_choice(b"\x05")


def test_claude_history_policy_result_round_trips_scope_and_outcome():
    decoder = ServerMessageDecoder()

    assert decoder.feed(
        encode_claude_history_policy_result("native", persistent=True, applied=True)
    ) == [ClaudeHistoryPolicyResult("native", True, True)]
    assert decoder.feed(
        encode_claude_history_policy_result("local", persistent=False, applied=True)
    ) == [ClaudeHistoryPolicyResult("local", False, True)]
    with pytest.raises(ValueError):
        encode_claude_history_policy_result("ask", persistent=True, applied=True)
    with pytest.raises(ValueError):
        encode_claude_history_policy_result("local", persistent=True, applied=1)


@pytest.mark.parametrize(
    ("policy", "persistent", "runtime", "prefetch", "forwarded"),
    [
        ("local", True, None, True, b""),
        ("local", False, "local", True, b""),
        ("native", True, None, False, b"wheel"),
        ("native", False, "native", False, b"wheel"),
    ],
)
def test_claude_history_policy_ack_applies_scope_and_original_wheel(
    policy,
    persistent,
    runtime,
    prefetch,
    forwarded,
):
    action = fast_display_client.apply_claude_history_policy_result(
        (policy, persistent, b"wheel"),
        ClaudeHistoryPolicyResult(policy, persistent, True),
    )

    assert action is not None
    assert action.update_runtime
    assert action.runtime_choice == runtime
    assert action.prefetch is prefetch
    assert action.forwarded_input == forwarded


def test_claude_history_policy_ack_rejects_mismatch_and_preserves_failed_state():
    assert (
        fast_display_client.apply_claude_history_policy_result(
            ("local", False, b"wheel"),
            ClaudeHistoryPolicyResult("native", False, True),
        )
        is None
    )

    failed = fast_display_client.apply_claude_history_policy_result(
        ("local", True, b"wheel"),
        ClaudeHistoryPolicyResult("local", True, False),
    )
    assert failed is not None
    assert not failed.update_runtime
    assert not failed.prefetch
    assert failed.forwarded_input == b""


def test_claude_history_reconnect_resends_only_this_time_choice():
    assert fast_display_client.claude_history_reconnect_frame(None) == b""
    message = InputFrameDecoder().feed(
        fast_display_client.claude_history_reconnect_frame("native")
    )[0]
    assert decode_claude_history_choice(message.data) == ("native", False)


def test_server_claude_history_choice_persists_only_when_requested():
    settings = MagicMock()
    settings.set_claude_history_policy.return_value = True

    applied, override = fast_display_server.apply_claude_history_choice(
        "local",
        persistent=True,
        current_override="native",
        settings=settings,
    )
    assert (applied, override) == (True, None)
    settings.set_claude_history_policy.assert_called_once_with("local")

    settings.reset_mock()
    applied, override = fast_display_server.apply_claude_history_choice(
        "native",
        persistent=False,
        current_override="local",
        settings=settings,
    )
    assert (applied, override) == (True, "native")
    settings.set_claude_history_policy.assert_not_called()


def test_server_failed_persistent_history_choice_keeps_previous_override():
    settings = MagicMock()
    settings.set_claude_history_policy.return_value = False

    assert fast_display_server.apply_claude_history_choice(
        "local",
        persistent=True,
        current_override="native",
        settings=settings,
    ) == (False, "native")


def test_server_resolves_only_readable_paths_from_visible_agent_cwd(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "src"
    source.mkdir()
    code = source / "main.py"
    code.write_text("print('ok')\n")
    pane = fast_display_server._PaneGeometry("%8", 20, 0, 60, 24)
    monkeypatch.setattr(
        fast_display_server,
        "_list_agent_panes",
        lambda _session: (pane,),
    )
    monkeypatch.setattr(
        fast_display_server,
        "_pane_current_path",
        lambda _pane: str(tmp_path),
    )

    assert fast_display_server.resolve_path_result(
        "$4", 1, "%8", "src/main.py", path_open_policy="ask"
    ) == PathResult(1, PathKind.FILE, str(code.resolve()))
    assert fast_display_server.resolve_path_result(
        "$4", 2, "%8", "src", path_open_policy="ask"
    ) == PathResult(2, PathKind.DIRECTORY, str(source.resolve()))
    assert fast_display_server.resolve_path_result(
        "$4", 3, "%8", "missing.py", path_open_policy="ask"
    ) == PathResult(3, PathKind.UNAVAILABLE)
    assert fast_display_server.resolve_path_result(
        "$4", 4, "%9", "src/main.py", path_open_policy="ask"
    ) == PathResult(4, PathKind.UNAVAILABLE)


def test_server_reads_nested_provider_cwd_from_history_source(monkeypatch):
    target = MagicMock()
    pane = fast_display_server._PaneGeometry(
        "%8",
        20,
        0,
        60,
        24,
        history_server=target,
        history_pane_id="%2",
    )
    nested_argv = ["tmux", "-L", "nested", "display-message"]
    monkeypatch.setattr(
        fast_display_server.tmux_server,
        "target_argv",
        lambda *args: nested_argv,
    )
    checked = MagicMock(return_value="/remote/workspace\n")
    monkeypatch.setattr(fast_display_server.subprocess, "check_output", checked)

    assert fast_display_server._pane_current_path(pane) == "/remote/workspace"
    assert checked.call_args.args[0] is nested_argv


def test_server_path_open_choice_persists_and_uses_managed_vim(monkeypatch):
    resolved = PathResult(
        7,
        PathKind.FILE,
        "/workspace/src/main.py",
        "internal",
    )
    settings = MagicMock()
    settings.set_path_open_policy.return_value = True
    manager = MagicMock()
    manager.slot_for_owner.return_value = "primary"
    manager.open_viewer.return_value = MagicMock(
        ok=True,
        level="success",
        message="Opened remote file inside Railmux",
    )
    monkeypatch.setattr(
        fast_display_server,
        "resolve_path_result",
        lambda *_args, **_kwargs: resolved,
    )
    monkeypatch.setattr(fast_display_server, "Settings", lambda: settings)
    monkeypatch.setattr(
        fast_display_server,
        "manager_for_session",
        lambda _session: manager,
    )
    monkeypatch.setattr(fast_display_server.shutil, "which", lambda _name: "/usr/bin/vim")

    result = fast_display_server.apply_path_open_request(
        "$4",
        7,
        "%8",
        "src/main.py",
        "internal",
        True,
        12,
        3,
    )

    assert result == PathOpenResult(
        7,
        True,
        "success",
        "Opened remote file inside Railmux",
    )
    settings.set_path_open_policy.assert_called_once_with("internal")
    manager.open_viewer.assert_called_once_with(
        "primary",
        "%8",
        "/workspace/src/main.py",
        line=12,
        column=3,
    )


def test_server_external_path_choice_never_mutates_tmux(monkeypatch):
    settings = MagicMock()
    settings.set_path_open_policy.return_value = True
    manager = MagicMock()
    monkeypatch.setattr(
        fast_display_server,
        "resolve_path_result",
        lambda *_args, **_kwargs: PathResult(
            8,
            PathKind.DIRECTORY,
            "/workspace/src",
            "external",
        ),
    )
    monkeypatch.setattr(fast_display_server, "Settings", lambda: settings)
    monkeypatch.setattr(
        fast_display_server,
        "manager_for_session",
        lambda _session: manager,
    )

    result = fast_display_server.apply_path_open_request(
        "$4",
        8,
        "%8",
        "src",
        "external",
        False,
        None,
        None,
    )

    assert result.applied
    assert result.level == "success"
    settings.set_path_open_policy.assert_not_called()
    manager.assert_not_called()


def test_server_options_change_clears_only_a_this_time_history_override():
    assert fast_display_server.refresh_claude_history_override(
        "native", "local", "native"
    ) == (None, None)
    assert fast_display_server.refresh_claude_history_override(
        "native", "local", "local"
    ) == ("native", "local")
    assert fast_display_server.refresh_claude_history_override(
        None, None, "native"
    ) == (None, None)


def test_clipboard_payload_round_trips_and_surface_reencodes_osc52():
    data = "Review layout 你好".encode()
    decoder = ServerMessageDecoder()

    assert decoder.feed(encode_clipboard_copy(data)) == [ClipboardCopy(data)]

    output = io.BytesIO()
    surface = TerminalSurface(output)
    with patch(
        "railmux.fast_display_client.local_clipboard.copy",
        return_value=False,
    ):
        surface.copy_to_clipboard(data)
    assert b"\033]52;c;" + base64.b64encode(data) + b"\007" in output.getvalue()


def test_clipboard_payload_uses_native_local_writer_before_osc52():
    data = "Copied status 你好".encode()
    output = io.BytesIO()
    surface = TerminalSurface(output)

    with patch(
        "railmux.fast_display_client.local_clipboard.copy",
        return_value=True,
    ) as native:
        surface.copy_to_clipboard(data)

    native.assert_called_once_with(data)
    assert output.getvalue() == b""
    assert surface.active is False


def test_local_text_selection_replays_a_plain_click_unchanged():
    route = HistorySnapshot(1, "%8", 3, 0, 6, 1)
    source = SelectionSource(
        route,
        (b"\033[0mabcHello!\033[0m",),
        3,
    )
    selection = LocalTextSelection()
    press = SgrMouseEvent(b"down", 0, 4, 1, True)
    release = SgrMouseEvent(b"up", 0, 4, 1, False)

    assert selection.pointer_event(press, source).handled is True
    action = selection.pointer_event(release, source)

    assert action.handled is True
    assert action.replay_events == (press, release)
    assert action.copy_data is None
    assert selection.active is False


def test_local_text_selection_opens_url_or_remote_path_only_on_clean_release():
    route = HistorySnapshot(1, "%8", 0, 0, 80, 1)
    selection = LocalTextSelection()
    url_source = SelectionSource(
        route,
        (b"See https://example.test/docs.",),
        0,
    )
    press = SgrMouseEvent(b"url-down", 0, 8, 1, True)
    release = SgrMouseEvent(b"url-up", 0, 8, 1, False)

    selection.pointer_event(press, url_source)
    url_action = selection.pointer_event(release, url_source)

    assert url_action.open_target == ClickTarget(
        "url",
        "https://example.test/docs",
        "%8",
        highlight_row=0,
        highlight_column=4,
        highlight_text=b"https://example.test/docs",
        highlight_segments=((0, 4, b"https://example.test/docs"),),
    )
    assert url_action.replay_events == ()

    path_source = SelectionSource(
        route,
        (b"changed src/railmux/app.py:123:7",),
        0,
    )
    path_press = SgrMouseEvent(b"path-down", 0, 12, 1, True)
    selection.pointer_event(path_press, path_source)
    path_action = selection.pointer_event(
        SgrMouseEvent(b"path-up", 0, 12, 1, False),
        path_source,
    )
    assert path_action.open_target == ClickTarget(
        "path",
        "src/railmux/app.py",
        "%8",
        123,
        7,
        highlight_row=0,
        highlight_column=8,
        highlight_text=b"src/railmux/app.py:123:7",
        highlight_segments=((0, 8, b"src/railmux/app.py:123:7"),),
    )


def test_local_text_selection_stops_url_before_chinese_prose():
    route = HistorySnapshot(1, "%8", 0, 0, 100, 1)
    source = SelectionSource(
        route,
        (
            "See https://github.com/NVIDIA/TensorRT-LLM/pull/17000)，并注明由"
            .encode(),
        ),
        0,
    )
    selection = LocalTextSelection()
    press = SgrMouseEvent(b"down", 0, 20, 1, True)
    release = SgrMouseEvent(b"up", 0, 20, 1, False)

    selection.pointer_event(press, source)
    action = selection.pointer_event(release, source)

    assert action.open_target is not None
    assert action.open_target.value == (
        "https://github.com/NVIDIA/TensorRT-LLM/pull/17000"
    )
    assert action.open_target.highlight_text == (
        b"https://github.com/NVIDIA/TensorRT-LLM/pull/17000"
    )


def test_local_text_selection_uses_pane_offset_for_hover_and_click():
    route = HistorySnapshot(1, "%8", 10, 2, 24, 1)
    source = SelectionSource(
        route,
        (b"sidebar---See https://example.test",),
        10,
    )
    selection = LocalTextSelection()
    hover = SgrMouseEvent(b"hover", 35, 18, 3, True)

    assert selection.hover(hover, source)
    assert selection.segments() == ((2, 14, b"https://example.test"),)

    press = SgrMouseEvent(b"down", 0, 18, 3, True)
    release = SgrMouseEvent(b"up", 0, 18, 3, False)
    selection.pointer_event(press, source)
    action = selection.pointer_event(release, source)
    assert action.open_target is not None
    assert action.open_target.value == "https://example.test"


def test_local_text_selection_recognizes_wrapped_path_from_second_row():
    route = HistorySnapshot(1, "%8", 4, 1, 16, 2)
    source = SelectionSource(
        route,
        (b"See /home/user/l", b"ong/file.py     "),
        0,
    )
    selection = LocalTextSelection()
    hover = SgrMouseEvent(b"hover", 35, 7, 3, True)

    assert selection.hover(hover, source)
    assert selection.segments() == (
        (1, 8, b"/home/user/l"),
        (2, 4, b"ong/file.py"),
    )

    selection.pointer_event(SgrMouseEvent(b"down", 0, 7, 3, True), source)
    action = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 7, 3, False),
        source,
    )
    assert action.open_target is not None
    assert action.open_target.value == "/home/user/long/file.py"


def test_local_text_selection_joins_agent_indented_hard_wrapped_path():
    route = HistorySnapshot(1, "%8", 20, 2, 50, 2)
    source = SelectionSource(
        route,
        (
            b"sidebar".ljust(20)
            + b"Report: /home/user/project/",
            b"sidebar".ljust(20)
            + b"    results/index.html             ",
        ),
        20,
    )
    selection = LocalTextSelection()
    hover = SgrMouseEvent(b"hover", 35, 30, 4, True)

    assert selection.hover(hover, source)
    assert selection.segments() == (
        (2, 28, b"/home/user/project/"),
        (3, 24, b"results/index.html"),
    )

    selection.pointer_event(
        SgrMouseEvent(b"down", 0, 30, 4, True),
        source,
    )
    action = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 30, 4, False),
        source,
    )
    assert action.open_target is not None
    assert action.open_target.value == (
        "/home/user/project/results/index.html"
    )
    assert action.open_target.highlight_segments == (
        (2, 28, b"/home/user/project/"),
        (3, 24, b"results/index.html"),
    )

    first_row_selection = LocalTextSelection()
    first_row_selection.pointer_event(
        SgrMouseEvent(b"down-first", 0, 35, 3, True),
        source,
    )
    first_row_action = first_row_selection.pointer_event(
        SgrMouseEvent(b"up-first", 0, 35, 3, False),
        source,
    )
    assert first_row_action.open_target is not None
    assert first_row_action.open_target.value == action.open_target.value


def test_local_text_selection_joins_indented_path_split_inside_name():
    route = HistorySnapshot(1, "%8", 0, 0, 50, 2)
    source = SelectionSource(
        route,
        (
            b"Report: /home/user/TensorRT-",
            b"    LLM/results/index.html",
        ),
        0,
    )
    selection = LocalTextSelection()

    assert selection.hover(
        SgrMouseEvent(b"hover", 35, 8, 2, True),
        source,
    )
    assert selection.segments() == (
        (0, 8, b"/home/user/TensorRT-"),
        (1, 4, b"LLM/results/index.html"),
    )


def test_local_text_selection_does_not_join_adjacent_path_list_items():
    route = HistorySnapshot(1, "%8", 0, 0, 50, 2)
    source = SelectionSource(
        route,
        (
            b"First: /home/user/first.txt",
            b"    sibling/second.txt",
        ),
        0,
    )
    selection = LocalTextSelection()

    assert selection.hover(
        SgrMouseEvent(b"hover", 35, 10, 1, True),
        source,
    )
    assert selection.segments() == (
        (0, 7, b"/home/user/first.txt"),
    )


def test_local_text_selection_does_not_append_indented_prose_to_directory():
    route = HistorySnapshot(1, "%8", 0, 0, 50, 2)
    source = SelectionSource(
        route,
        (
            b"Directory: /home/user/project/",
            b"    Summary",
        ),
        0,
    )
    selection = LocalTextSelection()

    assert selection.hover(
        SgrMouseEvent(b"hover", 35, 20, 1, True),
        source,
    )
    assert selection.segments() == (
        (0, 11, b"/home/user/project/"),
    )


def test_local_text_selection_recognizes_wrapped_url_from_second_row():
    route = HistorySnapshot(1, "%8", 0, 0, 18, 2)
    source = SelectionSource(
        route,
        (b"Visit https://exam", b"ple.test/docs     "),
        0,
    )
    selection = LocalTextSelection()
    hover = SgrMouseEvent(b"hover", 35, 3, 2, True)

    assert selection.hover(hover, source)
    assert selection.segments() == (
        (0, 6, b"https://exam"),
        (1, 0, b"ple.test/docs"),
    )

    selection.pointer_event(SgrMouseEvent(b"down", 0, 3, 2, True), source)
    action = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 3, 2, False),
        source,
    )
    assert action.open_target is not None
    assert action.open_target.value == "https://example.test/docs"


def test_local_text_selection_resolves_all_three_soft_wrapped_url_rows():
    route = HistorySnapshot(1, "%8", 0, 0, 67, 3)
    source = SelectionSource(
        route,
        (
            b"    https://github.com/NVIDIA/TensorRT-LLM/blob/"
            b"746e43a80b418b2e521",
            b"38846b4789dd6e49f8466/tests/unittest/_torch/visual_gen/"
            b"multi_gpu/te",
            b"st_parallel_conv.py#L159-L271",
        ),
        0,
    )
    expected = (
        "https://github.com/NVIDIA/TensorRT-LLM/blob/"
        "746e43a80b418b2e52138846b4789dd6e49f8466/tests/unittest/"
        "_torch/visual_gen/multi_gpu/test_parallel_conv.py#L159-L271"
    )
    expected_segments = (
        (0, 4, source.rows[0][4:]),
        (1, 0, source.rows[1]),
        (2, 0, source.rows[2]),
    )

    for row, column in ((1, 10), (2, 10), (3, 10)):
        selection = LocalTextSelection()
        assert selection.hover(
            SgrMouseEvent(b"hover", 35, column, row, True),
            source,
        )
        assert selection.segments() == expected_segments
        selection.pointer_event(
            SgrMouseEvent(b"down", 0, column, row, True),
            source,
        )
        action = selection.pointer_event(
            SgrMouseEvent(b"up", 0, column, row, False),
            source,
        )
        assert action.open_target is not None
        assert action.open_target.value == expected


def test_local_text_selection_strips_label_before_absolute_path():
    route = HistorySnapshot(1, "%8", 0, 0, 40, 1)
    source = SelectionSource(
        route,
        (b"failed path:=/home/user/project/file.py",),
        0,
    )
    selection = LocalTextSelection()

    selection.pointer_event(SgrMouseEvent(b"down", 0, 24, 1, True), source)
    action = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 24, 1, False),
        source,
    )

    assert action.open_target is not None
    assert action.open_target.value == "/home/user/project/file.py"
    assert action.open_target.highlight_column == 13


def test_local_text_selection_hovers_semantic_targets_without_opening_them():
    route = HistorySnapshot(1, "%8", 2, 3, 40, 1)
    source = SelectionSource(
        route,
        (b"See https://example.test/docs",),
        0,
    )
    selection = LocalTextSelection()
    hover = SgrMouseEvent(b"hover", 35, 10, 4, True)

    assert hover.is_hover_motion is True
    assert selection.hover(hover, source) is True
    assert selection.segments() == (
        (3, 6, b"https://example.test/docs"),
    )
    assert selection.hover(hover, source) is False

    moved_away = SgrMouseEvent(b"away", 35, 3, 4, True)
    assert selection.hover(moved_away, source) is True
    assert selection.segments() == ()


def test_local_text_selection_click_flash_expires_without_clearing_hover():
    selection = LocalTextSelection()
    target = ClickTarget(
        "url",
        "https://example.test",
        "%8",
        highlight_row=4,
        highlight_column=7,
        highlight_text=b"https://example.test",
    )

    assert selection.flash(target, now=10.0, duration=0.18) is True
    assert selection.clear_expired_flash(10.17) is False
    assert selection.clear_expired_flash(10.18) is True
    assert selection.segments() == ()


def test_local_text_selection_does_not_open_on_drag_or_unfocused_pane():
    route = HistorySnapshot(1, "%8", 0, 0, 40, 1)
    selection = LocalTextSelection()
    source = SelectionSource(route, (b"https://example.test",), 0)
    press = SgrMouseEvent(b"down", 0, 2, 1, True)
    selection.pointer_event(press, source)
    selection.pointer_event(SgrMouseEvent(b"drag", 32, 8, 1, True), None)
    dragged = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 8, 1, False),
        None,
    )
    assert dragged.open_target is None
    assert dragged.copy_data

    unfocused = SelectionSource(
        route,
        (b"https://example.test",),
        0,
        semantic_open=False,
    )
    selection.cancel()
    assert selection.hover(
        SgrMouseEvent(b"hover", 35, 2, 1, True),
        unfocused,
    )
    assert selection.segments() == (
        (0, 0, b"https://example.test"),
    )
    selection.pointer_event(press, unfocused)
    replayed = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 2, 1, False),
        unfocused,
    )
    assert replayed.open_target is None
    assert replayed.replay_events


def test_local_text_selection_preserves_two_remote_click_gestures():
    history = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(history.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(request_id, "%8", 3, 0, 6, 1, (b"Hello!",))
    history.accept_prefetch(HistoryBatch(request_id, (route,)))
    selection = LocalTextSelection()
    forwarded: list[bytes] = []

    for suffix in ("first", "second"):
        press = SgrMouseEvent(f"{suffix}-down".encode(), 0, 4, 1, True)
        release = SgrMouseEvent(f"{suffix}-up".encode(), 0, 4, 1, False)
        source = history.selection_source(press, (b"abcHello!",))
        selection.pointer_event(press, source)
        action = selection.pointer_event(release, source)
        for replay in action.replay_events:
            routed = history.pointer_event(replay)
            forwarded.append(routed.forwarded_input)

    assert forwarded == [
        b"first-down",
        b"first-up",
        b"second-down",
        b"second-up",
    ]


def test_local_text_selection_copies_and_highlights_one_visible_pane():
    route = HistorySnapshot(1, "%8", 3, 0, 6, 1)
    source = SelectionSource(
        route,
        (b"\033[0mabc\033[31mHello!\033[0m",),
        3,
    )
    selection = LocalTextSelection()

    selection.pointer_event(SgrMouseEvent(b"down", 0, 4, 1, True), source)
    drag = selection.pointer_event(SgrMouseEvent(b"drag", 32, 8, 1, True), None)
    release = selection.pointer_event(SgrMouseEvent(b"up", 0, 8, 1, False), None)

    assert drag == SelectionAction(handled=True, repaint=True)
    assert release.copy_data == b"Hello"
    assert selection.segments() == ((0, 3, b"Hello"),)
    assert selection.active is True


def test_local_text_selection_clamps_drag_and_handles_wide_characters():
    route = HistorySnapshot(1, "%8", 10, 4, 5, 2)
    source = SelectionSource(
        route,
        (
            "\033[0m你ab \033[0m".encode(),
            "\033[0mcd   \033[0m".encode(),
        ),
        0,
    )
    selection = LocalTextSelection()

    # Start on the continuation cell of 你, then drag beyond this pane.
    selection.pointer_event(SgrMouseEvent(b"down", 0, 12, 5, True), source)
    selection.pointer_event(SgrMouseEvent(b"drag", 32, 99, 99, True), None)
    action = selection.pointer_event(SgrMouseEvent(b"up", 0, 99, 99, False), None)

    assert action.copy_data == "你ab\ncd".encode()
    assert selection.segments() == (
        (4, 10, "你ab ".encode()),
        (5, 10, b"cd   "),
    )


def test_local_text_selection_cancels_when_pane_geometry_changes():
    route = HistorySnapshot(1, "%8", 10, 4, 5, 2)
    source = SelectionSource(route, (b"first", b"second"), 0)
    selection = LocalTextSelection()
    selection.pointer_event(SgrMouseEvent(b"down", 0, 11, 5, True), source)
    selection.pointer_event(SgrMouseEvent(b"drag", 32, 12, 5, True), None)

    changed = replace(route, width=6)
    assert selection.validate_routes((changed,)) is True
    assert selection.active is False
    assert selection.capturing is False


def test_local_text_selection_uses_the_displayed_history_viewport():
    history = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(history.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(
        request_id,
        "%8",
        4,
        1,
        6,
        2,
        (b"old-0", b"old-1", b"old-2"),
    )
    history.accept_prefetch(HistoryBatch(request_id, (route,)))
    history.wheel(SgrMouseEvent(b"wheel", 64, 5, 2, True))

    source = history.selection_source(
        SgrMouseEvent(b"down", 0, 5, 2, True),
        (b"live-0", b"live-1", b"live-2"),
    )

    assert source is not None
    assert source.rows == (b"old-0", b"old-1")
    assert source.row_x_offset == 0


def test_surface_paints_local_selection_after_remote_styled_rows():
    output = io.BytesIO()
    surface = TerminalSurface(output, mouse=False)
    screen = AppliedScreen(
        width=8,
        height=2,
        cursor_x=0,
        cursor_y=0,
        cursor_visible=False,
        terminal_modes=TerminalMode.NONE,
        rows=(b"\033[31mHello", b"world"),
        changed_rows=(0, 1),
        clear=True,
    )

    surface.paint(screen, selection=((0, 0, b"Hel"),))

    painted = output.getvalue()
    assert painted.index(b"\033[31mHello") < painted.index(b"\033[0;7mHel")


def test_remote_osc52_decoder_is_chunked_bounded_and_fail_closed():
    decoder = fast_display_server._Osc52ClipboardDecoder()
    payload = base64.b64encode("Session title".encode())

    assert decoder.feed(b"noise\033]52;c;" + payload[:4]) == ()
    assert decoder.feed(payload[4:] + b"\007tail") == (b"Session title",)
    assert decoder.feed(b"\033]52;c;not base64!\007") == ()


def test_history_choice_capability_requires_available_non_backed_transcript():
    with pytest.raises(ValueError, match="history capabilities"):
        encode_history_snapshot(
            HistorySnapshot(
                1,
                "%8",
                0,
                0,
                10,
                2,
                (b"", b""),
                history_choice_required=True,
            )
        )
    with pytest.raises(ValueError, match="history capabilities"):
        encode_history_snapshot(
            HistorySnapshot(
                1,
                "%8",
                0,
                0,
                10,
                2,
                (b"", b""),
                transcript_backed=True,
                transcript_available=True,
                history_choice_required=True,
            )
        )


def test_v10_history_prefetch_batch_round_trip_is_atomic_and_bounded():
    decoder = InputFrameDecoder()
    request = decoder.feed(encode_history_prefetch(17, 300))[0]
    assert request.kind is InputKind.PREFETCH_HISTORY
    assert decode_history_prefetch(request.data) == (17, 300)

    snapshots = (
        HistorySnapshot(17, "%8", 31, 0, 49, 2, (b"a", b"b", b"c")),
        HistorySnapshot(17, "%9", 31, 3, 49, 2, (b"d", b"e", b"f"), True),
    )
    batch = HistoryBatch(17, snapshots)

    assert ServerMessageDecoder().feed(encode_history_batch(batch)) == [batch]
    with pytest.raises(ValueError):
        encode_history_prefetch(1, 301)


def test_client_decoder_recovers_from_false_marker_and_reads_patch():
    false = DISPLAY_MAGIC + struct.pack(">I", 1) + b"x"
    patch = ScreenUpdate(UpdateKind.PATCH, 2, 4, 2, 2, 1, False, ((1, b"changed"),))

    updates = ClientScreenUpdateDecoder().feed(false + encode_update(patch))

    assert len(updates) == 1
    assert updates[0].kind is ClientUpdateKind.PATCH
    assert updates[0].rows == ((1, b"changed"),)


def test_input_protocol_decodes_bytes_resize_and_keyframe_request():
    decoder = InputFrameDecoder()
    packet = b"".join(
        (
            encode_client_input(b"one"),
            encode_client_resize(120, 40),
            encode_client_keyframe_request(),
            encode_heartbeat(),
        )
    )

    assert decoder.feed(packet[:5]) == []
    messages = decoder.feed(packet[5:])

    assert [(message.kind, message.data) for message in messages] == [
        (InputKind.BYTES, b"one"),
        (InputKind.RESIZE, struct.pack(">HH", 120, 40)),
        (InputKind.REQUEST_KEYFRAME, b""),
        (InputKind.HEARTBEAT, b""),
    ]
    with pytest.raises(ValueError):
        encode_client_input(b"")
    with pytest.raises(ValueError):
        encode_client_resize(39, 40)


def test_screen_model_applies_patch_and_rejects_gap_or_wrong_geometry():
    decoder = ClientScreenUpdateDecoder()
    model = ScreenModel()
    size = os.terminal_size((4, 2))
    keyframe = decoder.feed(encode_update(_keyframe()))[0]
    first = model.apply(keyframe, size)
    assert first is not None
    assert first.clear is True
    assert first.rows == (b"row-0", b"row-1")

    patch = ScreenUpdate(UpdateKind.PATCH, 2, 4, 2, 3, 1, True, ((1, b"latest"),))
    applied = model.apply(decoder.feed(encode_update(patch))[0], size)
    assert applied is not None
    assert applied.clear is False
    assert applied.changed_rows == (1,)
    assert applied.rows == (b"row-0", b"latest")

    gap = ScreenUpdate(UpdateKind.PATCH, 4, 4, 2, 0, 0, True, ())
    assert model.apply(decoder.feed(encode_update(gap))[0], size) is None
    assert model.apply(keyframe, os.terminal_size((5, 2))) is None


def test_terminal_surface_paints_only_changed_patch_rows_and_restores_mouse():
    decoder = ClientScreenUpdateDecoder()
    model = ScreenModel()
    size = os.terminal_size((4, 2))
    model.apply(decoder.feed(encode_update(_keyframe()))[0], size)
    patch = ScreenUpdate(UpdateKind.PATCH, 2, 4, 2, 1, 1, True, ((1, b"changed"),))
    applied = model.apply(decoder.feed(encode_update(patch))[0], size)
    assert applied is not None
    stream = io.BytesIO()
    surface = TerminalSurface(stream)

    surface.paint(applied)
    surface.close()

    rendered = stream.getvalue()
    assert b"\033[?1003h\033[?1006h" in rendered
    assert b"\033[?1003l\033[?1006l" in rendered
    assert b"\033[2;1H\033[2Kchanged" in rendered
    assert b"\033[1;1H" not in rendered
    assert b"\033[2J" in rendered  # alternate-screen initialization only


def test_terminal_surface_can_leave_mouse_to_the_local_terminal():
    stream = io.BytesIO()
    surface = TerminalSurface(stream, mouse=False)

    surface.start()
    surface.close()

    assert b"?1003" not in stream.getvalue()
    assert b"?1006" not in stream.getvalue()


def test_terminal_surface_can_temporarily_yield_and_restore_mouse():
    stream = io.BytesIO()
    surface = TerminalSurface(stream)

    surface.start()
    surface.suspend_mouse()
    surface.start()
    suspended = stream.getvalue()
    surface.resume_mouse()
    restored = stream.getvalue()

    assert suspended.count(b"\033[?1003h\033[?1006h") == 1
    assert suspended.endswith(b"\033[?1003l\033[?1006l")
    assert restored.endswith(b"\033[?1003h\033[?1006h")


def test_terminal_surface_reasserts_mouse_after_termux_keyboard_closes():
    stream = io.BytesIO()
    surface = TerminalSurface(stream, mouse_hover=False)

    surface.start()
    surface.suspend_mouse()
    surface.resume_mouse()
    stream.seek(0)
    stream.truncate()
    surface.resume_mouse(reassert=True)

    assert stream.getvalue() == (
        b"\033[?1002l\033[?1006l\033[?1002h\033[?1006h"
    )
    assert surface.mouse_active
    assert not surface.mouse_suspended


def test_terminal_surface_termux_route_never_requests_unsupported_any_event_mode():
    stream = io.BytesIO()
    surface = TerminalSurface(stream, mouse_hover=False)

    surface.start()
    surface.suspend_mouse()
    surface.resume_mouse()
    surface.close()

    rendered = stream.getvalue()
    assert rendered.count(b"\033[?1002h\033[?1006h") == 2
    assert rendered.count(b"\033[?1002l\033[?1006l") == 2
    assert b"?1003" not in rendered


def test_termux_detection_uses_local_environment_not_terminal_geometry():
    assert is_termux_environment({"TERMUX_VERSION": "0.118"})
    assert is_termux_environment({"PREFIX": "/data/data/com.termux/files/usr/"})
    assert not is_termux_environment({"TERM": "xterm-256color", "COLUMNS": "40"})


def test_termux_prompt_tap_yields_mouse_until_keyboard_input():
    touch = TermuxTouchKeyboard(enabled=True, timeout=10.0)
    press = SgrMouseEvent(b"press", 0, 40, 22, True)
    release = SgrMouseEvent(b"release", 0, 40, 22, False)

    action = touch.pointer_event(
        press,
        clicked_pane_id="%8",
        cursor_pane_id="%8",
        cursor_y=21,
        pane_frozen=False,
        now=5.0,
    )

    assert action.handled and action.suspend_mouse and action.show_hint
    assert touch.active
    assert touch.owns_local_focus
    assert touch.consumes_focus_out(b"\033[O")
    assert not touch.consumes_focus_out(b"\033[I")
    assert touch.pointer_event(
        release,
        clicked_pane_id="%8",
        cursor_pane_id="%8",
        cursor_y=21,
        pane_frozen=False,
        now=5.0,
    ).handled
    assert touch.keyboard_input()
    assert not touch.active
    assert not touch.owns_local_focus
    assert not touch.consumes_focus_out(b"\033[O")


def test_termux_live_route_tap_yields_mouse_when_provider_hides_dec_cursor():
    history = LocalHistoryView()
    assert history.begin_prefetch(1.0) is not None
    request_id = history.prefetch_pending_id
    assert request_id is not None
    route = HistorySnapshot(
        request_id,
        "%8",
        30,
        0,
        50,
        24,
        (b"",) * 24,
    )
    history.accept_prefetch(HistoryBatch(request_id, (route,)))
    screen = AppliedScreen(
        width=80,
        height=24,
        cursor_x=40,
        cursor_y=21,
        cursor_visible=False,
        terminal_modes=TerminalMode.FOCUS_EVENTS,
        rows=(b"",) * 24,
        changed_rows=(),
        clear=False,
    )
    touch = TermuxTouchKeyboard(enabled=True)

    action = termux_prompt_touch_action(
        touch,
        SgrMouseEvent(b"press", 0, 41, 22, True),
        history,
        screen,
        now=5.0,
    )

    stream = io.BytesIO()
    surface = TerminalSurface(stream, mouse_hover=False)
    surface.start()
    if action.suspend_mouse:
        surface.suspend_mouse()
    assert action.handled and action.show_hint
    assert stream.getvalue().endswith(b"\033[?1002l\033[?1006l")


def test_termux_focus_reassertion_requires_remote_focus_events():
    screen = AppliedScreen(
        width=20,
        height=4,
        cursor_x=2,
        cursor_y=2,
        cursor_visible=False,
        terminal_modes=TerminalMode.FOCUS_EVENTS,
        rows=(b"one", b"two", b"three", b"four"),
        changed_rows=(),
        clear=False,
    )

    frame = focus_in_frame_for_screen(screen)
    assert frame is not None
    decoded = InputFrameDecoder().feed(frame)
    assert len(decoded) == 1
    assert decoded[0].data == b"\033[I"
    assert focus_in_frame_for_screen(
        replace(screen, terminal_modes=TerminalMode.NONE)
    ) is None
    assert focus_in_frame_for_screen(None) is None


def test_termux_prompt_tap_restores_mouse_as_keyboard_projection_opens():
    press = SgrMouseEvent(b"press", 0, 40, 22, True)
    desktop = TermuxTouchKeyboard(enabled=False, timeout=10.0)
    assert not desktop.pointer_event(
        press,
        clicked_pane_id="%8",
        cursor_pane_id="%8",
        cursor_y=21,
        pane_frozen=False,
        now=5.0,
    ).handled

    touch = TermuxTouchKeyboard(enabled=True, timeout=10.0)
    for clicked, cursor, frozen, y in (
        ("%9", "%8", False, 21),
        ("%8", "%8", True, 21),
        ("%8", "%8", False, 18),
    ):
        assert not touch.pointer_event(
            press,
            clicked_pane_id=clicked,
            cursor_pane_id=cursor,
            cursor_y=y,
            pane_frozen=frozen,
            now=5.0,
        ).handled

    touch.pointer_event(
        press,
        clicked_pane_id="%8",
        cursor_pane_id="%8",
        cursor_y=21,
        pane_frozen=False,
        now=5.0,
    )
    assert not touch.expire(14.9)
    assert touch.observe_projection(True, now=15.0)
    assert not touch.observe_projection(True, now=15.1)
    # If disabling mouse tracking hid the initiating release, the first
    # restored Railmux click must still retain both its press and release.
    restored_press = SgrMouseEvent(b"restored-press", 0, 20, 12, True)
    restored_release = SgrMouseEvent(b"restored-release", 0, 20, 12, False)
    assert not touch.pointer_event(
        restored_press,
        clicked_pane_id="%8",
        cursor_pane_id="%8",
        cursor_y=21,
        pane_frozen=False,
        now=15.2,
    ).handled
    assert not touch.pointer_event(
        restored_release,
        clicked_pane_id="%8",
        cursor_pane_id="%8",
        cursor_y=21,
        pane_frozen=False,
        now=15.2,
    ).handled
    assert not touch.keyboard_input()
    assert touch.active
    assert touch.keyboard_projected
    assert touch.observe_projection(False)
    assert not touch.active
    assert not touch.keyboard_projected


def test_termux_compact_navigation_never_yields_touch_to_soft_keyboard():
    touch = TermuxTouchKeyboard(enabled=True, timeout=10.0)
    press = SgrMouseEvent(b"press", 0, 4, 22, True)

    action = touch.pointer_event(
        press,
        clicked_pane_id="%8",
        cursor_pane_id="%8",
        cursor_y=21,
        pane_frozen=False,
        navigation_row=22,
        now=5.0,
    )

    assert not action.handled
    assert not action.suspend_mouse
    assert not touch.active


def test_termux_keyboard_projection_state_has_a_bounded_fallback():
    touch = TermuxTouchKeyboard(enabled=True, timeout=10.0)
    touch.pointer_event(
        SgrMouseEvent(b"press", 0, 40, 22, True),
        clicked_pane_id="%8",
        cursor_pane_id="%8",
        cursor_y=21,
        pane_frozen=False,
        now=5.0,
    )

    assert touch.observe_projection(True, now=6.0)
    assert not touch.expire(15.9)
    assert touch.expire(16.0)
    assert not touch.active
    # Expiration restores mouse ownership immediately but retains no timer or
    # input capture. A later close resize still requests the Termux mode
    # reassertion that was missing from the original implementation.
    assert touch.keyboard_projected
    assert touch.observe_projection(False)
    assert not touch.keyboard_projected


def test_termux_keyboard_close_queues_one_delayed_mouse_reassert():
    touch = TermuxTouchKeyboard(
        enabled=True,
        timeout=10.0,
        close_reassert_delay=0.15,
    )
    touch.pointer_event(
        SgrMouseEvent(b"press", 0, 40, 22, True),
        clicked_pane_id="%8",
        cursor_pane_id="%8",
        cursor_y=21,
        pane_frozen=False,
        now=5.0,
    )
    assert touch.observe_projection(True, now=6.0)

    assert touch.observe_projection(False, now=20.0)
    assert not touch.post_close_reassert_due(20.149)
    assert touch.post_close_reassert_due(20.15)
    assert not touch.post_close_reassert_due(20.16)


def test_termux_rapid_keyboard_reopen_cancels_delayed_reassert():
    touch = TermuxTouchKeyboard(
        enabled=True,
        close_reassert_delay=0.15,
    )
    press = SgrMouseEvent(b"press", 0, 40, 22, True)
    fields = {
        "clicked_pane_id": "%8",
        "cursor_pane_id": "%8",
        "cursor_y": 21,
        "pane_frozen": False,
    }
    touch.pointer_event(press, now=5.0, **fields)
    assert touch.observe_projection(True, now=6.0)
    assert touch.observe_projection(False, now=7.0)

    assert touch.pointer_event(press, now=7.05, **fields).handled
    assert not touch.post_close_reassert_due(7.2)


def test_termux_prompt_tap_times_out_when_keyboard_does_not_open():
    touch = TermuxTouchKeyboard(enabled=True, timeout=10.0)
    touch.pointer_event(
        SgrMouseEvent(b"press", 0, 40, 22, True),
        clicked_pane_id="%8",
        cursor_pane_id="%8",
        cursor_y=21,
        pane_frozen=False,
        now=5.0,
    )

    assert not touch.expire(14.9)
    assert touch.expire(15.0)
    assert not touch.active


def test_terminal_surface_paints_only_the_local_history_pane_rectangle():
    stream = io.BytesIO()
    surface = TerminalSurface(stream)
    snapshot = HistorySnapshot(
        1, "%9", x=10, y=2, width=8, height=2, lines=(b"one", b"two")
    )
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(encode_update(_keyframe(width=20, height=5)))[
            0
        ],
        os.terminal_size((20, 5)),
    )
    assert screen is not None

    surface.paint_overlays(screen, ((snapshot, (b"one", "你好long".encode())),))
    surface.close()

    rendered = stream.getvalue()
    assert b"\033[3;11H\033[8Xone" in rendered
    assert b"\033[4;11H\033[8X" + "你好long".encode() in rendered
    assert b"\033[3;1H" not in rendered


def test_terminal_surface_composites_live_rows_then_multiple_frozen_panes():
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(encode_update(_keyframe(width=12, height=3)))[
            0
        ],
        os.terminal_size((12, 3)),
    )
    assert screen is not None
    left = HistorySnapshot(1, "%8", 2, 0, 3, 2, ())
    right = HistorySnapshot(1, "%9", 7, 1, 3, 2, ())
    stream = io.BytesIO()

    TerminalSurface(stream).paint(
        screen,
        ((left, (b"a0", b"a1")), (right, (b"b0", b"b1"))),
    )

    rendered = stream.getvalue()
    live_row = rendered.index(b"\033[1;1H\033[2Krow-0")
    left_overlay = rendered.index(b"\033[1;3H\033[3Xa0")
    right_overlay = rendered.index(b"\033[2;8H\033[3Xb0")
    cursor = rendered.rindex(b"\033[1;2H")
    assert live_row < left_overlay < cursor
    assert live_row < right_overlay < cursor
    assert rendered.endswith(b"\033[?25h")


def test_terminal_surface_hides_cursor_covered_by_a_frozen_pane():
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(encode_update(_keyframe(width=12, height=3)))[
            0
        ],
        os.terminal_size((12, 3)),
    )
    assert screen is not None
    covering = HistorySnapshot(1, "%8", 0, 0, 4, 2, ())
    stream = io.BytesIO()

    TerminalSurface(stream).paint(screen, ((covering, (b"a", b"b")),))

    rendered = stream.getvalue()
    assert rendered.endswith(b"\033[1;2H")
    assert rendered.count(b"\033[?25l") == 1


def test_terminal_surface_reasserts_cached_cursor_without_repainting():
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(
            encode_update(_keyframe(width=12, height=3))
        )[0],
        os.terminal_size((12, 3)),
    )
    assert screen is not None
    screen = replace(screen, cursor_x=4, cursor_y=1, cursor_visible=True)
    stream = io.BytesIO()
    surface = TerminalSurface(stream, mouse_hover=False)
    surface.paint(screen)
    stream.seek(0)
    stream.truncate()

    surface.reassert_cursor()

    assert stream.getvalue() == b"\033[0m\033[?7h\033[2;5H\033[?25h"


def test_terminal_surface_local_highlight_repaints_only_changed_rows():
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(
            encode_update(_keyframe(width=20, height=4))
        )[0],
        os.terminal_size((20, 4)),
    )
    assert screen is not None
    stream = io.BytesIO()
    surface = TerminalSurface(stream, mouse_hover=False)
    surface.paint(screen)
    stream.seek(0)
    stream.truncate()

    surface.paint_changed_rows(
        screen,
        (),
        ((1, 4, b"https://example"),),
        {1},
    )

    rendered = stream.getvalue()
    assert b"\033[2J" not in rendered
    assert b"\033[2;1H\033[2Krow-1" in rendered
    assert b"\033[2;5H\033[0;7mhttps://example" in rendered


def test_terminal_surface_stable_cursor_visibility_is_not_reasserted_per_patch():
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(
            encode_update(_keyframe(width=12, height=3))
        )[0],
        os.terminal_size((12, 3)),
    )
    assert screen is not None
    stream = io.BytesIO()
    surface = TerminalSurface(stream, mouse_hover=False)
    surface.paint(screen)
    stream.seek(0)
    stream.truncate()

    surface.paint(replace(screen, changed_rows=(1,), clear=False))

    rendered = stream.getvalue()
    assert b"\033[?25h" not in rendered
    assert b"\033[?25l" not in rendered
    assert rendered.endswith(b"\033[1;2H")
    assert b"\033[2J" not in stream.getvalue()


def test_terminal_surface_projects_short_local_viewport_from_logical_bottom():
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(
            encode_update(_keyframe(width=105, height=22))
        )[0],
        os.terminal_size((105, 22)),
    )
    assert screen is not None
    screen = replace(screen, cursor_x=7, cursor_y=20, clear=True)
    stream = io.BytesIO()
    surface = TerminalSurface(stream)
    surface.set_physical_size(os.terminal_size((105, 4)))

    surface.paint(screen)

    rendered = stream.getvalue()
    assert b"\033[1;1H\033[2Krow-18" in rendered
    assert b"\033[2;1H\033[2Krow-19" in rendered
    assert b"\033[3;1H\033[2Krow-20" in rendered
    assert b"\033[4;1H\033[2Krow-21" in rendered
    assert b"row-17" not in rendered
    assert rendered.endswith(b"\033[3;8H\033[?25h")


def test_terminal_surface_clips_projected_patches_overlays_and_cursor():
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(encode_update(_keyframe(width=20, height=15)))[
            0
        ],
        os.terminal_size((20, 15)),
    )
    assert screen is not None
    screen = replace(
        screen, cursor_y=2, changed_rows=(2, 11, 14), clear=False
    )
    overlay = HistorySnapshot(
        1,
        "%9",
        x=3,
        y=10,
        width=6,
        height=4,
        lines=(b"hidden", b"one", b"two", b"three"),
    )
    stream = io.BytesIO()
    surface = TerminalSurface(stream)
    surface.set_physical_size(os.terminal_size((20, 4)))

    surface.paint(screen, ((overlay, overlay.lines),))

    rendered = stream.getvalue()
    assert b"row-2" not in rendered
    assert b"\033[1;1H\033[2Krow-11" in rendered
    assert b"\033[4;1H\033[2Krow-14" in rendered
    assert b"hidden" not in rendered
    assert b"\033[1;4H\033[6Xone" in rendered
    assert rendered.endswith(b"\033[1;1H")
    assert rendered.count(b"\033[?25l") == 1


def test_terminal_surface_maps_projected_mouse_rows_to_logical_screen():
    surface = TerminalSurface(io.BytesIO())
    surface.set_physical_size(os.terminal_size((105, 4)))

    top = surface.translate_mouse_event(
        SgrMouseEvent(b"\x1b[<64;8;1M", 64, 8, 1, True),
        logical_height=22,
    )
    status = surface.translate_mouse_event(
        SgrMouseEvent(b"\x1b[<0;8;4m", 0, 8, 4, False),
        logical_height=22,
    )

    assert (top.x, top.y, top.raw) == (8, 19, b"\x1b[<64;8;19M")
    assert (status.x, status.y, status.raw) == (8, 22, b"\x1b[<0;8;22m")


def test_compact_status_row_finds_styled_top_or_bottom_bar():
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(encode_update(_keyframe(width=40, height=4)))[
            0
        ],
        os.terminal_size((40, 4)),
    )
    assert screen is not None
    compact = b"\x1b[0;38;5;0m[Railmux][A1][A2] Codex "

    assert (
        fast_display_client.compact_status_row(
            replace(
                screen,
                rows=(compact, b"", b"", b""),
            )
        )
        == 1
    )
    assert (
        fast_display_client.compact_status_row(
            replace(
                screen,
                rows=(b"", b"", b"", compact),
            )
        )
        == 4
    )


def test_status_row_click_bypasses_stale_local_pointer_capture():
    view = LocalHistoryView()
    view._local_pointer_capture = True
    view.visible_routes = (
        HistorySnapshot(
            1,
            "%9",
            x=0,
            y=0,
            width=80,
            height=24,
            lines=(),
        ),
    )
    event = SgrMouseEvent(b"\x1b[<0;2;24M", 0, 2, 24, True)

    action = view.pointer_event(event, "%9", status_row=24)

    assert action.forwarded_input == event.raw
    assert action.refresh_routes is True
    assert view._local_pointer_capture is False
    assert view.visible_routes == ()


def test_status_row_wheels_forward_without_cancelling_agent_history():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(
        request_id,
        "%9",
        x=30,
        y=0,
        width=50,
        height=3,
        lines=(b"old", b"one", b"two", b"three"),
    )
    view.accept_prefetch(HistoryBatch(request_id, (route,)))
    view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))

    vertical = SgrMouseEvent(b"vertical", 64, 2, 24, True)
    horizontal = SgrMouseEvent(b"horizontal", 66, 2, 24, True)

    assert view.pointer_event(vertical, "%9", status_row=24) == HistoryAction(
        forwarded_input=vertical.raw
    )
    assert view.pointer_event(horizontal, "%9", status_row=24) == HistoryAction(
        forwarded_input=horizontal.raw
    )
    assert view.active is True
    assert view.visible_routes == (route,)


def test_resize_notifies_private_tmux_client_process_group(monkeypatch):
    set_size = MagicMock()
    notify = MagicMock()
    monkeypatch.setattr(fast_display_server, "_set_winsize", set_size)
    monkeypatch.setattr(fast_display_server.os, "killpg", notify)

    fast_display_server._resize_tmux_client(123, 9, 70, 18)

    set_size.assert_called_once_with(9, 70, 18)
    notify.assert_called_once_with(123, signal.SIGWINCH)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("tmux 3.4\n", ("-T", "RGB")),
        ("tmux 3.2a\n", ("-T", "RGB")),
        ("tmux 3.1c\n", ()),
        ("tmux 2.7\n", ()),
        ("unexpected\n", ()),
    ],
)
def test_private_tmux_client_enables_rgb_only_when_supported(
    monkeypatch,
    version,
    expected,
):
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "check_output",
        lambda *_args, **_kwargs: version,
    )
    fast_display_server._tmux_client_feature_args.cache_clear()
    try:
        assert fast_display_server._tmux_client_feature_args() == expected
    finally:
        fast_display_server._tmux_client_feature_args.cache_clear()


def test_mode_only_patch_reconciles_terminal_modes_once_and_restores_them():
    decoder = ClientScreenUpdateDecoder()
    model = ScreenModel()
    size = os.terminal_size((4, 2))
    first = model.apply(decoder.feed(encode_update(_keyframe()))[0], size)
    assert first is not None
    requested = TerminalMode.BRACKETED_PASTE | TerminalMode.FOCUS_EVENTS
    patch = ScreenUpdate(
        UpdateKind.PATCH,
        2,
        4,
        2,
        1,
        0,
        True,
        (),
        requested,
    )
    applied = model.apply(decoder.feed(encode_update(patch))[0], size)
    assert applied is not None
    assert applied.changed_rows == ()

    stream = io.BytesIO()
    surface = TerminalSurface(stream)
    assert surface.paint(first) is False
    assert surface.paint(applied) is True
    assert surface.paint(applied) is False
    surface.close()

    rendered = stream.getvalue()
    assert rendered.count(b"\033[?2004h") == 1
    assert rendered.count(b"\033[?1004h") == 1
    assert rendered.count(b"\033[?2004l") == 1
    assert rendered.count(b"\033[?1004l") == 1
    assert rendered.index(b"\033[?2004l") < rendered.index(b"\033[?1049l")
    assert rendered.index(b"\033[?1004l") < rendered.index(b"\033[?1049l")


def test_reconnect_releases_and_rearms_remote_input_modes():
    requested = TerminalMode.BRACKETED_PASTE | TerminalMode.FOCUS_EVENTS
    screen = AppliedScreen(
        width=4,
        height=2,
        cursor_x=1,
        cursor_y=0,
        cursor_visible=True,
        terminal_modes=requested,
        rows=(b"one", b"two"),
        changed_rows=(0, 1),
        clear=True,
    )
    stream = io.BytesIO()
    surface = TerminalSurface(stream)

    assert surface.paint(screen) is True
    surface.show_local_status("connection lost")
    surface.begin_reconnect()
    assert surface.terminal_modes is TerminalMode.NONE
    assert surface._local_status_text is None
    assert surface._last_screen is None
    assert surface._reconnect_status_screen is screen
    reconnect_rendered = stream.getvalue()
    assert reconnect_rendered.count(b"\033[?2004l") == 1
    assert reconnect_rendered.count(b"\033[?1004l") == 1
    surface.show_local_status("Reconnected; waiting for a fresh screen")
    stream.seek(0)
    stream.truncate()
    assert surface.paint(screen) is True
    assert surface._local_status_text is None
    assert surface._reconnect_status_screen is None

    rendered = stream.getvalue()
    assert rendered.count(b"\033[?2004h") == 1
    assert rendered.count(b"\033[?1004h") == 1
    assert b"\033[?2004l" not in rendered
    assert b"\033[?1004l" not in rendered
    assert b"Reconnected; waiting" not in rendered
    assert rendered.endswith(b"\033[1;2H\033[?25h")


def test_bottom_right_local_status_cannot_leave_a_pending_wrap():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((12, 4)))

    surface.show_local_status("exactly-12ch")

    painted = output.getvalue()
    text = painted.index(b"exactly-12ch")
    assert painted.rfind(b"\033[?7l", 0, text) >= 0
    assert painted.find(b"\033[?7h", text) > text


def test_ctrl_right_bracket_is_consumed_locally_with_trailing_data():
    forwarded, should_exit = split_local_escape(b"before" + LOCAL_ESCAPE + b"after")

    assert forwarded == b"before"
    assert should_exit is True
    assert split_local_escape(b"ordinary") == (b"ordinary", False)


def test_initial_terminal_size_waits_for_soft_keyboard_to_close(
    monkeypatch,
    capsys,
):
    sizes = iter(
        (
            os.terminal_size((105, 4)),
            os.terminal_size((105, 4)),
            os.terminal_size((105, 22)),
        )
    )
    monkeypatch.setattr(
        fast_display_client.os, "get_terminal_size", lambda _fd: next(sizes)
    )
    sleep = MagicMock()
    monkeypatch.setattr(fast_display_client.time, "sleep", sleep)

    size = fast_display_client.wait_for_usable_terminal_size(9)

    assert size == os.terminal_size((105, 22))
    assert sleep.call_count == 2
    error = capsys.readouterr().err
    assert "reports 105x4" in error
    assert "at least 40x12" in error
    assert "now 105x22" in error


def test_initial_terminal_size_rejects_a_terminal_that_is_too_narrow(
    monkeypatch,
):
    monkeypatch.setattr(
        fast_display_client.os,
        "get_terminal_size",
        lambda _fd: os.terminal_size((30, 50)),
    )
    sleep = MagicMock()
    monkeypatch.setattr(fast_display_client.time, "sleep", sleep)

    with pytest.raises(
        fast_display_client.ProbeError,
        match=r"30x50.*at least 40x12",
    ):
        fast_display_client.wait_for_usable_terminal_size(9)

    sleep.assert_not_called()


def test_ssh_paints_startup_surface_before_remote_preflight(monkeypatch):
    events = []
    stdin = MagicMock()
    stdout = MagicMock()
    stdin.isatty.return_value = True
    stdout.isatty.return_value = True
    stdout.fileno.return_value = 9
    monkeypatch.setattr(fast_display_client.sys, "stdin", stdin)
    monkeypatch.setattr(fast_display_client.sys, "stdout", stdout)
    monkeypatch.setattr(fast_display_client, "load_config", lambda: MagicMock())
    monkeypatch.setattr(
        fast_display_client.shutil, "which", lambda _name: "/usr/bin/ssh"
    )
    monkeypatch.setattr(
        fast_display_client,
        "wait_for_usable_terminal_size",
        lambda _fd: os.terminal_size((105, 22)),
    )
    monkeypatch.setattr(
        TerminalSurface,
        "show_startup",
        lambda _self, size, detail: events.append(("surface", size, detail)),
    )
    monkeypatch.setattr(
        TerminalSurface,
        "close",
        lambda _self: events.append(("close", None)),
    )

    def fail_after_surface(
        _args,
        size,
        *,
        before_interaction,
        before_local_restart,
        on_stage,
    ):
        events.append(("preflight", size))
        assert callable(before_interaction)
        assert callable(before_local_restart)
        assert callable(on_stage)
        raise fast_display_client.ProbeError("stop")

    monkeypatch.setattr(
        fast_display_client, "prepare_remote_process", fail_after_surface
    )

    with pytest.raises(fast_display_client.ProbeError, match="stop"):
        fast_display_client.run(parse_client_args(["server"]))

    assert events == [
        (
            "surface",
            os.terminal_size((105, 22)),
            "Connecting to remote host…",
        ),
        ("preflight", os.terminal_size((105, 22))),
        ("close", None),
    ]


def test_ssh_missing_client_fails_before_terminal_display(monkeypatch):
    stdin = MagicMock()
    stdout = MagicMock()
    stdin.isatty.return_value = True
    stdout.isatty.return_value = True
    recorder = MagicMock()
    wait_for_size = MagicMock()
    monkeypatch.setattr(fast_display_client.sys, "stdin", stdin)
    monkeypatch.setattr(fast_display_client.sys, "stdout", stdout)
    monkeypatch.setattr(fast_display_client, "load_config", lambda: MagicMock())
    monkeypatch.setattr(fast_display_client.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        fast_display_client, "SshDisplayRecorder", lambda *_args: recorder
    )
    monkeypatch.setattr(
        fast_display_client, "wait_for_usable_terminal_size", wait_for_size
    )

    with pytest.raises(
        fast_display_client.ProbeError,
        match="ssh is not installed or not on PATH",
    ):
        fast_display_client.run(parse_client_args(["server"]))

    wait_for_size.assert_not_called()
    recorder.finish.assert_called_once()


def test_ctrl_c_during_masked_remote_setup_restores_terminal(
    monkeypatch,
    capsys,
):
    events = []
    stdin = MagicMock()
    stdout = MagicMock()
    stdin.isatty.return_value = True
    stdout.isatty.return_value = True
    stdout.fileno.return_value = 9
    monkeypatch.setattr(fast_display_client.sys, "stdin", stdin)
    monkeypatch.setattr(fast_display_client.sys, "stdout", stdout)
    monkeypatch.setattr(fast_display_client, "load_config", lambda: MagicMock())
    monkeypatch.setattr(
        fast_display_client.shutil, "which", lambda _name: "/usr/bin/ssh"
    )
    monkeypatch.setattr(
        fast_display_client,
        "wait_for_usable_terminal_size",
        lambda _fd: os.terminal_size((105, 22)),
    )
    monkeypatch.setattr(
        TerminalSurface,
        "show_startup",
        lambda _self, _size, _detail: events.append("surface"),
    )
    monkeypatch.setattr(
        TerminalSurface,
        "close",
        lambda _self: events.append("close"),
    )
    monkeypatch.setattr(
        fast_display_client,
        "prepare_remote_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    assert fast_display_client.run(parse_client_args(["server"])) == 130
    assert events == ["surface", "close"]
    assert "cancelled during remote setup" in capsys.readouterr().err


def test_same_width_short_resize_is_only_a_local_projection():
    logical = os.terminal_size((105, 22))

    assert fast_display_client._is_soft_keyboard_projection(
        os.terminal_size((105, 4)), logical
    )
    assert not fast_display_client._is_soft_keyboard_projection(
        os.terminal_size((104, 4)), logical
    )
    assert not fast_display_client._is_soft_keyboard_projection(
        os.terminal_size((105, 12)), logical
    )


def test_explicit_tmux_copy_mode_key_remains_opaque_remote_input():
    decoder = TerminalInputDecoder()

    assert decoder.feed(b"\x02[") == [b"\x02["]


def test_terminal_input_decoder_preserves_order_and_partial_sgr_mouse():
    decoder = TerminalInputDecoder()

    assert decoder.feed(b"key\x1b[") == [b"key"]
    parts = decoder.feed(b"<64;40;12Mtail\x1b[<65;40;12M")

    assert len(parts) == 3
    assert isinstance(parts[0], SgrMouseEvent)
    assert parts[0].wheel_direction == 1
    assert (parts[0].x, parts[0].y) == (40, 12)
    assert parts[1] == b"tail"
    assert isinstance(parts[2], SgrMouseEvent)
    assert parts[2].wheel_direction == -1


def test_terminal_input_decoder_releases_ambiguous_escape_after_short_timeout():
    decoder = TerminalInputDecoder()

    assert decoder.feed(b"\x1b") == []
    assert decoder.flush_pending(delay=0) == [b"\x1b"]
    assert decoder.flush_pending(delay=0) == []


def test_terminal_input_decoder_retains_split_page_key_until_complete():
    decoder = TerminalInputDecoder()

    assert decoder.feed(b"before\x1b[5") == [b"before"]
    assert decoder.feed(b"~after") == [b"\x1b[5~after"]
    assert split_page_key_input(b"\x1b[5~after") == (b"\x1b[5~", b"after")


def test_page_key_input_splits_repeated_keys_and_preserves_order():
    data = b"a\x1b[5~\x1b[6~b"

    assert split_page_key_input(data) == (
        b"a",
        b"\x1b[5~",
        b"\x1b[6~",
        b"b",
    )
    assert page_key_direction(b"\x1b[5~") == 1
    assert page_key_direction(b"\x1b[6~") == -1
    assert page_key_direction(b"\x1b[5;2~") == 0


def test_page_key_input_does_not_intercept_alt_modified_page_key():
    data = b"\x1b\x1b[5~\x1b[6~"

    assert split_page_key_input(data) == (b"\x1b\x1b[5~", b"\x1b[6~")


def test_terminal_input_decoder_forwards_invalid_or_nonwheel_mouse_unchanged():
    decoder = TerminalInputDecoder()
    parts = decoder.feed(b"\x1b[<brokenM\x1b[<0;2;3M")

    assert parts[0] == b"\x1b[<brokenM"
    assert isinstance(parts[1], SgrMouseEvent)
    assert parts[1].wheel_direction == 0


def test_local_history_view_scrolls_cached_lines_and_restores_at_bottom():
    view = LocalHistoryView()
    prefetch_frame = view.begin_prefetch(1.0)
    prefetch_message = InputFrameDecoder().feed(prefetch_frame)[0]
    prefetch_id, max_lines = decode_history_prefetch(prefetch_message.data)
    assert max_lines == 300
    cached = HistorySnapshot(
        prefetch_id,
        "%8",
        x=30,
        y=2,
        width=40,
        height=3,
        lines=tuple(f"line-{index}".encode() for index in range(10)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    wheel_up = SgrMouseEvent(b"\x1b[<64;50;4M", 64, 50, 4, True)
    request = view.wheel(wheel_up)
    framed = InputFrameDecoder().feed(request.protocol_frame)[0]
    request_id, x, y, max_lines = decode_history_request(framed.data)
    assert (x, y, max_lines) == (50, 4, 2000)
    assert request.render_history is True
    assert view.overlays()[0][1] == (b"line-6", b"line-7", b"line-8")
    click = SgrMouseEvent(b"\x1b[<0;50;4M", 0, 50, 4, True)
    assert view.pointer_event(click, "%8").forwarded_input == b""
    assert view.active is True
    snapshot = HistorySnapshot(
        request_id,
        "%8",
        x=30,
        y=2,
        width=40,
        height=3,
        lines=tuple(f"line-{index}".encode() for index in range(10)),
    )

    accepted = view.accept(snapshot)

    assert accepted.render_history is True
    assert view.overlays()[0][1] == (b"line-6", b"line-7", b"line-8")
    wheel_down = SgrMouseEvent(b"\x1b[<65;50;4M", 65, 50, 4, True)
    restored = view.wheel(wheel_down)
    assert restored.restore_live is True
    assert view.active is False


def test_local_history_wheel_events_remain_one_row_during_a_burst():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"line-{index}".encode() for index in range(40)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (route,)))
    wheel_up = SgrMouseEvent(b"up", 64, 40, 2, True)

    view.wheel(wheel_up, now=1.00)
    assert view.viewports["%8"].offset == 1
    for now in (1.02, 1.04, 1.06):
        view.wheel(wheel_up, now=now)
    assert view.viewports["%8"].offset == 4

    view.wheel(wheel_up, now=1.08)
    assert view.viewports["%8"].offset == 5
    for now in (1.10, 1.12, 1.14):
        view.wheel(wheel_up, now=now)
    view.wheel(wheel_up, now=1.16)
    assert view.viewports["%8"].offset == 9

    view.wheel(wheel_up, now=1.30)
    assert view.viewports["%8"].offset == 10
    wheel_down = SgrMouseEvent(b"down", 65, 40, 2, True)
    view.wheel(wheel_down, now=1.31)
    assert view.viewports["%8"].offset == 9


def test_local_history_page_keys_move_one_visible_page_and_restore():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(
        request_id,
        "%8",
        x=30,
        y=0,
        width=40,
        height=6,
        lines=tuple(f"line-{index}".encode() for index in range(40)),
    )
    view.accept_prefetch(HistoryBatch(request_id, (route,)))

    page_up = view.page(b"\x1b[5~", 40, 2, now=1.0)

    assert page_up.render_history is True
    assert view.viewports["%8"].offset == 5
    view.page(b"\x1b[5~", 40, 2, now=1.1)
    assert view.viewports["%8"].offset == 10

    page_down = view.page(b"\x1b[6~", 40, 2, now=1.2)
    assert page_down.render_history is True
    assert view.viewports["%8"].offset == 5
    restored = view.page(b"\x1b[6~", 40, 2, now=1.3)
    assert restored.restore_live is True
    assert view.active is False


def test_local_history_page_keys_only_intercept_known_agent_cursor():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(
        request_id,
        "%8",
        x=30,
        y=0,
        width=40,
        height=6,
        lines=(b"old",) * 20,
    )
    view.accept_prefetch(HistoryBatch(request_id, (route,)))

    assert view.page(b"\x1b[5~", 4, 2) == HistoryAction(
        forwarded_input=b"\x1b[5~"
    )
    assert view.page(b"ordinary", 40, 2) == HistoryAction(
        forwarded_input=b"ordinary"
    )


def test_local_history_wheel_state_does_not_cross_panes():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshots = (
        HistorySnapshot(prefetch_id, "%8", 30, 0, 30, 3, (b"a",) * 40),
        HistorySnapshot(prefetch_id, "%9", 61, 0, 30, 3, (b"b",) * 40),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, snapshots))
    wheel_a = SgrMouseEvent(b"up-a", 64, 40, 2, True)
    wheel_b = SgrMouseEvent(b"up-b", 64, 70, 2, True)
    for index in range(9):
        view.wheel(wheel_a, now=1.0 + index * 0.02)
    assert view.viewports["%8"].offset == 9

    view.wheel(wheel_b, now=1.17)
    view.wheel(wheel_a, now=1.18)

    assert view.viewports["%9"].offset == 1
    assert view.viewports["%8"].offset == 10


def test_local_history_pointer_press_keeps_following_wheel_fine_grained():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(prefetch_id, "%8", 30, 0, 30, 3, (b"line",) * 40)
    view.accept_prefetch(HistoryBatch(prefetch_id, (route,)))
    wheel_up = SgrMouseEvent(b"up", 64, 40, 2, True)
    for index in range(5):
        view.wheel(wheel_up, now=1.0 + index * 0.02)
    assert view.viewports["%8"].offset == 5

    assert (
        view.pointer_event(SgrMouseEvent(b"press", 0, 40, 2, True), "%8")
        == HistoryAction()
    )
    assert (
        view.pointer_event(SgrMouseEvent(b"release", 0, 40, 2, False), "%8")
        == HistoryAction()
    )
    view.wheel(wheel_up, now=1.10)

    assert view.viewports["%8"].offset == 6


@pytest.mark.parametrize("transcript_backed", [False, True])
def test_long_code_block_scrolls_without_skipping_rows(transcript_backed):
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    lines = tuple(f"code block line {index:03d}".encode() for index in range(100))
    route = HistorySnapshot(
        request_id,
        "%8",
        30,
        0,
        50,
        5,
        lines,
        transcript_backed=transcript_backed,
        transcript_available=transcript_backed,
    )
    view.accept_prefetch(HistoryBatch(request_id, (route,)))
    wheel_up = SgrMouseEvent(b"up", 64, 40, 2, True)

    visible_rows = []
    for index in range(10):
        view.wheel(wheel_up, now=1.0 + index * 0.01)
        visible_rows.append(view.overlays()[0][1][0])

    assert visible_rows == [
        f"code block line {index:03d}".encode() for index in range(94, 84, -1)
    ]


def test_local_history_routes_sidebar_immediately_and_owns_agent_wheel():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    view.accept_prefetch(
        HistoryBatch(
            prefetch_id,
            (
                HistorySnapshot(
                    prefetch_id, "%8", 30, 0, 40, 2, (b"old", b"one", b"two")
                ),
            ),
        )
    )

    sidebar = b"\x1b[<64;4;4M"
    sidebar_action = view.wheel(SgrMouseEvent(sidebar, 64, 4, 4, True))
    assert sidebar_action.forwarded_input == sidebar

    agent_down = b"\x1b[<65;40;1M"
    agent_action = view.wheel(SgrMouseEvent(agent_down, 65, 40, 1, True))
    assert agent_action.forwarded_input == b""
    assert agent_action.protocol_frame == b""


def test_local_history_forwards_empty_mouse_aware_agent_history():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(
        request_id,
        "%8",
        30,
        0,
        40,
        3,
        (b"", b"", b""),
        mouse_forwardable=True,
    )
    view.accept_prefetch(HistoryBatch(request_id, (route,)))

    wheel_up = SgrMouseEvent(b"cc-up", 64, 40, 2, True)
    wheel_down = SgrMouseEvent(b"cc-down", 65, 40, 2, True)

    assert view.wheel(wheel_up) == HistoryAction(forwarded_input=b"cc-up")
    assert view.wheel(wheel_down) == HistoryAction(forwarded_input=b"cc-down")


def test_local_history_silently_waits_for_transcript_backed_claude_history():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(
        request_id,
        "%8",
        30,
        0,
        40,
        3,
        (b"", b"", b""),
        mouse_forwardable=True,
        transcript_backed=True,
    )
    view.accept_prefetch(HistoryBatch(request_id, (route,)))

    action = view.wheel(SgrMouseEvent(b"claude-up", 64, 40, 2, True))

    assert action.forwarded_input == b""
    assert action.info_message is None


def test_local_history_prompts_before_first_available_claude_scroll():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(
        request_id,
        "%8",
        30,
        0,
        40,
        3,
        (b"", b"", b""),
        mouse_forwardable=True,
        transcript_available=True,
        history_choice_required=True,
    )
    view.accept_prefetch(HistoryBatch(request_id, (route,)))

    action = view.wheel(SgrMouseEvent(b"first-up", 64, 40, 2, True))

    assert action.claude_history_prompt == b"first-up"
    assert action.forwarded_input == b""


def test_forced_policy_prefetch_preserves_other_pane_history_position():
    view = LocalHistoryView()
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    left = HistorySnapshot(
        first_id,
        "%7",
        0,
        0,
        30,
        2,
        (b"old", b"live-a", b"live-b"),
    )
    claude = HistorySnapshot(
        first_id,
        "%8",
        31,
        0,
        30,
        2,
        (b"", b""),
        mouse_forwardable=True,
        transcript_available=True,
        history_choice_required=True,
    )
    view.accept_prefetch(HistoryBatch(first_id, (left, claude)))
    view.wheel(SgrMouseEvent(b"left-up", 64, 10, 1, True))
    assert view.viewports["%7"].offset == 1

    forced = InputFrameDecoder().feed(view.begin_prefetch(1.1, force=True))[0]
    forced_id, _limit = decode_history_prefetch(forced.data)
    view.accept_prefetch(
        HistoryBatch(
            forced_id,
            (
                replace(left, request_id=forced_id),
                replace(
                    claude,
                    request_id=forced_id,
                    history_choice_required=False,
                    transcript_backed=True,
                    lines=(b"cc-old", b"cc-live-a", b"cc-live-b"),
                ),
            ),
        )
    )

    assert view.viewports["%7"].offset == 1


def test_local_history_enters_warm_cache_immediately_then_prefetches_near_top():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(
        request_id,
        "%8",
        0,
        0,
        80,
        30,
        tuple(f"line-{index}".encode() for index in range(300)),
        more_available=True,
    )
    view.accept_prefetch(HistoryBatch(request_id, (route,)))

    wheel = SgrMouseEvent(b"up", 64, 40, 2, True)
    action = view.wheel(wheel)

    assert action.render_history is True
    assert action.protocol_frame == b""
    assert view.overlays()[0][1][-1] == b"line-298"

    request_action = HistoryAction()
    for _ in range(200):
        request_action = view.wheel(wheel)
        if request_action.protocol_frame:
            break
    request = InputFrameDecoder().feed(request_action.protocol_frame)[0]
    assert decode_history_request(request.data)[3] == 2000


def test_local_history_keeps_empty_plain_agent_wheel_from_tmux_copy_mode():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(request_id, "%8", 30, 0, 40, 3, (b"", b"", b""))
    view.accept_prefetch(HistoryBatch(request_id, (route,)))

    wheel_up = SgrMouseEvent(b"shell-up", 64, 40, 2, True)
    wheel_down = SgrMouseEvent(b"shell-down", 65, 40, 2, True)

    assert view.wheel(wheel_up) == HistoryAction()
    assert view.wheel(wheel_down) == HistoryAction()


def test_local_history_never_leaks_wheel_to_tmux_before_routes_are_ready():
    view = LocalHistoryView()
    wheel = SgrMouseEvent(b"wheel", 64, 40, 2, True)

    initial = view.wheel(wheel)
    view.begin_prefetch(1.0)
    pending = view.wheel(wheel)
    view.invalidate_routes()
    invalidated = view.wheel(wheel)

    assert initial == HistoryAction(refresh_routes=True)
    assert pending == HistoryAction(refresh_routes=True)
    assert invalidated == HistoryAction(refresh_routes=True)


def test_valid_empty_routes_forward_modal_wheel_after_prefetch():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    view.accept_prefetch(HistoryBatch(request_id, ()))
    wheel = SgrMouseEvent(b"wheel", 64, 40, 2, True)

    assert view.wheel(wheel) == HistoryAction(forwarded_input=b"wheel")


def test_agent_border_wheel_is_consumed_but_sidebar_wheel_still_forwards():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(request_id, "%8", 30, 0, 40, 3, (b"old", b"one", b"two"))
    view.accept_prefetch(HistoryBatch(request_id, (route,)))

    left_border = view.wheel(SgrMouseEvent(b"border", 64, 30, 2, True))
    sidebar = view.wheel(SgrMouseEvent(b"sidebar", 64, 5, 2, True))

    assert left_border == HistoryAction()
    assert sidebar == HistoryAction(forwarded_input=b"sidebar")


def test_local_history_cross_agent_click_preserves_prefetched_routes():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    first = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"a-{index}".encode() for index in range(8)),
    )
    second = HistorySnapshot(
        prefetch_id,
        "%9",
        61,
        0,
        30,
        3,
        tuple(f"b-{index}".encode() for index in range(8)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (first, second)))
    view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))

    outside_down = SgrMouseEvent(b"down-other", 0, 70, 2, True)
    action = view.pointer_event(outside_down)

    assert action.forwarded_input == b"down-other"
    assert action.restore_live is False
    assert action.refresh_routes is True
    assert view.active is True
    assert tuple(view.viewports) == ("%8",)
    assert view.visible_routes == (first, second)
    assert (
        view.pointer_event(
            SgrMouseEvent(b"release-other", 0, 70, 2, False)
        ).forwarded_input
        == b"release-other"
    )


def test_local_history_sidebar_click_invalidates_agent_routes():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshot = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"line-{index}".encode() for index in range(8)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (snapshot,)))
    view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))

    action = view.pointer_event(SgrMouseEvent(b"sidebar", 0, 5, 2, True))

    assert action.forwarded_input == b"sidebar"
    assert action.restore_live is True
    assert action.refresh_routes is True
    assert view.visible_routes == ()
    assert (
        view.pointer_event(
            SgrMouseEvent(b"sidebar-drag", 32, 6, 2, True)
        ).forwarded_input
        == b"sidebar-drag"
    )
    assert (
        view.pointer_event(
            SgrMouseEvent(b"sidebar-release", 0, 5, 2, False)
        ).forwarded_input
        == b"sidebar-release"
    )
    assert (
        view.pointer_event(SgrMouseEvent(b"stray-release", 0, 5, 2, False))
        == HistoryAction()
    )


def test_local_history_forwards_sidebar_right_click_gesture_unchanged():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    view.accept_prefetch(
        HistoryBatch(
            request_id,
            (
                HistorySnapshot(
                    request_id, "%8", 30, 0, 40, 2, (b"old", b"one", b"two")
                ),
            ),
        )
    )
    press = SgrMouseEvent(b"right-press", 2, 5, 2, True)
    release = SgrMouseEvent(b"right-release", 2, 5, 2, False)

    assert view.pointer_event(press).forwarded_input == b"right-press"
    assert view.pointer_event(release).forwarded_input == b"right-release"


def test_local_history_captures_an_in_pane_pointer_gesture_until_release():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshot = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"line-{index}".encode() for index in range(8)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (snapshot,)))
    view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))

    assert (
        view.pointer_event(SgrMouseEvent(b"down", 0, 40, 2, True), "%8")
        == HistoryAction()
    )
    assert (
        view.pointer_event(SgrMouseEvent(b"wheel", 64, 40, 2, True), "%8")
        == HistoryAction()
    )
    assert (
        view.pointer_event(SgrMouseEvent(b"drag", 32, 5, 10, True)) == HistoryAction()
    )
    assert view.pointer_event(SgrMouseEvent(b"up", 0, 5, 10, False)) == HistoryAction()
    assert view.active is True


def test_local_history_forwards_click_but_suppresses_drag_in_agent_pane():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshots = (
        HistorySnapshot(prefetch_id, "%8", 30, 0, 30, 3, (b"0",) * 8),
        HistorySnapshot(prefetch_id, "%9", 61, 0, 30, 3, (b"1",) * 8),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, snapshots))
    view.wheel(SgrMouseEvent(b"up-a", 64, 40, 2, True))
    view.wheel(SgrMouseEvent(b"up-b", 64, 70, 2, True))

    down = view.pointer_event(SgrMouseEvent(b"down-b", 0, 70, 2, True), "%8")
    drag = view.pointer_event(SgrMouseEvent(b"drag-b", 32, 71, 2, True), "%8")
    wheel = view.pointer_event(SgrMouseEvent(b"wheel-b", 64, 71, 2, True), "%8")
    release = view.pointer_event(SgrMouseEvent(b"release-b", 0, 71, 2, False), "%8")

    assert down == HistoryAction(forwarded_input=b"down-b", refresh_routes=True)
    assert drag == HistoryAction()
    assert wheel.forwarded_input == b""
    assert wheel.render_history is True
    assert release.forwarded_input == b"release-b"
    assert tuple(view.viewports) == ("%8", "%9")


def test_live_agent_click_keeps_focus_and_never_starts_mouse_copy_mode():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(request_id, "%8", 30, 0, 30, 3, (b"line",) * 3)
    view.accept_prefetch(HistoryBatch(request_id, (route,)))

    down = view.pointer_event(SgrMouseEvent(b"down", 0, 40, 2, True))
    drag = view.pointer_event(SgrMouseEvent(b"drag", 32, 41, 2, True))
    release = view.pointer_event(SgrMouseEvent(b"release", 0, 41, 2, False))

    assert down == HistoryAction(forwarded_input=b"down", refresh_routes=True)
    assert drag == HistoryAction()
    assert release == HistoryAction(forwarded_input=b"release")

    # Capture is over: a stray motion report remains suppressed rather than
    # being able to invoke tmux's MouseDrag1Pane binding by itself.
    assert (
        view.pointer_event(SgrMouseEvent(b"stray-drag", 32, 41, 2, True))
        == HistoryAction()
    )


def test_local_history_wheel_over_another_pane_preserves_both_viewports():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    first = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"a-{index}".encode() for index in range(8)),
    )
    second = HistorySnapshot(
        prefetch_id,
        "%9",
        61,
        0,
        30,
        3,
        tuple(f"b-{index}".encode() for index in range(8)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (first, second)))
    view.wheel(SgrMouseEvent(b"up-a", 64, 40, 2, True))
    old_lines = view.overlays()[0][1]

    action = view.wheel(SgrMouseEvent(b"up-b", 64, 70, 2, True))

    assert old_lines == (b"a-4", b"a-5", b"a-6")
    assert action.restore_live is False
    assert action.render_history is True
    assert tuple(view.viewports) == ("%8", "%9")
    assert tuple(lines for _snapshot, lines in view.overlays()) == (
        (b"a-4", b"a-5", b"a-6"),
        (b"b-4", b"b-5", b"b-6"),
    )


def test_local_history_reaching_bottom_restores_only_that_pane():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshots = tuple(
        HistorySnapshot(
            prefetch_id,
            pane_id,
            x,
            0,
            30,
            3,
            tuple(f"{pane_id}-{index}".encode() for index in range(8)),
        )
        for pane_id, x in (("%8", 30), ("%9", 61))
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, snapshots))
    view.wheel(SgrMouseEvent(b"up-a", 64, 40, 2, True))
    view.wheel(SgrMouseEvent(b"up-b", 64, 70, 2, True))

    action = view.wheel(SgrMouseEvent(b"down-b", 65, 70, 2, True))

    assert action.restore_live is True
    assert tuple(view.viewports) == ("%8",)
    assert view.overlays()[0][1] == (b"%8-4", b"%8-5", b"%8-6")


def test_local_history_input_restores_only_its_routed_pane():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshots = (
        HistorySnapshot(prefetch_id, "%8", 30, 0, 30, 3, (b"0",) * 8),
        HistorySnapshot(prefetch_id, "%9", 61, 0, 30, 3, (b"1",) * 8),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, snapshots))
    view.wheel(SgrMouseEvent(b"up-a", 64, 40, 2, True))
    view.wheel(SgrMouseEvent(b"up-b", 64, 70, 2, True))

    assert view.cancel_for_input(70, 1) is True
    assert tuple(view.viewports) == ("%8",)
    assert view.cancel_for_input(40, 1) is True
    assert view.active is False


def test_local_history_input_outside_known_routes_restores_every_pane():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshot = HistorySnapshot(prefetch_id, "%8", 30, 0, 30, 3, (b"0",) * 8)
    view.accept_prefetch(HistoryBatch(prefetch_id, (snapshot,)))
    view.wheel(SgrMouseEvent(b"up-a", 64, 40, 2, True))

    assert view.cancel_for_input(5, 1) is True
    assert view.active is False


def test_deep_history_response_keeps_the_visible_anchor_when_output_advances():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    cached_lines = tuple(f"line-{index}".encode() for index in range(10))
    cached = HistorySnapshot(prefetch_id, "%8", 30, 0, 30, 3, cached_lines)
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    request = view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    request_id = decode_history_request(
        InputFrameDecoder().feed(request.protocol_frame)[0].data
    )[0]
    assert view.overlays()[0][1] == (b"line-6", b"line-7", b"line-8")

    deep = HistorySnapshot(
        request_id,
        "%8",
        30,
        0,
        30,
        3,
        (b"older-0", b"older-1", *cached_lines, b"new-10", b"new-11"),
    )
    action = view.accept(deep)

    assert action.render_history is True
    assert view.overlays()[0][1] == (b"line-6", b"line-7", b"line-8")


def test_history_progressively_extends_to_configured_limit_without_jumping():
    view = LocalHistoryView(history_limit=5000)
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    cached_lines = tuple(f"live-{index}".encode() for index in range(10))
    cached = HistorySnapshot(prefetch_id, "%8", 30, 0, 30, 3, cached_lines)
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    wheel_up = SgrMouseEvent(b"up", 64, 40, 2, True)

    initial = view.wheel(wheel_up)
    initial_request = decode_history_request(
        InputFrameDecoder().feed(initial.protocol_frame)[0].data
    )
    assert initial_request[3] == 2000
    first_page = (
        *(f"older-a-{index}".encode() for index in range(1990)),
        *cached_lines,
    )
    before = view.overlays()[0][1]
    first_action = view.accept(
        HistorySnapshot(
            initial_request[0],
            "%8",
            30,
            0,
            30,
            3,
            first_page,
            more_available=True,
        )
    )
    assert first_action.protocol_frame == b""
    assert view.overlays()[0][1] == before

    extension = HistoryAction()
    for _ in range(2100):
        extension = view.wheel(wheel_up)
        if extension.protocol_frame:
            break
    second_request = decode_history_request(
        InputFrameDecoder().feed(extension.protocol_frame)[0].data
    )
    assert second_request[3] == 4000

    before = view.overlays()[0][1]
    second_page = (
        *(f"older-b-{index}".encode() for index in range(2000)),
        *first_page,
    )
    second_action = view.accept(
        HistorySnapshot(
            second_request[0],
            "%8",
            30,
            0,
            30,
            3,
            second_page,
            more_available=True,
        )
    )
    assert view.overlays()[0][1] == before
    assert second_action.protocol_frame == b""

    extension = HistoryAction()
    for _ in range(2100):
        extension = view.wheel(wheel_up)
        if extension.protocol_frame:
            break
    final_request = decode_history_request(
        InputFrameDecoder().feed(extension.protocol_frame)[0].data
    )
    assert final_request[3] == 5000

    before = view.overlays()[0][1]
    final_page = (
        *(f"older-c-{index}".encode() for index in range(1000)),
        *second_page,
    )
    final_action = view.accept(
        HistorySnapshot(final_request[0], "%8", 30, 0, 30, 3, final_page)
    )
    assert final_action.protocol_frame == b""
    assert final_action.render_history is True
    assert view.overlays()[0][1] == before


def test_history_reuses_a_previous_deep_snapshot_without_refetching_less():
    view = LocalHistoryView(history_limit=5000)
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    cached_lines = tuple(f"live-{index}".encode() for index in range(10))
    cached = HistorySnapshot(prefetch_id, "%8", 30, 0, 30, 3, cached_lines)
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    wheel_up = SgrMouseEvent(b"up", 64, 40, 2, True)
    initial = view.wheel(wheel_up)
    request_id = decode_history_request(
        InputFrameDecoder().feed(initial.protocol_frame)[0].data
    )[0]
    deep_lines = (
        *(f"older-{index}".encode() for index in range(1990)),
        *cached_lines,
    )
    view.accept(HistorySnapshot(request_id, "%8", 30, 0, 30, 3, deep_lines))
    view.cancel_pane("%8")

    reopened = view.wheel(wheel_up)

    assert reopened.protocol_frame == b""
    assert view.viewports["%8"].loaded_limit == 2000
    assert len(view.viewports["%8"].snapshot.lines) == 2000


def test_history_stops_extending_after_a_short_deep_response():
    view = LocalHistoryView(history_limit=20000)
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    cached_lines = tuple(f"line-{index}".encode() for index in range(10))
    cached = HistorySnapshot(prefetch_id, "%8", 30, 0, 30, 3, cached_lines)
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    wheel_up = SgrMouseEvent(b"up", 64, 40, 2, True)
    initial = view.wheel(wheel_up)
    request_id = decode_history_request(
        InputFrameDecoder().feed(initial.protocol_frame)[0].data
    )[0]

    view.accept(
        HistorySnapshot(
            request_id,
            "%8",
            30,
            0,
            30,
            3,
            (b"older-a", b"older-b", *cached_lines),
        )
    )

    for _ in range(20):
        action = view.wheel(wheel_up)
        assert action.protocol_frame == b""


def test_history_top_info_appears_once_and_rearms_after_scrolling_down():
    view = LocalHistoryView(history_limit=20000)
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    cached = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"line-{index}".encode() for index in range(10)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    wheel_up = SgrMouseEvent(b"up", 64, 40, 2, True)
    request = view.wheel(wheel_up)
    request_id = decode_history_request(
        InputFrameDecoder().feed(request.protocol_frame)[0].data
    )[0]
    view.accept(
        HistorySnapshot(
            request_id,
            "%8",
            30,
            0,
            30,
            3,
            (b"older-a", b"older-b", *cached.lines),
        )
    )

    messages = [
        action.info_message
        for action in (view.wheel(wheel_up) for _ in range(20))
        if action.info_message
    ]

    assert messages == ["History top · complete session history loaded"]
    assert view.wheel(wheel_up).info_message is None

    wheel_down = SgrMouseEvent(b"down", 65, 40, 2, True)
    view.wheel(wheel_down)
    assert view.wheel(wheel_up).info_message == (
        "History top · complete session history loaded"
    )


def test_history_top_info_names_the_configured_limit_when_it_is_reached():
    view = LocalHistoryView(history_limit=2000)
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshot = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"line-{index}".encode() for index in range(2000)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (snapshot,)))
    wheel_up = SgrMouseEvent(b"up", 64, 40, 2, True)

    message = None
    for index in range(2100):
        message = view.wheel(wheel_up, now=1.0 + index * 0.02).info_message
        if message:
            break

    assert message == "History top · 2,000-line local limit"


def test_lost_deep_history_request_retries_after_a_bounded_timeout():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    cached = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        3,
        (b"oldest", b"older", b"one", b"two", b"newest"),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    wheel_up = SgrMouseEvent(b"up", 64, 40, 2, True)

    first = view.wheel(wheel_up, now=2.0)
    first_request = decode_history_request(
        InputFrameDecoder().feed(first.protocol_frame)[0].data
    )
    assert view.wheel(wheel_up, now=3.0).protocol_frame == b""

    retry = view.wheel(
        wheel_up,
        now=2.0 + fast_display_history._HISTORY_DEEP_TIMEOUT,
    )
    retry_request = decode_history_request(
        InputFrameDecoder().feed(retry.protocol_frame)[0].data
    )

    assert retry_request[0] != first_request[0]
    assert (
        view.accept(
            HistorySnapshot(
                first_request[0],
                "%8",
                30,
                0,
                30,
                3,
                (b"late", *cached.lines),
            )
        )
        == HistoryAction()
    )
    assert retry_request[0] in view._deep_pending


def test_periodic_prefetch_waits_for_new_screen_content_after_accept():
    gate = PeriodicPrefetchGate()

    assert gate.should_request()
    gate.sent(7)
    gate.accepted(7, 7)
    assert not gate.should_request()

    gate.screen_updated()
    assert gate.should_request()
    gate.sent(8)
    # A stale or overwritten response cannot quiet future prefetches.
    gate.accepted(8, 9)
    assert gate.should_request()

    gate.reset()
    assert gate.should_request()


def test_deep_history_response_without_anchor_does_not_jump_viewport():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    cached = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"old-{index}".encode() for index in range(10)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    request = view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    request_id = decode_history_request(
        InputFrameDecoder().feed(request.protocol_frame)[0].data
    )[0]
    before = view.overlays()

    action = view.accept(
        HistorySnapshot(
            request_id,
            "%8",
            30,
            0,
            30,
            3,
            tuple(f"different-{index}".encode() for index in range(20)),
        )
    )

    assert action == HistoryAction()
    assert view.overlays() == before


def test_deep_history_tolerates_one_dynamic_line_in_visible_anchor():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    cached = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        4,
        (
            b"oldest",
            b"stable-a",
            b"spinner-old",
            b"stable-b",
            b"stable-c",
            b"newest",
        ),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    request = view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    request_id = decode_history_request(
        InputFrameDecoder().feed(request.protocol_frame)[0].data
    )[0]
    assert view.overlays()[0][1] == (
        b"stable-a",
        b"spinner-old",
        b"stable-b",
        b"stable-c",
    )

    action = view.accept(
        HistorySnapshot(
            request_id,
            "%8",
            30,
            0,
            30,
            4,
            (
                b"older-a",
                b"older-b",
                b"stable-a",
                b"spinner-new",
                b"stable-b",
                b"stable-c",
                b"newest",
            ),
        )
    )

    assert action.render_history is True
    assert view.viewports["%8"].offset == 1
    assert view.viewports["%8"].loaded_limit == 2000
    assert view.overlays()[0][1] == (
        b"stable-a",
        b"spinner-new",
        b"stable-b",
        b"stable-c",
    )


def test_deep_history_response_with_duplicate_anchor_does_not_jump_viewport():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    cached = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        3,
        (b"0", b"repeat-a", b"repeat-b", b"repeat-c", b"4", b"5", b"6"),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    request = view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    request_id = decode_history_request(
        InputFrameDecoder().feed(request.protocol_frame)[0].data
    )[0]
    before = view.overlays()
    assert before[0][1] == (b"repeat-c", b"4", b"5")

    action = view.accept(
        HistorySnapshot(
            request_id,
            "%8",
            30,
            0,
            30,
            3,
            (
                b"older",
                b"repeat-a",
                b"repeat-b",
                b"repeat-c",
                b"middle",
                b"repeat-a",
                b"repeat-b",
                b"repeat-c",
                b"newer",
            ),
        )
    )

    assert action == HistoryAction()
    assert view.overlays() == before


def test_prefetch_geometry_change_restores_only_incompatible_viewport():
    view = LocalHistoryView()
    first_request = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first_request.data)
    snapshots = (
        HistorySnapshot(first_id, "%8", 30, 0, 30, 3, (b"0",) * 8),
        HistorySnapshot(first_id, "%9", 61, 0, 30, 3, (b"1",) * 8),
    )
    view.accept_prefetch(HistoryBatch(first_id, snapshots))
    view.wheel(SgrMouseEvent(b"up-a", 64, 40, 2, True))
    view.wheel(SgrMouseEvent(b"up-b", 64, 70, 2, True))
    second_request = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second_request.data)

    action = view.accept_prefetch(
        HistoryBatch(
            second_id,
            (
                HistorySnapshot(second_id, "%8", 30, 0, 30, 3, (b"new",) * 8),
                HistorySnapshot(second_id, "%9", 60, 0, 31, 3, (b"new",) * 8),
            ),
        )
    )

    assert action.restore_live is True
    assert tuple(view.viewports) == ("%8",)


def test_periodic_prefetch_never_moves_an_existing_frozen_viewport():
    view = LocalHistoryView()
    first_request = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first_request.data)
    cached = HistorySnapshot(
        first_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"old-{index}".encode() for index in range(8)),
    )
    view.accept_prefetch(HistoryBatch(first_id, (cached,)))
    view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    before = view.overlays()
    second_request = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second_request.data)
    advanced = HistorySnapshot(
        second_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"new-{index}".encode() for index in range(8)),
    )

    action = view.accept_prefetch(HistoryBatch(second_id, (advanced,)))

    assert action == HistoryAction()
    assert view.overlays() == before
    # A redraw without safe anchors cannot be spliced onto the old capture:
    # doing so would manufacture a false seam and omit intermediate rows. The
    # immutable frozen viewport remains visible, while the next history entry
    # starts from the newest internally contiguous capture.
    assert view.content_cache["%8"].lines == advanced.lines


def test_unaligned_hot_prefetch_never_creates_a_discontinuous_history_seam():
    view = LocalHistoryView(history_limit=2000)
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    old = HistorySnapshot(
        first_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"old-{index}".encode() for index in range(200)),
        more_available=True,
    )
    view.accept_prefetch(HistoryBatch(first_id, (old,)))

    second = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second.data)
    current = HistorySnapshot(
        second_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"new-{index}".encode() for index in range(200)),
        more_available=True,
    )
    view.accept_prefetch(HistoryBatch(second_id, (current,)))

    wheel = view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))

    # Do not expose a short/incomplete hot suffix. It has no anchor to the
    # previous capture and an active provider may repaint again before the deep
    # response, which used to leave a visible gap until returning to bottom.
    assert not wheel.render_history
    assert not view.active
    assert view.overlays() == ()
    # More wheel ticks while the bounded response is in flight adjust the
    # eventual starting point without exposing or replacing the hot suffix.
    assert view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True)) == HistoryAction()
    request = InputFrameDecoder().feed(wheel.protocol_frame)[0]
    request_id, _x, _y, max_lines = decode_history_request(request.data)
    assert max_lines == 2000

    deep = HistorySnapshot(
        request_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"deep-{index}".encode() for index in range(1800)) + current.lines,
    )
    accepted = view.accept(deep)

    assert accepted.render_history
    assert view.viewports["%8"].snapshot.lines == deep.lines
    assert view.overlays()[0][1] == (b"new-195", b"new-196", b"new-197")
    assert b"old-196" not in view.viewports["%8"].snapshot.lines


def test_initial_deep_history_retry_and_wheel_down_are_bounded():
    view = LocalHistoryView(history_limit=2000)
    route = HistorySnapshot(
        1,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"line-{index}".encode() for index in range(200)),
        more_available=True,
    )
    view.visible_routes = (route,)
    view.content_cache["%8"] = route
    view._routes_ready = True
    wheel_up = SgrMouseEvent(b"up", 64, 40, 2, True)
    wheel_down = SgrMouseEvent(b"down", 65, 40, 2, True)

    first = view.wheel(wheel_up, now=1.0)
    first_message = InputFrameDecoder().feed(first.protocol_frame)[0]
    first_id, *_rest = decode_history_request(first_message.data)
    assert view.wheel(wheel_up, now=2.0) == HistoryAction()

    retry = view.wheel(wheel_up, now=20.0)
    retry_message = InputFrameDecoder().feed(retry.protocol_frame)[0]
    retry_id, *_rest = decode_history_request(retry_message.data)
    assert retry_id != first_id
    assert view.metrics.timeouts == 1
    pending = view._deep_pending[retry_id]
    assert pending.initial_offset == 3

    assert view.wheel(wheel_down, now=21.0) == HistoryAction()
    assert not view.pending and not view.active
    late = replace(route, request_id=retry_id, lines=tuple(
        f"deep-{index}".encode() for index in range(2000)))
    assert view.accept(late) == HistoryAction()
    assert not view.active


def test_temporary_blank_fullscreen_tail_is_removed_after_main_view_returns():
    view = LocalHistoryView()

    def prefetch(lines, now):
        frame = InputFrameDecoder().feed(view.begin_prefetch(now, force=True))[0]
        request_id, _limit = decode_history_prefetch(frame.data)
        view.accept_prefetch(
            HistoryBatch(
                request_id,
                (
                    HistorySnapshot(
                        request_id,
                        "%8",
                        30,
                        0,
                        50,
                        60,
                        tuple(lines),
                    ),
                ),
            )
        )

    main = tuple(f"main-{index}".encode() for index in range(300))
    prefetch(main, 1.0)
    prefetch((*main[-60:], *(b"\033[0m   " for _ in range(60))), 2.0)
    assert view.content_cache["%8"].lines[-60:] == (b"\033[0m   ",) * 60

    returned = tuple(f"main-{index}".encode() for index in range(270, 330))
    prefetch(returned, 3.0)

    cached = view.content_cache["%8"].lines
    assert cached == tuple(f"main-{index}".encode() for index in range(330))
    wheel = view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    assert wheel.render_history is True
    assert any(line.strip() for line in view.overlays()[0][1])


def test_periodic_hot_prefetch_does_not_shrink_a_reusable_deep_snapshot():
    view = LocalHistoryView(history_limit=2000)
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    hot_lines = tuple(f"line-{index}".encode() for index in range(300))
    hot = HistorySnapshot(
        first_id,
        "%8",
        30,
        0,
        30,
        3,
        hot_lines,
        more_available=True,
    )
    view.accept_prefetch(HistoryBatch(first_id, (hot,)))
    wheel = SgrMouseEvent(b"up", 64, 40, 2, True)
    deep_action = view.wheel(wheel)
    assert deep_action.render_history is True
    assert deep_action.protocol_frame == b""
    for _ in range(300):
        deep_action = view.wheel(wheel)
        if deep_action.protocol_frame:
            break
    deep_id = decode_history_request(
        InputFrameDecoder().feed(deep_action.protocol_frame)[0].data
    )[0]
    deep_lines = (
        *(f"older-{index}".encode() for index in range(1700)),
        *hot_lines,
    )
    view.accept(
        HistorySnapshot(
            deep_id,
            "%8",
            30,
            0,
            30,
            3,
            deep_lines,
            more_available=True,
        )
    )
    view.cancel_pane("%8")

    second = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second.data)
    advanced = HistorySnapshot(
        second_id,
        "%8",
        30,
        0,
        30,
        3,
        (*hot_lines[25:], *(f"new-{index}".encode() for index in range(25))),
        more_available=True,
    )
    view.accept_prefetch(HistoryBatch(second_id, (advanced,)))

    assert len(view.content_cache["%8"].lines) == 2000
    assert view.content_cache["%8"].lines[-1] == b"new-24"
    reopened = view.wheel(wheel)
    assert reopened.protocol_frame == b""
    assert len(view.viewports["%8"].snapshot.lines) == 2000


def test_validated_deep_capture_recovers_unanchored_rewind_output():
    view = LocalHistoryView(history_limit=2000)
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    repeated = b"\033[48;2;33;58;43mrewind-row"
    hot_lines = (
        *(f"old-{index}".encode() for index in range(120)),
        repeated,
        repeated,
        repeated,
        *(f"new-{index}".encode() for index in range(176)),
        b"live-bottom",
    )
    hot = HistorySnapshot(
        first_id,
        "%8",
        30,
        0,
        30,
        3,
        hot_lines,
        more_available=True,
    )
    view.accept_prefetch(HistoryBatch(first_id, (hot,)))
    wheel_up = SgrMouseEvent(b"up", 64, 40, 2, True)
    wheel = view.wheel(wheel_up)
    assert wheel.render_history is True
    assert wheel.protocol_frame == b""
    for _ in range(300):
        wheel = view.wheel(wheel_up)
        if wheel.protocol_frame:
            break
    request_id = decode_history_request(
        InputFrameDecoder().feed(wheel.protocol_frame)[0].data
    )[0]
    # The frozen three-row viewport is byte-identical and occurs once, so the
    # deep response is safe. Its individually repeated text cannot provide the
    # two unique votes used by the generic rolling-cache timeline matcher.
    deep_lines = (
        *(f"rewind-output-{index}".encode() for index in range(1000)),
        repeated,
        repeated,
        repeated,
        b"live-bottom",
    )

    action = view.accept(
        HistorySnapshot(
            request_id,
            "%8",
            30,
            0,
            30,
            3,
            deep_lines,
            more_available=False,
        )
    )

    assert action.render_history is True
    assert len(view.content_cache["%8"].lines) == len(deep_lines)
    assert view.content_cache["%8"].lines[0] == b"rewind-output-0"
    assert view.overlays()[0][1] == (repeated, repeated, repeated)


def test_local_history_cache_survives_remote_scrollback_shrinking():
    view = LocalHistoryView(history_limit=2000)
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    old = tuple(f"old-{index}".encode() for index in range(1000))
    view.accept_prefetch(
        HistoryBatch(first_id, (HistorySnapshot(first_id, "%8", 30, 0, 30, 3, old),))
    )

    second = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second.data)
    # tmux retained only a short suffix after an alternate-screen redraw.
    short = (*old[-100:], *(f"new-{index}".encode() for index in range(20)))
    view.accept_prefetch(
        HistoryBatch(
            second_id, (HistorySnapshot(second_id, "%8", 30, 0, 30, 3, short),)
        )
    )

    stored = view.content_cache["%8"].lines
    assert stored[:3] == old[:3]
    assert stored[-20:] == tuple(f"new-{index}".encode() for index in range(20))
    assert len(stored) == 1020


def test_local_history_cache_discards_previous_rewind_generation():
    view = LocalHistoryView(history_limit=2000)
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    old = tuple(f"abandoned-{index}".encode() for index in range(1000))
    view.accept_prefetch(HistoryBatch(
        first_id,
        (HistorySnapshot(
            first_id, "%8", 30, 0, 30, 3, old, generation=11),),
    ))

    second = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second.data)
    current = (b"retained", b"replacement", b"live")
    view.accept_prefetch(HistoryBatch(
        second_id,
        (HistorySnapshot(
            second_id, "%8", 30, 0, 30, 3, current, generation=12),),
    ))

    assert view.content_cache["%8"].lines == current


def test_rewind_generation_change_closes_frozen_history_overlay():
    view = LocalHistoryView(history_limit=2000)
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    old = tuple(f"abandoned-{index}".encode() for index in range(100))
    view.accept_prefetch(HistoryBatch(
        first_id,
        (HistorySnapshot(
            first_id, "%8", 30, 0, 30, 3, old, generation=11),),
    ))
    assert view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True)).render_history

    second = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second.data)
    current = (b"retained", b"replacement", b"live")
    action = view.accept_prefetch(HistoryBatch(
        second_id,
        (HistorySnapshot(
            second_id, "%8", 30, 0, 30, 3, current, generation=12),),
    ))

    assert action.restore_live is True
    assert view.active is False
    assert view.content_cache["%8"].lines == current


def test_rewind_generation_change_rejects_stale_deep_response():
    view = LocalHistoryView(history_limit=2000)
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    old = tuple(f"abandoned-{index}".encode() for index in range(100))
    view.accept_prefetch(HistoryBatch(
        first_id,
        (HistorySnapshot(
            first_id, "%8", 30, 0, 30, 3, old, generation=11),),
    ))
    deep = view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    deep_id = decode_history_request(
        InputFrameDecoder().feed(deep.protocol_frame)[0].data
    )[0]

    # A newer prefetch observes the rewind before the old deep response arrives.
    second = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second.data)
    current = (b"retained", b"replacement", b"live")
    view.accept_prefetch(HistoryBatch(
        second_id,
        (HistorySnapshot(
            second_id, "%8", 30, 0, 30, 3, current, generation=12),),
    ))

    action = view.accept(HistorySnapshot(
        deep_id,
        "%8",
        30,
        0,
        30,
        3,
        (b"older", *old),
        generation=11,
    ))

    assert action == HistoryAction()
    assert view.active is False
    assert view.content_cache["%8"].lines == current


def test_rolling_claude_transcript_retains_older_cached_rows():
    view = LocalHistoryView(history_limit=2000)
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    previous = tuple(f"turn-{index}".encode() for index in range(1000))
    view.accept_prefetch(
        HistoryBatch(
            first_id,
            (
                HistorySnapshot(
                    first_id,
                    "%8",
                    30,
                    0,
                    30,
                    3,
                    previous,
                    transcript_backed=True,
                    transcript_available=True,
                ),
            ),
        )
    )

    second = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second.data)
    rolled = tuple(f"turn-{index}".encode() for index in range(250, 1100))
    view.accept_prefetch(
        HistoryBatch(
            second_id,
            (
                HistorySnapshot(
                    second_id,
                    "%8",
                    30,
                    0,
                    30,
                    3,
                    rolled,
                    transcript_backed=True,
                    transcript_available=True,
                ),
            ),
        )
    )

    assert view.content_cache["%8"].lines == tuple(
        f"turn-{index}".encode() for index in range(1100)
    )


def test_unaligned_rejected_deep_response_does_not_replace_history_cache():
    view = LocalHistoryView()
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    cached = HistorySnapshot(
        first_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"cached-{index}".encode() for index in range(10)),
    )
    view.accept_prefetch(HistoryBatch(first_id, (cached,)))
    request = view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    request_id = decode_history_request(
        InputFrameDecoder().feed(request.protocol_frame)[0].data
    )[0]

    action = view.accept(
        HistorySnapshot(
            request_id,
            "%8",
            30,
            0,
            30,
            3,
            tuple(f"unrelated-{index}".encode() for index in range(20)),
        )
    )

    assert action == HistoryAction()
    assert view.content_cache["%8"] == cached


def test_history_generations_reject_prefetch_and_deep_responses_after_invalidation():
    view = LocalHistoryView()
    first_request = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first_request.data)
    view.invalidate_routes()
    stale = HistorySnapshot(first_id, "%8", 30, 0, 30, 2, (b"a", b"b", b"c"))
    view.accept_prefetch(HistoryBatch(first_id, (stale,)))
    assert view.visible_routes == ()

    second_request = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second_request.data)
    current = HistorySnapshot(second_id, "%8", 30, 0, 30, 2, (b"a", b"b", b"c"))
    view.accept_prefetch(HistoryBatch(second_id, (current,)))
    deep = view.wheel(SgrMouseEvent(b"up", 64, 40, 1, True))
    deep_id = decode_history_request(
        InputFrameDecoder().feed(deep.protocol_frame)[0].data
    )[0]
    view.invalidate_routes()

    assert (
        view.accept(
            HistorySnapshot(deep_id, "%8", 30, 0, 30, 2, (b"old", b"a", b"b", b"c"))
        )
        == HistoryAction()
    )
    assert view.active is False


def test_reconnect_preserves_cache_only_after_a_fresh_timeline_anchor():
    view = LocalHistoryView()
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    old = tuple(f"line-{index}".encode() for index in range(100))
    view.accept_prefetch(
        HistoryBatch(first_id, (HistorySnapshot(first_id, "%8", 30, 0, 30, 3, old),))
    )

    assert view.mark_reconnected() is False
    assert view.visible_routes == ()
    second = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second.data)
    advanced = (
        *old[-20:],
        *(f"line-{index}".encode() for index in range(100, 110)),
    )
    view.accept_prefetch(
        HistoryBatch(
            second_id, (HistorySnapshot(second_id, "%8", 30, 0, 30, 3, advanced),)
        )
    )

    assert view.content_cache["%8"].lines == tuple(
        f"line-{index}".encode() for index in range(110)
    )


def test_reconnect_refreshes_routes_before_the_next_wheel_is_consumed():
    view = LocalHistoryView()
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    lines = tuple(f"line-{index}".encode() for index in range(100))
    route = HistorySnapshot(first_id, "%8", 30, 0, 30, 3, lines)
    view.accept_prefetch(HistoryBatch(first_id, (route,)))
    view.mark_reconnected()

    waiting = view.wheel(SgrMouseEvent(b"up-before", 64, 40, 2, True))
    assert waiting.refresh_routes is True
    assert waiting.forwarded_input == b""

    refresh = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    refresh_id, _limit = decode_history_prefetch(refresh.data)
    view.accept_prefetch(
        HistoryBatch(
            refresh_id,
            (replace(route, request_id=refresh_id),),
        )
    )
    scrolled = view.wheel(SgrMouseEvent(b"up-after", 64, 40, 2, True))

    assert scrolled.render_history is True
    assert view.overlays()[0][1] == lines[-4:-1]


def test_reconnect_rejects_unanchored_cache_for_a_reused_pane_id():
    view = LocalHistoryView()
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    view.accept_prefetch(
        HistoryBatch(
            first_id,
            (
                HistorySnapshot(
                    first_id,
                    "%8",
                    30,
                    0,
                    30,
                    3,
                    tuple(f"old-{index}".encode() for index in range(100)),
                ),
            ),
        )
    )
    view.mark_reconnected()
    second = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second.data)
    unrelated = tuple(f"new-{index}".encode() for index in range(20))

    view.accept_prefetch(
        HistoryBatch(
            second_id, (HistorySnapshot(second_id, "%8", 30, 0, 30, 3, unrelated),)
        )
    )

    assert view.content_cache["%8"].lines == unrelated


def test_history_content_cache_keeps_only_recent_pane_lifetimes():
    view = LocalHistoryView()
    pane_ids = []
    for index in range(12):
        request = InputFrameDecoder().feed(view.begin_prefetch(float(index + 1)))[0]
        request_id, _limit = decode_history_prefetch(request.data)
        pane_id = f"%{100 + index}"
        pane_ids.append(pane_id)
        snapshot = HistorySnapshot(
            request_id, pane_id, 30, 0, 30, 2, (b"a", b"b", b"c")
        )
        view.accept_prefetch(HistoryBatch(request_id, (snapshot,)))
        view.invalidate_routes()

    assert tuple(view.content_cache) == tuple(pane_ids[-8:])


def test_local_history_wheel_batch_preserves_distance_with_one_paint():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(
        request_id,
        "%8",
        0,
        0,
        30,
        3,
        tuple(f"line-{index}".encode() for index in range(20)),
    )
    view.accept_prefetch(HistoryBatch(request_id, (route,)))
    batch = fast_display_client._LocalInputBatch()
    events = TerminalInputDecoder().feed(b"\x1b[<64;5;2M" * 8)
    assert len(events) == 8
    for event in events:
        assert isinstance(event, SgrMouseEvent)
        action, deferred_paint = batch.prepare(event, view.pointer_event(event))
        assert action is not None
        assert deferred_paint is not None
        deferred_paint.defer(action)

    assert view.viewports["%8"].offset == 8
    surface = MagicMock(spec=TerminalSurface)
    screen = MagicMock(spec=AppliedScreen)
    overlays = view.overlays()

    batch.flush(surface, screen, overlays, ())

    surface.paint_overlays.assert_called_once_with(screen, overlays, ())
    surface.paint.assert_not_called()


def test_local_history_wheel_batch_restores_live_once_at_bottom():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    route = HistorySnapshot(
        request_id,
        "%8",
        0,
        0,
        30,
        3,
        tuple(f"line-{index}".encode() for index in range(20)),
    )
    view.accept_prefetch(HistoryBatch(request_id, (route,)))
    up = SgrMouseEvent(b"up", 64, 5, 2, True)
    for _ in range(3):
        view.pointer_event(up)
    assert view.viewports["%8"].offset == 3

    batch = fast_display_client._LocalInputBatch()
    down = SgrMouseEvent(b"down", 65, 5, 2, True)
    for _ in range(5):
        action, deferred_paint = batch.prepare(down, view.pointer_event(down))
        assert action is not None
        assert deferred_paint is not None
        deferred_paint.defer(action)
    assert not view.active

    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(
            encode_update(_keyframe(width=30, height=3))
        )[0],
        os.terminal_size((30, 3)),
    )
    assert screen is not None
    surface = MagicMock(spec=TerminalSurface)
    batch.flush(surface, screen, view.overlays(), ())

    surface.paint.assert_called_once()
    surface.paint_overlays.assert_not_called()


def test_forwarded_wheel_batch_remains_bounded_without_local_paint():
    batch = fast_display_client._LocalInputBatch()
    down = SgrMouseEvent(b"down", 65, 5, 2, True)
    up = SgrMouseEvent(b"up", 64, 5, 2, True)
    admitted = []

    for _ in range(8):
        action, deferred_paint = batch.prepare(
            down,
            HistoryAction(forwarded_input=down.raw),
        )
        if action is not None:
            admitted.append(action.forwarded_input)
        assert deferred_paint is None
    action, deferred_paint = batch.prepare(
        up,
        HistoryAction(forwarded_input=up.raw),
    )
    assert action is not None
    admitted.append(action.forwarded_input)
    assert deferred_paint is None

    surface = MagicMock(spec=TerminalSurface)
    batch.flush(surface, MagicMock(spec=AppliedScreen), (), ())

    assert admitted == [b"down", b"up"]
    surface.paint.assert_not_called()
    surface.paint_overlays.assert_not_called()


def test_only_bounded_layout_and_modal_keys_invalidate_live_routes():
    assert input_may_change_routes(b"\x1b[19~", routes_visible=True)
    assert input_may_change_routes(b"\x1b[20~", routes_visible=True)
    assert input_may_change_routes(b"?", routes_visible=True)
    assert input_may_change_routes(b"\x1b", routes_visible=False)
    assert input_may_change_routes(b"\r", routes_visible=False)
    assert not input_may_change_routes(b"ordinary input", routes_visible=True)
    assert not input_may_change_routes(b"\r", routes_visible=True)
    assert input_may_change_routes(
        b"\x1b[B", routes_visible=True, cursor_in_agent=False
    )
    assert input_may_change_routes(b"\x02", routes_visible=True, cursor_in_agent=True)
    assert not input_may_change_routes(
        b"\x1b[B", routes_visible=True, cursor_in_agent=True
    )
    assert not input_may_change_routes(
        b"\x1b[I", routes_visible=True, cursor_in_agent=False
    )
    assert not input_may_change_routes(
        b"\x1b[O", routes_visible=True, cursor_in_agent=False
    )


def test_screen_input_route_detection_distinguishes_sidebar_and_agent_cursor():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    request_id, _limit = decode_history_prefetch(prefetch.data)
    view.accept_prefetch(
        HistoryBatch(
            request_id,
            (
                HistorySnapshot(
                    request_id,
                    "%8",
                    x=30,
                    y=0,
                    width=50,
                    height=4,
                    lines=(b"old", b"one", b"two", b"new"),
                ),
            ),
        )
    )
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(encode_update(_keyframe(width=80, height=4)))[
            0
        ],
        os.terminal_size((80, 4)),
    )
    assert screen is not None
    sidebar = replace(screen, cursor_x=5, cursor_y=1)
    agent = replace(screen, cursor_x=40, cursor_y=1)

    assert screen_input_may_change_routes(b"\x1b[B", view, sidebar)
    assert not screen_input_may_change_routes(b"\x1b[B", view, agent)
    assert screen_input_may_change_routes(b"\x02", view, agent)
    assert not screen_input_may_change_routes(b"\x1b[I", view, sidebar)
    assert not screen_input_may_change_routes(b"\x1b[O", view, sidebar)


def test_full_window_ssh_command_uses_railmux_remote_subcommand_and_protocol():
    argv = build_ssh_argv(
        "server",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=("-J", "jump"),
    )

    assert argv[:9] == [
        "ssh",
        "-J",
        "jump",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=3",
        "-T",
        "server",
    ]
    assert "then exec railmux remote-server" in argv[-1]
    assert f"--protocol {PROTOCOL_VERSION}" in argv[-1]
    assert "python3 -m railmux remote-server" in argv[-1]
    assert '"$HOME/.local/share/railmux/ssh-venv/bin/python"' in argv[-1]
    assert "--session 'rail mux'" in argv[-1]
    assert "--width 120 --height 40 --fps 20.0" in argv[-1]


def test_windows_remote_command_uses_shell_neutral_direct_launch():
    argv = build_ssh_argv(
        "windows-server",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
        launch_mode=RemoteLaunchMode.DIRECT,
    )

    assert argv[-1].startswith("railmux remote-server ")
    assert "if [" not in argv[-1]
    assert "--session 'rail mux'" in argv[-1]


def test_full_window_ssh_keepalive_defaults_follow_user_overrides():
    argv = build_ssh_argv(
        "server",
        session="railmux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=("-o", "ServerAliveInterval=20"),
    )

    user_interval = argv.index("ServerAliveInterval=20")
    default_interval = argv.index("ServerAliveInterval=5")
    assert user_interval < default_interval < argv.index("server")


def test_takeover_flag_is_private_remote_server_argument():
    argv = build_ssh_argv(
        "server",
        session="railmux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
        replace_existing_client=True,
    )

    assert "--replace-existing-client" in argv[-1]


def test_existing_session_only_flag_is_private_remote_server_argument():
    argv = build_ssh_argv(
        "server",
        session="railmux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
        existing_session_only=True,
    )

    assert "--existing-session-only" in argv[-1]


def test_remote_install_command_uses_user_pip_then_matching_python_module():
    argv = build_ssh_install_argv(
        "server",
        version="1.2.3",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=("-J", "jump"),
    )

    assert argv[:5] == ["ssh", "-J", "jump", "-T", "server"]
    assert "python3 -m pip --version" in argv[-1]
    assert "python3 -m pip install --user --upgrade" in argv[-1]
    assert "'railmux[ssh]==1.2.3'" in argv[-1]
    assert "pip3 install --user --upgrade" in argv[-1]
    assert "&& exec python3 -m railmux remote-server" in argv[-1]
    assert '"$HOME/.local/share/railmux/ssh-venv/bin/python" -m pip' in argv[-1]
    assert "sudo" not in argv[-1]


def test_generated_remote_bootstrap_and_install_commands_are_posix_shell_syntax():
    bootstrap = build_ssh_argv(
        "server",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
    )[-1]
    installer = build_ssh_install_argv(
        "server",
        version="1.2.3",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
    )[-1]
    private_installer = build_ssh_private_venv_install_argv(
        "server",
        version="1.2.3",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
    )[-1]

    for command in (bootstrap, installer, private_installer):
        result = subprocess.run(
            ["/bin/sh", "-n", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode()


def test_private_remote_install_creates_managed_venv_without_sudo():
    argv = build_ssh_private_venv_install_argv(
        "server",
        version="1.2.3",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=("-J", "jump"),
    )

    assert argv[:5] == ["ssh", "-J", "jump", "-T", "server"]
    command = argv[-1]
    assert 'python3 -m venv "$HOME/.local/share/railmux/ssh-venv"' in command
    assert '"$HOME/.local/share/railmux/ssh-venv"/bin/python' in command
    assert "'railmux[ssh]==1.2.3'" in command
    assert "--user" not in command
    assert "sudo" not in command


def test_remote_install_help_is_exact_and_has_source_fallback():
    help_text = remote_install_help("server", "1.2.3")

    assert "python3 -m pip install --user --upgrade" in help_text
    assert "'railmux[ssh]==1.2.3'" in help_text
    assert "~/.local/share/railmux/ssh-venv/bin/python" in help_text
    assert "do not modify the system Python" in help_text
    assert "matching wheel or source checkout" in help_text


def test_remote_hello_is_strictly_bounded_and_typed():
    hello = parse_remote_hello(
        REMOTE_HELLO_PREFIX
        + b'{"protocol":6,"ready":true,"tmux":true,"version":"1.2.3"}\n'
    )

    assert hello == RemoteHello("1.2.3", 6, True)
    configured = parse_remote_hello(
        REMOTE_HELLO_PREFIX
        + b'{"config_status":"invalid","protocol":6,"ready":true,'
        b'"tmux":false,"tmux_configured":true,"version":"1.2.3"}\n'
    )
    assert configured.config_status == "invalid"
    assert configured.tmux_configured is True
    windows = parse_remote_hello(
        REMOTE_HELLO_PREFIX
        + b'{"platform":"windows-msys2","protocol":6,"ready":true,'
        b'"tmux":true,"version":"1.2.3"}\n'
    )
    assert windows.platform == "windows-msys2"
    with pytest.raises(ValueError):
        parse_remote_hello(
            REMOTE_HELLO_PREFIX + b'{"protocol":true,"ready":true,"tmux":true,'
            b'"version":"1.2.3"}\n'
        )
    with pytest.raises(ValueError):
        parse_remote_hello(REMOTE_HELLO_PREFIX + b"not-json\n")
    with pytest.raises(ValueError):
        parse_remote_hello(
            REMOTE_HELLO_PREFIX
            + b'{"platform":"windows","protocol":6,"ready":true,'
            b'"tmux":true,"version":"1.2.3"}\n'
        )


def test_remote_startup_wait_reads_hello_before_raw_mode():
    script = (
        "import sys; "
        "sys.stdout.buffer.write("
        f'b\'RAILMUX-REMOTE/1 {{"protocol":{PROTOCOL_VERSION},'
        '"ready":true,"tmux":true,'
        '"version":"1.2.3"}\\n\'); '
        "sys.stdout.buffer.flush()"
    )
    process = subprocess.Popen(
        [fast_display_client.sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    startup = await_remote_startup(process, timeout=2.0)
    process.wait(timeout=2.0)

    assert startup == RemoteStartup(
        RemoteStartKind.HELLO,
        RemoteHello("1.2.3", PROTOCOL_VERSION, True),
    )


def test_remote_startup_tolerates_a_non_newline_shell_banner():
    script = (
        "import sys; "
        "sys.stdout.buffer.write("
        f'b\'banner: RAILMUX-REMOTE/1 {{"protocol":{PROTOCOL_VERSION},'
        '"ready":true,'
        '"tmux":true,"version":"1.2.3"}\\n\'); '
        "sys.stdout.buffer.flush()"
    )
    process = subprocess.Popen(
        [fast_display_client.sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    startup = await_remote_startup(process, timeout=2.0)
    process.wait(timeout=2.0)

    assert startup == RemoteStartup(
        RemoteStartKind.HELLO,
        RemoteHello("1.2.3", PROTOCOL_VERSION, True),
    )


def test_remote_startup_rejects_an_old_wire_protocol_without_timing_out():
    process = subprocess.Popen(
        [
            fast_display_client.sys.executable,
            "-c",
            "import sys,time;sys.stdout.buffer.write(b'RMUXD5\\0');"
            "sys.stdout.buffer.flush();time.sleep(5)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        started = time.monotonic()
        startup = await_remote_startup(process, timeout=2.0)

        assert startup == RemoteStartup(RemoteStartKind.FAILED)
        assert time.monotonic() - started < 1.0
    finally:
        process.terminate()
        process.wait(timeout=2.0)


@pytest.mark.parametrize("waiter", ["hello", "attach"])
def test_reconnect_handshake_waits_are_locally_cancellable(waiter):
    process = subprocess.Popen(
        [
            fast_display_client.sys.executable,
            "-c",
            "import time; time.sleep(5)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, LOCAL_ESCAPE)
        with pytest.raises(fast_display_client.ReconnectCancelled) as exc:
            if waiter == "hello":
                await_remote_startup(process, timeout=2.0, cancel_fd=read_fd)
            else:
                fast_display_client.await_remote_attach_status(
                    process, timeout=2.0, cancel_fd=read_fd
                )
        assert exc.value.exit_code == 0
    finally:
        os.close(read_fd)
        os.close(write_fd)
        process.terminate()
        process.wait(timeout=2.0)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (REMOTE_ATTACH_ACCEPTED, RemoteAttachKind.ACCEPTED),
        (REMOTE_ATTACH_BUSY, RemoteAttachKind.BUSY),
    ],
)
def test_remote_attach_status_stops_at_line_before_display_frames(
    status,
    expected,
):
    script = (
        "import sys; "
        f"sys.stdout.buffer.write({status!r} + {DISPLAY_MAGIC!r}); "
        "sys.stdout.buffer.flush()"
    )
    process = subprocess.Popen(
        [fast_display_client.sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    assert (
        fast_display_client.await_remote_attach_status(process, timeout=2.0) is expected
    )
    assert os.read(process.stdout.fileno(), len(DISPLAY_MAGIC)) == DISPLAY_MAGIC
    process.wait(timeout=2.0)


def test_reconnect_is_default_and_can_be_disabled_or_explicit():
    default = parse_client_args(["server"])
    enabled = parse_client_args(["server", "--reconnect"])
    disabled = parse_client_args(["server", "--no-reconnect"])

    assert default.reconnect is True
    assert enabled.reconnect is True
    assert disabled.reconnect is False
    assert enabled.raw_argv == ("server", "--reconnect")
    assert disabled.raw_argv == ("server", "--no-reconnect")


def test_history_line_limit_is_optional_and_cli_bounded():
    assert parse_client_args(["server"]).history_lines is None
    assert (
        parse_client_args(
            [
                "server",
                "--history-lines",
                "2000",
            ]
        ).history_lines
        == 2000
    )
    assert (
        parse_client_args(
            [
                "server",
                "--history-lines",
                "20000",
            ]
        ).history_lines
        == 20000
    )

    with pytest.raises(SystemExit):
        parse_client_args(["server", "--history-lines", "1999"])
    with pytest.raises(SystemExit):
        parse_client_args(["server", "--history-lines", "20001"])


@pytest.mark.parametrize(
    ("enabled", "frames", "local_exit", "returncode", "expected"),
    [
        (True, 1, False, 255, True),
        (False, 1, False, 255, False),
        (True, 0, False, 255, False),
        (True, 1, True, 255, False),
        (True, 1, False, None, False),
        (True, 1, False, int(RemoteExit.DETACHED), False),
        (True, 1, False, int(RemoteExit.SOFT_QUIT), False),
        (True, 1, False, int(RemoteExit.HARD_QUIT), False),
    ],
)
def test_automatic_reconnect_classifies_only_unexpected_established_exit(
    enabled,
    frames,
    local_exit,
    returncode,
    expected,
):
    assert (
        fast_display_client.should_automatically_reconnect(
            enabled=enabled,
            painted_frames=frames,
            local_exit=local_exit,
            returncode=returncode,
        )
        is expected
    )


def test_reconnect_wait_local_ctrl_c_and_escape_are_cancellable():
    for byte, exit_code in ((b"\x03", 130), (LOCAL_ESCAPE, 0), (b"", 0)):
        read_fd, write_fd = os.pipe()
        try:
            if byte:
                os.write(write_fd, byte)
            else:
                os.close(write_fd)
                write_fd = -1
            with pytest.raises(fast_display_client.ReconnectCancelled) as exc:
                fast_display_client._consume_reconnect_input(read_fd)
            assert exc.value.exit_code == exit_code
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)


def test_automatic_reconnect_never_requests_takeover_or_interactive_auth(
    monkeypatch,
):
    process = _PreflightProcess()
    reconnect = MagicMock(return_value=(process, RemoteAttachKind.ACCEPTED))
    monkeypatch.setattr(fast_display_client, "_reconnect_remote_attach", reconnect)
    surface = MagicMock()
    args = parse_client_args(["server", "--reconnect"])

    selected = fast_display_client._automatic_reconnect(
        args,
        os.terminal_size((120, 40)),
        surface,
        9,
    )

    assert selected is process
    surface.begin_reconnect.assert_called_once_with()
    reconnect.assert_called_once()
    assert reconnect.call_args.kwargs == {
        "replace_existing_client": False,
        "cancel_fd": 9,
        "timeout": fast_display_client._RECONNECT_ATTEMPT_TIMEOUT,
        "noninteractive": True,
        "existing_session_only": True,
    }
    surface.show_local_status.assert_called_once()


def test_automatic_reconnect_waits_for_busy_helper_lease_without_takeover(
    monkeypatch,
):
    busy = _PreflightProcess()
    accepted = _PreflightProcess()
    reconnect = MagicMock(
        side_effect=(
            (busy, RemoteAttachKind.BUSY),
            (accepted, RemoteAttachKind.ACCEPTED),
        )
    )
    wait = MagicMock()
    monkeypatch.setattr(fast_display_client, "_reconnect_remote_attach", reconnect)
    monkeypatch.setattr(fast_display_client, "_wait_reconnect_delay", wait)
    args = parse_client_args(["server", "--reconnect"])

    selected = fast_display_client._automatic_reconnect(
        args,
        os.terminal_size((120, 40)),
        MagicMock(),
        9,
    )

    assert selected is accepted
    assert busy.terminated
    assert reconnect.call_count == 2
    assert all(
        call.kwargs["replace_existing_client"] is False
        for call in reconnect.call_args_list
    )
    wait.assert_called_once_with(0.5, 9)


def test_reconnect_window_outlives_the_remote_half_open_lease():
    assert (
        fast_display_client._RECONNECT_WINDOW
        > fast_display_server._CLIENT_LEASE_TIMEOUT
    )


def test_ssh_parser_accepts_ordered_exact_and_grouped_arguments():
    args = parse_client_args([
        "server",
        "--ssh-arg=-F",
        "--ssh-args=config -J jump -p 2222",
        "--ssh-arg=ProxyCommand=ssh -W %h:%p gateway",
    ])

    assert args.ssh_arg == [
        "-F",
        "config",
        "-J",
        "jump",
        "-p",
        "2222",
        "ProxyCommand=ssh -W %h:%p gateway",
    ]


def test_ssh_parser_selects_remote_platform_without_changing_default():
    assert parse_client_args(["server"]).remote_platform == "auto"
    assert (
        parse_client_args(["server", "--remote-platform", "windows"]).remote_platform
        == "windows"
    )


def test_reconnect_attach_forces_noninteractive_bounded_ssh(monkeypatch):
    process = _PreflightProcess()
    built = MagicMock(return_value=["ssh", "remote"])
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(fast_display_client, "build_ssh_argv", built)
    monkeypatch.setattr(fast_display_client, "_spawn_remote", spawn)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda *_args, **_kwargs: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda *_args, **_kwargs: RemoteAttachKind.ACCEPTED,
    )
    args = parse_client_args(
        [
            "server",
            "--ssh-arg=-J",
            "--ssh-arg=jump",
        ]
    )

    selected, status = fast_display_client._reconnect_remote_attach(
        args,
        os.terminal_size((120, 40)),
        replace_existing_client=False,
        cancel_fd=9,
        timeout=5.0,
        noninteractive=True,
    )

    assert selected is process
    assert status is RemoteAttachKind.ACCEPTED
    ssh_args = built.call_args.kwargs["ssh_args"]
    assert ssh_args == [
        "-o",
        "BatchMode=yes",
        "-J",
        "jump",
        "-o",
        "ConnectTimeout=5",
    ]
    assert built.call_args.kwargs["replace_existing_client"] is False
    assert built.call_args.kwargs["launch_mode"] is RemoteLaunchMode.POSIX
    spawn.assert_called_once_with(
        ["ssh", "remote"],
        suppress_stderr=True,
    )


def test_reconnect_connect_timeout_keeps_user_first_value(monkeypatch):
    process = _PreflightProcess()
    built = MagicMock(return_value=["ssh", "remote"])
    monkeypatch.setattr(fast_display_client, "build_ssh_argv", built)
    monkeypatch.setattr(
        fast_display_client,
        "_spawn_remote",
        MagicMock(return_value=process),
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda *_args, **_kwargs: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda *_args, **_kwargs: RemoteAttachKind.ACCEPTED,
    )
    args = parse_client_args([
        "server",
        "--ssh-args=-o ConnectTimeout=30",
    ])

    fast_display_client._reconnect_remote_attach(
        args,
        os.terminal_size((120, 40)),
        replace_existing_client=False,
        timeout=5.0,
        noninteractive=True,
    )

    ssh_args = built.call_args.kwargs["ssh_args"]
    assert ssh_args.index("ConnectTimeout=30") < ssh_args.index(
        "ConnectTimeout=5"
    )


def test_reconnect_reuses_selected_windows_launch_mode(monkeypatch):
    process = _PreflightProcess()
    built = MagicMock(return_value=["ssh", "remote"])
    monkeypatch.setattr(fast_display_client, "build_ssh_argv", built)
    monkeypatch.setattr(
        fast_display_client,
        "_spawn_remote",
        MagicMock(return_value=process),
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda *_args, **_kwargs: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(
                fast_display_client.__version__,
                PROTOCOL_VERSION,
                True,
                platform="windows-msys2",
            ),
        ),
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda *_args, **_kwargs: RemoteAttachKind.ACCEPTED,
    )
    args = parse_client_args(["server", "--remote-platform", "windows"])

    fast_display_client._reconnect_remote_attach(
        args,
        os.terminal_size((120, 40)),
        replace_existing_client=False,
    )

    assert built.call_args.kwargs["launch_mode"] is RemoteLaunchMode.DIRECT


def test_remote_command_keeps_railmux_tty_mode_after_user_flags():
    binary = build_remote_command_argv(
        "server",
        remote_args=("remote-server",),
        ssh_args=("-t",),
    )
    cooked = build_remote_command_argv(
        "server",
        remote_args=("config", "--remote-context"),
        ssh_args=("-T",),
        force_tty=True,
    )

    assert binary[:4] == ["ssh", "-t", "-T", "server"]
    assert cooked[:4] == ["ssh", "-T", "-tt", "server"]


def test_local_reconnect_status_is_bounded_to_terminal_bottom_row():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((12, 4)))

    surface.show_local_status("retry\x1b-secret-is-long")

    painted = output.getvalue()
    assert b"\033[?1049h" in painted
    assert b"\033[4;1H\033[2Kretry -secre" in painted
    assert b"is-long" not in painted


def test_startup_surface_uses_alternate_screen_and_restores_primary():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    size = os.terminal_size((80, 24))

    surface.show_startup(size)
    surface.close()

    painted = output.getvalue()
    assert painted.startswith(b"\033[?1049h")
    assert b"\033[?1003h" not in painted
    assert b"\033[?1006h" not in painted
    assert b"\033[?25l" not in painted
    assert b"Restoring your workspace" in painted
    assert painted.endswith(b"\033[?1049l")


def test_startup_interaction_stays_visible_and_returns_to_restoring():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    size = os.terminal_size((80, 24))

    surface.show_startup(size)
    output.seek(0)
    output.truncate()
    surface.begin_interaction()
    output.write(b"Upgrade local Railmux? [y/N] y\r\n")
    surface.show_startup(size)

    painted = output.getvalue()
    assert b"\033[?1049l" not in painted
    assert b"\033[2J\033[H" in painted
    assert b"\033[?25h" in painted
    assert b"Upgrade local Railmux? [y/N] y" in painted
    assert painted.endswith(
        fast_display_client.render_startup_surface(
            size.columns, size.lines
        ).encode("utf-8")
    )
    assert not surface.interaction_active


def test_startup_stage_change_repaints_without_reentering_terminal():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    size = os.terminal_size((80, 24))

    surface.show_startup(size, "Connecting to remote host…")
    output.seek(0)
    output.truncate()
    surface.show_startup(size, "Checking Railmux versions…")

    painted = output.getvalue()
    assert b"\033[?1049h" not in painted
    assert b"Checking Railmux versions" in painted
    assert b"Connecting to remote host" not in painted


def test_repeated_startup_prompt_keeps_previous_install_output_visible():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    size = os.terminal_size((80, 24))

    surface.show_startup(size)
    surface.begin_interaction()
    output.seek(0)
    output.truncate()
    output.write(b"Remote user-site install failed\r\n")
    surface.begin_interaction()

    painted = output.getvalue()
    assert painted == b"Remote user-site install failed\r\n"
    assert surface.interaction_active


def test_first_interactive_paint_activates_mouse_after_startup():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    size = os.terminal_size((20, 4))
    screen = AppliedScreen(
        width=20,
        height=4,
        cursor_x=0,
        cursor_y=0,
        cursor_visible=True,
        terminal_modes=TerminalMode.NONE,
        rows=(b"one", b"two", b"three", b"four"),
        changed_rows=(0, 1, 2, 3),
        clear=True,
    )

    surface.show_startup(size)
    output.seek(0)
    output.truncate()
    surface.paint(screen)

    painted = output.getvalue()
    assert b"\033[?1049h" not in painted
    assert b"\033[?25l" in painted
    assert b"\033[?1003h\033[?1006h" in painted


def test_local_status_preserves_painted_status_left_and_background():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((40, 4)))
    screen = AppliedScreen(
        width=40,
        height=4,
        cursor_x=2,
        cursor_y=2,
        cursor_visible=True,
        terminal_modes=TerminalMode.NONE,
        rows=(
            b"one",
            b"two",
            b"three",
            b"\033[0;30;48;2;95;175;0m Railmux [R][1][2]                    \033[0m",
        ),
        changed_rows=(0, 1, 2, 3),
        clear=True,
    )
    surface.paint(screen)
    output.seek(0)
    output.truncate()

    surface.show_local_status("Copied 12 chars.", level="success")

    painted = output.getvalue()
    assert b"\033[4;1H\033[2K" not in painted
    assert b"\033[4;21H" in painted
    assert b"\033[48;2;95;175;0m\033[1;38;5;17m\033[K" in painted
    assert b"Copied 12 chars." in painted
    assert b"\033[?25h" not in painted
    assert b"\033[?25l" not in painted


def test_reconnect_status_uses_retained_status_right_without_stale_cursor():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((40, 4)))
    screen = AppliedScreen(
        width=40,
        height=4,
        cursor_x=2,
        cursor_y=2,
        cursor_visible=True,
        terminal_modes=TerminalMode.NONE,
        rows=(
            b"one",
            b"two",
            b"three",
            b"\033[0;30;48;2;95;175;0m Railmux [R][1][2]                    \033[0m",
        ),
        changed_rows=(0, 1, 2, 3),
        clear=True,
    )
    surface.paint(screen)
    surface.begin_reconnect()
    output.seek(0)
    output.truncate()

    surface.show_local_status("Reconnecting (attempt 1)")

    painted = output.getvalue()
    assert b"\033[4;1H\033[2K" not in painted
    assert b"\033[4;21H" in painted
    assert b"\033[48;2;95;175;0m" in painted
    assert b"Reconnecting (attem" in painted
    assert painted.endswith(b"\033[0m\033[?25l")
    assert b"\033[3;3H\033[?25h" not in painted


def test_local_status_is_one_clickable_source_and_survives_remote_paint():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((40, 4)))
    screen = AppliedScreen(
        width=40,
        height=4,
        cursor_x=2,
        cursor_y=2,
        cursor_visible=True,
        terminal_modes=TerminalMode.NONE,
        rows=(b"one", b"two", b"three", b"status"),
        changed_rows=(0, 1, 2, 3),
        clear=True,
    )
    surface.paint(screen)
    surface.show_local_status("Exact warning", level="warning")

    click = SgrMouseEvent(b"down", 0, 30, 4, True)
    assert surface.local_status_at(click) == "Exact warning"
    assert surface.local_status_at(
        SgrMouseEvent(b"outside", 0, 10, 4, True)
    ) is None

    output.seek(0)
    output.truncate()
    surface.paint(replace(screen, changed_rows=(3,), clear=False))
    assert b"Exact warning" in output.getvalue()

    with patch(
        "railmux.fast_display_client.local_clipboard.copy",
        return_value=True,
    ) as native:
        assert surface.copy_local_status_at(click)
    native.assert_called_once_with(b"Exact warning")
    assert surface.local_status_at(click) == "Copied status message."

    surface.clear_local_status()
    assert surface.local_status_at(
        SgrMouseEvent(b"down", 0, 30, 4, True)
    ) is None


def test_interruptible_connection_status_yields_to_first_user_action():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.show_local_status(
        "Ctrl-] disconnects · reconnect on",
        interruptible=True,
    )

    assert surface.dismiss_interruptible_local_status()
    assert not surface.dismiss_interruptible_local_status()
    assert surface.local_status_at(
        SgrMouseEvent(b"down", 0, 1, 1, True)
    ) is None

    surface.show_local_status("Checking remote path…")
    assert not surface.dismiss_interruptible_local_status()


def test_timed_termux_hint_expires_and_newer_status_cancels_its_deadline():
    surface = TerminalSurface(io.BytesIO())
    surface.show_local_status(
        "Tap the prompt again to open the keyboard",
        interruptible=True,
        expires_at=7.0,
    )

    assert not surface.expire_local_status(6.99)
    assert surface.expire_local_status(7.0)
    assert surface._local_status_text is None

    surface.show_local_status(
        "Tap the prompt again to open the keyboard",
        interruptible=True,
        expires_at=8.0,
    )
    surface.show_local_status("Checking remote path…")
    assert not surface.expire_local_status(9.0)
    assert surface._local_status_text == "Checking remote path…"


def test_path_open_prompt_names_the_inside_surface_as_managed_vim():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((60, 12)))

    surface.show_path_open_prompt()

    painted = output.getvalue()
    assert b"Always use Railmux managed Vim" in painted
    assert b"Use Railmux managed Vim this time" in painted


def test_claude_history_prompt_is_local_bounded_and_mouse_selectable():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((40, 12)))

    surface.show_claude_history_prompt()

    painted = output.getvalue()
    assert b"Claude Code history" in painted
    assert b"Always use smooth local history" in painted
    assert b"Claude native" in painted
    assert b"\033[1;38;5;220m[1]\033[0m" in painted
    assert surface.claude_history_prompt_choice(SgrMouseEvent(b"", 0, 20, 4, True)) == (
        "local",
        True,
    )
    assert surface.claude_history_prompt_choice(SgrMouseEvent(b"", 0, 20, 5, True)) == (
        "local",
        False,
    )
    assert surface.claude_history_prompt_choice(SgrMouseEvent(b"", 0, 20, 6, True)) == (
        "native",
        True,
    )
    assert surface.claude_history_prompt_choice(SgrMouseEvent(b"", 0, 20, 7, True)) == (
        "native",
        False,
    )
    assert (
        surface.claude_history_prompt_choice(SgrMouseEvent(b"", 0, 20, 8, True)) is None
    )


def test_claude_history_save_confirmation_has_a_bounded_wait():
    timeout = fast_display_client._CLAUDE_HISTORY_SAVE_TIMEOUT

    assert not fast_display_client.claude_history_save_timed_out(None, 100.0)
    assert not fast_display_client.claude_history_save_timed_out(
        100.0, 100.0 + timeout - 0.001
    )
    assert fast_display_client.claude_history_save_timed_out(100.0, 100.0 + timeout)


class _PreflightProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode or 0

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def _accept_attach(monkeypatch):
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda _process: RemoteAttachKind.ACCEPTED,
    )


def test_compatible_remote_is_confirmed_before_attach(monkeypatch):
    _accept_attach(monkeypatch)
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )

    stages = []
    selected = prepare_remote_process(
        args,
        os.terminal_size((120, 40)),
        on_stage=stages.append,
    )

    assert selected is process
    assert process.stdin.getvalue() == REMOTE_START
    assert stages == [
        "Connecting to remote host…",
        "Checking Railmux versions…",
        "Attaching to workspace…",
    ]


def test_auto_remote_launch_falls_back_to_windows_and_pins_reconnect_mode(
    monkeypatch,
):
    _accept_attach(monkeypatch)
    posix = _PreflightProcess(1)
    windows = _PreflightProcess()
    processes = iter((posix, windows))
    commands = []

    def spawn(argv):
        commands.append(argv)
        return next(processes)

    startups = iter(
        (
            RemoteStartup(RemoteStartKind.FAILED, returncode=1),
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello(
                    fast_display_client.__version__,
                    PROTOCOL_VERSION,
                    True,
                    platform="windows-msys2",
                ),
            ),
        )
    )
    monkeypatch.setattr(fast_display_client, "_spawn_remote", spawn)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: next(startups),
    )
    args = parse_client_args(["server"])

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is windows
    assert posix.terminated is False
    assert "if [" in commands[0][-1]
    assert commands[1][-1].startswith("railmux remote-server ")
    assert args._selected_remote_launch_mode is RemoteLaunchMode.DIRECT
    assert windows.stdin.getvalue() == REMOTE_START


def test_auto_direct_fallback_accepts_authoritative_posix_hello(monkeypatch):
    posix_shell = _PreflightProcess(1)
    direct = _PreflightProcess()
    processes = iter((posix_shell, direct))
    startups = iter((
        RemoteStartup(RemoteStartKind.FAILED, returncode=1),
        RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(
                fast_display_client.__version__,
                PROTOCOL_VERSION,
                True,
                platform="posix",
            ),
        ),
    ))
    timeouts = []
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: next(processes))
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: (
            timeouts.append(timeout), next(startups))[1],
    )

    probe = fast_display_client.probe_remote_launch(
        "server",
        remote_args=("remote-server",),
        ssh_args=(),
        timeout=4.0,
    )

    assert probe.process is direct
    assert probe.launch_mode is RemoteLaunchMode.DIRECT
    assert probe.startup.hello is not None
    assert probe.startup.hello.platform == "posix"
    assert timeouts == [4.0, 4.0]


def test_explicit_windows_rejects_direct_posix_hello(monkeypatch):
    process = _PreflightProcess()
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda *_args, **_kwargs: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(
                fast_display_client.__version__,
                PROTOCOL_VERSION,
                True,
                platform="posix",
            ),
        ),
    )

    with pytest.raises(fast_display_client.ProbeError, match="non-Windows"):
        fast_display_client.probe_remote_launch(
            "server",
            remote_args=("remote-server",),
            ssh_args=(),
            remote_platform="windows",
        )


def test_explicit_windows_remote_failure_never_runs_posix_installer(monkeypatch):
    process = _PreflightProcess(1)
    args = parse_client_args(["server", "--remote-platform", "windows"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.FAILED, returncode=1),
    )
    install = MagicMock()
    monkeypatch.setattr(fast_display_client, "_install_remote_and_start", install)

    with pytest.raises(fast_display_client.ProbeError) as exc:
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "py -m pip install --upgrade" in str(exc.value)
    assert "railmux runtime install --yes" in str(exc.value)
    install.assert_not_called()


def test_reconnect_flag_does_not_change_initial_ssh_command():
    plain = parse_client_args(["server"])
    reconnecting = parse_client_args(["server", "--reconnect"])

    def command(args):
        return build_ssh_argv(
            args.destination,
            session=args.session,
            width=120,
            height=40,
            fps=args.fps,
            ssh_args=args.ssh_arg,
        )

    assert command(reconnecting) == command(plain)


def test_first_frame_timeout_applies_only_while_waiting():
    assert not fast_display_client.first_frame_timed_out(None, 100.0)
    assert not fast_display_client.first_frame_timed_out(100.0, 99.999)
    assert fast_display_client.first_frame_timed_out(100.0, 100.0)


def test_busy_legacy_attach_can_be_replaced_once_with_consent(monkeypatch):
    original = _PreflightProcess()
    retry = _PreflightProcess()
    replacement = _PreflightProcess()
    args = parse_client_args(["server"])
    statuses = iter(
        (
            RemoteAttachKind.BUSY,
            RemoteAttachKind.BUSY,
            RemoteAttachKind.ACCEPTED,
        )
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda _process: next(statuses),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: True)
    built = MagicMock(return_value=["ssh", "reconnect"])
    monkeypatch.setattr(fast_display_client, "build_ssh_argv", built)
    reconnects = iter((retry, replacement))
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: next(reconnects)
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )

    selected = fast_display_client._finish_remote_attach(
        args, os.terminal_size((120, 40)), original
    )

    assert selected is replacement
    assert original.terminated
    assert retry.terminated
    assert replacement.stdin.getvalue() == REMOTE_START
    assert [
        call.kwargs["replace_existing_client"] for call in built.call_args_list
    ] == [False, True]


def test_transient_current_attach_contention_retries_without_takeover(monkeypatch):
    original = _PreflightProcess()
    retry = _PreflightProcess()
    args = parse_client_args(["server"])
    statuses = iter((RemoteAttachKind.BUSY, RemoteAttachKind.ACCEPTED))
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda _process: next(statuses),
    )
    confirm = MagicMock()
    monkeypatch.setattr(fast_display_client, "_confirm", confirm)
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: retry)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )

    selected = fast_display_client._finish_remote_attach(
        args, os.terminal_size((120, 40)), original
    )

    assert selected is retry
    assert original.terminated
    confirm.assert_not_called()


def test_busy_attach_decline_leaves_remote_session_untouched(monkeypatch):
    process = _PreflightProcess()
    retry = _PreflightProcess()
    args = parse_client_args(["server"])
    statuses = iter((RemoteAttachKind.BUSY, RemoteAttachKind.BUSY))
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda _process: next(statuses),
    )
    events = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda _question: events.append("confirm") or False,
    )
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: retry)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )

    with pytest.raises(fast_display_client.ProbeError, match="still owned"):
        fast_display_client._finish_remote_attach(
            args,
            os.terminal_size((120, 40)),
            process,
            before_interaction=lambda: events.append("reveal"),
        )

    assert process.terminated
    assert retry.terminated
    assert events == ["reveal", "confirm"]


def test_missing_remote_prompts_then_installs_and_starts(monkeypatch):
    _accept_attach(monkeypatch)
    missing = _PreflightProcess(127)
    installed = _PreflightProcess()
    args = parse_client_args(["server"])
    spawn = MagicMock(return_value=missing)
    monkeypatch.setattr(fast_display_client, "_spawn_remote", spawn)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.MISSING, returncode=127),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: True)
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_and_start",
        lambda _args, _size, _version: (
            installed,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
            ),
        ),
    )

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is installed
    assert installed.stdin.getvalue() == REMOTE_START
    spawn.assert_called_once()


def test_missing_remote_decline_returns_copyable_install_help(
    monkeypatch,
):
    process = _PreflightProcess(127)
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.MISSING, returncode=127),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: False)

    with pytest.raises(fast_display_client.ProbeError) as exc:
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "python3 -m pip install --user" in str(exc.value)
    assert fast_display_client.__version__ in str(exc.value)


def test_remote_without_tmux_gives_system_package_guidance(monkeypatch):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True, False),
        ),
    )

    with pytest.raises(fast_display_client.ProbeError, match="tmux is not"):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert process.terminated


def test_newer_compatible_remote_prompts_for_local_upgrade_but_can_continue(
    monkeypatch,
    capsys,
):
    _accept_attach(monkeypatch)
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or False,
    )

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is process
    assert "Upgrade local Railmux to 999.0?" in questions[0]
    assert "protocol" not in questions[0].lower()
    assert process.stdin.getvalue() == REMOTE_START
    assert "continuing with local Railmux" in capsys.readouterr().err


def test_newer_remote_protocol_can_upgrade_and_restart_local_client(monkeypatch):
    process = _PreflightProcess()
    args = parse_client_args(["server", "--fps", "30"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION + 1, True),
        ),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: True)

    class Restarted(Exception):
        pass

    def restart(version, raw_args):
        assert version == "999.0"
        assert raw_args == ("server", "--fps", "30")
        raise Restarted

    monkeypatch.setattr(fast_display_client, "_upgrade_local_and_restart", restart)

    with pytest.raises(Restarted):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert process.terminated


def test_local_upgrade_reveals_prompt_then_restores_primary_before_exec(
    monkeypatch,
):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    events = []
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION + 1, True),
        ),
    )
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda _question: events.append("confirm") or True,
    )

    class Restarted(Exception):
        pass

    def restart(_version, _raw_args):
        events.append("restart")
        raise Restarted

    monkeypatch.setattr(fast_display_client, "_upgrade_local_and_restart", restart)

    with pytest.raises(Restarted):
        prepare_remote_process(
            args,
            os.terminal_size((120, 40)),
            before_interaction=lambda: events.append("reveal"),
            before_local_restart=lambda: events.append("restore"),
        )

    assert events == ["reveal", "confirm", "restore", "restart"]
    assert process.terminated


def test_released_021_client_can_offer_upgrade_to_remote_022(monkeypatch):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "__version__", "0.2.1")
    monkeypatch.setattr(fast_display_client, "PROTOCOL_VERSION", 6)
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("0.2.2", 7, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or True,
    )

    class Restarted(Exception):
        pass

    def restart(version, raw_args):
        assert version == "0.2.2"
        assert raw_args == ("server",)
        raise Restarted

    monkeypatch.setattr(fast_display_client, "_upgrade_local_and_restart", restart)

    with pytest.raises(Restarted):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert process.terminated
    assert "Remote Railmux 0.2.2 is newer than local 0.2.1" in questions[0]
    assert "Upgrade local Railmux to 0.2.2?" in questions[0]
    assert "requires SSH protocol v7" in questions[0]


def test_newer_protocol_with_non_newer_package_cannot_downgrade_local(
    monkeypatch,
):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION + 1, True),
        ),
    )
    confirm = MagicMock()
    monkeypatch.setattr(fast_display_client, "_confirm", confirm)

    with pytest.raises(
        fast_display_client.ProbeError, match="unsafe automatic local downgrade"
    ):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    confirm.assert_not_called()
    assert process.terminated


def test_local_upgrade_uses_current_python_user_site_and_restarts(monkeypatch):
    monkeypatch.setattr(fast_display_client.sys, "prefix", "/usr")
    monkeypatch.setattr(fast_display_client.sys, "base_prefix", "/usr")
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(fast_display_client.subprocess, "run", run)
    monkeypatch.setattr(
        "railmux.self_update.installed_version_matches", lambda _version: True
    )

    class Restarted(Exception):
        pass

    observed = {}

    def execv(executable, argv):
        observed["executable"] = executable
        observed["argv"] = argv
        raise Restarted

    monkeypatch.setattr(fast_display_client.os, "execv", execv)

    with pytest.raises(Restarted):
        fast_display_client._upgrade_local_and_restart(
            "1.2.3", ("server", "--fps", "30")
        )

    install = run.call_args.args[0]
    assert install == [
        fast_display_client.sys.executable,
        "-m",
        "pip",
        "install",
        "--user",
        "--upgrade",
        "railmux==1.2.3",
    ]
    assert observed["argv"] == [
        fast_display_client.sys.executable,
        "-m",
        "railmux",
        "ssh",
        "server",
        "--fps",
        "30",
    ]


def test_older_remote_protocol_prompts_for_matching_remote_upgrade(monkeypatch):
    _accept_attach(monkeypatch)
    old = _PreflightProcess()
    upgraded = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: old)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("0.1.0", PROTOCOL_VERSION - 1, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or True,
    )
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_and_start",
        lambda _args, _size, _version: (
            upgraded,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
            ),
        ),
    )

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is upgraded
    assert "uses older SSH protocol" in questions[0]
    assert old.terminated
    assert upgraded.stdin.getvalue() == REMOTE_START


def test_windows_remote_upgrade_fails_closed_with_windows_guidance(monkeypatch):
    process = _PreflightProcess()
    args = parse_client_args(["server", "--remote-platform", "windows"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(
                "0.1.0",
                PROTOCOL_VERSION - 1,
                True,
                platform="windows-msys2",
            ),
        ),
    )
    confirm = MagicMock()
    install = MagicMock()
    monkeypatch.setattr(fast_display_client, "_confirm", confirm)
    monkeypatch.setattr(fast_display_client, "_install_remote_and_start", install)

    with pytest.raises(fast_display_client.ProbeError) as exc:
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "py -m pip install --upgrade" in str(exc.value)
    assert "railmux runtime install --yes" in str(exc.value)
    confirm.assert_not_called()
    install.assert_not_called()


def test_older_compatible_remote_can_be_upgraded_to_local_version(monkeypatch):
    _accept_attach(monkeypatch)
    monkeypatch.setattr(fast_display_client, "__version__", "0.2.5")
    old = _PreflightProcess()
    upgraded = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: old)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("0.2.4", PROTOCOL_VERSION, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or True,
    )
    installed_versions = []

    def install(_args, _size, version):
        installed_versions.append(version)
        return (
            upgraded,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello("0.2.5", PROTOCOL_VERSION, True),
            ),
        )

    monkeypatch.setattr(fast_display_client, "_install_remote_and_start", install)

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is upgraded
    assert installed_versions == ["0.2.5"]
    assert "Remote Railmux 0.2.4 is older than local 0.2.5" in questions[0]
    assert old.terminated
    assert upgraded.stdin.getvalue() == REMOTE_START


def test_older_compatible_remote_can_continue_when_upgrade_declined(
    monkeypatch,
    capsys,
):
    _accept_attach(monkeypatch)
    monkeypatch.setattr(fast_display_client, "__version__", "0.2.5")
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("0.2.4", PROTOCOL_VERSION, True),
        ),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: False)

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is process
    assert not process.terminated
    assert process.stdin.getvalue() == REMOTE_START
    assert "continuing with compatible remote Railmux 0.2.4" in (
        capsys.readouterr().err
    )


def test_higher_remote_version_is_offered_to_local_before_protocol_direction(
    monkeypatch,
):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION - 1, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or False,
    )

    with pytest.raises(fast_display_client.ProbeError, match="newer remote"):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "Remote Railmux 999.0 is newer than local" in questions[0]
    assert "Upgrade local Railmux to 999.0?" in questions[0]
    assert process.terminated


def test_declining_local_upgrade_does_not_downgrade_remote_dependency_repair(
    monkeypatch,
):
    _accept_attach(monkeypatch)
    process = _PreflightProcess()
    repaired = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION, False),
        ),
    )
    answers = iter((False, True))
    monkeypatch.setattr(
        fast_display_client, "_confirm", lambda _question: next(answers)
    )
    installed_versions = []

    def install(_args, _size, version):
        installed_versions.append(version)
        return (
            repaired,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello("999.0", PROTOCOL_VERSION, True),
            ),
        )

    monkeypatch.setattr(fast_display_client, "_install_remote_and_start", install)

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is repaired
    assert installed_versions == ["999.0"]
    assert process.terminated
    assert repaired.stdin.getvalue() == REMOTE_START


def test_failed_remote_auto_install_returns_manual_recovery(monkeypatch):
    missing = _PreflightProcess(127)
    failed = _PreflightProcess(1)
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: missing)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.MISSING, returncode=127),
    )
    answers = iter((True, False))
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or next(answers),
    )
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_and_start",
        lambda _args, _size, _version: (
            failed,
            RemoteStartup(RemoteStartKind.FAILED, returncode=1),
        ),
    )

    with pytest.raises(fast_display_client.ProbeError) as exc:
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "user-site installation failed" in str(exc.value)
    assert "matching wheel or source checkout" in str(exc.value)
    assert "Remote user-site installation failed" in questions[1]


@pytest.mark.parametrize(
    "install_kind", [RemoteStartKind.FAILED, RemoteStartKind.TIMEOUT]
)
def test_failed_user_site_install_can_fall_back_to_private_venv(
    monkeypatch,
    install_kind,
):
    _accept_attach(monkeypatch)
    missing = _PreflightProcess(127)
    failed = _PreflightProcess(1)
    installed = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: missing)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.MISSING, returncode=127),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or True,
    )
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_and_start",
        lambda _args, _size, _version: (
            failed,
            RemoteStartup(install_kind, returncode=1),
        ),
    )
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_private_venv_and_start",
        lambda _args, _size, _version: (
            installed,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
            ),
        ),
    )

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is installed
    assert failed.poll() == 1
    assert "Remote user-site installation failed or timed out" in questions[1]
    assert installed.stdin.getvalue() == REMOTE_START


def test_remote_server_has_no_bare_tmux_server_argv():
    source = inspect.getsource(fast_display_server)

    assert '["tmux",' not in source
    assert "['tmux'," not in source


def test_remote_server_hello_reports_version_protocol_and_dependency(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setattr(fast_display_server.shutil, "which", lambda _name: "/tmux")
    monkeypatch.setattr(fast_display_server.sys, "stdout", MagicMock(buffer=output))

    fast_display_server._emit_remote_hello(True)

    hello = parse_remote_hello(output.getvalue())
    assert hello == RemoteHello(
        fast_display_client.__version__,
        PROTOCOL_VERSION,
        True,
        config_protocol=REMOTE_CONFIG_PROTOCOL,
    )


def test_remote_server_hello_identifies_managed_windows_runtime(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setenv("RAILMUX_WINDOWS_RUNTIME", "msys2")
    monkeypatch.setattr(fast_display_server.shutil, "which", lambda _name: "/tmux")
    monkeypatch.setattr(fast_display_server.sys, "stdout", MagicMock(buffer=output))

    fast_display_server._emit_remote_hello(True)

    hello = parse_remote_hello(output.getvalue())
    assert hello.platform == "windows-msys2"


def test_remote_server_waits_for_exact_start_confirmation(monkeypatch):
    remote_input = MagicMock(buffer=io.BytesIO(REMOTE_START))
    monkeypatch.setattr(fast_display_server.sys, "stdin", remote_input)
    monkeypatch.setattr(
        fast_display_server.select,
        "select",
        lambda *_args: ([remote_input.buffer], [], []),
    )

    assert fast_display_server._await_client_start() is True

    remote_input.buffer = io.BytesIO(b"wrong\n")
    assert fast_display_server._await_client_start() is False


def test_remote_server_missing_dependency_never_touches_tmux(monkeypatch):
    monkeypatch.setattr(fast_display_server, "_fast_dependency_ready", lambda: False)
    emit = MagicMock()
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", emit)
    monkeypatch.setattr(fast_display_server, "_await_client_start", lambda: True)
    socket_label = MagicMock()
    monkeypatch.setattr(fast_display_server.tmux_server, "socket_label", socket_label)

    result = fast_display_server.main(
        [
            "--protocol",
            str(PROTOCOL_VERSION),
            "--width",
            "80",
            "--height",
            "24",
        ]
    )

    assert result == 2
    emit.assert_called_once_with(False)
    socket_label.assert_not_called()


def test_remote_server_reports_missing_configured_tmux_without_fallback(
    monkeypatch,
):
    monkeypatch.setattr(
        fast_display_server,
        "load_config",
        lambda: Config(tmux_binary="/missing/bin/tmux"),
    )
    monkeypatch.setattr(
        fast_display_server,
        "check_executable",
        lambda *_args, **_kwargs: MagicMock(valid=False),
    )
    monkeypatch.setattr(fast_display_server, "_fast_dependency_ready", lambda: True)
    emit = MagicMock()
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", emit)
    monkeypatch.setattr(fast_display_server, "_await_client_start", lambda: True)
    socket_label = MagicMock()
    monkeypatch.setattr(fast_display_server.tmux_server, "socket_label", socket_label)

    result = fast_display_server.main(
        ["--protocol", str(PROTOCOL_VERSION), "--width", "80", "--height", "24"]
    )

    assert result == 2
    emit.assert_called_once_with(
        True,
        config_status="valid",
        tmux_configured=True,
        tmux_available=False,
    )
    socket_label.assert_not_called()


def test_remote_protocol_probe_exits_quietly_before_local_upgrade_prompt(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(fast_display_server, "_fast_dependency_ready", lambda: True)
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", MagicMock())
    monkeypatch.setattr(fast_display_server, "_await_client_start", lambda: False)

    result = fast_display_server.main(
        [
            "--protocol",
            str(PROTOCOL_VERSION - 1),
            "--width",
            "80",
            "--height",
            "24",
        ]
    )

    assert result == 2
    assert capsys.readouterr().err == ""


def test_remote_server_attaches_only_after_start_confirmation(monkeypatch):
    monkeypatch.setattr(fast_display_server, "_fast_dependency_ready", lambda: True)
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", MagicMock())
    monkeypatch.setattr(fast_display_server, "_await_client_start", lambda: True)
    monkeypatch.setattr(
        fast_display_server.tmux_server, "socket_label", lambda: "railmux"
    )
    serve = MagicMock(return_value=17)
    monkeypatch.setattr(fast_display_server, "serve", serve)

    result = fast_display_server.main(
        [
            "--protocol",
            str(PROTOCOL_VERSION),
            "--session",
            "custom",
            "--width",
            "80",
            "--height",
            "24",
            "--fps",
            "30",
        ]
    )

    assert result == 17
    serve.assert_called_once_with(
        "custom",
        80,
        24,
        30.0,
        replace_existing_client=False,
        existing_session_only=False,
    )


def test_remote_server_existing_only_flag_reaches_attach_boundary(monkeypatch):
    monkeypatch.setattr(fast_display_server, "_fast_dependency_ready", lambda: True)
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", MagicMock())
    monkeypatch.setattr(fast_display_server, "_await_client_start", lambda: True)
    monkeypatch.setattr(
        fast_display_server.tmux_server, "socket_label", lambda: "railmux"
    )
    serve = MagicMock(return_value=0)
    monkeypatch.setattr(fast_display_server, "serve", serve)

    assert (
        fast_display_server.main(
            [
                "--protocol",
                str(PROTOCOL_VERSION),
                "--width",
                "80",
                "--height",
                "24",
                "--existing-session-only",
            ]
        )
        == 0
    )
    serve.assert_called_once_with(
        "railmux",
        80,
        24,
        20.0,
        replace_existing_client=False,
        existing_session_only=True,
    )


def test_remote_server_busy_status_is_machine_readable(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setattr(fast_display_server, "_fast_dependency_ready", lambda: True)
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", MagicMock())
    monkeypatch.setattr(fast_display_server, "_await_client_start", lambda: True)
    monkeypatch.setattr(
        fast_display_server.tmux_server, "socket_label", lambda: "railmux"
    )
    monkeypatch.setattr(
        fast_display_server,
        "serve",
        MagicMock(side_effect=fast_display_server.DisplayServerBusy("held")),
    )
    monkeypatch.setattr(fast_display_server.sys, "stdout", MagicMock(buffer=output))

    result = fast_display_server.main(
        [
            "--protocol",
            str(PROTOCOL_VERSION),
            "--width",
            "80",
            "--height",
            "24",
        ]
    )

    assert result == 2
    assert output.getvalue() == REMOTE_ATTACH_BUSY


def test_current_attach_lock_is_released_before_display_lifetime(monkeypatch):
    events = []
    monkeypatch.setattr(
        fast_display_server, "_ensure_railmux_session", lambda _session: "$4"
    )
    monkeypatch.setattr(fast_display_server, "_validate_railmux", lambda _session: "$4")
    monkeypatch.setattr(
        fast_display_server,
        "_acquire_display_lock",
        lambda _session, **_kwargs: events.append("acquire") or 9,
    )
    monkeypatch.setattr(
        fast_display_server,
        "_release_display_lock",
        lambda _fd: events.append("release"),
    )
    monkeypatch.setattr(
        fast_display_server, "_use_smallest_window_size", lambda _session: None
    )
    monkeypatch.setattr(
        fast_display_server,
        "_spawn_tmux_client",
        lambda *_args: events.append("spawn") or (123, 10),
    )
    monkeypatch.setattr(
        fast_display_server, "_wait_until_attached", lambda *_args: True
    )
    monkeypatch.setattr(
        fast_display_server, "_emit_attach_status", lambda _status: None
    )

    def display(*_args):
        events.append("display")
        assert events.index("release") < events.index("display")
        return 17

    monkeypatch.setattr(fast_display_server, "_serve_attached", display)

    assert fast_display_server.serve("railmux", 80, 24, 30.0) == 17
    assert events == ["acquire", "spawn", "release", "display"]


def test_existing_session_only_attach_never_creates_outer_session(monkeypatch):
    ensure = MagicMock(side_effect=AssertionError("must not create"))
    monkeypatch.setattr(fast_display_server, "_ensure_railmux_session", ensure)
    monkeypatch.setattr(fast_display_server, "_validate_railmux", lambda _session: "$4")
    monkeypatch.setattr(
        fast_display_server, "_acquire_display_lock", lambda *_args, **_kwargs: 9
    )
    monkeypatch.setattr(fast_display_server, "_release_display_lock", lambda _fd: None)
    monkeypatch.setattr(
        fast_display_server, "_use_smallest_window_size", lambda _session: None
    )
    monkeypatch.setattr(
        fast_display_server, "_spawn_tmux_client", lambda *_args: (123, 10)
    )
    monkeypatch.setattr(
        fast_display_server, "_wait_until_attached", lambda *_args: True
    )
    monkeypatch.setattr(
        fast_display_server, "_emit_attach_status", lambda _status: None
    )
    monkeypatch.setattr(fast_display_server, "_serve_attached", lambda *_args: 0)

    assert (
        fast_display_server.serve(
            "railmux",
            80,
            24,
            30.0,
            existing_session_only=True,
        )
        == 0
    )
    ensure.assert_not_called()


def test_replacement_reenumerates_clients_inside_attach_lock(monkeypatch):
    detached = []
    monkeypatch.setattr(fast_display_server, "_validate_railmux", lambda _session: "$4")
    monkeypatch.setattr(
        fast_display_server,
        "_detach_session_clients",
        lambda session: detached.append(session),
    )
    monkeypatch.setattr(
        fast_display_server, "_acquire_display_lock", lambda *_args, **_kwargs: 9
    )
    monkeypatch.setattr(fast_display_server, "_release_display_lock", lambda _fd: None)
    monkeypatch.setattr(
        fast_display_server, "_use_smallest_window_size", lambda _session: None
    )
    monkeypatch.setattr(
        fast_display_server, "_spawn_tmux_client", lambda *_args: (123, 10)
    )
    monkeypatch.setattr(
        fast_display_server, "_wait_until_attached", lambda *_args: True
    )
    monkeypatch.setattr(
        fast_display_server, "_emit_attach_status", lambda _status: None
    )
    monkeypatch.setattr(fast_display_server, "_serve_attached", lambda *_args: 0)

    assert (
        fast_display_server.serve("railmux", 80, 24, 30.0, replace_existing_client=True)
        == 0
    )
    assert detached == ["$4", "$4"]


def test_server_starts_default_railmux_with_current_python(monkeypatch):
    identities = iter((None, "$7"))
    monkeypatch.setattr(
        fast_display_server, "_try_session_id", lambda _session: next(identities)
    )
    monkeypatch.setattr(
        fast_display_server, "_live_controller", lambda session_id: "%9"
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert fast_display_server._ensure_railmux_session("railmux") == "$7"
    assert calls[0][0][:7] == [
        "tmux",
        "-L",
        "railmux",
        "new-session",
        "-d",
        "-s",
        "railmux",
    ]
    assert shlex.split(calls[0][0][-1]) == [
        sys.executable,
        "-m",
        "railmux",
        "--inside-tmux",
        "--no-scroll-coalescing",
    ]


def test_server_does_not_change_an_existing_railmux_scroll_policy(monkeypatch):
    monkeypatch.setattr(fast_display_server, "_try_session_id", lambda _session: "$7")
    run = MagicMock(side_effect=AssertionError("existing session was restarted"))
    monkeypatch.setattr(subprocess, "run", run)

    assert fast_display_server._ensure_railmux_session("railmux") == "$7"
    run.assert_not_called()


def test_display_lock_is_scoped_by_socket_and_session(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fast_display_server.restart_state, "runtime_state_dir", lambda: tmp_path
    )
    sockets = iter(("/tmp/server-a/railmux", "/tmp/server-b/railmux"))
    monkeypatch.setattr(
        fast_display_server, "_tmux_output", lambda *_args: next(sockets)
    )

    first = fast_display_server._acquire_display_lock("$0")
    fast_display_server._release_display_lock(first)
    second = fast_display_server._acquire_display_lock("$0")
    fast_display_server._release_display_lock(second)

    locks = sorted(path.name for path in tmp_path.glob("fast-display-*.lock"))
    assert len(locks) == 2
    assert all(name.endswith("-0.lock") for name in locks)


def test_display_lock_reports_busy_without_unlinking_live_owner(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        fast_display_server.restart_state, "runtime_state_dir", lambda: tmp_path
    )
    monkeypatch.setattr(
        fast_display_server, "_tmux_output", lambda *_args: "/tmp/tmux/railmux"
    )
    first = fast_display_server._acquire_display_lock("$0")
    try:
        with pytest.raises(fast_display_server.DisplayServerBusy):
            fast_display_server._acquire_display_lock("$0", timeout=0)
    finally:
        fast_display_server._release_display_lock(first)


def test_attach_confirmation_matches_exact_child_pid(monkeypatch):
    rows = iter(("$4 998\n", "$4 123\n"))
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "check_output",
        lambda *_args, **_kwargs: next(rows),
    )
    monkeypatch.setattr(fast_display_server.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(fast_display_server, "_child_exited", lambda _pid: False)

    assert fast_display_server._wait_until_attached("$4", 123, timeout=1.0) is True


def test_compact_resize_preparation_waits_for_exact_controller_ack(monkeypatch):
    outputs = iter((
        "180 40 3 %8",
        "ready:0011223344556677:105:20",
    ))
    monkeypatch.setattr(
        fast_display_server, "_compact_tmux_output",
        lambda *_args: next(outputs),
    )
    set_option = MagicMock(return_value=True)
    monkeypatch.setattr(
        fast_display_server, "_set_compact_resize_option", set_option)
    sent = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(fast_display_server.subprocess, "run", sent)
    monkeypatch.setattr(
        fast_display_server.secrets,
        "token_hex",
        lambda _length: "0011223344556677",
    )
    progress = MagicMock()

    assert fast_display_server._request_compact_resize_preparation(
        "$4", 105, 20, progress=progress,
    ) == "ready:0011223344556677:105:20"

    set_option.assert_called_once_with(
        "$4", "request:0011223344556677:105:20")
    assert sent.call_args.args[0][-5:] == [
        "send-keys", "-l", "-t", "%8", "\x1b[34~",
    ]
    progress.assert_called_once_with()


def test_compact_resize_preparation_times_out_fail_open(monkeypatch):
    monkeypatch.setattr(
        fast_display_server,
        "_compact_tmux_output",
        lambda *_args: "180 40 3 %8",
    )
    set_option = MagicMock(return_value=True)
    clear = MagicMock()
    monkeypatch.setattr(
        fast_display_server, "_set_compact_resize_option", set_option)
    monkeypatch.setattr(
        fast_display_server, "_clear_compact_resize_option_if", clear)
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    monkeypatch.setattr(
        fast_display_server.secrets,
        "token_hex",
        lambda _length: "0011223344556677",
    )

    assert fast_display_server._request_compact_resize_preparation(
        "$4", 70, 18, timeout=0,
    ) is None
    clear.assert_called_once_with(
        "$4", "request:0011223344556677:70:18")


def test_noncompact_resize_never_contacts_controller(monkeypatch):
    output = MagicMock()
    monkeypatch.setattr(
        fast_display_server, "_compact_tmux_output", output)

    assert fast_display_server._request_compact_resize_preparation(
        "$4", 120, 30) is None
    output.assert_not_called()


def test_server_window_size_policy_accepts_native_old_tmux(monkeypatch):
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1),
    )
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "tmux 2.8\n",
    )

    fast_display_server._use_smallest_window_size("$4")


def test_server_window_size_policy_fails_closed_on_modern_tmux(monkeypatch):
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1),
    )
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "tmux 3.5a\n",
    )

    with pytest.raises(fast_display_server.DisplayServerError, match="multi-terminal"):
        fast_display_server._use_smallest_window_size("$4")


def test_server_window_size_policy_retries_a_transient_failure(monkeypatch):
    results = iter(
        (
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0),
        )
    )
    run = MagicMock(side_effect=lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(fast_display_server.subprocess, "run", run)
    monkeypatch.setattr(fast_display_server.time, "sleep", lambda _delay: None)

    fast_display_server._use_smallest_window_size("$4")

    assert run.call_count == 2


def test_server_does_not_auto_start_a_custom_missing_session(monkeypatch):
    monkeypatch.setattr(fast_display_server, "_try_session_id", lambda _session: None)

    with pytest.raises(fast_display_server.DisplayServerError, match="default"):
        fast_display_server._ensure_railmux_session("custom")


@pytest.mark.parametrize(
    ("resolved", "controller", "expected"),
    [
        (None, None, fast_display_server.RemoteExit.HARD_QUIT),
        ("$4", None, fast_display_server.RemoteExit.SOFT_QUIT),
        ("$4", "%8", fast_display_server.RemoteExit.DETACHED),
    ],
)
def test_server_classifies_remote_lifecycle(
    monkeypatch, resolved, controller, expected
):
    monkeypatch.setattr(
        fast_display_server, "_try_session_id", lambda _session: resolved
    )
    monkeypatch.setattr(
        fast_display_server, "_live_controller", lambda _session: controller
    )

    assert fast_display_server._classify_remote_exit("$4") is expected


def test_observed_soft_quit_intent_skips_tmux_requery(monkeypatch):
    target = fast_display_server.tmux_server.TmuxServerTarget("/tmp/tmux/railmux", 123)
    intended = MagicMock(return_value=True)
    classify = MagicMock()
    consume = MagicMock()
    monkeypatch.setattr(fast_display_server.tmux_health, "soft_exit_intended", intended)
    monkeypatch.setattr(fast_display_server, "_classify_remote_exit", classify)
    monkeypatch.setattr(fast_display_server.tmux_health, "consume_clean_exit", consume)

    assert (
        fast_display_server._classify_observed_exit("$4", target)
        is fast_display_server.RemoteExit.SOFT_QUIT
    )

    intended.assert_called_once_with(server_pid=123, session_id="$4")
    classify.assert_not_called()
    consume.assert_not_called()


def test_observed_hard_quit_requires_matching_clean_exit(monkeypatch):
    target = fast_display_server.tmux_server.TmuxServerTarget("/tmp/tmux/railmux", 123)
    monkeypatch.setattr(
        fast_display_server.tmux_health, "soft_exit_intended", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        fast_display_server,
        "_classify_remote_exit",
        lambda _session: fast_display_server.RemoteExit.HARD_QUIT,
    )
    consume = MagicMock(return_value=True)
    record = MagicMock(return_value=True)
    monkeypatch.setattr(fast_display_server.tmux_health, "consume_clean_exit", consume)
    monkeypatch.setattr(fast_display_server.tmux_health, "record_incident", record)

    assert (
        fast_display_server._classify_observed_exit("$4", target)
        is fast_display_server.RemoteExit.HARD_QUIT
    )
    consume.assert_called_once_with(server_pid=123, session_id="$4")
    record.assert_not_called()


def test_observed_unexpected_tmux_loss_records_incident(monkeypatch):
    target = fast_display_server.tmux_server.TmuxServerTarget("/tmp/tmux/railmux", 123)
    monkeypatch.setattr(
        fast_display_server.tmux_health, "soft_exit_intended", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        fast_display_server,
        "_classify_remote_exit",
        lambda _session: fast_display_server.RemoteExit.HARD_QUIT,
    )
    monkeypatch.setattr(
        fast_display_server.tmux_health,
        "consume_clean_exit",
        lambda **_kwargs: False,
    )
    record = MagicMock(return_value=True)
    monkeypatch.setattr(fast_display_server.tmux_health, "record_incident", record)

    with pytest.raises(
        fast_display_server.DisplayServerError,
        match="disappeared unexpectedly",
    ):
        fast_display_server._classify_observed_exit("$4", target)

    record.assert_called_once_with(
        component="remote-display",
        reason="remote-display-server-exit",
        consecutive_failures=1,
    )


@pytest.mark.parametrize(
    "exit_kind",
    [fast_display_server.RemoteExit.SOFT_QUIT, fast_display_server.RemoteExit.DETACHED],
)
def test_observed_surviving_session_does_not_consume_clean_exit(
    monkeypatch,
    exit_kind,
):
    target = fast_display_server.tmux_server.TmuxServerTarget("/tmp/tmux/railmux", 123)
    monkeypatch.setattr(
        fast_display_server.tmux_health, "soft_exit_intended", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        fast_display_server,
        "_classify_remote_exit",
        lambda _session: exit_kind,
    )
    consume = MagicMock()
    monkeypatch.setattr(fast_display_server.tmux_health, "consume_clean_exit", consume)

    assert fast_display_server._classify_observed_exit("$4", target) is exit_kind
    consume.assert_not_called()


def test_remote_watchdog_records_only_after_consecutive_failures(monkeypatch):
    watchdog = fast_display_server.tmux_health.FailureWatchdog.starting(
        0.0, interval=5.0, failure_limit=3
    )
    monkeypatch.setattr(fast_display_server, "_tmux_output", lambda *_args: "")
    record = MagicMock(return_value=True)
    monkeypatch.setattr(fast_display_server.tmux_health, "record_incident", record)

    assert not fast_display_server._remote_watchdog_tripped(watchdog, "$4", 123, 5.0)
    assert not fast_display_server._remote_watchdog_tripped(watchdog, "$4", 123, 10.0)
    assert fast_display_server._remote_watchdog_tripped(watchdog, "$4", 123, 15.0)
    record.assert_called_once_with(
        component="remote-display",
        reason="remote-display-watchdog-timeout",
        consecutive_failures=3,
    )


def test_server_resolves_only_noncontroller_pane_under_pointer(monkeypatch):
    monkeypatch.setattr(fast_display_server, "_live_controller", lambda _session: "%1")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0    \n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 1    \n"
        ),
    )

    assert (
        fast_display_server._pane_at_pointer("$4", 5, 5, claude_history_policy="ask")
        is None
    )
    pane = fast_display_server._pane_at_pointer(
        "$4", 40, 5, claude_history_policy="ask"
    )
    assert pane == fast_display_server._PaneGeometry(
        "%8", 31, 0, 49, 20, mouse_forwardable=True
    )


def test_server_projects_bounded_codex_history_generation(monkeypatch):
    marker = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    monkeypatch.setattr(
        fast_display_server, "_live_controller", lambda _session: "%1")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0    \n"
            f"$4 @1 0 0 %8 108 31 0 49 20 0 0 1   {marker} \n"
        ),
    )

    panes = fast_display_server._list_agent_panes(
        "$4", claude_history_policy="ask")

    assert len(panes) == 1
    assert panes[0].history_generation == fast_display_server._history_generation(
        marker)
    assert panes[0].history_generation != 0
    assert not panes[0].canonical_history


def test_server_accepts_canonical_history_only_for_matching_transcript(
    monkeypatch, tmp_path,
):
    session_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    path = tmp_path / f"rollout-2026-08-04T02-49-33-{session_id}.jsonl"
    path.write_text('{"type":"response_item"}\n')
    transcript = fast_display_server.tmux_server.encode_transcript_source(
        "codex", session_id, path)
    assert transcript is not None
    generation = (
        f"{tmux_ctl.RAILMUX_CANONICAL_HISTORY_PREFIX}{session_id}"
    )
    monkeypatch.setattr(
        fast_display_server, "_live_controller", lambda _session: "%1")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0    \n"
            f"$4 @1 0 0 %8 108 31 0 49 20 10 0 1  "
            f"{transcript} {generation} \n"
        ),
    )

    panes = fast_display_server._list_agent_panes(
        "$4", claude_history_policy="ask")

    assert len(panes) == 1
    assert panes[0].canonical_history
    assert panes[0].history_generation != fast_display_server._history_generation(
        session_id)

    generation = (
        f"{tmux_ctl.RAILMUX_CANONICAL_HISTORY_PREFIX}"
        "019fc605-5188-7212-bc48-ea023fe8b73c"
    )
    mismatched = fast_display_server._list_agent_panes(
        "$4", claude_history_policy="ask")

    assert len(mismatched) == 1
    assert not mismatched[0].canonical_history


@pytest.mark.parametrize(
    "legacy_prefix", tmux_ctl.RAILMUX_LEGACY_CANONICAL_HISTORY_PREFIXES
)
def test_server_released_canonical_marker_fails_back_to_raw(
    monkeypatch, tmp_path, legacy_prefix,
):
    session_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    path = tmp_path / f"rollout-2026-08-04T02-49-33-{session_id}.jsonl"
    path.write_text('{"type":"response_item"}\n')
    transcript = fast_display_server.tmux_server.encode_transcript_source(
        "codex", session_id, path)
    assert transcript is not None
    generation = f"{legacy_prefix}{session_id}"
    monkeypatch.setattr(
        fast_display_server, "_live_controller", lambda _session: "%1")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0    \n"
            f"$4 @1 0 0 %8 108 31 0 49 20 10 0 1  "
            f"{transcript} {generation} \n"
        ),
    )

    panes = fast_display_server._list_agent_panes(
        "$4", claude_history_policy="ask")

    assert len(panes) == 1
    assert not panes[0].canonical_history
    assert panes[0].history_generation == 0


def test_server_excludes_managed_shell_and_viewer_panes(monkeypatch):
    marker = fast_display_server.json.dumps(
        {
            "version": 1,
            "outer_session_id": "$4",
            "slot": "primary",
            "kind": "shell",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    monkeypatch.setattr(fast_display_server, "_live_controller", lambda _session: "%1")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0    \n"
            f"$4 @1 0 0 %8 108 31 0 49 20 0 0 1    {marker}\n"
        ),
    )

    assert fast_display_server._list_agent_panes(
        "$4",
        claude_history_policy="ask",
    ) == ()


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (
            "$4 @1 1 1 %1 101 0 0 80 24 0 0 0    \n"
            "$4 @1 1 0 %8 108 31 0 49 20 0 0 0    \n",
            (),
        ),
        (
            "$4 @1 1 0 %1 101 0 0 30 20 0 0 0    \n"
            "$4 @1 1 1 %8 108 0 0 80 24 0 0 0    \n",
            (fast_display_server._PaneGeometry("%8", 0, 0, 80, 24),),
        ),
        (
            "$4 @1 1 1 %1 101 0 0 80 24 0 0 0    \n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 0    \n",
            (),
        ),
    ],
)
def test_server_exposes_only_coherent_visible_panes_when_zoomed(
    monkeypatch, rows, expected
):
    monkeypatch.setattr(fast_display_server, "_live_controller", lambda _session: "%1")
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: rows)

    assert (
        fast_display_server._list_agent_panes("$4", claude_history_policy="ask")
        == expected
    )


def test_server_maps_nested_history_to_exact_real_pane(monkeypatch):
    target = fast_display_server.tmux_server.TmuxServerTarget("/tmp/default", 44)
    monkeypatch.setattr(fast_display_server, "_live_controller", lambda _session: "%1")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0    \n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 1 "
            '{"source":1}   \n'
        ),
    )
    monkeypatch.setattr(
        fast_display_server.tmux_server,
        "resolve_history_source",
        lambda marker, **_kwargs: (target, "$7") if marker else None,
    )
    monkeypatch.setattr(
        fast_display_server.tmux_server,
        "target_single_pane_id",
        lambda candidate, session, **_kwargs: (
            "%2" if (candidate, session) == (target, "$7") else None
        ),
    )

    assert fast_display_server._list_agent_panes("$4", claude_history_policy="ask") == (
        fast_display_server._PaneGeometry("%8", 31, 0, 49, 20, target, "%2", True),
    )


def test_server_recovers_exact_pre_v10_claude_transcript_from_binding(
    monkeypatch,
    tmp_path,
):
    session_id = "47fca075-9cb8-44fb-a314-d57ef2256ad9"
    transcript = tmp_path / "projects" / "-workspace" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    binding = fast_display_server.json.dumps(
        {
            "session_type": "claude",
            "key": session_id,
            "tmux_name": "cc-project-123",
            "cwd": "/workspace",
        }
    )

    backed, marker = fast_display_server._binding_transcript_source(binding)

    assert backed and marker is not None
    assert str(transcript) in marker


def test_server_rejects_ambiguous_pre_v10_claude_transcript(
    monkeypatch,
    tmp_path,
):
    session_id = "47fca075-9cb8-44fb-a314-d57ef2256ad9"
    for project in ("one", "two"):
        transcript = tmp_path / "projects" / project / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    binding = fast_display_server.json.dumps(
        {
            "session_type": "claude",
            "key": session_id,
            "tmux_name": "cc-project-123",
            "cwd": "/workspace",
        }
    )

    assert fast_display_server._binding_transcript_source(binding) == (
        False,
        None,
    )


def test_server_history_capture_preserves_sgr_but_filters_controls(monkeypatch):
    pane = fast_display_server._PaneGeometry("%8", 31, 0, 49, 2, mouse_forwardable=True)
    monkeypatch.setattr(fast_display_server, "_pane_at_pointer", lambda *args: pane)
    calls = []

    def fake_check_output(argv, **kwargs):
        calls.append((argv, kwargs))
        return (
            b"old\n"
            + b"\x1b[31;41mred"
            + b" " * 46
            + b"\x1b[0m\n"
            + b"\x1b]52;c;evil\x07visible\n"
        )

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    snapshot = fast_display_server.capture_history_snapshot("$4", 7, 40, 5, 2000)

    assert snapshot.pane_id == "%8"
    assert snapshot.mouse_forwardable is True
    assert b"old" in snapshot.lines[0]
    assert b"red" in snapshot.lines[1]
    assert b";31;" in snapshot.lines[1]
    pyte = __import__("pyte")
    styled = pyte.Screen(49, 1)
    pyte.ByteStream(styled).feed(snapshot.lines[1])
    assert styled.buffer[0][48].bg == "red"
    assert b"]52" not in snapshot.lines[2]
    assert b"visible" in snapshot.lines[2]
    assert calls[0][0] == [
        "tmux",
        "-L",
        "railmux",
        "capture-pane",
        "-p",
        "-e",
        "-N",
        "-t",
        "%8",
        "-S",
        "-2000",
    ]
    assert not any(
        destructive in calls[0][0]
        for destructive in ("kill-pane", "kill-session", "resize-pane", "send-keys")
    )


def test_server_history_capture_honours_limits_above_the_old_4096_cap(
    monkeypatch,
):
    pane = fast_display_server._PaneGeometry("%8", 31, 0, 49, 2)
    lines = tuple(f"line-{index}".encode() for index in range(5001))
    calls = []
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **_kwargs: calls.append(argv) or b"\n".join(lines),
    )
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    snapshot = fast_display_server._capture_pane_history(object(), pane, 7, 5000)

    assert snapshot is not None
    assert len(snapshot.lines) == 5000
    assert snapshot.lines[0] == b"line-1"
    assert snapshot.lines[-1] == b"line-5000"
    assert calls[0][-2:] == ["-S", "-5000"]


def test_server_raw_styled_hot_and_deep_history_keep_codex_foreground(
    monkeypatch,
):
    pyte = pytest.importorskip("pyte")
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        40,
        2,
        history_size=398,
        transcript_source="codex-marker",
        transcript_backed=True,
        transcript_provider="codex",
        history_generation=17,
    )
    wrap_head = (
        b"\033[48;5;22m 1 \033[0m\033[38;5;2m\033[48;5;22m"
        b"+export VERY_LONG_ADDITION="
    )
    wrap_continuation = (
        b"\033[39m   \033[0m\033[38;5;2m\033[48;5;22m"
        b"continued-value\033[39m"
    )
    highlighted = (
        b"\033[48;5;22m 2 \033[0m\033[38;5;2m\033[48;5;22m+"
        b"\033[38;2;205;214;244mvalue = 1\033[39m"
    )
    monochrome = (
        # tmux omits the leading background because this physical row inherits
        # it from ``highlighted``. History parsing must keep that stream state
        # while still returning an independently paintable row.
        b" 3 \033[0m\033[38;5;2m\033[48;5;22m"
        b"+#!/bin/bash\033[39m"
    )
    removed = (
        b"\033[48;5;52m 4 \033[0m\033[38;5;1m\033[48;5;52m"
        b"-removed = True\033[39m"
    )
    removed_inherited = (
        b" 5 \033[0m\033[38;5;1m\033[48;5;52m"
        b"-removed = False\033[39m"
    )
    ordinary = b"\033[0m ordinary output"
    raw = b"\n".join((
        *(f"old-{index}".encode() for index in range(99)),
        wrap_head,
        wrap_continuation,
        *(f"line-{index}".encode() for index in range(294)),
        highlighted,
        monochrome,
        removed,
        removed_inherited,
        ordinary,
    )) + b"\n"
    monkeypatch.setattr(
        subprocess, "check_output", lambda *_args, **_kwargs: raw)
    transcript_rows = MagicMock()
    monkeypatch.setattr(
        fast_display_server, "_transcript_rows", transcript_rows)

    terminal = fast_display_server._extended_pyte(pyte)
    hot = fast_display_server._capture_pane_history(
        terminal, pane, 1, 300)
    deep = fast_display_server._capture_pane_history(
        terminal, pane, 2, 400)

    assert hot is not None and deep is not None
    assert not hot.transcript_backed and not deep.transcript_backed
    assert hot.transcript_available and deep.transcript_available
    assert hot.lines == deep.lines[-300:]
    assert b"continued-value" in hot.lines[0]
    transcript_rows.assert_not_called()

    def styled_row(row):
        screen = terminal.Screen(40, 1)
        terminal.ByteStream(screen).feed(row)
        return screen

    highlighted_screen = styled_row(deep.lines[-5])
    monochrome_screen = styled_row(deep.lines[-4])
    removed_screen = styled_row(deep.lines[-3])
    removed_inherited_screen = styled_row(deep.lines[-2])
    ordinary_screen = styled_row(deep.lines[-1])
    highlighted_start = highlighted_screen.display[0].index("value")
    monochrome_start = monochrome_screen.display[0].index("#!/bin/bash")
    assert highlighted_screen.buffer[0][highlighted_start].fg == "cdd6f4"
    assert monochrome_screen.buffer[0][monochrome_start].fg == "00cd00"
    assert highlighted_screen.buffer[0][0].bg == "005f00"
    assert monochrome_screen.buffer[0][0].bg == "005f00"
    assert removed_screen.buffer[0][0].bg == "5f0000"
    assert removed_inherited_screen.buffer[0][0].bg == "5f0000"
    assert ordinary_screen.buffer[0][0].bg == "default"
    coloured_backgrounds = {
        char.bg
        for screen in (
            highlighted_screen,
            monochrome_screen,
            removed_screen,
            removed_inherited_screen,
        )
        for char in screen.buffer[0].values()
        if char.bg != "default"
    }
    assert coloured_backgrounds == {"005f00", "5f0000"}


def test_server_claude_history_uses_stable_transcript_suffix(monkeypatch):
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        20,
        2,
        mouse_forwardable=True,
        alternate_on=True,
        transcript_source="exact-marker",
        transcript_backed=True,
        claude_history_policy="local",
    )
    transcript_rows = tuple(f"transcript-{index}".encode() for index in range(500))
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"live-a\nlive-b\n",
    )
    monkeypatch.setattr(
        fast_display_server,
        "_transcript_rows",
        lambda *_args, **_kwargs: fast_display_server._TranscriptCacheEntry(
            (1, 2, 3, 4), transcript_rows, False
        ),
    )
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    hot = fast_display_server._capture_pane_history(object(), pane, 1, 300)
    deep = fast_display_server._capture_pane_history(object(), pane, 2, 400)

    assert hot is not None and deep is not None
    assert hot.transcript_backed and hot.more_available
    assert hot.lines == deep.lines[-300:]
    assert hot.lines[-2:] == (b"live-a", b"live-b")


@pytest.mark.parametrize(
    ("policy", "choice_required"),
    [("ask", True), ("native", False)],
)
def test_server_waits_for_local_choice_before_rendering_claude_transcript(
    monkeypatch,
    policy,
    choice_required,
):
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        20,
        2,
        mouse_forwardable=True,
        alternate_on=True,
        transcript_source="exact-marker",
        transcript_backed=True,
        claude_history_policy=policy,
    )
    transcript_rows = MagicMock()
    monkeypatch.setattr(fast_display_server, "_transcript_rows", transcript_rows)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"live-a\nlive-b\n",
    )
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    snapshot = fast_display_server._capture_pane_history(object(), pane, 1, 300)

    assert snapshot is not None
    assert snapshot.transcript_available
    assert snapshot.history_choice_required is choice_required
    assert not snapshot.transcript_backed
    transcript_rows.assert_not_called()


def test_server_codex_history_uses_canonical_transcript_after_rewind(
    monkeypatch,
):
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        40,
        2,
        mouse_forwardable=True,
        history_size=500,
        alternate_on=False,
        transcript_source="codex-marker",
        transcript_backed=True,
        transcript_provider="codex",
        history_generation=19,
        canonical_history=True,
    )
    canonical = (b"retained prompt", b"replacement answer")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: (
            b"abandoned red interruption\nlive-a\nlive-b\n"
        ),
    )
    monkeypatch.setattr(
        fast_display_server,
        "_transcript_rows",
        lambda *_args, **_kwargs: fast_display_server._TranscriptCacheEntry(
            (1, 2, 3, 4), canonical, False
        ),
    )
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    snapshot = fast_display_server._capture_pane_history(
        object(), pane, 7, 100)

    assert snapshot is not None
    assert snapshot.transcript_backed
    assert not snapshot.history_choice_required
    assert snapshot.generation == 19
    assert b"retained prompt" in snapshot.lines
    assert snapshot.lines[-2:] == (b"live-a", b"live-b")
    assert all(b"abandoned red interruption" not in line
               for line in snapshot.lines)


def test_server_unreadable_claude_transcript_preserves_native_wheel_fallback(
    monkeypatch,
):
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        20,
        2,
        mouse_forwardable=True,
        alternate_on=True,
        transcript_source="unreadable-marker",
        transcript_backed=True,
        claude_history_policy="local",
    )
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"live-a\nlive-b\n",
    )
    monkeypatch.setattr(
        fast_display_server, "_transcript_rows", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    snapshot = fast_display_server._capture_pane_history(object(), pane, 1, 300)

    assert snapshot is not None
    assert snapshot.mouse_forwardable
    assert not snapshot.transcript_backed


def test_transcript_wrapper_preserves_combined_sgr_after_line_wrap():
    pyte = fast_display_server._extended_pyte(__import__("pyte"))

    rows, dropped = fast_display_server._wrap_transcript_rows(pyte, "\033[0;31mabcd", 2)

    assert not dropped
    assert len(rows) == 2
    assert b"\033[31m" in rows[1]


def test_transcript_cache_evicts_least_recent_file_width(monkeypatch, tmp_path):
    pyte = fast_display_server._extended_pyte(__import__("pyte"))
    monkeypatch.setattr(fast_display_server, "_TRANSCRIPT_CACHE_LIMIT", 2)
    fast_display_server._TRANSCRIPT_CACHE.clear()
    keys = []
    try:
        for suffix in ("1", "2", "3"):
            session_id = f"47fca075-9cb8-44fb-a314-d57ef2256ad{suffix}"
            path = tmp_path / f"{session_id}.jsonl"
            path.write_text(
                '{"type":"user","message":{"role":"user","content":"hello"}}\n'
            )
            marker = fast_display_server.tmux_server.encode_transcript_source(
                "claude", session_id, path
            )
            assert marker is not None
            assert (
                fast_display_server._transcript_rows(
                    pyte, marker, 40, allow_stale=False
                )
                is not None
            )
            keys.append((str(path), 40))

        assert tuple(fast_display_server._TRANSCRIPT_CACHE) == tuple(keys[-2:])
    finally:
        fast_display_server._TRANSCRIPT_CACHE.clear()


def test_transcript_rows_render_codex_locator_with_codex_formatter(tmp_path):
    pyte = fast_display_server._extended_pyte(__import__("pyte"))
    session_id = "019fcaad-27a1-70c0-8029-8a9c7803fa6b"
    path = tmp_path / f"rollout-2026-08-04T02-49-33-{session_id}.jsonl"
    path.write_text(
        '{"type":"response_item","payload":{"type":"message",'
        '"role":"user","content":[{"type":"input_text",'
        '"text":"retained question"}]}}\n'
    )
    marker = fast_display_server.tmux_server.encode_transcript_source(
        "codex", session_id, path)
    assert marker is not None
    fast_display_server._TRANSCRIPT_CACHE.clear()
    try:
        entry = fast_display_server._transcript_rows(
            pyte, marker, 60, allow_stale=False)
        assert entry is not None
        assert any(b"retained question" in row for row in entry.rows)
    finally:
        fast_display_server._TRANSCRIPT_CACHE.clear()


def test_server_history_capture_truncates_to_newest_styled_byte_budget(
    monkeypatch,
):
    pane = fast_display_server._PaneGeometry("%8", 31, 0, 49, 2)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"oldest\nmiddle\nnewest\n",
    )
    line_bytes = fast_display_server._HISTORY_SNAPSHOT_RAW_BUDGET // 3 + 100
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(
            line + b"x" * line_bytes for line in lines
        ),
    )

    snapshot = fast_display_server._capture_pane_history(object(), pane, 7, 20000)

    assert snapshot is not None
    assert len(snapshot.lines) == 2
    assert snapshot.lines[0].startswith(b"middle")
    assert snapshot.lines[1].startswith(b"newest")
    assert ServerMessageDecoder().feed(encode_history_snapshot(snapshot)) == [snapshot]


def test_server_captures_nested_history_from_real_pane_without_resizing(
    monkeypatch,
):
    target = fast_display_server.tmux_server.TmuxServerTarget("/tmp/default", 44)
    pane = fast_display_server._PaneGeometry("%8", 31, 0, 49, 2, target, "%2")
    monkeypatch.setattr(fast_display_server, "_pane_at_pointer", lambda *_args: pane)
    monkeypatch.setattr(
        fast_display_server.tmux_server,
        "target_is_live",
        lambda candidate, **_kwargs: candidate == target,
    )
    calls = []
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **_kwargs: calls.append(argv) or b"old\nnew\n",
    )

    snapshot = fast_display_server.capture_history_snapshot("$4", 7, 40, 5, 300)

    assert snapshot.pane_id == "%8"
    assert calls == [
        [
            "tmux",
            "-S",
            "/tmp/default",
            "capture-pane",
            "-p",
            "-e",
            "-N",
            "-t",
            "%2",
            "-S",
            "-300",
        ]
    ]
    assert not any(
        item in calls[0]
        for item in ("resize-pane", "swap-pane", "send-keys", "kill-pane")
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--protocol", str(PROTOCOL_VERSION), "--width", "39", "--height", "24"],
        ["--protocol", str(PROTOCOL_VERSION), "--width", "80", "--height", "11"],
        [
            "--protocol",
            str(PROTOCOL_VERSION),
            "--width",
            "80",
            "--height",
            "24",
            "--fps",
            "61",
        ],
    ],
)
def test_server_rejects_unbounded_geometry_and_frame_rates(argv):
    with pytest.raises(SystemExit):
        parse_server_args(argv)


@dataclass(frozen=True)
class _Char:
    data: str = " "
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False


class _FakeScreen:
    lines = 1
    columns = 4
    buffer = {
        0: {
            0: _Char("A", fg="red", bold=True),
            1: _Char("你"),
            2: _Char(""),
            3: _Char("\x1b\x9b"),
        }
    }
    mode = {2004 << 5, 1004 << 5}


def test_server_renderer_preserves_wide_cells_and_filters_terminal_controls():
    rows = render_rows(_FakeScreen())

    assert len(rows) == 1
    rendered = rows[0]
    assert b"A" in rendered
    assert "你".encode() in rendered
    assert rendered.count("你".encode()) == 1
    assert b"\x1b\x1b" not in rendered
    assert "\x9b".encode() not in rendered
    assert "�".encode() in rendered
    assert rendered.endswith(b"\033[0m")


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (b"\033[2S", ["11111", "44444", "     ", "     ", "55555"]),
        (b"\033[2T", ["11111", "     ", "     ", "22222", "55555"]),
    ],
)
def test_server_terminal_model_applies_parameterized_scroll_inside_margins(
    operation,
    expected,
):
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    screen = terminal.Screen(5, 5)
    stream = terminal.ByteStream(screen)
    for row, value in enumerate(b"12345", 1):
        stream.feed(f"\033[{row};1H".encode() + bytes((value,)) * 5)

    # Restrict scrolling to rows 2-4 and keep the cursor outside that region.
    # SU/SD operate on DECSTBM regardless of cursor position and must not move
    # the cursor; pyte 0.8.2 silently ignored both sequences.
    stream.feed(b"\033[2;4r\033[5;3H")
    screen.dirty.clear()
    stream.feed(operation)

    assert screen.display == expected
    assert (screen.cursor.x, screen.cursor.y) == (2, 4)
    assert screen.dirty == {1, 2, 3}


def test_server_terminal_model_repeats_character_with_current_style():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    with pytest.warns(DeprecationWarning):
        screen = terminal.DiffScreen(8, 1)
    stream = terminal.ByteStream(screen)

    stream.feed(b"\033[31m#\033[4b")

    assert screen.display == ["#####   "]
    assert [screen.buffer[0][column].fg for column in range(5)] == ["red"] * 5


def test_server_terminal_model_ignores_private_device_status_queries():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    screen = terminal.Screen(8, 2)

    terminal.ByteStream(screen).feed(b"before\033[?6nafter")

    assert "".join(screen.display).replace(" ", "").startswith("beforeafter")


def test_server_history_renderer_uses_extended_terminal_sequences():
    pyte = pytest.importorskip("pyte")
    rendered = fast_display_server._render_history_line(
        fast_display_server._extended_pyte(pyte),
        b"\033[31m#\033[4b\033[0m",
        8,
    )

    assert rendered.count(b"#") == 5
    assert b";31;" in rendered


def test_server_projects_only_allowlisted_private_terminal_modes():
    assert terminal_modes_for_screen(_FakeScreen()) == (
        TerminalMode.BRACKETED_PASTE | TerminalMode.FOCUS_EVENTS
    )

    class OtherModes:
        mode = {1000 << 5, 1006 << 5, 9999 << 5}

    assert terminal_modes_for_screen(OtherModes()) is TerminalMode.NONE
