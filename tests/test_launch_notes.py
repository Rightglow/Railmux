from unittest.mock import Mock

import pytest
from packaging.version import Version

from railmux import __version__, launch_notes, restart_state
from railmux.ssh_display_diagnostics import SshDisplayDiagnostic
from railmux.ui.modals import LaunchNotesModal


@pytest.fixture
def launch(monkeypatch, tmp_path):
    marker = tmp_path / "launch-version"
    view = tmp_path / "view-state.json"
    monkeypatch.setattr(launch_notes, "default_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(restart_state, "portable_state_path", lambda: view)
    monkeypatch.setattr(restart_state, "legacy_state_path", lambda: tmp_path / "legacy.json")
    monkeypatch.setattr(
        "railmux.ssh_display_diagnostics.read_diagnostic",
        lambda: SshDisplayDiagnostic("none"),
    )
    monkeypatch.setattr(launch_notes.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(launch_notes.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(launch_notes.sys.stdout, "fileno", lambda: 1)
    monkeypatch.setattr(launch_notes.os, "get_terminal_size", lambda _fd: launch_notes.os.terminal_size((80, 24)))
    monkeypatch.setenv("TERM", "xterm-256color")
    show = Mock(return_value=True)
    monkeypatch.setattr(launch_notes, "_show_notes", show)
    return marker, view, show


def test_current_product_version_has_three_short_english_release_sections():
    current = launch_notes.recent_highlights(__version__)
    assert current[0][0] == Version(__version__).base_version
    assert len(current) == 3
    sections = launch_notes.recent_highlights("0.5.0rc3")
    assert [version for version, _ in sections] == ["0.5.0", "0.4.2"]
    content = " ".join(note for _, notes in sections for note in notes)
    assert all(text in content for text in ("Running", "Devin", "Codex history", "wrapped links", "pinned"))


@pytest.mark.parametrize("key", ["enter", "esc"])
def test_launch_notes_scroll_and_keep_continue_visible(key):
    proceed = Mock()
    modal = LaunchNotesModal("0.5.0rc3", launch_notes.recent_highlights("0.5.0rc3"),
                            on_continue=proceed)
    size = (40, 10)
    before = b"\n".join(modal.render(size, focus=True).text)
    assert b"continue" in before and b"Running" in before
    for _ in range(4):
        modal.keypress(size, "page down")
    after = b"\n".join(modal.render(size, focus=True).text)
    assert b"Codex history" in after and b"continue" in after
    assert modal.keypress(size, key) is None
    proceed.assert_called_once_with()


def test_fresh_install_opens_immediately_and_records_its_version(launch):
    marker, _, show = launch
    launch_notes.maybe_show_launch_notes()
    show.assert_not_called()
    assert marker.read_text().strip() == __version__


def test_legacy_workspace_notes_are_acknowledged_once_even_with_two_launchers(launch):
    marker, view, show = launch
    view.touch()

    def another_launch(*_args):
        assert not marker.exists()
        launch_notes.maybe_show_launch_notes()
        return True

    show.side_effect = another_launch
    launch_notes.maybe_show_launch_notes()
    launch_notes.maybe_show_launch_notes(existing_workspace=True)
    show.assert_called_once_with(__version__, launch_notes.recent_highlights(__version__))
    assert marker.read_text().strip() == __version__


def test_cancelled_upgrade_notes_are_retried_and_a_downgrade_is_quiet(launch, monkeypatch):
    marker, _, show = launch
    marker.write_text("0.4.2\n")
    show.return_value = False
    launch_notes.maybe_show_launch_notes()
    assert marker.read_text().strip() == "0.4.2"
    show.return_value = True
    launch_notes.maybe_show_launch_notes()
    assert show.call_count == 2
    monkeypatch.setattr(launch_notes, "__version__", "0.4.2")
    launch_notes.maybe_show_launch_notes()
    assert marker.read_text().strip() == __version__
    assert show.call_count == 2


def test_ssh_upgrade_recognizes_an_older_local_client(launch, monkeypatch):
    marker, _, show = launch
    monkeypatch.setattr(
        "railmux.ssh_display_diagnostics.read_diagnostic",
        lambda: SshDisplayDiagnostic("ok", client_version="0.4.2"),
    )
    launch_notes.maybe_show_launch_notes()
    show.assert_called_once()
    assert marker.read_text().strip() == __version__


def test_ssh_launcher_shows_notes_before_connecting(monkeypatch):
    from railmux import fast_display_client

    events = []
    monkeypatch.setattr(launch_notes, "maybe_show_launch_notes", lambda: events.append("notes"))
    monkeypatch.setattr(fast_display_client, "run", lambda args: events.append(args.destination) or 0)
    assert fast_display_client.main(["test-server"]) == 0
    assert events == ["notes", "test-server"]
