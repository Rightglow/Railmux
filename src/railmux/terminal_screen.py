"""Shared VT screen model and latest-state row differ.

Both the SSH display helper and the managed-Windows local PTY consume tmux's
terminal byte stream into this model.  Keeping the producer independent from
either transport is important: transport code owns lifecycle and input, while
this module owns only the final visible terminal state.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from functools import lru_cache
from types import SimpleNamespace

from railmux.fast_display_protocol import (
    MAX_CLIPBOARD_BYTES,
    ScreenUpdate,
    TerminalMode,
    UpdateKind,
)


_ANSI_FG = {
    "default": 39,
    "black": 30,
    "red": 31,
    "green": 32,
    "brown": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
    "brightblack": 90,
    "brightred": 91,
    "brightgreen": 92,
    "brightbrown": 93,
    "brightblue": 94,
    "brightmagenta": 95,
    "bfightmagenta": 95,  # pyte 0.8.2 compatibility typo
    "brightcyan": 96,
    "brightwhite": 97,
}
_ANSI_BG = {
    "default": 49,
    "black": 40,
    "red": 41,
    "green": 42,
    "brown": 43,
    "blue": 44,
    "magenta": 45,
    "cyan": 46,
    "white": 47,
    "brightblack": 100,
    "brightred": 101,
    "brightgreen": 102,
    "brightbrown": 103,
    "brightblue": 104,
    "brightmagenta": 105,
    "bfightmagenta": 105,
    "brightcyan": 106,
    "brightwhite": 107,
}
_OSC52_PREFIX = b"\033]52;"
_OSC52_MAX_ENCODED = ((MAX_CLIPBOARD_BYTES + 2) // 3) * 4


class Osc52ClipboardDecoder:
    """Extract bounded clipboard payloads across arbitrary PTY chunks."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> tuple[bytes, ...]:
        self._buffer.extend(data)
        decoded: list[bytes] = []
        while True:
            start = self._buffer.find(_OSC52_PREFIX)
            if start < 0:
                keep = min(len(self._buffer), len(_OSC52_PREFIX) - 1)
                if len(self._buffer) > keep:
                    del self._buffer[:-keep]
                break
            if start:
                del self._buffer[:start]
            selection_end = self._buffer.find(b";", len(_OSC52_PREFIX))
            if selection_end < 0:
                if len(self._buffer) > len(_OSC52_PREFIX) + 16:
                    del self._buffer[0]
                    continue
                break
            bel = self._buffer.find(b"\007", selection_end + 1)
            st = self._buffer.find(b"\033\\", selection_end + 1)
            endings = [
                (position, length)
                for position, length in ((bel, 1), (st, 2))
                if position >= 0
            ]
            if not endings:
                if len(self._buffer) - selection_end - 1 > _OSC52_MAX_ENCODED:
                    del self._buffer[0]
                    continue
                break
            end, terminator_length = min(endings)
            payload = bytes(self._buffer[selection_end + 1 : end])
            del self._buffer[: end + terminator_length]
            if not payload or len(payload) > _OSC52_MAX_ENCODED:
                continue
            try:
                value = base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError):
                continue
            if 0 < len(value) <= MAX_CLIPBOARD_BYTES:
                decoded.append(value)
        return tuple(decoded)


