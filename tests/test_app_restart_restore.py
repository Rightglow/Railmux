"""Tests for soft-quit feature: state file, orphan discovery, truncated ID
resolution, QuitConfirmModal s-key, and teardown branching."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from railmux import restart_state
from railmux.restart_state import OuterTmuxIdentity
from railmux.ui.app import App, _Running
from railmux.ui.workspace import (
    AgentWorkspace,
    SlotRestoreState,
    WorkspaceLayout,
    WorkspacePresentation,
)
from railmux.settings import LayoutProfile


from tests.app_test_harness import (
    _minimal_app,
    _project,
    isolate_tmux_identity_stamps as isolate_tmux_identity_stamps,
)


pytestmark = pytest.mark.usefixtures("isolate_tmux_identity_stamps")


def test_resolve_truncated_id_finds_full_uuid(tmp_path):
    """Given a truncated key, return the full session_id from .jsonl files."""
    proj = _project(claude_dir=tmp_path)
    full_id = "ae54affd-ec33-465c-b3c4-c1dc7c46990b"
    (tmp_path / f"{full_id}.jsonl").write_text("{}")
    (tmp_path / "other.jsonl").write_text("{}")

    result = App._resolve_truncated_id("ae54affd-ec33-46", proj)
    assert result == full_id


def test_resolve_truncated_id_no_match(tmp_path):
    proj = _project(claude_dir=tmp_path)
    (tmp_path / "ae54affd-ec33-465c-b3c4-c1dc7c46990b.jsonl").write_text("{}")

    result = App._resolve_truncated_id("zzzzzzzz-zzzz-zz", proj)
    assert result is None


def test_resolve_truncated_id_empty_dir(tmp_path):
    proj = _project(claude_dir=tmp_path)
    result = App._resolve_truncated_id("anything", proj)
    assert result is None


def test_resolve_truncated_id_skips_non_jsonl(tmp_path):
    proj = _project(claude_dir=tmp_path)
    (tmp_path / "readme.txt").write_text("hello")
    result = App._resolve_truncated_id("anything", proj)
    assert result is None


# ── _safe_name ───────────────────────────────────────────────────────────


def test_safe_name_truncates():
    assert (
        App._safe_name("ae54affd-ec33-465c-b3c4-c1dc7c46990b", 16) == "ae54affd-ec33-46"
    )


def test_safe_name_replaces_non_alnum():
    assert App._safe_name("abc def!ghi", 10) == "abc-def-gh"


def test_safe_name_strips_leading_dashes():
    assert App._safe_name("---abc", 10) == "abc"


# ── state file ───────────────────────────────────────────────────────────


def test_state_path_uses_xdg_runtime_dir(monkeypatch):
    monkeypatch.setitem(os.environ, "XDG_RUNTIME_DIR", "/run/user/1000")
    assert restart_state.instances_dir() == Path("/run/user/1000/railmux/instances")


def test_state_path_falls_back_to_tmp(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    assert restart_state.instances_dir() == Path("/tmp/railmux-1000/railmux/instances")


def test_save_and_load_state_round_trip(tmp_path, monkeypatch):
    """_save_state writes JSON; _load_state reads it back."""
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: tmp_path / "state.json")
    )

    app = _minimal_app(selected_project=_project("myproj"))
    app._save_state()
    assert (tmp_path / "state.json").is_file()

    data = app._load_state()
    assert data["project"] == "-tmp-myproj"
    assert data["right_kind"] == "empty"


def test_local_view_wins_over_shared_portable_view(tmp_path, monkeypatch):
    local_path = tmp_path / "local.json"
    portable_path = tmp_path / "portable-shared.json"
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: local_path))
    monkeypatch.setattr(
        App, "_portable_state_path", staticmethod(lambda: portable_path)
    )
    app = _minimal_app(selected_project=_project("one"))
    app._projects_pane = MagicMock(filter_text="mine")
    app._sessions_pane = MagicMock(filter_text="")
    app._save_state()
    restart_state.write_portable(
        {
            "schema_version": 1,
            "kind": "portable",
            "view": restart_state.build_view(
                {
                    "mode": "codex",
                    "project": "-tmp-other",
                    "session_filter": "foreign-filter",
                }
            ),
        },
        portable_path,
    )

    data = app._load_state()

    assert data["mode"] == "claude"
    assert data["project"] == "-tmp-one"
    assert data["project_filter"] == "mine"
    assert "session_filter" not in data


def test_foreign_local_owner_is_ignored_without_process_restore(tmp_path, monkeypatch):
    local_path = tmp_path / "foreign.json"
    portable_path = tmp_path / "portable.json"
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: local_path))
    monkeypatch.setattr(
        App, "_portable_state_path", staticmethod(lambda: portable_path)
    )
    app = _minimal_app()
    foreign = OuterTmuxIdentity("b" * 64, 456, "%9", "$9", "@9")
    restart_state.write_instance(
        foreign,
        {
            "schema_version": 1,
            "kind": "instance",
            "owner": foreign.to_json(),
            "view": restart_state.build_view({"mode": "codex"}),
            "recovery": {
                "right_kind": "agent",
                "right_tmux": "cx-foreign",
            },
        },
        local_path,
    )
    restart_state.write_portable(
        {
            "schema_version": 1,
            "kind": "portable",
            "view": restart_state.build_view({"mode": "claude"}),
        },
        portable_path,
    )

    data = app._load_state()

    assert data == {"mode": "claude"}
    assert "right_tmux" not in data


def test_state_saves_are_independent_when_one_destination_fails(tmp_path, monkeypatch):
    local_path = tmp_path / "local.json"
    portable_path = tmp_path / "portable.json"
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: local_path))
    monkeypatch.setattr(
        App, "_portable_state_path", staticmethod(lambda: portable_path)
    )
    app = _minimal_app()
    monkeypatch.setattr(restart_state, "write_portable", lambda *_a, **_k: False)

    app._save_state()

    assert local_path.exists()


def test_save_state_always_writes_right_kind(tmp_path, monkeypatch):
    """Even without a selected project, _save_state records the right-pane state."""
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: tmp_path / "state.json")
    )
    app = _minimal_app(selected_project=None)
    app._save_state()
    assert (tmp_path / "state.json").is_file()
    data = app._load_state()
    assert data["right_kind"] == "empty"
    assert data["mode"] == "claude"
    assert data["workspace"] == {
        "version": 1,
        "layout": "single",
        "target": "primary",
        "focus": "sidebar",
        "slots": {
            "primary": {"kind": "empty"},
            "secondary": {"kind": "empty"},
        },
    }


def test_save_state_with_claude_in_right_pane(tmp_path, monkeypatch):
    """When a Claude session is open, save its tmux name."""
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: tmp_path / "state.json")
    )
    app = _minimal_app(selected_project=_project("myproj"))
    app._right_pane_claude = "cc-abc123"
    app._save_state()
    data = app._load_state()
    assert data["right_kind"] == "agent"
    assert data["right_tmux"] == "cc-abc123"


def test_save_state_with_preview_in_right_pane(tmp_path, monkeypatch):
    """When a transcript preview is showing, save the session id."""
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: tmp_path / "state.json")
    )
    app = _minimal_app(selected_project=_project("myproj"))
    app._in_history_mode = True
    app._active_session_id = "abc123"
    app._save_state()
    data = app._load_state()
    assert data["right_kind"] == "preview"
    assert data["right_session"] == "abc123"


def test_soft_quit_portable_state_keeps_stable_agent_not_tmux_name(
    tmp_path, monkeypatch
):
    local_path = tmp_path / "local.json"
    portable_path = tmp_path / "portable.json"
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: local_path))
    monkeypatch.setattr(
        App, "_portable_state_path", staticmethod(lambda: portable_path)
    )
    project = _project("myproj")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = _minimal_app(selected_project=project)
    app._running[session_id] = _Running(
        key=session_id,
        tmux_name="cc-12345678-1234-12",
        label="myproj/session",
        project=project,
        session_type="claude",
    )
    app._right_pane_claude = "cc-12345678-1234-12"
    app._active_session_id = session_id
    app._primary_slot.mode_key = "claude"
    app._primary_slot.project_key = project.encoded_name

    app._save_state(portable_right=True)

    portable = restart_state.decode_portable(
        restart_state.read_json_object(portable_path)
    )
    assert portable == {
        "mode": "claude",
        "project": project.encoded_name,
        "right_kind": "agent",
        "right_mode": "claude",
        "right_session": session_id,
        "right_project": project.encoded_name,
    }
    assert "cc-12345678-1234-12" not in portable_path.read_text()
    assert app._load_state()["right_tmux"] == "cc-12345678-1234-12"


def test_portable_state_uses_explicit_active_secondary_slot():
    project = _project("secondary")
    session_id = "22345678-1234-1234-1234-1234567890ab"
    app = _minimal_app(selected_project=project)
    app._running[session_id] = _Running(
        key=session_id,
        tmux_name="cx-secondary",
        label="secondary/session",
        project=project,
        session_type="codex",
    )
    slot = app._agent_workspace().secondary
    slot.agent_tmux_name = "cx-secondary"
    slot.active_session_id = session_id
    slot.mode_key = "codex"
    slot.project_key = project.encoded_name
    app._agent_workspace().set_target(AgentWorkspace.SECONDARY)

    data = app._portable_right_state_data()

    assert data["right_mode"] == "codex"
    assert data["right_session"] == session_id


def test_local_state_keeps_full_dual_workspace_but_portable_does_not(
    tmp_path, monkeypatch
):
    local_path = tmp_path / "local.json"
    portable_path = tmp_path / "portable.json"
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: local_path))
    monkeypatch.setattr(
        App, "_portable_state_path", staticmethod(lambda: portable_path)
    )
    app = _minimal_app(selected_project=_project("dual"))
    workspace = app._agent_workspace()
    workspace.layout = WorkspaceLayout.STACKED
    workspace.primary.agent_tmux_name = "cc-primary"
    workspace.primary.active_session_id = "primary-session"
    workspace.primary.mode_key = "claude"
    workspace.secondary.in_history_mode = True
    workspace.secondary.active_session_id = "secondary-session"
    workspace.secondary.mode_key = "codex"
    workspace.secondary.project_key = "-tmp-secondary"
    workspace.secondary.restore_state = SlotRestoreState("agent", "cx-secondary")
    workspace.set_target(AgentWorkspace.SECONDARY)
    app._railmux_has_focus = True

    app._save_state(portable_right=True)

    saved = app._load_state()["workspace"]
    assert saved["layout"] == "stacked"
    assert saved["target"] == "secondary"
    assert saved["focus"] == "sidebar"
    assert saved["slots"]["primary"]["tmux"] == "cc-primary"
    assert saved["slots"]["secondary"] == {
        "kind": "preview",
        "session": "secondary-session",
        "mode": "codex",
        "project": "-tmp-secondary",
        "restore": {"kind": "agent", "tmux": "cx-secondary"},
    }
    assert "workspace" not in portable_path.read_text()


def test_managed_soft_restart_loads_dual_layout_after_controller_pane_changes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(restart_state, "runtime_base", lambda: tmp_path)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_topology",
        lambda _session: MagicMock(session_name="railmux"),
    )
    monkeypatch.setattr(
        "railmux.restart_state.tmux_ctl.pane_identity", lambda _pane: None
    )
    source = _minimal_app(selected_project=_project("dual"))
    source._auto_launched = True
    source._agent_workspace().layout = WorkspaceLayout.SIDE_BY_SIDE
    source._agent_workspace().set_target(AgentWorkspace.SECONDARY)
    source._railmux_has_focus = True

    source._save_state(portable_right=True)
    assert source._publish_managed_restart_handoff()

    replacement = _minimal_app()
    replacement._auto_launched = True
    replacement._restart_identity = OuterTmuxIdentity(
        server_digest=source._restart_identity.server_digest,
        server_pid=source._restart_identity.server_pid,
        pane_id="%2",
        session_id="$2",
        window_id="@2",
    )
    replacement._loaded_restart_source = None
    replacement._loaded_restart_state_path = None

    restored = replacement._load_state()

    assert restored is not None
    assert restored["workspace"]["layout"] == "side-by-side"
    assert restored["workspace"]["target"] == "secondary"
    assert replacement._loaded_restart_source == source._restart_identity

    replacement._pending_restore_state = restored
    replacement._running_recovery_ok = True
    replacement._restore_right_pane = MagicMock(return_value=True)
    replacement._restore_pending_right_pane(None, None)

    replacement._restore_right_pane.assert_called_once_with(restored)
    assert not restart_state.instance_state_path(source._restart_identity).exists()
    assert not restart_state.managed_handoff_path(
        replacement._restart_identity
    ).exists()


def test_local_state_snapshots_actual_agent_focus_before_save(tmp_path, monkeypatch):
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: tmp_path / "state.json")
    )
    app = _minimal_app()
    workspace = app._agent_workspace()
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    workspace.primary.pane_id = "%2"
    workspace.secondary.pane_id = "%3"
    workspace.set_target(AgentWorkspace.PRIMARY)
    app._railmux_has_focus = False

    def sync_focus():
        workspace.set_target(AgentWorkspace.SECONDARY)
        return workspace.secondary

    app._sync_target_slot_from_tmux = MagicMock(side_effect=sync_focus)

    app._save_state()

    saved = app._load_state()["workspace"]
    assert saved["target"] == "secondary"
    assert saved["focus"] == "secondary"


def test_local_state_saves_collapsed_agent_with_stable_identity():
    app = _minimal_app()
    workspace = app._agent_workspace()
    workspace.collapsed_secondary_agent = "cx-collapsed"
    app._running["collapsed-session"] = _Running(
        key="collapsed-session",
        tmux_name="cx-collapsed",
        label="collapsed",
        session_type="codex",
    )

    saved = app._workspace_recovery_state_data()

    assert saved["collapsed_secondary"] == {
        "tmux": "cx-collapsed",
        "session": "collapsed-session",
        "mode": "codex",
    }


def test_restore_workspace_rebuilds_both_slots_target_and_agent_focus(monkeypatch):
    app = _minimal_app()
    workspace = app._agent_workspace()
    transport = MagicMock()
    transport.displayed_real_pane.return_value = None
    app._display_transport_manager = transport
    app._agent_region_size = MagicMock(return_value=(200, 40))
    app._layout_fits = MagicMock(return_value=True)
    app._set_railmux_focus = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_tmux_bindings = MagicMock()

    def restore_primary(_state, slot):
        slot.pane_id = "%2"
        slot.agent_tmux_name = "cc-primary"
        return True

    def create_secondary(layout):
        workspace.layout = layout
        workspace.secondary.pane_id = "%3"
        return True

    def restore_secondary(slot, _saved, _bindings):
        slot.in_history_mode = True
        slot.active_session_id = "secondary-session"
        return True

    app._restore_agent_target = MagicMock(side_effect=restore_primary)
    transport.create_secondary.side_effect = create_secondary
    app._restore_workspace_slot = MagicMock(side_effect=restore_secondary)
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)
    saved = {
        "layout": "side-by-side",
        "target": "secondary",
        "focus": "secondary",
        "slots": {
            "primary": {"kind": "agent", "tmux": "cc-primary"},
            "secondary": {
                "kind": "preview",
                "session": "secondary-session",
                "mode": "codex",
            },
        },
    }

    assert app._restore_workspace({"running_bindings": []}, saved)

    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.target_slot_key == AgentWorkspace.SECONDARY
    app._set_railmux_focus.assert_called_with(False, force_border=True)
    app._restore_workspace_slot.assert_called_once_with(
        workspace.secondary, saved["slots"]["secondary"], []
    )


def test_restore_workspace_builds_final_dual_geometry_before_agent_content(monkeypatch):
    app = _minimal_app()
    workspace = app._agent_workspace()
    transport = MagicMock()
    app._display_transport_manager = transport
    app._railmux_pane_id = "%1"
    app._layout_profile = LayoutProfile("always", "side-by-side", 200, 600)
    app._active_sidebar_permille = 200
    app._active_primary_permille = 600
    app._agent_region_size = MagicMock(return_value=(143, 40))
    app._layout_fits = MagicMock(return_value=True)
    app._resize_sidebar_for_layout = MagicMock(return_value=True)
    app._set_railmux_focus = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._apply_layout_profile = MagicMock(return_value=True)

    def create_dual(layout, *, agent_width, secondary_extent):
        assert workspace.primary.pane_id is None
        assert workspace.secondary.pane_id is None
        workspace.primary.pane_id = "%2"
        workspace.secondary.pane_id = "%3"
        workspace.layout = layout
        assert (agent_width, secondary_extent) == (143, 57)
        return True

    def restore_primary(_state, slot):
        assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
        assert workspace.secondary.pane_id == "%3"
        slot.agent_tmux_name = "cc-primary"
        return True

    transport.create_dual.side_effect = create_dual
    app._restore_agent_target = MagicMock(side_effect=restore_primary)
    app._restore_workspace_slot = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.window_size", lambda _pane: (180, 40))
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)
    saved = {
        "layout": "side-by-side",
        "target": "primary",
        "focus": "sidebar",
        "slots": {
            "primary": {"kind": "agent", "tmux": "cc-primary"},
            "secondary": {"kind": "empty"},
        },
    }

    assert app._restore_workspace({}, saved)

    transport.create_dual.assert_called_once_with(
        WorkspaceLayout.SIDE_BY_SIDE,
        agent_width=143,
        secondary_extent=57,
    )
    app._restore_agent_target.assert_called_once()
    app._restore_workspace_slot.assert_called_once_with(
        workspace.secondary, saved["slots"]["secondary"], None
    )


def test_startup_prelayout_builds_saved_dual_before_first_frame(monkeypatch):
    app = _minimal_app()
    app._pending_restore_state = {
        "workspace": {
            "layout": "side-by-side",
            "slots": {
                "primary": {"kind": "agent", "tmux": "cc-primary"},
                "secondary": {"kind": "preview", "session": "history"},
            },
        },
    }
    app._prelayout_created = False
    app._planned_dual_restore_geometry = MagicMock(return_value=(143, 57))
    app._set_railmux_focus = MagicMock()
    transport = MagicMock()
    transport.create_dual.return_value = True
    app._display_transport_manager = transport
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists",
        lambda name: name == "cc-primary",
    )

    assert app._prelayout_pending_workspace()

    transport.create_dual.assert_called_once_with(
        WorkspaceLayout.SIDE_BY_SIDE,
        agent_width=143,
        secondary_extent=57,
    )
    transport.create_primary.assert_not_called()
    app._set_railmux_focus.assert_called_once_with(True, force_border=True)
    assert app._prelayout_created


def test_restore_reuses_startup_prelayout_without_recreating_dual_geometry(monkeypatch):
    app = _minimal_app()
    workspace = app._agent_workspace()
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    workspace.primary.pane_id = "%2"
    workspace.secondary.pane_id = "%3"
    app._prelayout_created = True
    app._planned_dual_restore_geometry = MagicMock(return_value=(143, 57))
    app._restore_agent_target = MagicMock(return_value=True)
    app._restore_workspace_slot = MagicMock(return_value=True)
    app._resize_sidebar_for_layout = MagicMock(return_value=True)
    app._agent_region_size = MagicMock(return_value=(200, 40))
    app._layout_fits = MagicMock(return_value=True)
    app._paint_slot_active_target = MagicMock()
    app._set_workspace_target = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._apply_layout_profile = MagicMock()
    app._install_tmux_bindings = MagicMock()
    transport = MagicMock()
    transport.create_primary.return_value = True
    transport.create_secondary.return_value = True
    app._display_transport_manager = transport
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)

    saved = {
        "layout": "side-by-side",
        "target": "primary",
        "focus": "sidebar",
        "slots": {
            "primary": {"kind": "agent", "tmux": "cc-primary"},
            "secondary": {"kind": "agent", "tmux": "cc-secondary"},
        },
    }

    assert app._restore_workspace({}, saved)

    transport.create_dual.assert_not_called()
    app._restore_agent_target.assert_called_once()
    app._restore_workspace_slot.assert_called_once()


def test_startup_prelayout_builds_saved_single_at_exact_width(monkeypatch):
    app = _minimal_app()
    app._pending_restore_state = {
        "workspace": {
            "layout": "single",
            "slots": {
                "primary": {"kind": "agent", "tmux": "cc-primary"},
                "secondary": {"kind": "empty"},
            },
        },
    }
    app._planned_restore_agent_region = MagicMock(return_value=(126, 38))
    app._set_railmux_focus = MagicMock()
    transport = MagicMock()
    transport.create_primary.return_value = True
    app._display_transport_manager = transport
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.session_exists", lambda _name: True)

    assert app._prelayout_pending_workspace()

    transport.create_primary.assert_called_once_with(agent_width=126)
    transport.create_dual.assert_not_called()


def test_startup_prelayout_ignores_empty_dead_and_compact_state(monkeypatch):
    app = _minimal_app()
    app._pending_restore_state = {
        "workspace": {
            "layout": "side-by-side",
            "slots": {
                "primary": {"kind": "agent", "tmux": "cc-dead"},
                "secondary": {"kind": "empty"},
            },
        },
    }
    transport = MagicMock()
    app._display_transport_manager = transport
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.session_exists", lambda _name: False)

    assert not app._prelayout_pending_workspace()
    transport.create_dual.assert_not_called()

    app._pending_restore_state["workspace"]["slots"]["primary"] = {
        "kind": "preview",
        "session": "history",
    }
    app._agent_workspace().presentation = WorkspacePresentation.COMPACT
    assert not app._prelayout_pending_workspace()
    transport.create_dual.assert_not_called()


def test_failed_startup_restore_closes_only_owned_empty_skeleton():
    app = _minimal_app()
    workspace = app._agent_workspace()
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    workspace.target_slot_key = AgentWorkspace.SECONDARY
    app._prelayout_created = True
    app._set_railmux_focus = MagicMock()
    transport = MagicMock()
    app._display_transport_manager = transport

    app._finish_prelayout_restore(False)

    transport.close_all.assert_called_once_with()
    assert workspace.layout is WorkspaceLayout.SINGLE
    assert workspace.target_slot_key == AgentWorkspace.PRIMARY
    assert not app._prelayout_created


def test_partial_startup_restore_keeps_prelayout_surface():
    app = _minimal_app()
    workspace = app._agent_workspace()
    workspace.primary.agent_tmux_name = "cc-primary"
    app._prelayout_created = True
    transport = MagicMock()
    app._display_transport_manager = transport

    app._finish_prelayout_restore(False)

    transport.close_all.assert_not_called()
    assert not app._prelayout_created


def test_restore_workspace_keeps_dual_layout_when_secondary_content_fails(monkeypatch):
    app = _minimal_app()
    workspace = app._agent_workspace()
    transport = MagicMock()
    transport.displayed_real_pane.return_value = None
    app._display_transport_manager = transport
    app._agent_region_size = MagicMock(return_value=(200, 40))
    app._layout_fits = MagicMock(return_value=True)
    app._set_railmux_focus = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_tmux_bindings = MagicMock()

    def create_primary():
        workspace.primary.pane_id = "%2"
        return True

    def create_secondary(layout):
        workspace.layout = layout
        workspace.secondary.pane_id = "%3"
        return True

    transport.create_primary.side_effect = create_primary
    transport.create_secondary.side_effect = create_secondary
    transport.reset_slot.return_value = True
    app._restore_workspace_slot = MagicMock(return_value=False)
    selected = []
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane",
        lambda pane: selected.append(pane) or True,
    )
    app._railmux_pane_id = "%1"
    saved = {
        "layout": "stacked",
        "target": "secondary",
        "focus": "sidebar",
        "slots": {
            "primary": {"kind": "empty"},
            "secondary": {"kind": "agent", "tmux": "cx-missing"},
        },
    }

    assert not app._restore_workspace({}, saved)

    assert workspace.layout is WorkspaceLayout.STACKED
    assert workspace.target_slot_key == AgentWorkspace.SECONDARY
    transport.reset_slot.assert_called_once_with(workspace.secondary)
    assert selected[-1] == "%1"
    app._set_railmux_focus.assert_called_with(True, force_border=True)


def test_restore_workspace_geometry_fallback_remembers_validated_secondary(monkeypatch):
    app = _minimal_app()
    workspace = app._agent_workspace()
    transport = MagicMock()
    transport.displayed_real_pane.return_value = None
    app._display_transport_manager = transport
    app._agent_region_size = MagicMock(return_value=(70, 20))
    app._layout_fits = MagicMock(return_value=False)
    app._set_railmux_focus = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._set_status = MagicMock()
    app._running["secondary-session"] = _Running(
        key="secondary-session",
        tmux_name="cx-secondary",
        label="secondary",
        session_type="codex",
    )

    def create_primary():
        workspace.primary.pane_id = "%2"
        return True

    transport.create_primary.side_effect = create_primary
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.session_exists", lambda _name: True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_topology",
        lambda _name: MagicMock(session_id="$secondary"),
    )
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)
    saved = {
        "layout": "stacked",
        "target": "secondary",
        "focus": "sidebar",
        "slots": {
            "primary": {"kind": "empty"},
            "secondary": {
                "kind": "agent",
                "tmux": "cx-secondary",
                "session": "secondary-session",
                "mode": "codex",
            },
        },
    }

    assert not app._restore_workspace({}, saved)

    assert workspace.layout is WorkspaceLayout.SINGLE
    assert workspace.collapsed_secondary_agent == "cx-secondary"
    assert app._adaptive_single_state == {
        "workspace": saved,
        "profile": LayoutProfile("always", "stacked", 200, 500),
        "visible": AgentWorkspace.PRIMARY,
    }
    assert app._adaptive_single_running_guards == {
        "cx-secondary": (
            app._running["secondary-session"],
            "$secondary",
        ),
    }
    transport.create_secondary.assert_not_called()


def test_restore_workspace_rebuilds_dual_directly_in_compact_mode(monkeypatch):
    app = _minimal_app()
    workspace = app._agent_workspace()
    workspace.presentation = WorkspacePresentation.COMPACT
    transport = MagicMock()
    transport.displayed_real_pane.return_value = None
    app._display_transport_manager = transport
    app._agent_region_size = MagicMock(return_value=(30, 10))
    app._layout_fits = MagicMock(return_value=False)
    app._set_railmux_focus = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._apply_layout_profile = MagicMock(return_value=False)

    def create_primary():
        workspace.primary.pane_id = "%2"
        return True

    def create_secondary(layout):
        workspace.layout = layout
        workspace.secondary.pane_id = "%3"
        return True

    transport.create_primary.side_effect = create_primary
    transport.create_secondary.side_effect = create_secondary
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)
    app._railmux_pane_id = "%1"
    saved = {
        "layout": "side-by-side",
        "target": "secondary",
        "focus": "sidebar",
        "slots": {
            "primary": {"kind": "empty"},
            "secondary": {"kind": "empty"},
        },
    }

    assert app._restore_workspace({}, saved)

    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.secondary.pane_id == "%3"
    assert app._pre_compact_layout_profile == LayoutProfile(
        "always", "side-by-side", 200, 500
    )
    app._layout_fits.assert_not_called()
    transport.create_secondary.assert_called_once_with(WorkspaceLayout.SIDE_BY_SIDE)


def test_restore_workspace_keeps_layout_when_primary_content_falls_back_empty(
    monkeypatch,
):
    app = _minimal_app()
    workspace = app._agent_workspace()
    transport = MagicMock()
    app._display_transport_manager = transport
    app._agent_region_size = MagicMock(return_value=(200, 40))
    app._layout_fits = MagicMock(return_value=True)
    app._set_railmux_focus = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._restore_agent_target = MagicMock(return_value=False)

    def create_primary():
        workspace.primary.pane_id = "%2"
        return True

    def create_secondary(layout):
        workspace.layout = layout
        workspace.secondary.pane_id = "%3"
        return True

    transport.create_primary.side_effect = create_primary
    transport.create_secondary.side_effect = create_secondary
    app._restore_workspace_slot = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)
    saved = {
        "layout": "side-by-side",
        "target": "secondary",
        "focus": "sidebar",
        "slots": {
            "primary": {"kind": "agent", "tmux": "cc-missing"},
            "secondary": {"kind": "empty"},
        },
    }

    assert not app._restore_workspace({}, saved)

    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.primary.pane_id == "%2"
    assert workspace.target_slot_key == AgentWorkspace.SECONDARY
    transport.create_primary.assert_called_once_with()


def test_workspace_preview_drops_unrepresented_agent_rollback(monkeypatch):
    app = _minimal_app()
    slot = app._agent_workspace().secondary
    app._restore_preview_target = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.session_exists", lambda _name: True)

    assert app._restore_workspace_slot(
        slot,
        {
            "kind": "preview",
            "session": "history",
            "mode": "codex",
            "restore": {"kind": "agent", "tmux": "cx-reused-name"},
        },
    )

    assert slot.restore_state == SlotRestoreState("empty")


def test_save_state_persists_codex_mode(tmp_path, monkeypatch):
    """Restart records the stable provider registry key."""
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: tmp_path / "state.json")
    )
    app = _minimal_app(selected_project=_project("myproj"))
    app._codex_mode = True
    app._save_state()
    data = app._load_state()
    assert data["mode"] == "codex"


def test_save_state_persists_real_binding_with_placeholder_tmux_name(
    tmp_path, monkeypatch
):
    """Resolution re-keys the registry but intentionally keeps cx-new---*.

    The soft-restart state must retain that otherwise-invisible association.
    """
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: tmp_path / "state.json")
    )
    project = _project("codex-proj")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = _minimal_app(selected_project=project)
    app._running[session_id] = _Running(
        key=session_id,
        tmux_name="cx-new---abcdef-1",
        label="codex-proj/resolved",
        project=project,
        session_type="codex",
    )

    app._save_state()

    data = app._load_state()
    assert data["running_bindings_version"] == 1
    assert data["running_bindings"] == [
        {
            "key": session_id,
            "tmux_name": "cx-new---abcdef-1",
            "session_type": "codex",
            "cwd": str(project.real_path),
        }
    ]


def test_save_state_persists_unresolved_placeholder_context(tmp_path, monkeypatch):
    """macOS needs launch context to resume safe heuristic resolution."""
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: tmp_path / "state.json")
    )
    project = _project("codex-proj")
    key = "__new__-abcdef-1"
    app = _minimal_app(selected_project=project)
    app._running[key] = _Running(
        key=key,
        tmux_name="cx-new---abcdef-1",
        label="codex-proj/(new)",
        project=project,
        placeholder_path=project.real_path,
        created_at=1234.5,
        pre_launch_ids=frozenset({"old-b", "old-a"}),
        session_type="codex",
    )

    app._save_state()

    binding = app._load_state()["running_bindings"][0]
    assert binding["key"] == key
    assert binding["created_at"] == 1234.5
    assert binding["pre_launch_ids"] == ["old-a", "old-b"]


def test_load_state_without_codex_mode_defaults_falsy(tmp_path, monkeypatch):
    """Ownerless legacy state migrates view-only and defaults to Claude."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"project": "-tmp-myproj", "right_kind": "empty"}))
    app = _minimal_app()
    monkeypatch.setattr(restart_state, "legacy_state_path", lambda: p)

    data = app._load_state()

    assert data == {"mode": "claude", "project": "-tmp-myproj"}
    assert p.exists()  # ownerless source remains available for manual cleanup


