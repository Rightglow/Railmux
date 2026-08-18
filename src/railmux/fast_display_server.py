"""Remote half of the coalesced full-window SSH client.

This module deliberately attaches one real tmux client inside a private PTY.
tmux therefore remains the compositor and input authority for the Railmux
sidebar, borders, status line, and agent panes.  PTY output is consumed into a
headless terminal screen and only bounded latest-state frames cross SSH.

Several helpers may attach to the same managed window. On EOF or lease expiry,
each helper terminates only the exact tmux *client process* it created; it never
kills the session, panes, or agents. An explicit, locally confirmed replacement
path exists solely to recover older helpers which held the attach lock for life.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import select
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Sequence

from railmux import (
    __version__,
    local_open,
    restart_state,
    tmux_ctl,
    tmux_health,
    tmux_server,
)
from railmux import transcript as transcript_renderer
from railmux.config import Config, ConfigError, load_config
from railmux.fast_display_protocol import (
    HistoryBatch,
    HistorySnapshot,
    InputKind,
    InputFrameDecoder,
    MAX_HISTORY_LINES,
    PathKind,
    PathOpenResult,
    PathResult,
    PROTOCOL_VERSION,
    REMOTE_CONFIG_PROTOCOL,
    REMOTE_ATTACH_ACCEPTED,
    REMOTE_ATTACH_BUSY,
    REMOTE_HELLO_PREFIX,
    REMOTE_START,
    RemoteExit,
    decode_claude_history_choice,
    decode_history_prefetch,
    decode_history_request,
    decode_path_request,
    decode_path_open_request,
    encode_claude_history_policy_result,
    encode_clipboard_copy,
    encode_history_batch,
    encode_history_snapshot,
    encode_path_result,
    encode_path_open_result,
    encode_update,
)
from railmux.fast_display_history import HistoryCaptureJob, HistoryCaptureWorker
from railmux.terminal_screen import (
    Osc52ClipboardDecoder as _Osc52ClipboardDecoder,
    ScreenState as _ScreenState,
    build_screen_update,
    extended_pyte as _extended_pyte,
    render_rows,
)
from railmux.ui.workspace import (
    COMPACT_ENTER_HEIGHT,
    COMPACT_ENTER_WIDTH,
    COMPACT_RESIZE_OPTION,
    COMPACT_RESIZE_SEQUENCE,
)
from railmux.settings import Settings
from railmux.provider_paths import provider_path
from railmux.runtime_config import (
    activate_runtime_environment,
    check_executable,
    check_utf8_locale,
)
from railmux.tool_panes import (
    TOOL_PANE_OPTION,
    is_tool_pane_marker,
    manager_for_session,
)


_COMPACT_PREPARE_TIMEOUT = 0.4
_COMPACT_TMUX_TIMEOUT = 0.15


class DisplayServerError(RuntimeError):
    """A bounded error safe to show through SSH stderr."""


class DisplayServerBusy(DisplayServerError):
    """The short attach mutex is held by a legacy or starting helper."""


def apply_claude_history_choice(
    policy: str,
    *,
    persistent: bool,
    current_override: str | None,
    settings: Settings | None = None,
) -> tuple[bool, str | None]:
    """Apply one remote history choice without losing a prior override."""
    applied = (
        (settings or Settings()).set_claude_history_policy(policy)
        if persistent
        else True
    )
    if not applied:
        return False, current_override
    # A persistent choice must leave the settings file authoritative so an
    # Options edit in the running Railmux UI takes effect on the next capture.
    return True, None if persistent else policy


def refresh_claude_history_override(
    current_override: str | None,
    persisted_at_choice: str | None,
    current_persisted: str,
) -> tuple[str | None, str | None]:
    """Clear a one-connection choice after Options changes its baseline."""
    if (
        current_override is not None
        and persisted_at_choice is not None
        and current_persisted != persisted_at_choice
    ):
        return None, None
    return current_override, persisted_at_choice


_WATCHDOG_INTERVAL = 5.0
_WATCHDOG_FAILURES = 3
_START_HANDSHAKE_TIMEOUT = 300.0
_ATTACH_LOCK_TIMEOUT = 2.0
_REPLACE_LOCK_TIMEOUT = 5.0
_CLIENT_LEASE_TIMEOUT = 45.0
_WINDOW_SIZE_ATTEMPTS = 3
# Leave generous headroom below both protocol byte ceilings after metadata,
# zlib framing, and viewport padding. An unusually wide, heavily styled
# scrollback may therefore return fewer lines than requested, which the client
# treats as the effective end instead of allowing the helper to fail.
_HISTORY_SNAPSHOT_RAW_BUDGET = 12 * 1024 * 1024


def _fast_dependency_ready() -> bool:
    """Return whether the installed SSH display dependency is usable."""
    try:
        import pyte
        from pyte import modes as _modes  # noqa: F401

        _extended_pyte(pyte)
    except (ImportError, AttributeError, TypeError):
        return False
    return True


def _emit_remote_hello(
    ready: bool,
    *,
    config_status: str = "valid",
    tmux_configured: bool = False,
    tmux_available: bool | None = None,
) -> None:
    """Describe compatibility before acquiring or attaching any tmux state."""
    payload = json.dumps(
        {
            "protocol": PROTOCOL_VERSION,
            "ready": ready,
            "tmux": (
                shutil.which("tmux") is not None
                if tmux_available is None
                else tmux_available
            ),
            "config_status": config_status,
            "tmux_configured": tmux_configured,
            "config_protocol": REMOTE_CONFIG_PROTOCOL,
            "platform": (
                "windows-msys2"
                if os.environ.get("RAILMUX_WINDOWS_RUNTIME") == "msys2"
                else "posix"
            ),
            "version": __version__,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    sys.stdout.buffer.write(REMOTE_HELLO_PREFIX + payload + b"\n")
    sys.stdout.buffer.flush()


def _await_client_start(timeout: float = _START_HANDSHAKE_TIMEOUT) -> bool:
    """Wait for the compatible local client before attaching to tmux."""
    readable, _writable, _exceptional = select.select(
        [sys.stdin.buffer], [], [], timeout
    )
    if not readable:
        return False
    return sys.stdin.buffer.readline(len(REMOTE_START) + 1) == REMOTE_START


@dataclass(frozen=True)
class _PaneGeometry:
    pane_id: str
    x: int
    y: int
    width: int
    height: int
    history_server: tmux_server.TmuxServerTarget | None = None
    history_pane_id: str | None = None
    mouse_forwardable: bool = False
    history_size: int = 0
    alternate_on: bool = False
    transcript_source: str | None = None
    transcript_backed: bool = False
    transcript_provider: str | None = None
    claude_history_policy: str = "ask"
    history_generation: int = 0
    canonical_history: bool = False


@dataclass(frozen=True)
class _TranscriptCacheEntry:
    identity: tuple[int, int, int, int]
    rows: tuple[bytes, ...]
    more_available: bool
    total_rows: int = 0
    timeline_stable: bool = True


@dataclass(frozen=True)
class _TranscriptFormatCacheEntry:
    identity: tuple[int, int, int, int]
    formatted: str
    truncated: bool


_TRANSCRIPT_CACHE: OrderedDict[tuple[str, int], _TranscriptCacheEntry] = OrderedDict()
_TRANSCRIPT_CACHE_LIMIT = 4
_TRANSCRIPT_FORMAT_CACHE: OrderedDict[
    tuple[str, str], _TranscriptFormatCacheEntry
] = OrderedDict()
_TRANSCRIPT_FORMAT_CACHE_LIMIT = 2
_TRANSCRIPT_FORMAT_CACHE_MAX_CHARS = 4 * 1024 * 1024
_INFERRED_TRANSCRIPTS: OrderedDict[tuple[str, int], tuple[bool, str | None]] = (
    OrderedDict()
)
_INFERRED_TRANSCRIPT_LIMIT = 8
_PANE_GEOMETRY_CACHE: OrderedDict[
    tuple[str, str], tuple[float, tuple[_PaneGeometry, ...]]
] = OrderedDict()
_PANE_GEOMETRY_CACHE_LIMIT = 8
_PANE_GEOMETRY_CACHE_TTL = 0.25
_TRANSCRIPT_MAX_BYTES = 32 * 1024 * 1024
_SESSION_BINDING_OPTION = "@railmux_binding_v1"
_SWAP_OPTIONS = ("@railmux_swap_primary", "@railmux_swap_secondary")
_HISTORY_GENERATION_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _history_generation(marker: str, server_identity: str = "") -> int:
    """Map one validated provider UUID to a bounded opaque wire epoch."""
    generation_marker = f"{marker}@{server_identity}"
    if marker.startswith(tmux_ctl.RAILMUX_CANONICAL_HISTORY_PREFIX):
        marker = marker[len(tmux_ctl.RAILMUX_CANONICAL_HISTORY_PREFIX) :]
    if not _HISTORY_GENERATION_RE.fullmatch(marker):
        return 0
    return int.from_bytes(
        hashlib.blake2b(
            generation_marker.lower().encode("ascii"),
            digest_size=8,
            person=b"railmux-history",
        ).digest(),
        "big",
    )


def _tmux_output(*args: str) -> str:
    try:
        return subprocess.check_output(
            tmux_server.tmux_argv(*args),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        ).strip()
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise DisplayServerError("tmux command failed") from exc


def _try_session_id(session: str) -> str | None:
    """Resolve a named session without treating absence as a server failure."""
    try:
        value = subprocess.check_output(
            tmux_server.tmux_argv(
                "display-message", "-p", "-t", session, "#{session_id}"
            ),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        ).strip()
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return None
    if not value.startswith("$") or not value[1:].isdigit():
        return None
    return value


def _session_controller_pane(session_id: str) -> str | None:
    """Return the unique live controller in an exact session-scoped snapshot.

    A swap keeper is a grouped tmux session sharing the Railmux window.  When
    that keeper exists, formatting ``#{session_id}`` with the shared pane as
    the target may report the keeper's ID even though the pane is also visible
    through the managed Railmux session.  Listing panes through the immutable
    managed session ID preserves the intended session scope and lets us check
    membership without confusing a healthy grouped window for identity drift.
    """
    try:
        snapshot = _tmux_output(
            "list-panes",
            "-t",
            session_id,
            "-F",
            "#{session_id}\t#{pane_id}\t#{pane_dead}\t"
            "#{@railmux_controller_pane}",
        )
    except DisplayServerError:
        return None

    panes: dict[str, bool] = {}
    controllers: set[str] = set()
    for row in snapshot.splitlines():
        parts = row.split("\t")
        if len(parts) != 4:
            return None
        row_session, pane_id, pane_dead, controller = parts
        if (
            row_session != session_id
            or not pane_id.startswith("%")
            or not pane_id[1:].isdigit()
            or pane_id in panes
            or pane_dead not in {"0", "1"}
            or not controller.startswith("%")
            or not controller[1:].isdigit()
        ):
            return None
        panes[pane_id] = pane_dead == "1"
        controllers.add(controller)

    if len(controllers) != 1:
        return None
    controller = next(iter(controllers))
    if controller not in panes or panes[controller]:
        return None
    return controller


def _live_controller(session_id: str) -> str | None:
    """Return the controller pane only when its managed identity is live."""
    return _session_controller_pane(session_id)


def _ensure_railmux_session(session: str, timeout: float = 15.0) -> str:
    """Return a session ID, starting the default Railmux session if absent."""
    session_id = _try_session_id(session)
    if session_id is not None:
        return session_id
    if session != "railmux":
        raise DisplayServerError(
            f"Railmux session is not available: {session}; only the default "
            "railmux session can be started automatically"
        )

    railmux_command = shlex.join(
        [
            sys.executable,
            "-m",
            "railmux",
            "--inside-tmux",
            "--no-scroll-coalescing",
        ]
    )
    try:
        result = subprocess.run(
            tmux_server.tmux_argv("new-session", "-d", "-s", session, railmux_command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DisplayServerError("could not start the Railmux tmux session") from exc

    # A concurrent client may have won the new-session race. In either case,
    # accept only the live session that can now be resolved by immutable ID.
    deadline = time.monotonic() + (1.0 if result.returncode else timeout)
    while time.monotonic() < deadline:
        session_id = _try_session_id(session)
        if session_id is not None and _live_controller(session_id) is not None:
            return session_id
        time.sleep(0.05)
    raise DisplayServerError("Railmux did not become ready after it was started")


def _validate_railmux(session: str) -> str:
    """Return the immutable managed session ID after conservative validation."""
    try:
        session_id = _tmux_output(
            "display-message", "-p", "-t", session, "#{session_id}"
        )
    except DisplayServerError as exc:
        raise DisplayServerError(
            f"Railmux session is not available: {session}"
        ) from exc
    if not session_id.startswith("$") or not session_id[1:].isdigit():
        raise DisplayServerError("tmux returned an invalid session identity")

    if _session_controller_pane(session_id) is None:
        raise DisplayServerError("the target is not a live managed Railmux window")

    return session_id


def _classify_remote_exit(session_id: str) -> RemoteExit:
    """Classify normal tmux-client exit without mutating any session state."""
    if _try_session_id(session_id) != session_id:
        return RemoteExit.HARD_QUIT
    if _live_controller(session_id) is not None:
        return RemoteExit.DETACHED
    return RemoteExit.SOFT_QUIT


def _classify_observed_exit(
    session_id: str,
    target: tmux_server.TmuxServerTarget,
) -> RemoteExit:
    """Distinguish an intentional hard quit from an abrupt tmux loss."""
    if tmux_health.soft_exit_intended(
        server_pid=target.server_pid, session_id=session_id
    ):
        return RemoteExit.SOFT_QUIT

    exit_kind = _classify_remote_exit(session_id)
    if exit_kind is not RemoteExit.HARD_QUIT:
        return exit_kind
    if tmux_health.consume_clean_exit(
        server_pid=target.server_pid, session_id=session_id
    ):
        return exit_kind
    tmux_health.record_incident(
        component="remote-display",
        reason="remote-display-server-exit",
        consecutive_failures=1,
    )
    raise DisplayServerError(
        "the managed tmux session disappeared unexpectedly; run "
        "'railmux doctor' for diagnostics"
    )


def _option_value(argv: list[str]) -> str:
    try:
        return subprocess.check_output(
            argv,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.5,
        ).strip()
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return ""


def _binding_transcript_source(raw: str) -> tuple[bool, str | None]:
    """Return whether a valid binding is Claude-owned and its exact locator."""
    if not raw or len(raw) > 8192:
        return False, None
    try:
        binding = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, None
    if not isinstance(binding, dict):
        return False, None
    session_id = binding.get("key")
    tmux_name = binding.get("tmux_name")
    cwd = binding.get("cwd")
    if (
        binding.get("session_type") != "claude"
        or not isinstance(session_id, str)
        or not re.fullmatch(r"[A-Za-z0-9-]{1,256}", session_id)
        or session_id.startswith("__new__-")
        or not isinstance(tmux_name, str)
        or not tmux_name
        or not isinstance(cwd, str)
        or not Path(cwd).is_absolute()
    ):
        return False, None

    configured = binding.get("transcript_path")
    candidates: list[Path] = []
    if isinstance(configured, str) and configured:
        candidates.append(Path(configured))
    else:
        claude_root = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        projects = claude_root / "projects"
        try:
            candidates.extend(projects.glob(f"*/{session_id}.jsonl"))
        except OSError:
            pass
    markers: set[str] = set()
    for path in candidates:
        marker = tmux_server.encode_transcript_source("claude", session_id, path)
        if marker is None:
            continue
        opened = tmux_server.open_transcript_source(marker)
        if opened is not None:
            os.close(opened[1])
            markers.add(marker)
    marker = next(iter(markers)) if len(markers) == 1 else None
    return marker is not None, marker


def _swap_binding_for_pane(
    session_id: str,
    window_id: str,
    pane_id: str,
    pane_pid: int,
) -> str:
    """Resolve an old displayed swap to its exact home-session binding."""
    matches: list[str] = []
    for option in _SWAP_OPTIONS:
        raw = _option_value(
            tmux_server.tmux_argv("show-window-options", "-v", "-t", window_id, option)
        )
        if not raw or len(raw) > 8192:
            continue
        try:
            state = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            not isinstance(state, dict)
            or state.get("version") != 1
            or state.get("phase") != "displayed"
            or state.get("agent_pane_id") != pane_id
            or state.get("agent_pane_pid") != pane_pid
            or state.get("display_window_id") != window_id
            or state.get("outer_session_id") != session_id
        ):
            continue
        tmux_name = state.get("agent_tmux_name")
        if not isinstance(tmux_name, str) or not tmux_name:
            continue
        binding = _option_value(
            tmux_server.tmux_argv(
                "show-options",
                "-v",
                "-t",
                tmux_name,
                _SESSION_BINDING_OPTION,
            )
        )
        if binding:
            matches.append(binding)
    return matches[0] if len(matches) == 1 else ""


def _inferred_transcript_source(
    *,
    session_id: str,
    window_id: str,
    pane_id: str,
    pane_pid: int,
    history_server: tmux_server.TmuxServerTarget | None,
    history_pane_id: str | None,
) -> tuple[bool, str | None]:
    """Recover a pre-v10 Claude locator from exact Railmux-owned metadata."""
    cache_key = (pane_id, pane_pid)
    cached = _INFERRED_TRANSCRIPTS.get(cache_key)
    if cached is not None:
        _INFERRED_TRANSCRIPTS.move_to_end(cache_key)
        return cached
    if history_server is not None and history_pane_id is not None:
        binding = _option_value(
            tmux_server.target_argv(
                history_server,
                "show-options",
                "-v",
                "-t",
                history_pane_id,
                _SESSION_BINDING_OPTION,
            )
        )
    else:
        binding = _swap_binding_for_pane(session_id, window_id, pane_id, pane_pid)
    result = _binding_transcript_source(binding)
    if not result[0] or result[1] is not None:
        _INFERRED_TRANSCRIPTS[cache_key] = result
        _INFERRED_TRANSCRIPTS.move_to_end(cache_key)
        while len(_INFERRED_TRANSCRIPTS) > _INFERRED_TRANSCRIPT_LIMIT:
            _INFERRED_TRANSCRIPTS.popitem(last=False)
    return result


def _pane_at_pointer(
    session_id: str,
    x: int,
    y: int,
    *,
    claude_history_policy: str | None = None,
    use_cache: bool = False,
) -> _PaneGeometry | None:
    """Resolve a non-controller pane from 1-based client coordinates."""
    pointer_x, pointer_y = x - 1, y - 1
    for pane in _list_agent_panes(
        session_id,
        claude_history_policy=claude_history_policy,
        use_cache=use_cache,
    ):
        if (
            pane.x <= pointer_x < pane.x + pane.width
            and pane.y <= pointer_y < pane.y + pane.height
        ):
            return pane
    return None


def _list_agent_panes(
    session_id: str,
    *,
    claude_history_policy: str | None = None,
    use_cache: bool = False,
) -> tuple[_PaneGeometry, ...]:
    """Return one coherent, fail-closed generation of visible agent panes."""
    if claude_history_policy is None:
        claude_history_policy = Settings().claude_history_policy
    cache_key = (session_id, claude_history_policy)
    now = time.monotonic()
    cached = _PANE_GEOMETRY_CACHE.get(cache_key) if use_cache else None
    if cached is not None and now - cached[0] <= _PANE_GEOMETRY_CACHE_TTL:
        _PANE_GEOMETRY_CACHE.move_to_end(cache_key)
        return cached[1]
    try:
        output = subprocess.check_output(
            tmux_server.tmux_argv(
                "list-panes",
                "-t",
                session_id,
                "-F",
                "#{session_id} #{window_id} #{window_zoomed_flag} "
                "#{pane_active} #{pane_id} #{pane_pid} #{pane_left} "
                "#{pane_top} #{pane_width} #{pane_height} "
                "#{history_size} #{alternate_on} #{mouse_any_flag} "
                f"#{{{tmux_server.HISTORY_SOURCE_OPTION}}} "
                f"#{{{tmux_server.TRANSCRIPT_SOURCE_OPTION}}} "
                f"#{{{tmux_ctl.RAILMUX_HISTORY_GENERATION_OPTION}}} "
                f"#{{{TOOL_PANE_OPTION}}} #{{pid}} "
                "#{@railmux_controller_pane}",
            ),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ()
    rows: list[tuple[bool, bool, _PaneGeometry]] = []
    seen: set[str] = set()
    controller: str | None = None
    for raw_row in output.splitlines():
        fields = raw_row.split(" ")
        if (
            len(fields) != 19
            or fields[0] != session_id
            or fields[2] not in ("0", "1")
            or fields[3] not in ("0", "1")
            or fields[11] not in ("0", "1")
            or fields[12] not in ("0", "1")
            or not fields[17].isdigit()
            or not fields[18].startswith("%")
            or not fields[18][1:].isdigit()
        ):
            return ()
        if controller is None:
            controller = fields[18]
        elif fields[18] != controller:
            return ()
        window_id = fields[1]
        pane_id = fields[4]
        try:
            pane_pid = int(fields[5])
            left, top, width, height = map(int, fields[6:10])
            history_size = int(fields[10])
        except ValueError:
            return ()
        if (
            pane_id in seen
            or not pane_id.startswith("%")
            or not pane_id[1:].isdigit()
            or not window_id.startswith("@")
            or not window_id[1:].isdigit()
            or pane_pid <= 0
            or left < 0
            or top < 0
            or width <= 0
            or height <= 0
            or history_size < 0
        ):
            return ()
        seen.add(pane_id)
        if is_tool_pane_marker(fields[16], outer_session_id=session_id):
            continue
        history_server = None
        history_pane_id = None
        marker = fields[13]
        if marker:
            source = tmux_server.resolve_history_pane(marker, timeout=0.25)
            if source is not None:
                history_server, history_pane_id = source
        transcript_marker = fields[14] or None
        transcript_locator = (
            tmux_server.decode_transcript_source(transcript_marker)
            if transcript_marker is not None
            else None
        )
        transcript_backed = transcript_locator is not None
        if not transcript_backed and fields[11] == "1" and fields[12] == "1":
            transcript_backed, transcript_marker = _inferred_transcript_source(
                session_id=session_id,
                window_id=window_id,
                pane_id=pane_id,
                pane_pid=pane_pid,
                history_server=history_server,
                history_pane_id=history_pane_id,
            )
        if transcript_backed and transcript_marker is not None:
            opened = tmux_server.open_transcript_source(transcript_marker)
            transcript_backed = opened is not None
            if opened is not None:
                transcript_locator = opened[0]
                os.close(opened[1])
        history_marker = fields[15]
        canonical_history = bool(
            transcript_backed
            and transcript_locator is not None
            and history_marker
            == (
                f"{tmux_ctl.RAILMUX_CANONICAL_HISTORY_PREFIX}"
                f"{transcript_locator.session_id}"
            )
        )
        rows.append(
            (
                fields[2] == "1",
                fields[3] == "1",
                _PaneGeometry(
                    pane_id=pane_id,
                    x=left,
                    y=top,
                    width=width,
                    height=height,
                    history_server=history_server,
                    history_pane_id=history_pane_id,
                    mouse_forwardable=fields[12] == "1",
                    history_size=history_size,
                    alternate_on=fields[11] == "1",
                    transcript_source=transcript_marker,
                    transcript_backed=transcript_backed,
                    transcript_provider=(
                        transcript_locator.provider
                        if transcript_backed and transcript_locator is not None
                        else None
                    ),
                    claude_history_policy=claude_history_policy,
                    history_generation=_history_generation(
                        history_marker,
                        (
                            str(history_server.server_pid)
                            if history_server is not None
                            else fields[17]
                        ),
                    ),
                    canonical_history=canonical_history,
                ),
            )
        )
    if (
        not rows
        or controller is None
        or controller not in seen
        or len({zoomed for zoomed, _active, _pane in rows}) != 1
    ):
        return ()
    active = [pane for _zoomed, is_active, pane in rows if is_active]
    if len(active) != 1:
        return ()
    if rows[0][0]:
        # tmux retains the old unzoomed geometry on hidden panes. Only the
        # active pane actually occupies the client when the window is zoomed.
        result = () if active[0].pane_id == controller else (active[0],)
    else:
        result = tuple(
            pane for _zoomed, _active, pane in rows if pane.pane_id != controller
        )
    if use_cache:
        _PANE_GEOMETRY_CACHE[cache_key] = (now, result)
        _PANE_GEOMETRY_CACHE.move_to_end(cache_key)
        while len(_PANE_GEOMETRY_CACHE) > _PANE_GEOMETRY_CACHE_LIMIT:
            _PANE_GEOMETRY_CACHE.popitem(last=False)
    return result


def _pane_current_path(pane: _PaneGeometry) -> str | None:
    """Read the real provider pane cwd for either display transport."""
    target_pane = pane.history_pane_id or pane.pane_id
    argv = (
        tmux_server.target_argv(
            pane.history_server,
            "display-message",
            "-p",
            "-t",
            target_pane,
            "#{pane_current_path}",
        )
        if pane.history_server is not None
        else tmux_server.tmux_argv(
            "display-message",
            "-p",
            "-t",
            target_pane,
            "#{pane_current_path}",
        )
    )
    try:
        current = subprocess.check_output(
            argv,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).rstrip("\n")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return (
        current if current and "\x00" not in current and "\n" not in current else None
    )


def visible_agent_snapshots(
    session_id: str,
    *,
    use_cache: bool = True,
) -> tuple[HistorySnapshot, ...]:
    """Return cached, geometry-only routes for local semantic interaction.

    This is deliberately the same fail-closed pane authority used by the fast
    SSH display. It does not capture history or transcript data and therefore
    cannot make local Windows rendering fall behind provider output.
    """
    return tuple(
        HistorySnapshot(
            0,
            pane.pane_id,
            pane.x,
            pane.y,
            pane.width,
            pane.height,
            mouse_forwardable=pane.mouse_forwardable,
        )
        for pane in _list_agent_panes(session_id, use_cache=use_cache)
    )


def resolve_path_result(
    session_id: str,
    request_id: int,
    pane_id: str,
    raw_path: str,
    *,
    path_open_policy: str | None = None,
) -> PathResult:
    """Resolve one explicit click against a currently visible agent pane."""
    pane = next(
        (
            candidate
            for candidate in _list_agent_panes(session_id)
            if candidate.pane_id == pane_id
        ),
        None,
    )
    if pane is None:
        return PathResult(request_id, PathKind.UNAVAILABLE)
    current = _pane_current_path(pane)
    if current is None:
        return PathResult(request_id, PathKind.UNAVAILABLE)
    normalized_path = provider_path(raw_path)
    if str(normalized_path) == "~":
        candidate = Path.home()
    elif str(normalized_path).startswith("~/"):
        candidate = Path.home() / str(normalized_path)[2:]
    else:
        candidate = normalized_path
        if not candidate.is_absolute():
            candidate = Path(current) / candidate
    try:
        resolved = candidate.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError):
        return PathResult(request_id, PathKind.UNAVAILABLE)
    if stat.S_ISREG(info.st_mode):
        kind = PathKind.FILE
        access = os.R_OK
    elif stat.S_ISDIR(info.st_mode):
        kind = PathKind.DIRECTORY
        access = os.R_OK | os.X_OK
    else:
        kind = PathKind.OTHER
        access = os.R_OK
    text = str(resolved)
    if (
        not os.access(resolved, access)
        or not text
        or len(text.encode("utf-8")) > 4096
        or "\x00" in text
        or "\n" in text
        or "\r" in text
    ):
        return PathResult(request_id, PathKind.UNAVAILABLE)
    policy = path_open_policy or Settings().path_open_policy
    return PathResult(request_id, kind, text, policy)


def apply_path_open_request(
    session_id: str,
    request_id: int,
    pane_id: str,
    raw_path: str,
    policy: str,
    persistent: bool,
    line: int | None,
    column: int | None,
) -> PathOpenResult:
    """Revalidate and apply one explicit clicked-path destination choice."""
    resolved = resolve_path_result(
        session_id,
        request_id,
        pane_id,
        raw_path,
        path_open_policy=policy,
    )
    if resolved.kind is PathKind.UNAVAILABLE:
        return PathOpenResult(
            request_id,
            False,
            "warning",
            "Path is no longer available in this workspace",
        )
    if persistent and not Settings().set_path_open_policy(policy):
        return PathOpenResult(
            request_id,
            False,
            "error",
            "Could not save the clicked-path preference",
        )
    if policy == "external":
        return PathOpenResult(
            request_id,
            True,
            "success",
            "Opening remote path in a separate terminal",
        )
    manager = manager_for_session(session_id)
    slot = manager.slot_for_owner(pane_id) if manager is not None else None
    if manager is None or slot is None:
        return PathOpenResult(
            request_id,
            False,
            "warning",
            "Could not match the clicked agent to a managed tool pane",
        )
    path = Path(resolved.path)
    if (
        resolved.kind is PathKind.FILE
        and local_open.is_vim_text_path(resolved.path)
        and shutil.which("vim") is not None
    ):
        outcome = manager.open_viewer(
            slot,
            pane_id,
            resolved.path,
            line=line,
            column=column,
        )
    else:
        directory = path if resolved.kind is PathKind.DIRECTORY else path.parent
        outcome = manager.open_shell(slot, pane_id, directory)
        if (
            outcome.ok
            and resolved.kind is PathKind.FILE
            and local_open.is_vim_text_path(resolved.path)
        ):
            return PathOpenResult(
                request_id,
                True,
                "warning",
                (
                    "Vim is unavailable; selected the managed terminal "
                    "(an existing shell keeps its current directory)"
                ),
            )
    return PathOpenResult(
        request_id,
        outcome.ok,
        outcome.level,
        outcome.message,
    )


@dataclass(frozen=True)
class _PathResolveJob:
    session_id: str
    request_id: int
    pane_id: str
    raw_path: str


@dataclass(frozen=True)
class _PathOpenJob:
    session_id: str
    request_id: int
    pane_id: str
    raw_path: str
    policy: str
    persistent: bool
    line: int | None
    column: int | None


class PathActionWorker:
    """Serialize path validation/actions away from a display/PTY loop."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._job: _PathResolveJob | _PathOpenJob | None = None
        self._result: PathResult | PathOpenResult | None = None
        self._in_flight = False
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="railmux-path-action",
            daemon=True,
        )
        self._thread.start()

    def submit_resolve(
        self,
        session_id: str,
        request_id: int,
        pane_id: str,
        raw_path: str,
    ) -> bool:
        with self._condition:
            if self._busy_unlocked():
                return False
            self._job = _PathResolveJob(
                session_id,
                request_id,
                pane_id,
                raw_path,
            )
            self._condition.notify()
            return True

    def submit(
        self,
        session_id: str,
        request_id: int,
        pane_id: str,
        raw_path: str,
        policy: str,
        persistent: bool,
        line: int | None,
        column: int | None,
    ) -> bool:
        with self._condition:
            if self._busy_unlocked():
                return False
            self._job = _PathOpenJob(
                session_id,
                request_id,
                pane_id,
                raw_path,
                policy,
                persistent,
                line,
                column,
            )
            self._condition.notify()
            return True

    def drain(self) -> tuple[PathResult | PathOpenResult, ...]:
        with self._condition:
            if self._result is None:
                return ()
            result = self._result
            self._result = None
            return (result,)

    @property
    def busy(self) -> bool:
        with self._condition:
            return self._busy_unlocked()

    def _busy_unlocked(self) -> bool:
        return (
            self._closed
            or self._job is not None
            or self._in_flight
            or self._result is not None
        )

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._job = None
            self._condition.notify()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._job is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                job = self._job
                self._job = None
                self._in_flight = True
            assert job is not None
            try:
                if isinstance(job, _PathResolveJob):
                    result = resolve_path_result(
                        job.session_id,
                        job.request_id,
                        job.pane_id,
                        job.raw_path,
                    )
                else:
                    result = apply_path_open_request(
                        job.session_id,
                        job.request_id,
                        job.pane_id,
                        job.raw_path,
                        job.policy,
                        job.persistent,
                        job.line,
                        job.column,
                    )
            except Exception:
                # A tool-pane command is a presentation operation. Keep an
                # unexpected subprocess/config failure out of the transport
                # loop and never turn it into provider or session mutation.
                if isinstance(job, _PathResolveJob):
                    result = PathResult(job.request_id, PathKind.UNAVAILABLE)
                else:
                    result = PathOpenResult(
                        job.request_id,
                        False,
                        "error",
                        "Could not complete path opening",
                    )
            with self._condition:
                self._in_flight = False
                if self._closed:
                    return
                self._result = result


