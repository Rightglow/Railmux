from __future__ import annotations

import shlex

from railmux import local_open


def test_url_opens_without_a_shell_and_warns_for_loopback(monkeypatch):
    launched: list[tuple[str, ...]] = []
    monkeypatch.setattr(local_open.sys, "platform", "darwin")
    monkeypatch.setattr(
        local_open.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "open" else None,
    )
    monkeypatch.setattr(
        local_open,
        "_detached_popen",
        lambda argv: launched.append(tuple(argv)),
    )

    public = local_open.open_url("https://example.test/docs")
    loopback = local_open.open_url("http://127.0.0.1:3000/")

    assert public.opened and public.level == "success"
    assert launched[0] == (
        "/usr/bin/open",
        "--",
        "https://example.test/docs",
    )
    assert loopback.opened and loopback.level == "warning"
    assert "not tunneled" in loopback.message


def test_url_without_opener_falls_back_to_copy(monkeypatch):
    monkeypatch.setattr(local_open.sys, "platform", "linux")
    monkeypatch.setattr(local_open.shutil, "which", lambda _name: None)

    result = local_open.open_url("https://example.test")

    assert not result.opened
    assert result.copy_data == b"https://example.test"


def test_managed_windows_url_and_path_openers_use_direct_argv(monkeypatch):
    launched: list[tuple[str, ...]] = []
    monkeypatch.setattr(local_open.sys, "platform", "linux")
    monkeypatch.setenv("RAILMUX_WINDOWS_RUNTIME", "msys2")
    monkeypatch.setattr(
        local_open.shutil,
        "which",
        lambda name: {
            "rundll32.exe": "/c/Windows/System32/rundll32.exe",
            "explorer.exe": "/c/Windows/explorer.exe",
            "cygpath": "/usr/bin/cygpath",
        }.get(name),
    )
    monkeypatch.setattr(
        local_open.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "C:\\work\\main.py\n",
    )
    monkeypatch.setattr(
        local_open,
        "_detached_popen",
        lambda argv: launched.append(tuple(argv)),
    )

    assert local_open.open_url(
        "https://www.baidu.com/s??wd=railmux&source=terminal"
    ).opened
    assert local_open.open_windows_path("/c/work/main.py", directory=False).opened

    assert launched == [
        (
            "/c/Windows/System32/rundll32.exe",
            "url.dll,FileProtocolHandler",
            "https://www.baidu.com/s??wd=railmux&source=terminal",
        ),
        ("/c/Windows/explorer.exe", "C:\\work\\main.py"),
    ]


def test_managed_windows_url_never_falls_back_to_file_explorer(monkeypatch):
    monkeypatch.setattr(local_open.sys, "platform", "linux")
    monkeypatch.setenv("RAILMUX_WINDOWS_RUNTIME", "msys2")
    monkeypatch.setattr(
        local_open.shutil,
        "which",
        lambda name: "/c/Windows/explorer.exe"
        if name == "explorer.exe"
        else None,
    )

    result = local_open.open_url("https://example.test/search??q=railmux")

    assert not result.opened
    assert result.copy_data == b"https://example.test/search??q=railmux"


def test_managed_windows_path_open_failure_copies_validated_path(monkeypatch):
    monkeypatch.setattr(local_open.shutil, "which", lambda _name: None)

    result = local_open.open_windows_path("/c/work/main.py", directory=False)

    assert not result.opened
    assert result.copy_data == b"/c/work/main.py"


def test_remote_html_uses_vim_with_location_and_safe_ssh_argv():
    argv = local_open.build_remote_open_argv(
        "work-host",
        ssh_args=("-J", "jump", "-T"),
        path="/remote/a weird/index.html",
        directory=False,
        line=12,
        column=7,
    )

    assert argv[:6] == ("ssh", "-J", "jump", "-t", "--", "work-host")
    assert "-T" not in argv
    command = argv[-1]
    assert "command -v vim" in command
    assert "cursor(12, 7)" in command
    assert shlex.quote("/remote/a weird/index.html") in command


