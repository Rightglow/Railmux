import os
import time

import pytest

from railmux.platform.process import provider_argv
from railmux.winlocal.conpty import PyWinPtyProcess


@pytest.mark.skipif(os.name != "nt", reason="requires Windows ConPTY")
def test_real_conpty_starts_reads_resizes_and_exits():
    process = PyWinPtyProcess.spawn(
        ["cmd.exe", "/d", "/s", "/c", "echo RAILMUX_CONPTY_OK"],
        cwd=None,
        env=dict(os.environ),
        columns=80,
        rows=24,
    )
    process.resize(100, 30)
    output = bytearray()
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline and b"RAILMUX_CONPTY_OK" not in output:
            try:
                output.extend(process.read(65536))
            except EOFError:
                break
        assert b"RAILMUX_CONPTY_OK" in output
    finally:
        if process.is_alive():
            process.terminate(force=True)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows ConPTY")
def test_real_conpty_launches_cmd_shim_from_path_with_spaces(tmp_path):
    directory = tmp_path / "shim directory"
    directory.mkdir()
    shim = directory / "provider.cmd"
    shim.write_text(
        "@echo off\r\necho RAILMUX_SHIM_OK:%~1\r\n", encoding="utf-8"
    )
    argv = provider_argv(str(shim), ("value with spaces",), windows=True)
    process = PyWinPtyProcess.spawn(
        argv,
        cwd=tmp_path,
        env=dict(os.environ),
        columns=80,
        rows=24,
    )
    output = bytearray()
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline and b"RAILMUX_SHIM_OK" not in output:
            try:
                output.extend(process.read(65536))
            except EOFError:
                break
        assert b"RAILMUX_SHIM_OK:value with spaces" in output
    finally:
        if process.is_alive():
            process.terminate(force=True)
