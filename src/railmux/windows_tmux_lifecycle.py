"""Fail-closed cleanup for abandoned MSYS2 tmux socket files.

MSYS2 tmux can leave its AF_UNIX socket behind after the last session exits.
The next tmux client then waits on an endpoint with no listener. Cleanup is
authorized only while the live server proves that Railmux's outer UI is its
sole session; provider histories are never opened or modified here.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from railmux import restart_state, tmux_server
from railmux.atomic_file import atomic_write_text
from railmux.provider_paths import running_in_windows_wrapper


_SCHEMA_VERSION = 1
_KIND = "windows-empty-tmux-exit"
_EXIT_SETTLE_NS = 500_000_000


@dataclass(frozen=True)
class _SocketIdentity:
    socket_dev: int
    socket_ino: int
    socket_ctime_ns: int
    parent_dev: int
    parent_ino: int


def _marker_name(label: str) -> str:
    return f"windows-empty-tmux-exit-{label}.json"


def _expected_socket_path(label: str) -> Path | None:
    root = Path(os.environ.get("TMUX_TMPDIR", "/tmp"))
    if not root.is_absolute() or ".." in root.parts:
        return None
    return root / f"tmux-{os.getuid()}" / label


def _socket_identity(path: Path) -> _SocketIdentity | None:
    """Describe one same-user socket without following either final path."""
    try:
        parent = path.parent.lstat()
        endpoint = path.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o022
        or not stat.S_ISSOCK(endpoint.st_mode)
        or endpoint.st_uid != os.getuid()
        or endpoint.st_mode & 0o022
    ):
        return None
    return _SocketIdentity(
        socket_dev=endpoint.st_dev,
        socket_ino=endpoint.st_ino,
        socket_ctime_ns=endpoint.st_ctime_ns,
        parent_dev=parent.st_dev,
        parent_ino=parent.st_ino,
    )


def arm_empty_server_exit(
    *, server_pid: int, session_id: str, pane_id: str,
) -> bool:
    """Authorize later stale-socket removal after an exact empty-server proof."""
    if not running_in_windows_wrapper() or server_pid <= 0:
        return False
    if not session_id.startswith("$") or not session_id[1:].isdigit():
        return False
    if not pane_id.startswith("%") or not pane_id[1:].isdigit():
        return False
    target = tmux_server.current_target()
    if target is None or target.server_pid != server_pid:
        return False

    # Failure to enumerate every server-wide session denies cleanup.
    from railmux import tmux_ctl

    snapshot = tmux_ctl.server_snapshot()
    if (
        os.environ.get("TMUX_PANE") != pane_id
        or tmux_ctl.current_session_id() != session_id
        or tmux_ctl.session_ids() != frozenset({session_id})
        or snapshot is None
        or snapshot.sessions != frozenset({"railmux"})
        or snapshot.panes != frozenset({pane_id})
    ):
        return False
    try:
        label = tmux_server.socket_label()
    except tmux_server.TmuxServerError:
        return False
    expected = _expected_socket_path(label)
    if expected is None or Path(target.socket_path) != expected:
        return False
    identity = _socket_identity(expected)
    if identity is None:
        return False
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "label": label,
        "server_pid": server_pid,
        "session_id": session_id,
        "pane_id": pane_id,
        "recorded_at_ns": time.time_ns(),
        "socket_path": str(expected),
        **asdict(identity),
    }
    try:
        atomic_write_text(
            restart_state.runtime_state_dir() / _marker_name(label),
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def clear_empty_server_exit() -> None:
    """Revoke an older exit proof once a Railmux UI is live again."""
    if not running_in_windows_wrapper():
        return
    try:
        label = tmux_server.socket_label()
        marker = restart_state.runtime_state_dir() / _marker_name(label)
        marker.unlink(missing_ok=True)
    except (OSError, tmux_server.TmuxServerError):
        pass


def _server_pid_is_gone(server_pid: int) -> bool:
    """Return true only when the recorded MSYS2 process no longer exists."""
    try:
        os.kill(server_pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, ValueError):
        # Permission errors and unsupported/ambiguous pid probes deny cleanup.
        return False
    return False


def recover_abandoned_socket() -> bool:
    """Remove one proven-dead MSYS2 socket; never act on a live endpoint."""
    if not running_in_windows_wrapper() or os.environ.get("TMUX"):
        return False
    try:
        label = tmux_server.socket_label()
    except tmux_server.TmuxServerError:
        return False
    try:
        marker = restart_state.runtime_state_dir() / _marker_name(label)
    except OSError:
        return False
    payload = restart_state.read_json_object(marker)
    required = {
        "schema_version", "kind", "label", "server_pid", "session_id",
        "pane_id", "recorded_at_ns", "socket_path", "socket_dev",
        "socket_ino", "socket_ctime_ns", "parent_dev", "parent_ino",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        return False
    session_id = payload.get("session_id")
    pane_id = payload.get("pane_id")
    identity_fields = (
        "socket_dev", "socket_ino", "socket_ctime_ns", "parent_dev",
        "parent_ino",
    )
    integer_fields = ("server_pid", "recorded_at_ns", *identity_fields)
    if (
        payload.get("schema_version") != _SCHEMA_VERSION
        or payload.get("kind") != _KIND
        or payload.get("label") != label
        or not isinstance(session_id, str)
        or not session_id.startswith("$")
        or not session_id[1:].isdigit()
        or not isinstance(pane_id, str)
        or not pane_id.startswith("%")
        or not pane_id[1:].isdigit()
        or any(
            not isinstance(payload.get(name), int)
            or isinstance(payload.get(name), bool)
            or payload[name] < 0
            for name in integer_fields
        )
        or payload["server_pid"] <= 0
    ):
        return False
    expected = _expected_socket_path(label)
    if expected is None or payload.get("socket_path") != str(expected):
        return False
    recorded = _SocketIdentity(**{
        name: payload[name] for name in identity_fields
    })
    if _socket_identity(expected) != recorded:
        return False

    def endpoint_state() -> bool | None:
        """True is live, False is absent/unanswered, None is indeterminate."""
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.25)
        try:
            client.connect(str(expected))
        except (socket.timeout, TimeoutError):
            # A stale MSYS2 AF_UNIX pathname times out instead of returning
            # ECONNREFUSED. The prior sole-pane proof is the cleanup authority.
            return False
        except OSError as exc:
            if exc.errno in {errno.ECONNREFUSED, errno.ENOENT}:
                return False
            return None
        else:
            return True
        finally:
            client.close()

    if endpoint_state() is not False:
        return False
    now_ns = time.time_ns()
    recorded_at_ns = payload["recorded_at_ns"]
    if recorded_at_ns > now_ns:
        return False
    remaining_ns = _EXIT_SETTLE_NS - (now_ns - recorded_at_ns)
    if remaining_ns > 0:
        time.sleep(remaining_ns / 1_000_000_000)
    if (
        _socket_identity(expected) != recorded
        or endpoint_state() is not False
        or not _server_pid_is_gone(payload["server_pid"])
    ):
        return False

    # Narrow the check/unlink race against pathname replacement. Every normal
    # Railmux launcher runs this recovery before starting tmux, and a changed
    # endpoint is retained. An adversarial same-user process can ignore that
    # convention, but cannot redirect this fixed pathname toward provider data.
    if _socket_identity(expected) != recorded:
        return False
    try:
        expected.unlink()
        marker.unlink(missing_ok=True)
    except OSError:
        return False
    return True
