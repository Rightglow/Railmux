import os
import subprocess
import sys
from pathlib import Path

import pytest

from railmux.platform import python_support_error
from railmux.platform.multiplex import PortableSelector, wait_readable
from railmux.platform.process import provider_argv


def test_windows_python_floor_is_310_without_changing_posix_floor():
    assert python_support_error(windows=True, version_info=(3, 9, 19)) == (
        "Railmux requires Python 3.10 or newer on native Windows "
        "(found 3.9.19); Linux, macOS, and WSL retain Python 3.9 support"
    )
    assert python_support_error(windows=True, version_info=(3, 10, 0)) is None
    assert python_support_error(windows=False, version_info=(3, 9, 0)) is None


def test_posix_wait_readable_preserves_descriptor_data():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"ready")
        assert wait_readable([read_fd], 0.1) == [read_fd]
        assert os.read(read_fd, 5) == b"ready"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_portable_selector_preserves_registration_data():
    read_fd, write_fd = os.pipe()
    selector = PortableSelector()
    try:
        key = selector.register(read_fd, 1, "pipe")
        assert key.data == "pipe"
        os.write(write_fd, b"x")
        events = selector.select(0.1)
        assert [(event.data, mask) for event, mask in events] == [("pipe", 1)]
        assert os.read(read_fd, 1) == b"x"
        assert selector.unregister(read_fd).data == "pipe"
    finally:
        selector.close()
        os.close(read_fd)
        os.close(write_fd)


def test_windows_launcher_imports_do_not_require_posix_only_modules(tmp_path):
    script = """
import importlib.abc
import sys

class BlockPosix(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'fcntl', 'termios', 'tty'}:
            raise ImportError(f'blocked POSIX module: {fullname}')
        return None

sys.meta_path.insert(0, BlockPosix())
import railmux.cli
import railmux.diagnostics
import railmux.fast_display_client
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_windows_npm_provider_shim_uses_one_bounded_cmd_command():
    argv = provider_argv(
        r"C:\Program Files\nodejs\codex.cmd",
        ("resume", "session with spaces"),
        windows=True,
    )

    assert argv[:4] == ("cmd.exe", "/d", "/s", "/c")
    assert '"C:\\Program Files\\nodejs\\codex.cmd"' in argv[4]
    assert '"session with spaces"' in argv[4]


def test_posix_provider_launch_remains_direct_argv():
    assert provider_argv("/opt/bin/codex", ("resume", "abc"), windows=False) == (
        "/opt/bin/codex",
        "resume",
        "abc",
    )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows pipe handles")
def test_windows_selector_waits_for_real_subprocess_pipe():
    process = subprocess.Popen(
        [sys.executable, "-c", "print('ready', flush=True)"],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    selector = PortableSelector()
    try:
        selector.register(process.stdout.fileno(), 1, "pipe")
        events = selector.select(5.0)
        assert [(event.data, mask) for event, mask in events] == [("pipe", 1)]
        assert process.stdout.readline().strip() == b"ready"
        assert process.wait(timeout=5) == 0
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows console APIs")
def test_windows_console_api_signatures_preserve_handle_width():
    from railmux.platform.console import _windows_kernel32

    kernel32 = _windows_kernel32()

    assert kernel32.GetConsoleMode.argtypes[0].__name__ == "c_void_p"
    assert kernel32.SetConsoleMode.argtypes[0].__name__ == "c_void_p"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows security APIs")
def test_windows_runtime_directory_applies_private_dacl(tmp_path):
    from railmux.platform.runtime_paths import ensure_private_dir

    directory = tmp_path / "private runtime"
    ensure_private_dir(directory)

    assert directory.is_dir()
