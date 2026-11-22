"""Offline release highlights for the first interactive launch after an upgrade."""
from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version

from railmux import __version__, restart_state
from railmux.atomic_file import atomic_write_text
from railmux.config import default_config_path


_HIGHLIGHTS = {
    "0.5.1": (
        "Delete Devin sessions from Sessions or Running with d or right-click Delete.",
        "Running Devin sessions show live busy, blocked, and idle status dots in the sidebar.",
        "Devin local scrollback on managed displays strips leaked prompt boxes and duplicated streaming frames.",
    ),
    "0.5.0": (
        "Press a in Running or click its scope label to see all running agents.",
        "Press g or choose More → Search to find sessions across CLIs.",
        "In a split, the filled half of the layout icon shows where sidebar actions open.",
    ),
    "0.4.2": (
        "Devin is available in preview on POSIX and WSL. Sign in to its CLI "
        "on the agent host, then choose Devin from the Mode picker (m).",
        "On SSH and managed Windows displays, Options → Codex history → Local "
        "preserves wrapped links and keeps the latest user prompt pinned during scrollback.",
    ),
}


def recent_highlights(version: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Use product versions for the summary, including on their RC builds."""
    current = Version(Version(version).base_version)
    included = sorted(
        (release for release in _HIGHLIGHTS if Version(release) <= current),
        key=Version,
        reverse=True,
    )
    return tuple((release, _HIGHLIGHTS[release]) for release in included[:2])


def _recorded_version(path: Path) -> Version | None:
    try:
        with path.open(encoding="ascii") as stream:
            saved = stream.read(65).strip()
    except (FileNotFoundError, UnicodeError):
        return None
    if len(saved) > 64:
        return None
    try:
        return Version(saved)
    except InvalidVersion:
        return None


def _has_older_use(current: Version, *, existing_workspace: bool) -> bool:
    if (existing_workspace or restart_state.portable_state_path().is_file()
            or restart_state.legacy_state_path().is_file()):
        return True
    from railmux.ssh_display_diagnostics import read_diagnostic

    previous = read_diagnostic().client_version
    return previous is not None and Version(previous) < current


def _show_notes(version: str, sections: tuple[tuple[str, tuple[str, ...]], ...]) -> bool:
    import urwid

    from railmux.ui.modals import LaunchNotesModal

    continued = False

    def continue_launch() -> None:
        nonlocal continued
        continued = True
        raise urwid.ExitMainLoop()

    dialog = LaunchNotesModal(version, sections, on_continue=continue_launch)
    screen = urwid.Overlay(
        dialog, urwid.SolidFill(), align="center", valign="middle",
        width=("relative", 94), height=("relative", 84),
    )
    urwid.MainLoop(screen, palette=[
        ("title", "white,bold", "default"),
        ("modal_key", "yellow,bold", "default"),
    ]).run()
    return continued


def maybe_show_launch_notes(*, existing_workspace: bool = False) -> None:
    """Acknowledge an upgrade once for the local launcher, without network I/O."""
    if (not sys.stdin.isatty() or not sys.stdout.isatty()
            or os.environ.get("TERM") in (None, "", "dumb")):
        return
    path = default_config_path().with_name("launch-version")
    current = Version(__version__)
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
        if size.columns < 32 or size.lines < 10:
            return
        previous = _recorded_version(path)
        if previous is not None and previous >= current:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a") as lock:
            # A simultaneous launcher should attach normally while the first
            # user is reading. Exiting the process releases the lock too.
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            previous = _recorded_version(path)
            if previous is not None and previous >= current:
                return
            upgrading = previous is not None or _has_older_use(
                current, existing_workspace=existing_workspace,
            )
            sections = recent_highlights(__version__)
            if upgrading and sections and not _show_notes(__version__, sections):
                return
            atomic_write_text(path, f"{current}\n", encoding="ascii")
    except OSError:
        # This state is optional. An unavailable terminal or state directory
        # should leave the ordinary workspace launch available.
        return
