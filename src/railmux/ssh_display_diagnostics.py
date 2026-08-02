"""Best-effort, privacy-safe diagnostics for the most recent SSH display."""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from railmux import restart_state
from railmux.atomic_file import atomic_write_text
from railmux.platform.filelock import try_lock, unlock


_RECORD_SCHEMA = 1
_RECORD_NAME = "ssh-display.json"
_LOCK_NAME = "ssh-display.lock"
_OUTCOMES = frozenset(
    {
        "connected",
        "local_disconnect",
        "remote_detach",
        "remote_soft_quit",
        "remote_hard_quit",
        "transport_failed",
        "startup_failed",
    }
)


@dataclass(frozen=True)
class SshDisplayStats:
    reached_first_frame: bool = False
    first_frame_ms: int | None = None
    duration_ms: int | None = None
    frames: int = 0
    keyframes: int = 0
    patches: int = 0
    painted_rows: int = 0
    wire_bytes: int = 0
    reconnect_attempts: int = 0
    reconnect_successes: int = 0
    history_prefetch_requests: int = 0
    history_deep_requests: int = 0
    history_timeouts: int = 0
    history_anchor_rejects: int = 0


@dataclass(frozen=True)
class SshDisplayDiagnostic:
    status: str
    client_version: str | None = None
    protocol: int | None = None
    phase: str | None = None
    outcome: str | None = None
    age: str | None = None
    stats: SshDisplayStats | None = None


def _paths() -> tuple[Path, Path]:
    root = restart_state.runtime_state_dir()
    return root / _RECORD_NAME, root / _LOCK_NAME


