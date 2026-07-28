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
        Path(env["XDG_RUNTIME_DIR"]).mkdir(mode=0o700)

        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--prefix",
                str(prefix),
                "--force-reinstall",
                f"{wheel}[ssh]",
            ],
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
                    "railmux.fast_display_client, pyte; "
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
        doctor = json.loads(_run(
            [str(railmux), "doctor", "--json"],
            cwd=root,
            env=env,
        ))
        if doctor.get("schema_version") != 1:
            raise RuntimeError("wheel doctor emitted an unexpected schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
