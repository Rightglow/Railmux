"""Terminal input decoding and pane-bounded local text selection.

This leaf module deliberately has no dependency on the SSH process lifecycle.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import lru_cache

from railmux.fast_display_protocol import HistorySnapshot, MAX_CLIPBOARD_BYTES

_SGR_MOUSE_PREFIX = b"\x1b[<"
_SGR_STYLE_RE = re.compile(rb"\x1b\[[0-9;]*m")
_PAGE_UP = b"\x1b[5~"
_PAGE_DOWN = b"\x1b[6~"


@dataclass(frozen=True)
class SgrMouseEvent:
    raw: bytes
    button: int
    x: int
    y: int
    pressed: bool

    @property
    def wheel_direction(self) -> int:
        base_button = self.button & 3
        if not self.pressed or not self.button & 64 or base_button not in (0, 1):
            return 0
        return -1 if base_button == 1 else 1

    def translated_y(self, offset: int) -> "SgrMouseEvent":
        """Translate a local projected row back into remote screen space."""
        if offset == 0:
            return self
        y = self.y + offset
        terminator = b"M" if self.pressed else b"m"
        raw = _SGR_MOUSE_PREFIX + f"{self.button};{self.x};{y}".encode() + terminator
        return replace(self, raw=raw, y=y)


def is_termux_environment(environ: Mapping[str, str] | None = None) -> bool:
    """Recognize an official Termux client without guessing from geometry."""
    values = os.environ if environ is None else environ
    if values.get("TERMUX_VERSION"):
        return True
    return values.get("PREFIX", "").rstrip("/") == "/data/data/com.termux/files/usr"


@dataclass(frozen=True)
class TouchKeyboardAction:
    """One local-only mouse-tracking transition for a Termux prompt tap."""

    handled: bool = False
    suspend_mouse: bool = False
    show_hint: bool = False


class TermuxTouchKeyboard:
    """Temporarily yield one focused agent prompt to Termux's soft keyboard."""

    def __init__(
        self,
        *,
        enabled: bool,
        timeout: float = 10.0,
        input_row_radius: int = 1,
    ) -> None:
        self.enabled = enabled
        self.timeout = timeout
        self.input_row_radius = input_row_radius
        self._active = False
        self._keyboard_projected = False
        self._deadline: float | None = None
        self._release_button: int | None = None

    @property
    def active(self) -> bool:
        return self._active

    @staticmethod
    def _is_plain_left_press(event: SgrMouseEvent) -> bool:
        return (
            event.pressed
            and event.button == 0
            and event.wheel_direction == 0
        )

    def pointer_event(
        self,
        event: SgrMouseEvent,
        *,
        clicked_pane_id: str | None,
        cursor_pane_id: str | None,
        cursor_y: int,
        cursor_visible: bool,
        pane_frozen: bool,
        now: float | None = None,
    ) -> TouchKeyboardAction:
        """Consume a prompt tap and its paired release when assistance applies."""
        if self._release_button is not None:
            if (
                not event.pressed
                and event.button & 3 == self._release_button
            ):
                self._release_button = None
                return TouchKeyboardAction(handled=True)
            # Termux normally emits only the paired release after the press.
            # Leave unrelated queued reports on their ordinary routing path.
            return TouchKeyboardAction()
        if (
            not self.enabled
            or self._active
            or pane_frozen
            or not cursor_visible
            or clicked_pane_id is None
            or clicked_pane_id != cursor_pane_id
            or abs((event.y - 1) - cursor_y) > self.input_row_radius
            or not self._is_plain_left_press(event)
        ):
            return TouchKeyboardAction()
        requested_at = time.monotonic() if now is None else now
        self._active = True
        self._keyboard_projected = False
        self._deadline = requested_at + self.timeout
        self._release_button = event.button & 3
        return TouchKeyboardAction(
            handled=True,
            suspend_mouse=True,
            show_hint=True,
        )

    def observe_projection(self, projected: bool) -> bool:
        """Track keyboard geometry and request mouse restore when it closes."""
        if not self._active:
            return False
        if projected:
            self._keyboard_projected = True
            self._deadline = None
            return False
        if self._keyboard_projected:
            return self.cancel()
        return False

    def keyboard_input(self) -> bool:
        """Restore tracking after input when no keyboard resize was observable."""
        if self._active and not self._keyboard_projected:
            return self.cancel()
        return False

    def expire(self, now: float | None = None) -> bool:
        """Restore tracking if the second tap never opens a keyboard."""
        if not self._active or self._deadline is None:
            return False
        checked_at = time.monotonic() if now is None else now
        return self.cancel() if checked_at >= self._deadline else False

    def cancel(self) -> bool:
        """Clear local touch state and report whether tracking must resume."""
        was_active = self._active
        self._active = False
        self._keyboard_projected = False
        self._deadline = None
        self._release_button = None
        return was_active


