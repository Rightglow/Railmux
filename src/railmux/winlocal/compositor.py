"""Terminal-cell compositor for the Windows daemon."""
from __future__ import annotations

from dataclasses import dataclass
import unicodedata

import pyte

from railmux.fast_display_protocol import ScreenUpdate, TerminalMode, UpdateKind
from railmux.terminal_emulator import extended_pyte, render_rows


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    width: int
    height: int


class TerminalPane:
    """One ConPTY/UI byte stream represented as a bounded terminal screen."""

    def __init__(self, width: int, height: int) -> None:
        terminal = extended_pyte(pyte)
        self.screen = terminal.Screen(width, height)
        self.stream = terminal.ByteStream(self.screen)

    def feed(self, data: bytes) -> None:
        self.stream.feed(data)

    def resize(self, width: int, height: int) -> None:
        self.screen.resize(height, width)

    @property
    def rows(self) -> tuple[bytes, ...]:
        return render_rows(self.screen)


class Compositor:
    """Compose sidebar and up to two agent screens into protocol keyframes."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.sequence = 0
        self._last_rows: tuple[bytes, ...] | None = None
        self._terminal = TerminalPane(width, height)

    def resize(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self._terminal.resize(width, height)
        self._last_rows = None

    def invalidate(self) -> None:
        """Force the next composed frame to be a complete keyframe."""
        self._last_rows = None

    def regions(
        self,
        *,
        has_primary: bool,
        has_secondary: bool,
        stacked: bool = False,
    ) -> dict[str, Region]:
        """Return the authoritative input/render geometry for this frame."""
        content_height = max(1, self.height - 1)
        if not has_primary:
            return {
                "sidebar": Region(0, 0, self.width, content_height),
            }
        sidebar_width = min(
            max(18, self.width * 3 // 10), max(18, self.width - 2)
        )
        result = {
            "sidebar": Region(0, 0, sidebar_width, content_height),
        }
        agent_x = min(self.width, sidebar_width + 1)
        agent_width = max(0, self.width - agent_x)
        if not has_primary or not agent_width:
            return result
        if not has_secondary:
            result["primary"] = Region(
                agent_x, 0, agent_width, content_height
            )
        elif stacked:
            top = max(1, (content_height - 1) // 2)
            result["primary"] = Region(agent_x, 0, agent_width, top)
            result["secondary"] = Region(
                agent_x, top + 1, agent_width, content_height - top - 1
            )
        else:
            left = max(1, (agent_width - 1) // 2)
            result["primary"] = Region(agent_x, 0, left, content_height)
            result["secondary"] = Region(
                agent_x + left + 1,
                0,
                agent_width - left - 1,
                content_height,
            )
        return result

    def compose(
        self,
        sidebar: TerminalPane,
        primary: TerminalPane | None,
        secondary: TerminalPane | None = None,
        *,
        stacked: bool = False,
        status: bytes = b"railmux",
        focus: str = "sidebar",
    ) -> ScreenUpdate:
        content_height = max(1, self.height - 1)
        geometry = self.regions(
            has_primary=primary is not None,
            has_secondary=secondary is not None,
            stacked=stacked,
        )
        panes = {
            "sidebar": sidebar,
            "primary": primary,
            "secondary": secondary,
        }
        regions = [
            (name, region, panes[name])
            for name, region in geometry.items()
            if panes[name] is not None
        ]
        sidebar_width = geometry["sidebar"].width
        agent_x = min(self.width, sidebar_width + 1)
        agent_width = max(0, self.width - agent_x)

        output = [b"\033[0m\033[2J\033[H"]
        for name, region, pane in regions:
            pane.resize(max(1, region.width), max(1, region.height))
            for offset, row in enumerate(pane.rows[: region.height]):
                output.append(_move(region.y + offset, region.x) + row)
            if name == focus and region.width < self.width:
                output.append(_border_accent(region))
        if agent_x > sidebar_width:
            output.append(_vertical_border(sidebar_width, content_height))
        if secondary is not None and stacked:
            output.append(_horizontal_border(agent_x, regions[-1][1].y - 1, agent_width))
        elif secondary is not None:
            output.append(_vertical_border(regions[-1][1].x - 1, content_height))
        output.append(
            _move(self.height - 1, 0)
            + b"\033[0;30;42m"
            + _bounded_status(status, self.width)
            + b"\033[K\033[0m"
        )
        self._terminal.feed(b"".join(output))
        rows = self._terminal.rows
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF or 1
        kind = UpdateKind.KEYFRAME if self._last_rows is None else UpdateKind.PATCH
        changed = tuple(enumerate(rows)) if self._last_rows is None else tuple(
            (index, row) for index, row in enumerate(rows) if row != self._last_rows[index]
        )
        self._last_rows = rows
        cursor_region = next((region for name, region, _pane in regions if name == focus), regions[0][1])
        cursor_pane = next((pane for name, _region, pane in regions if name == focus), sidebar)
        return ScreenUpdate(
            kind=kind,
            sequence=self.sequence,
            width=self.width,
            height=self.height,
            cursor_x=min(self.width - 1, cursor_region.x + cursor_pane.screen.cursor.x),
            cursor_y=min(self.height - 1, cursor_region.y + cursor_pane.screen.cursor.y),
            cursor_visible=not cursor_pane.screen.cursor.hidden,
            rows=changed,
            terminal_modes=TerminalMode.BRACKETED_PASTE | TerminalMode.FOCUS_EVENTS,
        )

    def compose_full(
        self,
        pane: TerminalPane,
        *,
        status: bytes = b"railmux",
    ) -> ScreenUpdate:
        """Compose one zoomed surface while retaining the shared status row."""
        content_height = max(1, self.height - 1)
        pane.resize(self.width, content_height)
        output = [b"\033[0m\033[2J\033[H"]
        for row, value in enumerate(pane.rows[:content_height]):
            output.append(_move(row, 0) + value)
        output.append(
            _move(self.height - 1, 0)
            + b"\033[0;30;42m"
            + _bounded_status(status, self.width)
            + b"\033[K\033[0m"
        )
        self._terminal.feed(b"".join(output))
        rows = self._terminal.rows
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF or 1
        kind = UpdateKind.KEYFRAME if self._last_rows is None else UpdateKind.PATCH
        changed = tuple(enumerate(rows)) if self._last_rows is None else tuple(
            (index, row)
            for index, row in enumerate(rows)
            if row != self._last_rows[index]
        )
        self._last_rows = rows
        return ScreenUpdate(
            kind=kind,
            sequence=self.sequence,
            width=self.width,
            height=self.height,
            cursor_x=min(self.width - 1, pane.screen.cursor.x),
            cursor_y=min(self.height - 1, pane.screen.cursor.y),
            cursor_visible=not pane.screen.cursor.hidden,
            rows=changed,
            terminal_modes=TerminalMode.BRACKETED_PASTE | TerminalMode.FOCUS_EVENTS,
        )


def _move(row: int, column: int) -> bytes:
    return f"\033[{row + 1};{column + 1}H".encode()


def _vertical_border(column: int, height: int) -> bytes:
    return b"".join(_move(row, column) + "│".encode() for row in range(height))


def _horizontal_border(column: int, row: int, width: int) -> bytes:
    return _move(row, column) + ("─" * width).encode()


def _border_accent(region: Region) -> bytes:
    if region.x == 0:
        column = region.x + region.width
    else:
        column = region.x - 1
    if column < 0:
        return b""
    return b"\033[32m" + _vertical_border(column, region.height) + b"\033[0m"


def _bounded_status(status: bytes, width: int) -> bytes:
    text = status.decode("utf-8", errors="replace")
    result = []
    cells = 0
    for character in text:
        if ord(character) < 32 or 0x7F <= ord(character) <= 0x9F:
            character = " "
        character_width = (
            0
            if unicodedata.combining(character)
            else 1 if ord(character) < 128 else 2
        )
        if cells + character_width > width:
            break
        result.append(character)
        cells += character_width
    return "".join(result).encode("utf-8")
