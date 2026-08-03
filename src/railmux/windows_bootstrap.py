"""Thin Windows bootstrap for a delegated POSIX Railmux runtime.

This module intentionally owns no terminal emulation, provider processes, or
Railmux UI state. The first preview slice discovers the default or explicitly
selected WSL distribution containing Railmux and hands the original argv to
it. Managed runtime installation and MSYS2 selection are later,
consent-gated stages.
"""
from __future__ import annotations

import ntpath
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from railmux import __version__


_MINIMUM_WINDOWS_PYTHON = (3, 10)
_WSL_DISTRO_ENV = "RAILMUX_WSL_DISTRO"
_PROBE_TIMEOUT_SECONDS = 10.0
_LOGIN_RESOLVE_COMMAND = "command -v railmux"


@dataclass(frozen=True)
class WslRuntime:
    executable: str
    distribution: str | None
    railmux_executable: str

    def argv(self, arguments: Sequence[str]) -> list[str]:
        return [
            *_wsl_prefix(self.executable, self.distribution),
            "--exec",
            self.railmux_executable,
            *arguments,
        ]


Probe = Callable[..., subprocess.CompletedProcess[bytes]]
PopenFactory = Callable[..., subprocess.Popen]


def _system_wsl_executable(
    environ: Mapping[str, str],
    *,
    is_file: Callable[[str], bool] = os.path.isfile,
) -> str | None:
    """Resolve only Microsoft's system WSL launcher, never a CWD/PATH shim."""
    system_root = environ.get("SystemRoot") or environ.get("SYSTEMROOT")
    if not system_root:
        return None
    directories = []
    if environ.get("PROCESSOR_ARCHITEW6432"):
        directories.append("Sysnative")
    directories.append("System32")
    for directory in directories:
        candidate = ntpath.join(system_root, directory, "wsl.exe")
        if is_file(candidate):
            return candidate
    return None


def _wsl_prefix(executable: str, distribution: str | None) -> list[str]:
    prefix = [executable]
    if distribution is not None:
        prefix.extend(("--distribution", distribution))
    return prefix


def _probe(
    argv: Sequence[str],
    *,
    timeout: float = _PROBE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def _probe_argv(
    executable: str,
    distribution: str | None,
    railmux_executable: str,
) -> list[str]:
    return [
        *_wsl_prefix(executable, distribution),
        "--exec",
        railmux_executable,
        "--version",
    ]


def _run_probe(argv: Sequence[str], *, probe: Probe) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return probe(list(argv), timeout=_PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _login_railmux_path(
    executable: str,
    distribution: str | None,
    *,
    probe: Probe,
) -> str | None:
    """Resolve a user install through a fixed login-shell command."""
    result = _run_probe(
        [
            *_wsl_prefix(executable, distribution),
            "--exec",
            "/bin/sh",
            "-lc",
            _LOGIN_RESOLVE_COMMAND,
        ],
        probe=probe,
    )
    if result is None or result.returncode:
        return None
    # Linux command output is UTF-8 regardless of the Windows ANSI code page.
    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    candidate = lines[-1].strip() if lines else ""
    if not candidate.startswith("/") or "\0" in candidate:
        return None
    verified = _run_probe(
        _probe_argv(executable, distribution, candidate),
        probe=probe,
    )
    if verified is None or verified.returncode:
        return None
    return candidate


def find_wsl_runtime(
    executable: str | None,
    *,
    environ: Mapping[str, str] = os.environ,
    probe: Probe = _probe,
) -> WslRuntime | None:
    if executable is None:
        return None
    requested = environ.get(_WSL_DISTRO_ENV, "").strip()
    distribution = requested or None
    direct = _run_probe(
        _probe_argv(executable, distribution, "railmux"),
        probe=probe,
    )
    if direct is not None and direct.returncode == 0:
        return WslRuntime(executable, distribution, "railmux")
    resolved = _login_railmux_path(
        executable,
        distribution,
        probe=probe,
    )
    if resolved is None:
        return None
    return WslRuntime(executable, distribution, resolved)


def _print_help() -> None:
    print(
        "usage: railmux [OPTIONS]\n"
        "       railmux ssh HOST [OPTIONS]\n"
        "       railmux config [--remote HOST] [OPTIONS]\n"
        "       railmux doctor [--remote HOST] [OPTIONS]\n\n"
        "Windows wrapper preview: commands run inside the default WSL "
        "distribution. Set RAILMUX_WSL_DISTRO to select another distribution.\n"
        "Railmux must already be installed inside that distribution."
    )


def _runtime_error(*, wsl_present: bool, requested: str) -> None:
    if requested:
        detail = (
            f"WSL distribution {requested!r} is unavailable or does not have "
            "Railmux installed."
        )
    elif wsl_present:
        detail = (
            "The default WSL distribution is unavailable or does not have "
            "Railmux installed."
        )
    else:
        detail = "The system WSL launcher is not installed."
    print(f"error: {detail}", file=sys.stderr)
    print(
        "This 0.4 preview does not install software or modify a WSL "
        "distribution. Install Railmux inside WSL, select it with "
        "RAILMUX_WSL_DISTRO if needed, or wait for the consent-based managed "
        "MSYS2 fallback.",
        file=sys.stderr,
    )


def _wait_for_runtime(
    argv: Sequence[str],
    *,
    popen: PopenFactory = subprocess.Popen,
) -> int:
    process = popen(list(argv))
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            # Windows sends the console event to wsl.exe as well. Keep the
            # wrapper alive so subprocess.run cannot kill the handoff before
            # the delegated process handles Ctrl-C and restores its terminal.
            continue


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
    resolve_wsl: Callable[[Mapping[str, str]], str | None] = _system_wsl_executable,
    probe: Probe = _probe,
    popen: PopenFactory = subprocess.Popen,
    version_info: tuple[int, ...] = sys.version_info,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if version_info < _MINIMUM_WINDOWS_PYTHON:
        print(
            "error: the Windows preview requires Python 3.10 or newer",
            file=sys.stderr,
        )
        return 2
    if arguments == ["--version"]:
        print(f"railmux {__version__} (Windows bootstrap)")
        return 0
    if arguments in (["-h"], ["--help"]):
        _print_help()
        return 0

    executable = resolve_wsl(environ)
    runtime = find_wsl_runtime(executable, environ=environ, probe=probe)
    if runtime is None:
        _runtime_error(
            wsl_present=executable is not None,
            requested=environ.get(_WSL_DISTRO_ENV, "").strip(),
        )
        return 2
    try:
        return _wait_for_runtime(runtime.argv(arguments), popen=popen)
    except OSError as exc:
        print(f"error: could not enter WSL: {exc}", file=sys.stderr)
        return 2
