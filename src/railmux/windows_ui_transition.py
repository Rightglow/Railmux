"""Fail-closed app-layer transitions for the managed Windows outer UI.

This module runs only inside Railmux's private MSYS2 runtime.  It never kills
the dedicated tmux server or a provider session.  A cooperative dev24+ App
saves its own state and returns swap-owned panes before ``exec``.  The one-time
legacy path is deliberately narrower: a detached, single-pane outer session
may have only its controller pane respawned, with rollback on failed startup.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from packaging.version import InvalidVersion, Version

from railmux import restart_state, tmux_server, windows_tmux_lifecycle


CURRENT_APP_OPTION = "@railmux_current_app_v1"
REQUESTED_APP_OPTION = "@railmux_requested_app_v1"
TRANSITION_STATUS_OPTION = "@railmux_app_transition_status_v1"
UPGRADE_WAKE_KEY = "F19"
UPGRADE_WAKE_SEQUENCE = "\x1b[33~"
_APP_RE = re.compile(
    r"railmux-([0-9]+(?:\.[0-9]+)*(?:\.dev[0-9]+)?)\Z"
)
_CONTENT_RE = re.compile(r"[0-9a-f]{64}\Z")
_PANE_RE = re.compile(r"%[0-9]+\Z")
_SESSION_RE = re.compile(r"\$[0-9]+\Z")
_OPTION_LIMIT = 2048
_APP_ROOT = Path("/opt/railmux/apps")
_BASE_MARKER = Path("/railmux-base.json")
_BASE_CONTENT_MARKER = Path("/railmux-base-content-v1.json")


@dataclass(frozen=True)
class UiAppIdentity:
    runtime: str
    app: str
    version: str
    base_content_id: str
    session_id: str
    pane_id: str
    pane_pid: int

    def payload(self) -> dict[str, object]:
        return {
            "schema": 1,
            "runtime": self.runtime,
            "app": self.app,
            "version": self.version,
            "base_content_id": self.base_content_id,
            "session_id": self.session_id,
            "pane_id": self.pane_id,
            "pane_pid": self.pane_pid,
        }


@dataclass(frozen=True)
class UpgradeRequest:
    runtime: str
    app: str
    version: str
    base_content_id: str
    pane_id: str
    nonce: str


@dataclass(frozen=True)
class TransitionOutcome:
    status: str
    detail: str | None = None


def diagnostic_status() -> dict[str, str | None]:
    """Return bounded managed-UI identity without paths, PIDs, or session IDs."""
    runtime = os.environ.get("RAILMUX_MSYS2_RUNTIME_ID", "")
    app = os.environ.get("RAILMUX_MSYS2_APP_ID", "")
    app_match = _APP_RE.fullmatch(app)
    result: dict[str, str | None] = {
        "runtime_id": runtime or None,
        "app_version": app_match.group(1) if app_match is not None else None,
        "base_content_id": _base_identity(runtime),
        "running_ui_version": None,
        "transition_status": None,
    }
    try:
        target = tmux_server.discover_target(timeout=None)
    except tmux_server.TmuxServerError:
        return result
    if target is None:
        return result
    session_id = tmux_server.target_session_id(target, "railmux", timeout=1.0)
    if session_id is None:
        return result
    current = read_current_app(target, session_id)
    if current is not None:
        result["running_ui_version"] = current.version
    status = _target_option(target, session_id, TRANSITION_STATUS_OPTION)
    if status is not None and re.fullmatch(r"[A-Za-z0-9:_.-]{1,96}", status):
        result["transition_status"] = status
    return result


def _read_json(path: Path) -> object | None:
    try:
        info = path.lstat()
        if not path.is_file() or path.is_symlink() or info.st_size > 4096:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _base_identity(runtime: str) -> str | None:
    base = _read_json(_BASE_MARKER)
    content = _read_json(_BASE_CONTENT_MARKER)
    content_id = content.get("content_id") if isinstance(content, dict) else None
    if (
        base != {"schema": 1, "runtime": runtime}
        or not isinstance(content, dict)
        or content.get("schema") != 1
        or content.get("runtime") != runtime
        or not isinstance(content_id, str)
        or _CONTENT_RE.fullmatch(content_id) is None
    ):
        return None
    return content_id


def _validated_app(
    app: str,
    version: str,
    *,
    runtime: str,
    base_content_id: str,
) -> Path | None:
    match = _APP_RE.fullmatch(app)
    if match is None or match.group(1) != version:
        return None
    if _base_identity(runtime) != base_content_id:
        return None
    application = _APP_ROOT / app
    executable = application / "venv" / "bin" / "railmux"
    marker = _read_json(application / "railmux-app.json")
    if marker not in (
        {"schema": 1, "runtime": runtime, "railmux": version},
        {
            "schema": 2,
            "runtime": runtime,
            "railmux": version,
            "base_content_id": base_content_id,
        },
    ):
        return None
    try:
        if application.is_symlink() or not executable.is_file():
            return None
    except OSError:
        return None
    return executable


def _decode_identity(raw: str | None) -> UiAppIdentity | None:
    if raw is None or len(raw) > _OPTION_LIMIT:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        identity = UiAppIdentity(
            payload["runtime"],
            payload["app"],
            payload["version"],
            payload["base_content_id"],
            payload["session_id"],
            payload["pane_id"],
            payload["pane_pid"],
        )
    except KeyError:
        return None
    if (
        payload.get("schema") != 1
        or not isinstance(identity.runtime, str)
        or not isinstance(identity.app, str)
        or not isinstance(identity.version, str)
        or not isinstance(identity.base_content_id, str)
        or _CONTENT_RE.fullmatch(identity.base_content_id) is None
        or not isinstance(identity.session_id, str)
        or _SESSION_RE.fullmatch(identity.session_id) is None
        or not isinstance(identity.pane_id, str)
        or _PANE_RE.fullmatch(identity.pane_id) is None
        or not isinstance(identity.pane_pid, int)
        or isinstance(identity.pane_pid, bool)
        or identity.pane_pid <= 0
        or _APP_RE.fullmatch(identity.app) is None
        or _APP_RE.fullmatch(identity.app).group(1) != identity.version
    ):
        return None
    return identity


def _target_option(
    target: tmux_server.TmuxServerTarget,
    session_id: str,
    option: str,
    *,
    timeout: float = 1.0,
) -> str | None:
    try:
        result = subprocess.run(
            tmux_server.target_argv(
                target, "show-options", "-qv", "-t", session_id, option),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.rstrip("\n")
    return value if result.returncode == 0 and len(value) <= _OPTION_LIMIT else None


def read_current_app(
    target: tmux_server.TmuxServerTarget,
    session_id: str,
) -> UiAppIdentity | None:
    identity = _decode_identity(
        _target_option(target, session_id, CURRENT_APP_OPTION))
    if (
        identity is None
        or identity.session_id != session_id
        or not tmux_server.target_is_live(target, timeout=1.0)
    ):
        return None
    return identity


def _set_target_option(
    target: tmux_server.TmuxServerTarget,
    scope: str,
    owner: str,
    option: str,
    value: str,
    *,
    timeout: float = 1.0,
) -> bool:
    if len(value) > _OPTION_LIMIT:
        return False
    try:
        subprocess.run(
            tmux_server.target_argv(
                target, "set-option", scope, "-t", owner, option, value),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _set_status(
    target: tmux_server.TmuxServerTarget,
    session_id: str,
    status: str,
) -> None:
    _set_target_option(
        target, "-q", session_id, TRANSITION_STATUS_OPTION, status)


def _current_managed_identity() -> UiAppIdentity | None:
    runtime = os.environ.get("RAILMUX_MSYS2_RUNTIME_ID", "")
    app = os.environ.get("RAILMUX_MSYS2_APP_ID", "")
    match = _APP_RE.fullmatch(app)
    target = tmux_server.current_target()
    pane_id = os.environ.get("TMUX_PANE", "")
    if target is None or match is None or _PANE_RE.fullmatch(pane_id) is None:
        return None
    session_id = tmux_server.target_session_id(target, "railmux", timeout=1.0)
    content_id = _base_identity(runtime)
    if session_id is None or content_id is None:
        return None
    try:
        raw_pid = subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_pid}"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).strip()
        pane_pid = int(raw_pid)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return UiAppIdentity(
        runtime,
        app,
        match.group(1),
        content_id,
        session_id,
        pane_id,
        pane_pid,
    )


def publish_current_app_ready() -> bool:
    """Publish current code identity only after App reached its ready boundary."""
    identity = _current_managed_identity()
    target = tmux_server.current_target()
    if identity is None or target is None:
        return False
    encoded = json.dumps(identity.payload(), separators=(",", ":"), sort_keys=True)
    if not _set_target_option(
        target, "-q", identity.session_id, CURRENT_APP_OPTION, encoded):
        return False
    _set_status(target, identity.session_id, "ready")
    try:
        subprocess.run(
            ["tmux", "set-option", "-pu", "-t", identity.pane_id, "remain-on-exit"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return True


def consume_upgrade_request() -> UpgradeRequest | None:
    """Consume and validate one cooperative request in the exact controller."""
    identity = _current_managed_identity()
    if identity is None:
        return None
    try:
        raw = subprocess.check_output(
            ["tmux", "show-options", "-pqv", "-t", identity.pane_id,
             REQUESTED_APP_OPTION],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).strip()
        subprocess.run(
            ["tmux", "set-option", "-pu", "-t", identity.pane_id,
             REQUESTED_APP_OPTION],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    if not raw or len(raw) > _OPTION_LIMIT:
        return None
    try:
        payload = json.loads(raw)
        request = UpgradeRequest(
            payload["runtime"], payload["app"], payload["version"],
            payload["base_content_id"], payload["pane_id"], payload["nonce"],
        )
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    try:
        newer = Version(request.version) > Version(identity.version)
    except InvalidVersion:
        newer = False
    if (
        payload.get("schema") != 1
        or request.runtime != identity.runtime
        or request.base_content_id != identity.base_content_id
        or request.pane_id != identity.pane_id
        or not isinstance(request.nonce, str)
        or re.fullmatch(r"[0-9a-f]{32}", request.nonce) is None
        or not newer
        or _validated_app(
            request.app,
            request.version,
            runtime=request.runtime,
            base_content_id=request.base_content_id,
        ) is None
    ):
        return None
    return request


def upgrade_exec_argv(request: UpgradeRequest, argv: Sequence[str]) -> list[str] | None:
    executable = _validated_app(
        request.app,
        request.version,
        runtime=request.runtime,
        base_content_id=request.base_content_id,
    )
    if executable is None:
        return None
    filtered = [value for value in argv[1:] if value != "--recover-ui-upgrade"]
    return [str(executable), *filtered]


@contextmanager
def _transition_lock(label: str, session_id: str) -> Iterator[bool]:
    root = restart_state.runtime_state_dir()
    safe_session = session_id.removeprefix("$")
    path = root / f"windows-ui-transition-{label}-{safe_session}.lock"
    try:
        stream = path.open("a+b")
    except OSError:
        yield False
        return
    locked = False
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            pass
        yield locked
    finally:
        if locked:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        stream.close()


def _session_shape(
    target: tmux_server.TmuxServerTarget,
    session_id: str,
) -> tuple[int, tuple[tuple[str, int, bool], ...], str | None] | None:
    try:
        attached_raw = subprocess.check_output(
            tmux_server.target_argv(
                target, "display-message", "-p", "-t", session_id,
                "#{session_attached}"),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).strip()
        panes_raw = subprocess.check_output(
            tmux_server.target_argv(
                target, "list-panes", "-s", "-t", session_id, "-F",
                "#{pane_id} #{pane_pid} #{pane_dead}"),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).splitlines()
        controller = subprocess.check_output(
            tmux_server.target_argv(
                target, "display-message", "-p", "-t", session_id,
                "#{@railmux_controller_pane}"),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).strip()
        attached = int(attached_raw)
        panes: list[tuple[str, int, bool]] = []
        for row in panes_raw:
            fields = row.split(" ")
            if len(fields) != 3 or _PANE_RE.fullmatch(fields[0]) is None:
                return None
            panes.append((fields[0], int(fields[1]), fields[2] == "1"))
        return attached, tuple(panes), controller or None
    except (
        OSError,
        UnicodeError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return None


def _legacy_app_from_pane(
    target: tmux_server.TmuxServerTarget,
    pane_id: str,
    pane_pid: int,
) -> tuple[str, str] | None:
    candidates: list[str] = []
    try:
        raw = Path(f"/proc/{pane_pid}/cmdline").read_bytes()
        if len(raw) <= _OPTION_LIMIT * 4:
            candidates.extend(
                value.decode("utf-8", errors="strict")
                for value in raw.split(b"\0") if value
            )
    except (OSError, UnicodeError):
        pass
    try:
        start = subprocess.check_output(
            tmux_server.target_argv(
                target, "display-message", "-p", "-t", pane_id,
                "#{pane_start_command}"),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        )
        if len(start) <= _OPTION_LIMIT * 4:
            candidates.append(start)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    pattern = re.compile(
        r"/opt/railmux/apps/"
        r"(railmux-([0-9]+(?:\.[0-9]+)*(?:\.dev[0-9]+)?))/venv/bin/railmux"
    )
    for candidate in candidates:
        match = pattern.search(candidate)
        if match is not None:
            return match.group(1), match.group(2)
    return None


def _wait_for_app(
    target: tmux_server.TmuxServerTarget,
    session_id: str,
    app: str,
    *,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = read_current_app(target, session_id)
        if current is not None and current.app == app:
            return True
        time.sleep(0.1)
    return False


def _request_cooperative(
    target: tmux_server.TmuxServerTarget,
    current: UiAppIdentity,
    *,
    target_app: str,
    target_version: str,
    base_content_id: str,
) -> bool:
    request = {
        "schema": 1,
        "runtime": current.runtime,
        "app": target_app,
        "version": target_version,
        "base_content_id": base_content_id,
        "pane_id": current.pane_id,
        "nonce": secrets.token_hex(16),
    }
    encoded = json.dumps(request, separators=(",", ":"), sort_keys=True)
    if not _set_target_option(
        target, "-p", current.pane_id, REQUESTED_APP_OPTION, encoded):
        return False
    try:
        subprocess.run(
            tmux_server.target_argv(
                target, "send-keys", "-t", current.pane_id, "-l", "--",
                UPGRADE_WAKE_SEQUENCE),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            check=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _respawn(
    target: tmux_server.TmuxServerTarget,
    pane_id: str,
    *,
    runtime: str,
    app: str,
    executable: Path,
    forwarded_args: Sequence[str],
    recovery_entry: bool,
) -> bool:
    args = [value for value in forwarded_args if value != "--inside-tmux"]
    recovery_args = ["--recover-ui-upgrade"] if recovery_entry else []
    command = tmux_server.target_argv(
        target,
        "respawn-pane", "-k", "-t", pane_id,
        "-e", "RAILMUX_WINDOWS_RUNTIME=msys2",
        "-e", f"RAILMUX_MSYS2_RUNTIME_ID={runtime}",
        "-e", f"RAILMUX_MSYS2_APP_ID={app}",
        str(executable), "--inside-tmux", *recovery_args, *args,
    )
    try:
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def ensure_current_ui(
    target: tmux_server.TmuxServerTarget,
    session_id: str,
    *,
    runtime: str,
    target_app: str,
    target_version: str,
    forwarded_args: Sequence[str],
    timeout: float = 8.0,
) -> TransitionOutcome:
    """Converge a detached managed outer UI without touching providers."""
    content_id = _base_identity(runtime)
    executable = (
        _validated_app(
            target_app,
            target_version,
            runtime=runtime,
            base_content_id=content_id,
        )
        if content_id is not None
        else None
    )
    if executable is None:
        return TransitionOutcome("blocked", "current app marker is invalid")
    label = tmux_server.socket_label()
    with _transition_lock(label, session_id) as locked:
        if not locked:
            return (
                TransitionOutcome("updated")
                if _wait_for_app(target, session_id, target_app, timeout=timeout)
                else TransitionOutcome("pending", "another launcher is checking it")
            )
        current = read_current_app(target, session_id)
        if current is not None:
            try:
                order = (Version(current.version) > Version(target_version)) - (
                    Version(current.version) < Version(target_version))
            except InvalidVersion:
                return TransitionOutcome("blocked", "running app version is invalid")
            if order == 0 and current.app == target_app:
                return TransitionOutcome("current")
            if order > 0:
                return TransitionOutcome("newer", current.version)
            shape = _session_shape(target, session_id)
            if shape is None or shape[0] != 0:
                return TransitionOutcome(
                    "pending", "the shared UI still has an attached terminal")
            if not _request_cooperative(
                target,
                current,
                target_app=target_app,
                target_version=target_version,
                base_content_id=content_id,
            ):
                return TransitionOutcome("pending", "upgrade request was not accepted")
            return (
                TransitionOutcome("updated")
                if _wait_for_app(target, session_id, target_app, timeout=timeout)
                else TransitionOutcome("pending", "running UI did not switch in time")
            )

        # dev11-dev23 do not publish CURRENT_APP_OPTION.  Limit their one-time
        # migration to a detached, single live controller pane.
        shape = _session_shape(target, session_id)
        if shape is None:
            return TransitionOutcome("pending", "outer UI identity was unavailable")
        attached, panes, controller = shape
        if attached != 0 or len(panes) != 1:
            return TransitionOutcome(
                "pending", "legacy UI is attached or has displayed agent panes")
        pane_id, pane_pid, dead = panes[0]
        if dead or controller != pane_id:
            return TransitionOutcome("pending", "legacy controller was not exact")
        legacy = _legacy_app_from_pane(target, pane_id, pane_pid)
        if legacy is None:
            return TransitionOutcome("pending", "legacy app version was not provable")
        legacy_app, legacy_version = legacy
        try:
            if Version(legacy_version) >= Version(target_version):
                return TransitionOutcome("pending", "legacy version was not older")
        except InvalidVersion:
            return TransitionOutcome("pending", "legacy version was invalid")
        legacy_executable = _validated_app(
            legacy_app,
            legacy_version,
            runtime=runtime,
            base_content_id=content_id,
        )
        if legacy_executable is None:
            return TransitionOutcome("pending", "legacy app marker was invalid")
        # Close every observation race under the same exact target.
        if _session_shape(target, session_id) != shape:
            return TransitionOutcome("pending", "outer UI changed during validation")
        _set_target_option(
            target, "-p", pane_id, "remain-on-exit", "on")
        windows_tmux_lifecycle.clear_empty_server_exit()
        _set_status(target, session_id, "transitioning:legacy")
        if not _respawn(
            target,
            pane_id,
            runtime=runtime,
            app=target_app,
            executable=executable,
            forwarded_args=forwarded_args,
            recovery_entry=True,
        ):
            return TransitionOutcome("pending", "legacy UI respawn failed")
        if _wait_for_app(target, session_id, target_app, timeout=timeout):
            return TransitionOutcome("updated")
        # Preserve usability when the new app cannot reach its ready boundary.
        _respawn(
            target,
            pane_id,
            runtime=runtime,
            app=legacy_app,
            executable=legacy_executable,
            forwarded_args=forwarded_args,
            recovery_entry=False,
        )
        _set_status(target, session_id, "pending:new-app-failed")
        return TransitionOutcome("pending", "new UI failed; legacy UI restored")
