from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import urwid

from railmux import tmux_server, windows_ui_transition as transition
from railmux.windows_msys2 import MSYS2_ARCHIVE_SHA256, MSYS2_RUNTIME_ID
from railmux.ui import app as app_module
from railmux.ui.app import App


RUNTIME = MSYS2_RUNTIME_ID
CONTENT = "a" * 64
TARGET_APP = "railmux-0.4.0.dev24"
TARGET_VERSION = "0.4.0.dev24"
TARGET = tmux_server.TmuxServerTarget("/tmp/private-railmux.sock", 123)


def _identity(version: str = "0.4.0.dev23") -> transition.UiAppIdentity:
    return transition.UiAppIdentity(
        RUNTIME,
        f"railmux-{version}",
        version,
        CONTENT,
        "$1",
        "%2",
        456,
    )


def _validated_tree(monkeypatch, tmp_path: Path) -> Path:
    base_marker = tmp_path / "railmux-base.json"
    content_marker = tmp_path / "railmux-base-content.json"
    applications = tmp_path / "opt" / "railmux" / "apps"
    application = applications / TARGET_APP
    executable = application / "venv" / "bin" / "railmux"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    base_marker.write_text(
        json.dumps({"schema": 2, "runtime": RUNTIME}), encoding="utf-8")
    content_marker.write_text(
        json.dumps({
            "schema": 2,
            "runtime": RUNTIME,
            "archive_sha256": MSYS2_ARCHIVE_SHA256,
            "content_id": CONTENT,
            "package_count": 3,
            "core_packages": {
                "tmux": "3.7.b-1",
                "python": "3.12.13-1",
                "python-pip": "26.1.2-1",
            },
        }),
        encoding="utf-8",
    )
    (application / "railmux-app.json").write_text(
        json.dumps({
            "schema": 2,
            "runtime": RUNTIME,
            "railmux": TARGET_VERSION,
            "base_content_id": CONTENT,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(transition, "_BASE_MARKER", base_marker)
    monkeypatch.setattr(transition, "_BASE_CONTENT_MARKER", content_marker)
    monkeypatch.setattr(transition, "_APP_ROOT", applications)
    return executable


def test_diagnostic_status_reports_effective_tmux_visual_fidelity(monkeypatch):
    monkeypatch.setattr(transition, "_base_identity", lambda _runtime: CONTENT)
    monkeypatch.setattr(transition.tmux_ctl, "tmux_version", lambda: (3, 6))
    monkeypatch.setattr(transition.tmux_server, "discover_target", lambda **_kw: None)

    status = transition.diagnostic_status({
        "RAILMUX_MSYS2_RUNTIME_ID": RUNTIME,
        "RAILMUX_MSYS2_APP_ID": TARGET_APP,
    })

    assert status["tmux_capability"] == {
        "minimum_supported": "2.7",
        "source": "effective_tmux",
        "support": "supported",
        "verification": "effective",
        "version": "3.6",
        "windows_visual_fidelity": "degraded",
        "windows_visual_fidelity_recommended": "3.7",
    }
    assert status["legacy_runtime"]["status"] == "not_reported"


def test_diagnostic_status_accepts_only_bounded_native_legacy_snapshot(monkeypatch):
    monkeypatch.setattr(transition, "_base_identity", lambda _runtime: CONTENT)
    monkeypatch.setattr(transition.tmux_ctl, "tmux_version", lambda: (3, 7))
    monkeypatch.setattr(transition.tmux_server, "discover_target", lambda **_kw: None)
    snapshot = {
        "schema": 1,
        "status": "blocked",
        "migration": "restart_required",
        "legacy_generation_count": 1,
        "busy_generation_count": 1,
        "process_count": 3,
        "tmux_process_count": 1,
        "provider_process_count": 2,
        "unreachable_generation_count": 1,
        "generations": [{
            "runtime": "msys2-2026-03-22",
            "status": "busy",
            "process_count": 3,
            "tmux_process_count": 1,
            "provider_process_count": 2,
            "tmux_server": "unreachable",
        }],
    }

    status = transition.diagnostic_status({
        "RAILMUX_MSYS2_RUNTIME_ID": RUNTIME,
        "RAILMUX_MSYS2_APP_ID": TARGET_APP,
        transition.LEGACY_RUNTIME_STATUS_ENV: json.dumps(snapshot),
    })
    assert status["legacy_runtime"] == snapshot

    status = transition.diagnostic_status({
        "RAILMUX_MSYS2_RUNTIME_ID": RUNTIME,
        "RAILMUX_MSYS2_APP_ID": TARGET_APP,
        transition.LEGACY_RUNTIME_STATUS_ENV: json.dumps({
            **snapshot,
            "generations": [{**snapshot["generations"][0], "runtime": "secret/path"}],
        }),
    })
    assert status["legacy_runtime"]["status"] == "not_reported"


def test_upgrade_exec_requires_exact_content_bound_app(monkeypatch, tmp_path):
    executable = _validated_tree(monkeypatch, tmp_path)
    request = transition.UpgradeRequest(
        RUNTIME, TARGET_APP, TARGET_VERSION, CONTENT, "%2", "b" * 32,
        time.time() + 10.0,
    )

    assert transition.upgrade_exec_argv(
        request,
        ["old-railmux", "--inside-tmux", "--project", "/work"],
    ) == [str(executable), "--inside-tmux", "--project", "/work"]

    (tmp_path / "railmux-base-content.json").write_text(
        json.dumps({
            "schema": 2,
            "runtime": RUNTIME,
            "archive_sha256": MSYS2_ARCHIVE_SHA256,
            "content_id": "c" * 64,
            "package_count": 3,
            "core_packages": {
                "tmux": "3.7.b-1",
                "python": "3.12.13-1",
                "python-pip": "26.1.2-1",
            },
        }),
        encoding="utf-8",
    )
    assert transition.upgrade_exec_argv(request, ["old-railmux"]) is None


def test_attached_cooperative_ui_is_never_mutated(monkeypatch, tmp_path):
    _validated_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(transition.tmux_server, "socket_label", lambda: "label")
    monkeypatch.setattr(transition, "_transition_lock", _always_locked)
    monkeypatch.setattr(transition, "read_current_app", lambda *_args: _identity())
    monkeypatch.setattr(
        transition, "_session_shape", lambda *_args: (1, (("%2", 456, False),), "%2"))
    requested = []
    monkeypatch.setattr(
        transition, "_request_cooperative", lambda *_args, **_kwargs: requested.append(1))

    result = transition.ensure_current_ui(
        TARGET,
        "$1",
        runtime=RUNTIME,
        target_app=TARGET_APP,
        target_version=TARGET_VERSION,
    )

    assert result.status == "pending"
    assert requested == []


class _always_locked:
    def __init__(self, *_args):
        pass

    def __enter__(self):
        return True

    def __exit__(self, *_args):
        return False


def test_detached_cooperative_ui_requests_exact_upgrade(monkeypatch, tmp_path):
    _validated_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(transition.tmux_server, "socket_label", lambda: "label")
    monkeypatch.setattr(transition, "_transition_lock", _always_locked)
    monkeypatch.setattr(transition, "read_current_app", lambda *_args: _identity())
    monkeypatch.setattr(
        transition, "_session_shape", lambda *_args: (0, (("%2", 456, False),), "%2"))
    monkeypatch.setattr(transition, "_probe_app_version", lambda *_args: True)
    requested = []
    monkeypatch.setattr(
        transition,
        "_request_cooperative",
        lambda *_args, **kwargs: requested.append(kwargs) or True,
    )
    monkeypatch.setattr(transition, "_wait_for_app", lambda *_args, **_kwargs: True)

    result = transition.ensure_current_ui(
        TARGET,
        "$1",
        runtime=RUNTIME,
        target_app=TARGET_APP,
        target_version=TARGET_VERSION,
    )

    assert result.status == "updated"
    assert requested == [{
        "target_app": TARGET_APP,
        "target_version": TARGET_VERSION,
        "base_content_id": CONTENT,
        "timeout": 15.0,
    }]


def test_cooperative_timeout_clears_request(monkeypatch, tmp_path):
    _validated_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(transition.tmux_server, "socket_label", lambda: "label")
    monkeypatch.setattr(transition, "_transition_lock", _always_locked)
    monkeypatch.setattr(transition, "read_current_app", lambda *_args: _identity())
    monkeypatch.setattr(
        transition, "_session_shape", lambda *_args: (0, (("%2", 456, False),), "%2"))
    monkeypatch.setattr(transition, "_probe_app_version", lambda *_args: True)
    monkeypatch.setattr(transition, "_request_cooperative", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(transition, "_wait_for_app", lambda *_args, **_kwargs: False)
    cleared = []
    monkeypatch.setattr(
        transition, "_clear_upgrade_request",
        lambda *_args: cleared.append(1),
    )

    result = transition.ensure_current_ui(
        TARGET,
        "$1",
        runtime=RUNTIME,
        target_app=TARGET_APP,
        target_version=TARGET_VERSION,
    )

    assert result.status == "pending"
    assert cleared == [1]


def test_expired_cooperative_request_is_consumed_but_rejected(monkeypatch):
    identity = _identity()
    payload = {
        "schema": 1,
        "runtime": RUNTIME,
        "app": TARGET_APP,
        "version": TARGET_VERSION,
        "base_content_id": CONTENT,
        "pane_id": "%2",
        "nonce": "b" * 32,
        "expires_at": time.time() - 1.0,
    }
    monkeypatch.setattr(
        transition, "_current_managed_identity", lambda: identity)
    monkeypatch.setattr(transition.tmux_server, "current_target", lambda: TARGET)
    monkeypatch.setattr(
        transition.subprocess,
        "check_output",
        lambda *_args, **_kwargs: json.dumps(payload),
    )
    cleared = []
    monkeypatch.setattr(
        transition.subprocess,
        "run",
        lambda *_args, **_kwargs: cleared.append(1),
    )

    assert transition.consume_upgrade_request() is None
    assert cleared == [1]


def test_unidentified_controller_is_never_respawned(monkeypatch, tmp_path):
    _validated_tree(monkeypatch, tmp_path)
    shape = (0, (("%2", 456, False),), "%2")
    monkeypatch.setattr(transition.tmux_server, "socket_label", lambda: "label")
    monkeypatch.setattr(transition, "_transition_lock", _always_locked)
    monkeypatch.setattr(transition, "read_current_app", lambda *_args: None)
    monkeypatch.setattr(transition, "_session_shape", lambda *_args: shape)
    mutations = []
    monkeypatch.setattr(
        transition, "_set_target_option",
        lambda *_args, **_kwargs: mutations.append(1) or True,
    )

    result = transition.ensure_current_ui(
        TARGET,
        "$1",
        runtime=RUNTIME,
        target_app=TARGET_APP,
        target_version=TARGET_VERSION,
    )

    assert result.status == "blocked"
    assert "Soft Quit" in (result.detail or "")
    assert mutations == []


def test_unidentified_multiple_panes_are_left_untouched(monkeypatch, tmp_path):
    _validated_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(transition.tmux_server, "socket_label", lambda: "label")
    monkeypatch.setattr(transition, "_transition_lock", _always_locked)
    monkeypatch.setattr(transition, "read_current_app", lambda *_args: None)
    monkeypatch.setattr(
        transition,
        "_session_shape",
        lambda *_args: (0, (("%2", 456, False), ("%3", 789, False)), "%2"),
    )
    result = transition.ensure_current_ui(
        TARGET,
        "$1",
        runtime=RUNTIME,
        target_app=TARGET_APP,
        target_version=TARGET_VERSION,
    )

    assert result.status == "blocked"


def test_current_controller_startup_is_recognized(monkeypatch, tmp_path):
    _validated_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(transition.tmux_server, "socket_label", lambda: "label")
    monkeypatch.setattr(transition, "_transition_lock", _always_locked)
    monkeypatch.setattr(transition, "read_current_app", lambda *_args: None)
    monkeypatch.setattr(
        transition, "_session_shape",
        lambda *_args: (0, (("%2", 456, False),), "%2"),
    )
    monkeypatch.setattr(
        transition, "_pane_app_version", lambda *_args: TARGET_VERSION)

    result = transition.ensure_current_ui(
        TARGET,
        "$1",
        runtime=RUNTIME,
        target_app=TARGET_APP,
        target_version=TARGET_VERSION,
    )

    assert result.status == "starting"


def test_cooperative_wake_saves_state_and_returns_provider_panes(monkeypatch):
    request = transition.UpgradeRequest(
        RUNTIME, TARGET_APP, TARGET_VERSION, CONTENT, "%2", "b" * 32,
        time.time() + 10.0,
    )
    monkeypatch.setattr(app_module, "running_in_windows_wrapper", lambda: True)
    monkeypatch.setattr(
        transition, "consume_upgrade_request", lambda: request)
    app = App.__new__(App)
    observed = []
    app._save_state = lambda **kwargs: observed.append(("save", kwargs))
    app._publish_managed_restart_handoff = lambda: observed.append(("handoff", {}))
    app._teardown_tmux = lambda **kwargs: observed.append(("teardown", kwargs))
    app._soft_quit_flag = False
    app._ui_upgrade_request = None

    with pytest.raises(urwid.ExitMainLoop):
        app._on_input("f19")

    assert app._soft_quit_flag is True
    assert app._ui_upgrade_request == request
    assert observed == [
        ("save", {"portable_right": True}),
        ("handoff", {}),
        ("teardown", {"defer_outer": True}),
    ]
