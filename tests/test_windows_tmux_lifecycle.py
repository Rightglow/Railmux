from __future__ import annotations

import json
import os
import socket
import stat

from railmux import tmux_ctl, windows_tmux_lifecycle
from railmux.tmux_ctl import ServerSnapshot


def _listening_socket(monkeypatch, tmp_path, label="railmux-test"):
    monkeypatch.setenv("RAILMUX_WINDOWS_RUNTIME", "msys2")
    monkeypatch.setenv("RAILMUX_TMUX_LABEL", label)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    root = tmp_path / f"tmux-{os.getuid()}"
    root.mkdir(mode=0o700)
    path = root / label
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    monkeypatch.setenv("TMUX", f"{path},123,0")
    monkeypatch.setenv("TMUX_PANE", "%1")
    return listener, path


def test_empty_server_proof_cleans_only_dead_socket_and_not_provider_data(
    monkeypatch, tmp_path,
):
    listener, socket_path = _listening_socket(monkeypatch, tmp_path)
    state_root = tmp_path / "state"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(state_root))
    monkeypatch.setattr(tmux_ctl, "current_session_id", lambda: "$1")
    monkeypatch.setattr(tmux_ctl, "session_ids", lambda: frozenset({"$1"}))
    monkeypatch.setattr(
        tmux_ctl, "server_snapshot",
        lambda: ServerSnapshot(frozenset({"railmux"}), frozenset({"%1"})),
    )
    history = tmp_path / "provider-session.jsonl"
    history.write_bytes(b'{"message":"unchanged"}\n')

    assert windows_tmux_lifecycle.arm_empty_server_exit(
        server_pid=123, session_id="$1", pane_id="%1")
    marker = (
        state_root / "railmux" /
        "windows-empty-tmux-exit-railmux-test.json"
    )
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert set(json.loads(marker.read_text(encoding="utf-8"))) == {
        "schema_version", "kind", "label", "server_pid", "session_id",
        "pane_id", "recorded_at_ns", "socket_path", "socket_dev",
        "socket_ino", "socket_ctime_ns", "parent_dev", "parent_ino",
    }

    monkeypatch.delenv("TMUX")
    assert not windows_tmux_lifecycle.recover_abandoned_socket()
    assert socket_path.exists()

    listener.close()
    def missing_process(_pid, _signal):
        raise ProcessLookupError

    monkeypatch.setattr(windows_tmux_lifecycle.os, "kill", missing_process)
    assert windows_tmux_lifecycle.recover_abandoned_socket()
    assert not socket_path.exists()
    assert not marker.exists()
    assert history.read_bytes() == b'{"message":"unchanged"}\n'


def test_unknown_session_denies_socket_cleanup_authority(monkeypatch, tmp_path):
    listener, socket_path = _listening_socket(monkeypatch, tmp_path)
    state_root = tmp_path / "state"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(state_root))
    monkeypatch.setattr(tmux_ctl, "current_session_id", lambda: "$1")
    monkeypatch.setattr(
        tmux_ctl, "session_ids", lambda: frozenset({"$1", "$99"}))
    monkeypatch.setattr(
        tmux_ctl, "server_snapshot",
        lambda: ServerSnapshot(
            frozenset({"railmux", "cc-hidden"}),
            frozenset({"%1", "%99"}),
        ),
    )

    assert not windows_tmux_lifecycle.arm_empty_server_exit(
        server_pid=123, session_id="$1", pane_id="%1")
    listener.close()
    monkeypatch.delenv("TMUX")
    assert not windows_tmux_lifecycle.recover_abandoned_socket()
    assert socket_path.exists()

    socket_path.unlink()


def test_replaced_socket_cannot_reuse_an_older_cleanup_proof(
    monkeypatch, tmp_path,
):
    listener, socket_path = _listening_socket(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(tmux_ctl, "current_session_id", lambda: "$1")
    monkeypatch.setattr(tmux_ctl, "session_ids", lambda: frozenset({"$1"}))
    monkeypatch.setattr(
        tmux_ctl, "server_snapshot",
        lambda: ServerSnapshot(frozenset({"railmux"}), frozenset({"%1"})),
    )
    assert windows_tmux_lifecycle.arm_empty_server_exit(
        server_pid=123, session_id="$1", pane_id="%1")

    listener.close()
    socket_path.unlink()
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(socket_path))
    replacement.close()
    monkeypatch.delenv("TMUX")

    assert not windows_tmux_lifecycle.recover_abandoned_socket()
    assert socket_path.exists()
    socket_path.unlink()


def test_unresponsive_socket_with_live_server_pid_is_never_removed(
    monkeypatch, tmp_path,
):
    listener, socket_path = _listening_socket(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(tmux_ctl, "current_session_id", lambda: "$1")
    monkeypatch.setattr(tmux_ctl, "session_ids", lambda: frozenset({"$1"}))
    monkeypatch.setattr(
        tmux_ctl, "server_snapshot",
        lambda: ServerSnapshot(frozenset({"railmux"}), frozenset({"%1"})),
    )
    assert windows_tmux_lifecycle.arm_empty_server_exit(
        server_pid=123, session_id="$1", pane_id="%1")
    listener.close()
    monkeypatch.delenv("TMUX")
    monkeypatch.setattr(windows_tmux_lifecycle.os, "kill", lambda _pid, _sig: None)

    assert not windows_tmux_lifecycle.recover_abandoned_socket()
    assert socket_path.exists()
    socket_path.unlink()
