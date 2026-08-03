from pathlib import Path

from railmux.provider_paths import provider_path


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
