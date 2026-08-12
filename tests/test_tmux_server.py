from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from railmux import __version__, tmux_server


@pytest.mark.parametrize(
    "label",
    ["default", "", "has/slash", "has space", "x" * 65, "非ascii"],
)
def test_socket_label_rejects_unsafe_or_shared_names(label):
    with pytest.raises(tmux_server.TmuxServerError):
        tmux_server.socket_label({tmux_server.SOCKET_LABEL_ENV: label})


def test_tmux_argv_always_selects_a_nondefault_label():
    assert tmux_server.tmux_argv(
        "list-sessions", env={tmux_server.SOCKET_LABEL_ENV: "rx-test-12"}
    ) == ["tmux", "-L", "rx-test-12", "list-sessions"]


def test_launcher_argv_preserves_multi_argument_python_module_prefix():
    assert tmux_server.launcher_argv(
        ["/usr/bin/python3", "-m", "railmux"],
        ["--mode", "codex"],
    ) == [
        "tmux", "-L", "railmux",
        "start-server", ";", "set-option", "-g", "status", "off", ";",
        "new-session", "-A", "-s", "railmux",
        "/usr/bin/python3", "-m", "railmux", "--inside-tmux",
        "--mode", "codex",
    ]


def test_launcher_argv_can_add_one_validated_client_feature():
    assert tmux_server.launcher_argv(
        ["/usr/bin/python3", "-m", "railmux"],
        [],
        client_features=("sync",),
    )[:7] == [
        "tmux", "-L", "railmux", "-T", "sync", "start-server", ";",
    ]


@pytest.mark.parametrize("feature", ["-L", "sync,focus", "", "非ascii"])
def test_launcher_argv_rejects_untrusted_client_features(feature):
    with pytest.raises(tmux_server.TmuxServerError):
        tmux_server.launcher_argv(["railmux"], [], client_features=(feature,))


def test_detached_launcher_session_is_pinned_before_and_after_create(
    monkeypatch,
):
    target = tmux_server.TmuxServerTarget("/tmp/private", 44)
    live = []
    session_ids = iter((None, "$7"))
    # Another launcher can win after our initial absence check. tmux then
    # rejects this duplicate create, while the exact resulting session is the
    # safe shared outcome.
    run = SimpleNamespace(returncode=1)
    launched = []

    def target_is_live(candidate, **kwargs):
        live.append((candidate, kwargs))
        return True

    monkeypatch.setattr(tmux_server, "target_is_live", target_is_live)
    monkeypatch.setattr(
        tmux_server,
        "target_session_id",
        lambda *_args, **_kwargs: next(session_ids),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: launched.append((argv, kwargs)) or run,
    )

    assert tmux_server.ensure_detached_launcher_session(
        target,
        ["/opt/railmux/bin/python", "-m", "railmux"],
        ["--mode", "codex"],
        env={"PATH": "/usr/bin"},
    ) == "$7"

    assert len(live) == 2
    assert launched[0][0] == [
        "tmux", "-S", "/tmp/private",
        "new-session", "-d", "-s", "railmux",
        "/opt/railmux/bin/python", "-m", "railmux", "--inside-tmux",
        "--mode", "codex",
    ]
    assert launched[0][1]["env"] == {"PATH": "/usr/bin"}


def test_detached_managed_windows_session_receives_only_runtime_identity(
    monkeypatch,
):
    target = tmux_server.TmuxServerTarget("/tmp/private", 44)
    session_ids = iter((None, "$7"))
    launched = []
    monkeypatch.setattr(tmux_server, "target_is_live", lambda *_a, **_k: True)
    monkeypatch.setattr(
        tmux_server,
        "target_session_id",
        lambda *_args, **_kwargs: next(session_ids),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: launched.append((argv, kwargs))
        or SimpleNamespace(returncode=0),
    )
    env = {
        "RAILMUX_WINDOWS_RUNTIME": "msys2",
        "RAILMUX_MSYS2_RUNTIME_ID": "msys2-2026-03-22-r1",
        "RAILMUX_MSYS2_APP_ID": f"railmux-{__version__}",
        "CODEX_API_KEY": "must-not-enter-tmux",
    }

    assert tmux_server.ensure_detached_launcher_session(
        target,
        ["/opt/railmux/bin/railmux"],
        [],
        env=env,
        initial_size=(164, 46),
    ) == "$7"

    argv = launched[0][0]
    assert argv == [
        "tmux", "-S", "/tmp/private", "new-session", "-d", "-s",
        "railmux",
        "-x", "164", "-y", "46",
        "-e", "RAILMUX_WINDOWS_RUNTIME=msys2",
        "-e", "RAILMUX_MSYS2_RUNTIME_ID=msys2-2026-03-22-r1",
        "-e", f"RAILMUX_MSYS2_APP_ID=railmux-{__version__}",
        "/opt/railmux/bin/railmux", "--inside-tmux",
    ]
    assert all("CODEX_API_KEY" not in value for value in argv)


