from __future__ import annotations

import io
from unittest.mock import Mock

from railmux import system_deps
from railmux.system_deps import TmuxInstallPlan


class TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def _which(*available: str):
    commands = set(available)
    return lambda command: f"/usr/bin/{command}" if command in commands else None


def test_macos_homebrew_plan_can_be_executed(monkeypatch):
    monkeypatch.setattr(system_deps.shutil, "which", _which("brew"))

    plan = system_deps.tmux_install_plan("darwin")

    assert plan.manager == "Homebrew"
    assert plan.command == ("brew", "install", "tmux")
    assert plan.display_command == "brew install tmux"


def test_macos_without_homebrew_only_gives_guidance(monkeypatch):
    monkeypatch.setattr(system_deps.shutil, "which", _which())

    plan = system_deps.tmux_install_plan("darwin")

    assert plan.command is None
    assert "https://brew.sh/" in plan.guidance


def test_apt_plan_uses_sudo_for_non_root_user(monkeypatch):
    monkeypatch.setattr(system_deps.shutil, "which", _which("apt-get", "sudo"))
    monkeypatch.setattr(system_deps.os, "geteuid", lambda: 1000)

    plan = system_deps.tmux_install_plan("linux")

    assert plan.manager == "apt-get"
    assert plan.command == ("sudo", "apt-get", "install", "tmux")
    assert plan.display_command == "sudo apt-get install tmux"


def test_apt_plan_does_not_offer_execution_without_privilege_helper(monkeypatch):
    monkeypatch.setattr(system_deps.shutil, "which", _which("apt-get"))
    monkeypatch.setattr(system_deps.os, "geteuid", lambda: 1000)

    plan = system_deps.tmux_install_plan("linux")

    assert plan.command is None
    assert "run as root: apt-get install tmux" in plan.guidance


def test_other_linux_managers_only_give_manual_guidance(monkeypatch):
    monkeypatch.setattr(system_deps.shutil, "which", _which("dnf", "sudo"))
    monkeypatch.setattr(system_deps.os, "geteuid", lambda: 1000)

    plan = system_deps.tmux_install_plan("linux")

    assert plan.manager == "dnf"
    assert plan.command is None
    assert plan.display_command == "sudo dnf install tmux"


def test_noninteractive_missing_tmux_never_runs_installer(monkeypatch):
    plan = TmuxInstallPlan(
        "Homebrew", ("brew", "install", "tmux"), "brew install tmux",
        "Install tmux with brew install tmux.",
    )
    monkeypatch.setattr(system_deps.tmux_ctl, "has_tmux", lambda: False)
    monkeypatch.setattr(system_deps, "tmux_install_plan", lambda: plan)
    run = Mock()
    monkeypatch.setattr(system_deps.subprocess, "run", run)
    stderr = io.StringIO()

    assert not system_deps.ensure_tmux_available(
        stdin=io.StringIO("y\n"), stderr=stderr
    )
    run.assert_not_called()
    assert "error: Install tmux with brew install tmux." in stderr.getvalue()


def test_interactive_confirmation_installs_and_continues(monkeypatch):
    plan = TmuxInstallPlan(
        "Homebrew", ("brew", "install", "tmux"), "brew install tmux",
        "Install tmux with brew install tmux.",
    )
    monkeypatch.setattr(
        system_deps.tmux_ctl, "has_tmux", Mock(side_effect=[False, True])
    )
    monkeypatch.setattr(system_deps.tmux_ctl, "tmux_version", Mock(return_value=(3, 5)))
    monkeypatch.setattr(system_deps, "tmux_install_plan", lambda: plan)
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(system_deps.subprocess, "run", run)
    stderr = TTYBuffer()

    assert system_deps.ensure_tmux_available(
        stdin=TTYBuffer("yes\n"), stderr=stderr
    )
    run.assert_called_once_with(("brew", "install", "tmux"), check=False)
    assert "tmux 3.5 is now available; continuing." in stderr.getvalue()


def test_declined_install_keeps_actionable_error(monkeypatch):
    plan = TmuxInstallPlan(
        "apt-get", ("sudo", "apt-get", "install", "tmux"),
        "sudo apt-get install tmux",
        "Install tmux with sudo apt-get install tmux.",
    )
    monkeypatch.setattr(system_deps.tmux_ctl, "has_tmux", lambda: False)
    monkeypatch.setattr(system_deps, "tmux_install_plan", lambda: plan)
    run = Mock()
    monkeypatch.setattr(system_deps.subprocess, "run", run)
    stderr = TTYBuffer()

    assert not system_deps.ensure_tmux_available(
        stdin=TTYBuffer("n\n"), stderr=stderr
    )
    run.assert_not_called()
    assert "Install tmux now with apt-get? [y/N]" in stderr.getvalue()
    assert "error: Install tmux with sudo apt-get install tmux." in stderr.getvalue()


def test_failed_installer_reports_status_and_guidance(monkeypatch):
    plan = TmuxInstallPlan(
        "apt-get", ("sudo", "apt-get", "install", "tmux"),
        "sudo apt-get install tmux",
        "Install tmux with sudo apt-get install tmux.",
    )
    monkeypatch.setattr(system_deps.tmux_ctl, "has_tmux", lambda: False)
    monkeypatch.setattr(system_deps, "tmux_install_plan", lambda: plan)
    monkeypatch.setattr(
        system_deps.subprocess, "run", Mock(return_value=Mock(returncode=17))
    )
    stderr = TTYBuffer()

    assert not system_deps.ensure_tmux_available(
        stdin=TTYBuffer("y\n"), stderr=stderr
    )
    assert "apt-get exited with status 17." in stderr.getvalue()
    assert "error: Install tmux with sudo apt-get install tmux." in stderr.getvalue()
