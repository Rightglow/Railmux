"""Read-readiness ports for POSIX descriptors and Windows kernel handles."""
from __future__ import annotations

import os
import selectors
import time
from collections.abc import Iterable
from typing import Any


def wait_readable(readers: Iterable[int], timeout: float | None) -> list[int]:
    """Return descriptors which can be read without blocking."""
    fds = list(readers)
    if not fds:
        return []
    if os.name != "nt":
        import select

        readable, _writable, _exceptional = select.select(fds, [], [], timeout)
        return list(readable)
    return _wait_windows(fds, timeout)


def _wait_windows(fds: list[int], timeout: float | None) -> list[int]:
    import ctypes
    import msvcrt

    if len(fds) > 64:
        raise ValueError("Windows can wait for at most 64 handles")
    handles = [msvcrt.get_osfhandle(fd) for fd in fds]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileType.argtypes = (ctypes.c_void_p,)
    kernel32.GetFileType.restype = ctypes.c_uint32
    kernel32.PeekNamedPipe.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    )
    kernel32.PeekNamedPipe.restype = ctypes.c_int
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32

    file_type_pipe = 0x0003
    wait_object_0 = 0
    wait_timeout = 0x102
    wait_failed = 0xFFFFFFFF
    broken_pipe_errors = {109, 232}  # ERROR_BROKEN_PIPE, ERROR_NO_DATA
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)

    # WaitForMultipleObjects supports console input, but not pipe objects.
    # ssh.exe stdout is a synchronous anonymous pipe, so poll it without
    # consuming bytes and use a zero-time wait for console/event-like handles.
    while True:
        ready: list[int] = []
        for fd, handle in zip(fds, handles):
            if kernel32.GetFileType(handle) == file_type_pipe:
                available = ctypes.c_uint32()
                if kernel32.PeekNamedPipe(
                    handle, None, 0, None, ctypes.byref(available), None
                ):
                    if available.value:
                        ready.append(fd)
                    continue
                error = ctypes.get_last_error()
                if error in broken_pipe_errors:
                    # Let the caller's read observe EOF.
                    ready.append(fd)
                    continue
                raise OSError(error, "PeekNamedPipe failed")
            result = kernel32.WaitForSingleObject(handle, 0)
            if result == wait_object_0:
                ready.append(fd)
            elif result not in {wait_timeout}:
                if result == wait_failed:
                    raise OSError(
                        ctypes.get_last_error(), "WaitForSingleObject failed"
                    )
                raise OSError(f"unexpected WaitForSingleObject result: {result}")
        if ready:
            return ready
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            time.sleep(min(0.01, remaining))
        else:
            time.sleep(0.01)


class PortableSelector:
    """The small DefaultSelector surface used by the display client."""

    def __init__(self) -> None:
        self._selector: selectors.BaseSelector | None = None
        self._keys: dict[int, selectors.SelectorKey] = {}
        if os.name != "nt":
            self._selector = selectors.DefaultSelector()

    def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        if self._selector is not None:
            return self._selector.register(fileobj, events, data)
        if events != selectors.EVENT_READ:
            raise ValueError("Windows display selector supports read events only")
        fd = fileobj if isinstance(fileobj, int) else fileobj.fileno()
        if fd in self._keys:
            raise KeyError(fd)
        key = selectors.SelectorKey(fileobj, fd, events, data)
        self._keys[fd] = key
        return key

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        if self._selector is not None:
            return self._selector.unregister(fileobj)
        fd = fileobj if isinstance(fileobj, int) else fileobj.fileno()
        try:
            return self._keys.pop(fd)
        except KeyError as exc:
            raise KeyError(fileobj) from exc

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        if self._selector is not None:
            return self._selector.select(timeout)
        ready = wait_readable(self._keys, timeout)
        return [(self._keys[fd], selectors.EVENT_READ) for fd in ready]

    def close(self) -> None:
        if self._selector is not None:
            self._selector.close()
        self._keys.clear()
