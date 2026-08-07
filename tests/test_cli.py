import os
import signal
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from railmux import tmux_server
from railmux.cli import (
    _interactive_terminal_size,
    _reset_terminal_modes,
    _run_tmux_client_with_watchdog,
    _show_startup_message,
    is_ssh_session,
    main,
)
from railmux.config import Config, ConfigError
from railmux.tmux_server import TmuxServerTarget


def test_interactive_terminal_size_uses_real_tty_dimensions(monkeypatch):
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("railmux.cli.sys.stdout.isatty", lambda: True)
    get_size = MagicMock(return_value=os.terminal_size((164, 46)))
    monkeypatch.setattr("railmux.cli.os.get_terminal_size", get_size)

    assert _interactive_terminal_size() == (164, 46)
    get_size.assert_called_once_with(sys.stdout.fileno())


def test_interactive_terminal_size_does_not_invent_non_tty_fallback(
    monkeypatch,
):
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: False)
    get_size = MagicMock()
    monkeypatch.setattr("railmux.cli.os.get_terminal_size", get_size)

    assert _interactive_terminal_size() is None
    get_size.assert_not_called()


@pytest.fixture(autouse=True)
def tmux_preflight_succeeds(monkeypatch, tmp_path):
    """CLI tests must not depend on the host's tmux or Railmux settings."""
    monkeypatch.setattr(
        "railmux.settings._config_path", lambda: tmp_path / "config.toml"
    )
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", lambda: True)
    monkeypatch.setattr(
        "railmux.self_update.maybe_upgrade_before_launch", lambda *_args: None
    )
    target = TmuxServerTarget("/tmp/tmux-test/railmux", 123)
    monkeypatch.setattr("railmux.cli.tmux_server.discover_target", lambda: target)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.is_current_server", lambda _target: True
    )


def test_no_scroll_coalescing_flag_reaches_app(tmp_path):
    with patch("railmux.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
            "--no-scroll-coalescing",
        ])

    assert result == 0
    app_cls.assert_called_once()
    assert app_cls.call_args.kwargs["scroll_coalescing"] is False
    app_cls.return_value.run.assert_called_once()


def test_scroll_coalescing_is_enabled_automatically_over_ssh(tmp_path):
    with patch.dict("os.environ", {"SSH_CONNECTION": "client 1 server 2"}), \
         patch("railmux.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
        ])

    assert result == 0
    assert app_cls.call_args.kwargs["scroll_coalescing"] is True


def test_scroll_coalescing_is_disabled_automatically_locally(tmp_path):
    clean_env = {
        "SSH_CONNECTION": "",
        "SSH_CLIENT": "",
        "SSH_TTY": "",
    }
    with patch.dict("os.environ", clean_env), patch("railmux.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
        ])

    assert result == 0
    assert app_cls.call_args.kwargs["scroll_coalescing"] is False


def test_force_enable_scroll_coalescing_locally(tmp_path):
    clean_env = {
        "SSH_CONNECTION": "",
        "SSH_CLIENT": "",
        "SSH_TTY": "",
    }
    with patch.dict("os.environ", clean_env), patch("railmux.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
            "--scroll-coalescing",
        ])

    assert result == 0
    assert app_cls.call_args.kwargs["scroll_coalescing"] is True


def test_is_ssh_session_recognizes_common_markers():
    assert is_ssh_session({"SSH_CONNECTION": "client 1 server 2"})
    assert is_ssh_session({"SSH_CLIENT": "client 1 2"})
    assert is_ssh_session({"SSH_TTY": "/dev/pts/1"})
    assert not is_ssh_session({})


def test_tmux_preflight_also_runs_for_inside_tmux(monkeypatch, tmp_path):
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", lambda: False)
    with patch("railmux.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
        ])

    assert result == 2
    app_cls.assert_not_called()


def test_doctor_runs_before_tmux_preflight(monkeypatch, tmp_path):
    doctor = MagicMock(return_value=0)
    preflight = MagicMock(return_value=False)
    monkeypatch.setattr("railmux.cli.run_doctor", doctor)
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", preflight)

    result = main(["doctor", "--claude-home", str(tmp_path)])

    assert result == 0
    doctor.assert_called_once_with(claude_home=tmp_path, json_output=False)
    preflight.assert_not_called()


