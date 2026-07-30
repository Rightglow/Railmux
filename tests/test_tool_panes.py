"""Fail-closed state contracts for managed per-agent tool panes."""

from __future__ import annotations

import json

from railmux.tool_panes import (
    PaneRef,
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
