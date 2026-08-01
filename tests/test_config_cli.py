from __future__ import annotations

import sys
import stat
from io import StringIO

from railmux import config_cli
from railmux.config import load_config
from railmux.config_cli import main
from railmux.runtime_config import ExecutableCheck


def test_program_menu_validates_and_persists_codex_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    output = StringIO()

    result = main(
        stdin=StringIO(f"2\n3\n{sys.executable}\nq\n"),
        stdout=output,
    )

    assert result == 0
    assert load_config().codex_binary == sys.executable
    assert "Validated" in output.getvalue()
    assert "Saved." in output.getvalue()


def test_category_reset_preserves_unknown_toml(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config" / "railmux" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[codex]\nauto_run = "always"\nunknown = "keep"\n'
        '[custom]\nvalue = 7\n'
    )

    result = main(stdin=StringIO("1\nr\ny\nb\nq\n"), stdout=StringIO())

    assert result == 0
    text = path.read_text()
    assert "auto_run" not in text
    assert 'unknown = "keep"' in text
    assert "[custom]" in text


def test_invalid_config_can_be_backed_up_and_reset(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config" / "railmux" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("[broken")
    output = StringIO()

    result = main(stdin=StringIO("y\nq\n"), stdout=output)

    assert result == 0
    assert not path.exists()
    backup = path.with_name("config.toml.invalid.bak")
    assert backup.read_text() == "[broken"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert "Backed up" in output.getvalue()


def test_resetting_tmux_to_path_does_not_validate_through_old_override(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    path = tmp_path / ".config" / "railmux" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[tmux]\nbinary = "/old/tmux"\n')
    observed: dict[str, str] = {}

    def check(_kind, value, *, environ):
        observed["path"] = environ["PATH"]
        return ExecutableCheck(value, "/usr/bin/tmux", "tmux 3.4", None)

    monkeypatch.setattr(config_cli, "check_executable", check)
    result = main(stdin=StringIO("2\n1\ntmux\nq\n"), stdout=StringIO())

    assert result == 0
    assert observed["path"] == "/usr/bin"
    assert load_config().tmux_binary == "tmux"


def test_remote_context_hides_and_preserves_local_history_limit(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config" / "railmux" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[ssh]\nhistory_lines = 12345\nclaude_history = "local"\n'
        '[codex]\nauto_run = "always"\n'
    )
    output = StringIO()

    result = main(
        ["--remote-context"],
        stdin=StringIO("1\nr\ny\nb\nr\ny\nq\n"),
        stdout=output,
    )

    assert result == 0
    assert "railmux ssh history lines" not in output.getvalue()
    assert load_config().ssh_history_lines == 12345
    text = path.read_text()
    assert "claude_history" not in text
    assert "auto_run" not in text


def test_remote_option_dispatches_without_loading_local_config(monkeypatch):
    observed = {}

    def run(destination, *, ssh_args, raw_argv):
        observed.update(
            destination=destination,
            ssh_args=tuple(ssh_args),
            raw_argv=tuple(raw_argv),
        )
        return 7

    monkeypatch.setattr("railmux.remote_config.run_remote_config", run)

    result = main(["--remote", "work", "--ssh-arg=-J", "--ssh-arg=jump"])

    assert result == 7
    assert observed == {
        "destination": "work",
        "ssh_args": ("-J", "jump"),
        "raw_argv": ("--remote", "work", "--ssh-arg=-J", "--ssh-arg=jump"),
    }


def test_remote_option_accepts_ordered_grouped_ssh_arguments(monkeypatch):
    observed = {}

    def run(_destination, *, ssh_args, raw_argv):
        observed["ssh_args"] = tuple(ssh_args)
        return 0

    monkeypatch.setattr("railmux.remote_config.run_remote_config", run)

    result = main([
        "--remote",
        "work",
        "--ssh-arg=-F",
        "--ssh-args=config -J jump -p 2222",
    ])

    assert result == 0
    assert observed["ssh_args"] == (
        "-F", "config", "-J", "jump", "-p", "2222",
    )
