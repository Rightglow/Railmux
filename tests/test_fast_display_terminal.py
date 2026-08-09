from __future__ import annotations

import io
import os
import signal
import struct
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from railmux.fast_display_protocol import (
    DISPLAY_MAGIC,
    HistoryBatch,
    HistorySnapshot,
    InputFrameDecoder,
    InputKind,
    ScreenUpdate,
    ScreenUpdateDecoder as ClientScreenUpdateDecoder,
    TerminalMode,
    UpdateKind,
    decode_history_prefetch,
    encode_heartbeat,
    encode_update,
)
from railmux import fast_display_client, fast_display_server
from railmux.fast_display_client import (
    AppliedScreen,
    LOCAL_ESCAPE,
    ScreenModel,
    TerminalSurface,
    UpdateKind as ClientUpdateKind,
    encode_input as encode_client_input,
    encode_keyframe_request as encode_client_keyframe_request,
    encode_resize as encode_client_resize,
    focus_in_frame_for_screen,
    parse_args as parse_client_args,
    split_local_escape,
    termux_prompt_touch_action,
)
from railmux.fast_display_history import (
    HistoryAction,
    LocalHistoryView,
)
from railmux.fast_display_input import (
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

    assert stream.getvalue() == (b"\033[?1002l\033[?1006l\033[?1002h\033[?1006h")
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
    assert (
        focus_in_frame_for_screen(replace(screen, terminal_modes=TerminalMode.NONE))
        is None
    )
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
        ClientScreenUpdateDecoder().feed(encode_update(_keyframe(width=12, height=3)))[
            0
        ],
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
        ClientScreenUpdateDecoder().feed(encode_update(_keyframe(width=20, height=4)))[
            0
        ],
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
        ClientScreenUpdateDecoder().feed(encode_update(_keyframe(width=12, height=3)))[
            0
        ],
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
    screen = replace(screen, cursor_y=2, changed_rows=(2, 11, 14), clear=False)
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