class _ExtendedScreenMixin:
    """Fill the row-mutating xterm gaps in pyte 0.8.2."""

    _last_graphic_character = ""
    _character_width = staticmethod(lambda _character: 1)

    def reset(self) -> None:
        self._last_graphic_character = ""
        super().reset()

    def draw(self, data: str) -> None:
        super().draw(data)
        for character in reversed(data):
            if self._character_width(character) > 0:
                self._last_graphic_character = character
                break

    def scroll_up(self, count: int | None = None) -> None:
        top = 0 if self.margins is None else self.margins.top
        bottom = self.lines - 1 if self.margins is None else self.margins.bottom
        amount = min(max(1, count or 1), bottom - top + 1)
        for row in range(top, bottom - amount + 1):
            source = row + amount
            if source in self.buffer:
                self.buffer[row] = self.buffer[source]
            else:
                self.buffer.pop(row, None)
        for row in range(bottom - amount + 1, bottom + 1):
            self.buffer.pop(row, None)
        self.dirty.update(range(top, bottom + 1))

    def scroll_down(self, count: int | None = None) -> None:
        top = 0 if self.margins is None else self.margins.top
        bottom = self.lines - 1 if self.margins is None else self.margins.bottom
        amount = min(max(1, count or 1), bottom - top + 1)
        for row in range(bottom, top + amount - 1, -1):
            source = row - amount
            if source in self.buffer:
                self.buffer[row] = self.buffer[source]
            else:
                self.buffer.pop(row, None)
        for row in range(top, top + amount):
            self.buffer.pop(row, None)
        self.dirty.update(range(top, bottom + 1))

    def repeat_character(self, count: int | None = None) -> None:
        if self._last_graphic_character:
            self.draw(self._last_graphic_character * max(1, count or 1))

    def report_device_status(self, mode: int, **kwargs: bool) -> None:
        if kwargs.get("private"):
            return
        super().report_device_status(mode)

    def select_graphic_rendition(self, *attrs: int) -> None:
        """Retain 256-colour indices instead of baking in pyte's palette."""
        super().select_graphic_rendition(*attrs)
        values = list(attrs)
        indexed: dict[str, int] = {}
        index = 0
        while index < len(values):
            attr = values[index]
            index += 1
            if attr == 0:
                indexed.clear()
                continue
            if 30 <= attr <= 37 or attr == 39 or 90 <= attr <= 97:
                indexed.pop("fg", None)
                continue
            if 40 <= attr <= 47 or attr == 49 or 100 <= attr <= 107:
                indexed.pop("bg", None)
                continue
            if attr not in (38, 48):
                continue
            key = "fg" if attr == 38 else "bg"
            indexed.pop(key, None)
            if index >= len(values):
                continue
            mode = values[index]
            index += 1
            if mode == 5 and index < len(values):
                colour = values[index]
                index += 1
                if 0 <= colour <= 255:
                    indexed[key] = colour
            elif mode == 2:
                index = min(len(values), index + 3)
        if indexed:
            self.cursor.attrs = self.cursor.attrs._replace(
                **{key: f"ansi256:{colour}" for key, colour in indexed.items()}
            )


@lru_cache(maxsize=4)
def _build_extended_pyte(pyte: object) -> object:
    class ExtendedScreen(_ExtendedScreenMixin, pyte.Screen):
        _character_width = staticmethod(pyte.screens.wcwidth)

    class ExtendedDiffScreen(_ExtendedScreenMixin, pyte.DiffScreen):
        _character_width = staticmethod(pyte.screens.wcwidth)

    class ExtendedByteStream(pyte.ByteStream):
        csi = dict(pyte.ByteStream.csi)
        csi.update({"S": "scroll_up", "T": "scroll_down", "b": "repeat_character"})
        events = frozenset(
            set(pyte.ByteStream.events)
            | {"scroll_up", "scroll_down", "repeat_character"}
        )

    return SimpleNamespace(
        Screen=ExtendedScreen,
        DiffScreen=ExtendedDiffScreen,
        ByteStream=ExtendedByteStream,
        screens=pyte.screens,
        modes=pyte.modes,
        _railmux_extended=True,
    )


def extended_pyte(pyte: object) -> object:
    """Idempotently adapt one imported pyte module for tmux's VT stream."""
    if getattr(pyte, "_railmux_extended", False):
        return pyte
    return _build_extended_pyte(pyte)


