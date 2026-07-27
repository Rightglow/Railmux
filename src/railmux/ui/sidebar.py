"""Shared chrome for the three sidebar sections."""
from __future__ import annotations

from collections.abc import Callable, Sequence

import urwid

from railmux.ui._widgets import ScrollableSidebarPane


def _truncate_title(title: str, maxcol: int) -> str:
    """Keep the stable section name visible and trim only the trailing detail."""
    maxcol = max(1, maxcol)
    if urwid.str_util.calc_width(title, 0, len(title)) <= maxcol:
        return title
    if maxcol == 1:
        return title[:1]
    pos, _ = urwid.calc_text_pos(title, 0, len(title), maxcol - 1)
    return title[:pos].rstrip() + "…"


class SidebarSection(urwid.WidgetWrap):
    """An unboxed pane preceded by one focus-aware labelled divider."""

    def __init__(
        self,
        pane: ScrollableSidebarPane,
        title: Callable[[], str],
        count: Callable[[], str] | None = None,
    ) -> None:
        self.pane = pane
        self._title = title
        self._count = count
        self._previous_section_focused = False
        self._header = urwid.Text("")
        self._pile = urwid.Pile([
            ("pack", self._header),
            ("weight", 1, pane),
        ])
        self._pile.focus_position = 1
        super().__init__(self._pile)

    def set_previous_section_focused(self, focused: bool) -> None:
        """Treat this title rule as the previous section's lower boundary."""
        if self._previous_section_focused == focused:
            return
        self._previous_section_focused = focused
        self._invalidate()

    def render(self, size, focus: bool = False):
        maxcol = size[0]
        raw_title = self._title()
        stable, separator, detail = raw_title.partition(" ")
        display_title = stable.upper() + separator + detail
        count = self._count() if self._count is not None else ""
        count = str(count).strip()

        prefix = "─ "
        count_segment = f" {count} " if count else ""
        end = "─"
        fixed = prefix + " " + count_segment + end
        fixed_width = urwid.calc_width(fixed, 0, len(fixed))
        title = _truncate_title(
            display_title, max(1, maxcol - fixed_width))
        base = prefix + title + " " + count_segment + end
        used = urwid.calc_width(base, 0, len(base))
        middle = " " + "─" * max(0, maxcol - used)
        header = prefix + title + middle + count_segment + end

        if focus:
            self._header.set_text(("pane_focus", header))
        elif self._previous_section_focused:
            # This row closes the selected section while introducing the next:
            # keep the boundary green without colouring the next title as if it
            # also owned keyboard focus.
            markup: list = [
                ("pane_focus", prefix),
                ("pane", title),
                ("pane_focus", middle),
            ]
            if count_segment:
                markup.append(("pane", count_segment))
            markup.append(("pane_focus", end))
            self._header.set_text(markup)
        else:
            # At rest, title and rule share one neutral tone so the three
            # section boundaries read as one consistent hierarchy.
            self._header.set_text(("pane", header))
        return super().render(size, focus)


class StableWeightedPile(urwid.Pile):
    """A weighted Pile whose row rounding never depends on focus position."""

    def __init__(self, widget_list, focus_item=None) -> None:
        super().__init__(widget_list, focus_item=focus_item)
        self._bottom_row_debt = 0

    def set_bottom_row_debt(self, rows: int) -> None:
        """Charge temporary footer growth to the bottom section first."""
        rows = max(0, int(rows))
        if rows == self._bottom_row_debt:
            return
        self._bottom_row_debt = rows
        self._invalidate()

    def get_rows_sizes(self, size, focus: bool = False):
        if len(size) != 2 or any(
            option[0] != urwid.WHSettings.WEIGHT
            for _widget, option in self.contents
        ):
            return super().get_rows_sizes(size, focus)

        maxcol, maxrow = size
        allocation_rows = maxrow + self._bottom_row_debt
        weights = [float(option[1]) for _widget, option in self.contents]
        remaining = max(0, allocation_rows)
        remaining_weight = sum(weights)
        heights: list[int] = []
        for weight in weights:
            if remaining <= 0 or remaining_weight <= 0:
                rows = 0
            else:
                rows = int(remaining * weight / remaining_weight + 0.5)
            heights.append(rows)
            remaining -= rows
            remaining_weight -= weight
        # Compute the normal allocation at the pre-expansion height, then pay
        # the footer's extra rows from Running (the bottom section). Only
        # pathological terminals where Running is already empty fall back to
        # the preceding section so the returned heights still fit maxrow.
        debt = self._bottom_row_debt
        for index in range(len(heights) - 1, -1, -1):
            take = min(debt, heights[index])
            heights[index] -= take
            debt -= take
            if debt == 0:
                break
        widths = (maxcol,) * len(heights)
        sizes = tuple((maxcol, rows) for rows in heights)
        return widths, tuple(heights), sizes


class UnifiedSidebarFrame(urwid.WidgetWrap):
    """Flat horizontal chrome with pointer-local wheel routing."""

    def __init__(
        self,
        sidebar: urwid.Pile,
        panes: Sequence[ScrollableSidebarPane],
    ) -> None:
        if len(panes) != 3:
            raise ValueError("the unified sidebar requires exactly three panes")
        self.sidebar = sidebar
        self.panes = tuple(panes)
        self._bottom = urwid.AttrMap(urwid.Divider("─"), "pane")
        self._layout = urwid.Pile([
            ("weight", 1, sidebar),
            ("pack", self._bottom),
        ])
        self._layout.focus_position = 0
        super().__init__(self._layout)

    def render(self, size, focus: bool = False):
        selected = self.sidebar.focus_position if focus else None
        sections = [widget for widget, _options in self.sidebar.contents]
        for index, section in enumerate(sections):
            if isinstance(section, SidebarSection):
                section.set_previous_section_focused(
                    selected is not None and selected == index - 1)
        bottom_attr = (
            "pane_focus"
            if selected is not None and selected == len(sections) - 1
            else "pane"
        )
        self._bottom.set_attr_map({None: bottom_attr})
        return super().render(size, focus)

    def mouse_event(self, size, event, button, col, row, focus):
        if event == "mouse press" and button in (4, 5) and len(size) >= 2:
            maxcol, maxrow = size[:2]
            inner_cols = maxcol
            inner_rows = maxrow - 1
            if inner_cols <= 0 or inner_rows <= 0:
                return True

            rows = self.sidebar.get_item_rows((inner_cols, inner_rows), focus)
            if not rows:
                return True
            if row >= maxrow - 1:
                section = 2
            else:
                boundary = 0
                section = len(rows) - 1
                for index, section_rows in enumerate(rows):
                    boundary += section_rows
                    if row < boundary:
                        section = index
                        break

            section_rows = rows[section]
            # Every section spends its first allocated row on its title rule.
            pane_rows = section_rows - 1
            if pane_rows <= 0:
                return True
            pane = self.panes[section]
            pane.mouse_event(
                (inner_cols, pane_rows), event, button,
                min(max(col, 0), inner_cols - 1), 0,
                focus and self.sidebar.focus_position == section,
            )
            return True
        return super().mouse_event(size, event, button, col, row, focus)
