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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from railmux.models import Project, SessionMeta

_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z")
_TIMEOUT = 10
_MAX_LINE = 1024 * 1024
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\]8;[^\x1b\x07]*(?:\x1b\\|\x07)")
_RULE_LINE_RE = re.compile(r"^─{10,}(?: .+ ─)?$")
_BRAILLE_SPINNER_RE = re.compile(r"^[\u2800-\u28ff]+\s+(.+)$")
_BUSY_VERBS = (
    "Thinking",
    "Typing",
    "Running tools",
    "Working",
    "Compacting",
    "Retrying connection",
    "Subagents running",
    "Answering a side question",
)
_BLOCKED_HEADERS = (
    "Permission to run the command",
    "Permission to fetch",
    "Permission to call MCP tool",
    "Write access to '",
    "Read access to '",
)
_BLOCKED_CHOICES = (
    "(Approve once)",
    "(Auto-approve edits in workspace dirs)",
    "(Auto-approve all tools)",
    "Yes, switch to bypass mode",
)


def project_for(cwd: Path, count: int = 0, activity: float = 0) -> Project:
    key = hashlib.sha256(os.fsencode(cwd)).hexdigest()[:24]
    return Project(cwd, f"-dv-{key}", Path(), count, activity)


def command(
    binary: str,
    session_id: str | None = None,
    *,
    bypass: bool = False,
) -> list[str]:
    # tmux owns the lifetime of the interactive agent. In-process mode also
    # gives discovery an exact lock owner in the pane's process tree.
    argv = [binary, "--process-model", "in-process"]
    if session_id is not None:
        if not _ID.fullmatch(session_id):
            raise ValueError("Invalid Devin session ID")
        argv.extend(("--resume", session_id))
    elif bypass:
        argv.extend(("--permission-mode", "dangerous"))
    return argv


def delete_command(binary: str, session_id: str) -> list[str]:
    """Build the exact non-interactive ``devin rm`` invocation for one ID."""
    if not _ID.fullmatch(session_id):
        raise ValueError("Invalid Devin session ID")
    return [binary, "rm", "--force", "--process-model", "in-process", session_id]


def _plain_line(raw: str) -> str:
    return _ANSI_RE.sub("", raw).rstrip()


def _is_footer_rule(text: str) -> bool:
    return bool(_RULE_LINE_RE.match(text))


def _is_busy_spinner(text: str) -> bool:
    stripped = text.strip()
    match = _BRAILLE_SPINNER_RE.match(stripped)
    if match is None:
        return False
    detail = match.group(1)
    return (
        "(esc twice to interrupt)" in detail
        or any(detail.startswith(verb) for verb in _BUSY_VERBS)
    )