def _colour_codes(value: str, *, foreground: bool) -> list[str]:
    named = _ANSI_FG if foreground else _ANSI_BG
    if value in named:
        return [str(named[value])]
    indexed = re.fullmatch(r"ansi256:([0-9]{1,3})", value)
    if indexed is not None and int(indexed.group(1)) <= 255:
        return ["38" if foreground else "48", "5", indexed.group(1)]
    if len(value) == 6:
        try:
            red, green, blue = (
                int(value[0:2], 16),
                int(value[2:4], 16),
                int(value[4:6], 16),
            )
        except ValueError:
            pass
        else:
            return ["38" if foreground else "48", "2", str(red), str(green), str(blue)]
    return ["39" if foreground else "49"]


def _style(char: object) -> bytes:
    codes: list[str] = ["0"]
    for enabled, code in (
        (char.bold, "1"),
        (char.italics, "3"),
        (char.underscore, "4"),
        (char.blink, "5"),
        (char.reverse, "7"),
        (char.strikethrough, "9"),
    ):
        if enabled:
            codes.append(code)
    codes.extend(_colour_codes(char.fg, foreground=True))
    codes.extend(_colour_codes(char.bg, foreground=False))
    return f"\033[{';'.join(codes)}m".encode()


def _style_key(char: object) -> tuple[object, ...]:
    return (
        char.fg,
        char.bg,
        char.bold,
        char.italics,
        char.underscore,
        char.strikethrough,
        char.reverse,
        char.blink,
    )


_DEFAULT_STYLE_KEY = (
    "default",
    "default",
    False,
    False,
    False,
    False,
    False,
    False,
)


def _last_rendered_column(row: object, columns: int) -> int:
    """Return the exclusive end of content that survives an EL 2 repaint.

    TerminalSurface clears a changed physical row before writing this
    serialization.  Default trailing blanks are therefore redundant, and on
    a large Windows Terminal viewport they can dominate every local semantic
    frame.  Styled blanks remain significant because their background or
    decoration is visible.
    """
    for column in range(columns - 1, -1, -1):
        char = row[column]
        if char.data not in {"", " "} or _style_key(char) != _DEFAULT_STYLE_KEY:
            return column + 1
    return 0


def render_rows(screen: object) -> tuple[bytes, ...]:
    """Render allowlisted rows for consumers that clear the full row first.

    Default trailing blanks are intentionally omitted. Every shared consumer
    must erase the row or overlay width before writing one of these byte rows.
    """
    rendered_rows: list[bytes] = []
    character_width = getattr(screen, "_character_width", lambda _value: 1)
    for row_index in range(screen.lines):
        rendered = [b"\033[0m"]
        previous_style: tuple[object, ...] | None = None
        row = screen.buffer[row_index]
        rendered_columns = _last_rendered_column(row, screen.columns)
        continuation_cells = 0
        continuation_data = ""
        for column in range(rendered_columns):
            if continuation_cells:
                continuation_cells -= 1
                continuation = row[column]
                if not continuation.data or continuation.data == continuation_data:
                    continue
                continuation_cells = 0
                continuation_data = ""
            char = row[column]
            style = _style_key(char)
            if style != previous_style:
                rendered.append(_style(char))
                previous_style = style
            if char.data:
                safe_data = "".join(
                    value
                    if value >= " "
                    and value != "\x7f"
                    and not "\x80" <= value <= "\x9f"
                    else "�"
                    for value in char.data
                )
                rendered.append(safe_data.encode("utf-8", errors="replace"))
                cell_width = max(
                    (character_width(value) for value in char.data),
                    default=1,
                )
                continuation_cells = max(0, cell_width - 1)
                continuation_data = char.data
        rendered.append(b"\033[0m")
        rendered_rows.append(b"".join(rendered))
    return tuple(rendered_rows)


def terminal_modes_for_screen(screen: object) -> TerminalMode:
    terminal_modes = TerminalMode.NONE
    if 2004 << 5 in screen.mode:
        terminal_modes |= TerminalMode.BRACKETED_PASTE
    if 1004 << 5 in screen.mode:
        terminal_modes |= TerminalMode.FOCUS_EVENTS
    return terminal_modes