@pytest.mark.parametrize(
    "initial_size",
    [None, (0, 24), (80, 0), (-1, 24), (80, 65536), (True, 24)],
)
def test_detached_session_does_not_invent_or_forward_invalid_size(
    monkeypatch, initial_size,
):
    target = tmux_server.TmuxServerTarget("/tmp/private", 44)
    session_ids = iter((None, "$7"))
    launched = []
    monkeypatch.setattr(tmux_server, "target_is_live", lambda *_a, **_k: True)
    monkeypatch.setattr(
        tmux_server,
        "target_session_id",
        lambda *_args, **_kwargs: next(session_ids),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kwargs: launched.append(argv)
        or SimpleNamespace(returncode=0),
    )

    assert tmux_server.ensure_detached_launcher_session(
        target, ["railmux"], [], initial_size=initial_size,
    ) == "$7"

    assert "-x" not in launched[0]
    assert "-y" not in launched[0]


def test_detached_session_rejects_unbounded_runtime_identity(monkeypatch):
    target = tmux_server.TmuxServerTarget("/tmp/private", 44)
    session_ids = iter((None, "$7"))
    launched = []
    monkeypatch.setattr(tmux_server, "target_is_live", lambda *_a, **_k: True)
    monkeypatch.setattr(
        tmux_server,
        "target_session_id",
        lambda *_args, **_kwargs: next(session_ids),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: launched.append(argv)
        or SimpleNamespace(returncode=0),
    )

    assert tmux_server.ensure_detached_launcher_session(
        target,
        ["railmux"],
        [],
        env={
            "RAILMUX_WINDOWS_RUNTIME": "msys2",
            "RAILMUX_MSYS2_APP_ID": "bad=value",
        },
    ) == "$7"

    assert "RAILMUX_WINDOWS_RUNTIME=msys2" in launched[0]
    assert all("bad=value" not in value for value in launched[0])


def test_detached_launcher_session_refuses_changed_server_identity(
    monkeypatch,
):
    target = tmux_server.TmuxServerTarget("/tmp/private", 44)
    session_id = MagicMock(return_value=None)
    monkeypatch.setattr(tmux_server, "target_session_id", session_id)
    monkeypatch.setattr(
        tmux_server, "target_is_live", MagicMock(side_effect=(True, False)))
    monkeypatch.setattr(
        subprocess, "run", MagicMock(return_value=SimpleNamespace(returncode=0)))

    assert tmux_server.ensure_detached_launcher_session(
        target, ["railmux"], [],
    ) is None
    session_id.assert_called_once_with(target, "railmux", timeout=0.5)


def test_current_socket_parser_allows_commas_in_the_path():
    env = {"TMUX": "/tmp/with,comma/railmux,123,0"}
    assert tmux_server.current_socket_path(env) == "/tmp/with,comma/railmux"


def test_current_target_parses_exact_inherited_server():
    env = {"TMUX": "/tmp/with,comma/railmux,4321,9"}

    assert tmux_server.current_target(env) == tmux_server.TmuxServerTarget(
        "/tmp/with,comma/railmux", 4321)
    assert tmux_server.current_target({"TMUX": "/tmp/s,-1,0"}) is None
    assert tmux_server.current_target({"TMUX": "/tmp/s,not-a-pid,0"}) is None
    assert tmux_server.current_target({}) is None


def test_target_is_live_rejects_reused_socket_with_different_server_pid(
        monkeypatch):
    target = tmux_server.TmuxServerTarget("/tmp/private", 44)
    monkeypatch.setattr(
        subprocess, "check_output", lambda *_args, **_kwargs: "45\n")

    assert tmux_server.target_is_live(target) is False


def test_full_socket_identity_accepts_same_socket_and_rejects_spoof(
    monkeypatch, tmp_path,
):
    dedicated_dir = tmp_path / "dedicated"
    foreign_dir = tmp_path / "foreign"
    dedicated_dir.mkdir()
    foreign_dir.mkdir()
    dedicated = dedicated_dir / "railmux"
    spoof = foreign_dir / "railmux"
    dedicated.touch()
    spoof.touch()
    target = tmux_server.TmuxServerTarget(str(dedicated), 123)

    monkeypatch.setenv("TMUX", f"{dedicated},123,0")
    assert tmux_server.is_current_server(target)

    monkeypatch.setenv("TMUX", f"{spoof},456,0")
    assert not tmux_server.is_current_server(target)


