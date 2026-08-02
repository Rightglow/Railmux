"""Small read-only transcript pager for the native Windows compositor."""
from __future__ import annotations

import io
import re
from pathlib import Path

from railmux.fast_display_input import SgrMouseEvent, TerminalInputDecoder
from railmux.transcript import format_transcript
from railmux.winlocal.compositor import TerminalPane


_PAGE_UP = (b"\x1b[5~", b"\x1b[A")
_PAGE_DOWN = (b"\x1b[6~", b"\x1b[B")
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_RECORDS = 2000


class HistoryViewer:
    """A bounded ANSI transcript viewport with keyboard and wheel scrolling."""

    def __init__(self, path: Path, fmt: str, width: int, height: int) -> None:
        self.path = path
        self.fmt = fmt
        self.terminal = TerminalPane(width, height)
        self.width = width
        self.height = height
        self._decoder = TerminalInputDecoder()
        chunks = list(format_transcript(_bounded_tail(path), fmt=fmt))
        self._lines = "".join(chunks).splitlines()[-20000:]
        self._offset = 0
        self._render()

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.terminal.resize(width, height)
        self._render()

    def input(self, data: bytes) -> bool:
        """Apply pager input and return False when the viewer should close."""
        for part in self._decoder.feed(data):
            if isinstance(part, SgrMouseEvent):
                direction = part.wheel_direction
                if direction:
                    self._scroll(direction * 3)
                continue
            if part in {b"q", b"\x1b"}:
                return False
            if any(key in part for key in _PAGE_UP):
                self._scroll(max(1, self.height - 2))
            elif any(key in part for key in _PAGE_DOWN):
                self._scroll(-max(1, self.height - 2))
            elif b"g" == part:
                self._offset = max(0, len(self._lines) - self.height)
                self._render()
            elif b"G" == part or b"\x1b[F" in part:
                self._offset = 0
                self._render()
        return True

    def _scroll(self, delta: int) -> None:
        maximum = max(0, len(self._lines) - max(1, self.height - 1))
        self._offset = max(0, min(maximum, self._offset + delta))
        self._render()

    def _render(self) -> None:
        end = max(0, len(self._lines) - self._offset)
        start = max(0, end - max(1, self.height - 1))
        body = "\n".join(self._lines[start:end])
        plain_path = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", self.path.name)
        status = (
            f"\x1b[0;30;46m History: {plain_path} · "
            f"{start + 1}-{end}/{len(self._lines)} · q closes \x1b[K\x1b[0m"
        )
        self.terminal.feed(
            ("\x1b[0m\x1b[2J\x1b[H" + body + "\n" + status).encode(
                "utf-8", errors="replace"
            )
        )


def _bounded_tail(path: Path) -> io.StringIO:
    """Return a whole-record suffix with bounded daemon memory use."""
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        start = max(0, size - _MAX_SOURCE_BYTES)
        stream.seek(start)
        raw = stream.read(_MAX_SOURCE_BYTES)
    if start:
        newline = raw.find(b"\n")
        raw = b"" if newline < 0 else raw[newline + 1 :]
    lines = raw.splitlines(keepends=True)[-_MAX_RECORDS:]
    return io.StringIO(b"".join(lines).decode("utf-8", errors="replace"))
