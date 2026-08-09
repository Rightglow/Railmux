from __future__ import annotations

import os
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from railmux.fast_display_protocol import (
    HistoryBatch,
    HistorySnapshot,
    InputFrameDecoder,
    ScreenUpdate,
    ScreenUpdateDecoder as ClientScreenUpdateDecoder,
    TerminalMode,
    UpdateKind,
    decode_history_prefetch,
    decode_history_request,
    encode_update,
)
from railmux import fast_display_client
from railmux.fast_display_client import (
    AppliedScreen,
    ScreenModel,
    TerminalSurface,
    screen_input_may_change_routes,
)
from railmux import fast_display_history
from railmux.fast_display_history import (
    HistoryAction,
    LocalHistoryView,
    PeriodicPrefetchGate,
    input_may_change_routes,
)
from railmux.fast_display_input import (
    SgrMouseEvent,
    TerminalInputDecoder,
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


def test_windows_history_wheel_distance_matches_native_three_row_step():
    view = LocalHistoryView(wheel_lines=3)
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    view.accept_prefetch(
        HistoryBatch(
            prefetch_id,
            (
                HistorySnapshot(
                    prefetch_id,
                    "%8",
                    30,
                    0,
                    30,
                    3,
                    tuple(f"line-{index}".encode() for index in range(40)),
                ),
            ),
        )
    )

    view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True), now=1.0)

    assert view.viewports["%8"].offset == 3


def test_posix_history_wheel_distance_remains_one_row():
    assert LocalHistoryView().wheel_lines == 1


@pytest.mark.parametrize(("windows", "expected"), ((False, 1), (True, 3)))
def test_client_selects_platform_history_wheel_distance(
    monkeypatch, windows, expected
):
    monkeypatch.setattr(
        fast_display_client, "running_in_windows_wrapper", lambda: windows
    )

    assert fast_display_client._local_history_wheel_lines() == expected


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

    assert view.page(b"\x1b[5~", 4, 2) == HistoryAction(forwarded_input=b"\x1b[5~")
    assert view.page(b"ordinary", 40, 2) == HistoryAction(forwarded_input=b"ordinary")


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
    late = replace(
        route,
        request_id=retry_id,
        lines=tuple(f"deep-{index}".encode() for index in range(2000)),
    )
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
    view.accept_prefetch(
        HistoryBatch(
            first_id,
            (HistorySnapshot(first_id, "%8", 30, 0, 30, 3, old, generation=11),),
        )
    )

    second = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second.data)
    current = (b"retained", b"replacement", b"live")
    view.accept_prefetch(
        HistoryBatch(
            second_id,
            (HistorySnapshot(second_id, "%8", 30, 0, 30, 3, current, generation=12),),
        )
    )

    assert view.content_cache["%8"].lines == current


def test_rewind_generation_change_closes_frozen_history_overlay():
    view = LocalHistoryView(history_limit=2000)
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    old = tuple(f"abandoned-{index}".encode() for index in range(100))
    view.accept_prefetch(
        HistoryBatch(
            first_id,
            (HistorySnapshot(first_id, "%8", 30, 0, 30, 3, old, generation=11),),
        )
    )
    assert view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True)).render_history

    second = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second.data)
    current = (b"retained", b"replacement", b"live")
    action = view.accept_prefetch(
        HistoryBatch(
            second_id,
            (HistorySnapshot(second_id, "%8", 30, 0, 30, 3, current, generation=12),),
        )
    )

    assert action.restore_live is True
    assert view.active is False
    assert view.content_cache["%8"].lines == current


def test_rewind_generation_change_rejects_stale_deep_response():
    view = LocalHistoryView(history_limit=2000)
    first = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first.data)
    old = tuple(f"abandoned-{index}".encode() for index in range(100))
    view.accept_prefetch(
        HistoryBatch(
            first_id,
            (HistorySnapshot(first_id, "%8", 30, 0, 30, 3, old, generation=11),),
        )
    )
    deep = view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    deep_id = decode_history_request(
        InputFrameDecoder().feed(deep.protocol_frame)[0].data
    )[0]

    # A newer prefetch observes the rewind before the old deep response arrives.
    second = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second.data)
    current = (b"retained", b"replacement", b"live")
    view.accept_prefetch(
        HistoryBatch(
            second_id,
            (HistorySnapshot(second_id, "%8", 30, 0, 30, 3, current, generation=12),),
        )
    )

    action = view.accept(
        HistorySnapshot(
            deep_id,
            "%8",
            30,
            0,
            30,
            3,
            (b"older", *old),
            generation=11,
        )
    )

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

    batch.flush(surface, screen, overlays, (), force=True)

    surface.paint_overlays.assert_called_once_with(screen, overlays, ())
    surface.paint.assert_not_called()


def test_local_history_wheel_batch_restores_live_immediately_at_bottom():
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

    history_paint = fast_display_client._DeferredHistoryPaint(interval=0.02)
    batch = fast_display_client._LocalInputBatch(history_paint)
    down = SgrMouseEvent(b"down", 65, 5, 2, True)
    restore = None
    for index in range(5):
        action, deferred_paint = batch.prepare(down, view.pointer_event(down))
        assert action is not None
        if action.restore_live:
            assert index == 2
            assert deferred_paint is None
            restore = action
            break
        assert deferred_paint is history_paint
        deferred_paint.defer(action, now=1.0 + index * 0.001)
    assert not view.active
    assert restore is not None
    assert restore.restore_live
    assert not history_paint.render_history

    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(encode_update(_keyframe(width=30, height=3)))[
            0
        ],
        os.terminal_size((30, 3)),
    )
    assert screen is not None
    surface = MagicMock(spec=TerminalSurface)
    batch.flush(surface, screen, view.overlays(), (), now=2.0)

    surface.paint.assert_not_called()
    surface.paint_overlays.assert_not_called()


def test_local_history_wheel_reads_share_one_bounded_paint_deadline():
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
        tuple(f"line-{index}".encode() for index in range(40)),
    )
    view.accept_prefetch(HistoryBatch(request_id, (route,)))
    history_paint = fast_display_client._DeferredHistoryPaint(interval=0.016)
    event = SgrMouseEvent(b"up", 64, 5, 2, True)
    surface = MagicMock(spec=TerminalSurface)
    initial_screen = MagicMock(spec=AppliedScreen)
    latest_screen = MagicMock(spec=AppliedScreen)

    for index in range(8):
        # Windows Terminal/RDP may deliver one SGR packet per stdin read.
        batch = fast_display_client._LocalInputBatch(history_paint)
        action, deferred_paint = batch.prepare(event, view.pointer_event(event))
        assert action is not None
        assert deferred_paint is history_paint
        deferred_paint.defer(action, now=1.0 + index * 0.001)
        assert not batch.flush(
            surface,
            initial_screen,
            view.overlays(),
            (),
            now=1.0 + index * 0.001,
        )

    assert view.viewports["%8"].offset == 8
    assert history_paint.next_timeout(now=1.008) == pytest.approx(0.008)
    assert history_paint.flush(
        surface,
        latest_screen,
        view.overlays(),
        (),
        now=1.016,
    )
    # A remote screen update may arrive while the gesture is being coalesced;
    # the deadline paint must compose onto the newest authoritative frame.
    surface.paint_overlays.assert_called_once_with(
        latest_screen,
        view.overlays(),
        (),
    )
    assert history_paint.deadline is None


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
