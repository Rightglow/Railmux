from __future__ import annotations

import subprocess

import pytest

from railmux import tmux_server


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


def test_current_socket_parser_allows_commas_in_the_path():
    env = {"TMUX": "/tmp/with,comma/railmux,123,0"}
    assert tmux_server.current_socket_path(env) == "/tmp/with,comma/railmux"


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

    monkeypatch.setattr(subprocess, "check_output", timeout)

    with pytest.raises(tmux_server.TmuxServerUnresponsive):
        tmux_server.discover_target(timeout=0.25)

    assert observed == {
        "argv": [
            "tmux", "-L", "railmux", "display-message", "-p",
            "#{socket_path}\t#{pid}",
        ],
        "timeout": 0.25,
    }


def test_scoped_target_environment_restores_the_caller(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%4")
    target = tmux_server.TmuxServerTarget("/tmp/private", 2)

    with tmux_server.scoped_target_environment(target):
        assert tmux_server.current_socket_path() == "/tmp/private"
        assert "TMUX_PANE" not in tmux_server.os.environ

    assert tmux_server.os.environ["TMUX"] == "/tmp/default,1,0"
    assert tmux_server.os.environ["TMUX_PANE"] == "%4"


def test_legacy_discovery_uses_default_label_without_relaxing_socket_label(
    monkeypatch,
):
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **_kwargs: "/tmp/default\t44\n",
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
            "44\tother\t$1\n44\trailmux\t$7\n45\trailmux\t$8\n"
        ),
    )

    assert tmux_server.target_session_id(target, "railmux") == "$7"


def test_target_session_id_rejects_ambiguous_or_malformed_output(monkeypatch):
    target = tmux_server.TmuxServerTarget("/tmp/private", 44)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **_kwargs: "44\trailmux\t$7\n44\trailmux\t$8\n",
    )

    assert tmux_server.target_session_id(target, "railmux") is None
