import json

from railmux import restart_state
from railmux.ssh_display_diagnostics import (
    SshDisplayRecorder,
    SshDisplayStats,
    read_diagnostic,
)


def test_record_is_private_and_doctor_view_omits_internal_identity_and_time(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(restart_state, "runtime_state_dir", lambda: tmp_path)
    recorder = SshDisplayRecorder("2.3.4", 12)
    recorder.mark_attached()
    recorder.finish(
        "local_disconnect",
        SshDisplayStats(
            reached_first_frame=True,
            first_frame_ms=42,
            frames=8,
            wire_bytes=1234,
            history_prefetch_requests=3,
        ),
    )

    raw = json.loads((tmp_path / "ssh-display.json").read_text())
    assert "token" in raw
    assert "destination" not in raw
    assert "session" not in raw
    diagnostic = read_diagnostic(now=raw["recorded_at"] + 90)
    encoded = json.dumps(diagnostic, default=lambda value: value.__dict__)
    assert diagnostic.outcome == "local_disconnect"
    assert diagnostic.age == "1_minutes"
    assert diagnostic.stats is not None
    assert diagnostic.stats.history_prefetch_requests == 3
    assert "token" not in encoded
    assert "recorded_at" not in encoded
    assert "started_at" not in encoded


def test_older_connection_cannot_overwrite_newer_connection_result(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(restart_state, "runtime_state_dir", lambda: tmp_path)
    older = SshDisplayRecorder("1.0", 10)
    newer = SshDisplayRecorder("2.0", 10)
    older.mark_attached()
    newer.mark_attached()

    older.finish("transport_failed", SshDisplayStats(frames=99))
    assert read_diagnostic().client_version == "2.0"
    assert read_diagnostic().phase == "in_progress"

    newer.finish("remote_detach", SshDisplayStats(frames=5))
    diagnostic = read_diagnostic()
    assert diagnostic.client_version == "2.0"
    assert diagnostic.outcome == "remote_detach"
    assert diagnostic.stats is not None
    assert diagnostic.stats.frames == 5


def test_older_startup_failure_cannot_overwrite_newer_attach(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(restart_state, "runtime_state_dir", lambda: tmp_path)
    timestamps = iter((100.0, 200.0, 200.0, 300.0))
    monkeypatch.setattr(
        "railmux.ssh_display_diagnostics.time.time",
        lambda: next(timestamps),
    )
    older = SshDisplayRecorder("1.0", 10)
    newer = SshDisplayRecorder("2.0", 10)
    newer.mark_attached()

    older.finish("startup_failed", SshDisplayStats())

    diagnostic = read_diagnostic(now=300.0)
    assert diagnostic.client_version == "2.0"
    assert diagnostic.phase == "in_progress"
