import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from railmux.diagnostics import (
    TmuxServerDiagnostic,
    _dedicated_tmux_diagnostic,
    _tool_diagnostic,
    collect_doctor_snapshot,
    run_doctor,
)
from railmux.ssh_display_diagnostics import (
    SshDisplayDiagnostic,
    SshDisplayStats,
)
from railmux.tmux_health import TmuxIncident


class _TTYStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def test_version_preserves_tmux_letter_suffix(monkeypatch):
    monkeypatch.setattr(
        "railmux.diagnostics.shutil.which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr(
        "railmux.diagnostics.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="tmux 3.2a", stderr="", returncode=0),
    )

    diagnostic = _tool_diagnostic("tmux", "-V")
    assert diagnostic.status == "available"
    assert diagnostic.version == "3.2a"


def test_windows_doctor_allows_stale_socket_settle_without_changing_posix(
    monkeypatch,
):
    observed = []
    monkeypatch.setattr(
        "railmux.diagnostics.shutil.which", lambda *_args, **_kwargs: "tmux")

    def discover(*, timeout, env):
        observed.append((timeout, env))
        return None

    monkeypatch.setattr(
        "railmux.diagnostics.tmux_server.discover_target", discover)
    posix_env = {"PATH": "/bin"}
    windows_env = {
        "PATH": "/usr/bin",
        "RAILMUX_WINDOWS_RUNTIME": "msys2",
    }

    assert _dedicated_tmux_diagnostic(posix_env).status == "not_running"
    assert _dedicated_tmux_diagnostic(windows_env).status == "not_running"
    assert observed == [(1.0, posix_env), (None, windows_env)]


def test_doctor_report_is_useful_and_redacts_user_values(
    monkeypatch, tmp_path,
):
    home = tmp_path / "private-user"
    config_dir = home / ".config" / "railmux"
    config_dir.mkdir(parents=True)
    secret_root = tmp_path / "company-secret-project"
    secret_binary = secret_root / "sk-secret-token" / "claude"
    config_dir.joinpath("config.toml").write_text(
        "[claude]\n"
        f"binary = '{secret_binary}'\n"
        "[codex]\n"
        "binary = 'private-codex-wrapper'\n"
        f"home = '{secret_root}'\n"
    )
    claude_home = secret_root / "claude-data"
    claude_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "railmux.diagnostics.shutil.which", lambda binary: str(binary))
    monkeypatch.setattr(
        "railmux.diagnostics.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=(
                "tool 9.8.7 /home/private-user "
                "12345678-1234-1234-1234-123456789abc sk-secret-token"
            ),
            stderr="private-host company-secret-project",
            returncode=0,
        ),
    )
    monkeypatch.setattr(
        "railmux.diagnostics._dedicated_tmux_diagnostic",
        lambda: TmuxServerDiagnostic("healthy", context="outside"),
    )
    monkeypatch.setattr(
        "railmux.diagnostics.tmux_health.read_last_incident",
        lambda: TmuxIncident(100, "remote-display",
                             "remote-display-watchdog-timeout", 3),
    )
    monkeypatch.setattr(
        "railmux.diagnostics.tmux_health.incident_age",
        lambda _recorded: "2 minutes ago",
    )
    monkeypatch.setattr(
        "railmux.diagnostics.read_ssh_display_diagnostic",
        lambda: SshDisplayDiagnostic(
            status="recorded",
            client_version="0.2.18",
            protocol=12,
            phase="finished",
            outcome="remote_detach",
            age="2_minutes",
            stats=SshDisplayStats(
                frames=8,
                painted_rows=20,
                wire_bytes=1234,
                reconnect_attempts=2,
                reconnect_successes=1,
                history_prefetch_requests=3,
                history_deep_requests=1,
            ),
        ),
    )
    output = StringIO()

    assert run_doctor(
        claude_home=claude_home,
        stdout=output,
        environ={
            "TMUX": "/private/socket,123,0",
            "SSH_CONNECTION": "private-client private-host",
            "TERM": "tmux-256color-private",
            "COLORTERM": "truecolor-private",
        },
    ) == 0

    report = output.getvalue()
    assert "Railmux diagnostics" in report
    assert "Claude Code: 9.8.7" in report
    assert "Inside tmux: yes" in report
    assert "Dedicated Railmux tmux: healthy" in report
    assert "Tmux watchdog: enabled" in report
    assert "SSH display watchdog timeout; 3 consecutive failures; " \
        "2 minutes ago" in report
    assert "SSH transport: yes" in report
    assert "256-colour=yes" in report
    assert "true-colour=no" in report
    assert "Config: ~/.config/railmux/config.toml; valid=yes" in report
    assert "Preferred agent display: swap" in report
    assert "Most recent railmux ssh (host not recorded)" in report
    assert "remote_detach" in report
    assert "frames=8" in report
    assert "reconnects=1/2" in report
    assert "Claude data: <custom>" in report
    assert "Privacy:" in report
    assert "review before sharing" in report
    for secret in (
        str(home), "private-user", "private-host", "private-client",
        "company-secret-project", "sk-secret-token",
        "12345678-1234-1234-1234-123456789abc",
        "/private/socket", "private-codex-wrapper",
    ):
        assert secret not in report


