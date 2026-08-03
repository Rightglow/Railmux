from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from railmux import __version__
from railmux.entrypoint import main as entrypoint_main
from railmux.windows_bootstrap import (
    WslRuntime,
    _system_wsl_executable,
    find_wsl_runtime,
    main,
)


def completed(argv, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def test_windows_version_does_not_require_or_probe_a_runtime(capsys):
    resolver = MagicMock(side_effect=AssertionError("runtime probe was unexpected"))

    assert main(["--version"], resolve_wsl=resolver, version_info=(3, 10)) == 0

    assert capsys.readouterr().out == (
        f"railmux {__version__} (Windows bootstrap)\n"
    )
    resolver.assert_not_called()


def test_windows_help_is_available_without_a_runtime(capsys):
    resolver = MagicMock(side_effect=AssertionError("runtime probe was unexpected"))

    assert main(["--help"], resolve_wsl=resolver, version_info=(3, 10)) == 0

    output = capsys.readouterr().out
    assert "usage: railmux" in output
    assert "RAILMUX_WSL_DISTRO" in output
    resolver.assert_not_called()


def test_public_entrypoint_selects_windows_before_importing_posix_cli(capsys):
    assert entrypoint_main(
        ["--version"],
        platform_name="nt",
    ) == 0

    assert capsys.readouterr().out.endswith("(Windows bootstrap)\n")


def test_public_entrypoint_still_dispatches_posix_version(capsys):
    with pytest.raises(SystemExit) as stopped:
        entrypoint_main(["--version"], platform_name="posix")

    assert stopped.value.code == 0
    assert capsys.readouterr().out == f"railmux {__version__}\n"


def test_windows_preview_rejects_python_39_before_runtime_probe(capsys):
    resolver = MagicMock(side_effect=AssertionError("runtime probe was unexpected"))

    assert main([], resolve_wsl=resolver, version_info=(3, 9, 19)) == 2

    assert "Python 3.10 or newer" in capsys.readouterr().err
    resolver.assert_not_called()


def test_system_wsl_path_never_uses_current_directory_or_path():
    expected = r"C:\Windows\System32\wsl.exe"

    result = _system_wsl_executable(
        {"SystemRoot": r"C:\Windows", "PATH": r"C:\untrusted-project"},
        is_file=lambda path: path == expected,
    )

    assert result == expected


def test_32_bit_python_prefers_sysnative_wsl_launcher():
    expected = r"C:\Windows\Sysnative\wsl.exe"

    result = _system_wsl_executable(
        {
            "SystemRoot": r"C:\Windows",
            "PROCESSOR_ARCHITEW6432": "AMD64",
        },
        is_file=lambda path: path == expected,
    )

    assert result == expected


def test_default_wsl_distribution_is_probed_without_guessing_a_name():
    probe = MagicMock(return_value=completed([], returncode=0))

    runtime = find_wsl_runtime("wsl.exe", environ={}, probe=probe)

    assert runtime == WslRuntime("wsl.exe", None, "railmux")
    assert probe.call_args.args[0] == [
        "wsl.exe",
        "--exec",
        "railmux",
        "--version",
    ]
    assert probe.call_count == 1


def test_explicit_wsl_distribution_is_the_only_distribution_probed():
    probe = MagicMock(return_value=completed([], returncode=0))

    runtime = find_wsl_runtime(
        "wsl.exe",
        environ={"RAILMUX_WSL_DISTRO": "开发环境"},
        probe=probe,
    )

    assert runtime == WslRuntime("wsl.exe", "开发环境", "railmux")
    assert probe.call_args.args[0] == [
        "wsl.exe",
        "--distribution",
        "开发环境",
        "--exec",
        "railmux",
        "--version",
    ]
    assert probe.call_count == 1


def test_login_shell_resolves_standard_user_install_then_verifies_it():
    calls = []

    def probe(argv, *, timeout):
        calls.append(argv)
        if argv[-2:] == ["railmux", "--version"]:
            return completed(argv, returncode=127)
        if argv[-2:] == ["-lc", "command -v railmux"]:
            return completed(
                argv,
                stdout="/home/用户/.local/bin/railmux\n".encode("utf-8"),
            )
        return completed(argv)

    runtime = find_wsl_runtime("wsl.exe", environ={}, probe=probe)

    assert runtime == WslRuntime(
        "wsl.exe",
        None,
        "/home/用户/.local/bin/railmux",
    )
    assert calls == [
        ["wsl.exe", "--exec", "railmux", "--version"],
        [
            "wsl.exe",
            "--exec",
            "/bin/sh",
            "-lc",
            "command -v railmux",
        ],
        [
            "wsl.exe",
            "--exec",
            "/home/用户/.local/bin/railmux",
            "--version",
        ],
    ]


def test_bootstrap_hands_exact_argv_to_wsl_without_a_shell():
    probe = MagicMock(return_value=completed([], returncode=0))
    process = MagicMock()
    process.wait.return_value = 17
    popen = MagicMock(return_value=process)
    arguments = ["ssh", "user@example", "--ssh-args=-J jump host", "开发"]

    result = main(
        arguments,
        environ={},
        resolve_wsl=lambda _environ: r"C:\Windows\System32\wsl.exe",
        probe=probe,
        popen=popen,
        version_info=(3, 10),
    )

    assert result == 17
    assert popen.call_args.args[0] == [
        r"C:\Windows\System32\wsl.exe",
        "--exec",
        "railmux",
        *arguments,
    ]
    assert popen.call_args.kwargs == {}


def test_ctrl_c_does_not_kill_the_wsl_handoff():
    probe = MagicMock(return_value=completed([], returncode=0))
    process = MagicMock()
    process.wait.side_effect = [KeyboardInterrupt, 23]
    popen = MagicMock(return_value=process)

    assert main(
        ["ssh", "example"],
        environ={},
        resolve_wsl=lambda _environ: r"C:\Windows\System32\wsl.exe",
        probe=probe,
        popen=popen,
        version_info=(3, 10),
    ) == 23

    assert process.wait.call_count == 2
    process.kill.assert_not_called()
    process.terminate.assert_not_called()


def test_missing_runtime_fails_actionably_without_installing(capsys):
    assert main(
        ["ssh", "example"],
        environ={},
        resolve_wsl=lambda _environ: None,
        version_info=(3, 10),
    ) == 2

    error = capsys.readouterr().err
    assert "system WSL launcher is not installed" in error
    assert "does not install software or modify a WSL distribution" in error
    assert "managed MSYS2 fallback" in error
