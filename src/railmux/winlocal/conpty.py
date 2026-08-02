"""Replaceable ConPTY primitive backed initially by pywinpty."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import shutil
from typing import Protocol


class ConPtyProcess(Protocol):
    pid: int

    def read(self, size: int = 65536) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def resize(self, columns: int, rows: int) -> None: ...
    def is_alive(self) -> bool: ...
    def terminate(self, *, force: bool = False) -> bool: ...


class PyWinPtyProcess:
    """UTF-8 byte facade over ``winpty.PtyProcess``."""

    def __init__(self, process: object) -> None:
        self._process = process
        self.pid = int(process.pid)

    @classmethod
    def spawn(
        cls,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str],
        columns: int,
        rows: int,
    ) -> "PyWinPtyProcess":
        from winpty import PTY, PtyProcess

        command_line = _cmd_shim_command_line(argv)
        if command_line is not None:
            command = shutil.which(argv[0]) or argv[0]
            backend = os.environ.get("PYWINPTY_BACKEND")
            terminal = PTY(
                columns,
                rows,
                backend=int(backend) if backend is not None else None,
            )
            environment = (
                "\0".join(f"{key}={value}" for key, value in env.items()) + "\0"
            )
            terminal.spawn(
                command,
                cwd=os.getcwd() if cwd is None else str(cwd),
                env=environment,
                cmdline=command_line,
            )
            return cls(PtyProcess(terminal))

        process = PtyProcess.spawn(
            list(argv),
            cwd=None if cwd is None else str(cwd),
            env=dict(env),
            dimensions=(rows, columns),
        )
        return cls(process)

    def read(self, size: int = 65536) -> bytes:
        data = self._process.read(size)
        return (
            data
            if isinstance(data, bytes)
            else data.encode("utf-8", errors="replace")
        )

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8", errors="replace")
        return int(self._process.write(text))

    def resize(self, columns: int, rows: int) -> None:
        self._process.setwinsize(rows, columns)

    def is_alive(self) -> bool:
        return bool(self._process.isalive())

    def terminate(self, *, force: bool = False) -> bool:
        result = self._process.terminate(force=force)
        return not self._process.isalive() if result is None else bool(result)


def _cmd_shim_command_line(argv: Sequence[str]) -> str | None:
    """Return the exact cmd.exe tail pywinpty must not quote a second time."""
    if (
        len(argv) == 5
        and Path(argv[0]).name.casefold() == "cmd.exe"
        and tuple(value.casefold() for value in argv[1:4]) == ("/d", "/s", "/c")
    ):
        # /S removes this added outer pair. The remaining command retains the
        # list2cmdline quotes around the .cmd path and provider arguments.
        return f' /d /s /c "{argv[4]}"'
    return None
