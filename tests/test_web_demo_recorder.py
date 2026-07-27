"""Safety and determinism checks for the public website recorder."""

from __future__ import annotations

import socket

from tools import record_web_demo
from railmux.ui.workspace import (
    WorkspacePresentation,
    presentation_for_geometry,
)


def test_public_output_scrubs_machine_specific_values_without_resizing() -> None:
    label = b"railmux-web-demo-123-workflow"
    private = (
        b"Project: /tmp/railmux-web-demo-ab12/projects/railmux "
        + socket.gethostname().encode()
        + b" "
        + label
    )

    public = record_web_demo._sanitize_public_output(
        private,
        b"/demo/railmux-web-workspace-v1",
        label,
    )

    assert len(public) == len(private)
    assert b"/tmp/railmux-web-demo-" not in public
    assert socket.gethostname().encode() not in public
    assert label not in public
    assert str(record_web_demo.ROOT).encode() not in public
    assert b"/demo/railmux-web-work" in public
    assert b"demo-host" in public
    assert b"demo-socket" in public


def test_fixture_environment_excludes_provider_credentials(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-pass")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-pass")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/private/claude")
    monkeypatch.setenv("CODEX_HOME", "/private/codex")

    _claude_home, env = record_web_demo._create_fixture(tmp_path)

    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "CODEX_HOME" not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "home" / ".config")

    demo_agent = (tmp_path / "bin" / "demo-agent").read_text(encoding="utf-8")
    assert "read-only source analysis" in demo_agent
    assert "sanitized transcript replay" in demo_agent
    assert "no provider session persisted" in demo_agent
    assert "ANTHROPIC_API_KEY" not in demo_agent
    assert "OPENAI_API_KEY" not in demo_agent


def test_only_mobile_demo_uses_compact_presentation() -> None:
    for profile in (record_web_demo.DESKTOP, record_web_demo.WORKFLOW):
        assert (
            presentation_for_geometry(
                WorkspacePresentation.WIDE,
                profile.width,
                profile.height,
            )
            is WorkspacePresentation.WIDE
        )
    assert (
        presentation_for_geometry(
            WorkspacePresentation.WIDE,
            record_web_demo.MOBILE.width,
            record_web_demo.MOBILE.height,
        )
        is WorkspacePresentation.COMPACT
    )


def test_cast_profiles_include_auditable_agent_transcript() -> None:
    capture, digest = record_web_demo._load_agent_runs()

    assert capture["source_commit"].startswith("f53145d")
    assert capture["capture_method"].startswith("Claude Code")
    assert len(capture["runs"]) == 2
    assert len(digest) == 64
    raw = record_web_demo.REAL_AGENT_RUNS.read_bytes()
    assert all(
        fragment not in raw
        for fragment in record_web_demo.FORBIDDEN_TRANSCRIPT_FRAGMENTS
    )
