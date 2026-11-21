"""Read Devin session metadata through ACP without opening its session store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from railmux.models import Project, SessionMeta

_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z")
_TIMEOUT = 10
_MAX_LINE = 1024 * 1024


def project_for(cwd: Path, count: int = 0, activity: float = 0) -> Project:
    key = hashlib.sha256(os.fsencode(cwd)).hexdigest()[:24]
    return Project(cwd, f"-dv-{key}", Path(), count, activity)


def command(binary: str, session_id: str | None = None) -> list[str]:
    # tmux owns the lifetime of the interactive agent. In-process mode also
    # gives discovery an exact lock owner in the pane's process tree.
    argv = [binary, "--process-model", "in-process"]
    if session_id is not None:
        if not _ID.fullmatch(session_id):
            raise ValueError("Invalid Devin session ID")
        argv.extend(("--resume", session_id))
    return argv


def lease_root() -> Path:
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    override = os.environ.get("DEVIN_SESSIONS_DIR")
    return Path(override).expanduser().absolute() if override else data / "devin/cli"


@dataclass(frozen=True)
class DevinSession:
    meta: SessionMeta
    locked: bool
    holder_pid: int | None


def _parse_session(row: object) -> DevinSession | None:
    if not isinstance(row, dict):
        return None
    session_id, cwd = row.get("sessionId"), row.get("cwd")
    extra = row.get("_meta")
    if not isinstance(extra, dict):
        extra = {}
    if (not isinstance(session_id, str) or not _ID.fullmatch(session_id)
            or not isinstance(cwd, str) or not cwd or "\x00" in cwd
            or not Path(cwd).is_absolute()
            or extra.get("cognition.ai/hasUserMessages") is False):
        return None
    try:
        stamp = datetime.fromisoformat(row["updatedAt"].replace("Z", "+00:00"))
        updated = stamp.timestamp() if stamp.tzinfo else 0
    except (KeyError, AttributeError, ValueError, OverflowError, OSError):
        updated = 0
    title = row.get("title")
    # Strip terminal controls and flatten multiline titles before displaying.
    if isinstance(title, str):
        title = " ".join("".join(c for c in title if c.isprintable() or c.isspace()).split())
    else:
        title = None
    locked = extra.get("cognition.ai/isLocked") is True
    pid = extra.get("cognition.ai/lockHolderPid")
    return DevinSession(
        SessionMeta(project_for(Path(cwd)), session_id, Path(), title,
                    0, 0, updated, session_type="devin"),
        locked, pid if locked and type(pid) is int and pid > 0 else None,
    )


def scan(binary: str) -> dict[str, DevinSession]:
    """Collect a complete paginated generation, bounded even for a stalled CLI."""
    with subprocess.Popen(
        [binary, "acp", "--process-model", "in-process"], cwd=Path.home(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        timer = threading.Timer(_TIMEOUT, process.kill)
        timer.daemon = True
        timer.start()
        try:
            def request(number: int, method: str, params: dict) -> dict:
                payload = dict(jsonrpc="2.0", id=number, method=method, params=params)
                process.stdin.write(json.dumps(payload).encode() + b"\n")
                process.stdin.flush()
                while True:
                    line = process.stdout.readline(_MAX_LINE)
                    if not line.endswith(b"\n"):
                        raise ValueError("Incomplete ACP response")
                    message = json.loads(line)
                    if isinstance(message, dict) and message.get("id") == number:
                        result = message.get("result")
                        if isinstance(result, dict):
                            return result
                        raise ValueError("ACP request failed")

            hello = request(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}})
            if hello.get("protocolVersion") != 1:
                raise ValueError("Unsupported ACP protocol")
            records = {}
            cursor = None
            seen = set()
            for number in range(2, 202):
                page = request(number, "session/list", {"cursor": cursor} if cursor else {})
                rows = page.get("sessions")
                if not isinstance(rows, list):
                    raise ValueError("Invalid ACP session inventory")
                for row in rows:
                    record = _parse_session(row)
                    if record is not None:
                        records.setdefault(record.meta.session_id, record)
                cursor = page.get("nextCursor")
                if cursor is None:
                    return records
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ValueError("Invalid ACP pagination cursor")
                seen.add(cursor)
            raise ValueError("ACP page limit exceeded")
        finally:
            timer.cancel()
            process.kill()


class DevinIndex:
    """One worker publishes complete snapshots and retains the last good view."""

    def __init__(self, binary: str) -> None:
        self.binary = binary
        self.snapshot: dict[str, DevinSession] | None = None
        self.failed = False
        self._reported_failure = False
        self._thread: threading.Thread | None = None
        self._last_requested = float("-inf")

    @property
    def is_pending(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def refresh(self) -> None:
        now = time.monotonic()
        if (self.is_pending
                or now - self._last_requested < 5 or not shutil.which(self.binary)):
            return
        self._last_requested = now
        self._thread = threading.Thread(target=self._refresh, daemon=True,
                                        name="railmux-devin-index")
        self._thread.start()

    def _refresh(self) -> None:
        try:
            self.snapshot = scan(self.binary)
        except (OSError, ValueError):
            self.failed = True
        else:
            self.failed = False

    def take_warning(self) -> bool:
        if self.failed == self._reported_failure:
            return False
        self._reported_failure = self.failed
        return self.failed

    def get(self, session_id: str) -> DevinSession | None:
        return (self.snapshot or {}).get(session_id)

    def projects(self) -> list[Project]:
        by_cwd: dict[Path, list[SessionMeta]] = defaultdict(list)
        for record in (self.snapshot or {}).values():
            by_cwd[record.meta.project.real_path].append(record.meta)
        return sorted(
            (project_for(cwd, len(rows), max(s.last_mtime for s in rows))
             for cwd, rows in by_cwd.items()),
            key=lambda project: (-project.last_activity_ts, str(project.real_path)),
        )