def test_enter_codex_mode_on_restore_applies_filter(monkeypatch):
    """_enter_codex_mode_on_restore flips the mode, loads the Codex filter and
    repaints the Projects pane with the Codex-visible set."""
    app = App.__new__(App)
    app._codex_mode = False
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {Path("/tmp/myproj"): 2}
    app._projects_pane = MagicMock()
    monkeypatch.setattr(app, "_visible_projects", lambda *a, **k: ["visible-proj"])

    app._enter_codex_mode_on_restore()

    assert app._codex_mode is True
    assert app._codex_project_filter == {Path("/tmp/myproj"): 2}
    app._projects_pane.set_projects.assert_called_once_with(["visible-proj"])


def test_toggle_codex_mode_round_trip_uses_cached_snapshot(monkeypatch):
    """A rapid Claude-Codex-Claude round trip never scans NFS on the UI path.

    It paints the warm snapshot immediately and schedules one background refresh.
    """
    import time as _time

    proj = _project("myproj")
    app = App.__new__(App)
    app._codex_mode = False
    app._selected_project = None
    app._project_snapshot = [proj]
    snapshot_at = _time.monotonic()
    app._project_snapshot_at = snapshot_at
    app._running = {}
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._projects_pane = MagicMock()
    app._sessions_pane = MagicMock()
    app._codex_index = MagicMock()
    app._codex_project_filter = {proj.real_path: 1}
    app._codex_index.sessions_for_cwd.return_value = []
    app._session_cache = MagicMock()
    app._session_cache.list_sessions.return_value = []
    app._claude_home = Path.home() / ".claude"
    schedule_refresh = MagicMock()
    monkeypatch.setattr(app, "_apply_tmux_bar", lambda *a, **k: None)
    monkeypatch.setattr(app, "_set_status", lambda *a, **k: None)
    monkeypatch.setattr(app, "_schedule_mode_data_refresh", schedule_refresh)

    with patch(
        "railmux.ui.app.list_projects",
        side_effect=AssertionError("toggle forced an NFS rescan"),
    ):
        app._toggle_codex_mode()
        app._toggle_codex_mode()

    assert app._codex_mode is False
    schedule_refresh.assert_called_once_with()
    assert app._project_snapshot_at == snapshot_at


