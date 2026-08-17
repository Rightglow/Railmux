from __future__ import annotations

import io
import base64
import threading
import time
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from railmux.fast_display_protocol import (
    ClipboardCopy,
    HistoryBatch,
    HistorySnapshot,
    InputFrameDecoder,
    InputKind,
    PathKind,
    PathOpenResult,
    PathResult,
    ServerMessageDecoder,
    TerminalMode,
    decode_history_prefetch,
    encode_history_batch,
    encode_history_prefetch,
    encode_history_snapshot,
    encode_clipboard_copy,
)
from railmux import fast_display_server
from railmux.fast_display_client import (
    AppliedScreen,
    TerminalSurface,
)
from railmux.fast_display_history import (
    LocalHistoryView,
)
from railmux.fast_display_input import (
    ClickTarget,
    LocalTextSelection,
    SelectionAction,
    SelectionSource,
    SgrMouseEvent,
)


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
        message="Opened file inside Railmux",
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
    monkeypatch.setattr(
        fast_display_server.shutil, "which", lambda _name: "/usr/bin/vim"
    )

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
        "Opened file inside Railmux",
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


def test_path_action_worker_keeps_caller_responsive_and_serializes(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def apply(_session, request_id, *_request):
        started.set()
        assert release.wait(1.0)
        return PathOpenResult(request_id, True, "success", "opened")

    monkeypatch.setattr(fast_display_server, "apply_path_open_request", apply)
    worker = fast_display_server.PathActionWorker()
    try:
        assert worker.submit("$4", 7, "%8", "/work", "internal", False, None, None)
        assert started.wait(0.5)
        assert worker.busy
        assert worker.drain() == ()
        assert not worker.submit(
            "$4", 8, "%8", "/other", "internal", False, None, None
        )

        release.set()
        deadline = time.monotonic() + 1.0
        results = ()
        while not results and time.monotonic() < deadline:
            results = worker.drain()
            time.sleep(0.01)
        assert results == (PathOpenResult(7, True, "success", "opened"),)
    finally:
        release.set()
        worker.close()


def test_path_action_worker_keeps_read_only_resolution_off_caller(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def resolve(_session, request_id, _pane, raw_path):
        started.set()
        assert release.wait(1.0)
        return PathResult(request_id, PathKind.DIRECTORY, raw_path, "ask")

    monkeypatch.setattr(fast_display_server, "resolve_path_result", resolve)
    worker = fast_display_server.PathActionWorker()
    try:
        assert worker.submit_resolve("$4", 9, "%8", "/c/work")
        assert started.wait(0.5)
        assert worker.drain() == ()
        assert not worker.submit_resolve("$4", 10, "%8", "/c/other")
        release.set()
        deadline = time.monotonic() + 1.0
        results = ()
        while not results and time.monotonic() < deadline:
            results = worker.drain()
            time.sleep(0.01)
        assert results == (
            PathResult(9, PathKind.DIRECTORY, "/c/work", "ask"),
        )
    finally:
        release.set()
        worker.close()


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


def test_local_text_selection_keeps_double_question_url_as_one_url():
    route = HistorySnapshot(1, "%8", 0, 0, 80, 1)
    source = SelectionSource(
        route,
        (b"Open https://www.baidu.com/s??wd=railmux now",),
        0,
    )
    selection = LocalTextSelection()

    selection.pointer_event(SgrMouseEvent(b"down", 0, 10, 1, True), source)
    target = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 10, 1, False), source
    ).open_target

    assert target is not None
    assert target.kind == "url"
    assert target.value == "https://www.baidu.com/s??wd=railmux"


def test_local_text_selection_recognizes_windows_drive_and_unc_paths():
    route = HistorySnapshot(1, "%8", 0, 0, 120, 4)
    source = SelectionSource(
        route,
        (
            rb"changed C:\work\rail mux\ignored.py C:\work\railmux\app.py:12:3",
            rb"share \\server\team\project\README.md",
            b"C:\\Users\\user\\.railmux\\windows\\",
            b"/c/Users/user/.railmux/",
        ),
        0,
    )
    selection = LocalTextSelection()

    selection.pointer_event(SgrMouseEvent(b"down", 0, 42, 1, True), source)
    drive = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 42, 1, False), source
    ).open_target
    assert drive is not None
    assert drive.value == r"C:\work\railmux\app.py"
    assert (drive.line, drive.column) == (12, 3)

    selection.pointer_event(SgrMouseEvent(b"down", 0, 12, 2, True), source)
    unc = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 12, 2, False), source
    ).open_target
    assert unc is not None
    assert unc.value == r"\\server\team\project\README.md"

    selection.pointer_event(SgrMouseEvent(b"down", 0, 5, 3, True), source)
    drive_directory = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 5, 3, False), source
    ).open_target
    assert drive_directory is not None
    assert drive_directory.value == "C:\\Users\\user\\.railmux\\windows\\"

    selection.pointer_event(SgrMouseEvent(b"down", 0, 5, 4, True), source)
    msys_directory = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 5, 4, False), source
    ).open_target
    assert msys_directory is not None
    assert msys_directory.value == "/c/Users/user/.railmux/"


