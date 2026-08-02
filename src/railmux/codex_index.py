"""Scan ~/.codex/sessions/ for Codex CLI sessions, extracting cheap metadata.

Codex stores sessions as date-hierarchical JSONL files::

    ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl

Each file begins with a ``session_meta`` record, followed by ``response_item``
(conversation turns) and ``event_msg`` (token counts, lifecycle events).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path

from railmux.atomic_file import atomic_write_text
from railmux.platform.file_security import (
    prepare_private_directory,
    private_regular_file,
)
from railmux.models import (
    AttentionCategory,
    AttentionState,
    Project,
    SessionMeta,
)
from railmux.renames import Renames

# Codex has no reliable provider-neutral signal that distinguishes a long
# running tool from a tool waiting for approval.  Red is an attention signal,
# so presume a pending function_call is blocked only after a conservative
# two-minute delay, avoiding false alarms from ordinary builds, SSH commands,
# and other tools that routinely exceed ten seconds.
_TOOL_BLOCK_AGE_S = 120


FileSignature = tuple[int, int]  # (mtime_ns, size)
_PERSISTENT_CACHE_SCHEMA = 1
_PERSISTENT_CACHE_MAX_BYTES = 16 * 1024 * 1024
_PERSISTENT_CACHE_MAX_RECORDS = 100_000


@dataclass(frozen=True)
class ScanReport:
    """Bounded, privacy-safe accounting for one index refresh."""

    complete: bool
    warning: str | None
    paths_seen: int
    stat_count: int
    parse_count: int
    transient_errors: int
    duration_s: float


class _ScanError:
    """Sentinel returned by :func:`_scan_codex_session` for a *transient*
    failure — an IO/OSError or an unexpected exception raised while reading a
    rollout — as distinct from ``None``, which marks a *deterministic* skip
    (a filtered codex_exec rollout, a missing cwd, an empty session, or a
    malformed session_meta header). Filtered subagents use a status-only
    result instead.

    The distinction drives the negative cache: deterministic skips are safe to
    remember by file signature so they aren't reopened every refresh, but a
    transient error must NOT be permanently negative-cached.  Otherwise a
    one-off NFS read glitch on an otherwise-stable rollout would hide it
    indefinitely (until its mtime+size changed or the index was invalidated).
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "SCAN_ERROR"


# Module-level singleton — compared with ``is`` at the call sites.
SCAN_ERROR = _ScanError()


@dataclass(frozen=True)
class HiddenCodexStatus:
    """Activity retained for a filtered subagent rollout.

    Subagent rollouts must not appear as duplicate sidebar conversations, but
    their status is still needed to refine the visible parent session while
    the same Codex process has the rollout open.
    """

    session_id: str
    status: str
    last_mtime: float
    pending_tool: bool


def persistent_cache_path(codex_home: Path) -> Path:
    """Return a private cache path unique to one Codex sessions root."""
    raw_base = os.environ.get("XDG_CACHE_HOME")
    base = (
        Path(raw_base).expanduser()
        if raw_base and Path(raw_base).expanduser().is_absolute()
        else Path.home() / ".cache"
    )
    try:
        root = (codex_home / "sessions").resolve()
    except OSError:
        root = (codex_home / "sessions").absolute()
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return base / "railmux" / "codex-index" / f"{digest}.json"


def _cache_attention(attention: AttentionState | None) -> dict | None:
    if attention is None:
        return None
    return {
        "category": attention.category.value,
        "summary": attention.summary,
        "retryable": attention.retryable,
        "timestamp": attention.timestamp,
        "event_order": attention.event_order,
    }


