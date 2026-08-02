"""Resolve the small, non-secret process environment owned by Railmux.

The configured tmux executable is represented by a PATH prefix because tmux
commands also run inside the dedicated server (bindings and run-shell helpers),
not only as Python subprocesses. Requiring the executable's conventional
``tmux`` basename makes every existing call site and server-side helper resolve
the same client without shell interpolation or a second command authority.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping

from railmux.config import Config


TMUX_BINARY_ENV = "RAILMUX_TMUX_BINARY"


@dataclass(frozen=True)
class ExecutableCheck:
    value: str
    resolved: str | None
    version: str | None
    error: str | None

    @property
    def valid(self) -> bool:
        return self.resolved is not None and self.error is None


def normalized_command(value: str) -> str:
    """Expand a path-like command before a launched agent changes cwd."""
    if "/" not in value:
        return value
    path = Path(value).expanduser()
    # Preserve a user-managed ``tmux`` symlink name. Resolving the final link
    # could turn ``.../tmux`` into ``.../tmux-3.4`` and break the PATH-based
    # single-command authority used by server-side helpers.
    return str(path.absolute())


def runtime_environment(
    config: Config,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the environment for Railmux-owned tmux and provider children."""
    result = dict(os.environ if environ is None else environ)
    tmux_command = normalized_command(config.tmux_binary)
    if "/" in tmux_command:
        tmux_path = Path(tmux_command)
        if tmux_path.name == "tmux":
            directory = str(tmux_path.parent)
            existing = [
                entry
                for entry in result.get("PATH", "").split(os.pathsep)
                if entry and entry != directory
            ]
            result["PATH"] = os.pathsep.join(
                [directory, *existing]
            )
            result[TMUX_BINARY_ENV] = tmux_command
    else:
        result.pop(TMUX_BINARY_ENV, None)
    if config.locale != "inherit":
        result["LC_ALL"] = config.locale
    return result


def activate_runtime_environment(
    config: Config,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Apply the configured environment to this Railmux process and children."""
    target = os.environ if environ is None else environ
    updated = runtime_environment(config, target)
    target.clear()
    target.update(updated)


def resolve_executable(
    value: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    command = normalized_command(value)
    path = None if environ is None else environ.get("PATH")
    try:
        return shutil.which(command, path=path)
    except (OSError, TypeError):
        return None


def check_executable(
    kind: str,
    value: str,
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = 3.0,
) -> ExecutableCheck:
    """Validate one argv-only program setting without launching its UI."""
    normalized = normalized_command(value.strip())
    if kind == "tmux" and (
        normalized != "tmux"
        and ("/" not in normalized or Path(normalized).name != "tmux")
    ):
        return ExecutableCheck(
            normalized, None, None,
            "the configured tmux executable must be named 'tmux'",
        )
    resolved = resolve_executable(normalized, environ=environ)
    if resolved is None:
        return ExecutableCheck(
            normalized, None, None, "executable was not found or is not executable"
        )
    version_args = ("-V",) if kind == "tmux" else ("--version",)
    try:
        result = subprocess.run(
            [resolved, *version_args],
            env=None if environ is None else dict(environ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ExecutableCheck(normalized, resolved, None, "version check timed out")
    except (OSError, UnicodeError):
        return ExecutableCheck(normalized, resolved, None, "version check failed")
    output = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode != 0 or not output:
        return ExecutableCheck(
            normalized, resolved, None, "version check did not succeed"
        )
    safe_version = "".join(
        character if character >= " " and character != "\x7f" else " "
        for character in output[0][:160]
    ).strip()
    return ExecutableCheck(
        normalized,
        resolved,
        safe_version or "version available",
        None,
    )


def check_utf8_locale(
    value: str,
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = 3.0,
) -> tuple[bool, str]:
    """Confirm that one installed locale selects a UTF-8 character map."""
    if value == "inherit":
        return True, "inherited environment"
    env = dict(os.environ if environ is None else environ)
    env["LC_ALL"] = value
    try:
        result = subprocess.run(
            ["locale", "charmap"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "locale check timed out"
    except (OSError, UnicodeError):
        return False, "the locale command is unavailable"
    charmap = result.stdout.strip()
    if result.returncode != 0:
        return False, "locale is not installed or could not be activated"
    if charmap.upper().replace("-", "") != "UTF8":
        return False, f"locale uses {charmap or 'an unknown character map'}, not UTF-8"
    return True, f"UTF-8 locale ({value})"
