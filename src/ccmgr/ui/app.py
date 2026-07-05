"""Top-level urwid app: sidebar + status bar.

ccmgr runs in the left pane of a tmux window. The right pane hosts the
currently-selected claude session. Switching sessions in ccmgr respawns
the right pane with a new claude --resume. Press `i` for a session-info
popup.
"""
from __future__ import annotations

from pathlib import Path

import urwid

from ccmgr import tmux_ctl
from ccmgr.config import Config
from ccmgr.discovery import list_projects
from ccmgr.favorites import Favorites
from ccmgr.launcher import build_resume_command, build_new_session_command
from ccmgr.models import Project, SessionMeta
from ccmgr.session_cache import SessionCache
from ccmgr.ui.modals import (
    DeleteConfirmModal,
    HelpModal,
    NewProjectModal,
    PathBrowserModal,
    ProjectInfoModal,
    QuitConfirmModal,
    RenameModal,
    RunningInfoModal,
    SessionInfoModal,
)
from ccmgr.ui.projects_pane import ProjectsPane
from ccmgr.ui.running_pane import RunningEntry, RunningSessionsPane
from ccmgr.ui.sessions_pane import SessionsPane
from ccmgr.ui.statusbar import HelpBar, StatusBar


PALETTE = [
    ("statusbar", "yellow,bold", "default"),
    # Focus highlight: bold black on brown (amber) — warm, much easier on the
    # eyes than the previous bright-white "light gray" background.
    ("focus", "black,bold", "brown"),
    # Persistent "currently-active project" highlight, shown even when focus
    # is elsewhere. Cool tone so it doesn't compete with the warm focus color.
    ("selected", "black,bold", "dark cyan"),
    ("title", "white,bold", ""),
    ("dim", "dark gray", ""),
    ("live", "light green,bold", ""),
    ("live_tag", "yellow,bold", ""),
    # Pane border. Dim by default; bright cyan + bold when the pane is focused
    # so it's obvious which pane Tab/Shift-Tab landed on.
    ("pane", "dark gray", ""),
    ("pane_focus", "light cyan,bold", ""),
]


