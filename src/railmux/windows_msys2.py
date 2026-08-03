"""Managed MSYS2 runtime discovery, installation, and safe handoff.

This module is imported by native Windows Python only.  The managed runtime
hosts the existing POSIX Railmux/tmux stack; provider programs and their data
remain Windows-native.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


MSYS2_RELEASE = "2026-03-22"
MSYS2_ARCHIVE_NAME = f"msys2-base-x86_64-{MSYS2_RELEASE.replace('-', '')}.sfx.exe"
MSYS2_ARCHIVE_URL = (
    "https://github.com/msys2/msys2-installer/releases/download/"
    f"{MSYS2_RELEASE}/{MSYS2_ARCHIVE_NAME}"
)
MSYS2_ARCHIVE_SHA256 = (
    "6fe0cc8154132040e034ff4daface2a4163a9d1f6ebaaa1133394bff460bd5cf"
)
MSYS2_RUNTIME_ID = f"msys2-{MSYS2_RELEASE}"
RUNTIME_SCHEMA = 1

_RUNTIME_OVERRIDE = "RAILMUX_MSYS2_ROOT"
_RUNTIME_MARKER = "railmux-runtime.json"
_RAILMUX_EXECUTABLE = "/opt/railmux/venv/bin/railmux"
_RAILMUX_PYTHON = "/opt/railmux/venv/bin/python"
_HANDOFF_COMMAND = (
    'unset MSYS2_ARG_CONV_EXCL; exec /opt/railmux/venv/bin/railmux "$@"'
)
_PROBE_TIMEOUT_SECONDS = 15.0
_DOWNLOAD_LIMIT = 128 * 1024 * 1024
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*(?:\.dev[0-9]+)?\Z")

_INITIAL_UPDATE_COMMAND = (
    "pacman-key --init && pacman-key --populate msys2 && "
    "pacman -Syu --noconfirm"
)
_PACKAGE_INSTALL_COMMAND = (
    "pacman -Syu --noconfirm --needed tmux python python-pip"
)
_VENV_COMMAND = "python -m venv /opt/railmux/venv"
_PACKAGE_COMMAND = (
    f'{_RAILMUX_PYTHON} -m pip install --disable-pip-version-check '
    '--no-cache-dir --only-binary=:all: "railmux[ssh]==$1"'
)


class RuntimeErrorBase(RuntimeError):
    """Safe user-facing managed-runtime failure."""


class RuntimeInstallError(RuntimeErrorBase):
    """A managed runtime could not be installed transactionally."""


Probe = Callable[..., subprocess.CompletedProcess[bytes]]
Runner = Callable[..., subprocess.CompletedProcess]
Downloader = Callable[[str, Path, str], None]


@dataclass(frozen=True)
class Msys2Runtime:
    root: Path
    managed: bool

    @property
    def bash(self) -> Path:
        return self.root / "usr" / "bin" / "bash.exe"

    def environment(self, environ: Mapping[str, str]) -> dict[str, str]:
        """Build the child-only environment without editing Windows state."""
        child = dict(environ)
        existing_path = child.get("PATH", "")
        child["PATH"] = str(self.root / "usr" / "bin") + (
            os.pathsep + existing_path if existing_path else ""
        )
        user_profile = child.get("USERPROFILE", "").strip()
        if user_profile:
            # MSYS converts this Windows path for POSIX children and back for
            # native provider children.  It keeps ~/.codex and ~/.claude on
            # the same Windows-owned authority used outside Railmux.
            child["HOME"] = user_profile
        child["SHELL"] = "/usr/bin/bash"
        child["MSYSTEM"] = "MSYS"
        child["MSYS2_PATH_TYPE"] = "inherit"
        # Preserve bootstrap argv exactly at the Windows -> MSYS boundary.
        # The fixed handoff command unsets this before Railmux launches native
        # providers, whose POSIX cwd arguments do require MSYS conversion.
        child["MSYS2_ARG_CONV_EXCL"] = "*"
        child["RAILMUX_WINDOWS_RUNTIME"] = "msys2"
        # Native PowerShell commonly has no TERM; OpenSSH-hosted PowerShell
        # can explicitly inherit ``dumb``, which makes tmux reject attach.
        if not child.get("TERM") or child.get("TERM") == "dumb":
            child["TERM"] = "xterm-256color"
        if child.get("WT_SESSION"):
            child.setdefault("COLORTERM", "truecolor")
        child["LANG"] = "C.UTF-8"
        child["LC_ALL"] = "C.UTF-8"
        child["PYTHONUTF8"] = "1"
        child.pop("TMUX", None)
        child.pop("TMUX_PANE", None)
        return child

    def argv(self, arguments: Sequence[str]) -> list[str]:
        return [
            str(self.bash),
            "--noprofile",
            "--norc",
            "-c",
            _HANDOFF_COMMAND,
            "railmux",
            *arguments,
        ]


def _managed_base(environ: Mapping[str, str]) -> Path | None:
    local_app_data = environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return None
    return Path(local_app_data) / "Railmux" / "runtimes"


def managed_root(environ: Mapping[str, str], *, version: str) -> Path | None:
    base = _managed_base(environ)
    return (
        None
        if base is None
        else base / MSYS2_RUNTIME_ID / f"railmux-{version}"
    )


def _probe(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float = _PROBE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(argv),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def _marker_matches(root: Path, *, version: str) -> bool:
    try:
        raw = (root / _RUNTIME_MARKER).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return data == {
        "schema": RUNTIME_SCHEMA,
        "runtime": MSYS2_RUNTIME_ID,
        "railmux": version,
    }


def probe_runtime(
    runtime: Msys2Runtime,
    *,
    version: str,
    environ: Mapping[str, str],
    probe: Probe = _probe,
) -> bool:
    if not runtime.bash.is_file():
        return False
    if runtime.managed and not _marker_matches(runtime.root, version=version):
        return False
    for _attempt in range(2):
        try:
            result = probe(
                runtime.argv(["--version"]),
                env=runtime.environment(environ),
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode:
            continue
        output = result.stdout.decode("utf-8", errors="replace").strip()
        if output == f"railmux {version}":
            return True
    return False


def find_runtime(
    *,
    version: str,
    environ: Mapping[str, str],
    probe: Probe = _probe,
) -> Msys2Runtime | None:
    requested = environ.get(_RUNTIME_OVERRIDE, "").strip()
    if requested:
        candidate = Msys2Runtime(Path(requested), managed=False)
        return (
            candidate
            if probe_runtime(candidate, version=version, environ=environ, probe=probe)
            else None
        )
    root = managed_root(environ, version=version)
    if root is None:
        return None
    candidate = Msys2Runtime(root, managed=True)
    return (
        candidate
        if probe_runtime(candidate, version=version, environ=environ, probe=probe)
        else None
    )


def download_verified(url: str, destination: Path, sha256: str) -> None:
    """Download one bounded artifact and verify it before execution."""
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            with destination.open("xb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _DOWNLOAD_LIMIT:
                        raise RuntimeInstallError("MSYS2 download exceeded its size limit")
                    digest.update(chunk)
                    output.write(chunk)
    except RuntimeInstallError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeInstallError("could not download the pinned MSYS2 runtime") from exc
    if digest.hexdigest().lower() != sha256.lower():
        raise RuntimeInstallError("the downloaded MSYS2 archive failed SHA-256 verification")


def _run_checked(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    runner: Runner,
    label: str,
) -> None:
    try:
        result = runner(list(argv), env=dict(env), check=False)
    except OSError as exc:
        raise RuntimeInstallError(f"could not start {label}") from exc
    if result.returncode:
        raise RuntimeInstallError(f"{label} failed with exit code {result.returncode}")


def _bash_command(root: Path, command: str, *arguments: str) -> list[str]:
    return [
        str(root / "usr" / "bin" / "bash.exe"),
        "--noprofile",
        "--norc",
        "-c",
        command,
        "railmux-install",
        *arguments,
    ]


def _write_marker(root: Path, *, version: str) -> None:
    marker = root / _RUNTIME_MARKER
    temporary = marker.with_suffix(".tmp")
    payload = json.dumps(
        {
            "schema": RUNTIME_SCHEMA,
            "runtime": MSYS2_RUNTIME_ID,
            "railmux": version,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    temporary.write_text(payload + "\n", encoding="utf-8")
    os.replace(temporary, marker)


@contextmanager
def install_lock(base: Path, *, timeout: float = 120.0) -> Iterator[None]:
    """Serialize installers; Windows releases the byte lock after a crash."""
    import msvcrt

    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / ".install.lock"
    with lock_path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeInstallError(
                        "another Railmux runtime installation is still running"
                    ) from None
                time.sleep(0.2)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def install_managed_runtime(
    *,
    version: str,
    environ: Mapping[str, str],
    downloader: Downloader = download_verified,
    runner: Runner = subprocess.run,
    probe: Probe = _probe,
    lock_factory: Callable[[Path], object] = install_lock,
) -> Msys2Runtime:
    """Install a fresh private runtime and activate it only after verification."""
    if not _VERSION_RE.fullmatch(version):
        raise RuntimeInstallError("the Railmux package version is invalid")
    if environ.get(_RUNTIME_OVERRIDE, "").strip():
        raise RuntimeInstallError(
            "RAILMUX_MSYS2_ROOT selects a user-owned runtime; Railmux will "
            "not install or modify it"
        )
    base = _managed_base(environ)
    if base is None:
        raise RuntimeInstallError("LOCALAPPDATA is unavailable")
    base.mkdir(parents=True, exist_ok=True)
    final_root = managed_root(environ, version=version)
    assert final_root is not None
    final_root.parent.mkdir(parents=True, exist_ok=True)

    with lock_factory(base):
        existing = find_runtime(version=version, environ=environ, probe=probe)
        if existing is not None:
            return existing
        if final_root.exists():
            if _marker_matches(final_root, version=version):
                raise RuntimeInstallError(
                    "the managed MSYS2 runtime is present but could not be "
                    "verified; retry after checking antivirus or disk load"
                )
            raise RuntimeInstallError(
                "the managed MSYS2 directory exists but is incomplete; "
                "it was left untouched"
            )

        with tempfile.TemporaryDirectory(prefix=".install-", dir=base) as raw_stage:
            stage = Path(raw_stage)
            archive = stage / MSYS2_ARCHIVE_NAME
            print(f"Downloading verified MSYS2 {MSYS2_RELEASE} runtime…")
            downloader(MSYS2_ARCHIVE_URL, archive, MSYS2_ARCHIVE_SHA256)

            _run_checked(
                [str(archive), "-y", f"-o{stage}"],
                env=environ,
                runner=runner,
                label="MSYS2 extraction",
            )
            root = stage / "msys64"
            if not (root / "usr" / "bin" / "bash.exe").is_file():
                raise RuntimeInstallError("the verified MSYS2 archive was incomplete")
            runtime = Msys2Runtime(root, managed=False)
            child_env = runtime.environment(environ)

            print("Updating the private MSYS2 base…")
            _run_checked(
                _bash_command(root, _INITIAL_UPDATE_COMMAND),
                env=child_env,
                runner=runner,
                label="MSYS2 base update",
            )
            # A new process is required after msys2-runtime/bash updates.
            child_env = runtime.environment(environ)
            print("Installing tmux and the private Python runtime…")
            _run_checked(
                _bash_command(root, _PACKAGE_INSTALL_COMMAND),
                env=child_env,
                runner=runner,
                label="MSYS2 package installation",
            )
            _run_checked(
                _bash_command(root, _VENV_COMMAND),
                env=child_env,
                runner=runner,
                label="Railmux virtual environment creation",
            )
            print(f"Installing Railmux {version} into the private runtime…")
            _run_checked(
                _bash_command(root, _PACKAGE_COMMAND, version),
                env=child_env,
                runner=runner,
                label="Railmux runtime package installation",
            )
            _write_marker(root, version=version)
            staged_runtime = Msys2Runtime(root, managed=True)
            if not probe_runtime(
                staged_runtime,
                version=version,
                environ=environ,
                probe=probe,
            ):
                raise RuntimeInstallError("the staged Railmux runtime failed validation")
            os.replace(root, final_root)

        installed = Msys2Runtime(final_root, managed=True)
        if not probe_runtime(
            installed,
            version=version,
            environ=environ,
            probe=probe,
        ):
            raise RuntimeInstallError("the activated Railmux runtime failed validation")
        return installed
