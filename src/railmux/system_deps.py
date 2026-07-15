"""Detection and optional installation of Railmux system dependencies."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import TextIO

from railmux import tmux_ctl


@dataclass(frozen=True)
class TmuxInstallPlan:
    """Platform-specific guidance for making ``tmux`` available."""

    manager: str | None
    command: tuple[str, ...] | None
    display_command: str | None
    guidance: str


def _is_root() -> bool:
    get_euid = getattr(os, "geteuid", None)
    return bool(get_euid and get_euid() == 0)


def _privileged_display(command: tuple[str, ...]) -> str:
    if _is_root():
        return shlex.join(command)
    if shutil.which("sudo"):
        return shlex.join(("sudo", *command))
    return f"run as root: {shlex.join(command)}"


def _apt_install_plan() -> TmuxInstallPlan:
    base = ("apt-get", "install", "tmux")
    if _is_root():
        command: tuple[str, ...] | None = base
    elif shutil.which("sudo"):
        command = ("sudo", *base)
    else:
        command = None
    display = _privileged_display(base)
    return TmuxInstallPlan(
        manager="apt-get",
        command=command,
        display_command=display,
        guidance=f"Install tmux with {display}.",
    )


def tmux_install_plan(platform_name: str | None = None) -> TmuxInstallPlan:
    """Return safe installation guidance for the current platform.

    Railmux only offers to execute package managers whose behaviour is covered
    by its tests (Homebrew and apt-get). Other common Linux managers still get
    an exact command, but remain a manual step.
    """
    platform_name = sys.platform if platform_name is None else platform_name

    if platform_name == "darwin":
        if shutil.which("brew"):
            command = ("brew", "install", "tmux")
            display = shlex.join(command)
            return TmuxInstallPlan(
                manager="Homebrew",
                command=command,
                display_command=display,
                guidance=f"Install tmux with {display}.",
            )
        return TmuxInstallPlan(
            manager=None,
            command=None,
            display_command="brew install tmux",
            guidance=(
                "Homebrew was not found. Install it from https://brew.sh/ "
                "(or use another package manager), then run brew install tmux."
            ),
        )

    if platform_name.startswith("linux"):
        if shutil.which("apt-get"):
            return _apt_install_plan()

        manual_managers = (
            ("dnf", ("dnf", "install", "tmux")),
            ("yum", ("yum", "install", "tmux")),
            ("pacman", ("pacman", "-S", "tmux")),
            ("zypper", ("zypper", "install", "tmux")),
            ("apk", ("apk", "add", "tmux")),
        )
        for manager, command in manual_managers:
            if shutil.which(manager):
                display = _privileged_display(command)
                return TmuxInstallPlan(
                    manager=manager,
                    command=None,
                    display_command=display,
                    guidance=f"Install tmux with {display}.",
                )

    return TmuxInstallPlan(
        manager=None,
        command=None,
        display_command=None,
        guidance="Install tmux with your system package manager and ensure it is on PATH.",
    )


def _is_interactive(stdin: TextIO, stderr: TextIO) -> bool:
    return bool(
        getattr(stdin, "isatty", lambda: False)()
        and getattr(stderr, "isatty", lambda: False)()
    )


def _confirm_install(
    plan: TmuxInstallPlan, stdin: TextIO, stderr: TextIO
) -> bool:
    print(
        f"Railmux can run: {plan.display_command}",
        file=stderr,
    )
    print(
        f"Install tmux now with {plan.manager}? [y/N] ",
        end="",
        file=stderr,
        flush=True,
    )
    answer = stdin.readline()
    return answer.strip().lower() in {"y", "yes"}


def ensure_tmux_available(
    *, stdin: TextIO | None = None, stderr: TextIO | None = None
) -> bool:
    """Ensure ``tmux`` is on PATH, optionally installing it with consent.

    Installation is offered only on an interactive terminal and defaults to
    "no". Package manager input/output is inherited from Railmux so sudo and
    package-manager prompts remain visible and under the user's control.
    """
    if tmux_ctl.has_tmux():
        return True

    stdin = sys.stdin if stdin is None else stdin
    stderr = sys.stderr if stderr is None else stderr
    plan = tmux_install_plan()
    print("tmux is required but was not found on PATH.", file=stderr)

    if plan.command is not None and _is_interactive(stdin, stderr):
        try:
            confirmed = _confirm_install(plan, stdin, stderr)
        except (EOFError, KeyboardInterrupt):
            print("\nInstallation cancelled.", file=stderr)
            confirmed = False

        if confirmed:
            try:
                result = subprocess.run(plan.command, check=False)
            except (OSError, KeyboardInterrupt) as exc:
                print(f"Could not run {plan.manager}: {exc}", file=stderr)
            else:
                if result.returncode == 0 and tmux_ctl.has_tmux():
                    tmux_ctl.tmux_version.cache_clear()
                    version = tmux_ctl.tmux_version()
                    suffix = f" {version[0]}.{version[1]}" if version != (0, 0) else ""
                    print(f"tmux{suffix} is now available; continuing.", file=stderr)
                    return True
                if result.returncode == 0:
                    print(
                        "The installer completed, but tmux is still not on PATH.",
                        file=stderr,
                    )
                else:
                    print(
                        f"{plan.manager} exited with status {result.returncode}.",
                        file=stderr,
                    )

    print(f"error: {plan.guidance}", file=stderr)
    return False