def _bounded_nonnegative(value: object, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(maximum, max(0, value))


def _optional_ms(value: object) -> int | None:
    if value is None:
        return None
    bounded = _bounded_nonnegative(value, 30 * 24 * 60 * 60 * 1000)
    return bounded


def _coarse_age(recorded_at: object, now: float) -> str | None:
    if not isinstance(recorded_at, (int, float)) or isinstance(recorded_at, bool):
        return None
    seconds = max(0.0, now - float(recorded_at))
    if seconds < 60:
        return "under_1_minute"
    if seconds < 60 * 60:
        return f"{min(59, int(seconds // 60))}_minutes"
    if seconds < 24 * 60 * 60:
        return f"{min(23, int(seconds // 3600))}_hours"
    return f"{min(30, int(seconds // 86400))}_days_or_more"


def _with_lock(callback) -> bool:
    try:
        record_path, lock_path = _paths()
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        if not try_lock(fd):
            return False
        callback(record_path)
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        try:
            unlock(fd)
        except OSError:
            pass
        os.close(fd)


class SshDisplayRecorder:
    """One non-authoritative record owner; all failures are deliberately soft."""

    def __init__(self, client_version: str, protocol: int) -> None:
        self._token = secrets.token_hex(16)
        self._client_version = client_version
        self._protocol = protocol
        self._started_wall = time.time()
        self._started_mono = time.monotonic()
        self._recorded = False
        self._attach_record_attempted = False

    def _payload(
        self,
        *,
        phase: str,
        outcome: str | None,
        stats: SshDisplayStats,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "record_schema": _RECORD_SCHEMA,
            "token": self._token,
            "recorded_at": time.time(),
            "started_at": self._started_wall,
            "client_version": self._client_version,
            "protocol": self._protocol,
            "phase": phase,
            "outcome": outcome,
            "stats": asdict(stats),
        }
        return payload

    def mark_attached(self) -> None:
        self._attach_record_attempted = True
        payload = self._payload(
            phase="in_progress",
            outcome=None,
            stats=SshDisplayStats(),
        )

        def write(path: Path) -> None:
            atomic_write_text(
                path,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
            )

        self._recorded = _with_lock(write)

    def finish(self, outcome: str, stats: SshDisplayStats) -> None:
        if outcome not in _OUTCOMES:
            outcome = "transport_failed"
        if stats.duration_ms is None:
            stats = SshDisplayStats(
                **{
                    **asdict(stats),
                    "duration_ms": int(
                        max(0.0, time.monotonic() - self._started_mono) * 1000
                    ),
                }
            )
        payload = self._payload(phase="finished", outcome=outcome, stats=stats)

        def write(path: Path) -> None:
            current = restart_state.read_json_object(path)
            if self._attach_record_attempted and not self._recorded:
                return
            if self._recorded:
                if current is None or current.get("token") != self._token:
                    return
            elif current is not None:
                current_started = current.get("started_at")
                if (
                    isinstance(current_started, (int, float))
                    and not isinstance(current_started, bool)
                    and current_started > self._started_wall
                ):
                    # A startup failure is useful only until a newer client
                    # has attached. It must not steal that newer owner's final
                    # result merely because the older prompt resolved later.
                    return
            atomic_write_text(
                path,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
            )

        _with_lock(write)


def read_diagnostic(*, now: float | None = None) -> SshDisplayDiagnostic:
    """Read a bounded shareable view; internal tokens and times never escape."""
    try:
        record_path, _lock_path = _paths()
        payload = restart_state.read_json_object(record_path)
    except OSError:
        return SshDisplayDiagnostic("unavailable")
    if payload is None:
        return SshDisplayDiagnostic("none")
    if payload.get("record_schema") != _RECORD_SCHEMA:
        return SshDisplayDiagnostic("unavailable")
    phase = payload.get("phase")
    if phase not in {"in_progress", "finished"}:
        return SshDisplayDiagnostic("unavailable")
    outcome = payload.get("outcome")
    if phase == "finished" and outcome not in _OUTCOMES:
        return SshDisplayDiagnostic("unavailable")
    raw_stats = payload.get("stats")
    if not isinstance(raw_stats, dict):
        raw_stats = {}
    stats = SshDisplayStats(
        reached_first_frame=raw_stats.get("reached_first_frame") is True,
        first_frame_ms=_optional_ms(raw_stats.get("first_frame_ms")),
        duration_ms=_optional_ms(raw_stats.get("duration_ms")),
        frames=_bounded_nonnegative(raw_stats.get("frames")),
        keyframes=_bounded_nonnegative(raw_stats.get("keyframes")),
        patches=_bounded_nonnegative(raw_stats.get("patches")),
        painted_rows=_bounded_nonnegative(raw_stats.get("painted_rows")),
        wire_bytes=_bounded_nonnegative(raw_stats.get("wire_bytes")),
        reconnect_attempts=_bounded_nonnegative(raw_stats.get("reconnect_attempts")),
        reconnect_successes=_bounded_nonnegative(raw_stats.get("reconnect_successes")),
        history_prefetch_requests=_bounded_nonnegative(
            raw_stats.get("history_prefetch_requests")
        ),
        history_deep_requests=_bounded_nonnegative(
            raw_stats.get("history_deep_requests")
        ),
        history_timeouts=_bounded_nonnegative(raw_stats.get("history_timeouts")),
        history_anchor_rejects=_bounded_nonnegative(
            raw_stats.get("history_anchor_rejects")
        ),
    )
    version = payload.get("client_version")
    try:
        safe_version = (
            str(Version(version))
            if isinstance(version, str) and len(version) <= 64
            else None
        )
    except InvalidVersion:
        safe_version = None
    protocol = payload.get("protocol")
    return SshDisplayDiagnostic(
        status="recorded",
        client_version=safe_version,
        protocol=(
            protocol
            if isinstance(protocol, int)
            and not isinstance(protocol, bool)
            and 0 <= protocol <= 10000
            else None
        ),
        phase=phase,
        outcome=(
            "in_progress_or_ended_without_outcome"
            if phase == "in_progress"
            else outcome
        ),
        age=_coarse_age(
            payload.get("recorded_at"), time.time() if now is None else now
        ),
        stats=stats,
    )
