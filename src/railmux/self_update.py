"""Bounded, consent-aware updates for the outer Railmux launcher."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import urllib.request
from importlib import metadata
from typing import NoReturn, Sequence

from packaging.version import InvalidVersion, Version

from railmux import __version__
from railmux.settings import Settings


_PYPI_PROJECT_URL = "https://pypi.org/pypi/railmux/json"
_MAX_RESPONSE_BYTES = 1_000_000


def latest_release(*, timeout: float = 1.0) -> str | None:
    """Return a newer stable PyPI release, or ``None`` on any check failure."""
    try:
        with urllib.request.urlopen(
            _PYPI_PROJECT_URL, timeout=timeout,
        ) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            return None
        payload = json.loads(raw)
        latest = payload["info"]["version"]
        if not isinstance(latest, str):
            return None
        parsed_latest = Version(latest)
        if not parsed_latest.is_prerelease and parsed_latest > Version(__version__):
            return latest
    except (
        OSError,
        TimeoutError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        InvalidVersion,
    ):
        return None
    return None


def installation_is_editable() -> bool:
    """Whether pip recorded Railmux as an editable source installation."""
    try:
        direct_url = metadata.distribution("railmux").read_text(
            "direct_url.json")
        if not direct_url:
            return False
        payload = json.loads(direct_url)
        return payload.get("dir_info", {}).get("editable") is True
    except (
        metadata.PackageNotFoundError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False


def upgrade_argv(version: str) -> list[str]:
    """Install into the interpreter environment that launched Railmux."""
    argv = [sys.executable, "-m", "pip", "install"]
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        argv.append("--user")
    argv.extend(("--upgrade", f"railmux=={version}"))
    return argv


def _prompt(current: str, latest: str) -> str:
    print(
        f"Railmux {latest} is available (installed: {current}).",
        file=sys.stderr,
    )
    while True:
        try:
            answer = input(
                "Update Railmux? [A]lways / [T]his time / [N]o / Ne[v]er: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return "no"
        if answer in {"a", "always"}:
            return "always"
        if answer in {"t", "this", "this time"}:
            return "this_time"
        if answer in {"", "n", "no"}:
            return "no"
        if answer in {"v", "never"}:
            return "never"
        print("Please enter A, T, N, or V.", file=sys.stderr)


def _restart(raw_args: Sequence[str]) -> NoReturn:
    argv = [sys.executable, "-m", "railmux", *raw_args]
    os.execv(sys.executable, argv)
    raise AssertionError("os.execv returned unexpectedly")


def maybe_upgrade_before_launch(
    raw_args: Sequence[str],
    settings: Settings,
) -> None:
    """Check once in the outer launcher and restart after an accepted update."""
    policy = settings.update_policy
    if policy == "never":
        return
    latest = latest_release()
    if latest is None:
        return

    # A source checkout must stay under the developer's control. Check before
    # prompting so "Always" is never persisted for an installation that this
    # updater cannot safely replace.
    if installation_is_editable():
        print(
            f"Railmux {latest} is available, but this is an editable source "
            "installation. Update the checkout and reinstall it instead.",
            file=sys.stderr,
        )
        return

    if policy == "ask":
        if not sys.stdin.isatty():
            return
        decision = _prompt(__version__, latest)
        if decision == "no":
            return
        if decision == "never":
            if not settings.set_update_policy("never"):
                print(
                    "warning: could not save the Never update preference; "
                    "skipping this launch only",
                    file=sys.stderr,
                )
            return
        if decision == "always" and not settings.set_update_policy("always"):
            print(
                "warning: could not save the Always update preference; "
                "updating this time only",
                file=sys.stderr,
            )

    argv = upgrade_argv(latest)
    print(f"Updating Railmux to {latest}...", file=sys.stderr)
    try:
        result = subprocess.run(argv, check=False)
    except OSError as exc:
        print(
            f"warning: could not start pip: {exc}\n"
            f"Update manually:\n  {shlex.join(argv)}",
            file=sys.stderr,
        )
        return
    if result.returncode:
        print(
            "warning: Railmux update failed; continuing with the installed "
            f"version.\nUpdate manually:\n  {shlex.join(argv)}",
            file=sys.stderr,
        )
        return
    print("Railmux update succeeded; restarting...", file=sys.stderr)
    try:
        _restart(raw_args)
    except OSError as exc:
        restart = [sys.executable, "-m", "railmux", *raw_args]
        print(
            f"error: Railmux was updated but could not restart: {exc}\n"
            f"Run:\n  {shlex.join(restart)}",
            file=sys.stderr,
        )