@dataclass(frozen=True)
class ScreenState:
    sequence: int
    width: int
    height: int
    cursor_x: int
    cursor_y: int
    cursor_visible: bool
    terminal_modes: TerminalMode
    rows: tuple[bytes, ...]


def build_screen_update(
    screen: object,
    modes: object,
    *,
    width: int,
    height: int,
    delivered: ScreenState | None,
    force_keyframe: bool = False,
) -> tuple[ScreenUpdate | None, ScreenState | None]:
    """Build one latest-state update without advancing the caller's base."""
    rows = render_rows(screen)
    cursor_x = min(screen.cursor.x, width - 1)
    cursor_y = min(screen.cursor.y, height - 1)
    cursor_visible = modes.DECTCEM in screen.mode
    terminal_modes = terminal_modes_for_screen(screen)
    keyframe = (
        force_keyframe
        or delivered is None
        or delivered.width != width
        or delivered.height != height
    )
    if keyframe:
        changed_rows = tuple(enumerate(rows))
        kind = UpdateKind.KEYFRAME
    else:
        changed_rows = tuple(
            (index, row)
            for index, row in enumerate(rows)
            if row != delivered.rows[index]
        )
        kind = UpdateKind.PATCH
        if not changed_rows and (
            cursor_x == delivered.cursor_x
            and cursor_y == delivered.cursor_y
            and cursor_visible == delivered.cursor_visible
            and terminal_modes == delivered.terminal_modes
        ):
            return None, None
    sequence = 1 if delivered is None else (delivered.sequence + 1) & 0xFFFFFFFF
    update = ScreenUpdate(
        kind=kind,
        sequence=sequence,
        width=width,
        height=height,
        cursor_x=cursor_x,
        cursor_y=cursor_y,
        cursor_visible=cursor_visible,
        rows=changed_rows,
        terminal_modes=terminal_modes,
    )
    return update, ScreenState(
        sequence=sequence,
        width=width,
        height=height,
        cursor_x=cursor_x,
        cursor_y=cursor_y,
        cursor_visible=cursor_visible,
        terminal_modes=terminal_modes,
        rows=rows,
    )


class ScreenProducer:
    """In-process VT producer used by the managed-Windows local adapter."""

    def __init__(self, width: int, height: int) -> None:
        import pyte

        self.pyte = extended_pyte(pyte)
        self.modes = self.pyte.modes
        self.width = width
        self.height = height
        self.screen = self.pyte.DiffScreen(width, height)
        self.stream = self.pyte.ByteStream(self.screen)
        self._clipboard = Osc52ClipboardDecoder()
        self._clipboard_ready: list[bytes] = []
        self.delivered: ScreenState | None = None
        self.received_output = False
        # Do not clear the physical terminal before the PTY has produced its
        # first byte. The first real sample is still a mandatory keyframe.
        self.dirty = False
        self.force_keyframe = True

    def feed(self, data: bytes) -> None:
        if data:
            self.received_output = True
            self._clipboard_ready.extend(self._clipboard.feed(data))
            self.stream.feed(data)
            self.dirty = True

    def drain_clipboard(self) -> tuple[bytes, ...]:
        ready = tuple(self._clipboard_ready)
        self._clipboard_ready.clear()
        return ready

    def resize(self, width: int, height: int) -> None:
        if (width, height) == (self.width, self.height):
            return
        self.screen.resize(lines=height, columns=width)
        self.width = width
        self.height = height
        self.force_keyframe = True
        self.dirty = True

    @property
    def synchronized_update_active(self) -> bool:
        return bool(2026 << 5 in self.screen.mode)

    def take_update(self) -> ScreenUpdate | None:
        if not (self.dirty or self.force_keyframe):
            return None
        update, state = build_screen_update(
            self.screen,
            self.modes,
            width=self.width,
            height=self.height,
            delivered=self.delivered,
            force_keyframe=self.force_keyframe,
        )
        self.dirty = False
        self.force_keyframe = False
        if update is not None:
            assert state is not None
            self.delivered = state
        return update