class App:
    def __init__(self, claude_home: Path, config: Config, auto_launched: bool = False) -> None:
        self._claude_home = claude_home
        self._config = config
        self._auto_launched = auto_launched
        self._status = StatusBar()
        self._selected_project: Project | None = None
        self._session_cache = SessionCache()
        self._favorites = Favorites()
        # session_id (or "__new__-N" placeholder) -> detached tmux session name
        self._claude_tmux_sessions: dict[str, str] = {}
        # detached tmux session name -> human-readable label for the Running pane
        self._running_labels: dict[str, str] = {}
        # detached tmux session name -> the Project that session belongs to.
        # Used by _on_running_select to re-sync the Projects/Sessions panes
        # when the user jumps between running sessions of different projects.
        self._running_projects: dict[str, Project] = {}
        self._new_session_counter: int = 0
        # placeholder_key -> (project_real_path, created_at_epoch). Used by
        # _refresh to promote `__new__-N` placeholders to the real session_id
        # (and its display title) once claude creates the jsonl on disk.
        self._placeholders: dict[str, tuple[Path, float]] = {}
        # The right pane in ccmgr's window; runs `tmux attach -t <claude_session>`.
        self._right_pane_id: str | None = None
        self._loop: urwid.MainLoop | None = None
        self._last_screen_size: tuple[int, int] | None = None

        projects = list_projects(claude_home)
        self._projects_pane = ProjectsPane(projects, on_select=self._on_project_select)
        self._sessions_pane = SessionsPane(
            on_select=self._on_session_select,
            live_threshold=float(config.live_badge_seconds),
        )
        self._running_pane = RunningSessionsPane(on_select=self._on_running_select)

        # Wrap each pane in AttrMap so its LineBox border highlights when
        # focused. The `pane`/`pane_focus` palette entries color only cells
        # with no explicit attribute (the border chars) — inner rows have
        # their own AttrMaps and are unaffected.
        self._sidebar = urwid.Pile([
            ("weight", 2, urwid.AttrMap(self._projects_pane, "pane", focus_map="pane_focus")),
            ("weight", 3, urwid.AttrMap(self._sessions_pane, "pane", focus_map="pane_focus")),
            ("weight", 1, urwid.AttrMap(self._running_pane, "pane", focus_map="pane_focus")),
        ])
        self._help_bar = HelpBar()
        footer = urwid.Pile([
            ("pack", self._help_bar),
            ("pack", self._status),
        ])
        self._frame = urwid.Frame(body=self._sidebar, footer=footer)
        # Auto-select the most recent project on startup.
        if projects:
            self._on_project_select(projects[0])

    # --- project / session selection callbacks ---

    def _on_project_select(self, project: Project | None) -> None:
        if project is None:
            self._open_new_project_modal()
            return
        self._selected_project = project
        self._projects_pane.set_selected(project.encoded_name)
        sessions = self._session_cache.list_sessions(project)
        self._sessions_pane.set_sessions(project, sessions, running_ids=set(self._claude_tmux_sessions.keys()),
                favorite_ids=self._favorites.get_ids())
        self._status.set_message(f"Project: {project.real_path}  ({len(sessions)} sessions)")
        # Auto-focus the sessions pane below so the user can j/k into a session
        # without pressing Tab. Only do this when triggered by an actual Enter
        # press, not from the initial-project auto-select during App.__init__.
        if self._loop is not None:
            self._sidebar.focus_position = 1

    def _on_session_select(self, session: SessionMeta | None) -> None:
        if session is None:
            self._launch_new_session()
            return
        self._launch_resume(session)

    def _on_running_select(self, entry: RunningEntry) -> None:
        # Re-attach the right pane to this already-running claude session AND
        # sync the Projects/Sessions panes to that session's project, so the
        # sidebar reflects what's actually showing on the right.
        ok = self._attach_in_right_pane(entry.tmux_name)
        if not ok:
            self._status.set_message("failed to re-attach")
            return
        project = self._running_projects.get(entry.tmux_name)
        if project is not None and (
            self._selected_project is None
            or self._selected_project.encoded_name != project.encoded_name
        ):
            self._selected_project = project
            self._projects_pane.set_selected(project.encoded_name)
            sessions = self._session_cache.list_sessions(project)
            self._sessions_pane.set_sessions(
                project,
                sessions,
                running_ids=set(self._claude_tmux_sessions.keys()),
                favorite_ids=self._favorites.get_ids(),
            )
        self._status.set_message(f"→ {entry.label}")

    # --- tmux integration (detached session per claude + attach in right pane) ---

    @staticmethod
    def _safe_name(s: str, n: int = 12) -> str:
        out = "".join(c if c.isalnum() else "-" for c in s)
        return (out.strip("-") or "x")[:n]

    def _claude_session_name(self, key: str) -> str:
        """Stable tmux session name for a given claude session key."""
        return f"cc-{self._safe_name(key, 16)}"

    def _ensure_detached_claude(self, name: str, shell_cmd: str) -> bool:
        """Create the detached tmux session running claude, if it doesn't already exist."""
        if tmux_ctl.session_exists(name):
            return True
        return tmux_ctl.new_detached_session(name, shell_cmd)

    def _attach_in_right_pane(self, claude_tmux_name: str) -> bool:
        """Make the right pane display the named claude tmux session.

        Either creates the right-pane split (first time) or respawns the existing
        right pane to attach to the new claude session. Either way the previous
        claude tmux session stays alive, detached.

        TMUX= prefix clears the env var so the nested `tmux attach` works; tmux
        otherwise refuses to attach from within another tmux session.
        """
        import shlex
        attach_cmd = f"TMUX= exec tmux attach-session -t {shlex.quote(claude_tmux_name)}"
        if self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id):
            ok = tmux_ctl.respawn_pane(self._right_pane_id, attach_cmd)
        else:
            # ccmgr (left) at 30%, claude (right, the new pane) at 70%.
            new_id = tmux_ctl.split_window_h(attach_cmd, size_percent=70)
            if not new_id:
                return False
            self._right_pane_id = new_id
            # tmux only draws pane borders once there are 2+ panes. We just
            # created the second one, so now is the moment to apply the
            # active/inactive border palette — matches the bright-cyan focus
            # highlight ccmgr uses on its own urwid panes.
            tmux_ctl.set_window_option("pane-border-style", "fg=colour240")
            tmux_ctl.set_window_option("pane-active-border-style", "fg=cyan,bold")
            ok = True
        if ok and self._right_pane_id:
            tmux_ctl.select_pane(self._right_pane_id)
        return ok

    def _launch_resume(self, session_meta: SessionMeta) -> None:
        sid = session_meta.session_id
        claude_tmux_name = self._claude_tmux_sessions.get(sid) or self._claude_session_name(sid)
        cmd = build_resume_command(
            claude_binary=self._config.claude_binary,
            session_id=sid,
            cwd=session_meta.project.real_path,
        )
        shell_cmd = self._shellify(cmd, cwd=session_meta.project.real_path)
        if not self._ensure_detached_claude(claude_tmux_name, shell_cmd):
            self._status.set_message("failed to create detached claude session")
            return
        self._claude_tmux_sessions[sid] = claude_tmux_name
        self._running_labels[claude_tmux_name] = f"{session_meta.project.display_name}/{session_meta.display_title}"
        self._running_projects[claude_tmux_name] = session_meta.project
        if not self._attach_in_right_pane(claude_tmux_name):
            self._status.set_message("failed to attach to claude session")
            return
        self._status.set_message(f"→ {session_meta.display_title}  ({len(self._claude_tmux_sessions)} session(s) running)")

    def _launch_new_session(self) -> None:
        if self._selected_project is None:
            self._status.set_message("Pick a project first.")
            return
        self._new_session_counter += 1
        placeholder = f"__new__-{self._new_session_counter}"
        claude_tmux_name = self._claude_session_name(placeholder)
        cmd = build_new_session_command(
            claude_binary=self._config.claude_binary,
            cwd=self._selected_project.real_path,
        )
        shell_cmd = self._shellify(cmd, cwd=self._selected_project.real_path)
        if not self._ensure_detached_claude(claude_tmux_name, shell_cmd):
            self._status.set_message("failed to create detached session")
            return
        self._claude_tmux_sessions[placeholder] = claude_tmux_name
        self._running_labels[claude_tmux_name] = f"{self._selected_project.display_name}/(new)"
        self._running_projects[claude_tmux_name] = self._selected_project
        import time
        self._placeholders[placeholder] = (self._selected_project.real_path, time.time())
        if not self._attach_in_right_pane(claude_tmux_name):
            self._status.set_message("failed to attach")
            return
        self._status.set_message(f"→ new session in {self._selected_project.display_name}")

    def _on_new_project_submit(self, path: Path) -> None:
        self._close_modal()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._status.set_message(str(e))
            return
        self._new_session_counter += 1
        placeholder = f"__new__-{self._new_session_counter}"
        claude_tmux_name = self._claude_session_name(placeholder)
        cmd = [self._config.claude_binary]
        shell_cmd = self._shellify(cmd, cwd=path)
        if not self._ensure_detached_claude(claude_tmux_name, shell_cmd):
            self._status.set_message("failed to create detached session")
            return
        self._claude_tmux_sessions[placeholder] = claude_tmux_name
        self._running_labels[claude_tmux_name] = f"{path.name}/(new)"
        import time
        self._placeholders[placeholder] = (path, time.time())
        if not self._attach_in_right_pane(claude_tmux_name):
            self._status.set_message("failed to attach")
            return
        self._status.set_message(f"→ new project: {path}")

    @staticmethod
    def _shellify(argv: list[str], cwd: Path) -> str:
        import shlex
        quoted = " ".join(shlex.quote(a) for a in argv)
        return f"cd {shlex.quote(str(cwd))} && exec {quoted}"

    # --- modals ---

    def _open_new_project_modal(self) -> None:
        modal = PathBrowserModal(
            start_path=Path.home(),
            on_submit=self._on_new_project_submit,
            on_cancel=self._close_modal,
        )
        self._show_overlay(modal, width=54, height=60)

    def _open_info_modal(self) -> None:
        """Show info for whichever pane has focus: Project, Sessions, or Running."""
        if self._sidebar.focus_position == 0:
            proj = self._projects_pane.focused_project()
            modal = ProjectInfoModal(project=proj, on_close=self._close_modal)
            self._show_overlay(modal, width=60, height=40)
            return
        if self._sidebar.focus_position == 2:
            # Running pane — show info from the focused running entry.
            from ccmgr.ui.running_pane import _RunningRow
            running_walker = self._running_pane._walker
            if running_walker:
                focus_w, _ = running_walker.get_focus()
                if isinstance(focus_w, _RunningRow):
                    entry = focus_w.entry
                    # Find session metadata if we know it (i.e. not a placeholder).
                    sid: str | None = None
                    for s_id, tmux_name in self._claude_tmux_sessions.items():
                        if tmux_name == entry.tmux_name:
                            sid = s_id
                            break
                    session = self._find_session_meta(sid) if sid else None
                    project = self._running_projects.get(entry.tmux_name)
                    label = self._running_labels.get(entry.tmux_name, entry.tmux_name)
                    is_placeholder = sid is not None and sid.startswith("__new__-")
                    modal = RunningInfoModal(
                        label=label,
                        tmux_name=entry.tmux_name,
                        project=project,
                        session=session,
                        is_placeholder=is_placeholder,
                        on_close=self._close_modal,
                    )
                    self._show_overlay(modal, width=60, height=35)
            return
        session = self._currently_focused_session_meta()
        running_label = None
        if session is not None:
            tmux_name = self._claude_tmux_sessions.get(session.session_id)
            if tmux_name and tmux_ctl.session_exists(tmux_name):
                running_label = f"detached as '{tmux_name}'"
        modal = SessionInfoModal(session=session, running_label=running_label, on_close=self._close_modal)
        self._show_overlay(modal, width=60, height=40)

    def _open_help_modal(self) -> None:
        modal = HelpModal(on_close=self._close_modal)
        self._show_overlay(modal, width=70, height=80)

    def _open_quit_confirm(self) -> None:
        modal = QuitConfirmModal(
            on_confirm=self._confirm_quit,
            on_cancel=self._close_modal,
            running_count=len(self._claude_tmux_sessions),
        )
        self._show_overlay(modal, width=50, height=30)

    # --- project shortcuts: editor + terminal ---

    def _active_project(self) -> Project | None:
        """Project to act on for c/t shortcuts.

        Prefer the focused project in the Projects pane; fall back to the
        currently-selected (loaded-into-Sessions) project.
        """
        if self._sidebar.focus_position == 0:
            focused = self._projects_pane.focused_project()
            if focused is not None:
                return focused
        return self._selected_project

    def _open_editor_for_active_project(self) -> None:
        import shutil
        import subprocess
        proj = self._active_project()
        if proj is None:
            self._status.set_message("no project focused/selected")
            return
        if shutil.which("code") is None:
            self._status.set_message("'code' not found on PATH (install VS Code)")
            return
        try:
            subprocess.Popen(
                ["code", str(proj.real_path)],
                cwd=str(proj.real_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._status.set_message(f"opened VS Code: {proj.real_path}")
        except OSError as e:
            self._status.set_message(f"failed to open code: {e}")

    def _open_terminal_for_active_project(self) -> None:
        import os
        import shlex
        import subprocess as _sp
        proj = self._active_project()
        if proj is None:
            self._status.set_message("no project focused/selected")
            return
        shell = os.environ.get("SHELL", "/bin/bash")
        cmd = f"cd {shlex.quote(str(proj.real_path))} && exec {shlex.quote(shell)}"
        # Split visibly in the same window: if a right pane (claude) exists,
        # put the terminal below it; otherwise split off the current pane.
        target = self._right_pane_id if (self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id)) else None
        new_pane = tmux_ctl.split_window_v(cmd, target=target)
        if not new_pane:
            self._status.set_message("failed to split for terminal")
            return
        # Auto-close the pane when the shell exits (default, but be explicit).
        _sp.run(
            ["tmux", "set-option", "-p", "-t", new_pane, "remain-on-exit", "off"],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        tmux_ctl.select_pane(new_pane)
        self._status.set_message(f"terminal: {proj.display_name}  (Ctrl-B then arrow = move panes)")

    def _confirm_quit(self) -> None:
        self._close_modal()
        self._teardown_tmux()
        raise urwid.ExitMainLoop()

    def _show_overlay(self, modal: urwid.Widget, width: int, height: int) -> None:
        if self._loop is None:
            return
        overlay = urwid.Overlay(
            modal,
            self._frame,
            align="center", width=("relative", width),
            valign="middle", height=("relative", height),
        )
        self._loop.widget = overlay

    def _close_modal(self) -> None:
        if self._loop is not None:
            self._loop.widget = self._frame
        # Keep tmux focus on ccmgr's pane so the next keystroke doesn't
        # accidentally land in Claude (can happen after mouse clicks).
        pane_id = tmux_ctl.current_pane_id()
        if pane_id:
            tmux_ctl.select_pane(pane_id)

    # --- key handling ---

    def _on_input(self, key: str) -> None:
        # Up/down that bubble through unhandled → pane boundary reached.
        # Move focus to the adjacent pane.
        if key == "up" and self._sidebar.focus_position > 0:
            self._sidebar.focus_position = self._sidebar.focus_position - 1
            return
        if key == "down" and self._sidebar.focus_position < 2:
            self._sidebar.focus_position = self._sidebar.focus_position + 1
            return
        if key == "esc":
            # Esc navigates "up" the pane hierarchy:
            #   Running → Sessions → Projects
            if self._sidebar.focus_position == 2:
                self._sidebar.focus_position = 1
                return
            if self._sidebar.focus_position == 1:
                self._sidebar.focus_position = 0
                return
            return
        if key == "ctrl c":
            self._open_quit_confirm()
            return
        if key in ("q", "Q"):
            self._open_quit_confirm()
            return
        if key in ("tab", "shift tab"):
            self._rotate_focus(reverse=(key == "shift tab"))
            return
        if key == "/":
            # Running pane doesn't support filtering — skip.
            if self._sidebar.focus_position == 2:
                self._status.set_message("No filter on Running pane.")
                return
            self._enter_filter_mode()
            return
        if key in ("i", "I"):
            self._open_info_modal()
            return
        if key == "?":
            self._open_help_modal()
            return
        if key in ("c", "C"):
            self._open_editor_for_active_project()
            return
        if key in ("t", "T"):
            self._open_terminal_for_active_project()
            return
        if key in ("d", "D"):
            self._on_delete_session()
            return
        if key in ("r", "R"):
            self._on_rename_session()
            return
        if key in ("f", "F"):
            self._on_toggle_favorite()
            return
        if key in ("n", "N"):
            self._launch_new_session()
            return
        if key in ("[", "]"):
            self._resize_divider(key == "]")
            return

    def _rotate_focus(self, reverse: bool = False) -> None:
        """Tab / Shift-Tab cycle through the three ccmgr sidebar panes.

        Jumping in/out of the claude pane uses tmux's native nav (Ctrl-B ←/→)
        so Tab keeps its normal meaning inside claude (autocomplete).
        """
        n = len(self._sidebar.contents)
        if n <= 1:
            return
        cur = self._sidebar.focus_position
        self._sidebar.focus_position = (cur - 1) % n if reverse else (cur + 1) % n

    def _teardown_tmux(self) -> None:
        """Clean up on quit: kill right pane, every detached claude session, and our own session if we own it."""
        if self._right_pane_id:
            try:
                tmux_ctl.kill_pane(self._right_pane_id)
            except Exception:
                pass
            self._right_pane_id = None
        for _, name in list(self._claude_tmux_sessions.items()):
            try:
                tmux_ctl.kill_session(name)
            except Exception:
                pass
        self._claude_tmux_sessions.clear()
        if self._auto_launched:
            session_name = tmux_ctl.current_session_name()
            if session_name == "ccmgr":
                try:
                    tmux_ctl.kill_session("ccmgr")
                except Exception:
                    pass

    def _enter_filter_mode(self) -> None:
        # Swap just the status row (second item in the footer Pile) for an
        # Edit widget; the help-hint row above it stays visible.
        edit = urwid.Edit(caption="filter: ")
        footer_pile = self._frame.contents["footer"][0]
        footer_pile.contents[1] = (edit, footer_pile.options("pack"))
        footer_pile.focus_position = 1
        self._frame.focus_position = "footer"

        def on_change(widget, new_text):
            current_idx = self._sidebar.focus_position
            if current_idx == 0:
                self._projects_pane.set_filter(new_text)
            elif current_idx == 1:
                self._sessions_pane.set_filter(new_text)
            # No filter for the Running pane (small list, not worth filtering).

        urwid.connect_signal(edit, "change", on_change)

        def restore(key):
            if key in ("enter", "esc"):
                footer_pile.contents[1] = (self._status, footer_pile.options("pack"))
                self._frame.focus_position = "body"
                return None
            return key

        original_keypress = edit.keypress
        def new_keypress(size, key):
            handled = restore(key)
            if handled is None:
                return None
            return original_keypress(size, key)
        edit.keypress = new_keypress

    # --- periodic refresh ---

    def _refresh(self) -> None:
        # Re-apply pane border styles — they can be lost after terminal pane
        # splits open/close, causing visual artifacts (miscolored borders).
        if tmux_ctl.in_tmux() and tmux_ctl.current_pane_id():
            tmux_ctl.set_window_option("pane-border-style", "fg=colour240")
            tmux_ctl.set_window_option("pane-active-border-style", "fg=cyan,bold")
            # Kill dead panes left by exited terminal splits.
            tmux_ctl._kill_dead_panes()

        projects = list_projects(self._claude_home)
        self._projects_pane.set_projects(projects)
        # Prune dead claude tmux sessions (e.g. claude exited via /quit).
        for sid in list(self._claude_tmux_sessions.keys()):
            if not tmux_ctl.session_exists(self._claude_tmux_sessions[sid]):
                tmux_name = self._claude_tmux_sessions.pop(sid)
                self._running_labels.pop(tmux_name, None)
                self._running_projects.pop(tmux_name, None)
                self._placeholders.pop(sid, None)

        # Promote any `__new__-N` placeholders to their real session_id +
        # display title once claude has written the jsonl on disk.
        self._resolve_placeholders(projects)
        running_ids = set(self._claude_tmux_sessions.keys())
        if self._selected_project is not None:
            matched = next((p for p in projects if p.encoded_name == self._selected_project.encoded_name), None)
            if matched is not None:
                self._selected_project = matched
                sessions = self._session_cache.list_sessions(matched)
                self._sessions_pane.set_sessions(matched, sessions, running_ids=running_ids,
                                                  favorite_ids=self._favorites.get_ids())
            else:
                self._selected_project = None
                self._sessions_pane.set_sessions(None, [], running_ids=running_ids,
                                                  favorite_ids=self._favorites.get_ids())

        # Populate the bottom "Running" pane.
        entries = [
            RunningEntry(tmux_name=name, label=self._running_labels.get(name, name))
            for name in self._claude_tmux_sessions.values()
        ]
        self._running_pane.set_running(entries)

        if self._claude_tmux_sessions:
            n = len(self._claude_tmux_sessions)
            self._status.set_message(f"● = running ({n}) · Enter on a row re-attaches it · Ctrl-B ← = ccmgr")

    def _resolve_placeholders(self, projects: list[Project]) -> None:
        """Replace any `__new__-N` placeholder key with the real session_id.

        For each live placeholder, look at its project's sessions and pick the
        newest one created after the placeholder timestamp whose session_id is
        not already claimed by another running tmux session. Update both the
        running-pane label and the _claude_tmux_sessions key.
        """
        if not self._placeholders:
            return
        # Index projects by real_path for cheap lookup. New-project flow may
        # create a project dir that didn't exist when ccmgr started, so we
        # rely on `projects` being a fresh list_projects() result.
        by_path = {p.real_path: p for p in projects}
        claimed = set(self._claude_tmux_sessions.keys())
        for placeholder, (real_path, created_at) in list(self._placeholders.items()):
            if placeholder not in self._claude_tmux_sessions:
                self._placeholders.pop(placeholder, None)
                continue
            project = by_path.get(real_path)
            if project is None:
                continue
            sessions = self._session_cache.list_sessions(project)
            # Newest session created since this placeholder was launched, not
            # already in use by another tmux session.
            candidate: SessionMeta | None = None
            for s in sessions:
                if s.session_id in claimed:
                    continue
                if s.last_mtime + 1.0 < created_at:
                    continue
                if candidate is None or s.last_mtime > candidate.last_mtime:
                    candidate = s
            if candidate is None:
                continue
            tmux_name = self._claude_tmux_sessions.pop(placeholder)
            self._claude_tmux_sessions[candidate.session_id] = tmux_name
            self._running_labels[tmux_name] = (
                f"{candidate.project.display_name}/{candidate.display_title}"
            )
            self._running_projects[tmux_name] = candidate.project
            self._placeholders.pop(placeholder, None)
            claimed.add(candidate.session_id)

    def _currently_focused_session_meta(self) -> SessionMeta | None:
        if not self._sessions_pane._walker:
            return None
        focus_w, _ = self._sessions_pane._walker.get_focus()
        from ccmgr.ui.sessions_pane import _SessionRow
        if isinstance(focus_w, _SessionRow):
            return focus_w.session
        return None

    def _find_session_meta(self, session_id: str, project: Project | None = None) -> SessionMeta | None:
        """Look up session metadata by ID, optionally scoped to a project."""
        if project is None:
            return None
        from ccmgr.session_index import _scan_session
        jsonl_path = project.claude_dir / f"{session_id}.jsonl"
        if not jsonl_path.exists():
            return None
        return _scan_session(project, jsonl_path)

    # --- delete session ---

    def _on_delete_session(self) -> None:
        """Delete the focused session from the current pane (with confirmation)."""
        pos = self._sidebar.focus_position

        if pos == 1:
            # Sessions pane — delete the focused session (JSONL + tmux).
            session = self._currently_focused_session_meta()
            if session is None:
                self._status.set_message("No session selected to delete.")
                return
            title = session.display_title
            detail = f"Permanently delete '{title}'?\n\nThis removes the session file from disk\nand kills its background tmux session."
            modal = DeleteConfirmModal(
                title=f"Delete '{title}'?",
                detail=detail,
                on_confirm=lambda: self._do_delete_session(session),
                on_cancel=self._close_modal,
            )
            self._show_overlay(modal, width=54, height=30)

        elif pos == 2:
            # Running pane — kill the detached tmux session.
            from ccmgr.ui.running_pane import _RunningRow
            running_walker = self._running_pane._walker
            if not running_walker:
                self._status.set_message("No running session selected.")
                return
            focus_w, _ = running_walker.get_focus()
            if not isinstance(focus_w, _RunningRow):
                self._status.set_message("No running session selected.")
                return
            entry = focus_w.entry
            label = self._running_labels.get(entry.tmux_name, entry.tmux_name)
            # Find the session_id so we can also delete the JSONL.
            session_id: str | None = None
            project: Project | None = None
            for s_id, tmux_name in self._claude_tmux_sessions.items():
                if tmux_name == entry.tmux_name:
                    session_id = s_id
                    project = self._running_projects.get(tmux_name)
                    break
            if session_id and not session_id.startswith("__new__-"):
                session_meta = self._find_session_meta(session_id, project)
                detail = (f"Kill '{label}'?\n\n"
                          "The detached tmux session will be killed.\n"
                          "The session file will be deleted from disk.")
            else:
                session_meta = None
                detail = f"Kill '{label}'?\n\nThe detached tmux session will be killed."
            modal = DeleteConfirmModal(
                title=f"Kill '{label}'?",
                detail=detail,
                on_confirm=lambda: self._do_kill_running(entry.tmux_name, session_id, project) if session_id and not session_id.startswith("__new__-") else self._do_kill_running(entry.tmux_name, None, None),
                on_cancel=self._close_modal,
            )
            self._show_overlay(modal, width=54, height=30)

        else:
            self._status.set_message("Use d on a session row or running-entry row to delete.")

    def _do_delete_session(self, session: SessionMeta) -> None:
        """Actually delete a session: remove JSONL + kill detached tmux."""
        self._close_modal()
        # Kill the detached tmux session, if running.
        tmux_name = self._claude_tmux_sessions.pop(session.session_id, None)
        if tmux_name is not None:
            self._running_labels.pop(tmux_name, None)
            self._running_projects.pop(tmux_name, None)
            self._placeholders.pop(session.session_id, None)
            if tmux_ctl.session_exists(tmux_name):
                tmux_ctl.kill_session(tmux_name)
        # Delete the JSONL file.
        try:
            session.jsonl_path.unlink(missing_ok=True)
        except OSError as e:
            self._status.set_message(f"Failed to delete session file: {e}")
            return
        self._status.set_message(f"Deleted: {session.display_title}")

    def _do_kill_running(self, tmux_name: str, session_id: str | None,
                         project: Project | None) -> None:
        """Kill a running detached tmux session, optionally deleting its JSONL."""
        self._close_modal()
        if tmux_ctl.session_exists(tmux_name):
            tmux_ctl.kill_session(tmux_name)
        # Remove from tracking.
        if session_id is not None:
            self._claude_tmux_sessions.pop(session_id, None)
            self._placeholders.pop(session_id, None)
        self._running_labels.pop(tmux_name, None)
        self._running_projects.pop(tmux_name, None)
        # Also clean any placeholder that maps to the same tmux_name.
        for sid in list(self._claude_tmux_sessions.keys()):
            if self._claude_tmux_sessions.get(sid) == tmux_name:
                self._claude_tmux_sessions.pop(sid, None)
                self._placeholders.pop(sid, None)
        # Delete the JSONL if we know the session.
        if session_id is not None and not session_id.startswith("__new__-") and project is not None:
            jsonl_path = project.claude_dir / f"{session_id}.jsonl"
            try:
                jsonl_path.unlink(missing_ok=True)
            except OSError:
                pass
        self._status.set_message(f"Killed: {tmux_name}")

    # --- rename session ---

    def _on_rename_session(self) -> None:
        """Open the rename modal for the focused session."""
        session = self._currently_focused_session_meta()
        if session is None:
            self._status.set_message("No session selected to rename.")
            return
        modal = RenameModal(
            current_title=session.display_title,
            on_submit=lambda new_title, s=session: self._do_rename(s, new_title),
            on_cancel=self._close_modal,
        )
        self._show_overlay(modal, width=50, height=22)

    def _do_rename(self, session: SessionMeta, new_title: str) -> None:
        """Write a new ai-title record to the session JSONL."""
        self._close_modal()
        import json
        record = json.dumps({"type": "ai-title", "aiTitle": new_title})
        try:
            with session.jsonl_path.open("a") as f:
                f.write(record + "\n")
        except OSError as e:
            self._status.set_message(f"Failed to rename: {e}")
            return
        # Invalidate the cache so subsequent renders pick up the new title.
        self._session_cache.invalidate()
        self._status.set_message(f"Renamed to: {new_title}")

    # --- toggle favorite ---

    def _on_toggle_favorite(self) -> None:
        """Toggle favorite status for the focused session."""
        session = self._currently_focused_session_meta()
        if session is None:
            self._status.set_message("No session selected.")
            return
        now_fav = self._favorites.toggle(session.session_id)
        label = "⭐" if now_fav else "unstarred"
        self._status.set_message(f"{label} {session.display_title}")

    # --- resize divider ---

    def _resize_divider(self, expand_ccmgr: bool) -> None:
        """Move the vertical divider: [ shrinks ccmgr, ] expands it."""
        if not self._right_pane_id or not tmux_ctl.pane_alive(self._right_pane_id):
            self._status.set_message("No claude pane to resize against.")
            return
        direction = "-R" if expand_ccmgr else "-L"
        tmux_ctl.resize_pane(self._right_pane_id, direction, 5)

    # --- periodic refresh ---

    def _on_tick(self, loop, _user_data) -> None:
        self._refresh()
        loop.set_alarm_in(self._config.poll_interval_ms / 1000.0, self._on_tick)

    # --- lifecycle ---

    def run(self) -> None:
        # Enable clipboard sync in the outer ccmgr session so mouse
        # selection in either pane is copied to the system clipboard.
        # Mouse mode is NOT enabled here — it would break right-click
        # in the Claude pane.  Inner Claude sessions have mouse on
        # for scroll/copy-mode (see tmux_ctl.new_detached_session).
        import subprocess as _sp
        if tmux_ctl.in_tmux():
            sess = tmux_ctl.current_session_name() or "ccmgr"
            _sp.run(
                ["tmux", "set-option", "-t", sess, "set-clipboard", "on"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )

        self._loop = urwid.MainLoop(self._frame, palette=PALETTE, unhandled_input=self._on_input)
        try:
            self._loop.screen.set_terminal_properties(colors=256)
        except Exception:
            pass
        # Intercept Ctrl-C as a regular keypress so we can show a confirm-quit
        # popup instead of slamming out via SIGINT. Ctrl-\ (quit) is left
        # active as an emergency hard-exit.
        try:
            self._loop.screen.tty_signal_keys(intr="undefined")
        except Exception:
            pass
        # Right pane is created lazily on first session launch — startup is
        # ccmgr-only, no empty pane.
        self._loop.set_alarm_in(self._config.poll_interval_ms / 1000.0, self._on_tick)
        try:
            self._loop.run()
        except KeyboardInterrupt:
            # Ctrl-C / SIGINT — fall through to teardown.
            pass
        finally:
            # Always clean up tmux, regardless of how we exited the loop.
            self._teardown_tmux()
