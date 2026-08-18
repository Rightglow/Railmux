from __future__ import annotations

import sys
import stat
from io import StringIO

import pytest

from railmux import config_cli
from railmux.config import load_config
from railmux.config_cli import main
from railmux.runtime_config import ExecutableCheck


class _TTYStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def test_interactive_editor_uses_and_restores_alternate_screen(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    output = _TTYStringIO()

    result = main(stdin=_TTYStringIO("q\n"), stdout=output)

    assert result == 0
    rendered = output.getvalue()
    assert rendered.startswith("\033[?1049h\033[2J\033[H")
    assert rendered.endswith("\033[0m\033[?25h\033[?1049l")


def test_back_redraws_a_clean_parent_page(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    output = _TTYStringIO()

    result = main(stdin=_TTYStringIO("1\nb\nq\n"), stdout=output)

    assert result == 0
    pages = output.getvalue().split(config_cli._PAGE_CLEAR)[1:]
    assert len(pages) == 3
    root, behavior, restored_root = pages
    assert "Layout retention" not in root
    assert "> 1. Behavior / Options" in behavior
    assert "Layout retention" in behavior
    assert "Layout retention" not in restored_root
    assert "r. Reset all Railmux-managed settings" in restored_root


def test_setting_page_uses_breadcrumb_and_returns_feedback(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    output = _TTYStringIO()

    result = main(stdin=_TTYStringIO("1\n1\n1\nq\n"), stdout=output)

    assert result == 0
    pages = output.getvalue().split(config_cli._PAGE_CLEAR)[1:]
    editor = next(page for page in pages if "> Layout retention" in page)
    category_after_save = pages[-1]
    assert (
        "Railmux configuration > Behavior / Options > Layout retention"
        in editor
    )
    assert "Current: ask" in editor
    assert "Saved." in category_after_save
    assert "Layout retention [always]" in category_after_save


def test_redirected_editor_does_not_emit_terminal_control_sequences(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    output = StringIO()

    result = main(stdin=StringIO("q\n"), stdout=output)

    assert result == 0
    assert "\033[" not in output.getvalue()


def test_interactive_editor_restores_alternate_screen_after_error(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    output = _TTYStringIO()

    def fail_load():
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(config_cli, "load_config", fail_load)
    with pytest.raises(RuntimeError, match="unexpected failure"):
        main(stdin=_TTYStringIO(""), stdout=output)

    assert output.getvalue().endswith("\033[0m\033[?25h\033[?1049l")


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
        '[interaction]\nhistory_lines = 12345\nclaude_history = "native"\n'
        '[ssh]\nhistory_lines = 2345\nclaude_history = "local"\n'
        '[codex]\nauto_run = "always"\n'
    )
    output = StringIO()

    result = main(
        ["--remote-context"],
        stdin=StringIO("1\nr\ny\nb\nr\ny\nq\n"),
        stdout=output,
    )

    assert result == 0
    assert "Remote Railmux configuration" in output.getvalue()
    assert "Managed history lines" not in output.getvalue()
    assert load_config().ssh_history_lines == 12345
    text = path.read_text()
    assert "claude_history" not in text
    assert "auto_run" not in text


@pytest.mark.parametrize(
    ("menu_entry", "key", "canonical", "legacy", "expected"),
    (
        ("4", "claude_history", '"native"', '"local"', "ask"),
        ("5", "path_open", '"external"', '"internal"', "ask"),
    ),
)
def test_single_behavior_reset_clears_canonical_and_released_alias(
    monkeypatch,
    tmp_path,
    menu_entry,
    key,
    canonical,
    legacy,
    expected,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config" / "railmux" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"[interaction]\n{key} = {canonical}\n"
        f"[ssh]\n{key} = {legacy}\n"
    )

    result = main(
        stdin=StringIO(f"1\n{menu_entry}\nr\nq\n"),
        stdout=StringIO(),
    )

    assert result == 0
    config = load_config()
    assert getattr(
        config,
        "claude_history" if key == "claude_history" else "interaction_path_open",
    ) == expected
    assert key not in path.read_text()


def test_single_history_limit_reset_clears_released_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config" / "railmux" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "[interaction]\nhistory_lines = 12000\n"
        "[ssh]\nhistory_lines = 13000\n"
    )

    # Managed history is the sixth Behavior entry.
    result = main(stdin=StringIO("1\n6\nr\nq\n"), stdout=StringIO())

    assert result == 0
    assert load_config().history_lines == 10000
    assert "history_lines" not in path.read_text()


def test_remote_option_dispatches_without_loading_local_config(monkeypatch):
    observed = {}

    def run(destination, *, ssh_args, raw_argv, remote_platform):
        observed.update(
            destination=destination,
            ssh_args=tuple(ssh_args),
            raw_argv=tuple(raw_argv),
            remote_platform=remote_platform,
        )
        return 7

    monkeypatch.setattr("railmux.remote_config.run_remote_config", run)

    result = main(["--remote", "work", "--ssh-arg=-J", "--ssh-arg=jump"])

    assert result == 7
    assert observed == {
        "destination": "work",
        "ssh_args": ("-J", "jump"),
        "raw_argv": ("--remote", "work", "--ssh-arg=-J", "--ssh-arg=jump"),
        "remote_platform": "auto",
    }


def test_remote_option_accepts_ordered_grouped_ssh_arguments(monkeypatch):
    observed = {}

    def run(_destination, *, ssh_args, raw_argv, remote_platform):
        observed["ssh_args"] = tuple(ssh_args)
        observed["remote_platform"] = remote_platform
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
    assert observed["remote_platform"] == "auto"


def test_remote_option_forwards_explicit_windows_platform(monkeypatch):
    observed = {}

    def run(_destination, **kwargs):
        observed.update(kwargs)
        return 0

    monkeypatch.setattr("railmux.remote_config.run_remote_config", run)

    assert main([
        "--remote", "work", "--remote-platform", "windows",
    ]) == 0
    assert observed["remote_platform"] == "windows"


def test_remote_only_argument_errors_return_cli_status(capsys):
    assert main(["--ssh-args=-J jump"]) == 2
    assert "--ssh-args requires --remote" in capsys.readouterr().err


def test_remote_context_conflict_returns_cli_status(capsys):
    assert main(["--remote", "work", "--remote-context"]) == 2
    assert "--remote and --remote-context" in capsys.readouterr().err
