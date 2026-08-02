from railmux.winlocal.session_store import SessionRecord, SessionStore


def test_foreign_daemon_record_becomes_resume_offer(tmp_path):
    path = tmp_path / "sessions.json"
    old = SessionStore(path, "old-daemon")
    old.save((SessionRecord(
        record_id="r1",
        provider="codex",
        cwd=r"C:\work",
        phase="resolved",
        daemon_id="old-daemon",
        provider_session_id="session-1",
        pid=123,
    ),))

    records = SessionStore(path, "new-daemon").load()

    assert len(records) == 1
    assert records[0].phase == "resume_offer"
    assert records[0].pid is None
    assert records[0].provider_session_id == "session-1"


def test_same_daemon_keeps_live_identity(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path, "daemon")
    expected = SessionRecord(
        record_id="r1",
        provider="claude",
        cwd=r"C:\repo",
        phase="launching",
        daemon_id="daemon",
        pid=321,
    )
    store.save((expected,))

    loaded = store.load()[0]

    assert loaded.phase == "launching"
    assert loaded.pid == 321