def test_local_doctor_progress_is_transient(monkeypatch):
    stdout = _TTYStringIO()
    stderr = _TTYStringIO()
    snapshot = MagicMock()
    monkeypatch.setattr(
        "railmux.diagnostics.collect_doctor_snapshot",
        lambda **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        "railmux.diagnostics.render_doctor_terminal_text",
        lambda _snapshot, _stream: "doctor result",
    )

    result = run_doctor(
        claude_home=Path("unused"),
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0
    assert stdout.getvalue() == "doctor result\n"
    assert "Collecting local diagnostics" in stderr.getvalue()
    assert stderr.getvalue().endswith("\r\033[2K")


def test_doctor_reports_missing_tools_and_invalid_config(
    monkeypatch, tmp_path,
):
    home = tmp_path / "home"
    config_dir = home / ".config" / "railmux"
    config_dir.mkdir(parents=True)
    config_dir.joinpath("config.toml").write_text("[broken")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("railmux.diagnostics.shutil.which", lambda _binary: None)
    monkeypatch.setattr(
        "railmux.diagnostics._dedicated_tmux_diagnostic",
        lambda: TmuxServerDiagnostic("unavailable"),
    )
    monkeypatch.setattr(
        "railmux.diagnostics.tmux_health.read_last_incident", lambda: None
    )
    output = StringIO()

    assert run_doctor(
        claude_home=home / ".claude", stdout=output, environ={}) == 0

    report = output.getvalue()
    assert "tmux: not found" in report
    assert "Dedicated Railmux tmux: unavailable" in report
    assert "Last tmux incident: none recorded" in report
    assert "Claude Code: not found" in report
    assert "Codex: not found" in report
    assert "valid=no (invalid TOML)" in report
    assert "file=absent" not in report


def test_doctor_never_falls_back_when_configured_tmux_is_missing(
    monkeypatch, tmp_path,
):
    home = tmp_path / "home"
    config_dir = home / ".config" / "railmux"
    config_dir.mkdir(parents=True)
    config_dir.joinpath("config.toml").write_text(
        '[tmux]\nbinary = "/missing/bin/tmux"\n'
    )
    monkeypatch.setenv("HOME", str(home))
    probe = MagicMock()
    monkeypatch.setattr("railmux.diagnostics._dedicated_tmux_diagnostic", probe)
    monkeypatch.setattr("railmux.diagnostics._legacy_tmux_diagnostic", probe)

    snapshot = collect_doctor_snapshot(claude_home=home / ".claude")

    assert snapshot.tools["tmux"].status == "missing"
    assert snapshot.dedicated_tmux.status == "unavailable"
    probe.assert_not_called()


def test_doctor_json_uses_versioned_redacted_snapshot(monkeypatch, tmp_path):
    home = tmp_path / "private-user"
    config_dir = home / ".config" / "railmux"
    config_dir.mkdir(parents=True)
    secret_root = tmp_path / "company-secret-project"
    config_dir.joinpath("config.toml").write_text(
        "[claude]\n"
        f"binary = '{secret_root}/sk-secret-token/claude'\n"
        "[codex]\n"
        "binary = 'private-codex-wrapper'\n"
        f"home = '{secret_root}'\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "railmux.diagnostics.shutil.which", lambda binary: str(binary)
    )
    monkeypatch.setattr(
        "railmux.diagnostics.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=(
                "tool 9.8.7 private-host "
                "12345678-1234-1234-1234-123456789abc sk-secret-token"
            ),
            stderr=str(secret_root),
            returncode=0,
        ),
    )
    monkeypatch.setattr(
        "railmux.diagnostics._dedicated_tmux_diagnostic",
        lambda: TmuxServerDiagnostic("healthy", context="outside"),
    )
    monkeypatch.setattr(
        "railmux.diagnostics._legacy_tmux_diagnostic",
        lambda: TmuxServerDiagnostic(
            "healthy", candidate_count=2, restart_recommended=True
        ),
    )
    monkeypatch.setattr(
        "railmux.diagnostics.tmux_health.read_last_incident",
        lambda: TmuxIncident(
            100, "remote-display", "remote-display-watchdog-timeout", 3
        ),
    )
    monkeypatch.setattr(
        "railmux.diagnostics.tmux_health.incident_age",
        lambda _recorded: "2 minutes ago",
    )
    monkeypatch.setattr(
        "railmux.diagnostics.read_ssh_display_diagnostic",
        lambda: SshDisplayDiagnostic(
            status="recorded",
            client_version="0.2.18",
            protocol=12,
            phase="finished",
            outcome="local_disconnect",
            age="under_1_minute",
            stats=SshDisplayStats(frames=5, wire_bytes=900),
        ),
    )
    output = StringIO()

    assert run_doctor(
        claude_home=secret_root / "claude-data",
        stdout=output,
        environ={
            "TMUX": "/private/socket,123,0",
            "SSH_CONNECTION": "private-client private-host",
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
        },
        json_output=True,
    ) == 0

    payload = json.loads(output.getvalue())
    assert payload["schema_version"] == 3
    assert payload["locale_configured"] is False
    assert set(payload["ssh_display"]) == {
        "age",
        "client_version",
        "outcome",
        "phase",
        "protocol",
        "stats",
        "status",
    }
    assert set(payload["ssh_display"]["stats"]) == {
        "duration_ms",
        "first_frame_ms",
        "frames",
        "history_anchor_rejects",
        "history_deep_requests",
        "history_prefetch_requests",
        "history_timeouts",
        "keyframes",
        "painted_rows",
        "patches",
        "reached_first_frame",
        "reconnect_attempts",
        "reconnect_successes",
        "wire_bytes",
    }
    assert payload["ssh_display"]["outcome"] == "local_disconnect"
    assert payload["dedicated_tmux"] == {
        "candidate_count": None,
        "context": "outside",
        "restart_recommended": False,
        "status": "healthy",
    }
    assert payload["legacy_tmux"]["candidate_count"] == 2
    assert payload["last_tmux_incident"]["category"] == (
        "remote-display-watchdog-timeout"
    )
    assert payload["tools"]["claude_code"] == {
        "configured": True,
        "status": "available",
        "version": "9.8.7",
    }
    assert payload["data_directories"]["claude"]["path"] == "<custom>"
    encoded = output.getvalue()
    for secret in (
        str(home),
        str(secret_root),
        "private-user",
        "private-host",
        "private-client",
        "sk-secret-token",
        "12345678-1234-1234-1234-123456789abc",
        "/private/socket",
        "private-codex-wrapper",
    ):
        assert secret not in encoded
