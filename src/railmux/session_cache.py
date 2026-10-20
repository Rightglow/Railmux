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
        # Claude ``continued-in`` links seen by listings: predecessor id ->
        # successor id. The App uses them to move live bindings and favorites.
        self.continuations: dict[str, str] = {}

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

        results = self._hide_continued(project, results)
        results.sort(key=lambda s: s.last_mtime, reverse=True)
        return results

    def _hide_continued(
        self, project: Project, results: list[SessionMeta]
    ) -> list[SessionMeta]:
        """Drop background sessions and sessions continued in a successor.

        Claude Code copies the whole conversation into the successor, so the
        predecessor is a stale duplicate. A user rename moves forward with it.
        A background successor of a visible session is the same interactive
        conversation now hosted by Claude's daemon, so it stays visible.
        """
        by_id = {meta.session_id: meta for meta in results}
        visible = {meta.session_id for meta in results if not meta.background}
        pending = list(visible)
        while pending:
            successor = by_id[pending.pop()].continued_in_id
            if successor in by_id and successor not in visible:
                visible.add(successor)
                pending.append(successor)
        results = [meta for meta in results if meta.session_id in visible]
        hidden: dict[str, str] = {}
        for meta in results:
            successor = meta.continued_in_id
            if (
                successor is not None
                and successor != meta.session_id
                and (project.claude_dir / f"{successor}.jsonl").is_file()
            ):
                hidden[meta.session_id] = successor
        if not hidden:
            return results
        self.continuations.update(hidden)
        carried: set[str] = set()
        if self._renames is not None:
            # Walk each chain forward so A -> B -> C carries A's rename to C.
            for _ in range(len(hidden)):
                changed = False
                for predecessor, successor in hidden.items():
                    title = self._renames.get(predecessor)
                    if title and not self._renames.get(successor):
                        self._renames.set(successor, title)
                        carried.add(successor)
                        changed = True
                if not changed:
                    break
        return [
            self._with_override(meta) if meta.session_id in carried else meta
            for meta in results
            if meta.session_id not in hidden
        ]

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
