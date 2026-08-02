"""Durable Windows provider records; only the live daemon authorizes adoption."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from railmux.atomic_file import atomic_write_text


SCHEMA_VERSION = 1
_PHASES = frozenset({"launching", "unresolved", "resolved", "stopped", "resume_offer"})


@dataclass(frozen=True)
class SessionRecord:
    record_id: str
    provider: str
    cwd: str
    phase: str
    daemon_id: str
    provider_session_id: str | None = None
    pid: int | None = None
    updated_at: int = 0

    def for_daemon(self, daemon_id: str) -> "SessionRecord":
        """Demote stale process claims to a user-visible resume offer."""
        if self.daemon_id == daemon_id or self.phase in {"stopped", "resume_offer"}:
            return self
        return replace(self, phase="resume_offer", pid=None)


class SessionStore:
    def __init__(self, path: Path, daemon_id: str) -> None:
        self.path = path
        self.daemon_id = daemon_id

    def load(self) -> tuple[SessionRecord, ...]:
        try:
            if self.path.stat().st_size > 2 * 1024 * 1024:
                return ()
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return ()
        if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
            return ()
        rows = payload.get("sessions")
        if not isinstance(rows, list) or len(rows) > 1024:
            return ()
        records = []
        for raw in rows:
            record = _decode_record(raw)
            if record is not None:
                records.append(record.for_daemon(self.daemon_id))
        return tuple(records)

    def save(self, records: tuple[SessionRecord, ...]) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "sessions": [asdict(replace(row, updated_at=int(time.time()))) for row in records],
        }
        atomic_write_text(
            self.path,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )


def _decode_record(raw: object) -> SessionRecord | None:
    if not isinstance(raw, dict):
        return None
    required = ("record_id", "provider", "cwd", "phase", "daemon_id")
    if not all(isinstance(raw.get(key), str) and raw[key] for key in required):
        return None
    if raw["phase"] not in _PHASES or raw["provider"] not in {"claude", "codex"}:
        return None
    session_id = raw.get("provider_session_id")
    pid = raw.get("pid")
    updated = raw.get("updated_at", 0)
    if session_id is not None and not isinstance(session_id, str):
        return None
    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
        return None
    if not isinstance(updated, int) or isinstance(updated, bool) or updated < 0:
        return None
    return SessionRecord(
        record_id=raw["record_id"][:128],
        provider=raw["provider"],
        cwd=raw["cwd"][:32768],
        phase=raw["phase"],
        daemon_id=raw["daemon_id"][:128],
        provider_session_id=session_id[:256] if session_id else None,
        pid=pid,
        updated_at=updated,
    )

