"""Keep one agent pane visually stable while text is selected in the other."""
from __future__ import annotations

import argparse
from dataclasses import dataclass

from railmux import tmux_ctl
from railmux.ui.workspace import (
    AgentSlot,
    AgentWorkspace,
    DisplayTransportKind,
    WorkspaceLayout,
)


@dataclass(frozen=True)
class _Projection:
    pane_id: str
    selection_key: str
    peer_pane_id: str


def reconcile_pane(pane_id: str) -> bool:
    """Apply one pane-mode transition emitted by the shared tmux hook."""
    state = tmux_ctl.selection_pane_state(pane_id)
    if (state is None or state.frozen_by is not None
            or state.selection_key is None or state.peer_pane_id is None):
        return False
    if state.in_mode:
        return tmux_ctl.freeze_selection_peer(
            state.peer_pane_id, state.selection_key)

    states = tmux_ctl.list_selection_pane_states()
    if states is None:
        return False
    if any(
        candidate.in_mode
        and candidate.frozen_by is None
        and candidate.selection_key == state.selection_key
        for candidate in states
    ):
        return True
    return tmux_ctl.release_selection_peer(
        state.peer_pane_id, state.selection_key)


class SelectionIsolationManager:
    """Project and reconcile pane-local markers for one Railmux workspace."""

    def __init__(self, owner_pane_id: str) -> None:
        self._owner_pane_id = owner_pane_id
        self._projected: dict[str, _Projection] = {}

    def sync(self, workspace: AgentWorkspace, *, enabled: bool) -> None:
        desired = self._desired_projections(workspace) if enabled else {}
        if desired == self._projected:
            return
        self.release_all()
        for pane_id, old in self._projected.items():
            if desired.get(pane_id) == old:
                continue
            tmux_ctl.unset_pane_user_option_if_value(
                pane_id, tmux_ctl.RAILMUX_SELECTION_KEY_OPTION,
                old.selection_key)
            tmux_ctl.unset_pane_user_option_if_value(
                pane_id, tmux_ctl.RAILMUX_SELECTION_PEER_OPTION,
                old.peer_pane_id)
        applied: dict[str, _Projection] = {}
        for pane_id, projection in desired.items():
            if (tmux_ctl.set_pane_user_option(
                    pane_id, tmux_ctl.RAILMUX_SELECTION_KEY_OPTION,
                    projection.selection_key)
                    and tmux_ctl.set_pane_user_option(
                        pane_id, tmux_ctl.RAILMUX_SELECTION_PEER_OPTION,
                        projection.peer_pane_id)):
                applied[pane_id] = projection
            else:
                tmux_ctl.unset_pane_user_option_if_value(
                    pane_id, tmux_ctl.RAILMUX_SELECTION_KEY_OPTION,
                    projection.selection_key)
                tmux_ctl.unset_pane_user_option_if_value(
                    pane_id, tmux_ctl.RAILMUX_SELECTION_PEER_OPTION,
                    projection.peer_pane_id)
        self._projected = applied

    def maintain(self) -> None:
        """Heal a missed hook and release exact stale freezes after crashes."""
        if tmux_ctl.tmux_version() < (3, 0):
            return
        states = tmux_ctl.list_selection_pane_states()
        if states is None:
            return
        active_keys = {
            state.selection_key
            for state in states
            if state.in_mode and state.frozen_by is None
            and state.selection_key is not None
        }
        for state in states:
            if (state.pane_id in self._projected and state.in_mode
                    and state.frozen_by is None):
                projection = self._projected[state.pane_id]
                tmux_ctl.freeze_selection_peer(
                    projection.peer_pane_id, projection.selection_key)
            if (state.frozen_by is not None
                    and self._owns_key(state.frozen_by)
                    and state.frozen_by not in active_keys):
                tmux_ctl.release_selection_peer(
                    state.pane_id, state.frozen_by)

    def release_all(self) -> None:
        """Resume every peer frozen by this manager's exact selection keys."""
        for projection in set(self._projected.values()):
            tmux_ctl.release_selection_peer(
                projection.peer_pane_id, projection.selection_key)

    def close(self) -> None:
        self.sync(AgentWorkspace(), enabled=False)

    def _owns_key(self, selection_key: str) -> bool:
        return selection_key in {
            f"{self._owner_pane_id}:{AgentWorkspace.PRIMARY}",
            f"{self._owner_pane_id}:{AgentWorkspace.SECONDARY}",
        }

    def _desired_projections(
        self, workspace: AgentWorkspace,
    ) -> dict[str, _Projection]:
        if (tmux_ctl.tmux_version() < (3, 0)
                or workspace.layout is WorkspaceLayout.SINGLE):
            return {}
        primary = workspace.primary
        secondary = workspace.secondary
        if primary.pane_id is None or secondary.pane_id is None:
            return {}
        desired: dict[str, _Projection] = {}
        for slot, peer in ((primary, secondary.pane_id),
                           (secondary, primary.pane_id)):
            key = f"{self._owner_pane_id}:{slot.key}"
            for pane_id in self._selectable_panes(slot):
                desired[pane_id] = _Projection(pane_id, key, peer)
        return desired

    @staticmethod
    def _selectable_panes(slot: AgentSlot) -> tuple[str, ...]:
        if slot.pane_id is None:
            return ()
        panes = [slot.pane_id]
        if (slot.transport_kind is DisplayTransportKind.NESTED
                and slot.agent_tmux_name is not None):
            inner = tmux_ctl.session_pane_id(slot.agent_tmux_name)
            if inner is not None and inner not in panes:
                panes.append(inner)
        return tuple(panes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--pane", required=True)
    # Present so the installed hook carries an ownership marker. Pane-local
    # exact markers, not this value, authorize every mutation below.
    parser.add_argument("--lease-token", required=True)
    args = parser.parse_args(argv)
    if not tmux_ctl._PANE_ID_RE.fullmatch(args.pane):
        return 2
    reconcile_pane(args.pane)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