def test_load_state_missing_file_returns_none():
    app = _minimal_app()
    with patch.object(
        App, "_state_path", return_value=Path("/tmp/railmux-nonexistent.json")
    ):
        assert app._load_state() is None


def test_load_state_invalid_json_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json")
    app = _minimal_app()
    with patch.object(App, "_state_path", return_value=p):
        assert app._load_state() is None


def test_load_state_rejects_non_object_json(tmp_path):
    p = tmp_path / "bad-shape.json"
    p.write_text("[]")
    app = _minimal_app()
    with patch.object(App, "_state_path", return_value=p):
        assert app._load_state() is None


def test_newer_state_schemas_are_ignored_and_never_overwritten(tmp_path, monkeypatch):
    local = tmp_path / "local.json"
    portable = tmp_path / "portable.json"
    newer_portable = {"schema_version": 2, "kind": "portable"}
    newer_local = {"schema_version": 2, "kind": "instance"}
    portable.write_text(json.dumps(newer_portable))
    local.write_text(json.dumps(newer_local))
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: local))
    monkeypatch.setattr(App, "_portable_state_path", staticmethod(lambda: portable))
    app = _minimal_app()

    assert app._load_state() is None
    app._save_state()

    assert json.loads(portable.read_text()) == newer_portable
    assert json.loads(local.read_text()) == newer_local


# ── _discover_orphans parsing ────────────────────────────────────────────
