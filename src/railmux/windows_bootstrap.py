"""Thin native Windows bootstrap into the managed MSYS2 Railmux runtime."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

from railmux import __version__
from railmux.windows_msys2 import (
    MSYS2_RELEASE,
    Msys2Runtime,
    RuntimeInstallError,
    find_runtime,
    install_managed_runtime,
    managed_root,
)


_MINIMUM_WINDOWS_PYTHON = (3, 10)


def _print_help() -> None:
    print(
        "usage: railmux [OPTIONS]\n"
        "       railmux ssh HOST [OPTIONS]\n"
        "       railmux config [--remote HOST] [OPTIONS]\n"
        "       railmux doctor [--remote HOST] [OPTIONS]\n"
        "       railmux runtime {status,install} [--yes]\n\n"
        "Windows preview: Railmux runs in a private managed MSYS2/tmux "
        "runtime while Codex and Claude Code remain Windows-native and use "
        "the existing Windows user session directories."
    )


def _runtime_status(
    runtime: Msys2Runtime | None,
    *,
    environ: Mapping[str, str],
) -> int:
    print("Railmux Windows runtime")
    print(f"Backend: managed MSYS2 {MSYS2_RELEASE}")
    print(f"Status: {'ready' if runtime is not None else 'not installed'}")
    if runtime is not None:
        ownership = "Railmux-managed" if runtime.managed else "user-owned override"
        print(f"Runtime: {ownership} at {runtime.root}")
    else:
        root = managed_root(environ, version=__version__)
        if root is not None:
            print(f"Managed location: {root}")
    print("Provider data: shared from the Windows user profile")
    return 0 if runtime is not None else 1


def _confirm_install(*, input_fn: Callable[[str], str] = input) -> bool:
    try:
        answer = input_fn(
            "Install the private MSYS2/tmux runtime now? "
            "This downloads about 50 MB and uses about 300 MB [y/N] "
        )
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in {"y", "yes"}


def _install(
    *,
    environ: Mapping[str, str],
    assume_yes: bool,
    input_fn: Callable[[str], str] = input,
) -> Msys2Runtime | None:
    if not assume_yes and not _confirm_install(input_fn=input_fn):
        print("MSYS2 runtime installation cancelled.", file=sys.stderr)
        return None
    try:
        runtime = install_managed_runtime(version=__version__, environ=environ)
    except RuntimeInstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    print("Railmux MSYS2 runtime is ready.")
    return runtime


def _wait_for_runtime(
    runtime: Msys2Runtime,
    arguments: Sequence[str],
    *,
    environ: Mapping[str, str],
    popen: Callable[..., object],
) -> int:
    try:
        process = popen(
            runtime.argv(arguments),
            env=runtime.environment(environ),
        )
    except OSError as exc:
        print(f"error: could not enter the MSYS2 runtime: {exc}", file=sys.stderr)
        return 2
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            # The console event reaches the MSYS child too.  Keep this thin
            # parent alive so it does not terminate the child before tmux and
            # Railmux restore their terminal state.
            continue


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
    version_info: tuple[int, ...] = sys.version_info,
    runtime_finder: Callable[..., Msys2Runtime | None] = find_runtime,
    popen: Callable[..., object] = subprocess.Popen,
    input_fn: Callable[[str], str] = input,
    stdin_isatty: Callable[[], bool] = sys.stdin.isatty,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if version_info < _MINIMUM_WINDOWS_PYTHON:
        print(
            "error: the Windows preview requires Python 3.10 or newer",
            file=sys.stderr,
        )
        return 2
    if arguments == ["--version"]:
        print(f"railmux {__version__} (Windows MSYS2 bootstrap)")
        return 0
    if arguments in (["-h"], ["--help"]):
        _print_help()
        return 0

    runtime = runtime_finder(version=__version__, environ=environ)
    if arguments[:2] == ["runtime", "status"] and len(arguments) == 2:
        return _runtime_status(runtime, environ=environ)
    if arguments and arguments[0] == "runtime":
        if arguments[1:] not in (["install"], ["install", "--yes"]):
            print("error: usage: railmux runtime {status,install} [--yes]", file=sys.stderr)
            return 2
        runtime = runtime or _install(
            environ=environ,
            assume_yes=arguments[-1:] == ["--yes"],
            input_fn=input_fn,
        )
        return 0 if runtime is not None else 2

    if runtime is None:
        root = managed_root(environ, version=__version__)
        print(
            "Railmux for Windows needs its private MSYS2/tmux runtime.\n"
            "The Windows Codex/Claude executables and their existing session "
            "directories remain shared.",
            file=sys.stderr,
        )
        if root is not None:
            print(f"Managed location: {root}", file=sys.stderr)
        if not stdin_isatty():
            print(
                "Run 'railmux runtime install' interactively or "
                "'railmux runtime install --yes' to install it.",
                file=sys.stderr,
            )
            return 2
        runtime = _install(environ=environ, assume_yes=False, input_fn=input_fn)
        if runtime is None:
            return 2

    return _wait_for_runtime(
        runtime,
        arguments,
        environ=environ,
        popen=popen,
    )