def _render_history_lines(
    pyte: object,
    lines: Sequence[bytes],
    width: int,
) -> tuple[bytes, ...]:
    """Render physical tmux rows while retaining cross-row terminal style.

    ``tmux capture-pane -e`` is a terminal stream, not a collection of
    independently styled strings. In particular, tmux omits an SGR prefix
    when a row inherits the previous row's foreground, background, or text
    attributes. Keep one decoder alive for the ordered capture, but clear its
    one-row cell buffer between rows. :func:`render_rows` then makes every
    result independently paintable for the local history overlay.
    """
    screen = pyte.Screen(width, 1)
    stream = pyte.ByteStream(screen)
    rendered: list[bytes] = []
    for line in lines:
        screen.buffer[0].clear()
        screen.cursor.x = 0
        screen.cursor.y = 0
        stream.feed(line)
        rendered.append(render_rows(screen)[0])
    return tuple(rendered)


def _render_history_line(pyte: object, line: bytes, width: int) -> bytes:
    """Render one self-contained physical row with allowlisted SGR styling."""
    return _render_history_lines(pyte, (line,), width)[0]


def _read_transcript_tail(fd: int) -> tuple[str, bool]:
    """Read a bounded, whole-record suffix from an already validated file."""
    size = os.fstat(fd).st_size
    start = max(0, size - _TRANSCRIPT_MAX_BYTES)
    os.lseek(fd, start, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = size - start
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if start:
        newline = raw.find(b"\n")
        raw = b"" if newline < 0 else raw[newline + 1 :]
    return raw.decode("utf-8", errors="replace"), start > 0


def _wrap_transcript_rows(
    pyte: object, text: str, width: int
) -> tuple[tuple[bytes, ...], bool, int]:
    """Wrap allowlisted transcript ANSI into independently paintable rows."""
    rows: deque[bytes] = deque(maxlen=MAX_HISTORY_LINES)
    row = bytearray(b"\033[0m")
    column = 0
    active: list[bytes] = []
    dropped = False
    total_rows = 0

    def finish() -> None:
        nonlocal row, column, dropped, total_rows
        total_rows += 1
        if len(rows) == rows.maxlen:
            dropped = True
        rows.append(bytes(row) + b"\033[0m")
        row = bytearray(b"\033[0m" + b"".join(active))
        column = 0

    for match in re.finditer(r"\x1b\[[0-9;]*m|[^\x1b]+", text):
        token = match.group(0)
        if token.startswith("\x1b["):
            encoded = token.encode("ascii")
            params = token[2:-1].split(";")
            reset_at = max(
                (index for index, param in enumerate(params) if param in ("", "0")),
                default=-1,
            )
            if reset_at >= 0:
                active.clear()
                remaining = params[reset_at + 1 :]
                if remaining:
                    active.append(f"\033[{';'.join(remaining)}m".encode("ascii"))
            else:
                active.append(encoded)
            row.extend(encoded)
            continue
        for char in token:
            if char == "\n":
                finish()
                continue
            if char == "\r":
                continue
            if char == "\t":
                spaces = 8 - column % 8
                for _ in range(spaces):
                    if column >= width:
                        finish()
                    row.extend(b" ")
                    column += 1
                continue
            cell_width = pyte.screens.wcwidth(char)
            if cell_width < 0:
                continue
            if cell_width > 0 and column + cell_width > width:
                finish()
            row.extend(char.encode("utf-8"))
            column += max(0, cell_width)
    if column or len(row) > len(b"\033[0m" + b"".join(active)):
        finish()
    return tuple(rows), dropped, total_rows


def _transcript_rows(
    pyte: object,
    marker: str,
    width: int,
    *,
    allow_stale: bool,
) -> _TranscriptCacheEntry | None:
    """Return a stable cumulative transcript suffix with an inode-aware cache."""
    opened = tmux_server.open_transcript_source(marker)
    if opened is None:
        return None
    source, fd = opened
    try:
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
        key = (str(source.path), width)
        cached = _TRANSCRIPT_CACHE.get(key)
        if cached is not None and (
            cached.identity == identity or (allow_stale and bool(cached.rows))
        ):
            _TRANSCRIPT_CACHE.move_to_end(key)
            return cached
        format_key = (str(source.path), source.provider)
        formatted_entry = _TRANSCRIPT_FORMAT_CACHE.get(format_key)
        if formatted_entry is not None and formatted_entry.identity == identity:
            _TRANSCRIPT_FORMAT_CACHE.move_to_end(format_key)
            formatted = formatted_entry.formatted
            truncated = formatted_entry.truncated
        else:
            try:
                raw, truncated = _read_transcript_tail(fd)
            except OSError:
                return None
            formatted = "".join(
                transcript_renderer.format_transcript(
                    io.StringIO(raw),
                    source.provider,
                    claude_native=source.provider == "claude",
                )
            )
            if len(formatted) <= _TRANSCRIPT_FORMAT_CACHE_MAX_CHARS:
                _TRANSCRIPT_FORMAT_CACHE[format_key] = _TranscriptFormatCacheEntry(
                    identity,
                    formatted,
                    truncated,
                )
                _TRANSCRIPT_FORMAT_CACHE.move_to_end(format_key)
                while (
                    len(_TRANSCRIPT_FORMAT_CACHE) > _TRANSCRIPT_FORMAT_CACHE_LIMIT
                ):
                    _TRANSCRIPT_FORMAT_CACHE.popitem(last=False)
            else:
                _TRANSCRIPT_FORMAT_CACHE.pop(format_key, None)
        rows, dropped, total_rows = _wrap_transcript_rows(pyte, formatted, width)
        entry = _TranscriptCacheEntry(
            identity,
            rows,
            truncated or dropped,
            total_rows,
            not truncated,
        )
        _TRANSCRIPT_CACHE[key] = entry
        _TRANSCRIPT_CACHE.move_to_end(key)
        while len(_TRANSCRIPT_CACHE) > _TRANSCRIPT_CACHE_LIMIT:
            _TRANSCRIPT_CACHE.popitem(last=False)
        return entry
    finally:
        os.close(fd)


def _capture_pane_history(
    pyte: object,
    pane: _PaneGeometry,
    request_id: int,
    max_lines: int,
    *,
    allow_stale_transcript: bool = False,
) -> HistorySnapshot | None:
    nonce = secrets.token_hex(8)
    timeline_marker = f"RAILMUX-HISTORY-{nonce}"
    try:
        if pane.history_server is not None and pane.history_pane_id is not None:
            if not tmux_server.target_is_live(pane.history_server, timeout=0.25):
                return None
            argv = tmux_server.target_argv(
                pane.history_server,
                "capture-pane",
                "-p",
                "-e",
                "-N",
                "-t",
                pane.history_pane_id,
                "-S",
                f"-{max_lines}",
                ";",
                "display-message",
                "-p",
                "-t",
                pane.history_pane_id,
                f"{timeline_marker} #{{history_size}} #{{pane_height}}",
            )
        else:
            argv = tmux_server.tmux_argv(
                "capture-pane",
                "-p",
                "-e",
                "-N",
                "-t",
                pane.pane_id,
                "-S",
                f"-{max_lines}",
                ";",
                "display-message",
                "-p",
                "-t",
                pane.pane_id,
                f"{timeline_marker} #{{history_size}} #{{pane_height}}",
            )
        output = subprocess.check_output(
            argv,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    raw_lines = output.split(b"\n")
    if raw_lines and raw_lines[-1] == b"":
        raw_lines.pop()
    captured_history_size = pane.history_size
    captured_height = pane.height
    encoded_marker = timeline_marker.encode("ascii") + b" "
    if raw_lines and raw_lines[-1].startswith(encoded_marker):
        fields = raw_lines.pop().split(b" ")
        if len(fields) == 3:
            try:
                marker_history_size = int(fields[1])
                marker_height = int(fields[2])
            except ValueError:
                pass
            else:
                if marker_history_size >= 0 and marker_height > 0:
                    captured_history_size = marker_history_size
                    captured_height = marker_height
    # Retain the newest suffix when the styled representation reaches its byte
    # budget. Iterate backwards so an oversized response never sacrifices the
    # pane's current viewport merely to retain older scrollback.
    rendered_raw = _render_history_lines(pyte, raw_lines, pane.width)
    current_raw = rendered_raw[-pane.height :]
    transcript_entry = None
    # ``None`` is retained only for old/test geometry that predates the
    # provider field; every validated live marker carries an explicit value.
    transcript_provider = pane.transcript_provider or "claude"
    if (
        pane.transcript_backed
        and pane.transcript_source
        and (
            (transcript_provider == "codex" and pane.canonical_history)
            or (
                transcript_provider == "claude"
                and pane.claude_history_policy == "local"
                and pane.alternate_on
                and pane.history_size == 0
            )
        )
    ):
        transcript_entry = _transcript_rows(
            pyte,
            pane.transcript_source,
            pane.width,
            allow_stale=allow_stale_transcript,
        )
    source_lines: Sequence[tuple[bytes, bool]]
    transcript_used = transcript_entry is not None and bool(transcript_entry.rows)
    if transcript_used:
        assert transcript_entry is not None
        transcript_lines = transcript_entry.rows
        history_count = max(0, max_lines - pane.height)
        source_lines = (
            transcript_lines[-history_count:] if history_count else ()
        ) + current_raw
        timeline_end = max(
            pane.height,
            (transcript_entry.total_rows or len(transcript_entry.rows)) + pane.height,
        )
    else:
        source_lines = rendered_raw
        timeline_end = max(
            pane.height,
            captured_history_size + captured_height,
        )
    newest_first: list[bytes] = []
    packed_size = 2  # history line-count prefix
    budget_truncated = False
    for rendered in reversed(source_lines[-max_lines:]):
        line_size = 4 + len(rendered)
        if packed_size + line_size > _HISTORY_SNAPSHOT_RAW_BUDGET:
            budget_truncated = True
            break
        newest_first.append(rendered)
        packed_size += line_size
    lines = tuple(reversed(newest_first))
    if len(lines) < pane.height:
        blank = _render_history_line(pyte, b"", pane.width)
        lines += (blank,) * (pane.height - len(lines))
    if (
        transcript_used
        and transcript_entry is not None
        and not transcript_entry.timeline_stable
    ):
        timeline_start = timeline_end = 0
    else:
        timeline_end = max(timeline_end, len(lines))
        timeline_start = timeline_end - len(lines)
    return HistorySnapshot(
        request_id=request_id,
        pane_id=pane.pane_id,
        x=pane.x,
        y=pane.y,
        width=pane.width,
        height=pane.height,
        lines=lines,
        mouse_forwardable=pane.mouse_forwardable,
        transcript_backed=transcript_used,
        transcript_available=pane.transcript_backed,
        history_choice_required=(
            pane.transcript_backed
            and transcript_provider == "claude"
            and pane.claude_history_policy == "ask"
        ),
        more_available=(
            budget_truncated
            or (
                transcript_entry.more_available
                or len(transcript_entry.rows) + pane.height > len(lines)
                if transcript_used and transcript_entry is not None
                else pane.history_size + pane.height > len(lines)
            )
        ),
        generation=pane.history_generation,
        timeline_start=timeline_start,
        timeline_end=timeline_end,
    )


def capture_history_snapshot(
    session_id: str,
    request_id: int,
    x: int,
    y: int,
    max_lines: int,
    pyte: object | None = None,
    *,
    claude_history_policy: str | None = None,
    use_topology_cache: bool = False,
) -> HistorySnapshot:
    """Capture bounded styled history without entering tmux copy-mode."""
    pointer_options: dict[str, object] = {}
    if claude_history_policy is not None:
        pointer_options["claude_history_policy"] = claude_history_policy
    if use_topology_cache:
        pointer_options["use_cache"] = True
    pane = _pane_at_pointer(session_id, x, y, **pointer_options)
    if pane is None:
        return HistorySnapshot(request_id, None)
    try:
        if pyte is None:
            import pyte as pyte_module

            pyte = pyte_module
        pyte = _extended_pyte(pyte)
        snapshot = _capture_pane_history(pyte, pane, request_id, max_lines)
    except (ImportError, ValueError, IndexError):
        return HistorySnapshot(request_id, None)
    return snapshot or HistorySnapshot(request_id, None)


def capture_history_batch(
    pyte: object,
    session_id: str,
    request_id: int,
    max_lines: int,
    *,
    claude_history_policy: str | None = None,
    use_topology_cache: bool = False,
) -> HistoryBatch:
    """Atomically describe and warm-cache every visible agent pane."""
    pyte = _extended_pyte(pyte)
    snapshots = tuple(
        snapshot
        for pane in _list_agent_panes(
            session_id,
            claude_history_policy=claude_history_policy,
            use_cache=use_topology_cache,
        )
        if (
            snapshot := _capture_pane_history(
                pyte,
                pane,
                request_id,
                max_lines,
                allow_stale_transcript=True,
            )
        )
        is not None
    )
    return HistoryBatch(request_id, snapshots)


def _acquire_display_lock(
    session_id: str,
    *,
    timeout: float = _ATTACH_LOCK_TIMEOUT,
) -> int:
    """Boundedly serialize the validation-and-attach boundary."""
    key = session_id[1:] if session_id.startswith("$") else "invalid"
    if not key.isdigit():
        raise DisplayServerError("invalid session identity for display lock")
    try:
        socket_path = _tmux_output(
            "display-message", "-p", "-t", session_id, "#{socket_path}"
        )
        if not socket_path.startswith("/"):
            raise OSError("invalid tmux socket path")
        socket_key = hashlib.sha256(socket_path.encode()).hexdigest()[:16]
        path = (
            restart_state.runtime_state_dir() / f"fast-display-{socket_key}-{key}.lock"
        )
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or not restart_state.private_mode_is_safe(info.st_mode)
        ):
            raise OSError("unsafe display lock")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
    except BlockingIOError as exc:
        try:
            os.close(fd)
        except (NameError, OSError):
            pass
        raise DisplayServerBusy(
            "another full-window client is already starting or attached"
        ) from exc
    except OSError as exc:
        try:
            os.close(fd)
        except (NameError, OSError):
            pass
        raise DisplayServerError("could not create a safe display lock") from exc
    return fd


def _release_display_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _set_winsize(fd: int, width: int, height: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))


