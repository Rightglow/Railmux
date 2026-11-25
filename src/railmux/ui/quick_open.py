"""Search a bounded snapshot of running, starred, and recent sessions."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import urwid

from railmux.fuzzy import fuzzy_match, fuzzy_match_positions
from railmux.models import Project, SessionMeta
from railmux.session_cache import SessionCache
from railmux.ui._widgets import ClickableRow
from railmux.ui.modals import _action_legend, _modal_box


def _provider_attr(provider: str) -> str:
    lower = provider.lower()
    if "devin" in lower:
        return "search_provider_devin"
    if "codex" in lower:
        return "search_provider_codex"
    if "claude" in lower:
        return "search_provider_claude"
    return "search_provider_other"


def _highlight_runs(
    text: str,
    terms: tuple[str, ...],
    base_attr: str,
    match_attr: str,
) -> list[tuple[str, str]]:
    if not text:
        return []
    hits = fuzzy_match_positions(terms, text)
    if not hits:
        return [(base_attr, text)]
    runs: list[tuple[str, str]] = []
    start = 0
    in_hit = 0 in hits
    for index in range(1, len(text)):
        cur = index in hits
        if cur != in_hit:
            runs.append((match_attr if in_hit else base_attr, text[start:index]))
            start = index
            in_hit = cur
    runs.append((match_attr if in_hit else base_attr, text[start:]))
    return runs


def _choice_markup(
    choice: SessionChoice,
    terms: tuple[str, ...],
    *,
    active: bool,
) -> list[tuple[str, str]]:
    marker = "›" if active else " "
    title = " ".join(choice.title.split())
    project = " ".join(choice.project.split())
    base = "focus" if active else "body"
    dim = "focus" if active else "dim"
    star_attr = "focus" if active else "star"
    provider_attr = "focus" if active else _provider_attr(choice.provider)
    state_attr = (
        "focus" if active
        else ("live" if choice.state.lower().startswith("running") else "dim")
    )
    workspace_attr = "focus" if active else "search_workspace"
    match_attr = "search_match_focus" if active else "search_match"

    parts: list[tuple[str, str]] = [(base, f"{marker} ")]
    if choice.starred:
        parts.append((star_attr, "★ "))
    parts.append((dim, "["))
    parts.extend(_highlight_runs(choice.provider, terms, provider_attr, match_attr))
    parts.append((dim, " · "))
    parts.extend(_highlight_runs(choice.state, terms, state_attr, match_attr))
    parts.append((dim, "] "))
    if project:
        parts.extend(_highlight_runs(project, terms, workspace_attr, match_attr))
        parts.append((dim, " / "))
    parts.extend(_highlight_runs(title, terms, base, match_attr))
    return parts


@dataclass(frozen=True)
class SessionChoice:
    identity: tuple[str, str, str]
    title: str
    provider: str
    project: str
    state: str
    on_open: Callable[[], None]
    starred: bool = False

    @property
    def label(self) -> str:
        title = " ".join(self.title.split())
        project = " ".join(self.project.split())
        star = "★ " if self.starred else ""
        location = f"{project} / " if project else ""
        return f"{star}[{self.provider} · {self.state}] {location}{title}"


def recent_claude_sessions(
    projects: Iterable[Project], favorites: set[str],
) -> tuple[SessionMeta, ...]:
    """Read in a worker with an independent cache and no rename writes."""
    cache = SessionCache()
    rows: dict[str, SessionMeta] = {}
    for position, project in enumerate(projects):
        if project.claude_dir == Path():
            continue
        if position < 16:
            for meta in cache.list_sessions(project, top_n=10):
                rows[meta.session_id] = meta
        if not favorites or favorites.issubset(rows):
            continue
        try:
            for path in project.claude_dir.iterdir():
                key = path.stem
                if path.suffix != ".jsonl" or key not in favorites or key in rows:
                    continue
                meta = cache.get(project, key)
                if meta is not None and not meta.background and not meta.continued_in_id:
                    rows[key] = meta
        except OSError:
            continue
    return tuple(rows.values())


class _ResultRow(ClickableRow):
    def __init__(
        self,
        choice: SessionChoice,
        terms: tuple[str, ...],
        on_click: Callable[[], None],
    ) -> None:
        self._choice = choice
        self._terms = terms
        self._text = urwid.Text(_choice_markup(choice, terms, active=False))
        self._style = urwid.AttrMap(self._text, "body")
        super().__init__(self._style, on_click=on_click)

    def set_active(self, active: bool) -> None:
        self._text.set_text(_choice_markup(self._choice, self._terms, active=active))
        self._style.set_attr_map({None: "focus" if active else "body"})


class QuickOpen(urwid.WidgetWrap):
    """Keep filtering in memory and activate only the chosen snapshot entry."""

    _VIEWS = ("All", "Running", "Starred")
    _MOVEMENT = {"up", "down", "page up", "page down", "home", "end"}

    def __init__(
        self,
        choices: Iterable[SessionChoice],
        on_close: Callable[[], None],
        target_label: str,
        *,
        loading: bool = False,
    ) -> None:
        self._on_close = on_close
        self._view = 0
        self._choices = tuple(choices)
        self._loading = loading
        self._edit = urwid.Edit(("title", "Search: "))
        self._scope = urwid.Button("", on_press=lambda _button: self._advance())
        self._rows = urwid.SimpleFocusListWalker([])
        self._list = urwid.ListBox(self._rows)
        self._shown: list[SessionChoice] = []
        self._active_row: _ResultRow | None = None
        header = urwid.Pile([
            urwid.Text(("dim", f"Saved sessions open in {target_label}")),
            urwid.Columns([
                self._edit,
                (12, urwid.AttrMap(self._scope, "scope_chip_all", "focus")),
            ]),
            urwid.AttrMap(urwid.Divider("─"), "pane"),
        ])
        header.focus_position = 1
        footer = urwid.Pile([
            urwid.AttrMap(urwid.Divider("─"), "pane"),
            _action_legend(
                [("↑↓", "Move"), ("↵", "Open"), ("Tab", "View"), ("Esc", "Close")],
                align="center",
                separator="  ",
                wrap="space",
            ),
        ])
        self._frame = urwid.AttrMap(
            urwid.Frame(self._list, header=header, footer=footer, focus_part="header"),
            "body",
        )
        super().__init__(_modal_box(self._frame, title="Search sessions", pad_x=0))
        urwid.connect_signal(self._edit, "change", lambda _edit, text: self._rebuild(text))
        urwid.connect_signal(self._rows, "modified", self._sync_selection)
        self._rebuild("")

    def set_choices(self, choices: Iterable[SessionChoice], *, loading: bool) -> None:
        self._choices = tuple(choices)
        self._loading = loading
        self._rebuild(self._edit.get_edit_text())

    def _advance(self) -> None:
        self._view = (self._view + 1) % len(self._VIEWS)
        self._rebuild(self._edit.get_edit_text())

    def _rebuild(self, query: str) -> None:
        previous = self._selected()
        self._scope.set_label(self._VIEWS[self._view])
        choices: list[SessionChoice] = []
        terms = tuple(query.split())
        for choice in self._choices:
            if self._view == 1 and choice.identity[0] != "running":
                continue
            if self._view == 2 and not choice.starred:
                continue
            fields = (choice.provider, choice.state, choice.project, choice.title)
            if all(any(fuzzy_match(term, field) for field in fields) for term in terms):
                choices.append(choice)
            if len(choices) == 80:
                break
        self._shown = choices
        self._rows[:] = [
            _ResultRow(choice, terms, partial(self._open, choice))
            for choice in choices
        ] or [urwid.Text(("dim", "Loading recent sessions…" if self._loading else "No matching sessions."))]
        if previous is not None:
            for index, choice in enumerate(choices):
                if previous.identity == choice.identity:
                    self._rows.set_focus(index)
                    return
        self._rows.set_focus(0)
        self._list.set_focus_valign("top")

    def _sync_selection(self) -> None:
        # The search field keeps keyboard focus while the walker selects results.
        row, _ = self._rows.get_focus()
        active_row = row if isinstance(row, _ResultRow) else None
        if active_row is self._active_row:
            return
        if self._active_row is not None:
            self._active_row.set_active(False)
        if active_row is not None:
            active_row.set_active(True)
        self._active_row = active_row

    def _selected(self) -> SessionChoice | None:
        return self._shown[self._rows.focus] if self._shown else None

    def _open(self, choice: SessionChoice) -> None:
        self._on_close()
        choice.on_open()

    def keypress(self, size, key):
        if key == "esc":
            self._on_close()
        elif key == "enter":
            choice = self._selected()
            if choice is not None:
                self._open(choice)
        elif key == "tab":
            self._advance()
        elif key in self._MOVEMENT:
            width, height = size
            self._list.keypress((max(1, width - 2), max(1, height - 7)), key)
        elif key == "ctrl u":
            self._edit.set_edit_text("")
        else:
            return super().keypress(size, key)
        return None
