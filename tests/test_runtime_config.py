from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from railmux.config import Config
from railmux.runtime_config import (
    TMUX_BINARY_ENV,
    check_executable,
    check_utf8_locale,
    runtime_environment,
)


def test_native_windows_locale_uses_vt_unicode(monkeypatch):
    monkeypatch.setattr("railmux.runtime_config.is_windows", lambda: True)

    assert check_utf8_locale("C.UTF-8") == (
        True,
        "native Windows VT uses Unicode",
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX tmux PATH authority")
def test_runtime_environment_prepends_configured_tmux_and_sets_locale(tmp_path):
    binary = tmp_path / "bin" / "tmux"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    config = Config(tmux_binary=str(binary), locale="C.UTF-8")

    result = runtime_environment(config, {"PATH": "/usr/bin", "LANG": "C"})

    assert result["PATH"].split(os.pathsep) == [str(binary.parent), "/usr/bin"]
    assert result["LC_ALL"] == "C.UTF-8"
    assert result["LANG"] == "C"
    assert result[TMUX_BINARY_ENV] == str(binary)


def test_tmux_override_requires_the_conventional_executable_name(tmp_path):
    binary = tmp_path / "tmux-3.4"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)

    result = check_executable("tmux", str(binary))

    assert not result.valid
    assert "named 'tmux'" in (result.error or "")


def test_real_python_command_and_utf8_locale_validate(monkeypatch):
    result = check_executable("codex", os.sys.executable)
    monkeypatch.setattr(
        "railmux.runtime_config.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="UTF-8\n", stderr="", returncode=0
        ),
    )
    valid, detail = check_utf8_locale("C.UTF-8")

    assert result.valid
    assert Path(result.resolved or "").is_file()
    assert valid, detail
