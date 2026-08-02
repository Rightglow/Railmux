"""Raw console mode with matching POSIX and native-Windows implementations."""
from __future__ import annotations

import os
from types import TracebackType
from typing import Optional, Type


class RawConsole:
    """Temporarily put one terminal input descriptor into byte-oriented mode."""

    def __init__(self, fd: int, *, output_fd: int | None = None) -> None:
        self.fd = fd
        self.output_fd = output_fd
        self._saved_posix: Optional[list[object]] = None
        self._saved_windows: int | None = None
        self._saved_windows_output: int | None = None
        self._saved_windows_fd_mode: int | None = None
        self._saved_windows_output_fd_mode: int | None = None
        self._saved_windows_input_cp: int | None = None
        self._saved_windows_output_cp: int | None = None

    def __enter__(self) -> "RawConsole":
        if os.name == "nt":
            self._enter_windows()
        else:
            self._enter_posix()
        return self

    def __exit__(
        self,
        _exc_type: Type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if os.name == "nt":
            self._exit_windows()
        else:
            self._exit_posix()

    def _enter_posix(self) -> None:
        import termios
        import tty

        self._saved_posix = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)

    def _exit_posix(self) -> None:
        if self._saved_posix is None:
            return
        import termios

        termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved_posix)
        self._saved_posix = None

    def _enter_windows(self) -> None:
        import ctypes
        import msvcrt

        kernel32 = _windows_kernel32()
        handle = msvcrt.get_osfhandle(self.fd)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            raise OSError(ctypes.get_last_error(), "GetConsoleMode failed")
        self._saved_windows_fd_mode = msvcrt.setmode(self.fd, os.O_BINARY)
        enable_extended_flags = 0x0080
        enable_virtual_terminal_input = 0x0200
        disabled = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020 | 0x0040
        raw_mode = (mode.value & ~disabled) | (
            enable_extended_flags | enable_virtual_terminal_input
        )
        if not kernel32.SetConsoleMode(handle, raw_mode):
            error = ctypes.get_last_error()
            msvcrt.setmode(self.fd, self._saved_windows_fd_mode)
            self._saved_windows_fd_mode = None
            raise OSError(error, "SetConsoleMode failed")
        self._saved_windows = mode.value
        self._saved_windows_input_cp = kernel32.GetConsoleCP()
        if not kernel32.SetConsoleCP(65001):
            error = ctypes.get_last_error()
            self._exit_windows()
            raise OSError(error, "SetConsoleCP failed")
        if self.output_fd is not None:
            self._saved_windows_output_fd_mode = msvcrt.setmode(
                self.output_fd, os.O_BINARY
            )
            output_handle = msvcrt.get_osfhandle(self.output_fd)
            output_mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(output_handle, ctypes.byref(output_mode)):
                error = ctypes.get_last_error()
                self._exit_windows()
                raise OSError(error, "GetConsoleMode output failed")
            vt_output = output_mode.value | 0x0001 | 0x0004 | 0x0008
            if not kernel32.SetConsoleMode(output_handle, vt_output):
                error = ctypes.get_last_error()
                self._exit_windows()
                raise OSError(error, "SetConsoleMode output failed")
            self._saved_windows_output = output_mode.value
            self._saved_windows_output_cp = kernel32.GetConsoleOutputCP()
            if not kernel32.SetConsoleOutputCP(65001):
                error = ctypes.get_last_error()
                self._exit_windows()
                raise OSError(error, "SetConsoleOutputCP failed")

    def _exit_windows(self) -> None:
        if self._saved_windows is None:
            return
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(self.fd)
        kernel32 = _windows_kernel32()
        output_error = 0
        if self._saved_windows_output_cp is not None:
            if not kernel32.SetConsoleOutputCP(self._saved_windows_output_cp):
                output_error = ctypes.get_last_error()
            self._saved_windows_output_cp = None
        if self._saved_windows_output is not None and self.output_fd is not None:
            output_handle = msvcrt.get_osfhandle(self.output_fd)
            if not kernel32.SetConsoleMode(output_handle, self._saved_windows_output):
                output_error = ctypes.get_last_error()
            self._saved_windows_output = None
        if self._saved_windows_output_fd_mode is not None and self.output_fd is not None:
            msvcrt.setmode(self.output_fd, self._saved_windows_output_fd_mode)
            self._saved_windows_output_fd_mode = None
        input_cp_error = 0
        if self._saved_windows_input_cp is not None:
            if not kernel32.SetConsoleCP(self._saved_windows_input_cp):
                input_cp_error = ctypes.get_last_error()
            self._saved_windows_input_cp = None
        if not kernel32.SetConsoleMode(handle, self._saved_windows):
            error = ctypes.get_last_error()
            self._saved_windows = None
            raise OSError(error, "SetConsoleMode restore failed")
        self._saved_windows = None
        if self._saved_windows_fd_mode is not None:
            msvcrt.setmode(self.fd, self._saved_windows_fd_mode)
            self._saved_windows_fd_mode = None
        if output_error:
            raise OSError(output_error, "SetConsoleMode output restore failed")
        if input_cp_error:
            raise OSError(input_cp_error, "SetConsoleCP restore failed")


def _windows_kernel32():
    """Load console APIs with pointer-width-safe ctypes declarations."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleMode.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    )
    kernel32.GetConsoleMode.restype = ctypes.c_int
    kernel32.SetConsoleMode.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.SetConsoleMode.restype = ctypes.c_int
    for name in ("GetConsoleCP", "GetConsoleOutputCP"):
        function = getattr(kernel32, name)
        function.argtypes = ()
        function.restype = ctypes.c_uint32
    for name in ("SetConsoleCP", "SetConsoleOutputCP"):
        function = getattr(kernel32, name)
        function.argtypes = (ctypes.c_uint32,)
        function.restype = ctypes.c_int
    return kernel32
