"""Managed MSYS2 runtime discovery, installation, and safe handoff.

This module is imported by native Windows Python only.  The managed runtime
hosts the existing POSIX Railmux/tmux stack; provider programs and their data
remain Windows-native.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import importlib.metadata as importlib_metadata
import json
import lzma
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from railmux.windows_install_log import (
    InstallReporter,
    install_log_path,
    stream_process_output,
)
from railmux.windows_pacman import (
    PacmanMirrorDecision,
    PacmanMirrorError,
    deactivate_pacman_hosts,
    optimize_pacman_mirror,
    validate_transaction_package_mirrors,
    write_msys_only_pacman_config,
)
from railmux.windows_paths import (
    legacy_local_app_data_root,
    managed_windows_data_root,
)
from railmux.release_version import (
    PROJECT_VERSION_PATTERN,
    PROJECT_VERSION_RE,
    ProjectVersion,
    is_project_version,
    parse_project_version,
)
from railmux.terminal_status import STYLE_ACCENT, STYLE_MUTED, styled


MSYS2_RELEASE = "2026-03-22"
MSYS2_ARCHIVE_NAME = f"msys2-base-x86_64-{MSYS2_RELEASE.replace('-', '')}.tar.xz"
MSYS2_ARCHIVE_SOURCES = (
    (
        "GitHub",
        "https://github.com/msys2/msys2-installer/releases/download/"
        f"{MSYS2_RELEASE}/{MSYS2_ARCHIVE_NAME}",
    ),
    (
        "MSYS2 repository",
        f"https://repo.msys2.org/distrib/x86_64/{MSYS2_ARCHIVE_NAME}",
    ),
    (
        "TUNA mirror",
        f"https://mirrors.tuna.tsinghua.edu.cn/msys2/distrib/x86_64/"
        f"{MSYS2_ARCHIVE_NAME}",
    ),
    (
        "NJU mirror",
        f"https://mirror.nju.edu.cn/msys2/distrib/x86_64/{MSYS2_ARCHIVE_NAME}",
    ),
)
MSYS2_ARCHIVE_SIZE = 53_466_096
MSYS2_ARCHIVE_SHA256 = (
    "6b4a986a3ec4f1e40313bdf17903a6f5c854373d4230c40f14c5e35c4bac7fce"
)
MSYS2_ARCHIVE_MEMBER_COUNT = 16_485
MSYS2_ARCHIVE_UNPACKED_SIZE = 289_361_533
# Schema-1 base-content markers released in dev24/dev25 use the SFX digest as
# their pinned release-lineage token.  Keep accepting and writing that durable
# value while the bootstrap consumes the equivalent official tar.xz artifact.
MSYS2_BASE_LINEAGE_SHA256 = (
    "6fe0cc8154132040e034ff4daface2a4163a9d1f6ebaaa1133394bff460bd5cf"
)
MSYS2_RUNTIME_ID = f"msys2-{MSYS2_RELEASE}"
_NATIVE_WINDOWS = os.name == "nt"
RUNTIME_SCHEMA = 1

_RUNTIME_OVERRIDE = "RAILMUX_MSYS2_ROOT"
_RUNTIME_MARKER = "railmux-runtime.json"
_BASE_MARKER = "railmux-base.json"
_BASE_CONTENT_MARKER = "railmux-base-content-v1.json"
_APP_MARKER = "railmux-app.json"
_APP_ROOT = "/opt/railmux/apps"
_LEGACY_RAILMUX_EXECUTABLE = "/opt/railmux/venv/bin/railmux"
_HANDOFF_COMMAND = (
    'unset MSYS2_ARG_CONV_EXCL; executable=$1; shift; exec "$executable" "$@"'
)
_PROBE_TIMEOUT_SECONDS = 15.0
_DOWNLOAD_LIMIT = 128 * 1024 * 1024
_DOWNLOAD_LOG_STEP = 8 * 1024 * 1024
_DOWNLOAD_PROBE_BYTES = 1024 * 1024
_DOWNLOAD_PROBE_SECONDS = 8.0
_DOWNLOAD_PROBE_READ_SIZE = 64 * 1024
_DOWNLOAD_PROBE_TIMEOUT = 10.0
_DOWNLOAD_SLOW_REMAINING_SECONDS = 60.0
_DOWNLOAD_SWITCH_RATIO = 1.25
_CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z")
_VERSION_RE = PROJECT_VERSION_RE

_PACMAN_CONFIG = "/etc/railmux-pacman.conf"
_PACMAN_PACKAGES = "tmux python python-pip"
_PIP_CACHE_NAME = "pip"
_PIP_INITIAL_TIMEOUT_SECONDS = 60
_PIP_RETRY_TIMEOUT_SECONDS = 120
_PIP_INITIAL_RETRIES = 5
_PIP_RECOVERY_RETRIES = 5
_PACKAGE_URL_COMMAND = (
    f"pacman --config {_PACMAN_CONFIG} -Sp --print-format '%l' "
    f"--needed {_PACMAN_PACKAGES} 2>/dev/null"
)
_PACKAGE_INVENTORY_COMMAND = "set -o pipefail; pacman -Q | LC_ALL=C sort"
_PACKAGE_INVENTORY_LIMIT = 1024 * 1024
_CORE_RUNTIME_PACKAGES = ("tmux", "python", "python-pip")
_MAX_BASE_UPDATE_PASSES = 3
_PACMAN_LOG_RELATIVE = Path("var") / "log" / "pacman.log"
_PACMAN_LOG_MAX_SIZE = 8 * 1024 * 1024
_PACMAN_LOG_APPEND_LIMIT = 2 * 1024 * 1024
_PACMAN_LOCAL_DB_ENTRY_LIMIT = 4096
_PACMAN_LOCAL_DESC_LIMIT = 64 * 1024
# Keep this aligned with alpm_pkg_is_core_package in the pacman build shipped
# by the pinned base. Any one of these packages makes MSYS2 close its processes.
_PACMAN_CORE_PACKAGES = frozenset(
    {
        "bash",
        "filesystem",
        "mintty",
        "msys2-runtime",
        "msys2-runtime-devel",
        "pacman",
        "pacman-mirrors",
    }
)
_PACMAN_CORE_CHANGE_RE = re.compile(
    r"\[ALPM] (?:upgraded|downgraded) ([^ ]+) \(([^ ]+) -> ([^)]+)\)",
    re.IGNORECASE,
)
_PRIVATE_GPG_SHUTDOWN_COMMAND = (
    "gpgconf --homedir /etc/pacman.d/gnupg --kill all"
)
_IN_USE_APPS_COMMAND = (
    "for f in /proc/[0-9]*/cmdline; do "
    "[ -r \"$f\" ] || continue; tr '\\0' '\\n' <\"$f\"; done"
)
_VENV_COMMAND = 'python -m venv "$1/venv"'
_PACKAGE_COMMAND = (
    'cache=$(cygpath -u "$3") && mkdir -p "$cache" && '
    '"$1/venv/bin/python" -m pip install --disable-pip-version-check '
    '--cache-dir "$cache" --only-binary=:all: '
    '--timeout "$4" --retries "$5" "railmux[ssh]==$2"'
)
_LOCAL_APP_CHECK_COMMAND = (
    'chmod 755 "$1/venv/bin/railmux" && '
    '"$1/venv/bin/python" -m pip check && '
    '"$1/venv/bin/python" -c '
    "'import packaging, pyte, tomlkit, typing_extensions, urwid, wcwidth, "
    "railmux, sys; assert railmux.__version__ == sys.argv[1]' \"$2\""
)
_LOCAL_APP_COPY_FILE_LIMIT = 20_000
_LOCAL_APP_COPY_SIZE_LIMIT = 128 * 1024 * 1024
_DEPENDENCY_SEED_NAMES = frozenset(
    {"packaging", "pyte", "tomlkit", "typing_extensions.py", "urwid", "wcwidth"}
)
_DEPENDENCY_DIST_INFO_RE = re.compile(
    r"(?:packaging|pyte|tomlkit|typing_extensions|urwid|wcwidth)-.+\.dist-info\Z",
    re.IGNORECASE,
)


class RuntimeErrorBase(RuntimeError):
    """Safe user-facing managed-runtime failure."""


class RuntimeInstallError(RuntimeErrorBase):
    """A managed runtime could not be installed transactionally."""


@dataclass(frozen=True)
class BaseContentIdentity:
    """Exact package content bound to one otherwise immutable shared base."""

    content_id: str
    package_count: int
    core_packages: Mapping[str, str]

    def marker(self) -> dict[str, object]:
        return {
            "schema": 1,
            "runtime": MSYS2_RUNTIME_ID,
            "archive_sha256": MSYS2_BASE_LINEAGE_SHA256,
            "content_id": self.content_id,
            "package_count": self.package_count,
            "core_packages": dict(self.core_packages),
        }


@dataclass(frozen=True)
class RuntimePrunePlan:
    """Bounded, marker-validated managed files eligible for explicit removal."""

    root: Path
    remove_apps: tuple[Path, ...]
    retained_apps: tuple[str, ...]
    pip_cache: Path | None = None

    @property
    def empty(self) -> bool:
        return not self.remove_apps and self.pip_cache is None


Probe = Callable[..., subprocess.CompletedProcess[bytes]]
Runner = Callable[..., subprocess.CompletedProcess]
Downloader = Callable[[str, Path, str], None]
MirrorOptimizer = Callable[[Path], PacmanMirrorDecision]
Extractor = Callable[..., None]


@dataclass(frozen=True)
class _ArchiveProbe:
    label: str
    url: str
    data: bytes
    elapsed: float

    @property
    def rate(self) -> float:
        return len(self.data) / max(self.elapsed, 0.001)


ArchiveProbe = Callable[[str, str, int], _ArchiveProbe]
ArchiveResume = Callable[[str, str, int, int, Callable[[bytes], None]], None]


@dataclass(frozen=True)
class _PacmanLogCheckpoint:
    existed: bool
    size: int
    identity: tuple[int, int] | None
    tail_offset: int
    tail_digest: str
    core_versions: tuple[tuple[str, str], ...]


def _format_download_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size} B"


class _DownloadProgress:
    """Render bounded progress without flooding redirected logs."""

    def __init__(self, url: str) -> None:
        self._stream = sys.stderr
        self._source = urllib.parse.urlsplit(url).hostname or "approved source"
        self._interactive = bool(getattr(self._stream, "isatty", lambda: False)())
        self._last_logged = 0
        self._active_line = False

    def _message(self, downloaded: int, expected: int | None) -> str:
        source = styled(self._source, STYLE_ACCENT, stream=self._stream)
        current = styled(
            _format_download_size(downloaded), STYLE_MUTED, stream=self._stream
        )
        if expected:
            percent = min(100.0, downloaded * 100.0 / expected)
            total = styled(
                _format_download_size(expected), STYLE_MUTED, stream=self._stream
            )
            return (
                f"  {source}: {current} / {total} "
                f"({styled(f'{percent:.1f}%', STYLE_ACCENT, stream=self._stream)})"
            )
        return f"  {source}: {current} downloaded"

    def update(
        self,
        downloaded: int,
        expected: int | None,
        *,
        final: bool = False,
    ) -> None:
        message = self._message(downloaded, expected)
        if self._interactive:
            self._stream.write(f"\r{message}")
            self._active_line = not final
            if final:
                self._stream.write("\n")
            self._stream.flush()
            return
        if final or downloaded - self._last_logged >= _DOWNLOAD_LOG_STEP:
            self._stream.write(f"{message}\n")
            self._stream.flush()
            self._last_logged = downloaded

    def abort(self) -> None:
        if self._interactive and self._active_line:
            self._stream.write("\n")
            self._stream.flush()
            self._active_line = False


def _format_download_rate(rate: float) -> str:
    return f"{_format_download_size(int(rate))}/s"


def _validate_https_response(url: str, response: object) -> None:
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise RuntimeInstallError("the MSYS2 runtime source did not use HTTPS")
    final_url = getattr(response, "geturl", lambda: url)()
    if urllib.parse.urlsplit(final_url).scheme.lower() != "https":
        raise RuntimeInstallError(
            "the MSYS2 runtime source redirected outside HTTPS"
        )


def _validate_range_response(
    url: str,
    response: object,
    *,
    start: int,
    end: int,
    expected_size: int,
) -> None:
    _validate_https_response(url, response)
    if getattr(response, "status", None) != 206:
        raise RuntimeInstallError("the MSYS2 source did not honor a range request")
    raw_range = response.headers.get("Content-Range", "")
    match = _CONTENT_RANGE_RE.fullmatch(raw_range)
    if match is None:
        raise RuntimeInstallError("the MSYS2 source returned an invalid content range")
    actual_start, actual_end, actual_size = (int(value) for value in match.groups())
    if (
        actual_start != start
        or actual_end != end
        or actual_size != expected_size
    ):
        raise RuntimeInstallError("the MSYS2 source returned the wrong content range")
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        try:
            actual_length = int(raw_length)
        except (TypeError, ValueError):
            actual_length = -1
        if actual_length != end - start + 1:
            raise RuntimeInstallError("the MSYS2 source returned the wrong range size")


def _range_request(url: str, *, start: int, end: int) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept-Encoding": "identity",
            "Range": f"bytes={start}-{end}",
            "User-Agent": "Railmux-MSYS2-bootstrap/1",
        },
    )


def _probe_archive_source(
    label: str,
    url: str,
    expected_size: int,
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
    clock: Callable[[], float] = time.monotonic,
) -> _ArchiveProbe:
    """Read a bounded prefix used both for selection and final assembly."""
    end = min(_DOWNLOAD_PROBE_BYTES, expected_size) - 1
    request = _range_request(url, start=0, end=end)
    started = clock()
    data = bytearray()
    try:
        with opener(request, timeout=_DOWNLOAD_PROBE_TIMEOUT) as response:
            _validate_range_response(
                url,
                response,
                start=0,
                end=end,
                expected_size=expected_size,
            )
            while len(data) <= end:
                remaining = end + 1 - len(data)
                try:
                    chunk = response.read(
                        min(_DOWNLOAD_PROBE_READ_SIZE, remaining)
                    )
                except (OSError, http.client.HTTPException):
                    if data:
                        break
                    raise
                if not chunk:
                    break
                data.extend(chunk)
                if clock() - started >= _DOWNLOAD_PROBE_SECONDS:
                    break
    except RuntimeInstallError:
        raise
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise RuntimeInstallError("could not probe the MSYS2 source") from exc
    if not data:
        raise RuntimeInstallError("the MSYS2 source returned no probe data")
    return _ArchiveProbe(
        label=label,
        url=url,
        data=bytes(data),
        elapsed=max(clock() - started, 0.001),
    )


def _resume_archive_source(
    _label: str,
    url: str,
    offset: int,
    expected_size: int,
    write_chunk: Callable[[bytes], None],
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> None:
    """Append the exact remaining range from one approved source."""
    if offset >= expected_size:
        return
    request = _range_request(url, start=offset, end=expected_size - 1)
    try:
        with opener(request, timeout=60) as response:
            _validate_range_response(
                url,
                response,
                start=offset,
                end=expected_size - 1,
                expected_size=expected_size,
            )
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                write_chunk(chunk)
    except RuntimeInstallError:
        raise
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise RuntimeInstallError("could not resume the MSYS2 source") from exc


def _probe_other_sources(
    sources: Sequence[tuple[str, str]],
    *,
    expected_size: int,
    probe_source: ArchiveProbe,
) -> list[_ArchiveProbe]:
    if not sources:
        return []
    results: list[_ArchiveProbe] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sources)) as pool:
        futures = {
            pool.submit(probe_source, label, url, expected_size): label
            for label, url in sources
        }
        for future in concurrent.futures.as_completed(futures):
            label = futures[future]
            try:
                result = future.result()
            except RuntimeInstallError as exc:
                print(f"  {label}: unavailable ({exc})", file=sys.stderr)
            else:
                results.append(result)
                print(
                    f"  {label}: {_format_download_rate(result.rate)}",
                    file=sys.stderr,
                )
    return results


def _probe_is_slow(probe: _ArchiveProbe, *, expected_size: int) -> bool:
    remaining = max(0, expected_size - len(probe.data))
    return remaining / probe.rate > _DOWNLOAD_SLOW_REMAINING_SECONDS


def _select_probe(
    primary: _ArchiveProbe | None,
    alternatives: Sequence[_ArchiveProbe],
) -> _ArchiveProbe:
    candidates = ([primary] if primary is not None else []) + list(alternatives)
    if not candidates:
        raise RuntimeInstallError("no approved MSYS2 source could be probed")
    fastest = max(candidates, key=lambda candidate: candidate.rate)
    if (
        primary is not None
        and fastest is not primary
        and fastest.rate < primary.rate * _DOWNLOAD_SWITCH_RATIO
    ):
        return primary
    return fastest


@dataclass(frozen=True)
class Msys2Runtime:
    root: Path
    managed: bool
    app_name: str | None = None

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
        if self.managed:
            # The MSYS-side privacy exception independently verifies the
            # matching on-disk runtime marker before trusting this identifier.
            child["RAILMUX_MSYS2_RUNTIME_ID"] = MSYS2_RUNTIME_ID
            if self.app_name is not None:
                child["RAILMUX_MSYS2_APP_ID"] = self.app_name
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
        executable = (
            f"{_APP_ROOT}/{self.app_name}/venv/bin/railmux"
            if self.app_name is not None
            else _LEGACY_RAILMUX_EXECUTABLE
        )
        return [
            str(self.bash),
            "--noprofile",
            "--norc",
            "-c",
            _HANDOFF_COMMAND,
            "railmux",
            executable,
            *arguments,
        ]


def _managed_base(environ: Mapping[str, str]) -> Path | None:
    data_root = managed_windows_data_root(environ)
    if data_root is None:
        return None
    return data_root / "runtimes"


def _managed_cache(environ: Mapping[str, str]) -> Path | None:
    data_root = managed_windows_data_root(environ)
    if data_root is None:
        return None
    return data_root / "cache"


def _path_is_link_or_reparse(path: Path) -> bool:
    info = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & reparse_flag
    )


def _archive_cache_is_valid(path: Path) -> bool:
    try:
        if (
            _path_is_link_or_reparse(path)
            or path.stat().st_size != MSYS2_ARCHIVE_SIZE
        ):
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest().lower() == MSYS2_ARCHIVE_SHA256.lower()
    except OSError:
        return False


def _prepare_cached_archive(
    cache: Path,
    *,
    downloader: Downloader | None,
    prior_caches: Sequence[Path] = (),
) -> tuple[Path, str]:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / MSYS2_ARCHIVE_NAME
    if _archive_cache_is_valid(archive):
        return archive, "verified local cache"
    try:
        archive.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeInstallError("could not replace the private archive cache") from exc
    for prior_cache in prior_caches:
        prior_archive = prior_cache / MSYS2_ARCHIVE_NAME
        if prior_archive == archive or not _archive_cache_is_valid(prior_archive):
            continue
        try:
            shutil.copyfile(prior_archive, archive)
        except OSError:
            archive.unlink(missing_ok=True)
            continue
        if _archive_cache_is_valid(archive):
            return archive, "verified prior local cache"
        archive.unlink(missing_ok=True)
    source = download_from_sources(
        archive,
        MSYS2_ARCHIVE_SHA256,
        downloader=downloader,
    )
    return archive, source


def _safe_archive_parts(member: tarfile.TarInfo) -> tuple[str, ...]:
    name = member.name
    raw_parts = name.split("/")
    pure = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or not raw_parts
        or raw_parts[0] != "msys64"
    ):
        raise RuntimeInstallError(
            "the verified MSYS2 archive contained an unsafe path"
        )
    return tuple(raw_parts)


def _ensure_safe_archive_directory(root: Path, parts: Sequence[str]) -> Path:
    current = root
    for part in parts:
        candidate = current / part
        try:
            if _path_is_link_or_reparse(candidate) or not candidate.is_dir():
                raise RuntimeInstallError(
                    "the MSYS2 extraction staging directory became unsafe"
                )
        except FileNotFoundError:
            try:
                candidate.mkdir()
            except OSError as exc:
                raise RuntimeInstallError(
                    "could not create the MSYS2 extraction staging directory"
                ) from exc
            if _path_is_link_or_reparse(candidate) or not candidate.is_dir():
                raise RuntimeInstallError(
                    "the MSYS2 extraction staging directory became unsafe"
                )
        except OSError as exc:
            raise RuntimeInstallError(
                "could not inspect the MSYS2 extraction staging directory"
            ) from exc
        current = candidate
    return current


def _apply_archive_mode(path: Path, mode: int) -> None:
    if _NATIVE_WINDOWS:
        # Windows chmod maps a missing owner-write bit to the NTFS read-only
        # attribute; applying tar's POSIX 0444/0555 modes would prevent pacman
        # from replacing those files during the first base update.
        os.chmod(path, stat.S_IWRITE)
    else:
        os.chmod(path, mode)


def _extract_msys2_archive(
    archive: Path,
    destination: Path,
    *,
    reporter: InstallReporter,
) -> None:
    """Extract the pinned tar.xz without executing downloaded code."""
    reporter.command_started("MSYS2 archive extraction", progress="extract")
    seen: set[tuple[str, ...]] = set()
    directories: list[tuple[Path, int]] = []
    member_count = 0
    unpacked_size = 0
    try:
        if _path_is_link_or_reparse(destination) or not destination.is_dir():
            raise RuntimeInstallError(
                "the MSYS2 extraction staging directory is unsafe"
            )
        with tarfile.open(archive, mode="r|xz") as stream:
            for member in stream:
                member_count += 1
                if member_count > MSYS2_ARCHIVE_MEMBER_COUNT:
                    raise RuntimeInstallError(
                        "the verified MSYS2 archive member count changed"
                    )
                parts = _safe_archive_parts(member)
                if parts in seen:
                    raise RuntimeInstallError(
                        "the verified MSYS2 archive contained a duplicate path"
                    )
                seen.add(parts)
                if not (member.isdir() or member.isreg()):
                    raise RuntimeInstallError(
                        "the verified MSYS2 archive contained an unsupported link "
                        "or special file"
                    )
                parent = _ensure_safe_archive_directory(destination, parts[:-1])
                target = parent / parts[-1]
                if member.isdir():
                    directory = _ensure_safe_archive_directory(destination, parts)
                    directories.append((directory, member.mode & 0o777))
                else:
                    if member.size < 0:
                        raise RuntimeInstallError(
                            "the verified MSYS2 archive contained an invalid file"
                        )
                    unpacked_size += member.size
                    if unpacked_size > MSYS2_ARCHIVE_UNPACKED_SIZE:
                        raise RuntimeInstallError(
                            "the verified MSYS2 archive expanded beyond its "
                            "pinned size"
                        )
                    try:
                        try:
                            target.lstat()
                        except FileNotFoundError:
                            pass
                        else:
                            raise RuntimeInstallError(
                                "the MSYS2 extraction staging path already exists"
                            )
                        source = stream.extractfile(member)
                        if source is None:
                            raise RuntimeInstallError(
                                "the verified MSYS2 archive file was unreadable"
                            )
                        remaining = member.size
                        with target.open("xb") as output:
                            while remaining:
                                chunk = source.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    raise RuntimeInstallError(
                                        "the verified MSYS2 archive ended early"
                                    )
                                output.write(chunk)
                                remaining -= len(chunk)
                        _apply_archive_mode(target, member.mode & 0o777)
                    except RuntimeInstallError:
                        raise
                    except OSError as exc:
                        raise RuntimeInstallError(
                            "could not write the MSYS2 extraction staging file"
                        ) from exc
                reporter.extraction_progress(
                    member_count, MSYS2_ARCHIVE_MEMBER_COUNT
                )
        if member_count != MSYS2_ARCHIVE_MEMBER_COUNT:
            raise RuntimeInstallError(
                "the verified MSYS2 archive member count changed"
            )
        if unpacked_size != MSYS2_ARCHIVE_UNPACKED_SIZE:
            raise RuntimeInstallError(
                "the verified MSYS2 archive expanded to an unexpected size"
            )
        for directory, mode in reversed(directories):
            _apply_archive_mode(directory, mode)
    except RuntimeInstallError:
        reporter.note(
            "MSYS2 archive extraction failed; the temporary base was not "
            "published.",
            level="error",
        )
        raise
    except (OSError, lzma.LZMAError, tarfile.TarError, EOFError) as exc:
        reporter.note(
            "MSYS2 archive extraction failed; the temporary base was not "
            "published.",
            level="error",
        )
        raise RuntimeInstallError(
            "the verified MSYS2 tar.xz archive could not be extracted"
        ) from exc
    reporter.command_succeeded()


def managed_root(environ: Mapping[str, str]) -> Path | None:
    base = _managed_base(environ)
    if base is None:
        return None
    shared = _find_shared_root(base)
    return shared if shared is not None else base / "shared" / MSYS2_RUNTIME_ID


def managed_runtime_status(
    *,
    version: str,
    environ: Mapping[str, str],
    verify: bool = False,
    probe: Probe | None = None,
) -> dict[str, object]:
    """Return bounded native-bootstrap state without installing anything."""
    root = managed_root(environ)
    if root is None or not root.exists():
        return {
            "schema": 1,
            "runtime": MSYS2_RUNTIME_ID,
            "status": "not_installed",
            "current_app": False,
            "layers": [],
        }
    base_valid = _base_marker_matches(root)
    identity = _decode_base_content_marker(root) if base_valid else None
    layers = _marked_app_versions(root) if base_valid else None
    current_app = bool(layers is not None and version in layers)
    if base_valid and identity is not None:
        status = "ready" if current_app else "base_ready"
    elif base_valid:
        status = "legacy_base"
    else:
        status = "incomplete"
    result: dict[str, object] = {
        "schema": 1,
        "runtime": MSYS2_RUNTIME_ID,
        "status": status,
        "base_marker": "valid" if base_valid else "invalid",
        "content_identity": (
            identity.content_id if identity is not None else None
        ),
        "package_count": (
            identity.package_count if identity is not None else None
        ),
        "core_packages": (
            dict(identity.core_packages) if identity is not None else None
        ),
        "current_app": current_app,
        "layers": sorted(layers or (), key=_version_key, reverse=True),
    }
    if verify and identity is not None:
        try:
            observed = _collect_base_content_identity(
                root, environ=environ, probe=_probe if probe is None else probe)
        except RuntimeInstallError:
            result["content_verification"] = "unavailable"
        else:
            result["content_verification"] = (
                "match"
                if observed.content_id == identity.content_id
                else "drift"
            )
    return result


def _app_name(version: str) -> str:
    return f"railmux-{version}"


def _app_root(root: Path, *, app_name: str) -> Path:
    return root / "opt" / "railmux" / "apps" / app_name


def _app_posix(app_name: str) -> str:
    return f"{_APP_ROOT}/{app_name}"


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


def _read_json_marker(path: Path) -> object | None:
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _base_marker_matches(root: Path) -> bool:
    if root.is_symlink() or not root.is_dir():
        return False
    return _read_json_marker(root / _BASE_MARKER) == {
        "schema": RUNTIME_SCHEMA,
        "runtime": MSYS2_RUNTIME_ID,
    }


def _decode_base_content_marker(root: Path) -> BaseContentIdentity | None:
    payload = _read_json_marker(root / _BASE_CONTENT_MARKER)
    if not isinstance(payload, dict):
        return None
    content_id = payload.get("content_id")
    package_count = payload.get("package_count")
    core = payload.get("core_packages")
    if (
        payload.get("schema") != 1
        or payload.get("runtime") != MSYS2_RUNTIME_ID
        or payload.get("archive_sha256") != MSYS2_BASE_LINEAGE_SHA256
        or not isinstance(content_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", content_id) is None
        or not isinstance(package_count, int)
        or isinstance(package_count, bool)
        or not 1 <= package_count <= 4096
        or not isinstance(core, dict)
        or set(core) != set(_CORE_RUNTIME_PACKAGES)
        or any(
            not isinstance(value, str)
            or not value
            or len(value) > 128
            or any(ord(char) < 0x20 or ord(char) > 0x7e for char in value)
            for value in core.values()
        )
    ):
        return None
    return BaseContentIdentity(content_id, package_count, dict(core))


def _collect_base_content_identity(
    root: Path,
    *,
    environ: Mapping[str, str],
    probe: Probe = _probe,
) -> BaseContentIdentity:
    """Read the signed pacman database view without changing the base."""
    runtime = Msys2Runtime(root, managed=False)
    try:
        result = probe(
            _bash_command(root, _PACKAGE_INVENTORY_COMMAND),
            env=runtime.environment(environ),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeInstallError(
            "could not inventory the private MSYS2 package set"
        ) from exc
    raw = result.stdout
    if result.returncode or not isinstance(raw, bytes) or not raw:
        raise RuntimeInstallError(
            "the private MSYS2 package inventory was unavailable"
        )
    if len(raw) > _PACKAGE_INVENTORY_LIMIT:
        raise RuntimeInstallError(
            "the private MSYS2 package inventory exceeded its safety bound"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeInstallError(
            "the private MSYS2 package inventory was not UTF-8"
        ) from exc
    rows = text.splitlines()
    if not rows or rows != sorted(rows) or len(rows) > 4096:
        raise RuntimeInstallError(
            "the private MSYS2 package inventory was invalid"
        )
    packages: dict[str, str] = {}
    normalized: list[str] = []
    for row in rows:
        fields = row.split(" ", 1)
        if (
            len(fields) != 2
            or not fields[0]
            or not fields[1]
            or fields[0] in packages
            or any(ord(char) < 0x20 or ord(char) > 0x7e for char in row)
        ):
            raise RuntimeInstallError(
                "the private MSYS2 package inventory was invalid"
            )
        packages[fields[0]] = fields[1]
        normalized.append(row)
    if any(name not in packages for name in _CORE_RUNTIME_PACKAGES):
        raise RuntimeInstallError(
            "the private MSYS2 package inventory omitted a required package"
        )
    encoded = ("\n".join(normalized) + "\n").encode("ascii")
    return BaseContentIdentity(
        hashlib.sha256(encoded).hexdigest(),
        len(packages),
        {name: packages[name] for name in _CORE_RUNTIME_PACKAGES},
    )


def _ensure_base_content_identity(
    root: Path,
    *,
    environ: Mapping[str, str],
    probe: Probe,
) -> BaseContentIdentity:
    existing = _decode_base_content_marker(root)
    if existing is not None:
        return existing
    identity = _collect_base_content_identity(
        root, environ=environ, probe=probe)
    _write_json_marker(root / _BASE_CONTENT_MARKER, identity.marker())
    return identity


def _app_marker_matches(root: Path, *, app_name: str, version: str) -> bool:
    application = _app_root(root, app_name=app_name)
    if application.is_symlink() or not application.is_dir():
        return False
    payload = _read_json_marker(application / _APP_MARKER)
    if payload == {
        "schema": RUNTIME_SCHEMA,
        "runtime": MSYS2_RUNTIME_ID,
        "railmux": version,
    }:
        return True
    identity = _decode_base_content_marker(root)
    return bool(
        identity is not None
        and payload == {
            "schema": 2,
            "runtime": MSYS2_RUNTIME_ID,
            "railmux": version,
            "base_content_id": identity.content_id,
        }
    )


def _version_key(version: str) -> ProjectVersion:
    return parse_project_version(version)


def _marked_app_versions(root: Path) -> dict[str, Path] | None:
    applications = root / "opt" / "railmux" / "apps"
    try:
        _validate_managed_application_root(root)
    except RuntimeInstallError:
        return None
    try:
        candidates = list(applications.iterdir())
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    result: dict[str, Path] = {}
    for candidate in candidates:
        try:
            attributes = getattr(candidate.lstat(), "st_file_attributes", 0)
        except OSError:
            continue
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            candidate.is_symlink()
            or attributes & reparse_flag
            or not candidate.is_dir()
        ):
            continue
        prefix = "railmux-"
        if not candidate.name.startswith(prefix):
            continue
        version = candidate.name[len(prefix):]
        if (
            is_project_version(version)
            and _app_marker_matches(
                root, app_name=candidate.name, version=version)
        ):
            result[version] = candidate
    return result


def _in_use_app_names(
    root: Path,
    *,
    environ: Mapping[str, str],
    probe: Probe = _probe,
) -> frozenset[str] | None:
    """Inventory every process argv; ambiguity denies destructive pruning."""
    runtime = Msys2Runtime(root, managed=False)
    try:
        result = probe(
            _bash_command(root, _IN_USE_APPS_COMMAND),
            env=runtime.environment(environ),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or len(result.stdout) > _PACKAGE_INVENTORY_LIMIT:
        return None
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
    pattern = re.compile(
        rf"(?:^|/)opt/railmux/apps/"
        rf"(railmux-{PROJECT_VERSION_PATTERN})(?:/|$)"
    )
    return frozenset(match.group(1) for match in pattern.finditer(text))


def plan_managed_runtime_prune(
    *,
    version: str,
    environ: Mapping[str, str],
    include_caches: bool = False,
    probe: Probe = _probe,
) -> RuntimePrunePlan:
    """Build a fail-closed plan without deleting any file."""
    if not is_project_version(version):
        raise RuntimeInstallError("the Railmux package version is invalid")
    if environ.get(_RUNTIME_OVERRIDE, "").strip():
        raise RuntimeInstallError(
            "RAILMUX_MSYS2_ROOT selects a user-owned runtime; Railmux will "
            "not prune it"
        )
    root = managed_root(environ)
    cache = _managed_cache(environ)
    if root is None or cache is None or not _base_marker_matches(root):
        raise RuntimeInstallError("a complete Railmux-managed runtime was not found")
    layers = _marked_app_versions(root)
    in_use = _in_use_app_names(root, environ=environ, probe=probe)
    if layers is None or in_use is None:
        raise RuntimeInstallError(
            "could not prove which Railmux app layers are inactive; nothing "
            "was removed"
        )
    current_name = _app_name(version)
    ordered = sorted(layers, key=_version_key, reverse=True)
    retained = {current_name, *in_use}
    for candidate in ordered:
        if candidate != version:
            retained.add(_app_name(candidate))
            break
    remove = tuple(
        layers[candidate]
        for candidate in ordered
        if _app_name(candidate) not in retained
    )
    pip_cache = cache / _PIP_CACHE_NAME if include_caches else None
    if pip_cache is not None:
        _validate_pip_cache_path(pip_cache)
        if not pip_cache.exists():
            pip_cache = None
    return RuntimePrunePlan(
        root,
        remove,
        tuple(sorted(retained)),
        pip_cache,
    )


def apply_managed_runtime_prune(
    plan: RuntimePrunePlan,
    *,
    version: str,
    environ: Mapping[str, str],
    probe: Probe = _probe,
    lock_factory: Callable[[Path], object] | None = None,
) -> RuntimePrunePlan:
    """Re-plan under the install lock, then remove only unchanged candidates."""
    base = _managed_base(environ)
    if base is None:
        raise RuntimeInstallError(
            "a non-virtualized Windows user data directory is unavailable"
        )
    effective_lock = install_lock if lock_factory is None else lock_factory
    with effective_lock(base):
        current = plan_managed_runtime_prune(
            version=version,
            environ=environ,
            include_caches=plan.pip_cache is not None,
            probe=probe,
        )
        if current.root != plan.root or current.remove_apps != plan.remove_apps:
            raise RuntimeInstallError(
                "the managed runtime changed while pruning; nothing was removed"
            )
        for application in current.remove_apps:
            _validate_app_layer_path(current.root, application)
            app_name = application.name
            candidate_version = app_name.removeprefix("railmux-")
            if (
                application.is_symlink()
                or not application.is_dir()
                or not _app_marker_matches(
                    current.root,
                    app_name=app_name,
                    version=candidate_version,
                )
            ):
                raise RuntimeInstallError(
                    "an app layer changed while pruning; remaining files were "
                    "left untouched"
                )
            quarantine = application.with_name(
                f".railmux-prune-{app_name}-{secrets.token_hex(8)}")
            try:
                os.replace(application, quarantine)
            except OSError as exc:
                raise RuntimeInstallError(
                    f"could not isolate app layer {app_name}; it was left "
                    "untouched"
                ) from exc
            try:
                shutil.rmtree(quarantine)
            except OSError as exc:
                raise RuntimeInstallError(
                    f"app layer {app_name} was isolated from use but its "
                    f"cleanup did not finish: {quarantine}"
                ) from exc
        if current.pip_cache is not None:
            _validate_pip_cache_path(current.pip_cache)
            if current.pip_cache.exists():
                try:
                    shutil.rmtree(current.pip_cache)
                except OSError as exc:
                    raise RuntimeInstallError(
                        "the private pip cache could not be fully removed"
                    ) from exc
        return current


def _find_shared_root(base: Path) -> Path | None:
    canonical = base / "shared" / MSYS2_RUNTIME_ID
    if _base_marker_matches(canonical):
        return canonical
    legacy_parent = base / MSYS2_RUNTIME_ID
    candidates: list[tuple[ProjectVersion, Path]] = []
    try:
        paths = list(legacy_parent.glob("railmux-*"))
    except OSError:
        return None
    for candidate in paths:
        prefix = "railmux-"
        version = candidate.name[len(prefix) :] if candidate.name.startswith(prefix) else ""
        if (
            not candidate.is_symlink()
            and _base_marker_matches(candidate)
            and is_project_version(version)
        ):
            candidates.append((_version_key(version), candidate))
    if candidates:
        return max(candidates)[1]
    return None


def probe_runtime(
    runtime: Msys2Runtime,
    *,
    version: str,
    environ: Mapping[str, str],
    probe: Probe = _probe,
) -> bool:
    if not runtime.bash.is_file():
        return False
    if runtime.managed:
        if runtime.app_name is None:
            if not _marker_matches(runtime.root, version=version):
                return False
        elif not (
            _base_marker_matches(runtime.root)
            and _app_marker_matches(
                runtime.root, app_name=runtime.app_name, version=version
            )
        ):
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
        root = Path(requested)
        app_name = _app_name(version)
        candidates = []
        application = _app_root(root, app_name=app_name)
        if not application.is_symlink() and application.is_dir():
            candidates.append(
                Msys2Runtime(root, managed=False, app_name=app_name)
            )
        candidates.append(Msys2Runtime(root, managed=False))
        for candidate in candidates:
            if probe_runtime(
                candidate, version=version, environ=environ, probe=probe
            ):
                return candidate
        return None
    root = managed_root(environ)
    if root is None:
        return None
    candidate = Msys2Runtime(root, managed=True, app_name=_app_name(version))
    return (
        candidate
        if probe_runtime(candidate, version=version, environ=environ, probe=probe)
        else None
    )


def download_verified(url: str, destination: Path, sha256: str) -> None:
    """Download one bounded artifact and verify it before execution."""
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise RuntimeInstallError("the MSYS2 runtime source did not use HTTPS")
    digest = hashlib.sha256()
    total = 0
    expected: int | None = None
    progress = _DownloadProgress(url)
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            final_url = getattr(response, "geturl", lambda: url)()
            if urllib.parse.urlsplit(final_url).scheme.lower() != "https":
                raise RuntimeInstallError(
                    "the MSYS2 runtime source redirected outside HTTPS"
                )
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None:
                try:
                    parsed_length = int(raw_length)
                except (TypeError, ValueError):
                    parsed_length = 0
                if parsed_length > 0:
                    expected = parsed_length
            if expected is not None and expected > _DOWNLOAD_LIMIT:
                raise RuntimeInstallError("MSYS2 download exceeded its size limit")
            progress.update(0, expected)
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
                    progress.update(total, expected)
        if digest.hexdigest().lower() != sha256.lower():
            raise RuntimeInstallError(
                "the downloaded MSYS2 archive failed SHA-256 verification"
            )
    except (
        RuntimeInstallError,
        OSError,
        urllib.error.URLError,
        http.client.HTTPException,
    ) as exc:
        progress.abort()
        try:
            destination.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise RuntimeInstallError(
                "could not remove an incomplete MSYS2 download"
            ) from cleanup_error
        if isinstance(exc, RuntimeInstallError):
            raise
        raise RuntimeInstallError("could not download the pinned MSYS2 runtime") from exc
    progress.update(total, expected, final=True)


def download_adaptive(
    destination: Path,
    sha256: str,
    *,
    sources: Sequence[tuple[str, str]] = MSYS2_ARCHIVE_SOURCES,
    expected_size: int = MSYS2_ARCHIVE_SIZE,
    probe_source: ArchiveProbe = _probe_archive_source,
    resume_source: ArchiveResume = _resume_archive_source,
) -> str:
    """Select a responsive approved source, then resume across failures."""
    if not sources:
        raise RuntimeInstallError("no approved MSYS2 download source is configured")
    if expected_size <= 0 or expected_size > _DOWNLOAD_LIMIT:
        raise RuntimeInstallError("the pinned MSYS2 archive size is invalid")
    for _label, url in sources:
        if urllib.parse.urlsplit(url).scheme.lower() != "https":
            raise RuntimeInstallError("the MSYS2 runtime source did not use HTTPS")

    primary: _ArchiveProbe | None = None
    remaining_sources = list(sources[1:])
    primary_label, primary_url = sources[0]
    print(f"Probing {primary_label}…", flush=True)
    try:
        primary = probe_source(primary_label, primary_url, expected_size)
    except RuntimeInstallError as exc:
        print(
            f"{primary_label} probe failed ({exc}); checking approved alternatives…",
            file=sys.stderr,
        )
    else:
        print(
            f"  {primary.label}: {_format_download_rate(primary.rate)}",
            file=sys.stderr,
        )

    alternatives: list[_ArchiveProbe] = []
    if primary is None or _probe_is_slow(primary, expected_size=expected_size):
        if primary is not None:
            remaining_seconds = (
                expected_size - len(primary.data)
            ) / primary.rate
            print(
                f"{primary.label} is slow (~{remaining_seconds:.0f}s remaining); "
                "checking approved alternatives…",
                file=sys.stderr,
            )
        alternatives = _probe_other_sources(
            remaining_sources,
            expected_size=expected_size,
            probe_source=probe_source,
        )
    selected = _select_probe(primary, alternatives)
    if primary is not None and selected is not primary:
        print(
            f"Switching to {selected.label} "
            f"({_format_download_rate(selected.rate)}).",
            file=sys.stderr,
        )
    elif alternatives:
        print(
            f"Continuing with {selected.label}; no approved source was "
            "materially faster.",
            file=sys.stderr,
        )

    measured = {probe.url: probe for probe in alternatives}
    if primary is not None:
        measured[primary.url] = primary
    ordered = [selected]
    ordered.extend(
        probe
        for probe in sorted(
            measured.values(),
            key=lambda candidate: candidate.rate,
            reverse=True,
        )
        if probe.url != selected.url
    )
    measured_urls = set(measured)
    ordered.extend(
        _ArchiveProbe(label, url, b"", 1.0)
        for label, url in sources
        if url not in measured_urls
    )

    digest = hashlib.sha256()
    offset = 0
    try:
        with destination.open("xb") as output:
            output.write(selected.data)
            digest.update(selected.data)
            offset = len(selected.data)
            if offset > expected_size:
                raise RuntimeInstallError("the MSYS2 probe exceeded the pinned size")

            last_error: RuntimeInstallError | None = None
            for candidate in ordered:
                progress = _DownloadProgress(candidate.url)
                progress.update(offset, expected_size)

                def write_chunk(chunk: bytes) -> None:
                    nonlocal offset
                    if offset + len(chunk) > expected_size:
                        raise RuntimeInstallError(
                            "the MSYS2 download exceeded its pinned size"
                        )
                    output.write(chunk)
                    digest.update(chunk)
                    offset += len(chunk)
                    progress.update(offset, expected_size)

                try:
                    resume_source(
                        candidate.label,
                        candidate.url,
                        offset,
                        expected_size,
                        write_chunk,
                    )
                except RuntimeInstallError as exc:
                    last_error = exc
                    progress.abort()
                    print(
                        f"{candidate.label} transfer failed ({exc}); "
                        "resuming from another approved source…",
                        file=sys.stderr,
                    )
                    continue
                if offset != expected_size:
                    last_error = RuntimeInstallError(
                        "the MSYS2 source ended before the pinned size"
                    )
                    progress.abort()
                    print(
                        f"{candidate.label} transfer ended early; resuming "
                        "from another approved source…",
                        file=sys.stderr,
                    )
                    continue
                progress.update(offset, expected_size, final=True)
                break
            else:
                raise RuntimeInstallError(
                    "every approved MSYS2 source failed during transfer"
                ) from last_error

        if digest.hexdigest().lower() != sha256.lower():
            raise RuntimeInstallError(
                "the downloaded MSYS2 archive failed SHA-256 verification"
            )
    except (RuntimeInstallError, OSError) as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise RuntimeInstallError(
                "could not remove an incomplete MSYS2 download"
            ) from cleanup_error
        if isinstance(exc, RuntimeInstallError):
            raise
        raise RuntimeInstallError("could not write the pinned MSYS2 runtime") from exc
    return selected.label


def _download_from_sources_sequentially(
    destination: Path,
    sha256: str,
    *,
    sources: Sequence[tuple[str, str]],
    downloader: Downloader,
) -> str:
    """Compatibility fallback when range selection is unavailable."""
    last_error: RuntimeInstallError | None = None
    for index, (label, url) in enumerate(sources, start=1):
        print(
            f"Downloading from {label} ({index}/{len(sources)})…",
            flush=True,
        )
        try:
            downloader(url, destination, sha256)
        except RuntimeInstallError as exc:
            last_error = exc
            try:
                destination.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise RuntimeInstallError(
                    "could not remove an incomplete MSYS2 download"
                ) from cleanup_error
            if index < len(sources):
                print(
                    f"{label} failed ({exc}); trying the next approved source…",
                    file=sys.stderr,
                )
            continue
        return label
    raise RuntimeInstallError(
        "could not download the pinned MSYS2 runtime from any approved source"
    ) from last_error


def download_from_sources(
    destination: Path,
    sha256: str,
    *,
    sources: Sequence[tuple[str, str]] = MSYS2_ARCHIVE_SOURCES,
    downloader: Downloader | None = None,
) -> str:
    """Use adaptive range selection, retaining a full-download fallback."""
    if downloader is not None:
        return _download_from_sources_sequentially(
            destination,
            sha256,
            sources=sources,
            downloader=downloader,
        )
    try:
        return download_adaptive(destination, sha256, sources=sources)
    except RuntimeInstallError as exc:
        print(
            f"Adaptive MSYS2 download failed ({exc}); retrying ordinary "
            "approved-source downloads…",
            file=sys.stderr,
        )
        try:
            destination.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise RuntimeInstallError(
                "could not remove an incomplete MSYS2 download"
            ) from cleanup_error
        return _download_from_sources_sequentially(
            destination,
            sha256,
            sources=sources,
            downloader=download_verified,
        )


def _run_checked(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    reporter: InstallReporter,
    label: str,
    runner: Runner | None = None,
    progress: str | None = None,
    allow_failure: bool = False,
) -> int:
    reporter.command_started(label, progress=progress)
    try:
        if runner is None:
            process = subprocess.Popen(
                list(argv),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=-1,
            )
            returncode = stream_process_output(process, reporter)
        else:
            result = runner(list(argv), env=dict(env), check=False)
            reporter.command_output(getattr(result, "stdout", None))
            reporter.command_output(getattr(result, "stderr", None))
            reporter.command_output_finished()
            returncode = result.returncode
    except OSError as exc:
        raise RuntimeInstallError(f"could not start {label}") from exc
    if returncode:
        if allow_failure:
            return returncode
        _explain_windows_status(returncode, reporter)
        reporter.command_failed(label, returncode)
        raise RuntimeInstallError(f"{label} failed with exit code {returncode}")
    reporter.command_succeeded()
    return 0


def _explain_windows_status(returncode: int, reporter: InstallReporter) -> None:
    if _NATIVE_WINDOWS and returncode & 0xFFFFFFFF == 0xC0000135:
        reporter.note(
            "Windows reported STATUS_DLL_NOT_FOUND, but this pass had neither "
            "pacman's restart announcement nor a newly completed core "
            "transaction with a matching package-database version change. "
            "The temporary base will not be published; a missing or "
            "quarantined staged DLL remains possible.",
            level="error",
        )


def _pacman_command(*, packages: bool, relaxed: bool) -> str:
    timeout = " --disable-download-timeout" if relaxed else ""
    needed = f" --needed {_PACMAN_PACKAGES}" if packages else ""
    keyring = (
        "pacman-key --init && pacman-key --populate msys2 && "
        if not packages
        else ""
    )
    operation = "-Syu" if packages else "-Syuu"
    return (
        'cache=$(cygpath -u "$1") && mkdir -p "$cache" && '
        f"{keyring}pacman --config {_PACMAN_CONFIG} --cachedir \"$cache\" "
        f"{operation} --noconfirm{timeout}{needed}"
    )


def _report_mirror_decision(
    decision: PacmanMirrorDecision,
    reporter: InstallReporter,
) -> None:
    for mirror_probe in decision.probes:
        reporter.command_output(
            f"mirror probe {mirror_probe.label}: "
            f"{_format_download_rate(mirror_probe.rate)}\n"
        )
    for label, reason in decision.failures:
        reporter.command_output(
            f"mirror probe {label}: unavailable ({reason})\n"
        )


def _pacman_desc_value(text: str, field: str) -> str | None:
    lines = text.splitlines()
    marker = f"%{field}%"
    for index, line in enumerate(lines[:-1]):
        if line == marker and lines[index + 1]:
            return lines[index + 1]
    return None


def _pacman_core_versions(root: Path) -> dict[str, str]:
    local_db = root / "var" / "lib" / "pacman" / "local"
    try:
        if _path_is_link_or_reparse(local_db) or not local_db.is_dir():
            raise RuntimeInstallError("the private pacman database is unsafe")
        entries = list(local_db.iterdir())
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeInstallError("could not inspect the private pacman database") from exc
    if len(entries) > _PACMAN_LOCAL_DB_ENTRY_LIMIT:
        raise RuntimeInstallError("the private pacman database is unexpectedly large")
    versions: dict[str, str] = {}
    for entry in entries:
        if not any(
            entry.name.startswith(prefix)
            for prefix in (
                "bash-",
                "filesystem-",
                "mintty-",
                "msys2-runtime-",
                "pacman-",
            )
        ):
            continue
        desc = entry / "desc"
        try:
            if (
                _path_is_link_or_reparse(entry)
                or not entry.is_dir()
                or _path_is_link_or_reparse(desc)
                or not desc.is_file()
                or desc.stat().st_size > _PACMAN_LOCAL_DESC_LIMIT
            ):
                raise RuntimeInstallError("the private pacman database is unsafe")
            text = desc.read_text(encoding="utf-8")
        except RuntimeInstallError:
            raise
        except (OSError, UnicodeError) as exc:
            raise RuntimeInstallError(
                "could not read the private pacman database"
            ) from exc
        name = _pacman_desc_value(text, "NAME")
        version = _pacman_desc_value(text, "VERSION")
        if name is None or version is None or not _is_pacman_core_package(name):
            continue
        if name in versions:
            raise RuntimeInstallError(
                "the private pacman database contains duplicate core packages"
            )
        versions[name] = version
    return versions


def _pacman_log_checkpoint(root: Path) -> _PacmanLogCheckpoint:
    """Pin the append-only pacman journal before a staged transaction."""
    path = root / _PACMAN_LOG_RELATIVE
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _PacmanLogCheckpoint(
            False,
            0,
            None,
            0,
            hashlib.sha256().hexdigest(),
            tuple(sorted(_pacman_core_versions(root).items())),
        )
    except OSError as exc:
        raise RuntimeInstallError("could not inspect the private pacman log") from exc
    try:
        unsafe = _path_is_link_or_reparse(path)
    except OSError as exc:
        raise RuntimeInstallError("could not inspect the private pacman log") from exc
    if (
        unsafe
        or not stat.S_ISREG(info.st_mode)
        or info.st_size < 0
        or info.st_size > _PACMAN_LOG_MAX_SIZE
    ):
        raise RuntimeInstallError("the private pacman log is unsafe")
    tail_offset = max(0, info.st_size - 4096)
    try:
        with path.open("rb") as stream:
            stream.seek(tail_offset)
            tail = stream.read(info.st_size - tail_offset)
    except OSError as exc:
        raise RuntimeInstallError("could not read the private pacman log") from exc
    return _PacmanLogCheckpoint(
        True,
        info.st_size,
        (info.st_dev, info.st_ino),
        tail_offset,
        hashlib.sha256(tail).hexdigest(),
        tuple(sorted(_pacman_core_versions(root).items())),
    )


def _is_pacman_core_package(name: str) -> bool:
    return name in _PACMAN_CORE_PACKAGES or name.startswith("msys2-runtime-")


def _pacman_log_core_transaction(
    root: Path, checkpoint: _PacmanLogCheckpoint
) -> tuple[str, ...]:
    """Return core packages from one newly completed journal transaction."""
    path = root / _PACMAN_LOG_RELATIVE
    try:
        info = path.lstat()
        unsafe = _path_is_link_or_reparse(path)
    except OSError:
        return ()
    if (
        unsafe
        or not stat.S_ISREG(info.st_mode)
        or info.st_size < checkpoint.size
        or info.st_size > _PACMAN_LOG_MAX_SIZE
        or info.st_size - checkpoint.size > _PACMAN_LOG_APPEND_LIMIT
    ):
        return ()
    if checkpoint.existed and (info.st_dev, info.st_ino) != checkpoint.identity:
        return ()
    try:
        with path.open("rb") as stream:
            if checkpoint.existed:
                stream.seek(checkpoint.tail_offset)
                previous_tail = stream.read(checkpoint.size - checkpoint.tail_offset)
                if hashlib.sha256(previous_tail).hexdigest() != checkpoint.tail_digest:
                    return ()
            stream.seek(checkpoint.size)
            appended = stream.read(_PACMAN_LOG_APPEND_LIMIT + 1)
    except OSError:
        return ()
    if not appended or len(appended) > _PACMAN_LOG_APPEND_LIMIT:
        return ()

    core_started = False
    transaction_started = False
    changed: dict[str, tuple[str, str]] = {}
    for line in appended.decode("utf-8", errors="replace").splitlines():
        lowered = line.lower()
        if "[pacman] starting core system upgrade" in lowered:
            core_started = True
            transaction_started = False
            changed.clear()
            continue
        if not core_started:
            continue
        if "[alpm] transaction started" in lowered:
            transaction_started = True
            changed.clear()
            continue
        if not transaction_started:
            continue
        match = _PACMAN_CORE_CHANGE_RE.search(line)
        if match is not None and _is_pacman_core_package(match.group(1)):
            changed[match.group(1)] = (match.group(2), match.group(3))
        if "[alpm] transaction completed" in lowered:
            if changed:
                try:
                    after = _pacman_core_versions(root)
                except RuntimeInstallError:
                    return ()
                before = dict(checkpoint.core_versions)
                proven = tuple(
                    sorted(
                        name
                        for name, (old_version, new_version) in changed.items()
                        if before.get(name) == old_version
                        and after.get(name) == new_version
                        and old_version != new_version
                    )
                )
                if proven:
                    return proven
            transaction_started = False
    return ()


def _restart_evidence(
    root: Path,
    checkpoint: _PacmanLogCheckpoint,
    reporter: InstallReporter,
    returncode: int,
) -> bool:
    if reporter.command_requested_msys2_restart:
        return True
    if returncode & 0xFFFFFFFF != 0xC0000135:
        return False
    packages = _pacman_log_core_transaction(root, checkpoint)
    if not packages:
        return False
    reporter.note(
        "The private pacman journal confirms a completed core update "
        f"({', '.join(packages)}); its final console prompt was not captured.",
        level="warning",
    )
    return True


def _refresh_pacman_mirrors(
    root: Path,
    *,
    reporter: InstallReporter,
    mirror_optimizer: MirrorOptimizer,
) -> PacmanMirrorDecision | None:
    try:
        decision = mirror_optimizer(root)
    except PacmanMirrorError as exc:
        reporter.note(
            f"Mirror measurement unavailable ({exc}); using the official order."
        )
        return None
    _report_mirror_decision(decision, reporter)
    return decision


def _run_pacman_with_recovery(
    root: Path,
    *,
    packages: bool,
    cache: Path,
    env: Mapping[str, str],
    reporter: InstallReporter,
    runner: Runner | None,
    label: str,
    mirror_optimizer: MirrorOptimizer,
    allow_core_restart: bool = False,
) -> bool:
    checkpoint = _pacman_log_checkpoint(root) if not packages else None
    argv = _bash_command(root, _pacman_command(packages=packages, relaxed=False), str(cache))
    returncode = _run_checked(
        argv,
        env=env,
        reporter=reporter,
        runner=runner,
        label=label,
        progress="pacman",
        allow_failure=True,
    )
    restart_requested = (
        _restart_evidence(root, checkpoint, reporter, returncode)
        if checkpoint is not None
        else False
    )
    if returncode == 0:
        return restart_requested
    if not packages and restart_requested and not allow_core_restart:
        reporter.note(
            "MSYS2 requested another core restart on the final permitted "
            "update pass; the temporary base will not be published.",
            level="error",
        )
        reporter.command_failed(label, returncode)
        raise RuntimeInstallError(
            f"{label} exhausted the permitted core-update restarts"
        )
    if allow_core_restart and not packages and restart_requested:
        reporter.note(
            "MSYS2 replaced core runtime files and closed its own process as "
            "expected; continuing from a fresh Windows-launched shell.",
            level="warning",
        )
        return True
    network_failure = reporter.command_had_network_failure
    hard_failed_hosts = reporter.hard_failed_mirror_hosts
    if not network_failure:
        _explain_windows_status(returncode, reporter)
        reporter.command_failed(label, returncode)
        raise RuntimeInstallError(f"{label} failed with exit code {returncode}")
    reporter.note(
        "Measured package sources were exhausted; rechecking them before "
        "one resilient retry."
    )
    _refresh_pacman_mirrors(
        root,
        reporter=reporter,
        mirror_optimizer=mirror_optimizer,
    )
    if hard_failed_hosts:
        try:
            deactivated = deactivate_pacman_hosts(root, hard_failed_hosts)
        except PacmanMirrorError as exc:
            reporter.note(f"Could not exclude hard-failing mirrors ({exc}).")
        else:
            if deactivated:
                reporter.note(
                    "Excluded mirrors that returned HTTP 403/404 for packages."
                )
    reporter.note(
        "Retrying with completed packages cached and the low-speed abort "
        "disabled; a slow working source may take time."
    )
    retry_argv = _bash_command(
        root,
        _pacman_command(packages=packages, relaxed=True),
        str(cache),
    )
    retry_checkpoint = _pacman_log_checkpoint(root) if not packages else None
    retry_returncode = _run_checked(
        retry_argv,
        env=env,
        reporter=reporter,
        runner=runner,
        label=f"{label} resilient retry",
        progress="pacman",
        allow_failure=True,
    )
    restart_requested = (
        _restart_evidence(root, retry_checkpoint, reporter, retry_returncode)
        if retry_checkpoint is not None
        else False
    )
    if retry_returncode == 0:
        return restart_requested
    if not packages and restart_requested and not allow_core_restart:
        reporter.note(
            "MSYS2 requested another core restart on the final permitted "
            "update pass; the temporary base will not be published.",
            level="error",
        )
        reporter.command_failed(f"{label} resilient retry", retry_returncode)
        raise RuntimeInstallError(
            f"{label} resilient retry exhausted the permitted "
            "core-update restarts"
        )
    if allow_core_restart and not packages and restart_requested:
        reporter.note(
            "MSYS2 replaced core runtime files and closed its own process as "
            "expected; continuing from a fresh Windows-launched shell.",
            level="warning",
        )
        return True
    _explain_windows_status(retry_returncode, reporter)
    reporter.command_failed(f"{label} resilient retry", retry_returncode)
    raise RuntimeInstallError(
        f"{label} resilient retry failed with exit code {retry_returncode}"
    )


def _run_base_update_with_restarts(
    root: Path,
    *,
    cache: Path,
    env: Mapping[str, str],
    reporter: InstallReporter,
    runner: Runner | None,
    mirror_optimizer: MirrorOptimizer,
) -> None:
    """Run MSYS2's full upgrade in fresh processes until restart-safe."""
    try:
        for pass_number in range(1, _MAX_BASE_UPDATE_PASSES + 1):
            restart_requested = _run_pacman_with_recovery(
                root,
                packages=False,
                cache=cache,
                env=env,
                reporter=reporter,
                runner=runner,
                label=f"MSYS2 base update pass {pass_number}",
                mirror_optimizer=mirror_optimizer,
                allow_core_restart=pass_number < _MAX_BASE_UPDATE_PASSES,
            )
            if restart_requested:
                reporter.note(
                    "Starting a new MSYS2 process for the remaining update…"
                )
                continue
            if pass_number == 1:
                # MSYS2's supported unattended update procedure invokes Syuu
                # twice, even when the first pass did not request a restart.
                reporter.note(
                    "Starting the required second MSYS2 update pass in a new "
                    "process…"
                )
                continue
            return
        raise RuntimeInstallError(
            "MSYS2 repeatedly requested a core-runtime restart; the temporary "
            "base was not published"
        )
    finally:
        _stop_private_gpg_agents(
            root,
            env=env,
            reporter=reporter,
            runner=runner,
            strict=sys.exc_info()[0] is None,
        )


