from __future__ import annotations

import io

from railmux.terminal_status import (
    STYLE_ACCENT,
    TransientStatusLine,
    command_status,
    styled,
)


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_transient_status_replaces_and_clears_one_terminal_line():
    stream = _TTYBuffer()
    status = TransientStatusLine(stream)

    status.show("Connecting…")
    status.show("Checking…")
    status.clear()
    status.clear()

    assert stream.getvalue() == ("\r\033[2KConnecting…\r\033[2KChecking…\r\033[2K")


def test_transient_status_is_silent_for_redirected_output():
    stream = io.StringIO()
    status = TransientStatusLine(stream)

    status.show("Connecting…")
    status.clear()

    assert stream.getvalue() == ""


def test_terminal_style_honors_tty_and_no_color(monkeypatch):
    stream = _TTYBuffer()
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert styled("Railmux", STYLE_ACCENT, stream=stream).startswith("\033[")
    assert "\033[" in command_status("railmux doctor", "Checking…", stream=stream)

    monkeypatch.setenv("NO_COLOR", "1")
    assert styled("Railmux", STYLE_ACCENT, stream=stream) == "Railmux"
    assert command_status(
        "railmux doctor", "Checking…", stream=stream
    ) == "railmux doctor: Checking…"