def test_discover_target_uses_explicit_label_and_times_out(monkeypatch):
    observed = {}

    def timeout(argv, **kwargs):
        observed["argv"] = argv
        observed["timeout"] = kwargs["timeout"]
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(tmux_server.TmuxServerUnresponsive):
        tmux_server.discover_target(timeout=0.25)

    assert observed == {
        "argv": [
            "tmux", "-L", "railmux", "display-message", "-p",
            "#{socket_path} #{pid}",
        ],
        "timeout": 0.25,
    }


@pytest.mark.parametrize(
    ("env", "expected_timeout"),
    [
        ({}, 2.0),
        ({"RAILMUX_WINDOWS_RUNTIME": "msys2"}, 5.0),
    ],
)
def test_discover_target_allows_managed_msys_stale_socket_settle(
    monkeypatch, env, expected_timeout,
):
    observed = {}

    def discover(label, *, timeout, env):
        observed.update(label=label, timeout=timeout, env=env)
        return None

    monkeypatch.setattr(tmux_server, "_discover_label_target", discover)

    assert tmux_server.discover_target(env=env) is None
    assert observed == {
        "label": "railmux",
        "timeout": expected_timeout,
        "env": env,
    }


def test_discover_target_classifies_tmux_client_server_mismatch(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="",
            stderr="server version is too old for client\n",
            returncode=0,
        ),
    )

    with pytest.raises(
        tmux_server.TmuxClientServerMismatch, match="railmux config"
    ):
        tmux_server.discover_target()


def test_discover_target_preserves_spaces_in_socket_path(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="/tmp/private socket/railmux 123\n",
            stderr="",
            returncode=0,
        ),
    )

    assert tmux_server.discover_target() == tmux_server.TmuxServerTarget(
        "/tmp/private socket/railmux",
        123,
    )


def test_scoped_target_environment_restores_the_caller(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%4")
    target = tmux_server.TmuxServerTarget("/tmp/private", 2)

    with tmux_server.scoped_target_environment(target):
        assert tmux_server.current_socket_path() == "/tmp/private"
        assert "TMUX_PANE" not in tmux_server.os.environ

    assert tmux_server.os.environ["TMUX"] == "/tmp/default,1,0"
    assert tmux_server.os.environ["TMUX_PANE"] == "%4"


def test_runtime_environment_sync_targets_only_the_proven_server(monkeypatch):
    target = tmux_server.TmuxServerTarget("/tmp/private", 44)
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs))
        or SimpleNamespace(returncode=0),
    )

    assert tmux_server.sync_server_environment(
        target,
        {"PATH": "/opt/tmux/bin:/usr/bin", "LANG": "C.UTF-8"},
    )

    assert calls[0][0] == [
        "tmux", "-S", "/tmp/private", "set-environment", "-g",
        "-u", "LC_ALL",
    ]
    assert all(call[0][1:3] == ["-S", "/tmp/private"] for call in calls)


def test_legacy_discovery_uses_default_label_without_relaxing_socket_label(
    monkeypatch,
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kwargs: SimpleNamespace(
            stdout="/tmp/default 44\n", stderr="", returncode=0
        ),
    )

    assert tmux_server.discover_legacy_target() == (
        tmux_server.TmuxServerTarget("/tmp/default", 44)
    )


def test_exact_legacy_kill_revalidates_before_destructive_command(monkeypatch):
    target = tmux_server.TmuxServerTarget("/tmp/default", 44)
    observations = iter((True, False))
    monkeypatch.setattr(
        tmux_server, "target_has_session", lambda *_args, **_kwargs: next(observations))
    called = []
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **_kwargs: called.append(argv))

    assert tmux_server.kill_target_session(target, "$7")
    assert called == [["tmux", "-S", "/tmp/default", "kill-session", "-t", "$7"]]


def test_exact_legacy_kill_refuses_changed_identity(monkeypatch):
    target = tmux_server.TmuxServerTarget("/tmp/default", 44)
    monkeypatch.setattr(
        tmux_server, "target_has_session", lambda *_args, **_kwargs: False)
    called = []
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **_kwargs: called.append(argv))

    assert not tmux_server.kill_target_session(target, "$7")
    assert called == []


def test_target_session_id_matches_exact_name_and_server(monkeypatch):
    target = tmux_server.TmuxServerTarget("/tmp/private", 44)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **_kwargs: (
            "44 $1 other\n44 $7 railmux\n45 $8 railmux\n"
        ),
    )

    assert tmux_server.target_session_id(target, "railmux") == "$7"


