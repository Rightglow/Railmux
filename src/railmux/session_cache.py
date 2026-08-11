"""mtime-keyed cache wrapping railmux.session_index.list_sessions."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace
from pathlib import Path

from railmux.models import Project, SessionMeta
from railmux.renames import Renames
from railmux.session_index import (
    FileSignature,
    _SessionScanState,
    _TOOL_BLOCK_AGE_S,
    _scan_session_incremental,
)


_DEFAULT_TOP_N = 30


@dataclass(frozen=True)
class _CacheEntry:
    signature: FileSignature
    meta: SessionMeta | None
    state: _SessionScanState


class SessionCache:
    def __init__(self, renames: Renames | None = None) -> None:
        self._entries: dict[Path, _CacheEntry] = {}
        # User-assigned titles, overlaid at read time so they survive Claude
        # Code rewriting its own ai-title record every turn.
        self._renames = renames

    def list_sessions(
        self, project: Project, top_n: int = _DEFAULT_TOP_N
    ) -> list[SessionMeta]:
        """Return up to `top_n` most-recent sessions for `project`.

        Older sessions beyond `top_n` exist on disk but are not parsed here.
        This keeps heavy-traffic projects (30+ sessions) snappy on cold cache
        fills. Set `top_n=0` for no cap (full scan).
        """
        # Phase 1: scandir for mtimes (cheap).
        candidates: list[tuple[FileSignature, Path]] = []
        try:
            scan = os.scandir(project.claude_dir)
        except OSError:
            return []
        with scan:
            for entry in scan:
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                signature = (
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_mtime_ns,
                    stat.st_size,
                )
                candidates.append((signature, Path(entry.path)))

        # Phase 2: sort by mtime desc, optionally cap.
        candidates.sort(key=lambda item: item[0][2], reverse=True)
        if top_n > 0:
            candidates = candidates[:top_n]

        # Phase 3: parse (with cache).
        now = time.time()
        current_paths: set[Path] = set()
        results: list[SessionMeta] = []
        for signature, path in candidates:
            current_paths.add(path)
            meta = self._meta_for(project, path, signature, now)
            if meta is not None:
                results.append(meta)

        # Evict stale entries from this project only. Other projects may have
        # running sessions whose metadata should remain warm between polls.
        for stale in list(self._entries.keys()):
            if stale.parent == project.claude_dir and stale not in current_paths:
                del self._entries[stale]

        results.sort(key=lambda s: s.last_mtime, reverse=True)
        return results

    def get(self, project: Project, session_id: str) -> SessionMeta | None:
        """Cache-backed lookup of a single session's metadata by id.

        Used by the Running pane so its status comes from the same source as
        the Sessions pane (no separate scan path to drift out of sync)."""
        path = project.claude_dir / f"{session_id}.jsonl"
        try:
            stat = path.stat()
        except OSError:
            return None
        signature = (
            stat.st_dev,
            stat.st_ino,
            stat.st_mtime_ns,
            stat.st_size,
        )
        return self._meta_for(project, path, signature, time.time())

    def _meta_for(
        self, project: Project, path: Path, signature: FileSignature, now: float
    ) -> SessionMeta | None:
        """Cached-or-scanned SessionMeta for `path`.

        Pending-tool status ages from cached metadata without reopening the
        provider file. Scan results use the identity observed on the opened
        descriptor, so an append during the read forces another incremental
        scan on the next poll.
        """
        cached = self._entries.get(path)
        if cached is not None and cached.signature == signature:
            return self._with_override(self._aged(cached.meta, now))
        result = _scan_session_incremental(
            project,
            path,
            None if cached is None else cached.state,
        )
        if result.state is None:
            # A transient open/read/stat failure must not publish a false
            # disappearance. Keep the last coherent metadata generation and
            # retry because its old signature cannot match the next scandir.
            return self._with_override(
                self._aged(None if cached is None else cached.meta, now)
            )
        self._entries[path] = _CacheEntry(
            result.signature,
            result.meta,
            result.state,
        )
        return self._with_override(self._aged(result.meta, now))

    @staticmethod
    def _aged(meta: SessionMeta | None, now: float) -> SessionMeta | None:
        if meta is None or not meta.pending_tool:
            return meta
        status = "blocked" if now - meta.last_mtime > _TOOL_BLOCK_AGE_S else "busy"
        return meta if meta.status == status else replace(meta, status=status)

    def _with_override(self, meta: SessionMeta | None) -> SessionMeta | None:
        """Overlay a user rename onto *meta*'s title, if one exists.

        The cache stores the raw parse; the override is applied on the way out
        so a rename takes effect on the next poll without invalidating the
        cache, and Claude's own ai-title rewrites can never clobber it."""
        if meta is None or self._renames is None:
            return meta
        override = self._renames.get(meta.session_id)
        return replace(meta, title=override) if override else meta

    def invalidate(self) -> None:
        self._entries.clear()
