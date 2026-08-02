"""Pure terminal-emulation helpers shared by POSIX tmux and Windows ConPTY."""
from __future__ import annotations

from functools import lru_cache
from types import SimpleNamespace

from railmux.fast_display_protocol import TerminalMode


class ExtendedScreenMixin:
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


@lru_cache(maxsize=4)
def build_extended_pyte(pyte: object) -> object:
    class ExtendedScreen(ExtendedScreenMixin, pyte.Screen):
        _character_width = staticmethod(pyte.screens.wcwidth)

    class ExtendedDiffScreen(ExtendedScreenMixin, pyte.DiffScreen):
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
        _railmux_extended=True,
    )


def extended_pyte(pyte: object) -> object:
    if getattr(pyte, "_railmux_extended", False):
        return pyte
    return build_extended_pyte(pyte)


_ANSI_FG = {
    "default": 39, "black": 30, "red": 31, "green": 32, "brown": 33,
    "blue": 34, "magenta": 35, "cyan": 36, "white": 37,
    "brightblack": 90, "brightred": 91, "brightgreen": 92,
    "brightbrown": 93, "brightblue": 94, "brightmagenta": 95,
    "bfightmagenta": 95, "brightcyan": 96, "brightwhite": 97,
}
_ANSI_BG = {
    "default": 49, "black": 40, "red": 41, "green": 42, "brown": 43,
    "blue": 44, "magenta": 45, "cyan": 46, "white": 47,
    "brightblack": 100, "brightred": 101, "brightgreen": 102,
    "brightbrown": 103, "brightblue": 104, "brightmagenta": 105,
    "bfightmagenta": 105, "brightcyan": 106, "brightwhite": 107,
}


def _colour_codes(value: str, *, foreground: bool) -> list[str]:
    named = _ANSI_FG if foreground else _ANSI_BG
    if value in named:
        return [str(named[value])]
    if len(value) == 6:
        try:
            red, green, blue = (
                int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
            )
        except ValueError:
            pass
        else:
            return ["38" if foreground else "48", "2", str(red), str(green), str(blue)]
    return ["39" if foreground else "49"]


def _style(char: object) -> bytes:
    codes: list[str] = ["0"]
    for enabled, code in (
        (char.bold, "1"), (char.italics, "3"), (char.underscore, "4"),
        (char.blink, "5"), (char.reverse, "7"), (char.strikethrough, "9"),
    ):
        if enabled:
            codes.append(code)
    codes.extend(_colour_codes(char.fg, foreground=True))
    codes.extend(_colour_codes(char.bg, foreground=False))
    return f"\033[{';'.join(codes)}m".encode()


def _style_key(char: object) -> tuple[object, ...]:
    return (
        char.fg, char.bg, char.bold, char.italics, char.underscore,
        char.strikethrough, char.reverse, char.blink,
    )


def render_rows(screen: object) -> tuple[bytes, ...]:
    """Render independently paintable rows with allowlisted SGR controls."""
    rendered_rows: list[bytes] = []
    for row_index in range(screen.lines):
        rendered = [b"\033[0m"]
        previous_style: tuple[object, ...] | None = None
        row = screen.buffer[row_index]
        for column in range(screen.columns):
            char = row[column]
            style = _style_key(char)
            if style != previous_style:
                rendered.append(_style(char))
                previous_style = style
            if char.data:
                safe_data = "".join(
                    value if value >= " " and value != "\x7f"
                    and not "\x80" <= value <= "\x9f" else "�"
                    for value in char.data
                )
                rendered.append(safe_data.encode("utf-8", errors="replace"))
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

