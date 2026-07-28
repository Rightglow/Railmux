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
    assert "shift+tab to cycle" in demo_agent
    assert "RESUMED_SESSIONS" in demo_agent
    assert "❯" in demo_agent
    assert "●" in demo_agent
    assert "SIGWINCH" in demo_agent
    assert "Claude Code v2.1.220" in demo_agent
    assert "OpenAI Codex (v0.145.0)" in demo_agent
    assert "ANTHROPIC_API_KEY" not in demo_agent
    assert "OPENAI_API_KEY" not in demo_agent
    assert (tmp_path / "bin" / "demo-codex").is_symlink()
    assert (
        tmp_path
        / "home"
        / ".codex"
        / "sessions"
        / "2026"
        / "07"
        / "28"
    ).is_dir()


def test_startup_surface_normalizes_pipe_newlines_for_terminal_replay(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        record_web_demo.subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"centered one\ncentered two\r\n",
    )

    output = record_web_demo._startup_surface(
        "python", {"PATH": "/bin"}, 80, 24
    )

    assert output == b"centered one\r\ncentered two\r\n"


def test_only_mobile_demo_uses_compact_presentation() -> None:
    for profile in (
        record_web_demo.DESKTOP,
        record_web_demo.DUAL,
        record_web_demo.WORKFLOW,
        record_web_demo.TOUR,
        record_web_demo.CONTROLS,
    ):
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
    assert record_web_demo.MOBILE.width == 46
    assert record_web_demo.MOBILE.height == 38


def test_cast_profiles_include_auditable_agent_transcript() -> None:
    capture, digest = record_web_demo._load_agent_runs()

    assert capture["capture_method"].startswith("Audited non-persistent")
    assert {run["agent"] for run in capture["runs"]} == {
        "Claude Code",
        "Codex",
    }
    assert len(capture["runs"]) == 3
    assert all(len(run["source_commit"]) == 40 for run in capture["runs"])
    assert all(run["captured_at"].startswith("2026-07-") for run in capture["runs"])
    assert capture["startup_banner"]["version_output"] == (
        "2.1.220 (Claude Code)"
    )
    assert "{cwd}" in capture["startup_banner"]["lines"][-1]
    assert len(digest) == 64
    raw = record_web_demo.REAL_AGENT_RUNS.read_bytes()
    assert all(
        fragment not in raw
        for fragment in record_web_demo.FORBIDDEN_TRANSCRIPT_FRAGMENTS
    )
