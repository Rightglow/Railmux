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
        *,
        scope_toggle: tuple[Callable[[], str], Callable[[], None]] | None = None,
        collapse_toggle: tuple[Callable[[], bool], Callable[[], None]] | None = None,
    ) -> None:
        self.pane = pane
        self._title = title
        self._count = count
        self._scope_toggle = scope_toggle
        self._collapse_toggle = collapse_toggle
        self._scope_hit: tuple[int, int, int] | None = None
        self._collapse_hit: tuple[int, int, int] | None = None
        self._collapsed = False
        self._previous_section_focused = False
        self._header = urwid.Text("")
        self._pile = urwid.Pile([
            ("pack", self._header),
            ("weight", 1, pane),
        ])
        self._pile.focus_position = 1
        super().__init__(self._pile)

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        collapsed = bool(collapsed)
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        if collapsed:
            self._pile.contents[:] = [
                (self._header, self._pile.options("pack")),
            ]
            self._pile.focus_position = 0
        else:
            self._pile.contents[:] = [
                (self._header, self._pile.options("pack")),
                (self.pane, self._pile.options("weight", 1)),
            ]
            self._pile.focus_position = 1
        self._invalidate()

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
        stable = stable.upper()
        count = self._count() if self._count is not None else ""
        count = str(count).strip()

        is_collapsed = (
            self._collapse_toggle[0]()
            if self._collapse_toggle is not None else self._collapsed
        )
        collapse_button = (
            ("▸" if is_collapsed else "▾")
            if self._collapse_toggle is not None else ""
        )
        prefix = "─ "
        count_segment = f" {count} " if count else ""
        collapse_segment = (
            f"{collapse_button} " if count_segment else f" {collapse_button} "
        ) if collapse_button else ""
        end = "─"
        fixed = prefix + " " + count_segment + collapse_segment + end
        fixed_width = urwid.calc_width(fixed, 0, len(fixed))
        title_width = maxcol - fixed_width
        title_attr = "pane_focus" if focus else "pane"
        rule_attr = "pane_focus" if focus or self._previous_section_focused else "pane"
        title = [(title_attr, _truncate_title(stable + separator + detail, title_width))]

        self._scope_hit = None
        if self._scope_toggle is not None:
            label = f"[{self._scope_toggle[0]()}]"
            leading = stable + " "
            leading_width = urwid.calc_width(leading, 0, len(leading))
            label_width = urwid.calc_width(label, 0, len(label))
            # Keep the clickable label whole when trimming trailing detail.
            remaining = title_width - leading_width - label_width
            if remaining >= 0:
                title = [(title_attr, leading), ("btn_label", label)]
                if detail and remaining > 1:
                    title.append((title_attr, " " + _truncate_title(detail, remaining - 1)))
                start = urwid.calc_width(prefix, 0, len(prefix)) + leading_width
                self._scope_hit = (maxcol, start, start + label_width)

        used = fixed_width + sum(urwid.calc_width(text, 0, len(text)) for _, text in title)
        middle = " " + "─" * max(0, maxcol - used)
        self._collapse_hit = None
        header_parts: list = [
            (rule_attr, prefix), *title, (rule_attr, middle),
            (title_attr, count_segment),
        ]
        if collapse_button:
            btn_start = (
                urwid.calc_width(prefix, 0, len(prefix))
                + sum(urwid.calc_width(text, 0, len(text)) for _, text in title)
                + urwid.calc_width(middle, 0, len(middle))
                + urwid.calc_width(count_segment, 0, len(count_segment))
            )
            btn_width = urwid.calc_width(collapse_segment, 0, len(collapse_segment))
            self._collapse_hit = (maxcol, max(0, btn_start - 1), btn_start + btn_width)
            header_parts.append(("btn_label", collapse_segment))
        header_parts.append((rule_attr, end))
        self._header.set_text(header_parts)
        return super().render(size, focus)

    def mouse_event(self, size, event, button, col, row, focus):
        if row == 0 and event == "mouse press" and button == 1:
            if self._scope_toggle is not None and self._scope_hit is not None:
                width, start, end = self._scope_hit
                if size[0] == width and start <= col < end:
                    self._scope_toggle[1]()
                    self._invalidate()
                    return True
            if self._collapse_toggle is not None and self._collapse_hit is not None:
                width, start, end = self._collapse_hit
                if size[0] == width and start <= col < end:
                    self._collapse_toggle[1]()
                    self._invalidate()
                    return True
        if self._collapsed:
            return True
        return super().mouse_event(size, event, button, col, row, focus)


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
            option[0] not in (urwid.WHSettings.WEIGHT, urwid.WHSettings.PACK)
            for _widget, option in self.contents
        ):
            return super().get_rows_sizes(size, focus)

        maxcol, maxrow = size
        fixed_rows: dict[int, int] = {}
        weights: list[tuple[int, float]] = []
        for idx, (widget, option) in enumerate(self.contents):
            if option[0] == urwid.WHSettings.PACK:
                fixed_rows[idx] = max(1, widget.rows((maxcol,), focus=False))
            else:
                weights.append((idx, float(option[1])))
        if not weights:
            return super().get_rows_sizes(size, focus)

        fixed_total = sum(fixed_rows.values())
        allocation_rows = max(0, maxrow - fixed_total + self._bottom_row_debt)
        remaining = allocation_rows
        remaining_weight = sum(weight for _idx, weight in weights)
        heights = [0] * len(self.contents)
        for idx, rows in fixed_rows.items():
            heights[idx] = rows
        for idx, weight in weights:
            if remaining <= 0 or remaining_weight <= 0:
                rows = 0
            else:
                rows = int(remaining * weight / remaining_weight + 0.5)
            heights[idx] = rows
            remaining -= rows
            remaining_weight -= weight
        # Pay the footer's extra rows from the lowest expanded section first.
        debt = self._bottom_row_debt
        for idx, _weight in reversed(weights):
            take = min(debt, heights[idx])
            heights[idx] -= take
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
