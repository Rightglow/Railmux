#!/usr/bin/env python3
"""Install one built Railmux wheel into an isolated prefix and smoke its CLIs."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"{' '.join(argv)} failed ({result.returncode}):\n"
            f"{result.stdout}{result.stderr}"
        )
    return result.stdout


def _installed_paths(prefix: Path) -> tuple[Path, Path]:
    """Locate the wheel's import root and console script under a pip prefix."""
    package_roots = sorted({
        path
        for leaf in ("site-packages", "dist-packages")
        for path in prefix.rglob(leaf)
        if path.is_dir() and (path / "railmux").is_dir()
    })
    scripts = sorted(
        path
        for executable in ("railmux", "railmux.exe")
        for path in prefix.rglob(executable)
        if (
            path.is_file()
            and path.parent.name.lower() in {"bin", "scripts"}
        )
    )
    if len(package_roots) != 1:
        raise RuntimeError(
            "isolated wheel install did not create one Railmux package root"
        )
    if len(scripts) != 1:
        raise RuntimeError(
            "isolated wheel install did not create one Railmux console script"
        )
    return package_roots[0], scripts[0]


def _install_argv(python: str, prefix: Path, wheel: Path) -> list[str]:
    """Install into *prefix* without mutating the invoking environment."""
    return [
        python,
        "-m",
        "pip",
        "install",
        "--prefix",
        str(prefix),
        # A prefix is isolated for imports, but pip can still uninstall
        # matching packages from the invoking virtualenv during a forced
        # reinstall. Ignore that environment and populate the prefix only.
        "--ignore-installed",
        f"{wheel}[ssh]",
    ]


def _user_state_snapshot(env: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """Return only paths beneath user-state roots, never their contents."""
    snapshot = {}
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"):
        root = Path(env[name])
        snapshot[name] = tuple(sorted(
            str(path.relative_to(root)) + ("/" if path.is_dir() else "")
            for path in root.rglob("*")
        ))
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        parser.error("wheel must name one built .whl file")

    with tempfile.TemporaryDirectory(prefix="railmux-wheel-smoke-") as raw:
        root = Path(raw)
        prefix = root / "install"
        env = dict(os.environ)
        env["HOME"] = str(root / "home")
        env["XDG_CONFIG_HOME"] = str(root / "config")
        env["XDG_RUNTIME_DIR"] = str(root / "runtime")
        Path(env["HOME"]).mkdir()
        Path(env["XDG_CONFIG_HOME"]).mkdir()
        Path(env["XDG_RUNTIME_DIR"]).mkdir(mode=0o700)

        _run(
            _install_argv(sys.executable, prefix, wheel),
            cwd=root,
            env=env,
        )
        site_packages, railmux = _installed_paths(prefix)
        # The wheel and its declared SSH dependencies live under the isolated
        # prefix. Put that prefix first so Railmux itself cannot resolve to an
        # editable source checkout or inherit undeclared CI dependencies.
        env["PYTHONPATH"] = str(site_packages)
        imported = json.loads(_run(
            [
                sys.executable,
                "-c",
                (
                    "import json, pathlib, railmux, "
                    "railmux.fast_display_client, railmux.remote_config, pyte; "
                    "print(json.dumps({'version': railmux.__version__, "
                    "'path': str(pathlib.Path(railmux.__file__).resolve())}))"
                ),
            ],
            cwd=root,
            env=env,
        ))
        if prefix.resolve() not in Path(imported["path"]).parents:
            raise RuntimeError(
                f"wheel import escaped isolated environment: {imported['path']}"
            )
        version_line = _run(
            [str(railmux), "--version"],
            cwd=root,
            env=env,
        ).strip()
        if version_line != f"railmux {imported['version']}":
            raise RuntimeError(f"unexpected --version output: {version_line!r}")
        before_help = _user_state_snapshot(env)
        help_commands = (
            ("--help",),
            ("config", "--help"),
            ("doctor", "--help"),
            ("ssh", "--help"),
        )
        for arguments in help_commands:
            output = _run(
                [str(railmux), *arguments],
                cwd=root,
                env=env,
            )
            if "usage:" not in output.lower():
                raise RuntimeError(
                    f"{' '.join(arguments)} did not render command help"
                )
        if _user_state_snapshot(env) != before_help:
            raise RuntimeError("help commands created user configuration or state")
        doctor = json.loads(_run(
            [str(railmux), "doctor", "--json"],
            cwd=root,
            env=env,
        ))
        if doctor.get("schema_version") != 3:
            raise RuntimeError("wheel doctor emitted an unexpected schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