def test_config_runs_before_tmux_preflight(monkeypatch):
    config_main = MagicMock(return_value=0)
    preflight = MagicMock(return_value=False)
    monkeypatch.setattr("railmux.config_cli.main", config_main)
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", preflight)

    result = main(["config"])

    assert result == 0
    config_main.assert_called_once_with([])
    preflight.assert_not_called()


def test_doctor_json_is_forwarded_before_tmux_preflight(monkeypatch, tmp_path):
    doctor = MagicMock(return_value=0)
    preflight = MagicMock(return_value=False)
    monkeypatch.setattr("railmux.cli.run_doctor", doctor)
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", preflight)

    result = main(["doctor", "--json", "--claude-home", str(tmp_path)])

    assert result == 0
    doctor.assert_called_once_with(claude_home=tmp_path, json_output=True)
    preflight.assert_not_called()


def test_doctor_remote_dispatches_read_only_remote_preflight(monkeypatch):
    remote_doctor = MagicMock(return_value=2)
    preflight = MagicMock(return_value=False)
    monkeypatch.setattr(
        "railmux.ssh_doctor.run_remote_ssh_doctor",
        remote_doctor,
    )
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", preflight)

    result = main([
        "doctor",
        "--remote",
        "example",
        "--ssh-arg=-J",
        "--ssh-arg=jump",
        "--json",
    ])

    assert result == 2
    remote_doctor.assert_called_once_with(
        "example",
        ssh_args=["-J", "jump"],
        json_output=True,
        remote_platform="auto",
    )
    preflight.assert_not_called()


def test_doctor_ssh_accepts_grouped_arguments(monkeypatch):
    remote_doctor = MagicMock(return_value=0)
    monkeypatch.setattr(
        "railmux.ssh_doctor.run_remote_ssh_doctor",
        remote_doctor,
    )

    result = main([
        "doctor",
        "--remote",
        "example",
        "--ssh-args=-J jump -p 2222",
    ])

    assert result == 0
    remote_doctor.assert_called_once_with(
        "example",
        ssh_args=["-J", "jump", "-p", "2222"],
        json_output=False,
        remote_platform="auto",
    )


def test_doctor_remote_forwards_explicit_windows_platform(monkeypatch):
    remote_doctor = MagicMock(return_value=0)
    monkeypatch.setattr(
        "railmux.ssh_doctor.run_remote_ssh_doctor", remote_doctor)

    assert main([
        "doctor", "--remote", "example",
        "--remote-platform", "windows",
    ]) == 0
    remote_doctor.assert_called_once_with(
        "example",
        ssh_args=[],
        json_output=False,
        remote_platform="windows",
    )


@pytest.mark.parametrize("help_flag", ["-h", "--help"])
def test_doctor_help_exposes_only_remote_and_grouped_ssh_args(help_flag, capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["doctor", help_flag])

    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert "--remote HOST" in help_text
    assert "--ssh HOST" not in help_text
    assert "--ssh-arg VALUE" not in help_text
    assert "--ssh-args ARGS" in help_text
    assert "--claude-home" not in help_text


@pytest.mark.parametrize("help_flag", ["-h", "--help"])
def test_root_help_is_public_command_summary_only(help_flag, capsys):
    with pytest.raises(SystemExit) as stopped:
        main([help_flag])

    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert "railmux ssh HOST" in help_text
    assert "railmux config" in help_text
    assert "railmux doctor" in help_text
    assert "railmux COMMAND --help" in help_text
    assert "remote-server" not in help_text
    assert "--inside-tmux" not in help_text
    assert "--claude-home" not in help_text


@pytest.mark.parametrize("help_flag", ["-h", "--help"])
def test_ssh_help_is_complete_and_side_effect_free(
    help_flag,
    monkeypatch,
    capsys,
):
    def unexpected_config_load():
        raise AssertionError("ssh help must not load configuration")

    monkeypatch.setattr("railmux.cli.load_config", unexpected_config_load)

    with pytest.raises(SystemExit) as stopped:
        main(["ssh", help_flag])

    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert "--session SESSION" in help_text
    assert "--fps FPS" in help_text
    assert "--ssh-args ARGS" in help_text
    assert "--ssh-arg VALUE" not in help_text