def _decode_cache_attention(raw: object) -> AttentionState | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or frozenset(raw) != frozenset({
        "category", "summary", "retryable", "timestamp", "event_order",
    }):
        raise ValueError("invalid cached attention")
    summary = raw["summary"]
    retryable = raw["retryable"]
    timestamp = raw["timestamp"]
    event_order = raw["event_order"]
    if (
        not isinstance(summary, str)
        or len(summary) > 1000
        or (retryable is not None and not isinstance(retryable, bool))
        or (
            timestamp is not None
            and (not isinstance(timestamp, str) or len(timestamp) > 64)
        )
        or not isinstance(event_order, int)
        or isinstance(event_order, bool)
        or event_order < 0
    ):
        raise ValueError("invalid cached attention")
    try:
        category = AttentionCategory(raw["category"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid cached attention category") from exc
    return AttentionState(
        category=category,
        summary=summary,
        retryable=retryable,
        timestamp=timestamp,
        event_order=event_order,
    )


def _cache_session(meta: SessionMeta) -> dict:
    return {
        "session_id": meta.session_id,
        "cwd": str(meta.project.real_path),
        "project_key": meta.project.encoded_name,
        "title": meta.title,
        "message_count": meta.message_count,
        "token_total": meta.token_total,
        "last_mtime": meta.last_mtime,
        "size_bytes": meta.size_bytes,
        "git_branch": meta.git_branch,
        "last_user_message": meta.last_user_message,
        "status": meta.status,
        "pending_tool": meta.pending_tool,
        "attention": _cache_attention(meta.attention),
        "forked_from_id": meta.forked_from_id,
    }


def _decode_cache_session(path: Path, raw: object) -> SessionMeta:
    keys = frozenset({
        "session_id", "cwd", "project_key", "title", "message_count", "token_total",
        "last_mtime", "size_bytes", "git_branch", "last_user_message",
        "status", "pending_tool", "attention", "forked_from_id",
    })
    if not isinstance(raw, dict) or frozenset(raw) != keys:
        raise ValueError("invalid cached session")
    session_id = raw["session_id"]
    cwd_raw = raw["cwd"]
    project_key = raw["project_key"]
    title = raw["title"]
    git_branch = raw["git_branch"]
    last_user_message = raw["last_user_message"]
    forked_from_id = raw["forked_from_id"]
    strings = (
        (session_id, 256, False),
        (cwd_raw, 4096, False),
        (project_key, 256, False),
        (title, 1000, True),
        (git_branch, 4096, True),
        (last_user_message, 1000, True),
        (forked_from_id, 256, True),
    )
    if any(
        (value is None and not nullable)
        or (
            value is not None
            and (not isinstance(value, str) or not value or len(value) > limit)
        )
        for value, limit, nullable in strings
    ):
        raise ValueError("invalid cached session string")
    counts = (raw["message_count"], raw["token_total"], raw["size_bytes"])
    if any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > 10**21
        for value in counts
    ):
        raise ValueError("invalid cached session count")
    last_mtime = raw["last_mtime"]
    if (
        not isinstance(last_mtime, (int, float))
        or isinstance(last_mtime, bool)
        or not math.isfinite(float(last_mtime))
        or float(last_mtime) < 0
        or raw["status"] not in {"idle", "busy", "blocked"}
        or not isinstance(raw["pending_tool"], bool)
    ):
        raise ValueError("invalid cached session state")
    cwd = Path(cwd_raw)
    if (
        not cwd.is_absolute()
        or Path(os.path.normpath(cwd_raw)) != cwd
        or not project_key.startswith("-cx-")
    ):
        raise ValueError("invalid cached cwd")
    project = Project(
        real_path=cwd,
        encoded_name=project_key,
        claude_dir=Path(),
        session_count=0,
        last_activity_ts=0.0,
    )
    return SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=path,
        title=title,
        message_count=raw["message_count"],
        token_total=raw["token_total"],
        last_mtime=float(last_mtime),
        size_bytes=raw["size_bytes"],
        git_branch=git_branch,
        last_user_message=last_user_message,
        status=raw["status"],
        pending_tool=raw["pending_tool"],
        session_type="codex",
        attention=_decode_cache_attention(raw["attention"]),
        forked_from_id=forked_from_id,
    )


def _decode_cache_hidden(raw: object) -> HiddenCodexStatus:
    if not isinstance(raw, dict) or frozenset(raw) != frozenset({
        "session_id", "status", "last_mtime", "pending_tool",
    }):
        raise ValueError("invalid cached hidden status")
    session_id = raw["session_id"]
    last_mtime = raw["last_mtime"]
    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id) > 256
        or raw["status"] not in {"idle", "busy", "blocked"}
        or not isinstance(last_mtime, (int, float))
        or isinstance(last_mtime, bool)
        or not math.isfinite(float(last_mtime))
        or float(last_mtime) < 0
        or not isinstance(raw["pending_tool"], bool)
    ):
        raise ValueError("invalid cached hidden status")
    return HiddenCodexStatus(
        session_id=session_id,
        status=raw["status"],
        last_mtime=float(last_mtime),
        pending_tool=raw["pending_tool"],
    )


def _path_key(path: Path) -> Path:
    """Resolve a path for identity comparisons without requiring it to exist."""
    try:
        return path.resolve()
    except OSError:
        return path


def _lineage_members(
    sessions: tuple[SessionMeta, ...] | list[SessionMeta],
) -> dict[str, tuple[SessionMeta, ...]]:
    """Map every Codex UUID to its same-project fork lineage.

    Codex rewind creates a new rollout whose ``forked_from_id`` points at the
    previous rollout.  Those files are checkpoints of one user-visible
    conversation, not six independent rows.  Links are joined only when both
    endpoints are present in the same cwd: a deliberate ``resume -C`` into a
    different project must not make a conversation disappear from either
    project's sidebar.

    A tiny union-find keeps malformed cycles and branching histories bounded
    and deterministic.  Distinct branches are retained on disk; the newest
    member is merely chosen as the current UI representative.
    """
    by_id = {meta.session_id: meta for meta in sessions}
    parent = {session_id: session_id for session_id in by_id}

    def find(session_id: str) -> str:
        root = session_id
        while parent[root] != root:
            root = parent[root]
        while parent[session_id] != session_id:
            next_id = parent[session_id]
            parent[session_id] = root
            session_id = next_id
        return root

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            # The lexical tie-break makes corrupt cycles/walk order harmless.
            low, high = sorted((left_root, right_root))
            parent[high] = low

    for meta in by_id.values():
        parent_id = meta.forked_from_id
        ancestor = by_id.get(parent_id) if parent_id else None
        if (ancestor is not None
                and _path_key(ancestor.project.real_path)
                == _path_key(meta.project.real_path)):
            union(meta.session_id, ancestor.session_id)

    grouped: dict[str, list[SessionMeta]] = {}
    for meta in by_id.values():
        grouped.setdefault(find(meta.session_id), []).append(meta)
    result: dict[str, tuple[SessionMeta, ...]] = {}
    for members in grouped.values():
        ordered = tuple(sorted(
            members,
            key=lambda item: (
                item.last_mtime, str(item.jsonl_path), item.session_id),
            reverse=True,
        ))
        for meta in members:
            result[meta.session_id] = ordered
    return result


