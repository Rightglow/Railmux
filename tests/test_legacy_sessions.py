from pathlib import Path

from railmux import legacy_sessions, tmux_server
from railmux.ui.app import App, _Running


def test_discover_legacy_sessions_is_read_only_and_records_topology(monkeypatch):
    target = tmux_server.TmuxServerTarget("/tmp/default", 44)
    monkeypatch.setattr(
        legacy_sessions.tmux_server, "discover_legacy_target",
        lambda **_kwargs: target,
    )
    monkeypatch.setattr(
        legacy_sessions.tmux_server, "target_is_live",
        lambda candidate, **_kwargs: candidate == target,
    )
    output = (
        "cx-one\t/work/a\t12\t$1\t%1\t1\t1\tmarker\tbinding\n"
        "cx-many\t/work/b\t13\t$2\t%2\t2\t1\t\t\n"
        "cc-many-panes\t/work/c\t14\t$3\t%3\t1\t2\t\t\n"
    )
    calls = []
    monkeypatch.setattr(
        legacy_sessions.subprocess,
        "check_output",
        lambda argv, **_kwargs: calls.append(argv) or output,
    )

    discovered_target, records, complete = legacy_sessions.discover()

    assert discovered_target == target
    assert complete
    assert records == (
        legacy_sessions.LegacySession(
            "cx-one", Path("/work/a"), 12, "$1", "%1", "marker", "binding", True
        ),
        legacy_sessions.LegacySession(
            "cx-many", Path("/work/b"), 13, "$2", "%2", "", "", False
        ),
        legacy_sessions.LegacySession(
            "cc-many-panes", Path("/work/c"), 14, "$3", "%3", "", "", False
        ),
    )
    assert calls[0][:4] == ["tmux", "-S", "/tmp/default", "list-sessions"]
    assert not any("set-option" in call or "kill-session" in call for call in calls)


def test_duplicate_provider_id_prefers_dedicated_but_keeps_unique_legacy_row():
    app = App.__new__(App)
    target = tmux_server.TmuxServerTarget("/tmp/default", 44)
    current = _Running("provider-id", "cx-same", "current")
    legacy = _Running(
        "__legacy__-44-7",
        "cx-same::legacy:44:7",
        "legacy",
        legacy_server=target,
        legacy_session_id="$7",
        provider_session_id="provider-id",
    )
    app._running = {current.key: current, legacy.key: legacy}

    assert app._by_session_id("provider-id") is current
    assert app._by_tmux(legacy.tmux_name) is legacy
    assert app._running_session_ids() == {"provider-id"}


def test_explicit_legacy_kill_uses_pinned_server_identity(monkeypatch):
    app = App.__new__(App)
    target = tmux_server.TmuxServerTarget("/tmp/default", 44)
    legacy = _Running(
        "provider-id",
        "cx-old::legacy:44:7",
        "old",
        legacy_server=target,
        legacy_session_id="$7",
        provider_session_id="provider-id",
    )
    app._running = {legacy.key: legacy}
    monkeypatch.setattr(app, "_return_agent_before_kill", lambda _name: True)
    monkeypatch.setattr(app, "_agent_session_alive", lambda _name: False)
    monkeypatch.setattr(app, "_refresh", lambda: None)
    statuses = []
    monkeypatch.setattr(app, "_set_status", lambda *args: statuses.append(args))
    killed = []
    monkeypatch.setattr(
        tmux_server,
        "kill_target_session",
        lambda server, session: killed.append((server, session)) or True,
    )

    app._kill_tmux_session(legacy.tmux_name, legacy.label)

    assert killed == [(target, "$7")]
    assert app._running == {}
    assert statuses[-1][0] == "Killed: old  (file kept)"
