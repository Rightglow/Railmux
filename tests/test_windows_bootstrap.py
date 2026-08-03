from __future__ import annotations

from unittest.mock import MagicMock

from railmux import __version__
from railmux.entrypoint import main as entrypoint_main
from railmux.windows_bootstrap import main
from railmux.windows_msys2 import Msys2Runtime


def test_windows_version_does_not_probe_or_install(capsys):
    finder = MagicMock(side_effect=AssertionError("runtime probe was unexpected"))

    assert main(["--version"], runtime_finder=finder, version_info=(3, 10)) == 0

    assert capsys.readouterr().out == (
        f"railmux {__version__} (Windows MSYS2 bootstrap)\n"
    )
    finder.assert_not_called()


def test_windows_help_describes_shared_native_provider_data(capsys):
    finder = MagicMock(side_effect=AssertionError("runtime probe was unexpected"))

    assert main(["--help"], runtime_finder=finder, version_info=(3, 10)) == 0

    output = capsys.readouterr().out
    assert "managed MSYS2/tmux" in output
    assert "Windows-native" in output
    assert "runtime {status,install}" in output
    finder.assert_not_called()


def test_public_entrypoint_selects_windows_before_posix_import(capsys):
    result = entrypoint_main(["--version"], platform_name="nt")

    assert result == 0
    assert capsys.readouterr().out.endswith("(Windows MSYS2 bootstrap)\n")


def test_windows_requires_python_310_before_runtime_probe(capsys):
    finder = MagicMock(side_effect=AssertionError("runtime probe was unexpected"))

    assert main([], runtime_finder=finder, version_info=(3, 9, 19)) == 2

    assert "Python 3.10 or newer" in capsys.readouterr().err
    finder.assert_not_called()


def test_noninteractive_missing_runtime_is_actionable_and_never_installs(capsys):
    assert main(
        ["doctor"],
        environ={"LOCALAPPDATA": r"C:\Users\u\AppData\Local"},
        runtime_finder=lambda **_kwargs: None,
        stdin_isatty=lambda: False,
        version_info=(3, 10),
    ) == 2

    error = capsys.readouterr().err
    assert "private MSYS2/tmux runtime" in error
    assert "railmux runtime install --yes" in error
    assert "existing session directories remain shared" in error


def test_runtime_status_reports_missing_without_installing(capsys):
    result = main(
        ["runtime", "status"],
        environ={"LOCALAPPDATA": r"C:\Users\u\AppData\Local"},
        runtime_finder=lambda **_kwargs: None,
        version_info=(3, 10),
    )

    assert result == 1
    output = capsys.readouterr().out
    assert "Status: not installed" in output
    assert "Provider data: shared" in output


def test_runtime_status_labels_user_owned_override(capsys, tmp_path):
    runtime = Msys2Runtime(tmp_path / "user-msys", managed=False)

    result = main(
        ["runtime", "status"],
        environ={"RAILMUX_MSYS2_ROOT": str(runtime.root)},
        runtime_finder=lambda **_kwargs: runtime,
        version_info=(3, 10),
    )

    assert result == 0
    output = capsys.readouterr().out
    assert f"Runtime: user-owned override at {runtime.root}" in output
    assert "Managed location:" not in output


def test_ready_runtime_receives_exact_argv_and_child_environment(tmp_path):
    root = tmp_path / "msys"
    bash = root / "usr" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.write_bytes(b"fixture")
    runtime = Msys2Runtime(root, managed=False)
    process = MagicMock()
    process.wait.return_value = 17
    popen = MagicMock(return_value=process)
    arguments = ["ssh", "user@example", "--ssh-args=-J jump host", "开发"]

    result = main(
        arguments,
        environ={"PATH": r"C:\Windows", "USERPROFILE": r"C:\Users\用户"},
        runtime_finder=lambda **_kwargs: runtime,
        popen=popen,
        version_info=(3, 10),
    )

    assert result == 17
    assert popen.call_args.args[0][-len(arguments) :] == arguments
    assert popen.call_args.kwargs["env"]["HOME"] == r"C:\Users\用户"


def test_ctrl_c_does_not_kill_the_msys_handoff(tmp_path):
    root = tmp_path / "msys"
    bash = root / "usr" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.write_bytes(b"fixture")
    runtime = Msys2Runtime(root, managed=False)
    process = MagicMock()
    process.wait.side_effect = [KeyboardInterrupt, 23]
    popen = MagicMock(return_value=process)

    assert main(
        [],
        environ={},
        runtime_finder=lambda **_kwargs: runtime,
        popen=popen,
        version_info=(3, 10),
    ) == 23

    process.kill.assert_not_called()
    process.terminate.assert_not_called()