@pytest.mark.parametrize("help_flag", ["-h", "--help"])
def test_config_help_is_complete_and_hides_internal_options(help_flag, capsys):
    assert main(["config", help_flag]) == 0

    help_text = capsys.readouterr().out
    assert "--remote HOST" in help_text
    assert "--ssh-args ARGS" in help_text
    assert "--ssh-arg VALUE" not in help_text
    assert "--remote-context" not in help_text


@pytest.mark.parametrize("help_flag", ["-h", "--help"])
def test_remote_server_help_has_no_protocol_handshake(help_flag, capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["remote-server", help_flag])

    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert help_text.startswith("usage: railmux remote-server")
    assert "--protocol PROTOCOL" in help_text
    assert "--width WIDTH" in help_text
    assert "--height HEIGHT" in help_text
    assert "--replace-existing-client" not in help_text
    assert "--existing-session-only" not in help_text
    assert "RAILMUX-REMOTE/" not in help_text


def test_legacy_doctor_flag_is_removed():
    with pytest.raises(SystemExit) as exc:
        main(["--doctor"])

    assert exc.value.code == 2


def test_ssh_subcommand_dispatches_before_local_tmux_preflight(monkeypatch):
    calls = []
    ssh_main = MagicMock(return_value=7)
    preflight = MagicMock(return_value=False)
    update = MagicMock(side_effect=lambda *_args: calls.append("update"))
    ssh_main.side_effect = lambda *_args: calls.append("connect") or 7
    monkeypatch.setattr(
        "railmux.self_update.maybe_upgrade_before_launch", update
    )
    monkeypatch.setattr("railmux.fast_display_client.main", ssh_main)
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", preflight)

    result = main(["ssh", "example", "--fps", "30"])

    assert result == 7
    assert calls == ["update", "connect"]
    raw_args, settings = update.call_args.args
    assert raw_args == ["ssh", "example", "--fps", "30"]
    assert settings.update_policy == "ask"
    ssh_main.assert_called_once_with(["example", "--fps", "30"])
    preflight.assert_not_called()


def test_ssh_does_not_require_the_locally_configured_tmux(monkeypatch):
    monkeypatch.setattr(
        "railmux.cli.load_config",
        lambda: Config(tmux_binary="/missing/bin/tmux"),
    )
    check = MagicMock()
    monkeypatch.setattr("railmux.cli.check_executable", check)
    ssh_main = MagicMock(return_value=0)
    monkeypatch.setattr("railmux.fast_display_client.main", ssh_main)

    assert main(["ssh", "example"]) == 0

    check.assert_not_called()
    ssh_main.assert_called_once_with(["example"])


def test_remote_server_subcommand_dispatches_to_internal_helper(monkeypatch):
    server_main = MagicMock(return_value=9)
    preflight = MagicMock(return_value=False)
    update = MagicMock()
    monkeypatch.setattr(
        "railmux.self_update.maybe_upgrade_before_launch", update
    )
    monkeypatch.setattr("railmux.fast_display_server.main", server_main)
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", preflight)

    result = main(["remote-server", "--protocol", "4"])

    assert result == 9
    update.assert_not_called()
    server_main.assert_called_once_with(["--protocol", "4"])
    preflight.assert_not_called()


def test_inside_tmux_fails_closed_on_a_foreign_server(monkeypatch, capsys):
    monkeypatch.setattr(
        "railmux.cli.tmux_server.is_current_server", lambda _target: False
    )

    with patch("railmux.ui.app.App") as app_cls:
        result = main(["--inside-tmux"])

    assert result == 2
    assert "reserved for Railmux's dedicated" in capsys.readouterr().err
    app_cls.assert_not_called()


