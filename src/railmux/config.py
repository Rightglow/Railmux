"""Load railmux configuration from TOML with sensible defaults."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9-3.10
    import tomli as tomllib

from railmux.setting_contracts import (
    SSH_HISTORY_DEFAULT_LINES,
    SSH_HISTORY_MAX_LINES,
    SSH_HISTORY_MIN_LINES,
    bounds_for,
    choices_for,
)
from railmux.provider_paths import provider_path


class ConfigError(ValueError):
    """A safe, user-facing configuration error without file contents."""


@dataclass(frozen=True)
class Config:
    tmux_binary: str = "tmux"
    claude_binary: str = "claude"
    codex_binary: str = "codex"
    locale: str = "inherit"
    codex_home: str = "~/.codex"
    poll_interval_ms: int = 1000
    agent_transport: str = "swap"
    show_empty_projects: bool = False
    ssh_history_lines: int = SSH_HISTORY_DEFAULT_LINES
    ssh_claude_history: str = "ask"
    interaction_path_open: str = "ask"

    @property
    def ssh_path_open(self) -> str:
        """Compatibility alias for clients released before 0.4.0."""
        return self.interaction_path_open

    def resolved_codex_home(self) -> Path:
        """The one resolved ``CODEX_HOME`` directory.

        Single source of truth so listing (CodexIndex), launching (new/resume),
        deleting (``codex delete``) and config/env-key reading all hit the same
        directory even when ``[codex] home`` is non-default. ``~`` is expanded
        and relative paths are made absolute before a launched Codex changes to
        its project cwd. The directory is not required to exist.
        """
        path = provider_path(self.codex_home).expanduser()
        try:
            return path.resolve()
        except OSError:
            return path.absolute()


def default_config_path() -> Path:
    return Path.home() / ".config" / "railmux" / "config.toml"


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _string(table: dict[str, Any], key: str, default: str, label: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value


def _command(table: dict[str, Any], key: str, default: str, label: str) -> str:
    value = _string(table, key, default, label)
    if any(character in value for character in ("\0", "\r", "\n")):
        raise ConfigError(f"{label} must be one executable name or path")
    return value


_LOCALE_RE = re.compile(r"[A-Za-z0-9_.@-]{1,128}\Z")


def _locale(table: dict[str, Any]) -> str:
    value = table.get("locale", "inherit")
    if (
        not isinstance(value, str)
        or not _LOCALE_RE.fullmatch(value)
    ):
        raise ConfigError(
            "environment.locale must be 'inherit' or a locale name"
        )
    return value


def _tmux_binary(table: dict[str, Any]) -> str:
    value = _command(table, "binary", "tmux", "tmux.binary")
    if value != "tmux" and ("/" not in value or Path(value).name != "tmux"):
        raise ConfigError(
            "tmux.binary must be 'tmux' or an executable path ending in /tmux"
        )
    return value


def load_config(config_path: Path | None = None) -> Config:
    if config_path is None:
        config_path = default_config_path()
    if not config_path.is_file():
        return Config()

    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("invalid TOML") from exc
    except OSError as exc:
        raise ConfigError("configuration file could not be read") from exc

    tmux = _table(data, "tmux")
    claude = _table(data, "claude")
    codex = _table(data, "codex")
    environment = _table(data, "environment")
    live = _table(data, "live")
    projects = _table(data, "projects")
    ssh = _table(data, "ssh")
    interaction = _table(data, "interaction")

    poll_value = live.get("poll_interval_ms", 1000)
    if isinstance(poll_value, bool):
        raise ConfigError("live.poll_interval_ms must be a positive integer")
    try:
        poll_interval_ms = int(poll_value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "live.poll_interval_ms must be a positive integer") from exc
    if poll_interval_ms <= 0:
        raise ConfigError("live.poll_interval_ms must be a positive integer")

    agent_transport = live.get("agent_transport", "swap")
    if agent_transport not in ("nested", "swap"):
        raise ConfigError(
            'live.agent_transport must be either "nested" or "swap"')

    history_lines = ssh.get("history_lines", SSH_HISTORY_DEFAULT_LINES)
    history_min, history_max = bounds_for("ssh.history_lines")
    if (
        not isinstance(history_lines, int)
        or isinstance(history_lines, bool)
        or not history_min <= history_lines <= history_max
    ):
        raise ConfigError(
            "ssh.history_lines must be an integer between "
            f"{SSH_HISTORY_MIN_LINES} and {SSH_HISTORY_MAX_LINES}"
        )
    claude_history = ssh.get("claude_history", "ask")
    if (
        not isinstance(claude_history, str)
        or claude_history not in choices_for("ssh.claude_history")
    ):
        raise ConfigError(
            'ssh.claude_history must be "ask", "local", or "native"'
        )
    # The transport-neutral key wins. The released SSH-only spelling remains
    # a read alias so an upgrade never silently forgets the user's choice.
    canonical_path_open = "path_open" in interaction
    path_open = interaction.get("path_open", ssh.get("path_open", "ask"))
    if (
        not isinstance(path_open, str)
        or path_open not in choices_for("interaction.path_open")
    ):
        label = "interaction.path_open" if canonical_path_open else "ssh.path_open"
        raise ConfigError(f'{label} must be "ask", "internal", or "external"')

    return Config(
        tmux_binary=_tmux_binary(tmux),
        claude_binary=_command(
            claude, "binary", "claude", "claude.binary"),
        codex_binary=_command(codex, "binary", "codex", "codex.binary"),
        locale=_locale(environment),
        codex_home=_string(codex, "home", "~/.codex", "codex.home"),
        poll_interval_ms=poll_interval_ms,
        agent_transport=agent_transport,
        show_empty_projects=projects.get("show_empty_projects") is True,
        ssh_history_lines=history_lines,
        ssh_claude_history=claude_history,
        interaction_path_open=path_open,
    )
