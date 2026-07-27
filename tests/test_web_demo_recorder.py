"""Safety and determinism checks for the public website recorder."""

from __future__ import annotations

import socket

from tools import record_web_demo


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
    assert "focused test proposed" in demo_agent
    assert "focused test passed" not in demo_agent
    assert "provider session was not persisted" in demo_agent