def test_foreign_tmux_launches_dedicated_server_with_clean_environment(
    monkeypatch,
):
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda: None
    )
    monkeypatch.setattr(
        "railmux.cli.tmux_server.is_current_server", lambda _target: False
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-user/default,456,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr("railmux.cli.sys.argv", ["/bin/railmux"])
    run_client = MagicMock(return_value=17)
    monkeypatch.setattr(
        "railmux.cli._run_tmux_client_with_watchdog", run_client
    )

    assert main(["--project", "/work"]) == 17

    argv, env = run_client.call_args.args
    assert run_client.call_args.kwargs == {
        "expected_target": None,
        "expected_session_id": None,
    }
    assert argv == [
        "tmux", "-L", "railmux",
        "start-server", ";", "set-option", "-g", "status", "off", ";",
        "new-session", "-A", "-s", "railmux",
        "/bin/railmux", "--inside-tmux", "--project", "/work",
    ]
    assert "TMUX" not in env
    assert "TMUX_PANE" not in env
    # The caller's environment is unchanged; only the replacement process is
    # detached from the foreign tmux identity.
    assert os.environ["TMUX"] == "/tmp/tmux-user/default,456,0"
    assert os.environ["TMUX_PANE"] == "%9"


def test_outer_launcher_checks_for_update_once(monkeypatch):
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda: None
    )
    monkeypatch.setattr(
        "railmux.cli.tmux_server.is_current_server", lambda _target: False
    )
    update = MagicMock()
    monkeypatch.setattr(
        "railmux.self_update.maybe_upgrade_before_launch", update
    )
    monkeypatch.setattr(
        "railmux.cli._run_tmux_client_with_watchdog",
        MagicMock(return_value=0),
    )

    assert main(["--project", "/work"]) == 0

    assert update.call_count == 1
    raw_args, settings = update.call_args.args
    assert raw_args == ["--project", "/work"]
    assert settings.update_policy == "ask"


def test_prelaunch_recovery_is_scoped_to_the_dedicated_server(monkeypatch):
    target = TmuxServerTarget("/tmp/tmux-private/railmux", 789)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda: target
    )
    monkeypatch.setattr(
        "railmux.cli.tmux_server.is_current_server", lambda _target: False
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-user/default,456,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    observed = {}

    def recover():
        observed["tmux"] = os.environ.get("TMUX")
        observed["pane"] = os.environ.get("TMUX_PANE")
        return MagicMock(unresolved=0)

    monkeypatch.setattr(
        "railmux.display_transport.recover_interrupted_swaps", recover
    )
    monkeypatch.setattr(
        "railmux.cli.tmux_server.target_session_id", lambda *_a, **_kw: "$7"
    )
    run_client = MagicMock(return_value=0)
    monkeypatch.setattr(
        "railmux.cli._run_tmux_client_with_watchdog", run_client
    )

    assert main([]) == 0

    assert observed == {"tmux": "/tmp/tmux-private/railmux,789,0", "pane": None}
    assert run_client.call_args.kwargs == {
        "expected_target": target,
        "expected_session_id": "$7",
    }
    assert os.environ["TMUX"] == "/tmp/tmux-user/default,456,0"
    assert os.environ["TMUX_PANE"] == "%9"


def test_managed_windows_precreates_missing_outer_session_for_bridge(
    monkeypatch,
):
    target = TmuxServerTarget("/tmp/tmux-private/railmux", 789)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda: target)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.is_current_server", lambda _target: False)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.target_session_id", lambda *_a, **_kw: None)
    monkeypatch.setattr("railmux.cli.sys.argv", ["/opt/railmux/bin/railmux"])
    monkeypatch.setattr(
        "railmux.provider_paths.running_in_managed_windows_wrapper",
        lambda: True,
    )
    client_env = {
        "RAILMUX_WINDOWS_RUNTIME": "msys2",
        "WT_SESSION": "opaque-terminal-identity",
    }
    monkeypatch.setattr(
        "railmux.cli.tmux_server.exec_environment", lambda: client_env
    )
    monkeypatch.setattr(
        "railmux.cli._interactive_terminal_size", lambda: (164, 46))
    prepared = MagicMock(return_value="$9")
    monkeypatch.setattr(
        "railmux.cli.tmux_server.ensure_detached_launcher_session", prepared)
    run_client = MagicMock(return_value=0)
    monkeypatch.setattr(
        "railmux.cli._run_tmux_client_with_watchdog", run_client)

    assert main(["--project", "/work"]) == 0

    prepared.assert_called_once_with(
        target,
        ["/opt/railmux/bin/railmux"],
        ["--project", "/work"],
        env=client_env,
        initial_size=(164, 46),
    )
    assert run_client.call_args.kwargs == {
        "expected_target": target,
        "expected_session_id": "$9",
    }
    assert run_client.call_args.args[0][:7] == [
        "tmux", "-L", "railmux", "-T", "sync", "start-server", ";",
    ]
    assert "opaque-terminal-identity" not in run_client.call_args.args[0]