def _stop_private_gpg_agents(
    root: Path,
    *,
    env: Mapping[str, str],
    reporter: InstallReporter,
    runner: Runner | None,
    strict: bool,
) -> None:
    returncode = _run_checked(
        _bash_command(root, _PRIVATE_GPG_SHUTDOWN_COMMAND),
        env=env,
        reporter=reporter,
        runner=runner,
        label="Stopping private MSYS2 keyring agents",
        allow_failure=True,
    )
    if returncode == 0:
        return
    if not strict:
        reporter.note(
            "Could not stop a private MSYS2 keyring agent while recovering "
            "from another installation failure.",
            level="warning",
        )
        return
    reporter.command_failed("Stopping private MSYS2 keyring agents", returncode)
    raise RuntimeInstallError(
        "stopping private MSYS2 keyring agents failed with exit code "
        f"{returncode}"
    )


def _completed_package_cache_count(cache: Path) -> int:
    """Count only complete package payloads, never signatures or partials."""
    try:
        return sum(
            1
            for path in cache.iterdir()
            if path.name.endswith(".pkg.tar.zst")
            and path.is_file()
            and not path.is_symlink()
        )
    except OSError:
        return 0


def _resolved_transaction_package_urls(
    root: Path,
    *,
    env: Mapping[str, str],
    runner: Runner | None,
) -> tuple[str, ...]:
    """Ask pacman for bounded real package URLs without changing state."""
    # Install test runners model transactional commands, not a second capture
    # channel. Tests that exercise this probe patch this helper explicitly.
    if runner is not None:
        return ()
    try:
        result = subprocess.run(
            _bash_command(root, _PACKAGE_URL_COMMAND),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode != 0:
        return ()
    try:
        lines = result.stdout.decode(
            "utf-8", errors="strict").strip().splitlines()
    except UnicodeError:
        return ()
    urls = tuple(dict.fromkeys(
        line.strip() for line in lines
        if line.strip().startswith("https://")
    ))
    return urls[:128]


def _validate_transaction_mirrors(
    root: Path,
    *,
    env: Mapping[str, str],
    reporter: InstallReporter,
    runner: Runner | None,
) -> None:
    package_urls = _resolved_transaction_package_urls(
        root, env=env, runner=runner)
    if not package_urls:
        reporter.note(
            "Actual package mirror preflight was unavailable; signed pacman "
            "fallback remains active.",
            level="muted",
        )
        return
    reporter.note(
        "Checking package availability on approved mirrors "
        f"(up to 12 samples from {len(package_urls)} resolved packages)…",
        level="muted",
    )
    try:
        decision = validate_transaction_package_mirrors(root, package_urls)
    except PacmanMirrorError as exc:
        reporter.note(
            f"Actual package mirror preflight was unavailable ({exc}); "
            "signed pacman fallback remains active.",
            level="warning",
        )
        return
    failed = len(decision.failures)
    checked = len(decision.package_names)
    if failed and decision.changed:
        reporter.note(
            f"Verified {checked} transaction package samples across "
            f"{len(decision.active_servers)} sources; excluded {failed} "
            "database-only or blocked sources.",
            level="warning",
        )
    elif failed:
        reporter.note(
            "Real-package probes were inconclusive; retaining the measured "
            "pool for pacman's signed fallback.",
            level="warning",
        )
    else:
        reporter.note(
            f"Verified {checked} transaction package samples across "
            f"{len(decision.active_servers)} sources.",
            level="success",
        )


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


def _write_json_marker(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _write_base_marker(root: Path) -> None:
    _write_json_marker(
        root / _BASE_MARKER,
        {"schema": RUNTIME_SCHEMA, "runtime": MSYS2_RUNTIME_ID},
    )


def _write_app_marker(app_root: Path, *, version: str) -> None:
    identity = _decode_base_content_marker(
        app_root.parents[3]
    )
    if identity is None:
        raise RuntimeInstallError(
            "the shared MSYS2 content identity was not published"
        )
    _write_json_marker(
        app_root / _APP_MARKER,
        {
            "schema": 2,
            "runtime": MSYS2_RUNTIME_ID,
            "railmux": version,
            "base_content_id": identity.content_id,
        },
    )


def _legacy_candidates(base: Path) -> list[tuple[Path, str]]:
    parent = base / MSYS2_RUNTIME_ID
    candidates: list[tuple[ProjectVersion, Path, str]] = []
    try:
        paths = list(parent.glob("railmux-*"))
    except OSError:
        return []
    for root in paths:
        if root.is_symlink() or not root.is_dir() or _base_marker_matches(root):
            continue
        payload = _read_json_marker(root / _RUNTIME_MARKER)
        if not isinstance(payload, dict):
            continue
        version = payload.get("railmux")
        if (
            not isinstance(version, str)
            or not is_project_version(version)
            or payload
            != {
                "schema": RUNTIME_SCHEMA,
                "runtime": MSYS2_RUNTIME_ID,
                "railmux": version,
            }
            or root.name != f"railmux-{version}"
            or not (root / "usr" / "bin" / "bash.exe").is_file()
        ):
            continue
        candidates.append((_version_key(version), root, version))
    return [
        (root, version)
        for _key, root, version in sorted(candidates, reverse=True)
    ]


def reusable_managed_base_candidate(
    environ: Mapping[str, str],
) -> tuple[Path, str | None] | None:
    """Return a structurally valid private base candidate without modifying it.

    Exact executable probing remains inside the serialized installer. Callers
    may use this only to choose a narrower, reuse-only authorization path.
    """
    if environ.get(_RUNTIME_OVERRIDE, "").strip():
        return None
    base = _managed_base(environ)
    if base is None:
        return None
    shared = _find_shared_root(base)
    if shared is not None:
        return shared, None
    candidates = _legacy_candidates(base)
    return candidates[0] if candidates else None


def _find_reusable_legacy_root(
    base: Path,
    *,
    environ: Mapping[str, str],
    probe: Probe,
) -> tuple[Path, str] | None:
    """Find a complete Railmux-owned pre-dev11 runtime without modifying it."""
    for root, version in _legacy_candidates(base):
        legacy = Msys2Runtime(root, managed=True)
        if probe_runtime(
            legacy, version=version, environ=environ, probe=probe
        ):
            return root, version
    return None


def _validate_pip_cache_path(cache: Path) -> None:
    if not (cache.exists() or cache.is_symlink()):
        return
    attributes = getattr(cache.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if cache.is_symlink() or attributes & reparse_flag:
        raise RuntimeInstallError(
            "the Railmux pip cache path is a link or reparse point and was "
            f"left untouched: {cache}"
        )
    if not cache.is_dir():
        raise RuntimeInstallError(
            f"the Railmux pip cache path is not a directory: {cache}"
        )


def _validate_app_layer_path(root: Path, application: Path) -> None:
    applications = root / "opt" / "railmux" / "apps"
    _validate_managed_application_root(root)
    if application.parent != applications:
        raise RuntimeInstallError(
            "an app cleanup candidate escaped the managed application directory"
        )
    try:
        attributes = getattr(application.lstat(), "st_file_attributes", 0)
    except OSError as exc:
        raise RuntimeInstallError(
            f"an app cleanup candidate could not be inspected: {application.name}"
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        application.is_symlink()
        or attributes & reparse_flag
        or not application.is_dir()
    ):
        raise RuntimeInstallError(
            "an app cleanup candidate is a link or reparse point and was left "
            f"untouched: {application.name}"
        )


def _validate_managed_application_root(root: Path) -> None:
    current = root
    for child in ("opt", "railmux", "apps"):
        try:
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
        except OSError as exc:
            raise RuntimeInstallError(
                "the managed application directory could not be inspected"
            ) from exc
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if current.is_symlink() or attributes & reparse_flag or not current.is_dir():
            raise RuntimeInstallError(
                "the managed application directory contains a link or reparse "
                "point; cleanup was refused"
            )
        current = current / child
    try:
        attributes = getattr(current.lstat(), "st_file_attributes", 0)
    except OSError as exc:
        raise RuntimeInstallError(
            "the managed application directory could not be inspected"
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if current.is_symlink() or attributes & reparse_flag or not current.is_dir():
        raise RuntimeInstallError(
            "the managed application directory contains a link or reparse "
            "point; cleanup was refused"
        )


def _venv_site_packages(application: Path) -> Path | None:
    """Return one ordinary MSYS2 venv site-packages directory."""
    library = application / "venv" / "lib"
    try:
        candidates = list(library.glob("python*/site-packages"))
    except OSError:
        return None
    valid: list[Path] = []
    for candidate in candidates:
        try:
            if _path_is_link_or_reparse(candidate) or not candidate.is_dir():
                continue
        except OSError:
            continue
        valid.append(candidate)
    return valid[0] if len(valid) == 1 else None


def _native_application_payload(version: str) -> tuple[Path, Path] | None:
    """Locate the currently executing pure-Python package and dist-info."""
    try:
        matches = [
            distribution
            for distribution in importlib_metadata.distributions(name="railmux")
            if distribution.version == version
        ]
    except (OSError, ValueError):
        return None
    # A long-lived Windows test/user environment can retain an old dist-info
    # directory even though pip installed the current package code. Selecting
    # ``distribution("railmux")`` would then be order-dependent and could
    # force an unnecessary network fallback. Never delete metadata here; use
    # the unique distribution whose version exactly matches the imported app.
    if len(matches) != 1:
        return None
    distribution = matches[0]
    package = Path(__file__).resolve().parent
    if not package.is_dir():
        return None
    info_names = {
        Path(str(item)).parts[0]
        for item in distribution.files or ()
        if Path(str(item)).parts
        and Path(str(item)).parts[0].casefold().startswith("railmux-")
        and Path(str(item)).parts[0].casefold().endswith(".dist-info")
    }
    if len(info_names) != 1:
        return None
    info = Path(distribution.locate_file(next(iter(info_names))))
    if not info.is_dir():
        return None
    return package, info


def _copy_local_tree(
    source: Path,
    destination: Path,
    *,
    budget: list[int],
    ignored_names: frozenset[str] = frozenset(),
) -> None:
    """Copy one bounded tree without following links or reparse points."""
    try:
        if _path_is_link_or_reparse(source) or not source.is_dir():
            raise RuntimeInstallError("a local application seed tree was unsafe")
    except OSError as exc:
        raise RuntimeInstallError(
            "a local application seed tree could not be inspected"
        ) from exc
    destination.mkdir()
    for current_text, directories, files in os.walk(source, followlinks=False):
        current = Path(current_text)
        relative = current.relative_to(source)
        target_root = destination / relative
        kept_directories: list[str] = []
        for name in directories:
            if name in ignored_names or name == "__pycache__":
                continue
            child = current / name
            try:
                if _path_is_link_or_reparse(child) or not child.is_dir():
                    raise RuntimeInstallError(
                        "a local application seed contained an unsafe directory"
                    )
            except OSError as exc:
                raise RuntimeInstallError(
                    "a local application seed could not be inspected"
                ) from exc
            kept_directories.append(name)
            (target_root / name).mkdir()
        directories[:] = kept_directories
        for name in files:
            if (
                name in ignored_names
                or name.endswith((".pyc", ".pyo"))
                or name.endswith((".pth", ".egg-link"))
            ):
                continue
            child = current / name
            try:
                info = child.lstat()
                if (
                    _path_is_link_or_reparse(child)
                    or not stat.S_ISREG(info.st_mode)
                ):
                    raise RuntimeInstallError(
                        "a local application seed contained an unsafe file"
                    )
            except OSError as exc:
                raise RuntimeInstallError(
                    "a local application seed could not be inspected"
                ) from exc
            budget[0] += 1
            budget[1] += info.st_size
            if (
                budget[0] > _LOCAL_APP_COPY_FILE_LIMIT
                or budget[1] > _LOCAL_APP_COPY_SIZE_LIMIT
            ):
                raise RuntimeInstallError(
                    "the local application seed exceeded its safety bound"
                )
            # Do not carry an NTFS read-only attribute from a packaged-Python
            # cache or an older app layer into the new writable venv.
            shutil.copyfile(child, target_root / name)


def _copy_local_file(source: Path, destination: Path, *, budget: list[int]) -> None:
    try:
        info = source.lstat()
        if _path_is_link_or_reparse(source) or not stat.S_ISREG(info.st_mode):
            raise RuntimeInstallError("a local dependency seed file was unsafe")
    except OSError as exc:
        raise RuntimeInstallError(
            "a local dependency seed file could not be inspected"
        ) from exc
    budget[0] += 1
    budget[1] += info.st_size
    if (
        budget[0] > _LOCAL_APP_COPY_FILE_LIMIT
        or budget[1] > _LOCAL_APP_COPY_SIZE_LIMIT
    ):
        raise RuntimeInstallError("the local application seed exceeded its safety bound")
    shutil.copyfile(source, destination)


def _dependency_seed_entries(site_packages: Path) -> list[Path] | None:
    try:
        entries = list(site_packages.iterdir())
    except OSError:
        return None
    selected = [
        entry
        for entry in entries
        if entry.name in _DEPENDENCY_SEED_NAMES
        or _DEPENDENCY_DIST_INFO_RE.fullmatch(entry.name)
    ]
    names = {entry.name for entry in selected}
    if not _DEPENDENCY_SEED_NAMES.issubset(names):
        return None
    for dependency in _DEPENDENCY_SEED_NAMES - {"typing_extensions.py"}:
        if not any(
            name.casefold().startswith(f"{dependency.casefold()}-")
            and name.casefold().endswith(".dist-info")
            for name in names
        ):
            return None
    if not any(
        name.casefold().startswith("typing_extensions-")
        and name.casefold().endswith(".dist-info")
        for name in names
    ):
        return None
    return selected


def _verified_dependency_seed(
    root: Path,
    *,
    version: str,
    environ: Mapping[str, str],
    probe: Probe,
) -> tuple[str, Path] | None:
    marked = _marked_app_versions(root)
    if not marked:
        return None
    for candidate_version in sorted(marked, key=_version_key, reverse=True):
        if candidate_version == version:
            continue
        app_name = _app_name(candidate_version)
        candidate = Msys2Runtime(root, managed=False, app_name=app_name)
        if not probe_runtime(
            candidate,
            version=candidate_version,
            environ=environ,
            probe=probe,
        ):
            continue
        site_packages = _venv_site_packages(marked[candidate_version])
        if (
            site_packages is not None
            and _dependency_seed_entries(site_packages) is not None
        ):
            return candidate_version, site_packages
    return None


def _install_application_from_local_seed(
    root: Path,
    *,
    final_app: Path,
    final_posix: str,
    version: str,
    environ: Mapping[str, str],
    reporter: InstallReporter,
    runner: Runner | None,
    probe: Probe,
) -> str:
    """Build an upgrade layer locally; return ready, unavailable, or failed."""
    payload = _native_application_payload(version)
    seed = _verified_dependency_seed(
        root, version=version, environ=environ, probe=probe)
    target_site = _venv_site_packages(final_app)
    if payload is None or seed is None or target_site is None:
        return "unavailable"
    source_package, source_info = payload
    seed_version, seed_site = seed
    seed_entries = _dependency_seed_entries(seed_site)
    if seed_entries is None:
        return "unavailable"
    budget = [0, 0]
    try:
        for source in seed_entries:
            destination = target_site / source.name
            if destination.exists() or destination.is_symlink():
                raise RuntimeInstallError(
                    "the new application environment unexpectedly contained "
                    f"{source.name}"
                )
            if source.is_dir():
                _copy_local_tree(source, destination, budget=budget)
            else:
                _copy_local_file(source, destination, budget=budget)
        _copy_local_tree(
            source_package,
            target_site / "railmux",
            budget=budget,
        )
        _copy_local_tree(
            source_info,
            target_site / source_info.name,
            budget=budget,
            ignored_names=frozenset(
                {"RECORD", "direct_url.json", "INSTALLER", "REQUESTED"}
            ),
        )
        entrypoint = final_app / "venv" / "bin" / "railmux"
        with entrypoint.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(
                f"#!{final_posix}/venv/bin/python\n"
                "from railmux.entrypoint import main\n"
                "raise SystemExit(main())\n"
            )
    except (OSError, shutil.Error, RuntimeInstallError) as exc:
        reporter.note(
            "Verified local app-layer reuse was unavailable "
            f"({exc}); falling back to the private PyPI cache.",
            level="warning",
        )
        return "failed"
    returncode = _run_checked(
        _bash_command(root, _LOCAL_APP_CHECK_COMMAND, final_posix, version),
        env=Msys2Runtime(root, managed=False).environment(environ),
        reporter=reporter,
        runner=runner,
        label="Local Railmux application validation",
        allow_failure=True,
    )
    if returncode:
        reporter.note(
            "The locally reused dependency layer did not satisfy the current "
            "package contract; falling back to the private PyPI cache.",
            level="warning",
        )
        return "failed"
    reporter.note(
        f"Reused verified dependencies from Railmux {seed_version}; installed "
        f"Railmux {version} from the current Windows package without network "
        "access.",
        level="success",
    )
    return "ready"


def _install_application(
    root: Path,
    *,
    version: str,
    cache: Path,
    environ: Mapping[str, str],
    reporter: InstallReporter,
    runner: Runner | None,
    probe: Probe,
) -> Msys2Runtime:
    """Install one versioned app layer without replacing the shared base."""
    app_name = _app_name(version)
    applications = root / "opt" / "railmux" / "apps"
    final_app = applications / app_name
    applications.mkdir(parents=True, exist_ok=True)
    _validate_pip_cache_path(cache)
    if final_app.is_symlink():
        raise RuntimeInstallError(
            f"the versioned Railmux application path is a link and was left "
            f"untouched: {final_app}"
        )
    if final_app.exists():
        candidate = Msys2Runtime(root, managed=False, app_name=app_name)
        marker = final_app / _APP_MARKER
        if _app_marker_matches(root, app_name=app_name, version=version):
            if probe_runtime(
                candidate, version=version, environ=environ, probe=probe
            ):
                reporter.note(
                    "The exact Railmux app layer is already ready; no files "
                    "were installed or replaced.",
                    level="muted",
                )
                return candidate
            raise RuntimeInstallError(
                "the published Railmux app has an exact marker but did not "
                "start after two attempts; retry after checking antivirus or "
                f"disk load. It was left untouched: {final_app}"
            )
        if marker.exists() or marker.is_symlink():
            raise RuntimeInstallError(
                "the versioned Railmux application marker is invalid; for "
                "safety the directory was left untouched. Provider histories "
                f"are outside this path: {final_app}"
            )
        reporter.note(
            "Removing an unpublished app layer left by an interrupted install; "
            "provider session files are outside this directory and untouched.",
            level="warning",
        )
        deadline = time.monotonic() + 2.0
        while True:
            try:
                if marker.exists() or marker.is_symlink():
                    raise RuntimeInstallError(
                        "the unpublished app layer gained a marker during "
                        f"recovery and was left untouched: {final_app}"
                    )
                if final_app.exists():
                    shutil.rmtree(final_app)
                final_app.mkdir()
                break
            except RuntimeInstallError:
                raise
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeInstallError(
                        "Windows could not clear the unpublished app layer; "
                        "retry after checking antivirus or disk indexing: "
                        f"{final_app}"
                    ) from exc
                time.sleep(0.1)
    else:
        final_app.mkdir()
    cache.mkdir(parents=True, exist_ok=True)
    _validate_pip_cache_path(cache)
    final_posix = _app_posix(app_name)
    child_env = Msys2Runtime(root, managed=False).environment(environ)
    _run_checked(
        _bash_command(root, _VENV_COMMAND, final_posix),
        env=child_env,
        reporter=reporter,
        runner=runner,
        label="Railmux virtual environment creation",
    )
    local_install = _install_application_from_local_seed(
        root,
        final_app=final_app,
        final_posix=final_posix,
        version=version,
        environ=environ,
        reporter=reporter,
        runner=runner,
        probe=probe,
    )
    if local_install == "failed":
        try:
            shutil.rmtree(final_app / "venv")
        except OSError as exc:
            raise RuntimeInstallError(
                "could not reset the unpublished local application seed"
            ) from exc
        _run_checked(
            _bash_command(root, _VENV_COMMAND, final_posix),
            env=child_env,
            reporter=reporter,
            runner=runner,
            label="Railmux virtual environment recovery",
        )
    if local_install == "ready":
        package_returncode = 0
    else:
        package_arguments = (
            final_posix,
            version,
            str(cache),
            str(_PIP_INITIAL_TIMEOUT_SECONDS),
            str(_PIP_INITIAL_RETRIES),
        )
        package_returncode = _run_checked(
            _bash_command(root, _PACKAGE_COMMAND, *package_arguments),
            env=child_env,
            reporter=reporter,
            runner=runner,
            label="Railmux runtime package installation",
            allow_failure=True,
        )
    if package_returncode:
        network_failure = (
            reporter.command_had_network_failure
            or reporter.command_had_pip_network_failure
        )
        if not network_failure:
            reporter.command_failed(
                "Railmux runtime package installation", package_returncode
            )
            raise RuntimeInstallError(
                "Railmux runtime package installation failed with exit code "
                f"{package_returncode}"
            )
        reporter.note(
            "The PyPI transfer was interrupted; retrying once with the "
            "Railmux-private cache and a 120-second network timeout. Completed "
            "downloads will be reused.",
            level="warning",
        )
        retry_arguments = (
            final_posix,
            version,
            str(cache),
            str(_PIP_RETRY_TIMEOUT_SECONDS),
            str(_PIP_RECOVERY_RETRIES),
        )
        _run_checked(
            _bash_command(root, _PACKAGE_COMMAND, *retry_arguments),
            env=child_env,
            reporter=reporter,
            runner=runner,
            label="Railmux runtime package installation retry",
        )

    installed = Msys2Runtime(root, managed=False, app_name=app_name)
    if not probe_runtime(
        installed, version=version, environ=environ, probe=probe
    ):
        raise RuntimeInstallError(
            "the unpublished Railmux application failed validation"
        )
    _write_app_marker(final_app, version=version)
    return installed


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
    downloader: Downloader | None = None,
    runner: Runner | None = None,
    probe: Probe = _probe,
    lock_factory: Callable[[Path], object] = install_lock,
    mirror_optimizer: MirrorOptimizer = optimize_pacman_mirror,
    extractor: Extractor = _extract_msys2_archive,
    verbose: bool = False,
    reuse_only: bool = False,
) -> Msys2Runtime:
    """Install a fresh private runtime and activate it only after verification."""
    if not is_project_version(version):
        raise RuntimeInstallError("the Railmux package version is invalid")
    if environ.get(_RUNTIME_OVERRIDE, "").strip():
        raise RuntimeInstallError(
            "RAILMUX_MSYS2_ROOT selects a user-owned runtime; Railmux will "
            "not install or modify it"
        )
    base = _managed_base(environ)
    cache_base = _managed_cache(environ)
    if base is None or cache_base is None:
        raise RuntimeInstallError(
            "a non-virtualized Windows user data directory is unavailable"
        )
    data_root = base.parent
    try:
        base.mkdir(parents=True, exist_ok=True)
        cache_base.mkdir(parents=True, exist_ok=True)
        log_path = install_log_path(
            environ, version=version, data_root=data_root
        )
    except OSError as exc:
        raise RuntimeInstallError("could not create the private runtime log") from exc
    final_root = base / "shared" / MSYS2_RUNTIME_ID
    pip_cache = cache_base / _PIP_CACHE_NAME
    final_root.parent.mkdir(parents=True, exist_ok=True)

    try:
        with InstallReporter(log_path, verbose=verbose) as reporter:
            with lock_factory(base):
                existing = find_runtime(version=version, environ=environ, probe=probe)
                if existing is not None:
                    reporter.note(
                        "The shared MSYS2 base and exact Railmux application "
                        "are already ready."
                    )
                    reporter.finish()
                    return existing

                shared_root = _find_shared_root(base)
                if final_root.exists() and shared_root != final_root:
                    raise RuntimeInstallError(
                        "the canonical shared MSYS2 directory exists but is "
                        f"incomplete; it was left untouched: {final_root}"
                    )
                if shared_root is not None and not (
                    shared_root / "usr" / "bin" / "bash.exe"
                ).is_file():
                    raise RuntimeInstallError(
                        "the shared MSYS2 base marker is valid but bash.exe is "
                        "missing; retry after checking antivirus or disk damage. "
                        f"The base was left untouched: {shared_root}"
                    )
                adopted_version: str | None = None
                if shared_root is None:
                    reusable = _find_reusable_legacy_root(
                        base, environ=environ, probe=probe
                    )
                    if reusable is not None:
                        shared_root, adopted_version = reusable

                if shared_root is not None:
                    reporter.phase(
                        1, 3, f"Reusing verified MSYS2 {MSYS2_RELEASE} base"
                    )
                    if adopted_version is None:
                        reporter.done("shared base")
                    else:
                        reporter.done(f"from Railmux {adopted_version}")
                        reporter.note(
                            "The existing private MSYS2, tmux, and Python files "
                            "will not be downloaded, copied, or upgraded.",
                            level="muted",
                        )

                    identity = _ensure_base_content_identity(
                        shared_root,
                        environ=environ,
                        probe=probe,
                    )
                    reporter.note(
                        "Verified private base identity "
                        f"{identity.content_id[:12]} · "
                        f"{identity.package_count} packages.",
                        level="muted",
                    )

                    reporter.phase(2, 3, f"Preparing Railmux {version} app layer")
                    _install_application(
                        shared_root,
                        version=version,
                        cache=pip_cache,
                        environ=environ,
                        reporter=reporter,
                        runner=runner,
                        probe=probe,
                    )
                    reporter.done()

                    reporter.phase(3, 3, "Validating the shared runtime")
                    if not _base_marker_matches(shared_root):
                        _write_base_marker(shared_root)
                    installed = Msys2Runtime(
                        shared_root,
                        managed=True,
                        app_name=_app_name(version),
                    )
                    if not probe_runtime(
                        installed,
                        version=version,
                        environ=environ,
                        probe=probe,
                    ):
                        raise RuntimeInstallError(
                            "the shared Railmux runtime failed validation"
                        )
                    reporter.done("ready; MSYS2 base reused")
                    reporter.finish()
                    return installed

                if reuse_only:
                    raise RuntimeInstallError(
                        "the reusable MSYS2 base changed or failed validation; "
                        "no full runtime installation was started"
                    )

                with tempfile.TemporaryDirectory(
                    prefix=".install-", dir=base
                ) as raw_stage:
                    stage = Path(raw_stage)
                    reporter.phase(
                        1, 7, f"Preparing verified MSYS2 {MSYS2_RELEASE} base"
                    )
                    archive, source = _prepare_cached_archive(
                        cache_base,
                        downloader=downloader,
                        prior_caches=tuple(
                            legacy / "cache"
                            for legacy in (legacy_local_app_data_root(environ),)
                            if legacy is not None and legacy != data_root
                        ),
                    )
                    reporter.done(source)

                    reporter.phase(2, 7, "Extracting the private MSYS2 base")
                    extractor(archive, stage, reporter=reporter)
                    root = stage / "msys64"
                    if not (root / "usr" / "bin" / "bash.exe").is_file():
                        raise RuntimeInstallError(
                            "the verified MSYS2 archive was incomplete"
                        )
                    try:
                        write_msys_only_pacman_config(root)
                    except PacmanMirrorError as exc:
                        raise RuntimeInstallError(
                            f"could not prepare the private package config ({exc})"
                        ) from exc
                    reporter.done()

                    reporter.phase(3, 7, "Selecting an MSYS2 package mirror")
                    decision = _refresh_pacman_mirrors(
                        root,
                        reporter=reporter,
                        mirror_optimizer=mirror_optimizer,
                    )
                    if decision is None:
                        reporter.done("official order")
                    else:
                        if decision.selected is None:
                            reporter.done("official order")
                        else:
                            reporter.done(
                                f"{decision.selected.label} · "
                                f"{_format_download_rate(decision.selected.rate)} · "
                                f"{len(decision.active)} measured fallbacks"
                            )

                    runtime = Msys2Runtime(root, managed=False)
                    child_env = runtime.environment(environ)
                    reporter.phase(4, 7, "Updating the private MSYS2 base")
                    reporter.note(
                        "pacman is noninteractive; displayed [Y/n] prompts do "
                        "not require input."
                    )
                    reporter.note(
                        "Core updates may close the first MSYS2 process; "
                        "Railmux will start the required fresh process "
                        "automatically.",
                        level="muted",
                    )
                    package_cache = cache_base / f"pacman-{MSYS2_RUNTIME_ID}"
                    package_cache.mkdir(parents=True, exist_ok=True)
                    _run_base_update_with_restarts(
                        root,
                        cache=package_cache,
                        env=child_env,
                        reporter=reporter,
                        runner=runner,
                        mirror_optimizer=mirror_optimizer,
                    )
                    reporter.done()

                    # A new process is required after msys2-runtime/bash updates.
                    child_env = runtime.environment(environ)
                    reporter.note("Rechecking package mirrors after the base update…")
                    post_update_decision = _refresh_pacman_mirrors(
                        root,
                        reporter=reporter,
                        mirror_optimizer=mirror_optimizer,
                    )
                    if post_update_decision is not None:
                        reporter.note(
                            f"Using {len(post_update_decision.active)} measured "
                            "package sources."
                        )
                    reporter.phase(5, 7, "Installing tmux and private Python")
                    reporter.note(
                        "Mirror fallback is per package; completed package "
                        "files stay in Railmux's private cache during retries.",
                        level="muted",
                    )
                    cached_packages = _completed_package_cache_count(package_cache)
                    if cached_packages:
                        reporter.note(
                            f"Reusable cache contains {cached_packages} completed "
                            "package files; pacman will not fetch them again.",
                            level="muted",
                        )
                    _validate_transaction_mirrors(
                        root,
                        env=child_env,
                        reporter=reporter,
                        runner=runner,
                    )
                    try:
                        _run_pacman_with_recovery(
                            root,
                            packages=True,
                            cache=package_cache,
                            env=child_env,
                            reporter=reporter,
                            runner=runner,
                            label="MSYS2 package installation",
                            mirror_optimizer=mirror_optimizer,
                        )
                    finally:
                        _stop_private_gpg_agents(
                            root,
                            env=child_env,
                            reporter=reporter,
                            runner=runner,
                            strict=sys.exc_info()[0] is None,
                        )
                    reporter.done()

                    identity = _ensure_base_content_identity(
                        root,
                        environ=environ,
                        probe=probe,
                    )
                    reporter.note(
                        "Recorded private base identity "
                        f"{identity.content_id[:12]} · "
                        f"{identity.package_count} packages.",
                        level="muted",
                    )

                    reporter.phase(6, 7, f"Installing Railmux {version}")
                    _install_application(
                        root,
                        version=version,
                        cache=pip_cache,
                        environ=environ,
                        reporter=reporter,
                        runner=runner,
                        probe=probe,
                    )
                    reporter.done()

                    reporter.phase(7, 7, "Validating and activating the runtime")
                    _write_base_marker(root)
                    staged_runtime = Msys2Runtime(
                        root, managed=True, app_name=_app_name(version)
                    )
                    if not probe_runtime(
                        staged_runtime,
                        version=version,
                        environ=environ,
                        probe=probe,
                    ):
                        raise RuntimeInstallError(
                            "the staged Railmux runtime failed validation"
                        )
                    os.replace(root, final_root)

                installed = Msys2Runtime(
                    final_root, managed=True, app_name=_app_name(version)
                )
                if not probe_runtime(
                    installed,
                    version=version,
                    environ=environ,
                    probe=probe,
                ):
                    raise RuntimeInstallError(
                        "the activated Railmux runtime failed validation"
                    )
                reporter.done("ready")
                reporter.finish()
                return installed
    except RuntimeInstallError as exc:
        raise RuntimeInstallError(f"{exc}; full log: {log_path}") from exc
    except OSError as exc:
        detail = f"; full log: {log_path}" if log_path.is_file() else ""
        raise RuntimeInstallError(f"runtime installation failed{detail}") from exc
