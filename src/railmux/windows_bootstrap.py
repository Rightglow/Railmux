"""Thin native Windows bootstrap into the managed MSYS2 Railmux runtime."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

from railmux import __version__
from railmux.diagnostic_contract import DOCTOR_SCHEMA_VERSION
from railmux.windows_msys2 import (
    MSYS2_RELEASE,
    Msys2Runtime,
    RuntimeInstallError,
    apply_managed_runtime_uninstall,
    apply_managed_runtime_prune,
    find_runtime,
    install_managed_runtime,
    managed_runtime_status,
    managed_root,
    plan_managed_runtime_uninstall,
    plan_managed_runtime_prune,
    reusable_managed_base_candidate,
)
from railmux.windows_paths import managed_windows_data_root


_MINIMUM_WINDOWS_PYTHON = (3, 10)


def _print_help() -> None:
    print(
        "usage: railmux [OPTIONS]\n"
        "       railmux ssh HOST [OPTIONS]\n"
        "       railmux config [--remote HOST] [OPTIONS]\n"
        "       railmux doctor [--remote HOST] [OPTIONS]\n"
        "       railmux runtime status [--json] [--verify]\n"
        "       railmux runtime install [--yes] [--verbose]\n"
        "       railmux runtime uninstall [--dry-run] [--yes]\n"
        "       railmux runtime prune [--dry-run] [--yes] [--caches]\n\n"
        "Railmux for Windows runs in a private managed MSYS2/tmux "
        "runtime while Codex and Claude Code remain Windows-native and use "
        "the existing Windows user session directories."
    )


def _runtime_status(
    runtime: Msys2Runtime | None,
    *,
    environ: Mapping[str, str],
    json_output: bool = False,
    verify: bool = False,
) -> int:
    snapshot = managed_runtime_status(
        version=__version__, environ=environ, verify=verify)
    data_root = (
        managed_windows_data_root(environ)
        if runtime is None or runtime.managed
        else None
    )
    if data_root is not None:
        snapshot["data_root"] = str(data_root)
        snapshot["log_directory"] = str(data_root / "logs")
    if json_output:
        json.dump(snapshot, sys.stdout, indent=2, sort_keys=True)
        print()
        return 0 if runtime is not None else 1
    print("Railmux Windows runtime")
    print(f"Backend: managed MSYS2 {MSYS2_RELEASE}")
    status = snapshot.get("status")
    if runtime is not None:
        display_status = "ready"
    elif status == "base_ready":
        display_status = "reusable base ready; current app layer not installed"
    elif status == "incomplete":
        display_status = "incomplete"
    else:
        display_status = "not installed"
    print(f"Status: {display_status}")
    if runtime is not None:
        ownership = "Railmux-managed" if runtime.managed else "user-owned override"
        print(f"Runtime: {ownership} at {runtime.root}")
    else:
        root = managed_root(environ)
        if root is not None:
            print(f"Managed location: {root}")
        print("Next: run 'railmux runtime install' from an interactive PowerShell.")
    if data_root is not None:
        print(f"Data root: {data_root}")
        print(f"Install logs: {data_root / 'logs'}")
    print("Provider data: shared from the Windows user profile")
    content = snapshot.get("content_identity")
    if isinstance(content, str):
        print(f"Base identity: {content[:12]} ({snapshot['package_count']} packages)")
    layers = snapshot.get("layers")
    if isinstance(layers, list):
        print(f"Application layers: {len(layers)}")
    verification = snapshot.get("content_verification")
    if isinstance(verification, str):
        print(f"Base verification: {verification}")
    capability = snapshot.get("tmux_capability")
    if isinstance(capability, dict):
        tmux_version = capability.get("version")
        fidelity = capability.get("windows_visual_fidelity")
        capability_verification = capability.get("verification")
        if isinstance(tmux_version, str):
            detail = (
                f"tmux {tmux_version} (recorded base marker; "
                f"verification={capability_verification}); "
                f"Windows visual fidelity={fidelity}"
            )
            print(f"Terminal rendering: {detail}")
        elif runtime is not None:
            print(
                "Terminal rendering: tmux unknown; "
                "Windows visual fidelity=unknown"
            )
    return 0 if runtime is not None else 1


def _native_doctor_missing_runtime(
    *,
    environ: Mapping[str, str],
    json_output: bool,
    remote_requested: bool,
) -> int:
    runtime = managed_runtime_status(
        version=__version__, environ=environ, verify=False)
    if json_output:
        json.dump(
            {
                "schema_version": DOCTOR_SCHEMA_VERSION,
                "status": "runtime_not_installed",
                "railmux_version": __version__,
                "managed_windows": runtime,
                "remote_preflight": "not_run" if remote_requested else None,
            },
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        print()
        return 2
    print(
        "Railmux diagnostics (native Windows bootstrap; managed runtime "
        "was not entered)"
    )
    print(f"Railmux: {__version__}")
    print("Platform: native Windows bootstrap")
    print("Managed Windows runtime: not ready")
    if remote_requested:
        print(
            "Remote preflight: not run; install the managed runtime first, "
            "then retry the same command."
        )
    print("Next: run 'railmux runtime install' from an interactive PowerShell.")
    return 2


def _confirm_prune(*, input_fn: Callable[[str], str] = input) -> bool:
    try:
        answer = input_fn(
            "Remove only the listed inactive Railmux app layers and selected "
            "private caches? Provider sessions and histories are outside "
            "these paths [y/N] "
        )
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in {"y", "yes"}


def _confirm_uninstall(*, input_fn: Callable[[str], str] = input) -> bool:
    try:
        answer = input_fn(
            "Remove Railmux's private MSYS2, tmux, Python, application "
            "layers, and package caches? Codex/Claude session histories and "
            "user-owned MSYS2 installations are outside these paths [y/N] "
        )
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in {"y", "yes"}


def _uninstall(
    *,
    environ: Mapping[str, str],
    dry_run: bool,
    assume_yes: bool,
    input_fn: Callable[[str], str],
) -> int:
    try:
        plan = plan_managed_runtime_uninstall(environ=environ)
    except RuntimeInstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("Railmux Windows runtime uninstall")
    print(f"  remove private runtime: {plan.root}")
    if plan.cache is not None:
        print("  remove private download/package caches")
    print("  retain: Codex/Claude histories, Railmux workspace state, install logs")
    if dry_run:
        print("Dry run only; nothing was removed.")
        return 0
    if not assume_yes and not _confirm_uninstall(input_fn=input_fn):
        print("Runtime uninstall cancelled.", file=sys.stderr)
        return 2
    try:
        apply_managed_runtime_uninstall(plan, environ=environ)
    except RuntimeInstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("Removed the private MSYS2, tmux, Python, and Railmux app layers.")
    print("Codex and Claude session files were not accessed.")
    print("You may now run 'pip uninstall railmux' to remove the native launcher.")
    return 0


def _prune(
    *,
    environ: Mapping[str, str],
    dry_run: bool,
    assume_yes: bool,
    include_caches: bool,
    input_fn: Callable[[str], str],
) -> int:
    try:
        plan = plan_managed_runtime_prune(
            version=__version__,
            environ=environ,
            include_caches=include_caches,
        )
    except RuntimeInstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("Railmux Windows runtime cleanup")
    for application in plan.remove_apps:
        print(f"  remove app layer: {application.name}")
    if plan.pip_cache is not None:
        print("  clear private pip download cache")
    print("  retain: " + ", ".join(plan.retained_apps))
    if plan.empty:
        print("Nothing is eligible for removal.")
        return 0
    if dry_run:
        print("Dry run only; nothing was removed.")
        return 0
    if not assume_yes and not _confirm_prune(input_fn=input_fn):
        print("Runtime cleanup cancelled.", file=sys.stderr)
        return 2
    try:
        applied = apply_managed_runtime_prune(
            plan,
            version=__version__,
            environ=environ,
        )
    except RuntimeInstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"Removed {len(applied.remove_apps)} inactive app layer(s)"
        + (" and cleared the private pip cache." if applied.pip_cache else ".")
    )
    print("Codex and Claude session files were not accessed.")
    return 0


def _confirm_install(*, input_fn: Callable[[str], str] = input) -> bool:
    try:
        answer = input_fn(
            "No reusable MSYS2 base was found. Install the private MSYS2/tmux "
            "runtime now? On Windows, Railmux "
            "depends on a complete private MSYS2 compatibility wrapper, "
            "including tmux and Python. This downloads a 50 MB base plus "
            "required updates and packages, "
            "and uses about 700 MB or more of private disk space [y/N] "
        )
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in {"y", "yes"}


def _install(
    *,
    environ: Mapping[str, str],
    assume_yes: bool,
    verbose: bool = False,
    input_fn: Callable[[str], str] = input,
) -> Msys2Runtime | None:
    reuse_only = False
    if not assume_yes:
        reusable = reusable_managed_base_candidate(environ)
        if reusable is None:
            if not _confirm_install(input_fn=input_fn):
                print("MSYS2 runtime installation cancelled.", file=sys.stderr)
                return None
        else:
            print(
                f"Reusing the matching MSYS2 {MSYS2_RELEASE} private base"
                f"; only the Railmux {__version__} app layer will be "
                "installed."
            )
            reuse_only = True
    try:
        runtime = install_managed_runtime(
            version=__version__,
            environ=environ,
            verbose=verbose,
            reuse_only=reuse_only,
        )
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
            "error: Railmux for Windows requires Python 3.10 or newer",
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
    if arguments[:2] == ["runtime", "status"]:
        status_flags = arguments[2:]
        if (
            len(status_flags) != len(set(status_flags))
            or any(flag not in {"--json", "--verify"} for flag in status_flags)
        ):
            print(
                "error: usage: railmux runtime status [--json] [--verify]",
                file=sys.stderr,
            )
            return 2
        return _runtime_status(
            runtime,
            environ=environ,
            json_output="--json" in status_flags,
            verify="--verify" in status_flags,
        )
    if arguments and arguments[0] == "doctor" and runtime is None:
        json_output = "--json" in arguments[1:]
        return _native_doctor_missing_runtime(
            environ=environ,
            json_output=json_output,
            remote_requested="--remote" in arguments[1:],
        )
    if arguments and arguments[0] == "runtime":
        install_arguments = arguments[1:]
        if install_arguments[:1] == ["uninstall"]:
            uninstall_flags = install_arguments[1:]
            if (
                len(uninstall_flags) != len(set(uninstall_flags))
                or any(flag not in {"--dry-run", "--yes"} for flag in uninstall_flags)
                or "--dry-run" in uninstall_flags and "--yes" in uninstall_flags
            ):
                print(
                    "error: usage: railmux runtime uninstall "
                    "[--dry-run] [--yes]",
                    file=sys.stderr,
                )
                return 2
            return _uninstall(
                environ=environ,
                dry_run="--dry-run" in uninstall_flags,
                assume_yes="--yes" in uninstall_flags,
                input_fn=input_fn,
            )
        if install_arguments[:1] == ["prune"]:
            prune_flags = install_arguments[1:]
            if (
                len(prune_flags) != len(set(prune_flags))
                or any(
                    flag not in {"--dry-run", "--yes", "--caches"}
                    for flag in prune_flags
                )
                or "--dry-run" in prune_flags and "--yes" in prune_flags
            ):
                print(
                    "error: usage: railmux runtime prune "
                    "[--dry-run] [--yes] [--caches]",
                    file=sys.stderr,
                )
                return 2
            return _prune(
                environ=environ,
                dry_run="--dry-run" in prune_flags,
                assume_yes="--yes" in prune_flags,
                include_caches="--caches" in prune_flags,
                input_fn=input_fn,
            )
        install_flags = install_arguments[1:] if install_arguments[:1] == ["install"] else []
        if (
            not install_arguments
            or install_arguments[0] != "install"
            or len(install_flags) != len(set(install_flags))
            or any(flag not in {"--yes", "--verbose"} for flag in install_flags)
        ):
            print(
                "error: usage: railmux runtime install [--yes] [--verbose]",
                file=sys.stderr,
            )
            return 2
        runtime = runtime or _install(
            environ=environ,
            assume_yes="--yes" in install_flags,
            verbose="--verbose" in install_flags,
            input_fn=input_fn,
        )
        return 0 if runtime is not None else 2

    if runtime is None:
        root = managed_root(environ)
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
        runtime = _install(
            environ=environ,
            assume_yes=False,
            verbose=False,
            input_fn=input_fn,
        )
        if runtime is None:
            return 2

    return _wait_for_runtime(
        runtime,
        arguments,
        environ=environ,
        popen=popen,
    )