def test_posix_missing_outer_session_keeps_ordinary_new_session_path(
    monkeypatch,
):
    target = TmuxServerTarget("/tmp/tmux-private/railmux", 789)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda: target)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.is_current_server", lambda _target: False)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.target_session_id", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "railmux.provider_paths.running_in_managed_windows_wrapper",
        lambda: False,
    )
    monkeypatch.setattr(
        "railmux.cli.tmux_server.exec_environment",
        lambda: {"WT_SESSION": "must-not-enable-windows-features"},
    )
    prepared = MagicMock()
    monkeypatch.setattr(
        "railmux.cli.tmux_server.ensure_detached_launcher_session", prepared)
    run_client = MagicMock(return_value=0)
    monkeypatch.setattr(
        "railmux.cli._run_tmux_client_with_watchdog", run_client)

    assert main([]) == 0

    prepared.assert_not_called()
    assert run_client.call_args.kwargs == {
        "expected_target": target,
        "expected_session_id": None,
    }
    assert "-T" not in run_client.call_args.args[0]


def test_local_tmux_watchdog_exits_and_records_after_consecutive_failures(
    monkeypatch, capsys,
):
    class FrozenClient:
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    client = FrozenClient()
    monkeypatch.setattr("railmux.cli.subprocess.Popen", lambda *_a, **_k: client)
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("railmux.cli.time.sleep", lambda _seconds: None)
    times = iter((0.0, 5.0, 10.0, 15.0))
    monkeypatch.setattr("railmux.cli.time.monotonic", lambda: next(times))
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target",
        MagicMock(side_effect=tmux_server.TmuxServerUnresponsive("frozen")),
    )
    record = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.cli.tmux_health.record_incident", record)

    main_result = _run_tmux_client_with_watchdog(
        ["tmux", "-L", "railmux"], {}
    )
    assert main_result == 2
    assert client.terminated
    record.assert_called_once_with(
        component="launcher",
        reason="launcher-watchdog-timeout",
        consecutive_failures=3,
    )
    assert "stopped responding" in capsys.readouterr().err


