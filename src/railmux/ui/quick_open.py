"""Search a bounded snapshot of running, starred, and recent sessions."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import urwid

from railmux.fuzzy import fuzzy_match
from railmux.models import Project, SessionMeta
from railmux.session_cache import SessionCache


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
        self._edit = urwid.Edit("Search: ")
        self._scope = urwid.Button("", on_press=lambda _button: self._advance())
        self._rows = urwid.SimpleFocusListWalker([])
        self._list = urwid.ListBox(self._rows)
        header = urwid.Pile([
            urwid.Text(("title", f"Saved sessions open in {target_label}")),
            urwid.Columns([self._edit, (12, urwid.AttrMap(self._scope, "body", "focus"))]),
        ])
        header.focus_position = 1
        footer = urwid.Text("↑↓ Move  Enter Open  Tab View  Esc Close", align="center")
        self._frame = urwid.Frame(self._list, header=header, footer=footer, focus_part="header")
        super().__init__(urwid.LineBox(self._frame, title="Quick Open"))
        urwid.connect_signal(self._edit, "change", lambda _edit, text: self._rebuild(text))
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
        terms = query.split()
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
            urwid.AttrMap(
                urwid.Button(choice.label, on_press=lambda _button, c=choice: self._open(c)),
                "body", "focus",
            )
            for choice in choices
        ] or [urwid.Text(("dim", "Loading recent sessions…" if self._loading else "No matching sessions."))]
        if previous is not None:
            for index, choice in enumerate(choices):
                if previous.identity == choice.identity:
                    self._rows.set_focus(index)
                    break

    def _selected(self) -> SessionChoice | None:
        shown = getattr(self, "_shown", [])
        return shown[self._rows.focus] if shown else None

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
            self._list.keypress((max(1, width - 2), max(1, height - 5)), key)
        elif key == "ctrl u":
            self._edit.set_edit_text("")
        else:
            return super().keypress(size, key)
        return None
