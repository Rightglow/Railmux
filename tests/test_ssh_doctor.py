from __future__ import annotations

import io
from unittest.mock import MagicMock

from railmux import __version__, fast_display_client
from railmux.fast_display_protocol import PROTOCOL_VERSION
from railmux.ssh_doctor import (
    RemoteSshDoctorSnapshot,
    collect_remote_ssh_snapshot,
    render_remote_ssh_text,
    render_remote_ssh_terminal_text,
    run_remote_ssh_doctor,
)


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_remote_ssh_doctor_reads_hello_without_attaching_or_leaking_host(
    monkeypatch,
):
    destination = "private-user@secret-host"
    process = MagicMock()
    built = MagicMock(return_value=["ssh", destination])
    stopped = MagicMock()
    monkeypatch.setattr("railmux.ssh_doctor.shutil.which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(fast_display_client, "build_ssh_argv", built)
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(fast_display_client, "_stop_unstarted_remote", stopped)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout: fast_display_client.RemoteStartup(
            fast_display_client.RemoteStartKind.HELLO,
            fast_display_client.RemoteHello(
                __version__,
                PROTOCOL_VERSION,
                True,
                True,
            ),
        ),
    )

    snapshot = collect_remote_ssh_snapshot(destination, ssh_args=("-J", "jump"))
    rendered = render_remote_ssh_text(snapshot)

    assert snapshot.status == "ready"
    assert snapshot.compatible
    assert snapshot.read_only
    assert destination not in rendered
    assert "no session was attached, created, resized, or replaced" in rendered
    assert built.call_args.kwargs["existing_session_only"] is True
    assert built.call_args.kwargs["ssh_args"] == ("-J", "jump")
    stopped.assert_called_once_with(process)


def test_remote_ssh_doctor_json_failure_is_scriptable_and_private(monkeypatch):
    destination = "secret-host"
    process = MagicMock()
    monkeypatch.setattr("railmux.ssh_doctor.shutil.which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(
        fast_display_client,
        "build_ssh_argv",
        lambda *_args, **_kwargs: ["ssh", destination],
    )
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(fast_display_client, "_stop_unstarted_remote", lambda _p: None)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout: fast_display_client.RemoteStartup(
            fast_display_client.RemoteStartKind.FAILED,
            returncode=255,
        ),
    )
    output = io.StringIO()

    assert (
        run_remote_ssh_doctor(
            destination,
            stdout=output,
            json_output=True,
        )
        == 2
    )
    payload = output.getvalue()
    assert '"status": "connection_failed"' in payload
    assert destination not in payload


def test_remote_ssh_doctor_treats_same_protocol_version_drift_as_usable(
    monkeypatch,
):
    process = MagicMock()
    monkeypatch.setattr("railmux.ssh_doctor.shutil.which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(
        fast_display_client,
        "build_ssh_argv",
        lambda *_args, **_kwargs: ["ssh", "host"],
    )
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(fast_display_client, "_stop_unstarted_remote", lambda _p: None)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout: fast_display_client.RemoteStartup(
            fast_display_client.RemoteStartKind.HELLO,
            fast_display_client.RemoteHello(
                "0.1.0",
                PROTOCOL_VERSION,
                True,
                True,
            ),
        ),
    )

    snapshot = collect_remote_ssh_snapshot("host")

    assert snapshot.status == "ready_with_version_difference"
    assert snapshot.compatible


def test_remote_ssh_doctor_reports_invalid_remote_config(monkeypatch):
    process = MagicMock()
    monkeypatch.setattr("railmux.ssh_doctor.shutil.which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(
        fast_display_client,
        "build_ssh_argv",
        lambda *_args, **_kwargs: ["ssh", "host"],
    )
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    monkeypatch.setattr(fast_display_client, "_stop_unstarted_remote", lambda _p: None)
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process, timeout: fast_display_client.RemoteStartup(
            fast_display_client.RemoteStartKind.HELLO,
            fast_display_client.RemoteHello(
                __version__,
                PROTOCOL_VERSION,
                True,
                False,
                "invalid",
                True,
            ),
        ),
    )

    snapshot = collect_remote_ssh_snapshot("host")

    assert snapshot.status == "config_invalid"
    assert not snapshot.compatible
    assert "railmux config" in (snapshot.detail or "")


def test_remote_doctor_progress_is_transient_and_private(monkeypatch):
    stdout = _TTYBuffer()
    stderr = _TTYBuffer()
    monkeypatch.setenv("NO_COLOR", "1")
    snapshot = MagicMock(compatible=True)
    monkeypatch.setattr(
        "railmux.ssh_doctor.collect_remote_ssh_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        "railmux.ssh_doctor.render_remote_ssh_text",
        lambda _snapshot: "doctor result",
    )

    result = run_remote_ssh_doctor(
        "private-host",
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0
    assert stdout.getvalue() == "doctor result\n"
    assert stderr.getvalue() == (
        "\r\033[2Krailmux doctor: Checking remote SSH compatibility…\r\033[2K"
    )
    assert "private-host" not in stderr.getvalue()


def test_remote_doctor_json_never_emits_progress(monkeypatch):
    stdout = _TTYBuffer()
    stderr = _TTYBuffer()
    monkeypatch.setattr(
        "railmux.ssh_doctor.collect_remote_ssh_snapshot",
        lambda *_args, **_kwargs: RemoteSshDoctorSnapshot(
            2,
            "connection_failed",
            __version__,
            PROTOCOL_VERSION,
        ),
    )

    result = run_remote_ssh_doctor(
        "private-host",
        stdout=stdout,
        stderr=stderr,
        json_output=True,
    )

    assert result == 2
    assert stderr.getvalue() == ""
    assert '"status": "connection_failed"' in stdout.getvalue()


def test_remote_doctor_terminal_report_colors_only_interactive_output(monkeypatch):
    snapshot = RemoteSshDoctorSnapshot(
        2,
        "ready",
        __version__,
        PROTOCOL_VERSION,
        remote_version=__version__,
        remote_protocol=PROTOCOL_VERSION,
        compatible=True,
    )
    stream = _TTYBuffer()
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)

    rendered = render_remote_ssh_terminal_text(snapshot, stream)

    assert "\033[" in rendered
    assert "Compatible now:" in rendered
    assert render_remote_ssh_terminal_text(snapshot, io.StringIO()) == (
        render_remote_ssh_text(snapshot)
    )