def test_managed_windows_direct_attach_notifies_height_only_resize(monkeypatch):
    target = TmuxServerTarget("/tmp/railmux", 77)

    class DirectClient:
        pid = 321
        returncode = None

        def poll(self):
            return self.returncode

    client = DirectClient()
    monkeypatch.setattr(
        "railmux.cli.subprocess.Popen", lambda *_args, **_kwargs: client)
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("railmux.cli.sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("railmux.cli.termios.tcgetattr", lambda _fd: ["saved"])
    sizes = [os.terminal_size((120, 40)), os.terminal_size((120, 55))]
    monkeypatch.setattr(
        "railmux.cli.os.get_terminal_size",
        lambda _fd: sizes.pop(0) if len(sizes) > 1 else sizes[0],
    )

    def finish_after_poll(_seconds):
        client.returncode = 0

    monkeypatch.setattr("railmux.cli.time.sleep", finish_after_poll)
    monkeypatch.setattr(
        "railmux.provider_paths.running_in_managed_windows_wrapper",
        lambda: True,
    )
    notify = MagicMock()
    monkeypatch.setattr("railmux.cli.os.kill", notify)

    assert _run_tmux_client_with_watchdog(
        ["tmux", "-L", "railmux"], {}, expected_target=target,
        expected_session_id="$7",
    ) == 0
    notify.assert_called_once_with(321, signal.SIGWINCH)


def test_posix_direct_attach_does_not_poll_or_signal_resize(monkeypatch):
    class DirectClient:
        pid = 321
        returncode = None

        def poll(self):
            return self.returncode

    client = DirectClient()
    monkeypatch.setattr(
        "railmux.cli.subprocess.Popen", lambda *_args, **_kwargs: client)
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("railmux.cli.termios.tcgetattr", lambda _fd: ["saved"])

    def finish_after_poll(_seconds):
        client.returncode = 0

    monkeypatch.setattr("railmux.cli.time.sleep", finish_after_poll)
    monkeypatch.setattr(
        "railmux.provider_paths.running_in_managed_windows_wrapper",
        lambda: False,
    )
    get_size = MagicMock()
    notify = MagicMock()
    monkeypatch.setattr("railmux.cli.os.get_terminal_size", get_size)
    monkeypatch.setattr("railmux.cli.os.kill", notify)

    assert _run_tmux_client_with_watchdog(
        ["tmux", "-L", "railmux"], {},
    ) == 0
    get_size.assert_not_called()
    notify.assert_not_called()


def test_abrupt_tmux_client_exit_records_disappeared_server(monkeypatch):
    target = TmuxServerTarget("/tmp/railmux", 77)

    class ExitedClient:
        returncode = 1

        def poll(self):
            return self.returncode

    monkeypatch.setattr(
        "railmux.cli.subprocess.Popen", lambda *_args, **_kwargs: ExitedClient())
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("railmux.cli.tmux_server.discover_target", lambda **_kw: None)
    record = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.cli.tmux_health.record_incident", record)
    monkeypatch.setattr(
        "railmux.cli.tmux_health.consume_clean_exit", lambda **_kwargs: False)

    assert _run_tmux_client_with_watchdog(
        ["tmux", "-L", "railmux"], {}, expected_target=target,
        expected_session_id="$7",
    ) == 1
    record.assert_called_once_with(
        component="launcher",
        reason="launcher-server-exit",
        consecutive_failures=1,
    )


def test_intentional_hard_quit_does_not_record_launcher_incident(monkeypatch):
    target = TmuxServerTarget("/tmp/railmux", 77)

    class ExitedClient:
        returncode = 1

        def poll(self):
            return self.returncode

    monkeypatch.setattr(
        "railmux.cli.subprocess.Popen", lambda *_args, **_kwargs: ExitedClient())
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda **_kw: None)
    consume = MagicMock(return_value=True)
    record = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.cli.tmux_health.consume_clean_exit", consume)
    monkeypatch.setattr("railmux.cli.tmux_health.record_incident", record)

    assert _run_tmux_client_with_watchdog(
        ["tmux", "-L", "railmux"], {}, expected_target=target,
        expected_session_id="$7",
    ) == 1
    consume.assert_called_once_with(server_pid=77, session_id="$7")
    record.assert_not_called()


def test_posix_attach_rejection_does_not_record_windows_incident(monkeypatch):
    target = TmuxServerTarget("/tmp/railmux", 77)

    class ExitedClient:
        returncode = 1

        def poll(self):
            return self.returncode

    monkeypatch.setattr(
        "railmux.cli.subprocess.Popen", lambda *_args, **_kwargs: ExitedClient())
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda **_kw: target)
    monkeypatch.setattr(
        "railmux.provider_paths.running_in_managed_windows_wrapper",
        lambda: False,
    )
    monkeypatch.setattr(
        "railmux.cli.windows_tmux_lifecycle.recover_abandoned_socket",
        lambda: False,
    )
    record = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.cli.tmux_health.record_incident", record)

    assert _run_tmux_client_with_watchdog(
        ["tmux", "-L", "railmux"], {}, expected_target=target,
        expected_session_id="$7",
    ) == 1
    record.assert_not_called()


def test_terminal_recovery_resets_keyboard_focus_mouse_and_wrap(monkeypatch):
    write = MagicMock()
    monkeypatch.setattr("railmux.cli.os.write", write)

    _reset_terminal_modes(12)

    payload = write.call_args.args[1]
    assert b"\x1b[?1l\x1b>" in payload
    assert b"\x1b[?1004l" in payload
    assert b"\x1b[?7h" in payload