class TerminalInputDecoder:
    """Split bounded SGR mouse reports while retaining partial terminal keys."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._pending_since: float | None = None

    def _finish(
        self,
        parts: list[bytes | SgrMouseEvent],
    ) -> list[bytes | SgrMouseEvent]:
        if self._buffer:
            if self._pending_since is None:
                self._pending_since = time.monotonic()
        else:
            self._pending_since = None
        return parts

    @staticmethod
    def _append_bytes(parts: list[bytes | SgrMouseEvent], data: bytes) -> None:
        if not data:
            return
        if parts and isinstance(parts[-1], bytes):
            parts[-1] += data
        else:
            parts.append(data)

    def feed(self, data: bytes) -> list[bytes | SgrMouseEvent]:
        self._buffer.extend(data)
        parts: list[bytes | SgrMouseEvent] = []
        while self._buffer:
            marker = self._buffer.find(_SGR_MOUSE_PREFIX)
            if marker < 0:
                keep = 0
                for prefix in (_SGR_MOUSE_PREFIX, _PAGE_UP, _PAGE_DOWN):
                    for size in range(
                        1,
                        min(len(self._buffer), len(prefix) - 1) + 1,
                    ):
                        if self._buffer[-size:] == prefix[:size]:
                            keep = max(keep, size)
                emit = len(self._buffer) - keep
                self._append_bytes(parts, bytes(self._buffer[:emit]))
                del self._buffer[:emit]
                return self._finish(parts)
            if marker:
                self._append_bytes(parts, bytes(self._buffer[:marker]))
                del self._buffer[:marker]
            end = next(
                (
                    index
                    for index, value in enumerate(
                        self._buffer[len(_SGR_MOUSE_PREFIX) :], len(_SGR_MOUSE_PREFIX)
                    )
                    if value in (ord("M"), ord("m"))
                ),
                None,
            )
            if end is None:
                if len(self._buffer) <= 64:
                    return self._finish(parts)
                self._append_bytes(parts, bytes((self._buffer[0],)))
                del self._buffer[0]
                continue
            raw = bytes(self._buffer[: end + 1])
            del self._buffer[: end + 1]
            fields = raw[len(_SGR_MOUSE_PREFIX) : -1].split(b";")
            try:
                if len(fields) != 3:
                    raise ValueError
                button, x, y = (int(field) for field in fields)
                if not 0 <= button <= 255 or not 1 <= x <= 1000 or not 1 <= y <= 500:
                    raise ValueError
            except ValueError:
                self._append_bytes(parts, raw)
                continue
            parts.append(SgrMouseEvent(raw, button, x, y, raw[-1:] == b"M"))
        return self._finish(parts)

    def next_timeout(self, maximum: float = 0.1, delay: float = 0.02) -> float:
        if self._pending_since is None:
            return maximum
        remaining = delay - (time.monotonic() - self._pending_since)
        return max(0.0, min(maximum, remaining))

    def flush_pending(self, delay: float = 0.02) -> list[bytes]:
        if (
            not self._buffer
            or self._pending_since is None
            or time.monotonic() - self._pending_since < delay
        ):
            return []
        data = bytes(self._buffer)
        self._buffer.clear()
        self._pending_since = None
        return [data]


@dataclass(frozen=True)
class SelectionSource:
    """One immutable visible agent-pane surface eligible for local selection."""

    route: HistorySnapshot
    rows: tuple[bytes, ...]
    row_x_offset: int


SelectionSegment = tuple[int, int, bytes]


@dataclass(frozen=True)
class SelectionAction:
    """Pure routing result for one local text-selection pointer event."""

    handled: bool = False
    replay_events: tuple[SgrMouseEvent, ...] = ()
    repaint: bool = False
    copy_data: bytes | None = None


@lru_cache(maxsize=4096)
def _display_width(character: str) -> int:
    """Return one terminal-cell width without adding a base dependency."""
    try:
        import pyte
    except ImportError:
        if unicodedata.combining(character) or character == "\u200d":
            return 0
        return 2 if unicodedata.east_asian_width(character) in ("F", "W") else 1
    return max(0, pyte.screens.wcwidth(character))


def _plain_display_cells(line: bytes, width: int) -> tuple[str | None, ...]:
    """Decode one server-rendered SGR row into bounded display cells."""
    plain = _SGR_STYLE_RE.sub(b"", line).decode("utf-8", errors="replace")
    cells: list[str | None] = [" "] * width
    column = 0
    for character in plain:
        cell_width = _display_width(character)
        if cell_width == 0:
            previous = column - 1
            while previous >= 0 and cells[previous] is None:
                previous -= 1
            if previous >= 0:
                cells[previous] = (cells[previous] or "") + character
            continue
        if column >= width or column + cell_width > width:
            break
        cells[column] = character
        for continuation in range(1, cell_width):
            cells[column + continuation] = None
        column += cell_width
    return tuple(cells)


class LocalTextSelection:
    """Own one pane-bounded, visible-screen selection for ``railmux ssh``."""

    def __init__(self) -> None:
        self._press: SgrMouseEvent | None = None
        self._route: HistorySnapshot | None = None
        self._rows: tuple[tuple[str | None, ...], ...] = ()
        self._anchor: tuple[int, int] | None = None
        self._head: tuple[int, int] | None = None
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def capturing(self) -> bool:
        return self._press is not None

    def cancel(self) -> bool:
        was_active = self._active
        self._press = None
        self._route = None
        self._rows = ()
        self._anchor = None
        self._head = None
        self._active = False
        return was_active

    def validate_routes(
        self,
        routes: tuple[HistorySnapshot, ...],
    ) -> bool:
        """Cancel when a refreshed route set no longer matches the capture."""
        if self._route is None:
            return False
        route = self._route
        if any(
            candidate.pane_id == route.pane_id
            and candidate.x == route.x
            and candidate.y == route.y
            and candidate.width == route.width
            and candidate.height == route.height
            for candidate in routes
        ):
            return False
        return self.cancel()

    @staticmethod
    def _is_plain_left_press(event: SgrMouseEvent) -> bool:
        return (
            event.pressed
            and event.wheel_direction == 0
            and not event.button & 32
            and event.button & 3 == 0
        )

    @staticmethod
    def _point(
        event: SgrMouseEvent,
        route: HistorySnapshot,
    ) -> tuple[int, int]:
        x = min(
            route.width - 1,
            max(0, event.x - 1 - route.x),
        )
        y = min(
            route.height - 1,
            max(0, event.y - 1 - route.y),
        )
        return x, y

    def _begin(
        self,
        event: SgrMouseEvent,
        source: SelectionSource,
    ) -> None:
        route = source.route
        decoded: list[tuple[str | None, ...]] = []
        decode_width = source.row_x_offset + route.width
        for index in range(route.height):
            line = source.rows[index] if index < len(source.rows) else b""
            cells = _plain_display_cells(line, decode_width)
            decoded.append(
                cells[source.row_x_offset : source.row_x_offset + route.width]
            )
        self._press = event
        self._route = route
        self._rows = tuple(decoded)
        self._anchor = self._point(event, route)
        self._head = self._anchor
        self._active = False

    def pointer_event(
        self,
        event: SgrMouseEvent,
        source: SelectionSource | None,
    ) -> SelectionAction:
        """Capture a drag, or replay an unchanged click through normal routing."""
        if self._press is not None:
            assert self._route is not None
            if event.pressed and event.button & 32:
                head = self._point(event, self._route)
                changed = head != self._head
                self._head = head
                if head != self._anchor:
                    self._active = True
                return SelectionAction(
                    handled=True,
                    repaint=changed and self._active,
                )
            if not event.pressed:
                press = self._press
                if not self._active:
                    self.cancel()
                    return SelectionAction(
                        handled=True,
                        replay_events=(press, event),
                    )
                head = self._point(event, self._route)
                changed = head != self._head
                self._head = head
                self._press = None
                return SelectionAction(
                    handled=True,
                    repaint=changed,
                    copy_data=self.selected_text(),
                )
            press = self._press
            repaint = self.cancel()
            return SelectionAction(
                replay_events=(press,),
                repaint=repaint,
            )

        if self._is_plain_left_press(event) and source is not None:
            repaint = self.cancel()
            self._begin(event, source)
            return SelectionAction(handled=True, repaint=repaint)

        return SelectionAction(repaint=self.cancel())

    def _ordered_points(self) -> tuple[tuple[int, int], tuple[int, int]]:
        assert self._anchor is not None and self._head is not None
        if (self._anchor[1], self._anchor[0]) <= (self._head[1], self._head[0]):
            return self._anchor, self._head
        return self._head, self._anchor

    @staticmethod
    def _row_span(
        row: int,
        start: tuple[int, int],
        end: tuple[int, int],
        width: int,
    ) -> tuple[int, int]:
        start_x = start[0] if row == start[1] else 0
        end_x = end[0] if row == end[1] else width - 1
        return start_x, end_x

    @staticmethod
    def _adjust_wide_start(
        cells: tuple[str | None, ...],
        start: int,
    ) -> int:
        while start > 0 and cells[start] is None:
            start -= 1
        return start

    def selected_text(self) -> bytes | None:
        if not self._active or self._route is None:
            return None
        start, end = self._ordered_points()
        lines: list[str] = []
        for row in range(start[1], end[1] + 1):
            cells = self._rows[row]
            start_x, end_x = self._row_span(row, start, end, self._route.width)
            start_x = self._adjust_wide_start(cells, start_x)
            text = "".join(cell or "" for cell in cells[start_x : end_x + 1]).rstrip(
                " "
            )
            lines.append(text)
        data = "\n".join(lines).encode("utf-8")
        if not data:
            return None
        if len(data) > MAX_CLIPBOARD_BYTES:
            data = (
                data[:MAX_CLIPBOARD_BYTES]
                .decode("utf-8", errors="ignore")
                .encode("utf-8")
            )
        return data or None

    def segments(self) -> tuple[SelectionSegment, ...]:
        """Return reverse-video text runs in logical screen coordinates."""
        if not self._active or self._route is None:
            return ()
        start, end = self._ordered_points()
        segments: list[SelectionSegment] = []
        for row in range(start[1], end[1] + 1):
            cells = self._rows[row]
            start_x, end_x = self._row_span(row, start, end, self._route.width)
            start_x = self._adjust_wide_start(cells, start_x)
            text = "".join(cell or "" for cell in cells[start_x : end_x + 1])
            segments.append(
                (
                    self._route.y + row,
                    self._route.x + start_x,
                    text.encode("utf-8"),
                )
            )
        return tuple(segments)


def page_key_direction(data: bytes) -> int:
    """Return local-history direction for an unmodified terminal page key."""
    if data == _PAGE_UP:
        return 1
    if data == _PAGE_DOWN:
        return -1
    return 0


def split_page_key_input(data: bytes) -> tuple[bytes, ...]:
    """Split complete page keys from adjacent opaque terminal input."""
    parts: list[bytes] = []
    start = 0
    while start < len(data):
        matches: list[tuple[int, bytes]] = []
        for key in (_PAGE_UP, _PAGE_DOWN):
            position = data.find(key, start)
            while position > 0 and data[position - 1] == 0x1B:
                position = data.find(key, position + len(key))
            if position >= 0:
                matches.append((position, key))
        if not matches:
            parts.append(data[start:])
            break
        position, key = min(matches, key=lambda item: item[0])
        if position > start:
            parts.append(data[start:position])
        parts.append(key)
        start = position + len(key)
    return tuple(part for part in parts if part)
