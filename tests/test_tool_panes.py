"""Fail-closed state contracts for managed per-agent tool panes."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from railmux.tool_panes import (
    PaneRef,
    ToolPaneManager,
    ToolState,
    decode_state,
    is_tool_pane_marker,
)


def _ref(pane: int, pid: int, session: int = 4, window: int = 2) -> PaneRef:
    return PaneRef(f"%{pane}", pid, f"${session}", f"@{window}")


def test_tool_state_round_trips_exact_process_and_tmux_identities():
    state = ToolState(
        "primary",
        "$4",
        _ref(8, 108),
        _ref(9, 109),
        _ref(12, 112, session=7, window=5),
        "viewer",
        "railmux-tool-4-primary",
        "$7",
        _ref(13, 113, session=7, window=5),
    )

    assert decode_state(
        state.to_json(),
        slot="primary",
        outer_session_id="$4",
    ) == state
    assert decode_state(
        state.to_json(),
        slot="secondary",
        outer_session_id="$4",
    ) is None


def test_tool_state_rejects_malformed_or_reused_identity_shapes():
    state = ToolState(
        "primary",
        "$4",
        _ref(8, 108),
        _ref(9, 109),
        None,
        "shell",
        None,
        None,
        None,
    )
    value = json.loads(state.to_json())
    value["shell"]["pane_pid"] = True
    assert decode_state(
        json.dumps(value),
        slot="primary",
        outer_session_id="$4",
    ) is None
    value = json.loads(state.to_json())
    value["parking_session"] = "unexpected"
    assert decode_state(
        json.dumps(value),
        slot="primary",
        outer_session_id="$4",
    ) is None


def test_tool_pane_marker_is_scoped_to_one_outer_session():
    marker = json.dumps(
        {
            "version": 1,
            "outer_session_id": "$4",
            "slot": "secondary",
            "kind": "viewer",
        }
    )

    assert is_tool_pane_marker(marker, outer_session_id="$4")
    assert not is_tool_pane_marker(marker, outer_session_id="$5")
    assert not is_tool_pane_marker(
        marker[:-1] + ', "extra": true}',
        outer_session_id="$4",
    )
    assert not is_tool_pane_marker("not-json", outer_session_id="$4")


def test_tool_split_is_orthogonal_to_workspace_layout():
    manager = object.__new__(ToolPaneManager)
    manager._show_option = MagicMock(return_value="stacked")
    manager._output = MagicMock(return_value="%9")
    manager._pane_ref = MagicMock(return_value=PaneRef("%9", 99, "$4", "@1"))
    owner = PaneRef("%2", 22, "$4", "@1")

    result = manager._split_tool(owner, "sleep 1")

    assert result == PaneRef("%9", 99, "$4", "@1")
    assert manager._output.call_args.args[:3] == (
        "split-window", "-h", "-d",
    )

    manager._show_option.return_value = "side-by-side"
    manager._split_tool(owner, "sleep 1")
    assert manager._output.call_args.args[:3] == (
        "split-window", "-v", "-d",
    )


def test_tmux_27_reports_managed_tools_as_an_explicit_version_limit(tmp_path):
    manager = object.__new__(ToolPaneManager)
    manager._output = MagicMock(return_value="2.7")

    shell = manager.open_shell("primary", "%8", tmp_path)
    viewer = manager.open_viewer("primary", "%8", str(tmp_path / "a.py"))

    assert not shell.ok and shell.level == "warning"
    assert not viewer.ok and viewer.level == "warning"
    assert "tmux 3.0 or newer" in shell.message
    assert viewer.message == shell.message


def test_owner_slot_falls_back_to_existing_selection_marker(monkeypatch):
    manager = object.__new__(ToolPaneManager)
    manager.outer_session_id = "$4"
    clicked = _ref(8, 108)
    controller = _ref(9, 109)
    refs = {"%8": clicked, "%9": controller}
    monkeypatch.setattr(manager, "_pane_ref", refs.get)
    monkeypatch.setattr(manager, "_show_option", lambda _name: None)
    monkeypatch.setattr(manager, "_outer_window_ids", lambda: frozenset({"@2"}))
    monkeypatch.setattr(
        manager,
        "_output",
        lambda *args, **_kwargs: (
            "%9:secondary"
            if args[:4] == ("show-options", "-p", "-v", "-t")
            and args[4] == "%8"
            else "%9"
            if args[:4] == ("show-window-options", "-v", "-t", "@2")
            else None
        ),
    )

    assert manager.slot_for_owner("%8") == "secondary"


def test_owner_slot_rejects_selection_marker_outside_outer_window(monkeypatch):
    manager = object.__new__(ToolPaneManager)
    manager.outer_session_id = "$4"
    refs = {
        "%8": _ref(8, 108),
        "%9": _ref(9, 109, window=7),
    }
    monkeypatch.setattr(manager, "_pane_ref", refs.get)
    monkeypatch.setattr(manager, "_show_option", lambda _name: None)
    monkeypatch.setattr(manager, "_outer_window_ids", lambda: frozenset({"@2"}))
    monkeypatch.setattr(
        manager,
        "_output",
        lambda *args, **_kwargs: (
            "%9:primary" if args[0] == "show-options" else "%9"
        ),
    )

    assert manager.slot_for_owner("%8") is None