def test_target_session_id_rejects_ambiguous_or_malformed_output(monkeypatch):
    target = tmux_server.TmuxServerTarget("/tmp/private", 44)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **_kwargs: "44 $7 railmux\n44 $8 railmux\n",
    )

    assert tmux_server.target_session_id(target, "railmux") is None


def test_nested_history_source_round_trip_revalidates_exact_legacy_target(
    monkeypatch,
):
    target = tmux_server.TmuxServerTarget("/tmp/default", 44)
    marker = tmux_server.encode_history_source(target, "$7", legacy=True)
    monkeypatch.setattr(
        tmux_server, "discover_legacy_target", lambda **_kwargs: target)
    panes = []
    monkeypatch.setattr(
        tmux_server,
        "target_has_session",
        lambda *_args, **_kwargs: pytest.fail("session was probed twice"),
    )
    monkeypatch.setattr(
        tmux_server,
        "target_single_pane_id",
        lambda candidate, session, **_kwargs: (
            panes.append((candidate, session)) or "%2"
        ),
    )

    assert marker is not None
    assert tmux_server.resolve_history_pane(marker) == (target, "%2")
    assert panes == [(target, "$7")]


def test_nested_history_source_rejects_changed_server_or_extra_fields(monkeypatch):
    target = tmux_server.TmuxServerTarget("/tmp/default", 44)
    marker = tmux_server.encode_history_source(target, "$7", legacy=True)
    monkeypatch.setattr(
        tmux_server,
        "discover_legacy_target",
        lambda **_kwargs: tmux_server.TmuxServerTarget("/tmp/default", 45),
    )

    assert marker is not None
    assert tmux_server.resolve_history_pane(marker) is None
    assert tmux_server.resolve_history_pane(
        marker[:-1] + ',"unexpected":true}'
    ) is None


def test_target_single_pane_id_requires_one_live_exact_pane(monkeypatch):
    target = tmux_server.TmuxServerTarget("/tmp/default", 44)
    outputs = iter((
        "44 $7 %2 0\n",
        "44 $7 %2 0\n44 $7 %3 0\n",
    ))
    monkeypatch.setattr(
        subprocess, "check_output", lambda *_args, **_kwargs: next(outputs))

    assert tmux_server.target_single_pane_id(target, "$7") == "%2"
    assert tmux_server.target_single_pane_id(target, "$7") is None


def test_transcript_source_round_trip_opens_only_exact_same_user_file(tmp_path):
    session_id = "47fca075-9cb8-44fb-a314-d57ef2256ad9"
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text('{"type":"user"}\n')
    marker = tmux_server.encode_transcript_source("claude", session_id, path)

    assert marker is not None
    source = tmux_server.decode_transcript_source(marker)
    assert source is not None and source.path == path
    opened = tmux_server.open_transcript_source(marker)
    assert opened is not None
    os.close(opened[1])


def test_codex_transcript_source_requires_matching_rollout_file(tmp_path):
    session_id = "019fcaad-27a1-70c0-8029-8a9c7803fa6b"
    path = tmp_path / f"rollout-2026-08-04T02-49-33-{session_id}.jsonl"
    path.write_text('{"type":"response_item"}\n')

    marker = tmux_server.encode_transcript_source("codex", session_id, path)

    assert marker is not None
    source = tmux_server.decode_transcript_source(marker)
    assert source is not None
    assert source.provider == "codex" and source.path == path
    opened = tmux_server.open_transcript_source(marker)
    assert opened is not None
    os.close(opened[1])
    assert tmux_server.encode_transcript_source(
        "codex", "019fc572-0cc5-7630-86a7-806fde2d88fc", path
    ) is None
    assert tmux_server.encode_transcript_source(
        "unknown", session_id, path
    ) is None


def test_transcript_source_rejects_final_symlink_and_extra_fields(tmp_path):
    session_id = "47fca075-9cb8-44fb-a314-d57ef2256ad9"
    real = tmp_path / f"{session_id}.jsonl.real"
    real.write_text("{}\n")
    linked = tmp_path / f"{session_id}.jsonl"
    linked.symlink_to(real)
    marker = tmux_server.encode_transcript_source("claude", session_id, linked)

    assert marker is not None
    assert tmux_server.open_transcript_source(marker) is None
    assert tmux_server.decode_transcript_source(
        marker[:-1] + ',"unexpected":true}'
    ) is None
    assert tmux_server.decode_transcript_source(None) is None
