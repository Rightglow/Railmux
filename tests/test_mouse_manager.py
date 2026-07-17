"""Crash/concurrency safety for server-global root wheel forwarding."""
from __future__ import annotations

from unittest.mock import MagicMock

from railmux import tmux_ctl
from railmux.mouse_manager import RootWheelForwardingManager


def _snapshot(*panes: str) -> tmux_ctl.ServerSnapshot:
    return tmux_ctl.ServerSnapshot(
        sessions=frozenset({"railmux"}), panes=frozenset(panes))


def _install_mocks(monkeypatch, tmp_path):
    backup = {"WheelUpPane": "stock-up", "WheelDownPane": None}
    monkeypatch.setattr(
        "railmux.mouse_manager.restart_state.runtime_state_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(tmux_ctl, "server_snapshot",
                        lambda: _snapshot("%1", "%2"))
    monkeypatch.setattr(tmux_ctl, "prepare_root_wheel_bindings",
                        lambda: backup)
    install = MagicMock(return_value=True)
    restore = MagicMock()
    monkeypatch.setattr(tmux_ctl, "set_root_wheel_forwarding", install)
    monkeypatch.setattr(tmux_ctl, "restore_root_wheel_bindings", restore)
    monkeypatch.setattr(tmux_ctl, "root_wheel_bindings_owned_by",
                        lambda _token: True)
    return backup, install, restore


def test_multiple_owners_share_one_install_and_last_owner_restores(
        monkeypatch, tmp_path):
    backup, install, restore = _install_mocks(monkeypatch, tmp_path)
    first = RootWheelForwardingManager("server", "%1")
    second = RootWheelForwardingManager("server", "%2")

    assert first.open()
    assert second.open()
    assert install.call_count == 1
    first.close()
    restore.assert_not_called()
    second.close()

    restore.assert_called_once()
    assert restore.call_args.args[0] == backup


def test_dead_owner_is_pruned_by_next_process(monkeypatch, tmp_path):
    _backup, install, restore = _install_mocks(monkeypatch, tmp_path)
    crashed = RootWheelForwardingManager("server", "%1")
    assert crashed.open()
    # Simulate process death without close(); only the second pane remains.
    monkeypatch.setattr(tmux_ctl, "server_snapshot", lambda: _snapshot("%2"))
    successor = RootWheelForwardingManager("server", "%2")

    assert successor.open()
    assert install.call_count == 1
    successor.close()

    restore.assert_called_once()


def test_stale_restored_transaction_reinstalls_in_same_open(
        monkeypatch, tmp_path):
    _backup, install, restore = _install_mocks(monkeypatch, tmp_path)
    crashed = RootWheelForwardingManager("server", "%1")
    assert crashed.open()
    monkeypatch.setattr(tmux_ctl, "server_snapshot", lambda: _snapshot("%2"))
    monkeypatch.setattr(
        tmux_ctl, "root_wheel_bindings_owned_by", lambda _token: False)
    successor = RootWheelForwardingManager("server", "%2")

    assert successor.open()

    restore.assert_called_once()
    assert install.call_count == 2


def test_custom_root_bindings_fail_closed_without_mutation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "railmux.mouse_manager.restart_state.runtime_state_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(tmux_ctl, "server_snapshot", lambda: _snapshot("%1"))
    monkeypatch.setattr(tmux_ctl, "prepare_root_wheel_bindings",
                        lambda: None)
    install = MagicMock()
    monkeypatch.setattr(tmux_ctl, "set_root_wheel_forwarding", install)

    assert not RootWheelForwardingManager("server", "%1").open()
    install.assert_not_called()


def test_busy_coordination_lock_never_blocks_startup(monkeypatch, tmp_path):
    _install_mocks(monkeypatch, tmp_path)
    holder = RootWheelForwardingManager("server", "%1")
    contender = RootWheelForwardingManager("server", "%2")

    with holder._locked():
        assert contender.open() is False
