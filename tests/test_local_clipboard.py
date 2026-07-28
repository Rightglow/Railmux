"""Native clipboard fallback selection and execution."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from railmux import local_clipboard


def test_macos_uses_pbcopy_without_a_shell(monkeypatch):
    monkeypatch.setattr(local_clipboard.sys, "platform", "darwin")
    monkeypatch.setattr(
        local_clipboard.shutil,
        "which",
        lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None,
    )
    run = MagicMock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(local_clipboard.subprocess, "run", run)

    assert local_clipboard.copy("status 你好".encode())
    run.assert_called_once_with(
        ("/usr/bin/pbcopy",),
        input="status 你好".encode(),
        stdout=local_clipboard.subprocess.DEVNULL,
        stderr=local_clipboard.subprocess.DEVNULL,
        timeout=2.0,
        check=False,
    )


def test_linux_prefers_wayland_then_x11_helpers(monkeypatch):
    monkeypatch.setattr(local_clipboard.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(
        local_clipboard.shutil,
        "which",
        lambda name: "/usr/bin/wl-copy" if name == "wl-copy" else None,
    )

    assert local_clipboard.command() == ("/usr/bin/wl-copy",)


def test_wsl_uses_clip_exe_without_a_display_server(monkeypatch):
    monkeypatch.setattr(local_clipboard.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(
        local_clipboard.shutil,
        "which",
        lambda name: (
            "/mnt/c/Windows/System32/clip.exe"
            if name == "clip.exe"
            else None
        ),
    )

    assert local_clipboard.command() == (
        "/mnt/c/Windows/System32/clip.exe",
    )


def test_native_clipboard_failure_falls_back_cleanly(monkeypatch):
    monkeypatch.setattr(local_clipboard, "command", lambda: ("/bad/writer",))
    monkeypatch.setattr(
        local_clipboard.subprocess,
        "run",
        MagicMock(side_effect=TimeoutError),
    )

    assert local_clipboard.copy(b"status") is False
