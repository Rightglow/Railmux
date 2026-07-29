from __future__ import annotations

import io
from unittest.mock import MagicMock

from railmux import __version__, fast_display_client
from railmux.fast_display_protocol import PROTOCOL_VERSION
from railmux.ssh_doctor import (
    collect_remote_ssh_snapshot,
    render_remote_ssh_text,
    run_remote_ssh_doctor,
)


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

    assert run_remote_ssh_doctor(
        destination,
        stdout=output,
        json_output=True,
    ) == 2
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
