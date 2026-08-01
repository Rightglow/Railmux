from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import MagicMock

from railmux import remote_config
from railmux.fast_display_client import (
    RemoteHello,
    RemoteStartKind,
    RemoteStartup,
    build_remote_command_argv,
)
from railmux.fast_display_protocol import PROTOCOL_VERSION, REMOTE_CONFIG_PROTOCOL


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def _hello(*, config_protocol: int = REMOTE_CONFIG_PROTOCOL) -> RemoteStartup:
    return RemoteStartup(
        RemoteStartKind.HELLO,
        RemoteHello(
            "1.2.3",
            PROTOCOL_VERSION,
            True,
            config_protocol=config_protocol,
        ),
    )


def test_remote_command_uses_shared_discovery_and_forced_tty():
    argv = build_remote_command_argv(
        "work",
        remote_args=("config", "--remote-context"),
        ssh_args=("-J", "jump"),
        force_tty=True,
    )

    assert argv[:5] == ["ssh", "-J", "jump", "-tt", "work"]
    assert "remote-server" not in argv[-1]
    assert "config --remote-context" in argv[-1]
    assert "command -v railmux" in argv[-1]


def test_compatible_probe_stops_before_start_token(monkeypatch):
    process = MagicMock()
    stopped = []
    monkeypatch.setattr(
        remote_config,
        "_start_probe",
        lambda *_args, **_kwargs: (process, _hello()),
    )
    monkeypatch.setattr(
        remote_config,
        "_stop_unstarted_remote",
        lambda candidate: stopped.append(candidate),
    )

    remote_config._ensure_remote_config_cli("work", (), ("--remote", "work"))

    assert stopped == [process]
    process.stdin.write.assert_not_called()


def test_remote_editor_can_repair_invalid_remote_config(monkeypatch):
    process = MagicMock()
    startup = RemoteStartup(
        RemoteStartKind.HELLO,
        RemoteHello(
            "1.2.3",
            PROTOCOL_VERSION,
            True,
            config_status="invalid",
            config_protocol=REMOTE_CONFIG_PROTOCOL,
        ),
    )
    monkeypatch.setattr(
        remote_config,
        "_start_probe",
        lambda *_args, **_kwargs: (process, startup),
    )
    stopped = []
    monkeypatch.setattr(
        remote_config,
        "_stop_unstarted_remote",
        lambda candidate: stopped.append(candidate),
    )

    remote_config._ensure_remote_config_cli("work", (), ("--remote", "work"))

    assert stopped == [process]


def test_missing_remote_can_install_without_tmux_attach(monkeypatch):
    calls = []
    processes = [MagicMock(), MagicMock()]
    startups = [
        RemoteStartup(RemoteStartKind.MISSING, returncode=127),
        _hello(),
    ]

    def start(_destination, _ssh_args, *, install=None):
        calls.append(install)
        return processes[len(calls) - 1], startups[len(calls) - 1]

    monkeypatch.setattr(remote_config, "_start_probe", start)
    monkeypatch.setattr(remote_config, "_confirm", lambda _question: True)
    monkeypatch.setattr(remote_config, "_stop_unstarted_remote", lambda _process: None)

    remote_config._ensure_remote_config_cli("work", (), ("--remote", "work"))

    assert calls == [None, "user"]
    for process in processes:
        process.stdin.write.assert_not_called()


def test_remote_context_upgrade_restarts_config_subcommand(monkeypatch):
    hello = RemoteHello(
        "999.0",
        PROTOCOL_VERSION,
        True,
        config_protocol=REMOTE_CONFIG_PROTOCOL + 1,
    )
    observed = {}
    monkeypatch.setattr(remote_config, "_confirm", lambda _question: True)

    def restart(version, raw_argv, *, subcommand):
        observed.update(
            version=version,
            raw_argv=tuple(raw_argv),
            subcommand=subcommand,
        )
        raise RuntimeError("reexec")

    monkeypatch.setattr(remote_config, "_upgrade_local_and_restart", restart)

    try:
        remote_config._validate_config_protocol(
            hello,
            raw_argv=("--remote", "work"),
        )
    except RuntimeError as exc:
        assert str(exc) == "reexec"
    else:
        raise AssertionError("upgrade path did not restart")
    assert observed == {
        "version": "999.0",
        "raw_argv": ("--remote", "work"),
        "subcommand": "config",
    }


def test_run_remote_config_launches_cooked_editor(monkeypatch):
    monkeypatch.setattr(remote_config.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(remote_config.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(remote_config.shutil, "which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(
        remote_config,
        "_ensure_remote_config_cli",
        lambda *_args, **_kwargs: None,
    )
    observed = {}

    def run(argv, *, check):
        observed["argv"] = argv
        observed["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(remote_config.subprocess, "run", run)

    result = remote_config.run_remote_config(
        "work",
        ssh_args=("-p", "2222"),
        raw_argv=("--remote", "work"),
    )

    assert result == 0
    assert observed["argv"][:5] == ["ssh", "-p", "2222", "-tt", "work"]
    assert "config --remote-context" in observed["argv"][-1]
    assert observed["check"] is False


def test_remote_config_progress_is_transient(monkeypatch):
    stderr = _TTYBuffer()
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(remote_config.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(remote_config.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(remote_config.sys, "stderr", stderr)
    monkeypatch.setattr(remote_config.shutil, "which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(
        remote_config,
        "_ensure_remote_config_cli",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        remote_config.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    result = remote_config.run_remote_config(
        "private-host",
        ssh_args=(),
        raw_argv=("--remote", "private-host"),
    )

    assert result == 0
    assert stderr.getvalue() == (
        "\r\033[2Krailmux config: Connecting and checking remote Railmux…"
        "\r\033[2Krailmux config: Opening remote settings…"
        "\r\033[2K"
    )
    assert "private-host" not in stderr.getvalue()


def test_remote_config_clears_progress_before_install_prompt(monkeypatch):
    status = MagicMock()
    process = MagicMock()
    startup = RemoteStartup(RemoteStartKind.MISSING, returncode=127)
    monkeypatch.setattr(
        remote_config,
        "_start_probe",
        lambda *_args, **_kwargs: (process, startup),
    )
    monkeypatch.setattr(remote_config, "_stop_unstarted_remote", lambda _process: None)
    monkeypatch.setattr(remote_config, "_confirm", lambda _question: False)

    try:
        remote_config._ensure_remote_config_cli(
            "work",
            (),
            ("--remote", "work"),
            status=status,
        )
    except remote_config.ProbeError:
        pass
    else:
        raise AssertionError("declining installation did not stop remote config")

    status.clear.assert_called_once_with()


def test_remote_config_clears_progress_before_error(monkeypatch):
    stderr = _TTYBuffer()
    monkeypatch.setattr(remote_config.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(remote_config.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(remote_config.sys, "stderr", stderr)
    monkeypatch.setattr(remote_config.shutil, "which", lambda _name: "/usr/bin/ssh")

    def fail_probe(*_args, **_kwargs):
        raise remote_config.ProbeError("probe failed")

    monkeypatch.setattr(
        remote_config,
        "_ensure_remote_config_cli",
        fail_probe,
    )

    result = remote_config.run_remote_config(
        "private-host",
        ssh_args=(),
        raw_argv=("--remote", "private-host"),
    )

    assert result == 2
    assert "\r\033[2Kerror: probe failed\n" in stderr.getvalue()
    assert "private-host" not in stderr.getvalue()
