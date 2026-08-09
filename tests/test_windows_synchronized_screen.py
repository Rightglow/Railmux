from __future__ import annotations

import io

import pytest
import urwid

from railmux.ui import app as app_module


def _screen(monkeypatch, events: list[str], *, failure: Exception | None = None):
    screen = object.__new__(app_module._SynchronizedOutputScreen)
    # RawScreen closes these in __del__; the focused unit fixture deliberately
    # skips its terminal-owning constructor.
    screen._resize_pipe_rd = io.StringIO()
    screen._resize_pipe_wr = io.StringIO()
    screen.screen_buf = []
    screen._screen_buf_canvas = None
    screen.write = lambda data: events.append(data)
    screen.flush = lambda: events.append("flush")

    def base_draw(_self, _size, _canvas):
        events.append("draw")
        if failure is not None:
            raise failure

    monkeypatch.setattr(urwid.raw_display.Screen, "draw_screen", base_draw)
    return screen


def test_windows_screen_commits_one_complete_urwid_frame(monkeypatch):
    events: list[str] = []
    screen = _screen(monkeypatch, events)

    screen.draw_screen((80, 24), object())

    assert events == [
        app_module._SYNC_OUTPUT_BEGIN,
        "draw",
        app_module._SYNC_OUTPUT_END,
        "flush",
    ]


def test_windows_screen_does_not_emit_empty_sync_frames(monkeypatch):
    events: list[str] = []
    screen = _screen(monkeypatch, events)
    canvas = object()
    screen.screen_buf = [[("body", None, b"same")]]
    screen._screen_buf_canvas = canvas

    screen.draw_screen((80, 24), canvas)

    assert events == []


def test_windows_screen_releases_sync_mode_after_draw_failure(monkeypatch):
    events: list[str] = []
    screen = _screen(
        monkeypatch, events, failure=RuntimeError("render failed"))

    with pytest.raises(RuntimeError, match="render failed"):
        screen.draw_screen((80, 24), object())

    assert events[-2:] == [app_module._SYNC_OUTPUT_END, "flush"]


def test_managed_windows_selects_synchronized_screen(monkeypatch):
    monkeypatch.setattr(app_module, "running_in_windows_wrapper", lambda: True)

    assert app_module._screen_class_for_platform() is app_module._SynchronizedOutputScreen


def test_posix_keeps_stock_urwid_screen(monkeypatch):
    monkeypatch.setattr(app_module, "running_in_windows_wrapper", lambda: False)

    assert app_module._screen_class_for_platform() is urwid.raw_display.Screen


def test_legacy_managed_windows_tmux_reduces_codex_motion(monkeypatch):
    monkeypatch.setattr(
        app_module, "running_in_managed_windows_wrapper", lambda: True)
    monkeypatch.setattr(app_module.tmux_ctl, "tmux_version", lambda: (3, 6))

    assert app_module._reduce_codex_motion_for_terminal() is True


@pytest.mark.parametrize("version", [(3, 7), (3, 8)])
def test_current_managed_windows_tmux_keeps_codex_motion(monkeypatch, version):
    monkeypatch.setattr(
        app_module, "running_in_managed_windows_wrapper", lambda: True)
    monkeypatch.setattr(app_module.tmux_ctl, "tmux_version", lambda: version)

    assert app_module._reduce_codex_motion_for_terminal() is False


def test_posix_never_changes_codex_motion(monkeypatch):
    monkeypatch.setattr(
        app_module, "running_in_managed_windows_wrapper", lambda: False)
    monkeypatch.setattr(
        app_module.tmux_ctl,
        "tmux_version",
        lambda: pytest.fail("POSIX must not need a tmux compatibility probe"),
    )

    assert app_module._reduce_codex_motion_for_terminal() is False
