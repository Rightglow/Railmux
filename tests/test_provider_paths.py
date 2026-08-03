import json
import os
from pathlib import Path

from railmux import __version__, provider_paths
from railmux.provider_paths import (
    private_mode_is_safe,
    provider_path,
    running_in_managed_windows_wrapper,
)


def test_windows_drive_path_is_visible_through_msys_mount():
    assert provider_path(
        r"C:\Users\用户\project",
        environ={"RAILMUX_WINDOWS_RUNTIME": "msys2"},
    ) == Path("/c/Users/用户/project")


def test_extended_windows_drive_path_is_normalized():
    assert provider_path(
        r"\\?\D:\work\repo",
        environ={"RAILMUX_WINDOWS_RUNTIME": "msys2"},
    ) == Path("/d/work/repo")


def test_posix_runtime_preserves_existing_path_behavior():
    raw = r"C:\Users\Alice\project"
    assert provider_path(raw, environ={}) == Path(raw)


def test_forward_slash_windows_drive_path_is_normalized():
    assert provider_path(
        "E:/workspace/project",
        environ={"RAILMUX_WINDOWS_RUNTIME": "msys2"},
    ) == Path("/e/workspace/project")


def test_noacl_mode_requires_exact_managed_runtime_marker(monkeypatch, tmp_path):
    marker = tmp_path / "railmux-runtime.json"
    marker.write_text(json.dumps({
        "schema": 1,
        "runtime": "msys2-test",
        "railmux": __version__,
    }), encoding="utf-8")
    marker.chmod(0o644)
    monkeypatch.setattr(provider_paths, "_MANAGED_RUNTIME_MARKER", marker)
    monkeypatch.setattr(provider_paths.sys, "platform", "cygwin")
    monkeypatch.setenv("RAILMUX_WINDOWS_RUNTIME", "msys2")
    monkeypatch.setenv("RAILMUX_MSYS2_RUNTIME_ID", "msys2-test")

    assert running_in_managed_windows_wrapper()
    assert private_mode_is_safe(0o100644)
    assert not private_mode_is_safe(0o100666)

    monkeypatch.setenv("RAILMUX_MSYS2_RUNTIME_ID", "other-runtime")
    assert not running_in_managed_windows_wrapper()
    assert not private_mode_is_safe(0o100644)


def test_managed_runtime_marker_must_be_same_owner(monkeypatch, tmp_path):
    marker = tmp_path / "railmux-runtime.json"
    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(provider_paths, "_MANAGED_RUNTIME_MARKER", marker)
    monkeypatch.setattr(provider_paths.sys, "platform", "cygwin")
    monkeypatch.setenv("RAILMUX_WINDOWS_RUNTIME", "msys2")
    monkeypatch.setenv("RAILMUX_MSYS2_RUNTIME_ID", "msys2-test")
    real_lstat = marker.lstat
    monkeypatch.setattr(
        provider_paths.Path, "lstat",
        lambda self: os.stat_result((
            real_lstat().st_mode, real_lstat().st_ino,
            real_lstat().st_dev, 1, os.getuid() + 1, 0,
            real_lstat().st_size, 0, 0, 0,
        )),
    )

    assert not running_in_managed_windows_wrapper()
