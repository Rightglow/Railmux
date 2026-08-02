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
        if os.name == "nt":
            alive = _windows_process_alive(self.pid)
            if alive is not None:
                return alive
        return bool(self._process.isalive())

    def terminate(self, *, force: bool = False) -> bool:
        if os.name == "nt":
            if not self.is_alive():
                return True
            try:
                self._process.sendintr()
            except (EOFError, OSError):
                pass
            if _wait_until_stopped(self, 0.5):
                return True
            if not force:
                return False
            _terminate_windows_process(self.pid)
            return _wait_until_stopped(self, 2.0)
        result = self._process.terminate(force=force)
        return not self._process.isalive() if result is None else bool(result)


def _wait_until_stopped(process: "PyWinPtyProcess", timeout: float) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process.is_alive():
            return True
        time.sleep(0.02)
    return not process.is_alive()


def _windows_process_alive(pid: int) -> bool | None:
    """Query the exact child PID without trusting pywinpty's delayed status."""
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    query_limited = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(synchronize | query_limited, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: the PID no longer exists.
            return False
        return None
    try:
        code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(code)):
            return None
        return code.value == still_active
    finally:
        close_handle(handle)


def _terminate_windows_process(pid: int) -> None:
    """Force-stop the daemon-owned process via its exact Windows PID."""
    import ctypes
    from ctypes import wintypes

    process_terminate = 0x0001
    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    terminate_process = kernel32.TerminateProcess
    terminate_process.argtypes = (wintypes.HANDLE, wintypes.UINT)
    terminate_process.restype = wintypes.BOOL
    wait_one = kernel32.WaitForSingleObject
    wait_one.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_one.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_terminate | synchronize, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # Process already exited between checks.
            return
        raise ctypes.WinError(error)
    try:
        if not terminate_process(handle, 1):
            error = ctypes.get_last_error()
            if error != 5 or _windows_process_alive(pid) is not False:
                raise ctypes.WinError(error)
        wait_one(handle, 2000)
    finally:
        close_handle(handle)


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
