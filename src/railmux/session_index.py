"""Scan a project directory for sessions, extracting cheap metadata."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from railmux.models import Project, SessionMeta


# Duration (seconds) after which a running tool_use is presumed to be
# waiting for user approval rather than still executing.  Must be long
# enough to cover auto-approved tool runs (bash commands, API calls)
# but short enough that genuinely-blocked sessions surface quickly.
_TOOL_BLOCK_AGE_S = 10
_CHECKPOINT_BYTES = 4096


FileSignature = tuple[int, int, int, int]  # dev, ino, mtime_ns, size


@dataclass
class _SessionScanState:
    """Append-resumable metadata state for one provider-owned JSONL."""

    dev: int
    ino: int
    mtime_ns: int
    observed_size: int
    offset: int = 0
    checkpoint: bytes = b""
    title: str | None = None
    user_count: int = 0
    assistant_token_totals: dict[str, int] = field(default_factory=dict)
    anonymous_assistant_seq: int = 0
    git_branch: str | None = None
    last_user_message: str | None = None
    first_user_message: str | None = None
    last_rtype: str = ""
    last_stop_reason: str = ""
    background: bool = False

    def clone(self) -> _SessionScanState:
        return _SessionScanState(
            dev=self.dev,
            ino=self.ino,
            mtime_ns=self.mtime_ns,
            observed_size=self.observed_size,
            offset=self.offset,
            checkpoint=self.checkpoint,
            title=self.title,
            user_count=self.user_count,
            assistant_token_totals=self.assistant_token_totals.copy(),
            anonymous_assistant_seq=self.anonymous_assistant_seq,
            git_branch=self.git_branch,
            last_user_message=self.last_user_message,
            first_user_message=self.first_user_message,
            last_rtype=self.last_rtype,
            last_stop_reason=self.last_stop_reason,
            background=self.background,
        )


@dataclass(frozen=True)
class _SessionScanResult:
    signature: FileSignature
    meta: SessionMeta | None
    state: _SessionScanState | None


def list_sessions(project: Project) -> list[SessionMeta]:
    """List all sessions in a project, sorted by mtime descending."""
    results: list[SessionMeta] = []
    for path in project.claude_dir.glob("*.jsonl"):
        meta = _scan_session(project, path)
        if meta is not None:
            results.append(meta)
    results.sort(key=lambda s: s.last_mtime, reverse=True)
    return results


def _extract_text(content) -> str | None:
    """Pull meaningful display text from a user-message content field.

    Returns None when the content is a system command, tool result, or
    other internal markup that isn't useful for display.
    """
    if isinstance(content, str):
        s = content.strip()
        if not s:
            return None
        # Skip system commands injected by Claude Code harness.
        if s.startswith("<command-name>") or s.startswith("<local-command"):
            return None
        return s
    if isinstance(content, list):
        # Content blocks — prefer text blocks, skip tool results.
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "tool_result":
                continue  # tool output, not user text
            if btype == "text":
                t = block.get("text", "")
                if isinstance(t, str) and t.strip():
                    return t.strip()
        return None
    return None


def _nonnegative_int(value: object) -> int:
    """Return a provider count only when it is a genuine non-negative int."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _claude_usage_total(usage: object) -> int:
    """Total billed/context tokens reported for one Claude API message."""
    if not isinstance(usage, dict):
        return 0
    return sum(
        _nonnegative_int(usage.get(field))
        for field in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
    )


def _signature(st: os.stat_result) -> FileSignature:
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _checkpoint(fd: int, offset: int) -> bytes:
    start = max(0, offset - _CHECKPOINT_BYTES)
    size = offset - start
    if size <= 0:
        return b""
    pread = getattr(os, "pread", None)
    if pread is not None:
        raw = pread(fd, size, start)
    else:  # pragma: no cover - exercised by Python builds without pread
        current = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, start, os.SEEK_SET)
            raw = os.read(fd, size)
        finally:
            os.lseek(fd, current, os.SEEK_SET)
    return hashlib.blake2s(raw, digest_size=16).digest()


def _can_resume(fd: int, st: os.stat_result, state: _SessionScanState) -> bool:
    return (
        st.st_dev == state.dev
        and st.st_ino == state.ino
        and st.st_size > state.observed_size
        and st.st_mtime_ns >= state.mtime_ns
        and state.offset <= state.observed_size
        and _checkpoint(fd, state.offset) == state.checkpoint
    )


