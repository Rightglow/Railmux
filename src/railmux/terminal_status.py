"""Small terminal-only status primitives shared by command-line workflows."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TextIO


STYLE_RESET = "\033[0m"
STYLE_ACCENT = "\033[1;32m"
STYLE_HEADING = "\033[1m"
STYLE_MUTED = "\033[2m"
STYLE_SUCCESS = "\033[32m"
STYLE_WARNING = "\033[33m"
STYLE_ERROR = "\033[31m"
STYLE_PROMPT = "\033[1;33m"


def stream_is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def terminal_color_enabled(
    stream: TextIO,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Respect terminal capability and the conventional NO_COLOR opt-out."""
    values = os.environ if environ is None else environ
    return (
        stream_is_tty(stream)
        and "NO_COLOR" not in values
        and values.get("TERM", "").lower() != "dumb"
    )


def styled(text: str, style: str, *, stream: TextIO) -> str:
    if not terminal_color_enabled(stream):
        return text
    return f"{style}{text}{STYLE_RESET}"


def command_status(label: str, detail: str, *, stream: TextIO) -> str:
    """Format a compact human-only command stage without exposing arguments."""
    return (
        f"{styled(label, STYLE_ACCENT, stream=stream)}: "
        f"{styled(detail, STYLE_MUTED, stream=stream)}"
    )


class TransientStatusLine:
    """Show one replaceable line without adding entries to scrollback."""

    def __init__(
        self,
        stream: TextIO,
        *,
        enabled: bool = True,
    ) -> None:
        self._stream = stream
        self._enabled = enabled and stream_is_tty(stream)
        self._visible = False

    def show(self, message: str) -> None:
        if not self._enabled:
            return
        self._stream.write(f"\r\033[2K{message}")
        self._stream.flush()
        self._visible = True

    def clear(self) -> None:
        if not self._visible:
            return
        self._stream.write("\r\033[2K")
        self._stream.flush()
        self._visible = False
