"""Privacy-safe, non-interactive environment diagnostics."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

from railmux import __version__
from railmux import legacy_sessions, tmux_health, tmux_server
from railmux.config import Config, ConfigError, default_config_path, load_config
from railmux.provider_paths import running_in_windows_wrapper
from railmux.runtime_config import normalized_command, runtime_environment
from railmux.ssh_display_diagnostics import (
    SshDisplayDiagnostic,
    read_diagnostic as read_ssh_display_diagnostic,
)
from railmux.terminal_status import (
    STYLE_ACCENT,
    STYLE_ERROR,
    STYLE_HEADING,
    STYLE_MUTED,
    STYLE_SUCCESS,
    STYLE_WARNING,
    TransientStatusLine,
    command_status,
    stream_is_tty,
    styled,
)


_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+){1,3}(?:[A-Za-z]|[-+][0-9A-Za-z.-]+)?)"
)
DOCTOR_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class ToolDiagnostic:
    status: str
    version: str | None = None
    configured: bool = False


@dataclass(frozen=True)
class TmuxServerDiagnostic:
    status: str
    context: str | None = None
    candidate_count: int | None = None
    restart_recommended: bool = False


@dataclass(frozen=True)
class IncidentDiagnostic:
    status: str
    category: str | None = None
    consecutive_failures: int | None = None
    age: str | None = None


@dataclass(frozen=True)
class ConfigDiagnostic:
    path: str
    status: str
    error_category: str | None = None


@dataclass(frozen=True)
class DirectoryDiagnostic:
    path: str
    exists: bool
    readable: bool
    writable: bool


@dataclass(frozen=True)
class ManagedWindowsDiagnostic:
    runtime_id: str | None
    app_version: str | None
    base_content_id: str | None
    running_ui_version: str | None
    transition_status: str | None


@dataclass(frozen=True)
class DoctorSnapshot:
    """Versioned, privacy-safe authority shared by text and JSON output."""

    schema_version: int
    railmux_version: str
    python_version: str
    platform_system: str
    platform_machine: str
    tools: dict[str, ToolDiagnostic]
    dedicated_tmux: TmuxServerDiagnostic
    legacy_tmux: TmuxServerDiagnostic
    watchdog_enabled: bool
    last_tmux_incident: IncidentDiagnostic
    inside_tmux: bool
    ssh_transport: bool
    terminal_256_colour: bool
    terminal_true_colour: bool
    locale_utf8: bool
    locale_configured: bool
    config: ConfigDiagnostic
    preferred_agent_display: str
    data_directories: dict[str, DirectoryDiagnostic]
    ssh_display: SshDisplayDiagnostic
    managed_windows: ManagedWindowsDiagnostic | None = None


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


def _tool_diagnostic(
    binary: str,
    *version_args: str,
    environ: dict[str, str] | None = None,
    configured: bool = False,
) -> ToolDiagnostic:
    """Return a bounded tool status without retaining configured commands."""
    try:
        command = normalized_command(binary)
        search_path = None if environ is None else environ.get("PATH")
        found = (
            shutil.which(command)
            if search_path is None
            else shutil.which(command, path=search_path)
        )
    except (OSError, TypeError):
        found = None
    if found is None:
        return ToolDiagnostic("missing", configured=configured)
    try:
        result = subprocess.run(
            [found, *(version_args or ("--version",))],
            env=environ,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolDiagnostic("timeout", configured=configured)
    except OSError:
        return ToolDiagnostic("unavailable", configured=configured)
    text = f"{result.stdout}\n{result.stderr}"
    match = _VERSION_RE.search(text)
    return ToolDiagnostic(
        "available" if match else "unavailable",
        match.group(1) if match else None,
        configured,
    )


def _directory_diagnostic(path: Path) -> DirectoryDiagnostic:
    try:
        exists = path.is_dir()
        readable = exists and os.access(path, os.R_OK)
        writable = exists and os.access(path, os.W_OK)
    except OSError:
        exists = readable = writable = False
    return DirectoryDiagnostic(
        path=_display_path(path),
        exists=exists,
        readable=readable,
        writable=writable,
    )


def _terminal_diagnostic(environ: dict[str, str]) -> tuple[bool, bool]:
    term = environ.get("TERM", "").lower()
    colorterm = environ.get("COLORTERM", "").lower()
    colours_256 = "256color" in term
    truecolour = colorterm in {"truecolor", "24bit"}
    return colours_256, truecolour


def _dedicated_tmux_diagnostic(
    environ: dict[str, str] | None = None,
) -> TmuxServerDiagnostic:
    """Return a bounded health result without exposing the socket pathname."""
    search_path = None if environ is None else environ.get("PATH")
    found = (
        shutil.which("tmux")
        if search_path is None
        else shutil.which("tmux", path=search_path)
    )
    if found is None:
        return TmuxServerDiagnostic("unavailable")
    source = os.environ if environ is None else environ
    timeout = None if running_in_windows_wrapper(source) else 1.0
    try:
        target = tmux_server.discover_target(timeout=timeout, env=environ)
    except tmux_server.TmuxClientServerMismatch:
        return TmuxServerDiagnostic("client_server_mismatch")
    except tmux_server.TmuxServerUnresponsive:
        return TmuxServerDiagnostic("unresponsive")
    except tmux_server.TmuxServerError:
        return TmuxServerDiagnostic("configuration_error")
    if target is None:
        return TmuxServerDiagnostic("not_running")
    context = (
        "inside"
        if tmux_server.is_current_server(target)
        else "outside"
    )
    return TmuxServerDiagnostic("healthy", context=context)


def _legacy_tmux_diagnostic(
    environ: dict[str, str] | None = None,
) -> TmuxServerDiagnostic:
    """Report only a bounded count; never expose session names or paths."""
    target, sessions, complete = legacy_sessions.discover(
        timeout=1.0, env=environ)
    if not complete:
        return TmuxServerDiagnostic("unavailable")
    if target is None:
        return TmuxServerDiagnostic("not_running")
    count = sum(
        session.name.startswith(("cc-", "cx-")) for session in sessions
    )
    return TmuxServerDiagnostic(
        "healthy",
        candidate_count=count,
        restart_recommended=bool(count),
    )


def _last_tmux_incident_diagnostic() -> IncidentDiagnostic:
    incident = tmux_health.read_last_incident()
    if incident is None:
        return IncidentDiagnostic("none")
    return IncidentDiagnostic(
        status="recorded",
        category=incident.reason,
        consecutive_failures=(
            None
            if incident.reason.endswith("-server-exit")
            else incident.consecutive_failures
        ),
        age=tmux_health.incident_age(incident.recorded_at),
    )


def collect_doctor_snapshot(
    *,
    claude_home: Path,
    environ: dict[str, str] | None = None,
) -> DoctorSnapshot:
    """Collect one bounded diagnostic snapshot for every output renderer."""
    env = dict(os.environ if environ is None else environ)
    config_path = default_config_path()
    if config_path.is_file():
        try:
            config = load_config(config_path)
            config_diagnostic = ConfigDiagnostic(
                path=_display_path(config_path),
                status="valid",
            )
        except ConfigError as exc:
            config = Config()
            config_diagnostic = ConfigDiagnostic(
                path=_display_path(config_path),
                status="invalid",
                error_category=(
                    "invalid_toml"
                    if str(exc) == "invalid TOML"
                    else "invalid_config"
                ),
            )
    else:
        config = Config()
        config_diagnostic = ConfigDiagnostic(
            path=_display_path(config_path),
            status="absent",
        )

    effective_env = runtime_environment(config, env)
    colours_256, truecolour = _terminal_diagnostic(env)
    locale_name = effective_env.get("LC_ALL") or effective_env.get("LC_CTYPE") or ""
    locale_utf8 = "UTF-8" in locale_name.upper() or "UTF8" in locale_name.upper()
    if not locale_utf8:
        try:
            locale_probe = subprocess.run(
                ["locale", "charmap"],
                env=effective_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            pass
        else:
            locale_utf8 = (
                locale_probe.returncode == 0
                and locale_probe.stdout.strip().upper().replace("-", "") == "UTF8"
            )
    tools = {
        "tmux": _tool_diagnostic(
            config.tmux_binary, "-V", environ=effective_env,
            configured=config.tmux_binary != "tmux"),
        "claude_code": _tool_diagnostic(
            config.claude_binary, environ=effective_env,
            configured=config.claude_binary != "claude"),
        "codex": _tool_diagnostic(
            config.codex_binary, environ=effective_env,
            configured=config.codex_binary != "codex"),
    }
    configured_tmux_missing = (
        config.tmux_binary != "tmux"
        and tools["tmux"].status == "missing"
    )
    dedicated_tmux = (
        TmuxServerDiagnostic("unavailable")
        if configured_tmux_missing
        else (
            _dedicated_tmux_diagnostic(effective_env)
            if config.tmux_binary != "tmux"
            else _dedicated_tmux_diagnostic()
        )
    )
    legacy_tmux = (
        TmuxServerDiagnostic("unavailable")
        if configured_tmux_missing
        else (
            _legacy_tmux_diagnostic(effective_env)
            if config.tmux_binary != "tmux"
            else _legacy_tmux_diagnostic()
        )
    )
    managed_windows = None
    if running_in_windows_wrapper(env):
        from railmux.windows_ui_transition import diagnostic_status

        managed_windows = ManagedWindowsDiagnostic(**diagnostic_status(env))
    return DoctorSnapshot(
        schema_version=DOCTOR_SCHEMA_VERSION,
        railmux_version=__version__,
        python_version=platform.python_version(),
        platform_system=platform.system() or "unknown",
        platform_machine=platform.machine() or "unknown",
        tools=tools,
        dedicated_tmux=dedicated_tmux,
        legacy_tmux=legacy_tmux,
        watchdog_enabled=True,
        last_tmux_incident=_last_tmux_incident_diagnostic(),
        inside_tmux=bool(env.get("TMUX")),
        ssh_transport=is_ssh_session(env),
        terminal_256_colour=colours_256,
        terminal_true_colour=truecolour,
        locale_utf8=locale_utf8,
        locale_configured=config.locale != "inherit",
        config=config_diagnostic,
        preferred_agent_display=config.agent_transport,
        data_directories={
            "claude": _directory_diagnostic(claude_home),
            "codex": _directory_diagnostic(
                Path(config.codex_home).expanduser()
            ),
        },
        ssh_display=read_ssh_display_diagnostic(),
        managed_windows=managed_windows,
    )


def _tool_text(diagnostic: ToolDiagnostic) -> str:
    suffix = " (configured; manage with 'railmux config')" if diagnostic.configured else ""
    if diagnostic.status == "missing":
        return "not found" + suffix
    if diagnostic.version is not None:
        return diagnostic.version + suffix
    if diagnostic.status == "timeout":
        return "available (version timed out)" + suffix
    return "available (version unavailable)" + suffix


def _dedicated_tmux_text(diagnostic: TmuxServerDiagnostic) -> str:
    if diagnostic.status == "unavailable":
        return "unavailable (tmux not found)"
    if diagnostic.status == "unresponsive":
        return "unresponsive (watchdog will not kill or restart it)"
    if diagnostic.status == "configuration_error":
        return "configuration error"
    if diagnostic.status == "client_server_mismatch":
        return "selected client is incompatible with the existing server"
    if diagnostic.status == "not_running":
        return "not running"
    context = (
        "current process is inside it"
        if diagnostic.context == "inside"
        else "current process is outside it"
    )
    return f"healthy ({context})"


def _legacy_tmux_text(diagnostic: TmuxServerDiagnostic) -> str:
    if diagnostic.status == "unavailable":
        return "unavailable (inventory timed out or changed)"
    if diagnostic.status == "not_running":
        return "not running"
    if diagnostic.candidate_count:
        return (
            f"healthy ({diagnostic.candidate_count} Railmux candidate(s); "
            "restart recommended)"
        )
    return "healthy (no Railmux candidates)"


def _incident_text(diagnostic: IncidentDiagnostic) -> str:
    if diagnostic.status == "none":
        return "none recorded"
    descriptions = {
        "launcher-attach-rejected": "Windows tmux client attach rejected",
        "launcher-relay-failed": "Windows terminal bridge failed",
        "launcher-watchdog-timeout": "local client watchdog timeout",
        "launcher-server-exit": "dedicated tmux server exited",
        "remote-display-watchdog-timeout": "SSH display watchdog timeout",
        "remote-display-server-exit": "SSH tmux server exited",
        "startup-probe-timeout": "startup health probe timeout",
    }
    description = descriptions.get(
        diagnostic.category or "", "tmux health failure"
    )
    if diagnostic.consecutive_failures is None:
        return f"{description}; {diagnostic.age}"
    return (
        f"{description}; {diagnostic.consecutive_failures} consecutive failures; "
        f"{diagnostic.age}"
    )


def _config_text(diagnostic: ConfigDiagnostic) -> str:
    if diagnostic.status == "valid":
        return f"{diagnostic.path}; valid=yes"
    if diagnostic.status == "invalid":
        detail = (
            "invalid TOML"
            if diagnostic.error_category == "invalid_toml"
            else "invalid configuration"
        )
        return f"{diagnostic.path}; valid=no ({detail})"
    return f"{diagnostic.path}; file=absent (defaults active)"


def _directory_text(diagnostic: DirectoryDiagnostic) -> str:
    return (
        f"{diagnostic.path}; exists={_yes_no(diagnostic.exists)}, "
        f"readable={_yes_no(diagnostic.readable)}, "
        f"writable={_yes_no(diagnostic.writable)}"
    )


def _ssh_display_text(diagnostic: SshDisplayDiagnostic) -> str:
    if diagnostic.status == "none":
        return "none recorded"
    if diagnostic.status != "recorded":
        return "unavailable"
    identity = (
        f"client {diagnostic.client_version or 'unknown'}, "
        f"protocol v{diagnostic.protocol if diagnostic.protocol is not None else 'unknown'}"
    )
    if diagnostic.outcome == "in_progress_or_ended_without_outcome":
        result = "may still be active or ended without a recorded outcome"
    else:
        result = diagnostic.outcome or "unknown outcome"
    stats = diagnostic.stats or SshDisplayDiagnostic("none").stats
    if stats is None:
        return f"{identity}; {result}; age={diagnostic.age or 'unknown'}"
    return (
        f"{identity}; {result}; age={diagnostic.age or 'unknown'}; "
        f"frames={stats.frames}, rows={stats.painted_rows}, "
        f"wire_bytes={stats.wire_bytes}, reconnects={stats.reconnect_successes}/"
        f"{stats.reconnect_attempts}, history_prefetch={stats.history_prefetch_requests}, "
        f"history_deep={stats.history_deep_requests}, "
        f"history_timeouts={stats.history_timeouts}, "
        f"anchor_rejects={stats.history_anchor_rejects}"
    )


def render_doctor_text(snapshot: DoctorSnapshot) -> str:
    """Render the stable human report from the structured authority."""
    lines = [
        "Railmux diagnostics",
        f"Railmux: {snapshot.railmux_version}",
        f"Python: {snapshot.python_version}",
        f"Platform: {snapshot.platform_system} ({snapshot.platform_machine})",
        f"tmux: {_tool_text(snapshot.tools['tmux'])}",
        (
            "Dedicated Railmux tmux: "
            f"{_dedicated_tmux_text(snapshot.dedicated_tmux)}"
        ),
        f"Legacy default tmux: {_legacy_tmux_text(snapshot.legacy_tmux)}",
        "Tmux watchdog: enabled; reports and exits, never auto-kills or restarts",
        f"Last tmux incident: {_incident_text(snapshot.last_tmux_incident)}",
        f"Claude Code: {_tool_text(snapshot.tools['claude_code'])}",
        f"Codex: {_tool_text(snapshot.tools['codex'])}",
        f"Inside tmux: {_yes_no(snapshot.inside_tmux)}",
        f"SSH transport: {_yes_no(snapshot.ssh_transport)}",
        (
            "Terminal capabilities: "
            f"256-colour={_yes_no(snapshot.terminal_256_colour)}, "
            f"true-colour={_yes_no(snapshot.terminal_true_colour)}"
        ),
        (
            "Locale: UTF-8="
            f"{_yes_no(snapshot.locale_utf8)}, source="
            f"{'configured' if snapshot.locale_configured else 'inherited'}"
        ),
        f"Config: {_config_text(snapshot.config)}",
        "Settings repair: run 'railmux config' (no tmux required)",
        f"Preferred agent display: {snapshot.preferred_agent_display}",
    ]
    if snapshot.managed_windows is not None:
        managed = snapshot.managed_windows
        identity = (
            managed.base_content_id[:12]
            if managed.base_content_id is not None
            else "unavailable"
        )
        lines.extend((
            f"Windows managed runtime: {managed.runtime_id or 'unavailable'}; "
            f"content={identity}",
            f"Windows app layer: installed={managed.app_version or 'unavailable'}, "
            f"running={managed.running_ui_version or 'not running'}, "
            f"transition={managed.transition_status or 'none'}",
        ))
    lines.extend((
        (
            "Most recent railmux ssh (host not recorded): "
            f"{_ssh_display_text(snapshot.ssh_display)}"
        ),
        f"Claude data: {_directory_text(snapshot.data_directories['claude'])}",
        f"Codex data: {_directory_text(snapshot.data_directories['codex'])}",
        (
            "Privacy: session IDs, transcript content, credentials, hostnames, "
            "and raw custom paths are omitted; review before sharing."
        ),
    ))
    return "\n".join(lines)


def render_doctor_terminal_text(snapshot: DoctorSnapshot, stream: TextIO) -> str:
    """Style the human report while leaving redirection and JSON unchanged."""
    lines = render_doctor_text(snapshot).splitlines()
    rendered: list[str] = []
    for index, line in enumerate(lines):
        if index == 0:
            rendered.append(styled(line, STYLE_ACCENT, stream=stream))
            continue
        if line.startswith("Privacy:"):
            rendered.append(styled(line, STYLE_MUTED, stream=stream))
            continue
        label, separator, value = line.partition(": ")
        if not separator:
            rendered.append(line)
            continue
        value_style = ""
        lowered = value.lower()
        if label == "Config":
            value_style = STYLE_ERROR if "valid=no" in lowered else STYLE_SUCCESS
        elif label in {"tmux", "Claude Code", "Codex"}:
            if "not found" in lowered or "missing" in lowered:
                value_style = STYLE_WARNING
        elif label == "Dedicated Railmux tmux":
            if lowered.startswith("healthy"):
                value_style = STYLE_SUCCESS
            elif lowered.startswith("unavailable"):
                value_style = STYLE_ERROR
            else:
                value_style = STYLE_WARNING
        elif label == "Last tmux incident" and not lowered.startswith("none"):
            value_style = STYLE_WARNING
        elif label == "Settings repair":
            value_style = STYLE_ACCENT
        rendered_value = (
            styled(value, value_style, stream=stream) if value_style else value
        )
        rendered.append(
            f"{styled(label + ':', STYLE_HEADING, stream=stream)} {rendered_value}"
        )
    return "\n".join(rendered)


def run_doctor(
    *,
    claude_home: Path,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: dict[str, str] | None = None,
    json_output: bool = False,
) -> int:
    """Print a shareable diagnostic report without exposing user data."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    status = TransientStatusLine(
        stderr,
        enabled=not json_output and stream_is_tty(stdout),
    )
    status.show(
        command_status(
            "railmux doctor",
            "Collecting local diagnostics…",
            stream=stderr,
        )
    )
    try:
        snapshot = collect_doctor_snapshot(
            claude_home=claude_home,
            environ=environ,
        )
    finally:
        status.clear()
    if json_output:
        json.dump(asdict(snapshot), stdout, indent=2, sort_keys=True)
        print(file=stdout)
    else:
        print(render_doctor_terminal_text(snapshot, stdout), file=stdout)
    return 0