def _consume_record(state: _SessionScanState, rec: object) -> None:
    if not isinstance(rec, dict) or state.background:
        return
    # Background-job sessions are not interactive — they can't be resumed in
    # a terminal and shouldn't appear in the sidebar.
    if rec.get("sessionKind") == "bg":
        state.background = True
        return
    rtype = rec.get("type")
    if rtype == "ai-title":
        state.title = rec.get("aiTitle") or state.title
    # NOTE: "last-prompt" is deliberately NOT treated as a turn. Claude Code
    # writes it after an assistant turn completes.
    elif rtype == "user":
        state.last_rtype = "user"
        state.last_stop_reason = ""
        msg = rec.get("message", {}) or {}
        text = _extract_text(msg.get("content", "")) if isinstance(msg, dict) else None
        if text is not None:
            state.user_count += 1
            state.last_user_message = text
            if state.first_user_message is None:
                state.first_user_message = text
    elif rtype == "assistant":
        state.last_rtype = "assistant"
        msg = rec.get("message", {}) or {}
        if not isinstance(msg, dict):
            msg = {}
        state.last_stop_reason = msg.get("stop_reason", "")
        message_id = msg.get("id")
        record_uuid = rec.get("uuid")
        if isinstance(message_id, str) and message_id:
            assistant_key = f"message:{message_id}"
        elif isinstance(record_uuid, str) and record_uuid:
            assistant_key = f"record:{record_uuid}"
        else:
            assistant_key = f"anonymous:{state.anonymous_assistant_seq}"
            state.anonymous_assistant_seq += 1
        usage_total = _claude_usage_total(msg.get("usage"))
        state.assistant_token_totals[assistant_key] = max(
            usage_total,
            state.assistant_token_totals.get(assistant_key, 0),
        )
    if state.git_branch is None:
        git_branch = rec.get("gitBranch")
        if isinstance(git_branch, str) and git_branch:
            state.git_branch = git_branch


def _consume_lines(state: _SessionScanState, raw: bytes) -> None:
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line.decode("utf-8", errors="replace"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        _consume_record(state, rec)


def _meta_from_state(
    project: Project,
    jsonl_path: Path,
    state: _SessionScanState,
    st: os.stat_result,
) -> SessionMeta | None:
    if state.background:
        return None
    assistant_count = len(state.assistant_token_totals)
    message_count = state.user_count + assistant_count
    if message_count == 0 or (state.user_count > 0 and assistant_count == 0):
        return None

    pending_tool = (
        state.last_rtype == "assistant" and state.last_stop_reason == "tool_use"
    )
    if state.last_rtype == "user":
        status = "busy"
    elif pending_tool:
        age = time.time() - st.st_mtime
        status = "blocked" if age > _TOOL_BLOCK_AGE_S else "busy"
    else:
        status = "idle"

    title = state.title
    if title is None and state.first_user_message:
        first_line = state.first_user_message.split("\n")[0]
        title = first_line[:60] + ("..." if len(first_line) > 60 else "")
    elif title is not None and len(title) > 80:
        title = title[:80] + "…"

    preview: str | None = None
    if state.last_user_message:
        first_line = state.last_user_message.split("\n")[0]
        preview = first_line[:117] + "..." if len(first_line) > 120 else first_line
    return SessionMeta(
        project=project,
        session_id=jsonl_path.stem,
        jsonl_path=jsonl_path,
        title=title,
        message_count=message_count,
        token_total=sum(state.assistant_token_totals.values()),
        last_mtime=st.st_mtime,
        size_bytes=st.st_size,
        git_branch=state.git_branch,
        last_user_message=preview,
        status=status,
        pending_tool=pending_tool,
    )


def _scan_session_incremental(
    project: Project,
    jsonl_path: Path,
    previous: _SessionScanState | None = None,
) -> _SessionScanResult:
    session_id = jsonl_path.stem
    if not _looks_like_uuid(session_id):
        return _SessionScanResult((0, 0, 0, 0), None, None)
    try:
        fd = os.open(jsonl_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return _SessionScanResult((0, 0, 0, 0), None, None)
    try:
        st = os.fstat(fd)
        signature = _signature(st)
        if previous is not None and _can_resume(fd, st, previous):
            state = previous.clone()
            start = state.offset
        else:
            state = _SessionScanState(
                dev=st.st_dev,
                ino=st.st_ino,
                mtime_ns=st.st_mtime_ns,
                observed_size=st.st_size,
            )
            start = 0
        remaining = max(0, st.st_size - start)
        chunks: list[bytes] = []
        os.lseek(fd, start, os.SEEK_SET)
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        newline = raw.rfind(b"\n")
        committed_size = newline + 1 if newline >= 0 else 0
        if committed_size:
            _consume_lines(state, raw[:committed_size])
            state.offset = start + committed_size
        state.dev = st.st_dev
        state.ino = st.st_ino
        state.mtime_ns = st.st_mtime_ns
        state.observed_size = st.st_size
        state.checkpoint = _checkpoint(fd, state.offset)

        # A valid last JSON object without a newline remains visible for direct
        # scans, but is not committed into resumable state until its record
        # boundary is durable. This prevents counting it twice after append.
        visible_state = state
        trailing = raw[committed_size:].strip()
        if trailing:
            try:
                record = json.loads(trailing.decode("utf-8", errors="replace"))
            except (UnicodeError, json.JSONDecodeError):
                pass
            else:
                visible_state = state.clone()
                _consume_record(visible_state, record)
        return _SessionScanResult(
            signature,
            _meta_from_state(project, jsonl_path, visible_state, st),
            state,
        )
    except OSError:
        return _SessionScanResult((0, 0, 0, 0), None, None)
    finally:
        os.close(fd)


def _scan_session(project: Project, jsonl_path: Path) -> SessionMeta | None:
    return _scan_session_incremental(project, jsonl_path).meta


def _looks_like_uuid(s: str) -> bool:
    # 8-4-4-4-12 hex pattern
    parts = s.split("-")
    if len(parts) != 5:
        return False
    lengths = [8, 4, 4, 4, 12]
    if [len(p) for p in parts] != lengths:
        return False
    try:
        for p in parts:
            int(p, 16)
    except ValueError:
        return False
    return True