def test_local_text_selection_stops_url_before_chinese_prose():
    route = HistorySnapshot(1, "%8", 0, 0, 100, 1)
    source = SelectionSource(
        route,
        ("See https://github.com/NVIDIA/TensorRT-LLM/pull/17000)，并注明由".encode(),),
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
            b"sidebar".ljust(20) + b"Report: /home/user/project/",
            b"sidebar".ljust(20) + b"    results/index.html             ",
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
    assert action.open_target.value == ("/home/user/project/results/index.html")
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
    assert selection.segments() == ((0, 7, b"/home/user/first.txt"),)


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
    assert selection.segments() == ((0, 11, b"/home/user/project/"),)


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
            b"    https://github.com/NVIDIA/TensorRT-LLM/blob/746e43a80b418b2e521",
            b"38846b4789dd6e49f8466/tests/unittest/_torch/visual_gen/multi_gpu/te",
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


def test_local_text_selection_joins_url_across_codex_ran_decoration():
    first = "• Ran curl https://example.test/releases/rail"
    second = "  │ mux/0.4.1.dev1/index.html --head"
    width = max(len(first), len(second))
    route = HistorySnapshot(1, "%8", 3, 2, width, 2)
    source = SelectionSource(
        route,
        tuple((line + " " * (width - len(line))).encode() for line in (first, second)),
        0,
    )
    expected = "https://example.test/releases/railmux/0.4.1.dev1/index.html"
    expected_segments = (
        (2, 3 + first.index("https://"), b"https://example.test/releases/rail"),
        (3, 3 + second.index("mux/"), b"mux/0.4.1.dev1/index.html"),
    )

    for screen_row, text, token in (
        (3, first, "https://"),
        (4, second, "mux/"),
    ):
        selection = LocalTextSelection()
        screen_column = 3 + text.index(token) + 2
        assert selection.hover(
            SgrMouseEvent(b"hover", 35, screen_column, screen_row, True),
            source,
        )
        assert selection.segments() == expected_segments
        selection.pointer_event(
            SgrMouseEvent(b"down", 0, screen_column, screen_row, True),
            source,
        )
        action = selection.pointer_event(
            SgrMouseEvent(b"up", 0, screen_column, screen_row, False),
            source,
        )
        assert action.open_target is not None
        assert action.open_target.kind == "url"
        assert action.open_target.value == expected


def test_local_text_selection_joins_path_across_codex_ran_decoration():
    first = "• Ran /tmp/railmux-published-0.4.1.dev1-"
    second = "  │ 20260816/bin/pip --isolated"
    width = max(len(first), len(second))
    route = HistorySnapshot(1, "%8", 0, 0, width, 2)
    source = SelectionSource(
        route,
        tuple((line + " " * (width - len(line))).encode() for line in (first, second)),
        0,
    )
    selection = LocalTextSelection()
    column = second.index("20260816") + 2

    assert selection.hover(SgrMouseEvent(b"hover", 35, column, 2, True), source)
    assert selection.segments() == (
        (0, first.index("/tmp/"), b"/tmp/railmux-published-0.4.1.dev1-"),
        (1, second.index("20260816"), b"20260816/bin/pip"),
    )
    selection.pointer_event(SgrMouseEvent(b"down", 0, column, 2, True), source)
    action = selection.pointer_event(
        SgrMouseEvent(b"up", 0, column, 2, False),
        source,
    )
    assert action.open_target is not None
    assert action.open_target.kind == "path"
    assert action.open_target.value == (
        "/tmp/railmux-published-0.4.1.dev1-20260816/bin/pip"
    )


def test_local_text_selection_does_not_join_undecorated_box_drawing_rows():
    first = "Output /home/user/project/"
    second = "  │ sibling/file.py"
    width = max(len(first), len(second))
    route = HistorySnapshot(1, "%8", 0, 0, width, 2)
    source = SelectionSource(
        route,
        tuple((line + " " * (width - len(line))).encode() for line in (first, second)),
        0,
    )
    selection = LocalTextSelection()

    selection.pointer_event(SgrMouseEvent(b"down", 0, 12, 1, True), source)
    action = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 12, 1, False),
        source,
    )
    assert action.open_target is not None
    assert action.open_target.value == "/home/user/project/"


def test_local_text_selection_does_not_join_next_decorated_shell_option():
    first = "• Ran cat /home/user/project/"
    second = "  │ --verbose"
    width = len(first)
    route = HistorySnapshot(1, "%8", 0, 0, width, 2)
    source = SelectionSource(
        route,
        tuple((line + " " * (width - len(line))).encode() for line in (first, second)),
        0,
    )
    selection = LocalTextSelection()

    selection.pointer_event(SgrMouseEvent(b"down", 0, 12, 1, True), source)
    action = selection.pointer_event(
        SgrMouseEvent(b"up", 0, 12, 1, False),
        source,
    )
    assert action.open_target is not None
    assert action.open_target.value == "/home/user/project/"


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
    assert selection.segments() == ((3, 6, b"https://example.test/docs"),)
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
    assert selection.segments() == ((0, 0, b"https://example.test"),)
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
