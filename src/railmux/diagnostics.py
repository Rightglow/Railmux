"""Privacy-safe, non-interactive environment diagnostics."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TextIO

from railmux import __version__
from railmux import legacy_sessions, tmux_health, tmux_server
from railmux.config import Config, ConfigError, default_config_path, load_config


_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+){1,3}(?:[A-Za-z]|[-+][0-9A-Za-z.-]+)?)"
)


def is_ssh_session(environ: dict[str, str] | None = None) -> bool:
    """Return whether common OpenSSH transport markers are present."""
    env = os.environ if environ is None else environ
    return any(env.get(name) for name in (
        "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _display_path(path: Path) -> str:
    """Show home-relative paths, but never reveal an unrelated custom path."""
    try:
        path = path.expanduser().absolute()
        home = Path.home().absolute()
        relative = path.relative_to(home)
    except (OSError, RuntimeError, ValueError):
        return "<custom>"
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def _version(binary: str, *version_args: str) -> str:
    """Return only a numeric version token from a configured executable."""
    try:
        found = shutil.which(binary)
    except (OSError, TypeError):
        found = None
    if found is None:
        return "not found"
    try:
        result = subprocess.run(
            [binary, *(version_args or ("--version",))],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "available (version timed out)"
    except OSError:
        return "available (version unavailable)"
    text = f"{result.stdout}\n{result.stderr}"
    match = _VERSION_RE.search(text)
    return match.group(1) if match else "available (version unavailable)"


def _directory_status(path: Path) -> str:
    try:
        exists = path.is_dir()
        readable = exists and os.access(path, os.R_OK)
        writable = exists and os.access(path, os.W_OK)
    except OSError:
        exists = readable = writable = False
    return (
        f"{_display_path(path)}; exists={_yes_no(exists)}, "
        f"readable={_yes_no(readable)}, writable={_yes_no(writable)}"
    )


def _terminal_capabilities(environ: dict[str, str]) -> str:
    term = environ.get("TERM", "").lower()
    colorterm = environ.get("COLORTERM", "").lower()
    colours_256 = "256color" in term
    truecolour = colorterm in {"truecolor", "24bit"}
    return f"256-colour={_yes_no(colours_256)}, true-colour={_yes_no(truecolour)}"


def _dedicated_tmux_status() -> str:
    """Return a bounded health result without exposing the socket pathname."""
    if shutil.which("tmux") is None:
        return "unavailable (tmux not found)"
    try:
        target = tmux_server.discover_target(timeout=1.0)
    except tmux_server.TmuxServerUnresponsive:
        return "unresponsive (watchdog will not kill or restart it)"
    except tmux_server.TmuxServerError:
        return "configuration error"
    if target is None:
        return "not running"
    context = (
        "current process is inside it"
        if tmux_server.is_current_server(target)
        else "current process is outside it"
    )
    return f"healthy ({context})"


def _legacy_tmux_status() -> str:
    """Report only a bounded count; never expose session names or paths."""
    target, sessions, complete = legacy_sessions.discover(timeout=1.0)
    if not complete:
        return "unavailable (inventory timed out or changed)"
    if target is None:
        return "not running"
    count = sum(
        session.name.startswith(("cc-", "cx-")) for session in sessions
    )
    if count:
        return f"healthy ({count} Railmux candidate(s); restart recommended)"
    return "healthy (no Railmux candidates)"


def _last_tmux_incident() -> str:
    incident = tmux_health.read_last_incident()
    if incident is None:
        return "none recorded"
    descriptions = {
        "launcher-watchdog-timeout": "local client watchdog timeout",
        "launcher-server-exit": "dedicated tmux server exited",
        "remote-display-watchdog-timeout": "SSH display watchdog timeout",
        "remote-display-server-exit": "SSH tmux server exited",
        "startup-probe-timeout": "startup health probe timeout",
    }
    description = descriptions.get(incident.reason, "tmux health failure")
    age = tmux_health.incident_age(incident.recorded_at)
    if incident.reason.endswith("-server-exit"):
        return f"{description}; {age}"
    return (
        f"{description}; {incident.consecutive_failures} consecutive failures; "
        f"{age}"
    )


def run_doctor(
    *,
    claude_home: Path,
    stdout: TextIO | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    """Print a shareable diagnostic report without exposing user data."""
    stdout = sys.stdout if stdout is None else stdout
    env = dict(os.environ if environ is None else environ)
    config_path = default_config_path()
    if config_path.is_file():
        try:
            config = load_config(config_path)
            config_status = f"{_display_path(config_path)}; valid=yes"
        except ConfigError as exc:
            config = Config()
            config_status = (
                f"{_display_path(config_path)}; valid=no ({exc})")
    else:
        config = Config()
        config_status = f"{_display_path(config_path)}; file=absent (defaults active)"

    system = platform.system() or "unknown"
    machine = platform.machine() or "unknown"
    python_version = platform.python_version()

    lines = (
        "Railmux diagnostics",
        f"Railmux: {__version__}",
        f"Python: {python_version}",
        f"Platform: {system} ({machine})",
        f"tmux: {_version('tmux', '-V')}",
        f"Dedicated Railmux tmux: {_dedicated_tmux_status()}",
        f"Legacy default tmux: {_legacy_tmux_status()}",
        "Tmux watchdog: enabled; reports and exits, never auto-kills or restarts",
        f"Last tmux incident: {_last_tmux_incident()}",
        f"Claude Code: {_version(config.claude_binary)}",
        f"Codex: {_version(config.codex_binary)}",
        f"Inside tmux: {_yes_no(bool(env.get('TMUX')))}",
        f"SSH transport: {_yes_no(is_ssh_session(env))}",
        f"Terminal capabilities: {_terminal_capabilities(env)}",
        f"Config: {config_status}",
        f"Preferred agent display: {config.agent_transport}",
        f"Claude data: {_directory_status(claude_home)}",
        f"Codex data: {_directory_status(Path(config.codex_home).expanduser())}",
        (
            "Privacy: session IDs, transcript content, credentials, hostnames, "
            "and raw custom paths are omitted; review before sharing."
        ),
    )
    print("\n".join(lines), file=stdout)
    return 0