def _lineage_representatives(
    sessions: tuple[SessionMeta, ...] | list[SessionMeta],
) -> tuple[SessionMeta, ...]:
    """Return the newest visible member of each same-project fork lineage."""
    members_by_id = _lineage_members(sessions)
    representatives = {
        members[0].session_id: members[0]
        for members in members_by_id.values()
        if members
    }
    return tuple(sorted(
        representatives.values(),
        key=lambda item: (
            item.last_mtime, str(item.jsonl_path), item.session_id),
        reverse=True,
    ))


class CodexIndex:
    """mtime-keyed cache of all Codex sessions under ``codex_home/sessions/``."""

    def __init__(
        self,
        codex_home: Path,
        renames: Renames | None = None,
        *,
        cache_path: Path | None = None,
    ) -> None:
        self._codex_home = codex_home
        self._sessions_dir = codex_home / "sessions"
        self._cache_path = cache_path
        self._cache_dirty = False
        # path -> (file signature captured before parsing, metadata)
        self._entries: dict[Path, tuple[FileSignature, SessionMeta]] = {}
        # Filtered subagent rollouts remain absent from the public session
        # snapshot, while this status-only cache lets the background worker
        # publish their activity without UI-thread JSONL reads.
        self._hidden_statuses: dict[
            Path, tuple[FileSignature, HiddenCodexStatus]
        ] = {}
        # Negative cache: files that scanned to ``None`` (filtered codex_exec,
        # empty/cwd-less files, unparseable JSON). Keyed by
        # the signature they had when scanned so they aren't reopened every
        # refresh until their signature changes.  See ``_refresh``.
        self._negative: dict[Path, FileSignature] = {}
        # User-assigned titles, overlaid at read time (see railmux.renames).
        self._renames = renames
        self._load_persistent_cache()

    def _cache_relative_path(self, path: Path) -> str | None:
        try:
            relative = path.relative_to(self._sessions_dir)
        except ValueError:
            return None
        value = relative.as_posix()
        if (
            not value
            or value.startswith("/")
            or len(value) > 4096
            or any(part in ("", ".", "..") for part in relative.parts)
        ):
            return None
        return value

    def _cache_record_path(self, raw: object) -> Path:
        if not isinstance(raw, str) or not raw or len(raw) > 4096:
            raise ValueError("invalid cached path")
        relative = Path(raw)
        if (
            relative.is_absolute()
            or relative.as_posix() != raw
            or any(part in ("", ".", "..") for part in relative.parts)
        ):
            raise ValueError("invalid cached path")
        return self._sessions_dir / relative

    @staticmethod
    def _decode_cache_signature(raw: object) -> FileSignature:
        if (
            not isinstance(raw, list)
            or len(raw) != 2
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                for value in raw
            )
        ):
            raise ValueError("invalid cached signature")
        return raw[0], raw[1]

    def _load_persistent_cache(self) -> None:
        path = self._cache_path
        if path is None:
            return
        try:
            info = path.lstat()
            if (
                not private_regular_file(info)
                or info.st_size > _PERSISTENT_CACHE_MAX_BYTES
            ):
                return
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        root = str(_path_key(self._sessions_dir))
        if (
            not isinstance(data, dict)
            or frozenset(data) != frozenset({"schema", "root", "records"})
            or data["schema"] != _PERSISTENT_CACHE_SCHEMA
            or data["root"] != root
            or not isinstance(data["records"], list)
            or len(data["records"]) > _PERSISTENT_CACHE_MAX_RECORDS
        ):
            return
        seen: set[Path] = set()
        for raw in data["records"]:
            try:
                if (
                    not isinstance(raw, dict)
                    or frozenset(raw)
                    != frozenset({"path", "signature", "kind", "value"})
                ):
                    raise ValueError("invalid cached record")
                record_path = self._cache_record_path(raw["path"])
                if record_path in seen:
                    raise ValueError("duplicate cached path")
                signature = self._decode_cache_signature(raw["signature"])
                kind = raw["kind"]
                if kind == "session":
                    self._entries[record_path] = (
                        signature,
                        _decode_cache_session(record_path, raw["value"]),
                    )
                elif kind == "hidden":
                    self._hidden_statuses[record_path] = (
                        signature,
                        _decode_cache_hidden(raw["value"]),
                    )
                elif kind == "negative" and raw["value"] is None:
                    self._negative[record_path] = signature
                else:
                    raise ValueError("invalid cached kind")
                seen.add(record_path)
            except (TypeError, ValueError):
                # One corrupt record does not discard independently validated
                # signatures from the rest of the cache.
                continue

    def _prepare_cache_directory(self) -> bool:
        path = self._cache_path
        if path is None:
            return False
        return prepare_private_directory(path.parent)

    def _save_persistent_cache(self) -> None:
        path = self._cache_path
        if path is None or not self._cache_dirty:
            return
        records: list[dict] = []
        for record_path, (signature, meta) in self._entries.items():
            relative = self._cache_relative_path(record_path)
            if relative is not None:
                records.append({
                    "path": relative,
                    "signature": list(signature),
                    "kind": "session",
                    "value": _cache_session(meta),
                })
        for record_path, (signature, hidden) in self._hidden_statuses.items():
            relative = self._cache_relative_path(record_path)
            if relative is not None:
                records.append({
                    "path": relative,
                    "signature": list(signature),
                    "kind": "hidden",
                    "value": {
                        "session_id": hidden.session_id,
                        "status": hidden.status,
                        "last_mtime": hidden.last_mtime,
                        "pending_tool": hidden.pending_tool,
                    },
                })
        for record_path, signature in self._negative.items():
            relative = self._cache_relative_path(record_path)
            if relative is not None:
                records.append({
                    "path": relative,
                    "signature": list(signature),
                    "kind": "negative",
                    "value": None,
                })
        if len(records) > _PERSISTENT_CACHE_MAX_RECORDS:
            return
        records.sort(key=lambda record: record["path"])
        payload = json.dumps(
            {
                "schema": _PERSISTENT_CACHE_SCHEMA,
                "root": str(_path_key(self._sessions_dir)),
                "records": records,
            },
            # ASCII escaping also keeps malformed provider surrogate escapes
            # from turning this optional optimization into a worker failure.
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(payload.encode("utf-8")) > _PERSISTENT_CACHE_MAX_BYTES:
            return
        if not self._prepare_cache_directory():
            return
        try:
            atomic_write_text(path, payload)
        except OSError:
            return
        self._cache_dirty = False

    def _with_override(
        self,
        meta: SessionMeta,
        lineage: tuple[SessionMeta, ...] | None = None,
    ) -> SessionMeta:
        """Overlay the newest rename in *meta*'s logical lineage, if any."""
        if self._renames is None:
            return meta
        for member in lineage or (meta,):
            override = self._renames.get(member.session_id)
            if override:
                return replace(meta, title=override)
        return meta

    def _canonical(self) -> dict[str, SessionMeta]:
        """Map ``session_id -> newest cached entry`` for that id.

        Multiple rollout files can share one ``session_id`` (copies, migrated
        state, resumed threads).  Every query goes through this map so counts,
        lists and single-session lookups agree and the newest metadata always
        wins deterministically (instead of depending on ``os.walk`` order).
        """
        canon: dict[str, SessionMeta] = {}
        for _, meta in self._entries.values():
            if meta.project is None:
                continue
            sid = meta.session_id
            cur = canon.get(sid)
            if (cur is None
                    or (meta.last_mtime, str(meta.jsonl_path))
                    > (cur.last_mtime, str(cur.jsonl_path))):
                canon[sid] = meta
        return canon

    # -- public API -------------------------------------------------------

    def refresh(self) -> ScanReport:
        """Refresh cached metadata once for a group of related queries."""
        return self._refresh()

    def snapshot(self) -> tuple[SessionMeta, ...]:
        """Return a coherent immutable view of the current raw cache.

        ``SessionMeta`` and ``Project`` are frozen dataclasses.  Keeping the
        original objects (rather than reconstructing selected fields) also
        preserves fields added by newer providers, including attention state.
        """
        return tuple(sorted(
            self._canonical().values(),
            key=lambda meta: (meta.last_mtime, str(meta.jsonl_path)),
            reverse=True,
        ))

    def lineage_ids(
        self, session_id: str, *, refresh: bool = True,
    ) -> frozenset[str]:
        """UUID aliases belonging to *session_id*'s logical conversation."""
        if refresh:
            self._refresh()
        sessions = tuple(self._canonical().values())
        members = _lineage_members(sessions).get(session_id, ())
        return frozenset(meta.session_id for meta in members)

    def representative_for(
        self, session_id: str, *, refresh: bool = True,
    ) -> SessionMeta | None:
        """Newest UI representative for the lineage containing *session_id*."""
        if refresh:
            self._refresh()
        sessions = tuple(self._canonical().values())
        members = _lineage_members(sessions).get(session_id)
        if not members:
            return None
        return self._with_override(members[0], members)

    def hidden_statuses(self) -> dict[str, str]:
        """Newest status for each filtered subagent rollout UUID."""
        newest: dict[str, HiddenCodexStatus] = {}
        for _, hidden in self._hidden_statuses.values():
            current = newest.get(hidden.session_id)
            if (current is None
                    or (hidden.last_mtime, hidden.session_id)
                    > (current.last_mtime, current.session_id)):
                newest[hidden.session_id] = hidden
        return {
            session_id: hidden.status
            for session_id, hidden in newest.items()
        }

    def all_cwds(self, *, refresh: bool = True) -> dict[Path, int]:
        """Map from cwd to Codex session count for every cwd that has at
        least one Codex session.

        Used to filter the Projects pane in Codex mode — only projects whose
        ``real_path`` is a key in this dict are shown, and the count is used
        for the sidebar badge.
        """
        if refresh:
            self._refresh()
        counts: dict[Path, int] = {}
        for meta in _lineage_representatives(
                tuple(self._canonical().values())):
            cwd = meta.project.real_path
            counts[cwd] = counts.get(cwd, 0) + 1
        return counts

    def sessions_for_cwd(
        self, cwd: Path, *, refresh: bool = True,
    ) -> list[SessionMeta]:
        """All Codex sessions whose ``cwd`` matches *cwd*, sorted by mtime desc."""
        if refresh:
            self._refresh()
        try:
            target = cwd.resolve()
        except OSError:
            target = cwd
        canonical = tuple(self._canonical().values())
        members_by_id = _lineage_members(canonical)
        results: list[SessionMeta] = []
        for meta in _lineage_representatives(canonical):
            try:
                mc = meta.project.real_path.resolve()
            except OSError:
                mc = meta.project.real_path
            if mc == target:
                results.append(self._with_override(
                    meta, members_by_id.get(meta.session_id)))
        results.sort(key=lambda s: s.last_mtime, reverse=True)
        return results

    def get(self, session_id: str, *, refresh: bool = True) -> SessionMeta | None:
        """Look up a single Codex session by its UUID."""
        if refresh:
            self._refresh()
        meta = self._canonical().get(session_id)
        return self._with_override(meta) if meta is not None else None

    def invalidate(self) -> None:
        self._entries.clear()
        self._hidden_statuses.clear()
        self._negative.clear()
        self._cache_dirty = True

    # -- internal ---------------------------------------------------------

    def _refresh(self) -> ScanReport:
        """Stat cached files and re-scan any whose mtime changed (or new files)."""
        started = time.monotonic()
        sessions_dir = self._sessions_dir
        if not sessions_dir.is_dir():
            # A genuinely absent provider home is a valid empty source.  An
            # existing but inaccessible node is a scan failure and must not be
            # mistaken for a successful empty snapshot.
            missing = not sessions_dir.exists()
            if missing and (
                self._entries or self._hidden_statuses or self._negative
            ):
                self._entries.clear()
                self._hidden_statuses.clear()
                self._negative.clear()
                self._cache_dirty = True
                self._save_persistent_cache()
            return ScanReport(
                complete=missing,
                warning=None if missing else "Codex session directory is unavailable",
                paths_seen=0,
                stat_count=0,
                parse_count=0,
                transient_errors=0 if missing else 1,
                duration_s=time.monotonic() - started,
            )

        now = time.time()
        current_paths: set[Path] = set()
        walk_failed = False
        stat_count = 0
        parse_count = 0
        transient_errors = 0

        def _walk_error(_error: OSError) -> None:
            nonlocal walk_failed
            walk_failed = True

        # Walk the date hierarchy: sessions/YYYY/MM/DD/*.jsonl
        try:
            for root, _dirs, files in os.walk(
                    sessions_dir, onerror=_walk_error):
                for name in files:
                    if not name.endswith(".jsonl"):
                        continue
                    path = Path(root) / name
                    current_paths.add(path)
                    try:
                        stat_count += 1
                        stat = path.stat()
                    except OSError:
                        transient_errors += 1
                        continue
                    signature = (stat.st_mtime_ns, stat.st_size)
                    cached = self._entries.get(path)
                    if cached is not None and cached[0] == signature:
                        meta = cached[1]
                        # Only pending-tool status is time-dependent. Once its
                        # age crosses the threshold, update cached metadata;
                        # reopening an unchanged (possibly huge) rollout cannot
                        # reveal anything new and creates needless NFS I/O.
                        if (meta.pending_tool and meta.status == "busy"
                                and now - meta.last_mtime > _TOOL_BLOCK_AGE_S):
                            self._entries[path] = (
                                signature, replace(meta, status="blocked"))
                            self._cache_dirty = True
                        continue
                    hidden_cached = self._hidden_statuses.get(path)
                    if (hidden_cached is not None
                            and hidden_cached[0] == signature):
                        hidden = hidden_cached[1]
                        if (hidden.pending_tool and hidden.status == "busy"
                                and now - hidden.last_mtime
                                > _TOOL_BLOCK_AGE_S):
                            self._hidden_statuses[path] = (
                                signature,
                                replace(hidden, status="blocked"),
                            )
                            self._cache_dirty = True
                        continue
                    elif cached is None:
                        # Negative cache: a file that previously scanned to
                        # None (exec / empty / unparseable) isn't reopened
                        # until its signature changes.
                        neg = self._negative.get(path)
                        if neg is not None and neg == signature:
                            continue
                    parse_count += 1
                    result = _scan_codex_rollout(path)
                    if isinstance(result, SessionMeta):
                        self._entries[path] = (signature, result)
                        self._hidden_statuses.pop(path, None)
                        self._negative.pop(path, None)
                        self._cache_dirty = True
                    elif isinstance(result, HiddenCodexStatus):
                        self._hidden_statuses[path] = (signature, result)
                        self._entries.pop(path, None)
                        self._negative.pop(path, None)
                        self._cache_dirty = True
                    elif result is None:
                        # Deterministic skip (filtered codex_exec,
                        # missing cwd, empty, or malformed header): remember the
                        # miss by signature so we don't re-parse next tick, and
                        # drop any now-stale cached entry (file was reclassified
                        # or corrupted after its signature changed).
                        self._negative[path] = signature
                        self._entries.pop(path, None)
                        self._hidden_statuses.pop(path, None)
                        self._cache_dirty = True
                    else:
                        # SCAN_ERROR — a transient IO/parse error.  Do NOT
                        # negative-cache it: that would hide an otherwise-stable
                        # rollout until its signature changed.  Leave existing
                        # state untouched so the next refresh retries this file
                        # (its signature won't match a live entry and it isn't
                        # in the negative cache), and it reappears once the
                        # transient error clears.
                        transient_errors += 1
        except OSError:
            walk_failed = True

        # Evict deleted files only after a complete traversal. A partial NFS or
        # permission failure must not make an entire skipped subtree disappear.
        if not walk_failed:
            for stale in list(self._entries):
                if stale not in current_paths:
                    del self._entries[stale]
                    self._cache_dirty = True
            for stale in list(self._negative):
                if stale not in current_paths:
                    del self._negative[stale]
                    self._cache_dirty = True
            for stale in list(self._hidden_statuses):
                if stale not in current_paths:
                    del self._hidden_statuses[stale]
                    self._cache_dirty = True

            self._save_persistent_cache()

        warning = None
        if walk_failed:
            warning = "Codex session tree scan was incomplete"
        elif transient_errors:
            warning = (
                f"Codex session scan skipped {transient_errors} transient "
                "file error(s)"
            )
        return ScanReport(
            complete=not walk_failed,
            warning=warning,
            paths_seen=len(current_paths),
            stat_count=stat_count,
            parse_count=parse_count,
            transient_errors=transient_errors,
            duration_s=time.monotonic() - started,
        )


# Tool-call / output record pairs, matched by ``call_id``.  Real Codex 0.144.x
# rollouts are dominated by ``custom_tool_call`` (exec, apply_patch, …); plain
# ``function_call`` is a minority.  Both must be paired to detect a pending tool.
_CODEX_TOOL_CALLS = frozenset({"function_call", "custom_tool_call"})
_CODEX_TOOL_OUTPUTS = frozenset({"function_call_output", "custom_tool_call_output"})


def _event_timestamp(rec: dict) -> str | None:
    """Return a display-safe event timestamp without coercing malformed data."""
    timestamp = rec.get("timestamp")
    if (isinstance(timestamp, str)
            and 1 <= len(timestamp) <= 64
            and all(ch in "0123456789TtZz:+-. " for ch in timestamp)):
        return timestamp
    return None


def _codex_error_attention(
    payload: dict, rec: dict, event_order: int,
) -> AttentionState:
    """Build attention from Codex's dedicated error event, never transcript text.

    Real rollouts persist ``event_msg`` records with payload keys ``type``,
    ``codex_error_info`` and ``message``. The message can contain provider or
    account details, so Railmux deliberately does not copy or classify it.
    ``codex_error_info`` currently proves only broad values such as
    ``bad_request`` and ``other``; neither reliably proves capacity/rate-limit.
    """
    info = payload.get("codex_error_info")
    if info == "bad_request":
        summary = "Provider rejected the request."
    else:
        summary = "Provider reported an error."
    return AttentionState(
        category=AttentionCategory.UNKNOWN_ERROR,
        retryable=None,
        summary=summary,
        timestamp=_event_timestamp(rec),
        event_order=event_order,
    )


def _codex_abort_attention(
    rec: dict, event_order: int,
) -> AttentionState:
    """Generic fallback for an abort whose reason is absent or not user-driven."""
    return AttentionState(
        category=AttentionCategory.ABORTED,
        retryable=None,
        summary="Turn aborted.",
        timestamp=_event_timestamp(rec),
        event_order=event_order,
    )


def _scan_codex_rollout(
    path: Path,
) -> SessionMeta | HiddenCodexStatus | None | _ScanError:
    """Extract metadata from a single Codex rollout JSONL file.

    Tri-state result:

    * ``SessionMeta`` — a valid, indexable session.
    * ``None`` — a *deterministic* skip: the file is filtered (codex_exec /
      subagent), has no valid session_meta header / cwd, or contains zero
      meaningful messages.  Safe to negative-cache by signature.
    * ``SCAN_ERROR`` — a *transient* failure: the file couldn't be opened, or
      an unexpected exception was raised while reading it.  Must NOT be
      permanently negative-cached (see :class:`_ScanError`); the index retries
      it on the next refresh.

    Any *malformed record* (list/string/non-numeric where a dict/number is
    expected) is skipped inline rather than raising, so one bad line never
    aborts a scan and a structurally-bad rollout still yields a deterministic
    ``None`` — only genuinely unexpected errors surface as ``SCAN_ERROR``.
    """
    try:
        f = path.open("r", encoding="utf-8")
    except OSError:
        # Transient: the file may be mid-write, briefly unreadable, or on a
        # flaky NFS mount.  Signal ERROR so the index retries it rather than
        # hiding it behind the negative cache.
        return SCAN_ERROR
    try:
        return _parse_codex_session(path, f)
    except Exception:
        # A single corrupt / unexpected rollout must never crash the whole
        # index refresh — isolate it (#13).  Treat it as transient (retryable)
        # rather than a permanent skip, so a passing IO error can't hide the
        # file forever.
        return SCAN_ERROR
    finally:
        f.close()


def _scan_codex_session(path: Path) -> SessionMeta | None | _ScanError:
    """Compatibility scanner exposing only sidebar-visible sessions."""
    result = _scan_codex_rollout(path)
    return None if isinstance(result, HiddenCodexStatus) else result


def _parse_codex_session(
    path: Path, f,
) -> SessionMeta | HiddenCodexStatus | None:
    # -- read first line for session_meta -------------------------------
    first_line = f.readline().strip()
    if not first_line:
        return None
    try:
        first = json.loads(first_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(first, dict) or first.get("type") != "session_meta":
        return None
    payload = first.get("payload")
    if not isinstance(payload, dict):
        return None
    # Skip non-interactive "codex exec" rollouts — review/automation
    # threads that would otherwise flood the sidebar.  Blocklist (not
    # allowlist) so any interactive originator, missing field, or future
    # value is still shown.
    if payload.get("originator") == "codex_exec":
        return None
    # Skip subagent-produced rollouts.  A single Codex multi-agent run
    # spawns one rollout file per subagent, each with a distinct file
    # UUID/``id`` but sharing the parent conversation's ``session_id`` and
    # first user message — so without this they surface as hundreds of
    # duplicate sidebar entries for one logical conversation.  These are
    # marked by ``thread_source == "subagent"`` (vs ``"user"``) and by a
    # dict ``source`` like ``{"subagent": {...}}`` (vs a plain string such
    # as ``"cli"``).  Blocklist, consistent with the codex_exec skip above.
    source = payload.get("source")
    hidden_subagent = (
        payload.get("thread_source") == "subagent"
        or (isinstance(source, dict) and "subagent" in source)
    )
    session_id = payload.get("id")
    if not session_id or not isinstance(session_id, str):
        return None
    # A rollout with no usable cwd can't be mapped to a project or resumed —
    # skip it rather than falling back to root "/" and creating a bogus
    # sidebar project rooted at the filesystem root.
    cwd_str = payload.get("cwd")
    if not isinstance(cwd_str, str) or not cwd_str.strip():
        return None
    cwd = Path(cwd_str)
    forked_from = payload.get("forked_from_id")
    forked_from_id = (
        forked_from
        if isinstance(forked_from, str) and forked_from.strip()
        else None
    )

    # -- scan remaining lines for messages, events -------------------
    title: str | None = None
    message_count = 0
    token_total = 0
    first_user_message: str | None = None
    last_user_message: str | None = None
    # Tool-call state machine: a call_id is added when its call record is seen
    # and removed when its matching output arrives, so only genuinely unpaired
    # calls remain "pending".  Calls lacking a call_id get a synthetic key so
    # they still register as pending (they can never be paired).
    pending_calls: set[str] = set()
    nocid_seq = 0
    # Modern Codex rollouts have explicit turn lifecycle records.  Assistant
    # messages are *not* turn boundaries: Codex can emit one, run more tools,
    # and continue reasoning before the eventual task_complete.  Keep the
    # lifecycle state separate from the last message role so an intermediate
    # assistant message cannot make an active turn flash idle.  Old rollouts
    # without lifecycle records retain the legacy last-role fallback.
    lifecycle_seen = False
    turn_active = False
    last_message_role = ""
    attention: AttentionState | None = None
    # A dedicated error may be followed by ``task_complete`` for the same
    # failed turn. Preserve that error; only a completion with no intervening
    # error is successful and therefore allowed to clear older attention.
    current_turn_had_error = False

    for event_order, raw in enumerate(f, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue

        rtype = rec.get("type", "")
        if rtype == "response_item":
            rp = rec.get("payload")
            if not isinstance(rp, dict):
                continue
            pt = rp.get("type", "")
            if pt == "message":
                role = rp.get("role", "")
                if role == "user":
                    content = rp.get("content")
                    if isinstance(content, list):
                        text = _extract_codex_text(content)
                        if (text is not None
                                and not _is_codex_synthetic_message(text)):
                            # Harness-injected environment/context records use
                            # the user role too. Count only actual conversation
                            # messages and let only those affect legacy status.
                            message_count += 1
                            last_message_role = "user"
                            # Bias a real user message toward active even if
                            # task_started is flushed just after it (or absent
                            # in a legacy rollout).
                            turn_active = True
                            if first_user_message is None:
                                first_user_message = text
                            last_user_message = text
                elif role == "assistant":
                    message_count += 1
                    last_message_role = "assistant"
            elif pt in _CODEX_TOOL_CALLS:
                cid = rp.get("call_id")
                if isinstance(cid, str) and cid:
                    pending_calls.add(cid)
                else:
                    pending_calls.add(f"\0nocid{nocid_seq}")
                    nocid_seq += 1
            elif pt in _CODEX_TOOL_OUTPUTS:
                cid = rp.get("call_id")
                if isinstance(cid, str) and cid:
                    pending_calls.discard(cid)
        elif rtype == "event_msg":
            ep = rec.get("payload")
            if not isinstance(ep, dict):
                continue
            et = ep.get("type")
            if et == "token_count":
                # Direct schema: payload.info.total_token_usage is CUMULATIVE,
                # so keep the last value rather than summing across events.
                tok = _codex_cumulative_tokens(ep.get("info"))
                if tok is not None:
                    token_total = tok
            elif et == "task_started":
                lifecycle_seen = True
                turn_active = True
                current_turn_had_error = False
                attention = None
            elif et == "error":
                attention = _codex_error_attention(ep, rec, event_order)
                current_turn_had_error = True
            elif et == "task_complete":
                lifecycle_seen = True
                turn_active = False
                pending_calls.clear()
                if not current_turn_had_error:
                    attention = None
                current_turn_had_error = False
            elif et == "turn_aborted":
                lifecycle_seen = True
                turn_active = False
                pending_calls.clear()
                # The only durable real reason observed in current rollouts is
                # the exact provider enum ``interrupted``. It is user-driven,
                # not a model/provider failure, so it clears rather than adds
                # attention. Missing, future, or malformed reasons degrade to
                # a generic abort without inspecting transcript text.
                if ep.get("reason") == "interrupted":
                    attention = None
                    current_turn_had_error = False
                else:
                    attention = _codex_abort_attention(rec, event_order)
                    current_turn_had_error = True
            elif et == "thread_rolled_back":
                # task_complete / turn_aborted / thread_rolled_back: the turn is
                # over and any dangling tool calls are dead — clear them so an
                # aborted/rolled-back session never reads as busy/blocked.
                lifecycle_seen = True
                turn_active = False
                pending_calls.clear()
                attention = None
                current_turn_had_error = False

    # -- skip empty sessions --------------------------------------------
    if message_count == 0:
        return None

    # -- file stat -------------------------------------------------------
    # A stat failure here (e.g. the file was deleted mid-scan) is transient —
    # let it propagate so _scan_codex_session returns SCAN_ERROR and the file
    # is retried, rather than being negative-cached as a deterministic skip.
    st = path.stat()
    mtime = st.st_mtime
    size_bytes = st.st_size

    # -- status ----------------------------------------------------------
    # Priority: an unpaired tool call means we're mid-tool (busy, or blocked on
    # approval once stale); otherwise the last lifecycle/message signal decides.
    pending_tool = bool(pending_calls)
    if pending_tool:
        age = time.time() - mtime
        status = "blocked" if age > _TOOL_BLOCK_AGE_S else "busy"
    elif lifecycle_seen:
        status = "busy" if turn_active else "idle"
    elif last_message_role == "user":
        # Compatibility for old Codex rollouts without lifecycle records.
        status = "busy"
    else:
        # Legacy last-assistant, or a metadata-only lifecycle-free stream.
        status = "idle"

    # -- title fallback: first user message, first line ------------------
    if first_user_message:
        first_line = first_user_message.split("\n")[0]
        title = first_line[:60] + ("..." if len(first_line) > 60 else "")

    # -- preview: first line of latest user message -----------------------
    preview: str | None = None
    if last_user_message:
        first_line = last_user_message.split("\n")[0]
        preview = first_line[:117] + ("..." if len(first_line) > 120 else "") if len(first_line) > 120 else first_line

    # Synthesize a Project from the cwd.
    try:
        resolved = cwd.resolve()
    except OSError:
        resolved = cwd
    project = Project(
        real_path=resolved,
        encoded_name=_safe_encoded_name(resolved),
        claude_dir=Path(),  # unused for Codex sessions
        session_count=0,
        last_activity_ts=0.0,
    )

    meta = SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=path,
        title=title,
        message_count=message_count,
        token_total=token_total,
        last_mtime=mtime,
        size_bytes=size_bytes,
        git_branch=None,
        last_user_message=preview,
        status=status,
        pending_tool=pending_tool,
        session_type="codex",
        attention=attention,
        forked_from_id=forked_from_id,
    )
    if hidden_subagent:
        return HiddenCodexStatus(
            session_id=session_id,
            status=status,
            last_mtime=mtime,
            pending_tool=pending_tool,
        )
    return meta


def _codex_cumulative_tokens(info: object) -> int | None:
    """Return the cumulative total token count from a ``token_count`` event's
    ``info`` block, or ``None`` if it carries no usable number.

    Real schema (Codex 0.144.x)::

        event_msg.payload.info.total_token_usage.total_tokens

    ``total_tokens`` is preferred; if absent, fall back to
    ``input_tokens + output_tokens``.  Non-numeric values are ignored so a
    malformed event can't raise.
    """
    if not isinstance(info, dict):
        return None
    usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    have = False
    acc = 0
    for v in (inp, out):
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            acc += v
            have = True
    return acc if have else None


def _extract_codex_text(content: list) -> str | None:
    """Pull meaningful display text from Codex content blocks.

    Codex uses ``input_text`` for user messages and ``output_text`` for
    assistant messages.  Both are regular strings (not markdown blocks).
    """
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype in ("input_text", "output_text"):
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def _is_codex_synthetic_message(text: str) -> bool:
    """True when *text* is a system-generated placeholder, not a real user message.

    Codex prepends several synthetic user messages at the start of every
    session: ``<environment_context>``, ``# AGENTS.md instructions``,
    ``<permissions instructions>``, ``<collaboration_mode>``, etc.
    These make terrible titles — skip them so the first *real* user
    message becomes the display title.
    """
    return (text.startswith("<") or text.startswith("# AGENTS.md"))


def _safe_encoded_name(cwd: Path) -> str:
    """Stable encoded name for a cwd — used as a synthetic Project key."""
    # Use a simple scheme: replace separators and special chars with hyphens.
    s = str(cwd.resolve())
    out = "".join(c if c.isalnum() or c in "/." else "-" for c in s)
    # Prefix with "-" so it doesn't collide with Claude's path-encoded names
    # (which also start with "-").
    return "-cx-" + out.replace("/", "-").strip("-")[:120]
