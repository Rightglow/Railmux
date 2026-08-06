from __future__ import annotations

import os

from railmux import preview_pager


def test_pager_receives_complete_seekable_render_before_start(monkeypatch):
    def render(argv):
        assert argv == [
            "transcript", "--format", "codex", "--preview-limit", "2",
            "/tmp/session.jsonl",
        ]
        print("first")
        print("final")

    observed = {}

    def run(argv, *, stdin, env):
        observed["argv"] = argv
        observed["seekable"] = stdin.seekable()
        observed["payload"] = stdin.read()
        observed["env"] = env
        return 0

    monkeypatch.setattr(preview_pager.transcript, "main", render)
    monkeypatch.setattr(preview_pager.subprocess, "call", run)

    assert preview_pager.main([
        "--mouse", "--format", "codex", "--preview-limit", "2",
        "/tmp/session.jsonl",
    ]) == 0

    assert observed["seekable"] is True
    assert observed["payload"] == "first\nfinal\n"
    assert observed["argv"] == [
        "less", "-R", "+G", "--mouse", "--wheel-lines=3"]
    assert observed["env"]["LESSSECURE"] == "1"
    assert observed["env"]["LESSHISTFILE"] == "-"
    assert observed["env"]["LESSOPEN"] == ""
    assert observed["env"]["LESSCLOSE"] == ""
    assert observed["env"] is not os.environ