def test_managed_windows_fast_attach_rejection_uses_one_terminal_bridge(
    monkeypatch, capsys,
):
    target = TmuxServerTarget("/tmp/railmux", 77)

    class ExitedClient:
        returncode = 1

        def poll(self):
            return self.returncode

    class SuccessfulRelay:
        returncode = 0
        closed = False

        def poll(self):
            return self.returncode

        def close(self):
            self.closed = True

    relay = SuccessfulRelay()
    popen = MagicMock(return_value=ExitedClient())
    monkeypatch.setattr("railmux.cli.subprocess.Popen", popen)
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("railmux.cli.sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("railmux.cli.sys.stdin.fileno", lambda: 10)
    monkeypatch.setattr("railmux.cli.sys.stdout.fileno", lambda: 11)
    monkeypatch.setattr("railmux.cli.termios.tcgetattr", lambda _fd: ["saved"])
    monkeypatch.setattr("railmux.cli.termios.tcsetattr", lambda *_args: None)
    monkeypatch.setattr("railmux.cli.tty.setraw", lambda _fd: None)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda **_kw: target)
    monkeypatch.setattr(
        "railmux.provider_paths.running_in_managed_windows_wrapper",
        lambda: True,
    )
    start = MagicMock(return_value=relay)
    monkeypatch.setattr(
        "railmux.windows_attach_relay.start_relay_client", start)
    recover = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.cli.windows_tmux_lifecycle.recover_abandoned_socket", recover)
    record = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.cli.tmux_health.record_incident", record)

    assert _run_tmux_client_with_watchdog(
        ["tmux", "-L", "railmux"],
        {"RAILMUX_WINDOWS_RUNTIME": "msys2"},
        expected_target=target,
        expected_session_id="$7",
    ) == 0

    start.assert_called_once_with(
        target=target,
        session_id="$7",
        environ={"RAILMUX_WINDOWS_RUNTIME": "msys2"},
        stdin_fd=10,
        stdout_fd=11,
    )
    assert relay.closed
    recover.assert_called_once_with()
    record.assert_not_called()
    assert popen.call_args.kwargs["stderr"] is subprocess.DEVNULL
    stderr = capsys.readouterr().err
    assert "Windows terminal bridge" in stderr
    assert "direct tmux attach was unavailable" not in stderr
    assert "open terminal failed" not in stderr
    assert "cleaned an abandoned" not in stderr


def test_managed_windows_attach_without_bridge_is_actionable(
    monkeypatch, capsys,
):
    target = TmuxServerTarget("/tmp/railmux", 77)

    class ExitedClient:
        returncode = 1

        def poll(self):
            return self.returncode

    popen = MagicMock(return_value=ExitedClient())
    monkeypatch.setattr("railmux.cli.subprocess.Popen", popen)
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda **_kw: target)
    monkeypatch.setattr(
        "railmux.provider_paths.running_in_managed_windows_wrapper",
        lambda: True,
    )
    record = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.cli.tmux_health.record_incident", record)

    assert _run_tmux_client_with_watchdog(
        ["tmux", "-L", "railmux"],
        {"RAILMUX_WINDOWS_RUNTIME": "msys2"},
        expected_target=target,
        expected_session_id="$7",
    ) == 1

    assert popen.call_args.kwargs["stderr"] is subprocess.DEVNULL
    record.assert_called_once_with(
        component="launcher",
        reason="launcher-attach-rejected",
        consecutive_failures=1,
    )
    stderr = capsys.readouterr().err
    assert "bridge was unavailable" in stderr
    assert "run 'railmux doctor'" in stderr


def test_invalid_config_is_actionable_without_traceback(
    monkeypatch, tmp_path, capsys,
):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(
        "railmux.cli.load_config",
        MagicMock(side_effect=ConfigError("invalid TOML")),
    )
    monkeypatch.setattr(
        "railmux.cli.default_config_path", lambda: config_path)

    with patch("railmux.ui.app.App") as app_cls:
        result = main(["--inside-tmux"])

    stderr = capsys.readouterr().err
    assert result == 2
    assert "invalid TOML" in stderr
    assert "Traceback" not in stderr
    app_cls.assert_not_called()


def test_startup_message_paints_and_flushes_only_on_a_tty(monkeypatch):
    output = MagicMock()
    output.isatty.return_value = True
    monkeypatch.setattr("railmux.cli.sys.stdout", output)

    _show_startup_message()

    surface = output.write.call_args.args[0]
    assert surface.startswith("\033[2J\033[H")
    assert "RAILMUX" in surface
    assert "Restoring your workspace" in surface
    assert "Reconnecting sessions and panes…" in surface
    output.flush.assert_called_once_with()

    output.reset_mock()
    output.isatty.return_value = False
    _show_startup_message()
    output.write.assert_not_called()
