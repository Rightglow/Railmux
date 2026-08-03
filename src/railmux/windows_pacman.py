"""Network-adaptive mirror ordering for Railmux-owned MSYS2 runtimes."""
from __future__ import annotations

import concurrent.futures
import email.utils
import http.client
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


PACMAN_MIRROR_SOURCES = (
    ("MSYS2 geo mirror", "https://mirror.msys2.org/msys/$arch/"),
    ("MSYS2 repository", "https://repo.msys2.org/msys/$arch/"),
    ("TUNA mirror", "https://mirrors.tuna.tsinghua.edu.cn/msys2/msys/$arch/"),
    ("USTC mirror", "https://mirrors.ustc.edu.cn/msys2/msys/$arch/"),
    ("NJU mirror", "https://mirror.nju.edu.cn/msys2/msys/$arch/"),
)

_MIRRORLIST_RELATIVE = Path("etc") / "pacman.d" / "mirrorlist.msys"
_PACMAN_CONF_RELATIVE = Path("etc") / "pacman.conf"
_RAILMUX_PACMAN_CONF_RELATIVE = Path("etc") / "railmux-pacman.conf"
_MIRRORLIST_LIMIT = 256 * 1024
_PACMAN_CONF_LIMIT = 256 * 1024
_PROBE_BYTES = 256 * 1024
_PROBE_TIMEOUT = 8.0
_SWITCH_RATIO = 1.25
_FRESHNESS_WINDOW_SECONDS = 6 * 60 * 60
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_SERVER_RE = re.compile(r"\s*Server\s*=\s*(https://\S+)\s*\Z")
_INACTIVE_SERVER_RE = re.compile(
    r"\s*#\s*Railmux inactive:\s*Server\s*=\s*(https://\S+)\s*\Z"
)
_SECTION_RE = re.compile(r"\s*\[([^]]+)]\s*\Z")


class PacmanMirrorError(RuntimeError):
    """A mirror could not be measured or safely selected."""


@dataclass(frozen=True)
class PacmanMirrorProbe:
    label: str
    server: str
    bytes_read: int
    elapsed: float
    modified_at: float | None = None

    @property
    def rate(self) -> float:
        return self.bytes_read / max(self.elapsed, 0.001)


@dataclass(frozen=True)
class PacmanMirrorDecision:
    selected: PacmanMirrorProbe | None
    primary: str | None
    changed: bool
    probes: tuple[PacmanMirrorProbe, ...]
    failures: tuple[tuple[str, str], ...]
    active: tuple[PacmanMirrorProbe, ...] = ()


MirrorProbe = Callable[[str, str], PacmanMirrorProbe]


def _database_url(server: str) -> str:
    return urllib.parse.urljoin(server.replace("$arch", "x86_64"), "msys.db")


