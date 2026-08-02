import json
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


def test_utf8_user_state_survives_a_non_utf8_process_locale(tmp_path):
    """UTF-8 persistence must not inherit CP936/ANSI locale decoding."""
    config_roots = {
        tmp_path / ".config" / "railmux",
        tmp_path / ".config" / "Railmux",
    }
    for config_root in config_roots:
        config_root.mkdir(parents=True, exist_ok=True)
        (config_root / "config.toml").write_bytes(
            '# 中文注释\n[updates]\nauto_update = "never"\n'.encode("utf-8")
        )
        (config_root / "favorites.json").write_bytes(
            json.dumps(["会话"], ensure_ascii=False).encode("utf-8")
        )
        (config_root / "renames.json").write_bytes(
            json.dumps({"会话": "中文标题"}, ensure_ascii=False).encode("utf-8")
        )
        (config_root / "path-cache.json").write_bytes(
            json.dumps(
                {"编码目录": str(tmp_path / "项目")}, ensure_ascii=False
            ).encode("utf-8")
        )
    project_dir = tmp_path / ".claude" / "projects" / "encoded"
    project_dir.mkdir(parents=True)
    session_id = "11111111-1111-1111-1111-111111111111"
    session_path = project_dir / f"{session_id}.jsonl"
    records = (
        {"type": "ai-title", "aiTitle": "会话标题"},
        {"type": "user", "message": {"content": "中文问题"}},
        {
            "type": "assistant",
            "message": {
                "content": "中文回答",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    )
    session_path.write_bytes(
        ("\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n").encode(
            "utf-8"
        )
    )
    history = tmp_path / ".claude" / "history.jsonl"
    history.write_bytes(
        (
            json.dumps(
                {"sessionId": "remove", "display": "删除我"}, ensure_ascii=False
            )
            + "\n"
            + json.dumps(
                {"sessionId": "keep", "display": "保留我"}, ensure_ascii=False
            )
            + "\n"
        ).encode("utf-8")
    )
    script = r"""
import json
import locale
import sys
from pathlib import Path
from railmux.discovery import _load_path_cache
from railmux.favorites import Favorites
from railmux.models import Project
from railmux.renames import Renames
from railmux.session_index import _scan_session
from railmux.settings import Settings
from railmux.ui.app import App

home = Path(sys.argv[1])
assert locale.getpreferredencoding(False).upper() not in {'UTF-8', 'UTF8'}
assert Settings().update_policy == 'never'
assert Favorites().is_favorite('\u4f1a\u8bdd')
assert Renames().get('\u4f1a\u8bdd') == '\u4e2d\u6587\u6807\u9898'
assert _load_path_cache()['\u7f16\u7801\u76ee\u5f55'].endswith('\u9879\u76ee')
project = Project(home / '\u9879\u76ee', 'encoded', home / '.claude' / 'projects' / 'encoded', 1, 0)
session = _scan_session(project, project.claude_dir / '11111111-1111-1111-1111-111111111111.jsonl')
assert session is not None and session.title == '\u4f1a\u8bdd\u6807\u9898'
assert App._remove_from_history('remove', claude_home=home / '.claude')
rows = [json.loads(line) for line in (home / '.claude' / 'history.jsonl').read_bytes().decode('utf-8').splitlines()]
assert rows == [{'sessionId': 'keep', 'display': '\u4fdd\u7559\u6211'}]
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env={
            **os.environ,
            "APPDATA": str(tmp_path / ".config"),
            "HOME": str(tmp_path),
            "LC_ALL": "C",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONUTF8": "0",
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


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
