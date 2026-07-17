"""Crash-safe ownership for tmux's server-global root wheel bindings."""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from railmux import restart_state, tmux_ctl
from railmux.atomic_file import atomic_write_text


_VERSION = 1
_MAX_STATE_BYTES = 64 * 1024


class RootWheelForwardingManager:
    """Install one shared root-wheel wrapper per live tmux server.

    tmux key tables are server-global, so every Railmux pane on the same
    server shares one wrapper.  Owners are immutable pane IDs.  The last live
    owner restores only bindings that still carry this transaction's marker;
    a user config reload always wins.
    """

    def __init__(self, server_digest: str, owner_pane_id: str) -> None:
        key = "".join(ch for ch in server_digest if ch.isalnum())[:32]
        self._key = key or "unknown"
        self._owner_pane_id = owner_pane_id
        prefix = f"railmux-wheel-{os.getuid()}-{self._key}"
        self._lock_name = f"{prefix}.lock"
        self._state_name = f"{prefix}.json"
        self._lock_path: Path | None = None
        self._state_path: Path | None = None
        self._registered = False

    @contextmanager
    def _locked(self) -> Iterator[None]:
        root = restart_state.runtime_state_dir()
        self._lock_path = root / self._lock_name
        self._state_path = root / self._state_name
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            # A wedged peer must never delay Railmux startup or shutdown.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> dict | None:
        if self._state_path is None:
            return None
        try:
            info = self._state_path.lstat()
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_size > _MAX_STATE_BYTES):
                return None
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("version") != _VERSION:
            return None
        token = raw.get("token")
        phase = raw.get("phase")
        owners = raw.get("owners")
        backup = raw.get("backup")
        if (not isinstance(token, str) or not token or len(token) > 64
                or phase not in {"installing", "active"}
                or not isinstance(owners, list)
                or len(owners) > 1024
                or any(not isinstance(owner, str) or not owner.startswith("%")
                       for owner in owners)
                or not isinstance(backup, dict)
                or set(backup) != {"WheelUpPane", "WheelDownPane"}
                or any(value is not None and not isinstance(value, str)
                       for value in backup.values())):
            return None
        return raw

    def _save(self, state: dict) -> bool:
        if self._state_path is None:
            return False
        try:
            atomic_write_text(
                self._state_path,
                json.dumps(state, separators=(",", ":"), sort_keys=True),
            )
            os.chmod(self._state_path, 0o600)
            return True
        except OSError:
            return False

    def _remove_state(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.unlink()
        except OSError:
            pass

    @staticmethod
    def _live_panes() -> frozenset[str] | None:
        snapshot = tmux_ctl.server_snapshot()
        return snapshot.panes if snapshot is not None else None

    @staticmethod
    def _prune_owners(state: dict, live_panes: frozenset[str]) -> list[str]:
        return sorted({owner for owner in state["owners"] if owner in live_panes})

    def open(self) -> bool:
        """Register this pane and ensure both root wheel wrappers are active."""
        if self._registered or not self._owner_pane_id.startswith("%"):
            return self._registered
        try:
            with self._locked():
                live_panes = self._live_panes()
                if live_panes is None or self._owner_pane_id not in live_panes:
                    return False
                state = self._load()
                if state is not None:
                    token = state["token"]
                    state["owners"] = self._prune_owners(state, live_panes)
                    if state["phase"] == "installing":
                        current = tmux_ctl.read_root_wheel_bindings()
                        if not all(
                            tmux_ctl.root_wheel_binding_is_original_or_owned(
                                key, current.get(key),
                                state["backup"].get(key), token)
                            for key in ("WheelUpPane", "WheelDownPane")
                        ):
                            if state["owners"]:
                                return False
                            tmux_ctl.restore_root_wheel_bindings(
                                state["backup"], token=token)
                            self._remove_state()
                            state = None
                        else:
                            if not tmux_ctl.set_root_wheel_forwarding(
                                    state["backup"], token):
                                return False
                            state["phase"] = "active"
                    elif not tmux_ctl.root_wheel_bindings_owned_by(token):
                        # A config reload wins. If every recorded owner died,
                        # remove only our surviving per-key wrappers and forget
                        # the stale transaction.
                        if state["owners"]:
                            return False
                        tmux_ctl.restore_root_wheel_bindings(
                            state["backup"], token=token)
                        self._remove_state()
                        state = None
                    if state is not None:
                        state["owners"] = sorted(
                            {*state["owners"], self._owner_pane_id})
                        if not self._save(state):
                            return False
                        self._registered = True
                        return True

                backup = tmux_ctl.prepare_root_wheel_bindings()
                if backup is None:
                    return False
                token = secrets.token_hex(8)
                state = {
                    "version": _VERSION,
                    "phase": "installing",
                    "token": token,
                    "owners": [self._owner_pane_id],
                    "backup": backup,
                }
                if not self._save(state):
                    return False
                if not tmux_ctl.set_root_wheel_forwarding(backup, token):
                    tmux_ctl.restore_root_wheel_bindings(backup, token=token)
                    self._remove_state()
                    return False
                state["phase"] = "active"
                if not self._save(state):
                    tmux_ctl.restore_root_wheel_bindings(
                        backup, token=token)
                    self._remove_state()
                    return False
                self._registered = True
                return True
        except OSError:
            return False

    def close(self) -> None:
        """Release this pane; the last live owner restores stock bindings."""
        if not self._registered:
            return
        try:
            try:
                with self._locked():
                    state = self._load()
                    if state is None:
                        return
                    live_panes = self._live_panes()
                    if live_panes is None:
                        return
                    owners = set(self._prune_owners(state, live_panes))
                    owners.discard(self._owner_pane_id)
                    state["owners"] = sorted(owners)
                    if owners:
                        self._save(state)
                        return
                    tmux_ctl.restore_root_wheel_bindings(
                        state["backup"], token=state["token"])
                    self._remove_state()
            except OSError:
                pass
        finally:
            self._registered = False