def probe_pacman_mirror(
    label: str,
    server: str,
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
    clock: Callable[[], float] = time.monotonic,
) -> PacmanMirrorProbe:
    """Measure a bounded prefix from the actual MSYS package database."""
    if urllib.parse.urlsplit(server).scheme.lower() != "https":
        raise PacmanMirrorError("mirror is not HTTPS")
    request = urllib.request.Request(
        _database_url(server),
        headers={
            "Accept-Encoding": "identity",
            "Range": f"bytes=0-{_PROBE_BYTES - 1}",
            "User-Agent": "Railmux-pacman-mirror-probe/1",
        },
    )
    started = clock()
    data = bytearray()
    try:
        with opener(request, timeout=_PROBE_TIMEOUT) as response:
            final_url = getattr(response, "geturl", lambda: request.full_url)()
            if urllib.parse.urlsplit(final_url).scheme.lower() != "https":
                raise PacmanMirrorError("mirror redirected outside HTTPS")
            status = getattr(response, "status", 200)
            if status not in (200, 206):
                raise PacmanMirrorError(f"mirror returned HTTP {status}")
            raw_modified = getattr(response, "headers", {}).get("Last-Modified")
            modified_at: float | None = None
            if raw_modified:
                try:
                    modified_at = email.utils.parsedate_to_datetime(
                        raw_modified
                    ).timestamp()
                except (TypeError, ValueError, OverflowError):
                    modified_at = None
            while len(data) < _PROBE_BYTES:
                chunk = response.read(min(64 * 1024, _PROBE_BYTES - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if clock() - started >= _PROBE_TIMEOUT:
                    break
    except PacmanMirrorError:
        raise
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise PacmanMirrorError("mirror probe failed") from exc
    if len(data) < 1024:
        raise PacmanMirrorError("mirror returned too little database data")
    if not data.startswith(_ZSTD_MAGIC):
        raise PacmanMirrorError("mirror did not return an MSYS package database")
    return PacmanMirrorProbe(
        label=label,
        server=server,
        bytes_read=len(data),
        elapsed=max(clock() - started, 0.001),
        modified_at=modified_at,
    )


def _approved_servers_in_mirrorlist(text: str) -> list[tuple[str, str]]:
    approved = {server: label for label, server in PACMAN_MIRROR_SOURCES}
    result: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _SERVER_RE.fullmatch(line) or _INACTIVE_SERVER_RE.fullmatch(line)
        if match is None:
            continue
        server = match.group(1)
        label = approved.get(server)
        if label is not None:
            result.append((label, server))
    return result


def _primary_server(text: str) -> str | None:
    for line in text.splitlines():
        match = _SERVER_RE.fullmatch(line)
        if match is not None:
            return match.group(1)
    return None


def _probe_candidates(
    sources: Sequence[tuple[str, str]],
    *,
    probe: MirrorProbe,
) -> tuple[list[PacmanMirrorProbe], list[tuple[str, str]]]:
    results: list[PacmanMirrorProbe] = []
    failures: list[tuple[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sources)) as pool:
        futures = {
            pool.submit(probe, label, server): label
            for label, server in sources
        }
        for future in concurrent.futures.as_completed(futures):
            label = futures[future]
            try:
                results.append(future.result())
            except PacmanMirrorError as exc:
                failures.append((label, str(exc)))
    return results, failures


def _select_probe(
    probes: Sequence[PacmanMirrorProbe],
    *,
    primary: str | None,
) -> PacmanMirrorProbe | None:
    if not probes:
        return None
    dated = [item.modified_at for item in probes if item.modified_at is not None]
    if dated:
        newest = max(dated)
        eligible = [
            item
            for item in probes
            if item.modified_at is not None
            and newest - item.modified_at <= _FRESHNESS_WINDOW_SECONDS
        ]
    else:
        eligible = list(probes)
    fastest = max(eligible, key=lambda item: item.rate)
    current = next((item for item in probes if item.server == primary), None)
    current_is_eligible = current in eligible
    current_is_unknown = current is not None and current.modified_at is None
    if (
        current is not None
        and (current_is_eligible or current_is_unknown)
        and fastest.rate < current.rate * _SWITCH_RATIO
    ):
        return current
    return fastest


def _ordered_active_probes(
    probes: Sequence[PacmanMirrorProbe],
    selected: PacmanMirrorProbe,
) -> list[PacmanMirrorProbe]:
    dated = [item.modified_at for item in probes if item.modified_at is not None]
    newest = max(dated) if dated else None

    def sort_key(item: PacmanMirrorProbe) -> tuple[int, float]:
        fresh = (
            newest is None
            or item.modified_at is None
            or newest - item.modified_at <= _FRESHNESS_WINDOW_SECONDS
        )
        return (1 if fresh else 0, item.rate)

    ordered = [selected]
    ordered.extend(
        item
        for item in sorted(probes, key=sort_key, reverse=True)
        if item.server != selected.server
    )
    return ordered


def _server_lines(text: str) -> list[str]:
    result: list[str] = []
    for line in text.splitlines():
        match = _SERVER_RE.fullmatch(line) or _INACTIVE_SERVER_RE.fullmatch(line)
        if match is not None and match.group(1) not in result:
            result.append(match.group(1))
    return result


def _render_measured_pool(text: str, active_servers: Sequence[str]) -> str:
    lines = text.splitlines()
    original_servers = _server_lines(text)
    first_index = next(
        index
        for index, line in enumerate(lines)
        if _SERVER_RE.fullmatch(line) or _INACTIVE_SERVER_RE.fullmatch(line)
    )
    retained = [
        line
        for line in lines
        if not (_SERVER_RE.fullmatch(line) or _INACTIVE_SERVER_RE.fullmatch(line))
    ]
    removed_before = sum(
        1
        for line in lines[:first_index]
        if _SERVER_RE.fullmatch(line) or _INACTIVE_SERVER_RE.fullmatch(line)
    )
    insertion = first_index - removed_before
    active = list(dict.fromkeys(active_servers))
    active_set = set(active)
    rendered_servers = [f"Server = {server}" for server in active]
    rendered_servers.extend(
        f"# Railmux inactive: Server = {server}"
        for server in original_servers
        if server not in active_set
    )
    retained[insertion:insertion] = rendered_servers
    return "\n".join(retained) + "\n"


def _write_private_text(path: Path, text: str, *, error: str) -> None:
    temporary = path.with_suffix(path.suffix + ".railmux.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise PacmanMirrorError(error) from exc


def optimize_pacman_mirror(
    root: Path,
    *,
    probe: MirrorProbe = probe_pacman_mirror,
) -> PacmanMirrorDecision:
    """Promote a measured official source inside one staged private runtime."""
    mirrorlist = root / _MIRRORLIST_RELATIVE
    try:
        if mirrorlist.is_symlink() or mirrorlist.stat().st_size > _MIRRORLIST_LIMIT:
            raise PacmanMirrorError("private mirrorlist is not a safe regular file")
        text = mirrorlist.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PacmanMirrorError("could not read the private mirrorlist") from exc
    sources = _approved_servers_in_mirrorlist(text)
    primary = _primary_server(text)
    if not sources:
        return PacmanMirrorDecision(None, primary, False, (), ())
    probes, failures = _probe_candidates(sources, probe=probe)
    selected = _select_probe(probes, primary=primary)
    active = [] if selected is None else _ordered_active_probes(probes, selected)
    updated = (
        text
        if not active
        else _render_measured_pool(text, [item.server for item in active])
    )
    changed = updated != text
    if changed:
        _write_private_text(
            mirrorlist,
            updated,
            error="could not update the private mirrorlist",
        )
    return PacmanMirrorDecision(
        selected=selected,
        primary=primary,
        changed=changed,
        probes=tuple(sorted(probes, key=lambda item: item.rate, reverse=True)),
        failures=tuple(failures),
        active=tuple(active),
    )


def deactivate_pacman_hosts(root: Path, hosts: Sequence[str]) -> bool:
    """Remove hard-failing hosts from the active staged mirror pool."""
    blocked = {host.lower() for host in hosts}
    if not blocked:
        return False
    mirrorlist = root / _MIRRORLIST_RELATIVE
    try:
        if mirrorlist.is_symlink() or mirrorlist.stat().st_size > _MIRRORLIST_LIMIT:
            raise PacmanMirrorError("private mirrorlist is not a safe regular file")
        text = mirrorlist.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PacmanMirrorError("could not read the private mirrorlist") from exc
    active = []
    for line in text.splitlines():
        match = _SERVER_RE.fullmatch(line)
        if match is None:
            continue
        server = match.group(1)
        host = (urllib.parse.urlsplit(server).hostname or "").lower()
        if host not in blocked:
            active.append(server)
    if not active:
        return False
    updated = _render_measured_pool(text, active)
    if updated == text:
        return False
    _write_private_text(
        mirrorlist,
        updated,
        error="could not update the private mirrorlist",
    )
    return True


def write_msys_only_pacman_config(root: Path) -> Path:
    """Create a private config that syncs only the repository Railmux uses."""
    source = root / _PACMAN_CONF_RELATIVE
    destination = root / _RAILMUX_PACMAN_CONF_RELATIVE
    try:
        if source.is_symlink() or source.stat().st_size > _PACMAN_CONF_LIMIT:
            raise PacmanMirrorError("private pacman config is not a safe regular file")
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PacmanMirrorError("could not read the private pacman config") from exc
    output: list[str] = []
    section: str | None = None
    found_options = False
    found_msys = False
    found_msys_include = False
    for line in text.splitlines():
        match = _SECTION_RE.fullmatch(line)
        if match is not None:
            section = match.group(1).strip().lower()
            found_options = found_options or section == "options"
            found_msys = found_msys or section == "msys"
        if section is None or section in {"options", "msys"}:
            output.append(line)
            if (
                section == "msys"
                and line.strip() == "Include = /etc/pacman.d/mirrorlist.msys"
            ):
                found_msys_include = True
    if not (found_options and found_msys and found_msys_include):
        raise PacmanMirrorError("private pacman config lacks the required MSYS repo")
    rendered = "\n".join(output) + "\n"
    _write_private_text(
        destination,
        rendered,
        error="could not write the Railmux pacman config",
    )
    return destination
