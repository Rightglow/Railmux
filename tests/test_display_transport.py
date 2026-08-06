"""Transactional de-nested display transport (tmux is modeled in memory)."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from railmux import tmux_ctl
from railmux import tmux_server
from railmux import display_transport as transport_mod
from railmux.display_transport import (
    AgentDisplayTransport,
    recover_interrupted_swaps,
)
from railmux.ui.workspace import (
    AgentWorkspace,
    DisplayTransportKind,
    WorkspaceLayout,
)


class FakeTmux:
    def __init__(self, monkeypatch):
        self.sessions = {
            "railmux": {"id": "$1", "windows": {"@1"}, "attached": 1},
            "agent-a": {"id": "$2", "windows": {"@2"}, "attached": 0},
            "agent-b": {"id": "$3", "windows": {"@3"}, "attached": 0},
        }
        self.panes = {
            "%0": tmux_ctl.PaneIdentity(
                "%0", 100, "railmux", "$1", "@1", False, 40, 30),
            "%2": tmux_ctl.PaneIdentity(
                "%2", 202, "agent-a", "$2", "@2", False, 80, 24),
            "%3": tmux_ctl.PaneIdentity(
                "%3", 303, "agent-b", "$3", "@3", False, 80, 24),
        }
        self.window_options: dict[tuple[str, str], str] = {}
        self.pane_options: dict[tuple[str, str], str] = {}
        self.session_options: dict[tuple[str, str], str] = {}
        self.next_pane = 10
        self.next_session = 10
        self.swap_calls: list[tuple[str, str]] = []
        self.fit_calls: list[tuple[str, str]] = []
        self.killed_sessions: list[str] = []
        self.fail_marker_window: str | None = None
        self.fail_swap_at: int | None = None
        self.fail_respawn = False
        self.exit_after_respawn = False
        self.dead_after_respawn = False
        self.dead_panes: set[str] = set()
        self.respawned: list[tuple[str, str]] = []
        self.split_commands: list[str] = []
        self.split_kwargs: list[dict] = []
        self.dual_calls: list[tuple[str, str, str, int, int]] = []
        self.fast_enabled = False
        self.snapshot_calls = 0
        self.command_batches: list[tuple[tuple[str, ...], ...]] = []
        self._patch(monkeypatch)

    def _patch(self, monkeypatch):
        names = {
            "tmux_version": lambda: (3, 4),
            "pane_alive": lambda pane: pane in self.panes,
            "pane_process_alive": lambda pane: (
                pane in self.panes and pane not in self.dead_panes),
            "pane_identity": lambda pane: self.panes.get(pane),
            "session_exists": lambda name: name in self.sessions,
            "session_topology": self.session_topology,
            "session_has_window": lambda name, window: (
                name in self.sessions and window in self.sessions[name]["windows"]),
            "split_window_h": self.split_window_h,
            "split_window_v": self.split_window_h,
            "create_dual_pane_layout": self.create_dual_pane_layout,
            "respawn_pane": self.respawn_pane,
            "fit_session_to_pane": self.fit_session_to_pane,
            "wait_session_detached": lambda name, timeout=1.0: (
                self.sessions[name]["attached"] == 0),
            "create_grouped_session": self.create_grouped_session,
            "set_session_user_option": self.set_session_user_option,
            "show_session_user_option": self.show_session_user_option,
            "set_window_user_option": self.set_window_user_option,
            "show_window_user_option": self.show_window_user_option,
            "set_pane_user_option": self.set_pane_user_option,
            "swap_panes": self.swap_panes,
            "kill_session": self.kill_session,
            "kill_pane": self.kill_pane,
            "new_detached_session": self.new_detached_session,
            "select_pane": lambda _pane: True,
            "session_ids": lambda: frozenset(
                str(session["id"]) for session in self.sessions.values()),
            "list_window_user_options": self.list_window_user_options,
            "tmux_state_snapshot": self.tmux_state_snapshot,
            "run_command_queue": self.run_command_queue,
            "run_guarded_window_transaction": (
                self.run_guarded_window_transaction),
        }
        for name, value in names.items():
            monkeypatch.setattr(transport_mod.tmux_ctl, name, value)

    def _session_for_window(self, window: str) -> tuple[str, str]:
        for name, session in self.sessions.items():
            if window in session["windows"] and name != "railmux-keep-1":
                return name, str(session["id"])
        name, session = next(
            (name, session) for name, session in self.sessions.items()
            if window in session["windows"])
        return name, str(session["id"])

    def _relocate(self, pane_id: str, window: str) -> None:
        name, session_id = self._session_for_window(window)
        self.panes[pane_id] = replace(
            self.panes[pane_id], window_id=window,
            session_name=name, session_id=session_id)

    def session_topology(self, name: str):
        session = self.sessions.get(name)
        if session is None:
            return None
        windows = tuple(sorted(session["windows"]))
        panes = tuple(
            pane for pane in self.panes.values()
            if pane.window_id in session["windows"]
        )
        return tmux_ctl.SessionTopology(
            name, str(session["id"]), int(session["attached"]), windows, panes)

    def split_window_h(self, command, **kwargs):
        self.split_commands.append(command)
        self.split_kwargs.append(kwargs)
        pane_id = f"%{self.next_pane}"
        self.next_pane += 1
        self.panes[pane_id] = tmux_ctl.PaneIdentity(
            pane_id, 1000 + self.next_pane, "railmux", "$1", "@1",
            False, 80, 30)
        return pane_id

    def create_dual_pane_layout(
        self,
        primary_command,
        secondary_command,
        *,
        target,
        layout,
        agent_width,
        secondary_extent,
    ):
        self.dual_calls.append((
            primary_command,
            secondary_command,
            layout,
            agent_width,
            secondary_extent,
        ))
        assert target == "%0"
        primary = self.split_window_h(primary_command)
        secondary = self.split_window_h(secondary_command)
        return primary, secondary

    def respawn_pane(self, pane, command):
        if pane not in self.panes or self.fail_respawn:
            return False
        self.respawned.append((pane, command))
        if self.exit_after_respawn:
            self.panes.pop(pane, None)
        elif self.dead_after_respawn:
            self.dead_panes.add(pane)
        return True

    def create_grouped_session(self, name, target):
        if name in self.sessions or target not in self.sessions:
            return False
        self.sessions[name] = {
            "id": f"${self.next_session}",
            "windows": set(self.sessions[target]["windows"]),
            "attached": 0,
        }
        self.next_session += 1
        return True

    def set_session_user_option(self, session, name, value):
        if session not in self.sessions:
            return False
        key = (session, name)
        if value is None:
            self.session_options.pop(key, None)
        else:
            self.session_options[key] = value
        return True

    def show_session_user_option(self, session, name):
        return self.session_options.get((session, name))

    def set_window_user_option(self, window, name, value):
        if window == self.fail_marker_window:
            return False
        key = (window, name)
        if value is None:
            self.window_options.pop(key, None)
        else:
            self.window_options[key] = value
        return True

    def show_window_user_option(self, window, name):
        return self.window_options.get((window, name))

    def set_pane_user_option(self, pane, name, value):
        if pane not in self.panes:
            return False
        key = (pane, name)
        if value is None:
            self.pane_options.pop(key, None)
        else:
            self.pane_options[key] = value
        return True

    def swap_panes(self, source, target):
        self.swap_calls.append((source, target))
        if self.fail_swap_at == len(self.swap_calls):
            return False
        if source not in self.panes or target not in self.panes:
            return False
        source_window = self.panes[source].window_id
        target_window = self.panes[target].window_id
        self._relocate(source, target_window)
        self._relocate(target, source_window)
        return True

    def fit_session_to_pane(self, session, pane):
        self.fit_calls.append((session, pane))
        return True

    def kill_session(self, name):
        session = self.sessions.pop(name, None)
        if session is None:
            return False
        self.killed_sessions.append(name)
        still_owned = set().union(*(
            value["windows"] for value in self.sessions.values()))
        for pane_id in [
            pane_id for pane_id, pane in self.panes.items()
            if pane.window_id in session["windows"]
            and pane.window_id not in still_owned
        ]:
            del self.panes[pane_id]
        return True

    def kill_pane(self, pane):
        identity = self.panes.pop(pane, None)
        self.dead_panes.discard(pane)
        if identity is None:
            return False
        for name in [
            name for name, session in self.sessions.items()
            if identity.window_id in session["windows"]
            and not any(
                other.window_id == identity.window_id
                for other in self.panes.values())
        ]:
            del self.sessions[name]
        return True

    def new_detached_session(self, name, _command, env=None):
        if name in self.sessions:
            return False, "already exists"
        window = f"@{self.next_session}"
        pane = f"%{self.next_pane}"
        session_id = f"${self.next_session}"
        self.next_session += 1
        self.next_pane += 1
        self.sessions[name] = {
            "id": session_id, "windows": {window}, "attached": 0}
        self.panes[pane] = tmux_ctl.PaneIdentity(
            pane, 1000 + self.next_pane, name, session_id, window,
            False, 80, 24)
        return True, None

    def list_window_user_options(self, names):
        windows = sorted({
            window for session in self.sessions.values()
            for window in session["windows"]
        })
        return [tuple(
            [window]
            + [self.window_options.get((window, name), "") for name in names]
        ) for window in windows]

    def tmux_state_snapshot(self, names=()):
        if not self.fast_enabled:
            return None
        self.snapshot_calls += 1
        topologies = tuple(
            self.session_topology(name) for name in self.sessions
        )
        return tmux_ctl.TmuxStateSnapshot(
            topologies=tuple(item for item in topologies if item is not None),
            panes=tuple(self.panes.values()),
            window_options=tuple(
                (window, name, self.window_options.get((window, name)))
                for window in sorted({
                    window for session in self.sessions.values()
                    for window in session["windows"]
                })
                for name in names
            ),
        )

    def run_command_queue(self, commands):
        self.command_batches.append(commands)
        for command in commands:
            name, *args = command
            if name == "set-window-option":
                target = args[args.index("-t") + 1]
                if "-u" in args:
                    marker = args[-1]
                    if not self.set_window_user_option(target, marker, None):
                        return False
                elif not self.set_window_user_option(target, args[-2], args[-1]):
                    return False
            elif name == "set-option":
                target = args[args.index("-t") + 1]
                option = args[-1]
                value = None if "-u" in args else args[-1]
                if not self.set_pane_user_option(target, option, value):
                    return False
            elif name == "resize-window":
                continue
            elif name == "swap-pane":
                source = args[args.index("-s") + 1]
                target = args[args.index("-t") + 1]
                if not self.swap_panes(source, target):
                    return False
            else:
                raise AssertionError(f"unexpected command: {command}")
        return True

    def run_guarded_window_transaction(self, guards, commands):
        for window, marker, token in guards:
            if token not in self.window_options.get((window, marker), ""):
                return False
        return self.run_command_queue(commands)


@pytest.fixture
def rig(monkeypatch):
    fake = FakeTmux(monkeypatch)
    monkeypatch.setattr(
        transport_mod.tmux_server,
        "current_target",
        lambda **_kwargs: tmux_server.TmuxServerTarget("/tmp/dedicated", 77),
    )
    monkeypatch.setattr(
        transport_mod.tmux_server,
        "target_is_live",
        lambda target, **_kwargs: target
        == tmux_server.TmuxServerTarget("/tmp/dedicated", 77),
    )
    monkeypatch.setattr(transport_mod.time, "sleep", lambda _seconds: None)
    workspace = AgentWorkspace()
    manager = AgentDisplayTransport(
        workspace, "swap", auto_launched=True,
        outer_session_name="railmux", outer_session_id="$1",
        owner_pane_id="%0")
    return fake, workspace, manager


def test_successful_swap_out_and_home(rig):
    fake, workspace, manager = rig
    outcome = manager.attach(workspace.primary, "agent-a")

    assert outcome.ok and outcome.kind == DisplayTransportKind.SWAP
    assert workspace.primary.pane_id == "%2"
    assert fake.panes["%2"].window_id == "@1"
    placeholder = workspace.primary.swap_state.placeholder_pane_id
    assert fake.panes[placeholder].window_id == "@2"
    assert fake.panes["%2"].pane_pid == 202
    assert fake.window_options

    assert manager.return_home(workspace.primary)
    assert fake.fit_calls == [("agent-a", "%2")]
    assert fake.panes["%2"].window_id == "@2"
    assert workspace.primary.pane_id == placeholder
    assert workspace.primary.swap_state is None
    assert not fake.window_options
    assert "railmux-keep-1" in fake.killed_sessions


def test_exact_same_swap_target_is_one_snapshot_noop(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    swap_count = len(fake.swap_calls)
    respawn_count = len(fake.respawned)
    fake.fast_enabled = True

    outcome = manager.attach(workspace.primary, "agent-a")

    assert outcome.ok and outcome.display_stable and outcome.target_unchanged
    assert fake.snapshot_calls == 1
    assert len(fake.swap_calls) == swap_count
    assert len(fake.respawned) == respawn_count
    assert fake.command_batches == []


def test_fast_swap_switch_keeps_journal_and_bounded_command_budget(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    placeholder = workspace.primary.swap_state.placeholder_pane_id
    respawn_count = len(fake.respawned)
    fake.fast_enabled = True

    outcome = manager.attach(workspace.primary, "agent-b")

    assert outcome.ok and outcome.display_stable
    assert not outcome.target_unchanged
    assert fake.snapshot_calls == 3
    assert len(fake.command_batches) == 3
    assert len(fake.respawned) == respawn_count
    assert fake.panes["%2"].window_id == "@2"
    assert fake.panes["%3"].window_id == "@1"
    assert fake.panes[placeholder].window_id == "@3"
    state = workspace.primary.swap_state
    assert state is not None and state.agent_tmux_name == "agent-b"
    for window in (state.home_window_id, state.display_window_id):
        assert json.loads(fake.window_options[
            (window, "@railmux_swap_primary")
        ])["phase"] == "displayed"


def test_fast_switch_never_clears_concurrently_replaced_marker(
    rig, monkeypatch,
):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    old = workspace.primary.swap_state
    assert old is not None
    fake.fast_enabled = True
    original = fake.run_command_queue
    calls = 0

    def replace_after_return(commands):
        nonlocal calls
        calls += 1
        result = original(commands)
        if calls == 1:
            fake.window_options[
                (old.display_window_id, "@railmux_swap_primary")
            ] = "foreign-newer-marker"
        return result

    monkeypatch.setattr(
        transport_mod.tmux_ctl, "run_command_queue", replace_after_return)

    outcome = manager.attach(workspace.primary, "agent-b")

    assert not outcome.ok
    assert fake.panes["%3"].window_id == "@3"
    assert fake.window_options[
        (old.display_window_id, "@railmux_swap_primary")
    ] == "foreign-newer-marker"


def test_fast_switch_rechecks_new_agent_after_old_is_safe_home(
    rig, monkeypatch,
):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    fake.fast_enabled = True
    original = fake.run_command_queue
    calls = 0

    def attach_target_after_return(commands):
        nonlocal calls
        calls += 1
        result = original(commands)
        if calls == 1:
            fake.sessions["agent-b"]["attached"] = 1
        return result

    monkeypatch.setattr(
        transport_mod.tmux_ctl, "run_command_queue", attach_target_after_return)

    outcome = manager.attach(workspace.primary, "agent-b")

    assert outcome.ok and outcome.kind is DisplayTransportKind.NESTED
    assert fake.panes["%3"].window_id == "@3"
    assert workspace.primary.swap_state is None


def test_compact_parking_keeps_agent_home_and_slot_placeholder_stable(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    state = workspace.primary.swap_state
    assert state is not None

    assert manager.park(workspace.primary)
    assert fake.fit_calls == [("agent-a", "%2")]
    assert workspace.primary.display_parked is True
    assert workspace.primary.agent_tmux_name == "agent-a"
    assert workspace.primary.pane_id == state.placeholder_pane_id
    assert fake.panes[state.agent_pane_id].window_id == state.home_window_id
    assert fake.panes[state.placeholder_pane_id].window_id == state.display_window_id
    assert json.loads(
        fake.window_options[
            (state.display_window_id, "@railmux_swap_primary")]
    )["phase"] == "parked"

    assert manager.resume(workspace.primary)
    assert workspace.primary.display_parked is False
    assert workspace.primary.agent_tmux_name == "agent-a"
    assert workspace.primary.pane_id == state.agent_pane_id
    assert fake.panes[state.agent_pane_id].window_id == state.display_window_id
    assert json.loads(
        fake.window_options[
            (state.display_window_id, "@railmux_swap_primary")]
    )["phase"] == "displayed"


def test_compact_park_swap_failure_leaves_agent_displayed(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    fake.fail_swap_at = len(fake.swap_calls) + 1

    assert not manager.park(workspace.primary)
    assert workspace.primary.display_parked is False
    assert workspace.primary.pane_id == "%2"
    assert fake.panes["%2"].window_id == "@1"


def test_return_geometry_failure_does_not_weaken_identity_transaction(
    rig, monkeypatch,
):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    monkeypatch.setattr(
        transport_mod.tmux_ctl, "fit_session_to_pane", lambda *_args: False)

    assert manager.return_home(workspace.primary)
    assert fake.panes["%2"].window_id == "@2"
    assert workspace.primary.swap_state is None


def test_compact_park_marker_commit_failure_rolls_back_display(rig, monkeypatch):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    original = transport_mod._write_marker_pair
    writes = 0

    def fail_park_commit(state, slot_key):
        nonlocal writes
        writes += 1
        return False if writes == 2 else original(state, slot_key)

    monkeypatch.setattr(
        transport_mod, "_write_marker_pair", fail_park_commit)

    assert not manager.park(workspace.primary)
    assert workspace.primary.display_parked is False
    assert workspace.primary.pane_id == "%2"
    assert fake.panes["%2"].window_id == "@1"


def test_compact_resume_swap_failure_keeps_agent_parked_home(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    assert manager.park(workspace.primary)
    fake.fail_swap_at = len(fake.swap_calls) + 1

    assert not manager.resume(workspace.primary)
    assert workspace.primary.display_parked is True
    assert fake.panes["%2"].window_id == "@2"
    assert fake.panes[workspace.primary.pane_id].window_id == "@1"


def test_return_home_accepts_already_parked_agent(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    assert manager.park(workspace.primary)
    placeholder = workspace.primary.pane_id

    assert manager.return_home(workspace.primary)
    assert fake.panes["%2"].window_id == "@2"
    assert workspace.primary.pane_id == placeholder
    assert workspace.primary.agent_tmux_name is None
    assert workspace.primary.swap_state is None
    assert workspace.primary.display_parked is False
    assert not fake.window_options


def test_repeated_a_b_a_switch_keeps_process_identity(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    assert manager.attach(workspace.primary, "agent-b").ok
    assert manager.attach(workspace.primary, "agent-a").ok

    assert fake.panes["%2"].pane_pid == 202
    assert fake.panes["%3"].pane_pid == 303
    assert fake.panes["%2"].window_id == "@1"
    assert fake.panes["%3"].window_id == "@3"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda fake: fake.sessions["agent-a"].update(attached=1),
         "independent client"),
        (lambda fake: fake.sessions["agent-a"]["windows"].add("@9"),
         "single live pane/window"),
    ],
)
def test_unsupported_target_falls_back_nested(rig, mutate, reason):
    fake, workspace, manager = rig
    mutate(fake)
    outcome = manager.attach(workspace.primary, "agent-a")

    assert outcome.ok and outcome.fell_back
    assert outcome.kind == DisplayTransportKind.NESTED
    assert reason in (outcome.reason or "")
    assert workspace.primary.swap_state is None
    assert fake.respawned[-1][1] == (
        "TMUX= exec tmux -S /tmp/dedicated "
        "attach-session -t agent-a"
    )


def test_nested_attach_fails_closed_without_exact_dedicated_target(
        rig, monkeypatch):
    fake, workspace, _manager = rig
    monkeypatch.setattr(
        transport_mod.tmux_server, "current_target", lambda **_kwargs: None)
    manager = AgentDisplayTransport(
        workspace, "nested", auto_launched=True,
        outer_session_name="railmux", outer_session_id="$1",
        owner_pane_id="%0",
    )

    outcome = manager.attach(workspace.primary, "agent-a")

    assert not outcome.ok
    assert "server identity" in (outcome.reason or "")
    assert not fake.split_commands


def test_nested_attach_rejects_child_that_exits_during_startup(rig):
    fake, workspace, _manager = rig
    fake.sessions["agent-a"]["attached"] = 1
    fake.exit_after_respawn = True
    manager = AgentDisplayTransport(
        workspace, "nested", auto_launched=True,
        outer_session_name="railmux", outer_session_id="$1",
        owner_pane_id="%0",
    )

    outcome = manager.attach(workspace.primary, "agent-a")

    assert not outcome.ok
    assert outcome.reason == "nested tmux client exited during startup"
    assert workspace.primary.pane_id is None


def test_nested_attach_kills_dead_remain_on_exit_pane(rig):
    fake, workspace, _manager = rig
    fake.sessions["agent-a"]["attached"] = 1
    fake.dead_after_respawn = True
    manager = AgentDisplayTransport(
        workspace, "nested", auto_launched=True,
        outer_session_name="railmux", outer_session_id="$1",
        owner_pane_id="%0",
    )

    outcome = manager.attach(workspace.primary, "agent-a")

    assert not outcome.ok
    assert outcome.reason == "nested tmux client exited during startup"
    assert workspace.primary.pane_id is None
    assert workspace.primary.agent_tmux_name is None
    assert not fake.dead_panes


def test_old_tmux_falls_back_nested(rig, monkeypatch):
    _fake, workspace, manager = rig
    monkeypatch.setattr(transport_mod.tmux_ctl, "tmux_version", lambda: (2, 6))
    outcome = manager.attach(workspace.primary, "agent-a")
    assert outcome.ok and outcome.fell_back
    assert outcome.kind == DisplayTransportKind.NESTED


def test_legacy_target_always_uses_nested_attach_and_ignore_size(
    rig, monkeypatch,
):
    fake, workspace, manager = rig
    target = tmux_server.TmuxServerTarget("/tmp/legacy socket", 91)
    monkeypatch.setattr(
        transport_mod.tmux_server, "target_has_session",
        lambda candidate, session: candidate == target and session == "$9",
    )

    outcome = manager.attach(
        workspace.primary,
        "agent-a::legacy:91:9",
        server_target=target,
        session_target="$9",
    )

    assert outcome.ok
    assert outcome.kind == DisplayTransportKind.NESTED
    assert not fake.swap_calls
    command = fake.respawned[-1][1]
    assert "tmux -S '/tmp/legacy socket' attach-session -f ignore-size -t '$9'" == command.removeprefix("TMUX= exec ")
    assert "agent-a::legacy" not in command
    marker = fake.pane_options[
        (workspace.primary.pane_id, tmux_server.HISTORY_SOURCE_OPTION)
    ]
    assert json.loads(marker) == {
        "schema_version": 1,
        "scope": "legacy",
        "server_pid": 91,
        "session_id": "$9",
    }


def test_switching_from_swap_to_legacy_returns_real_agent_home_first(
    rig, monkeypatch,
):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    target = tmux_server.TmuxServerTarget("/tmp/default", 91)
    monkeypatch.setattr(
        transport_mod.tmux_server, "target_has_session",
        lambda _target, session: session == "$9",
    )

    outcome = manager.attach(
        workspace.primary,
        "agent-a::legacy:91:9",
        server_target=target,
        session_target="$9",
    )

    assert outcome.ok
    assert fake.panes["%2"].window_id == "@2"
    assert fake.panes["%2"].pane_pid == 202
    assert workspace.primary.swap_state is None
    assert workspace.primary.transport_kind is DisplayTransportKind.NESTED


def test_marker_failure_never_moves_real_pane(rig):
    fake, workspace, manager = rig
    fake.fail_marker_window = "@1"
    outcome = manager.attach(workspace.primary, "agent-a")
    assert outcome.ok and outcome.fell_back
    assert fake.panes["%2"].window_id == "@2"
    assert not fake.window_options


def test_inconsistent_existing_marker_fails_without_touching_agent(rig):
    fake, workspace, manager = rig
    fake.window_options[("@1", "@railmux_swap_primary")] = "not-json"
    outcome = manager.attach(workspace.primary, "agent-a")
    assert not outcome.ok
    assert "inconsistent" in (outcome.reason or "")
    assert fake.panes["%2"].window_id == "@2"
    assert fake.respawned == []


def test_swap_failure_clears_markers_and_falls_back(rig):
    fake, workspace, manager = rig
    fake.fail_swap_at = 1
    outcome = manager.attach(workspace.primary, "agent-a")
    assert outcome.ok and outcome.fell_back
    assert fake.panes["%2"].window_id == "@2"
    assert not fake.window_options


def test_failed_verify_rolls_back_before_nested_fallback(rig, monkeypatch):
    fake, workspace, manager = rig
    real_verify = transport_mod._verified_displayed
    calls = 0

    def fail_first(state):
        nonlocal calls
        calls += 1
        return False if calls == 1 else real_verify(state)

    monkeypatch.setattr(transport_mod, "_verified_displayed", fail_first)
    outcome = manager.attach(workspace.primary, "agent-a")
    assert outcome.ok and outcome.fell_back
    assert len(fake.swap_calls) == 2
    assert fake.panes["%2"].window_id == "@2"


def test_failed_rollback_retains_recovery_state(rig, monkeypatch):
    fake, workspace, manager = rig
    monkeypatch.setattr(transport_mod, "_verified_displayed", lambda _state: False)
    fake.fail_swap_at = 2
    outcome = manager.attach(workspace.primary, "agent-a")

    assert not outcome.ok
    assert workspace.primary.swap_state is not None
    assert workspace.primary.pane_id == "%2"
    assert fake.window_options
    assert "railmux-keep-1" in fake.sessions


def test_failed_commit_rolls_back_before_nested_fallback(rig, monkeypatch):
    fake, workspace, manager = rig
    original = transport_mod._write_marker_pair
    writes = 0

    def fail_commit(state, slot_key):
        nonlocal writes
        writes += 1
        return False if writes == 2 else original(state, slot_key)

    monkeypatch.setattr(transport_mod, "_write_marker_pair", fail_commit)
    outcome = manager.attach(workspace.primary, "agent-a")

    assert outcome.ok and outcome.fell_back
    assert "commit failed" in (outcome.reason or "")
    assert fake.panes["%2"].window_id == "@2"
    assert len(fake.swap_calls) == 2


def test_return_failure_keeps_real_marked_and_never_kills_it(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    state = workspace.primary.swap_state
    assert state is not None
    fake.fail_swap_at = len(fake.swap_calls) + 1

    assert not manager.close_slot(workspace.primary)
    assert workspace.primary.swap_state is state
    assert "%2" in fake.panes
    assert fake.window_options


def test_two_distinct_slots_share_keeper_without_duplicate_agent(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    assert manager.attach(workspace.secondary, "agent-b").ok

    assert workspace.primary.swap_state.agent_pane_id == "%2"
    assert workspace.secondary.swap_state.agent_pane_id == "%3"
    assert workspace.primary.swap_state.keeper_session == (
        workspace.secondary.swap_state.keeper_session)
    assert fake.panes["%2"].window_id == "@1"
    assert fake.panes["%3"].window_id == "@1"


def test_create_secondary_uses_requested_stacked_layout(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok

    assert manager.create_secondary(WorkspaceLayout.STACKED)

    assert workspace.layout is WorkspaceLayout.STACKED
    assert workspace.secondary.pane_id in fake.panes
    assert "railmux.pane_surface --empty 2" in fake.split_commands[-1]


def test_create_primary_and_reset_slot_use_branded_empty_surface(rig):
    fake, workspace, manager = rig

    assert manager.create_primary()
    pane_id = workspace.primary.pane_id
    assert pane_id is not None
    assert "railmux.pane_surface --empty 1" in fake.split_commands[-1]

    workspace.primary.agent_tmux_name = "agent-a"
    workspace.primary.active_session_id = "session-a"
    assert manager.reset_slot(workspace.primary)
    assert workspace.primary.pane_id == pane_id
    assert workspace.primary.agent_tmux_name is None
    assert "railmux.pane_surface --empty 1" in fake.respawned[-1][1]


def test_create_primary_can_build_final_startup_width(rig):
    fake, workspace, manager = rig

    assert manager.create_primary(agent_width=126)

    assert workspace.primary.pane_id is not None
    assert fake.split_kwargs[-1] == {
        "target": "%0",
        "size_percent": None,
        "size_cells": 126,
        "detached": True,
    }


def test_create_dual_builds_both_empty_slots_with_one_transport_call(rig):
    fake, workspace, manager = rig

    assert manager.create_dual(
        WorkspaceLayout.SIDE_BY_SIDE,
        agent_width=143,
        secondary_extent=57,
    )

    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.primary.pane_id == "%10"
    assert workspace.secondary.pane_id == "%11"
    primary, secondary, layout, width, extent = fake.dual_calls[-1]
    assert "railmux.pane_surface --empty 1" in primary
    assert "railmux.pane_surface --empty 2" in secondary
    assert (layout, width, extent) == ("side-by-side", 143, 57)


def test_prepare_kill_returns_swap_home_and_keeps_secondary_empty(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    assert manager.create_secondary(WorkspaceLayout.STACKED)
    assert manager.attach(workspace.secondary, "agent-b").ok
    placeholder = workspace.secondary.swap_state.placeholder_pane_id

    assert manager.prepare_kill("agent-b")

    assert workspace.layout is WorkspaceLayout.STACKED
    assert workspace.secondary.pane_id == placeholder
    assert workspace.secondary.agent_tmux_name is None
    assert workspace.secondary.swap_state is None
    assert fake.panes["%3"].window_id == "@3"
    assert fake.respawned[-1][0] == placeholder
    assert "railmux.pane_surface --empty 2" in fake.respawned[-1][1]
    assert "agent-b" in fake.sessions  # The caller kills only after safe return.


def test_prepare_kill_detaches_nested_client_into_same_empty_pane(rig):
    fake, workspace, _manager = rig
    workspace.primary.pane_id = "%0"
    manager = AgentDisplayTransport(
        workspace, "nested", auto_launched=True,
        outer_session_name="railmux", outer_session_id="$1",
        owner_pane_id="%0",
    )
    assert manager.create_secondary(WorkspaceLayout.SIDE_BY_SIDE)
    pane_id = workspace.secondary.pane_id
    assert manager.attach(workspace.secondary, "agent-b").ok
    marker_key = (pane_id, tmux_server.HISTORY_SOURCE_OPTION)
    assert marker_key in fake.pane_options

    assert manager.prepare_kill("agent-b")

    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.secondary.pane_id == pane_id
    assert workspace.secondary.agent_tmux_name is None
    assert fake.respawned[-1][0] == pane_id
    assert "railmux.pane_surface --empty 2" in fake.respawned[-1][1]
    assert marker_key not in fake.pane_options


def test_swap_empty_surface_failure_reports_that_agent_is_already_home(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    workspace.primary.active_session_id = "session-a"
    placeholder = workspace.primary.swap_state.placeholder_pane_id
    fake.fail_respawn = True

    outcome = manager.prepare_kill("agent-a")

    assert not outcome
    assert "returned home" in (outcome.error or "")
    assert "nothing was killed" in (outcome.error or "")
    assert workspace.primary.pane_id == placeholder
    assert workspace.primary.agent_tmux_name is None
    assert workspace.primary.active_session_id is None
    assert workspace.primary.swap_state is None
    assert fake.panes["%2"].window_id == "@2"
    assert "agent-a" in fake.sessions


def test_nested_empty_surface_failure_refuses_to_kill_attached_agent(rig):
    fake, workspace, _manager = rig
    workspace.primary.pane_id = "%0"
    manager = AgentDisplayTransport(
        workspace, "nested", auto_launched=True,
        outer_session_name="railmux", outer_session_id="$1",
        owner_pane_id="%0",
    )
    assert manager.attach(workspace.primary, "agent-a").ok
    fake.fail_respawn = True

    outcome = manager.prepare_kill("agent-a")

    assert not outcome
    assert "could not detach" in (outcome.error or "")
    assert workspace.primary.agent_tmux_name == "agent-a"
    assert "agent-a" in fake.sessions


def test_preview_returns_real_home_and_keeps_display_placeholder(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    placeholder = workspace.primary.swap_state.placeholder_pane_id

    assert manager.prepare_preview(workspace.primary)
    assert fake.panes["%2"].window_id == "@2"
    assert workspace.primary.pane_id == placeholder
    assert placeholder in fake.panes


def test_late_external_client_returns_home_and_converts_to_nested(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    fake.sessions["agent-a"]["attached"] = 1

    outcome = manager.fallback_for_external_client(workspace.primary)

    assert outcome is not None and outcome.ok and outcome.fell_back
    assert outcome.kind == DisplayTransportKind.NESTED
    assert fake.panes["%2"].window_id == "@2"
    assert workspace.primary.agent_tmux_name == "agent-a"
    assert workspace.primary.swap_state is None


def test_two_slots_cannot_claim_same_real_pane(rig):
    _fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    assert not workspace.can_display(workspace.secondary, "agent-a")


def test_stale_owner_recovery_swaps_home_and_cleans_keeper(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    del fake.panes["%0"]  # model SIGKILL closing the Railmux owner pane

    report = recover_interrupted_swaps()

    assert report.repaired == 1
    assert report.unresolved == 0
    assert fake.panes["%2"].window_id == "@2"
    assert "railmux-keep-1" in fake.killed_sessions
    assert "railmux" in fake.killed_sessions


def test_active_owner_is_not_recovered(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    report = recover_interrupted_swaps()
    assert report.skipped_active == 1
    assert fake.panes["%2"].window_id == "@1"


def test_recovery_recreates_only_missing_marked_placeholder(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    state = workspace.primary.swap_state
    assert state is not None
    del fake.panes["%0"]
    assert fake.kill_pane(state.placeholder_pane_id)
    assert "agent-a" not in fake.sessions

    report = recover_interrupted_swaps()

    assert report.repaired == 1
    topology = fake.session_topology("agent-a")
    assert topology is not None
    assert topology.single_live_pane.pane_id == "%2"
    assert topology.single_live_pane.pane_pid == 202