def _resize_tmux_client(
    pid: int,
    master_fd: int,
    width: int,
    height: int,
) -> None:
    """Resize the private PTY and notify its tmux client process group.

    ``TIOCSWINSZ`` on a PTY master updates the slave geometry but, unlike an
    ioctl on the slave, does not reliably signal the foreground process group.
    Without SIGWINCH tmux keeps its old client dimensions while the headless
    screen uses the new size, so bottom-row mouse clicks are misclassified as
    pane clicks.
    """
    _set_winsize(master_fd, width, height)
    try:
        os.killpg(pid, signal.SIGWINCH)
    except ProcessLookupError:
        # The serve loop will observe the exited attach client immediately.
        pass


def _is_compact_geometry(width: int, height: int) -> bool:
    return width < COMPACT_ENTER_WIDTH or height < COMPACT_ENTER_HEIGHT


def _compact_tmux_output(*args: str) -> str | None:
    """Run one handshake-only tmux read under the sub-frame deadline."""
    try:
        return subprocess.check_output(
            tmux_server.tmux_argv(*args),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_COMPACT_TMUX_TIMEOUT,
        ).strip()
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return None


def _set_compact_resize_option(
    session_id: str,
    value: str | None,
) -> bool:
    args = [
        "set-window-option",
        "-t",
        session_id,
    ]
    if value is None:
        args.extend(["-u", COMPACT_RESIZE_OPTION])
    else:
        args.extend([COMPACT_RESIZE_OPTION, value])
    try:
        return (
            subprocess.run(
                tmux_server.tmux_argv(*args),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_COMPACT_TMUX_TIMEOUT,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _clear_compact_resize_option_if(
    session_id: str,
    expected: str,
) -> None:
    current = _compact_tmux_output(
        "show-window-options",
        "-v",
        "-t",
        session_id,
        COMPACT_RESIZE_OPTION,
    )
    if current == expected:
        _set_compact_resize_option(session_id, None)


def _request_compact_resize_preparation(
    session_id: str,
    width: int,
    height: int,
    *,
    timeout: float = _COMPACT_PREPARE_TIMEOUT,
    progress: Callable[[], None] | None = None,
) -> str | None:
    """Give Railmux a bounded chance to park hidden agents before shrinking.

    tmux resizes hidden panes even while another pane is zoomed.  The private
    F20 target asks the controller to move swap-owned hidden agents back to
    their detached home windows before ``TIOCSWINSZ`` can narrow their PTYs.
    Older/busy controllers simply time out and retain the established resize
    behavior; the helper never mutates provider panes itself.
    """
    if not _is_compact_geometry(width, height):
        return None
    try:
        raw = _compact_tmux_output(
            "display-message",
            "-p",
            "-t",
            session_id,
            "#{window_width} #{window_height} #{window_panes}"
            " #{@railmux_controller_pane}",
        )
        current_width, current_height, pane_count, controller = raw.split(" ", 3)
        current = int(current_width), int(current_height)
        panes = int(pane_count)
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        _is_compact_geometry(*current)
        or panes <= 1
        or re.fullmatch(r"%[0-9]+", controller) is None
    ):
        return None

    token = secrets.token_hex(8)
    request = f"request:{token}:{width}:{height}"
    ready = f"ready:{token}:{width}:{height}"
    failed = f"failed:{token}:{width}:{height}"
    if not _set_compact_resize_option(session_id, request):
        return None
    try:
        sent = subprocess.run(
            tmux_server.tmux_argv(
                "send-keys",
                "-l",
                "-t",
                controller,
                COMPACT_RESIZE_SEQUENCE,
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_COMPACT_TMUX_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        sent = None
    if sent is None or sent.returncode != 0:
        _clear_compact_resize_option_if(session_id, request)
        return None

    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if progress is not None:
            progress()
        current_value = _compact_tmux_output(
            "show-window-options",
            "-v",
            "-t",
            session_id,
            COMPACT_RESIZE_OPTION,
        )
        if current_value is None:
            break
        if current_value == ready:
            return ready
        if current_value == failed or current_value != request:
            break
        time.sleep(0.02)
    _clear_compact_resize_option_if(session_id, request)
    return None


@lru_cache(maxsize=1)
def _tmux_client_feature_args() -> tuple[str, ...]:
    """Request per-client RGB output when the installed tmux supports it."""
    try:
        version_text = subprocess.check_output(
            tmux_server.tmux_argv("-V"),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ()
    match = re.search(r"(\d+)\.(\d+)", version_text)
    if match is None:
        return ()
    version = int(match.group(1)), int(match.group(2))
    # tmux 3.2 introduced both terminal-features and the client-scoped -T
    # override. Older supported releases retain their existing 256-colour
    # behavior rather than receiving an option they cannot parse.
    return ("-T", "RGB") if version >= (3, 2) else ()


def _spawn_tmux_client(session_id: str, width: int, height: int) -> tuple[int, int]:
    """Start an exact tmux attach client and return ``(pid, master_fd)``."""
    master_fd, slave_fd = os.openpty()
    _set_winsize(slave_fd, width, height)
    feature_args = _tmux_client_feature_args()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - exercised only by a real PTY smoke test
        try:
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            for target_fd in (0, 1, 2):
                os.dup2(slave_fd, target_fd)
            if slave_fd > 2:
                os.close(slave_fd)
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env.setdefault("COLORTERM", "truecolor")
            argv = tmux_server.tmux_argv(
                *feature_args, "attach-session", "-t", session_id, env=env
            )
            os.execvpe("tmux", argv, env)
        except BaseException:
            os._exit(127)
    os.close(slave_fd)
    os.set_blocking(master_fd, False)
    return pid, master_fd


def _wait_until_attached(session_id: str, pid: int, timeout: float = 2.0) -> bool:
    """Do not expose a frame until tmux has registered the private client."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _child_exited(pid):
            return False
        try:
            clients = subprocess.check_output(
                tmux_server.tmux_argv(
                    "list-clients", "-F", "#{session_id} #{client_pid}"
                ),
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.5,
            ).splitlines()
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ):
            clients = []
        if f"{session_id} {pid}" in clients:
            return True
        time.sleep(0.01)
    return False


def _detach_session_clients(session_id: str) -> None:
    """Detach only clients re-enumerated on one immutable managed session."""
    try:
        rows = subprocess.check_output(
            tmux_server.tmux_argv("list-clients", "-F", "#{session_id} #{client_name}"),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        ).splitlines()
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise DisplayServerError(
            "could not enumerate existing Railmux clients"
        ) from exc
    names: list[str] = []
    for row in rows:
        fields = row.split(" ", 1)
        if len(fields) != 2 or fields[0] != session_id:
            continue
        name = fields[1]
        if not name or len(name) > 512 or "\n" in name or "\0" in name:
            raise DisplayServerError("tmux returned an invalid client identity")
        names.append(name)
    for name in names:
        try:
            subprocess.run(
                tmux_server.tmux_argv("detach-client", "-t", name),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=True,
            )
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            raise DisplayServerError(
                "could not detach an existing Railmux client"
            ) from exc


def _use_smallest_window_size(session_id: str) -> None:
    """Give every shared-window client a viewport it can display completely."""
    last_error: BaseException | None = None
    for attempt in range(_WINDOW_SIZE_ATTEMPTS):
        try:
            result = subprocess.run(
                tmux_server.tmux_argv(
                    "set-window-option",
                    "-t",
                    session_id,
                    "window-size",
                    "smallest",
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_error = exc
        else:
            if result.returncode == 0:
                return
        if attempt + 1 < _WINDOW_SIZE_ATTEMPTS:
            time.sleep(0.05)
    # tmux < 2.9 has no window-size option and already uses the smallest
    # attached client. Only modern tmux failures mean the safety policy could
    # not be established.
    try:
        version_text = subprocess.check_output(
            tmux_server.tmux_argv("-V"),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
        match = re.search(r"(\d+)\.(\d+)", version_text)
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise DisplayServerError(
            "could not establish safe multi-terminal window sizing"
        ) from (last_error or exc)
    if match is None or (int(match.group(1)), int(match.group(2))) >= (2, 9):
        raise DisplayServerError(
            "could not establish safe multi-terminal window sizing"
        )


def _child_exited(pid: int) -> bool:
    try:
        found, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return found == pid


def _stop_client(pid: int, master_fd: int) -> None:
    """Stop only the private attach client; never address the tmux session."""
    try:
        os.close(master_fd)
    except OSError:
        pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if _child_exited(pid):
            return
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if _child_exited(pid):
            return
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _remote_watchdog_tripped(
    watchdog: tmux_health.FailureWatchdog,
    session_id: str,
    expected_server_pid: int,
    now: float,
) -> bool:
    """Run one due low-frequency probe and persist only a terminal failure."""
    if not watchdog.due(now):
        return False
    try:
        raw_identity = _tmux_output(
            "display-message",
            "-p",
            "-t",
            session_id,
            "#{pid} #{session_id}",
        )
    except DisplayServerError:
        raw_identity = ""
    healthy = raw_identity == f"{expected_server_pid} {session_id}"
    if not watchdog.observe(healthy, now):
        return False
    tmux_health.record_incident(
        component="remote-display",
        reason="remote-display-watchdog-timeout",
        consecutive_failures=watchdog.consecutive_failures,
    )
    return True


def _emit_attach_status(status: bytes) -> None:
    sys.stdout.buffer.write(status)
    sys.stdout.buffer.flush()


def serve(
    session: str,
    width: int,
    height: int,
    fps: float,
    *,
    replace_existing_client: bool = False,
    existing_session_only: bool = False,
) -> int:
    try:
        import pyte
        from pyte import modes
    except ImportError as exc:
        raise DisplayServerError(
            "pyte is required remotely; install railmux[ssh]"
        ) from exc
    pyte = _extended_pyte(pyte)

    try:
        existing_target = tmux_server.discover_target(timeout=2.0)
    except tmux_server.TmuxServerError as exc:
        raise DisplayServerError(
            "the selected tmux cannot inspect the existing Railmux server; "
            "run 'railmux config' on the remote host"
        ) from exc
    if existing_target is not None and not tmux_server.sync_server_environment(
        existing_target
    ):
        raise DisplayServerError(
            "could not apply the configured environment to the existing "
            "Railmux tmux server; run 'railmux config' on the remote host"
        )

    if replace_existing_client or existing_session_only:
        initial_session_id = _validate_railmux(session)
        if replace_existing_client:
            _detach_session_clients(initial_session_id)
            lock_timeout = _REPLACE_LOCK_TIMEOUT
        else:
            lock_timeout = _ATTACH_LOCK_TIMEOUT
    else:
        initial_session_id = _ensure_railmux_session(session)
        lock_timeout = _ATTACH_LOCK_TIMEOUT
    lock_fd = _acquire_display_lock(initial_session_id, timeout=lock_timeout)
    pid: int | None = None
    master_fd: int | None = None
    try:
        session_id = _validate_railmux(session)
        if session_id != initial_session_id:
            raise DisplayServerError("Railmux session changed while attaching")
        if replace_existing_client:
            # Close the race between the first detach and lock acquisition:
            # no newer helper can cross this boundary until this attach ends.
            _detach_session_clients(session_id)
        _use_smallest_window_size(session_id)
        compact_ready = _request_compact_resize_preparation(session_id, width, height)
        pid, master_fd = _spawn_tmux_client(session_id, width, height)
        if not _wait_until_attached(session_id, pid):
            _stop_client(pid, master_fd)
            pid = None
            master_fd = None
            raise DisplayServerError("the private tmux client failed to attach")
        if compact_ready is not None:
            _clear_compact_resize_option_if(session_id, compact_ready)
        _emit_attach_status(REMOTE_ATTACH_ACCEPTED)
    except BaseException:
        if pid is not None and master_fd is not None:
            _stop_client(pid, master_fd)
        raise
    finally:
        _release_display_lock(lock_fd)
    assert pid is not None and master_fd is not None
    return _serve_attached(pyte, modes, session_id, width, height, fps, pid, master_fd)


def _serve_attached(
    pyte: object,
    modes: object,
    session_id: str,
    width: int,
    height: int,
    fps: float,
    pid: int,
    master_fd: int,
) -> int:
    screen = pyte.DiffScreen(width, height)
    stream = pyte.ByteStream(screen)
    input_decoder = InputFrameDecoder()
    stdin_fd = sys.stdin.buffer.fileno()
    stdout_fd = sys.stdout.buffer.fileno()
    os.set_blocking(stdin_fd, False)
    os.set_blocking(stdout_fd, False)
    interval = 1.0 / fps
    next_frame = time.monotonic()
    screen_changed = True
    force_keyframe = True
    pty_open = True
    delivered: _ScreenState | None = None
    pending_packet: bytes | None = None
    pending_offset = 0
    pending_state: _ScreenState | None = None
    control_packets: deque[bytes] = deque()
    history_ready: deque[bytes] = deque()
    clipboard_decoder = _Osc52ClipboardDecoder()
    claude_history_override: str | None = None
    claude_history_persisted_at_override: str | None = None
    input_closed = False
    last_input = time.monotonic()
    watchdog = tmux_health.FailureWatchdog.starting(
        time.monotonic(),
        interval=_WATCHDOG_INTERVAL,
        failure_limit=_WATCHDOG_FAILURES,
    )
    try:
        target = tmux_server.discover_target(timeout=2.0)
    except tmux_server.TmuxServerError as exc:
        _stop_client(pid, master_fd)
        raise DisplayServerError(
            "dedicated tmux server stopped responding after attach"
        ) from exc
    if target is None:
        _stop_client(pid, master_fd)
        raise DisplayServerError("dedicated tmux server disappeared after attach")
    history_worker = HistoryCaptureWorker(
        pyte,
        capture_snapshot=capture_history_snapshot,
        capture_batch=capture_history_batch,
    )
    path_worker: PathActionWorker | None = None
    history_settings = Settings()
    persisted_history_policy = history_settings.claude_history_policy

    def settings_signature() -> tuple[int, int, int, int] | None:
        try:
            info = history_settings._path.stat()
        except OSError:
            return None
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)

    persisted_history_signature = settings_signature()

    def current_history_policy() -> str:
        nonlocal history_settings, persisted_history_policy
        nonlocal persisted_history_signature
        signature = settings_signature()
        if signature != persisted_history_signature:
            history_settings = Settings()
            persisted_history_policy = history_settings.claude_history_policy
            persisted_history_signature = settings_signature()
        return persisted_history_policy

    def discard_unsent_update() -> None:
        nonlocal pending_packet, pending_offset, pending_state
        if (
            pending_packet is not None
            and pending_state is not None
            and pending_offset == 0
        ):
            pending_packet = None
            pending_state = None

    def drain_pty_during_resize_prepare() -> None:
        """Keep provider output flowing during the bounded UI handshake."""
        nonlocal pty_open, screen_changed
        while pty_open:
            try:
                data = os.read(master_fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                pty_open = False
                return
            if not data:
                pty_open = False
                return
            stream.feed(data)
            screen_changed = True

    def queue_control_packet(packet: bytes, *, priority: bool = False) -> None:
        if len(control_packets) >= 4:
            if not priority:
                return
            control_packets.pop()
        discard_unsent_update()
        if priority:
            control_packets.appendleft(packet)
        else:
            control_packets.append(packet)

    def activate_control_packet() -> None:
        nonlocal pending_packet, pending_offset, pending_state
        if pending_packet is None and control_packets:
            pending_packet = control_packets.popleft()
            pending_offset = 0
            pending_state = None

    def apply_resize(new_width: int, new_height: int) -> None:
        nonlocal width, height, force_keyframe, screen_changed
        if not 40 <= new_width <= 1000 or not 12 <= new_height <= 500:
            return
        if (new_width, new_height) == (width, height):
            return
        discard_unsent_update()
        compact_ready = _request_compact_resize_preparation(
            session_id,
            new_width,
            new_height,
            progress=drain_pty_during_resize_prepare,
        )
        _resize_tmux_client(pid, master_fd, new_width, new_height)
        if compact_ready is not None:
            _clear_compact_resize_option_if(session_id, compact_ready)
        screen.resize(lines=new_height, columns=new_width)
        width, height = new_width, new_height
        force_keyframe = True
        screen_changed = True

    def schedule_latest_update(now: float) -> None:
        nonlocal pending_packet, pending_state, screen_changed
        nonlocal force_keyframe, next_frame
        if pending_packet is not None or not (screen_changed or force_keyframe):
            return
        update, state = build_screen_update(
            screen,
            modes,
            width=width,
            height=height,
            delivered=delivered,
            force_keyframe=force_keyframe,
        )
        if update is None:
            screen_changed = False
            force_keyframe = False
            next_frame = now + interval
            return
        pending_packet = encode_update(update)
        assert state is not None
        pending_state = state
        screen_changed = False
        force_keyframe = False
        next_frame = now + interval

    try:
        while pty_open and not _child_exited(pid):
            history_ready.extend(
                encode_history_snapshot(result)
                if isinstance(result, HistorySnapshot)
                else encode_history_batch(result)
                for result in history_worker.drain()
            )
            while history_ready and len(control_packets) < 4:
                queue_control_packet(history_ready.popleft())
            if path_worker is not None:
                for result in path_worker.drain():
                    packet = (
                        encode_path_open_result(result)
                        if isinstance(result, PathOpenResult)
                        else encode_path_result(result)
                    )
                    queue_control_packet(packet, priority=True)
            activate_control_packet()
            now = time.monotonic()
            timeout = (
                0.25
                if pending_packet is not None
                else max(0.0, min(0.25, next_frame - now))
            )
            timeout = min(
                timeout,
                max(0.0, last_input + _CLIENT_LEASE_TIMEOUT - now),
            )
            writable_fds = [stdout_fd] if pending_packet is not None else []
            readable, writable, _ = select.select(
                [master_fd, stdin_fd, history_worker.read_fd],
                writable_fds,
                [],
                timeout,
            )
            if history_worker.read_fd in readable:
                history_ready.extend(
                    encode_history_snapshot(result)
                    if isinstance(result, HistorySnapshot)
                    else encode_history_batch(result)
                    for result in history_worker.drain()
                )
                while history_ready and len(control_packets) < 4:
                    queue_control_packet(history_ready.popleft())
            if stdin_fd in readable:
                try:
                    packet = os.read(stdin_fd, 65536)
                except BlockingIOError:
                    packet = None
                if packet == b"":
                    input_closed = True
                    break
                for message in input_decoder.feed(packet or b""):
                    last_input = time.monotonic()
                    if message.kind is InputKind.HEARTBEAT:
                        continue
                    if message.kind is InputKind.RESIZE:
                        apply_resize(*struct.unpack(">HH", message.data))
                        continue
                    if message.kind is InputKind.REQUEST_KEYFRAME:
                        discard_unsent_update()
                        force_keyframe = True
                        screen_changed = True
                        continue
                    if message.kind is InputKind.REQUEST_HISTORY:
                        if len(control_packets) + len(history_ready) < 4:
                            if claude_history_override is not None:
                                (
                                    claude_history_override,
                                    claude_history_persisted_at_override,
                                ) = refresh_claude_history_override(
                                    claude_history_override,
                                    claude_history_persisted_at_override,
                                    current_history_policy(),
                                )
                            try:
                                request = decode_history_request(message.data)
                            except ValueError:
                                continue
                            submitted = history_worker.submit(
                                HistoryCaptureJob(
                                    "snapshot",
                                    session_id,
                                    request,
                                    claude_history_override,
                                )
                            )
                            if not submitted:
                                queue_control_packet(
                                    encode_history_snapshot(
                                        HistorySnapshot(request[0], None)
                                    )
                                )
                        continue
                    if message.kind is InputKind.PREFETCH_HISTORY:
                        if len(control_packets) + len(history_ready) < 4:
                            if claude_history_override is not None:
                                (
                                    claude_history_override,
                                    claude_history_persisted_at_override,
                                ) = refresh_claude_history_override(
                                    claude_history_override,
                                    claude_history_persisted_at_override,
                                    current_history_policy(),
                                )
                            try:
                                request_id, max_lines = decode_history_prefetch(
                                    message.data
                                )
                            except ValueError:
                                continue
                            submitted = history_worker.submit(
                                HistoryCaptureJob(
                                    "batch",
                                    session_id,
                                    (request_id, max_lines),
                                    claude_history_override,
                                )
                            )
                            if not submitted:
                                queue_control_packet(
                                    encode_history_batch(HistoryBatch(request_id, ()))
                                )
                        continue
                    if message.kind is InputKind.SET_CLAUDE_HISTORY:
                        try:
                            policy, persistent = decode_claude_history_choice(
                                message.data
                            )
                        except ValueError:
                            continue
                        applied, claude_history_override = apply_claude_history_choice(
                            policy,
                            persistent=persistent,
                            current_override=claude_history_override,
                            settings=history_settings,
                        )
                        if applied and persistent:
                            persisted_history_policy = policy
                            persisted_history_signature = settings_signature()
                        claude_history_persisted_at_override = (
                            current_history_policy()
                            if applied and not persistent
                            else None
                        )
                        queue_control_packet(
                            encode_claude_history_policy_result(
                                policy,
                                persistent=persistent,
                                applied=applied,
                            ),
                            priority=True,
                        )
                        continue
                    if message.kind is InputKind.RESOLVE_PATH:
                        try:
                            request = decode_path_request(message.data)
                        except ValueError:
                            continue
                        if path_worker is None:
                            path_worker = PathActionWorker()
                        if not path_worker.submit_resolve(
                            session_id, *request
                        ):
                            queue_control_packet(
                                encode_path_result(
                                    PathResult(request[0], PathKind.UNAVAILABLE)
                                ),
                                priority=True,
                            )
                        continue
                    if message.kind is InputKind.OPEN_PATH:
                        try:
                            request = decode_path_open_request(message.data)
                        except ValueError:
                            continue
                        if path_worker is None:
                            path_worker = PathActionWorker()
                        if not path_worker.submit(session_id, *request):
                            queue_control_packet(
                                encode_path_open_result(
                                    PathOpenResult(
                                        request[0],
                                        False,
                                        "warning",
                                        "Another path open is still completing",
                                    )
                                ),
                                priority=True,
                            )
                        continue
                    view = memoryview(message.data)
                    while view:
                        try:
                            written = os.write(master_fd, view)
                        except BlockingIOError:
                            select.select([], [master_fd], [], 0.05)
                            continue
                        except OSError as exc:
                            if exc.errno == errno.EIO:
                                pty_open = False
                                break
                            raise
                        view = view[written:]
            if master_fd in readable:
                try:
                    output = os.read(master_fd, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        pty_open = False
                        output = b""
                    else:
                        raise
                if not output:
                    pty_open = False
                else:
                    for clipboard_data in clipboard_decoder.feed(output):
                        queue_control_packet(
                            encode_clipboard_copy(clipboard_data),
                            priority=True,
                        )
                    stream.feed(output)
                    screen_changed = True

            if stdout_fd in writable and pending_packet is not None:
                try:
                    written = os.write(stdout_fd, pending_packet[pending_offset:])
                except BlockingIOError:
                    written = 0
                except BrokenPipeError:
                    return 0
                pending_offset += written
                if pending_offset == len(pending_packet):
                    if pending_state is not None:
                        # This is the only place the diff base advances.
                        # Replacing a wholly unsent display packet therefore
                        # recomputes against the last successfully sent state.
                        delivered = pending_state
                    pending_packet = None
                    pending_offset = 0
                    pending_state = None

            now = time.monotonic()
            if now - last_input >= _CLIENT_LEASE_TIMEOUT:
                return 0
            if _remote_watchdog_tripped(watchdog, session_id, target.server_pid, now):
                raise DisplayServerError(
                    "dedicated tmux server stopped responding; run "
                    "'railmux doctor' for diagnostics"
                )
            if (
                pending_packet is not None
                and pending_state is not None
                and pending_offset == 0
                and screen_changed
                and now >= next_frame
            ):
                pending_packet = None
                pending_state = None
                schedule_latest_update(now)
            elif pending_packet is None and not control_packets and now >= next_frame:
                schedule_latest_update(now)
        if input_closed:
            return 0
        return int(_classify_observed_exit(session_id, target))
    finally:
        if path_worker is not None:
            path_worker.close()
        history_worker.close()
        _stop_client(pid, master_fd)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="railmux remote-server",
        description="Internal coalesced full-window Railmux display server",
    )
    parser.add_argument(
        "--protocol",
        type=int,
        required=True,
        help="private display protocol version expected by the local client",
    )
    parser.add_argument(
        "--session",
        default="railmux",
        help="managed remote tmux session name (default: railmux)",
    )
    parser.add_argument(
        "--width",
        type=int,
        required=True,
        help="local terminal width in columns (40-1000)",
    )
    parser.add_argument(
        "--height",
        type=int,
        required=True,
        help="local terminal height in rows (12-500)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=20.0,
        help="maximum coalesced display update rate, 1-60 (default: 20)",
    )
    parser.add_argument(
        "--replace-existing-client",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--existing-session-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if not 40 <= args.width <= 1000:
        parser.error("--width must be between 40 and 1000")
    if not 12 <= args.height <= 500:
        parser.error("--height must be between 12 and 500")
    if not 1.0 <= args.fps <= 60.0:
        parser.error("--fps must be between 1 and 60")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if any(value in {"-h", "--help"} for value in effective_argv):
        # Help is ordinary CLI output, not a transport handshake. In
        # particular, do not print REMOTE_HELLO_PREFIX before argparse.
        parse_args(effective_argv)
        return 0
    config: Config | None = None
    config_error: ConfigError | None = None
    try:
        config = load_config()
    except ConfigError as exc:
        config_error = exc
    tmux_available: bool | None = None
    if config is not None:
        locale_valid, _locale_detail = check_utf8_locale(config.locale)
        if not locale_valid:
            config_error = ConfigError("configured locale is unavailable or not UTF-8")
            config = None
    if config is not None:
        if config.tmux_binary != "tmux":
            tmux_available = check_executable("tmux", config.tmux_binary).valid
        if tmux_available is not False:
            activate_runtime_environment(config)
        if tmux_available is None:
            tmux_available = shutil.which("tmux") is not None
    ready = _fast_dependency_ready()
    tmux_configured = bool(config is not None and config.tmux_binary != "tmux")
    if config_error is None and not tmux_configured:
        _emit_remote_hello(ready)
    else:
        _emit_remote_hello(
            ready,
            config_status="invalid" if config_error is not None else "valid",
            tmux_configured=tmux_configured,
            tmux_available=tmux_available,
        )
    args = parse_args(argv)
    # Compatibility probes intentionally stop after the hello. Do not emit
    # remote argparse/dependency diagnostics while the local client is asking
    # whether to upgrade; the local side owns that user-facing decision.
    if not _await_client_start():
        return 2
    if args.protocol != PROTOCOL_VERSION:
        print(
            "fast display server: incompatible client protocol",
            file=sys.stderr,
        )
        return 2
    if config_error is not None:
        print(
            "remote display: Railmux configuration is invalid; run "
            "'railmux config' on the remote host to repair or reset it",
            file=sys.stderr,
        )
        return 2
    if tmux_available is False:
        print(
            "remote display: the configured tmux executable is unavailable; "
            "run 'railmux config' on the remote host to correct or reset it",
            file=sys.stderr,
        )
        return 2
    if not ready:
        print(
            "remote display: pyte is unavailable; install 'railmux[ssh]'",
            file=sys.stderr,
        )
        return 2
    try:
        tmux_server.socket_label()
        return serve(
            args.session,
            args.width,
            args.height,
            args.fps,
            replace_existing_client=args.replace_existing_client,
            existing_session_only=args.existing_session_only,
        )
    except DisplayServerBusy as exc:
        _emit_attach_status(REMOTE_ATTACH_BUSY)
        print(f"fast display server: {exc}", file=sys.stderr)
        return 2
    except (DisplayServerError, tmux_server.TmuxServerError) as exc:
        print(f"fast display server: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
