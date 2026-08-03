"""Managed MSYS2 runtime discovery, installation, and safe handoff.

This module is imported by native Windows Python only.  The managed runtime
hosts the existing POSIX Railmux/tmux stack; provider programs and their data
remain Windows-native.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

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
from railmux.terminal_status import STYLE_ACCENT, STYLE_MUTED, styled


MSYS2_RELEASE = "2026-03-22"
MSYS2_ARCHIVE_NAME = f"msys2-base-x86_64-{MSYS2_RELEASE.replace('-', '')}.sfx.exe"
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
MSYS2_ARCHIVE_SIZE = 52_820_994
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
_DOWNLOAD_LOG_STEP = 8 * 1024 * 1024
_DOWNLOAD_PROBE_BYTES = 1024 * 1024
_DOWNLOAD_PROBE_SECONDS = 8.0
_DOWNLOAD_PROBE_READ_SIZE = 64 * 1024
_DOWNLOAD_PROBE_TIMEOUT = 10.0
_DOWNLOAD_SLOW_REMAINING_SECONDS = 60.0
_DOWNLOAD_SWITCH_RATIO = 1.25
_CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z")
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*(?:\.dev[0-9]+)?\Z")

_PACMAN_CONFIG = "/etc/railmux-pacman.conf"
_PACMAN_PACKAGES = "tmux python python-pip"
_PACKAGE_URL_COMMAND = (
    f"pacman --config {_PACMAN_CONFIG} -Sp --print-format '%l' "
    f"--needed {_PACMAN_PACKAGES} 2>/dev/null"
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
MirrorOptimizer = Callable[[Path], PacmanMirrorDecision]


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


def _managed_cache(environ: Mapping[str, str]) -> Path | None:
    local_app_data = environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return None
    return Path(local_app_data) / "Railmux" / "cache"


def _archive_cache_is_valid(path: Path) -> bool:
    try:
        if path.is_symlink() or path.stat().st_size != MSYS2_ARCHIVE_SIZE:
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
) -> tuple[Path, str]:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / MSYS2_ARCHIVE_NAME
    if _archive_cache_is_valid(archive):
        return archive, "verified local cache"
    try:
        archive.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeInstallError("could not replace the private archive cache") from exc
    source = download_from_sources(
        archive,
        MSYS2_ARCHIVE_SHA256,
        downloader=downloader,
    )
    return archive, source


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
) -> bool:
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
            return False
        reporter.command_failed(label, returncode)
        raise RuntimeInstallError(f"{label} failed with exit code {returncode}")
    reporter.command_succeeded()
    return True


def _pacman_command(*, packages: bool, relaxed: bool) -> str:
    timeout = " --disable-download-timeout" if relaxed else ""
    needed = f" --needed {_PACMAN_PACKAGES}" if packages else ""
    keyring = (
        "pacman-key --init && pacman-key --populate msys2 && "
        if not packages
        else ""
    )
    return (
        'cache=$(cygpath -u "$1") && mkdir -p "$cache" && '
        f"{keyring}pacman --config {_PACMAN_CONFIG} --cachedir \"$cache\" "
        f"-Syu --noconfirm{timeout}{needed}"
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
) -> None:
    argv = _bash_command(root, _pacman_command(packages=packages, relaxed=False), str(cache))
    if _run_checked(
        argv,
        env=env,
        reporter=reporter,
        runner=runner,
        label=label,
        progress="pacman",
        allow_failure=True,
    ):
        return
    network_failure = reporter.command_had_network_failure
    hard_failed_hosts = reporter.hard_failed_mirror_hosts
    if not network_failure:
        reporter.command_failed(label, 1)
        raise RuntimeInstallError(f"{label} failed with exit code 1")
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
    _run_checked(
        retry_argv,
        env=env,
        reporter=reporter,
        runner=runner,
        label=f"{label} resilient retry",
        progress="pacman",
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
    downloader: Downloader | None = None,
    runner: Runner | None = None,
    probe: Probe = _probe,
    lock_factory: Callable[[Path], object] = install_lock,
    mirror_optimizer: MirrorOptimizer = optimize_pacman_mirror,
    verbose: bool = False,
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
    cache_base = _managed_cache(environ)
    if base is None or cache_base is None:
        raise RuntimeInstallError("LOCALAPPDATA is unavailable")
    try:
        base.mkdir(parents=True, exist_ok=True)
        cache_base.mkdir(parents=True, exist_ok=True)
        log_path = install_log_path(environ, version=version)
    except OSError as exc:
        raise RuntimeInstallError("could not create the private runtime log") from exc
    final_root = managed_root(environ, version=version)
    assert final_root is not None
    final_root.parent.mkdir(parents=True, exist_ok=True)

    try:
        with InstallReporter(log_path, verbose=verbose) as reporter:
            with lock_factory(base):
                existing = find_runtime(version=version, environ=environ, probe=probe)
                if existing is not None:
                    reporter.note("The exact managed runtime is already ready.")
                    reporter.finish()
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
                    )
                    reporter.done(source)

                    reporter.phase(2, 7, "Extracting the private MSYS2 base")
                    _run_checked(
                        [str(archive), "-y", f"-o{stage}"],
                        env=environ,
                        reporter=reporter,
                        runner=runner,
                        label="MSYS2 extraction",
                        progress="extract",
                    )
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
                    package_cache = cache_base / f"pacman-{MSYS2_RUNTIME_ID}"
                    package_cache.mkdir(parents=True, exist_ok=True)
                    _run_pacman_with_recovery(
                        root,
                        packages=False,
                        cache=package_cache,
                        env=child_env,
                        reporter=reporter,
                        runner=runner,
                        label="MSYS2 base update",
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
                    reporter.done()

                    reporter.phase(6, 7, f"Installing Railmux {version}")
                    _run_checked(
                        _bash_command(root, _VENV_COMMAND),
                        env=child_env,
                        reporter=reporter,
                        runner=runner,
                        label="Railmux virtual environment creation",
                    )
                    _run_checked(
                        _bash_command(root, _PACKAGE_COMMAND, version),
                        env=child_env,
                        reporter=reporter,
                        runner=runner,
                        label="Railmux runtime package installation",
                    )
                    reporter.done()

                    reporter.phase(7, 7, "Validating and activating the runtime")
                    _write_marker(root, version=version)
                    staged_runtime = Msys2Runtime(root, managed=True)
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

                installed = Msys2Runtime(final_root, managed=True)
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