def test_remote_directory_and_binary_enter_a_directory_instead_of_vim():
    directory = local_open.build_remote_open_argv(
        "host",
        ssh_args=(),
        path="/remote/project",
        directory=True,
    )[-1]
    binary = local_open.build_remote_open_argv(
        "host",
        ssh_args=(),
        path="/remote/project/model.bin",
        directory=False,
    )[-1]

    assert "command -v vim" not in directory
    assert "cd -- /remote/project" in directory
    assert "command -v vim" not in binary
    assert "cd -- /remote/project" in binary


def test_remote_path_uses_macos_terminal_or_copies_safe_command(monkeypatch):
    launched: list[tuple[str, ...]] = []
    monkeypatch.setattr(local_open.sys, "platform", "darwin")
    monkeypatch.setattr(
        local_open.shutil,
        "which",
        lambda name: "/usr/bin/osascript" if name == "osascript" else None,
    )
    monkeypatch.setattr(
        local_open,
        "_detached_popen",
        lambda argv: launched.append(tuple(argv)),
    )

    opened = local_open.open_remote_path(
        "host",
        ssh_args=(),
        path="/remote/main.py",
        directory=False,
    )

    assert opened.opened
    assert opened.message == "Opening remote file in Vim · new terminal"
    assert launched[0][0] == "/usr/bin/osascript"
    assert "ssh -t -- host" in launched[0][-1]

    monkeypatch.setattr(local_open.sys, "platform", "linux")
    monkeypatch.setattr(local_open.shutil, "which", lambda _name: None)
    copied = local_open.open_remote_path(
        "host",
        ssh_args=(),
        path="/remote/main.py",
        directory=False,
    )
    assert not copied.opened
    assert copied.copy_data is not None
    assert copied.copy_data.startswith(b"ssh -t -- host ")


def test_remote_path_uses_new_wsl_terminal_tab(monkeypatch):
    launched: list[tuple[str, ...]] = []
    monkeypatch.setattr(local_open.sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    monkeypatch.setattr(
        local_open.shutil,
        "which",
        lambda name: {
            "wt.exe": "/mnt/c/Windows/System32/wt.exe",
            "wsl.exe": "/mnt/c/Windows/System32/wsl.exe",
        }.get(name),
    )
    monkeypatch.setattr(
        local_open,
        "_detached_popen",
        lambda argv: launched.append(tuple(argv)),
    )

    result = local_open.open_remote_path(
        "host",
        ssh_args=("-J", "jump"),
        path="/remote/main.py",
        directory=False,
    )

    assert result.opened
    assert result.message == "Opening remote file in Vim · new terminal"
    assert len(launched) == 1
    assert launched[0][:-1] == (
        "/mnt/c/Windows/System32/wt.exe",
        "new-tab",
        "/mnt/c/Windows/System32/wsl.exe",
        "--distribution",
        "Ubuntu-24.04",
        "--exec",
        "ssh",
        "-J",
        "jump",
        "-t",
        "--",
        "host",
    )
    assert "command -v vim" in launched[0][-1]


def test_remote_path_status_distinguishes_directory_and_binary(monkeypatch):
    monkeypatch.setattr(local_open.sys, "platform", "darwin")
    monkeypatch.setattr(
        local_open.shutil,
        "which",
        lambda name: "/usr/bin/osascript" if name == "osascript" else None,
    )
    monkeypatch.setattr(local_open, "_detached_popen", lambda _argv: None)

    directory = local_open.open_remote_path(
        "host",
        ssh_args=(),
        path="/remote/project",
        directory=True,
    )
    binary = local_open.open_remote_path(
        "host",
        ssh_args=(),
        path="/remote/project/model.bin",
        directory=False,
    )

    assert directory.message == "Opening remote directory · new terminal"
    assert binary.message == "Opening remote file's directory · new terminal"
