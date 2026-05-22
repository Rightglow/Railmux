"""Bottom sidebar pane: sessions in the currently-selected project."""
from __future__ import annotations

from collections.abc import Callable

import urwid

from ccmgr.models import Project, SessionMeta
from ccmgr.session_index import is_live


def _format_when(epoch: float) -> str:
    """Last-used timestamp, written exactly as `claude --resume` does:
    'just now', '5 minutes ago', '4 hours ago', '2 days ago', and a date for
    anything older than a week. Singular/plural picked correctly.
    """
    import time
    if epoch <= 0:
        return "—"
    delta = max(0.0, time.time() - epoch)
    if delta < 60:
        return "just now"
    if delta < 3600:
        n = int(delta // 60)
        return f"{n} minute{'s' if n != 1 else ''} ago"
    if delta < 86400:
        n = int(delta // 3600)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    if delta < 7 * 86400:
        n = int(delta // 86400)
        return f"{n} day{'s' if n != 1 else ''} ago"
    from datetime import datetime
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def _format_size(nbytes: int) -> str:
    """Human file size: 175907 -> '175.9KB', 1342177 -> '1.3MB'."""
    if nbytes >= 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.1f}MB"
    if nbytes >= 1024:
        return f"{nbytes / 1024:.1f}KB"
    return f"{nbytes}B"


class _SessionRow(urwid.WidgetWrap):
    def __init__(self, session: SessionMeta, live_threshold: float, is_running: bool = False) -> None:
        self.session = session
        is_active = is_live(session, live_threshold)

        # Two-line layout, matching `claude --resume`:
        #   Title
        #   <relative time> · <branch> · <size>
        title_markup: list = []
        title_markup.append("● " if is_running else "  ")
        title_markup.append(session.display_title)
        if is_active:
            title_markup.append(("live_tag", " [LIVE]"))
        title_text = urwid.Text(title_markup, wrap="clip")

        try:
            size_str = _format_size(session.jsonl_path.stat().st_size)
        except OSError:
            size_str = "—"
        parts = [_format_when(session.last_mtime)]
        if session.git_branch:
            parts.append(session.git_branch)
        parts.append(size_str)
        meta_text = urwid.Text(("dim", "  " + " · ".join(parts)), wrap="clip")

        body = urwid.Pile([title_text, meta_text])

        row_attr = "live" if is_running else None
        focus_remap = {None: "focus", "live": "focus", "dim": "focus", "live_tag": "focus"}
        super().__init__(urwid.AttrMap(body, row_attr, focus_map=focus_remap))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        return key


class _NewSessionRow(urwid.WidgetWrap):
    def __init__(self) -> None:
        self._text = urwid.Text("+ New session")
        super().__init__(urwid.AttrMap(self._text, "dim", focus_map="focus"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        return key


class SessionsPane(urwid.WidgetWrap):
    def __init__(self, on_select: Callable[[SessionMeta | None], None], live_threshold: float) -> None:
        self._on_select = on_select
        self._live_threshold = live_threshold
        self._sessions: list[SessionMeta] = []
        self._project: Project | None = None
        self._filter = ""
        self._running_ids: set[str] = set()

        self._new_row = _NewSessionRow()
        self._walker = urwid.SimpleFocusListWalker([urwid.Text("(no project selected)", align="center")])
        self._listbox = urwid.ListBox(self._walker)
        self._pile = urwid.Pile([
            ("pack", self._new_row),
            ("pack", urwid.Divider("─")),
            ("weight", 1, self._listbox),
        ])
        self._linebox = urwid.LineBox(self._pile, title="Sessions")
        super().__init__(self._linebox)

    def set_sessions(self, project: Project | None, sessions: list[SessionMeta], running_ids: set[str] | None = None) -> None:
        prior_focus = self._remember_focus()
        self._project = project
        self._sessions = sessions
        if running_ids is not None:
            self._running_ids = running_ids

        if project is None:
            self._walker[:] = [urwid.Text("(no project selected)", align="center")]
            self._linebox.set_title("Sessions")
            return

        # Apply current filter (if any) when rendering.
        if self._filter:
            filtered = [s for s in sessions if self._filter.lower() in s.display_title.lower()]
        else:
            filtered = sessions
        self._render(filtered)
        self._linebox.set_title(f"Sessions ({project.display_name})")

        self._restore_focus(prior_focus)

    def set_filter(self, needle: str) -> None:
        self._filter = needle
        if self._project is not None:
            filtered = [s for s in self._sessions if needle.lower() in s.display_title.lower()]
            self._render(filtered)

    def _render(self, sessions: list[SessionMeta]) -> None:
        rows = [
            _SessionRow(s, self._live_threshold, is_running=(s.session_id in self._running_ids))
            for s in sessions
        ]
        if not rows:
            rows = [urwid.Text("  (no matches)" if self._filter else "  (no sessions yet)", align="left")]
        self._walker[:] = rows

    def _remember_focus(self) -> str | None:
        if not self._walker:
            return None
        focus_w, _ = self._walker.get_focus()
        if isinstance(focus_w, _SessionRow):
            return focus_w.session.session_id
        return None

    def _restore_focus(self, session_id: str | None) -> None:
        if not self._walker:
            return
        if session_id is not None:
            for i, w in enumerate(self._walker):
                if isinstance(w, _SessionRow) and w.session.session_id == session_id:
                    self._walker.set_focus(i)
                    return
        for i, w in enumerate(self._walker):
            if isinstance(w, _SessionRow):
                self._walker.set_focus(i)
                return
        self._walker.set_focus(0)

    def keypress(self, size, key):
        if key == "enter":
            if self._pile.focus_position == 0:
                # On + New session row
                self._on_select(None)
                return None
            if self._walker:
                focus_w, _ = self._walker.get_focus()
                if isinstance(focus_w, _SessionRow):
                    self._on_select(focus_w.session)
                    return None
        # Boundary consume.
        if key == "up" and self._pile.focus_position == 0:
            return None
        if key == "down":
            if self._pile.focus_position == 2 and self._walker:
                cur = self._walker.focus
                last_selectable_idx = None
                for i, w in enumerate(self._walker):
                    if isinstance(w, _SessionRow):
                        last_selectable_idx = i
                if last_selectable_idx is not None and cur == last_selectable_idx:
                    return None
        return super().keypress(size, key)