def classify_pane_status(lines: Sequence[str]) -> str:
    """Classify one live Devin viewport into ``blocked``, ``busy``, or ``idle``.

    Inspects only Devin's live bottom chrome (the prompt box, its status bar,
    the spinner row immediately above the prompt box, or an active approval /
    input card at the bottom of the viewport) so quoted text in earlier
    conversation lines cannot falsely mark an idle pane as busy or blocked.
    """
    plain = [_plain_line(line) for line in lines]
    while plain and not plain[-1].strip():
        plain.pop()
    if not plain:
        return "idle"

    # Locate the live prompt box at the bottom of the viewport:
    #   [optional spinner]
    #   ────... [title ─]
    #   ❭ <input / placeholder>
    #   ────...
    #   <status bar>
    top_rule: int | None = None
    bottom_rule: int | None = None
    status_line: str = ""
    if len(plain) >= 4:
        bottom_idx = len(plain) - 1
        # Status line sits immediately below the bottom rule.
        for candidate_bottom in (bottom_idx - 1, bottom_idx):
            if candidate_bottom >= 2 and _is_footer_rule(plain[candidate_bottom]):
                # Search up to 10 rows above for the matching top rule.
                search_start = max(0, candidate_bottom - 10)
                for candidate_top in range(candidate_bottom - 1, search_start - 1, -1):
                    if _is_footer_rule(plain[candidate_top]):
                        input_slice = plain[candidate_top + 1 : candidate_bottom]
                        if input_slice and input_slice[0].startswith("❭"):
                            top_rule = candidate_top
                            bottom_rule = candidate_bottom
                            if candidate_bottom + 1 < len(plain):
                                status_line = plain[candidate_bottom + 1]
                            break
                if top_rule is not None:
                    break

    tail_start = max(0, len(plain) - 16)
    tail_region = plain[tail_start:]

    # Active approval / question prompts replace or sit at the bottom chrome.
    if any(
        "Devin needs input" in row or "Devin needs authentication" in row
        for row in plain[max(0, len(plain) - 8) :]
    ):
        return "blocked"
    if top_rule is None:
        has_perm_header = any(
            any(header in row for header in _BLOCKED_HEADERS)
            for row in tail_region
        )
        has_perm_choice = any(
            any(choice in row for choice in _BLOCKED_CHOICES)
            for row in tail_region
        )
        if has_perm_header and has_perm_choice:
            return "blocked"
        # An active turn without a visible prompt box (e.g., very narrow or
        # transient redraw) still renders the spinner at the bottom.
        for row in plain[max(0, len(plain) - 4) :]:
            if _is_busy_spinner(row):
                return "busy"
        return "idle"

    # Prompt box is present: check the spinner row immediately above it,
    # the active-turn placeholder inside it, and the startup status row.
    checked = 0
    for idx in range(top_rule - 1, -1, -1):
        row = plain[idx].strip()
        if not row:
            continue
        if _is_busy_spinner(row):
            return "busy"
        checked += 1
        if checked >= 2:
            break

    assert bottom_rule is not None
    input_rows = plain[top_rule + 1 : bottom_rule]
    if any(row.strip() == "❭ Guide Devin while it works" for row in input_rows):
        return "busy"
    if "Starting session…" in status_line or "Starting session..." in status_line:
        return "busy"
    return "idle"


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
        self._lock = threading.Lock()
        self._generation = 0
        self._rescan_requested = False

    @property
    def is_pending(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def refresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if self.is_pending:
                if force:
                    self._rescan_requested = True
                return
            if (not force and now - self._last_requested < 5) or not shutil.which(self.binary):
                return
            self._last_requested = now
            self._rescan_requested = False
            generation = self._generation
            self._thread = threading.Thread(
                target=self._refresh, args=(generation,), daemon=True,
                name="railmux-devin-index",
            )
            self._thread.start()

    def _refresh(self, generation: int | None = None) -> None:
        if generation is None:
            generation = self._generation
        try:
            records = scan(self.binary)
        except (OSError, ValueError):
            with self._lock:
                self.failed = True
                rescan = self._rescan_requested
                self._rescan_requested = False
        else:
            with self._lock:
                if generation == self._generation:
                    self.snapshot = records
                    self.failed = False
                rescan = self._rescan_requested or generation != self._generation
                self._rescan_requested = False
                if rescan:
                    self._last_requested = float("-inf")
        if rescan and shutil.which(self.binary):
            with self._lock:
                self._last_requested = time.monotonic()
                next_gen = self._generation
                self._thread = threading.Thread(
                    target=self._refresh, args=(next_gen,), daemon=True,
                    name="railmux-devin-index",
                )
                self._thread.start()

    def invalidate(self, *, deleted_ids: Iterable[str] = ()) -> None:
        """Drop deleted IDs immediately and prevent an in-flight scan from resurrecting them."""
        removed = set(deleted_ids)
        with self._lock:
            self._generation += 1
            self._last_requested = float("-inf")
            if self.snapshot is not None and removed:
                self.snapshot = {
                    sid: record
                    for sid, record in self.snapshot.items()
                    if sid not in removed
                }
            if self.is_pending:
                self._rescan_requested = True

    def mark_unlocked(self, session_id: str) -> None:
        """Clear a stale lock flag after Railmux stops the local Devin pane."""
        with self._lock:
            self._generation += 1
            self._last_requested = float("-inf")
            if self.snapshot is not None and session_id in self.snapshot:
                record = self.snapshot[session_id]
                if record.locked or record.holder_pid is not None:
                    updated = dict(self.snapshot)
                    updated[session_id] = DevinSession(record.meta, False, None)
                    self.snapshot = updated
            if self.is_pending:
                self._rescan_requested = True

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
