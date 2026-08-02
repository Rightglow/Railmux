"""Best-effort native clipboard writers for explicit user copy actions."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def command() -> tuple[str, ...] | None:
    """Return a trusted platform clipboard command, if one is available."""
    if os.name == "nt":
        return None
    if sys.platform == "darwin":
        if executable := shutil.which("pbcopy"):
            return (executable,)
        return None
    if os.environ.get("WAYLAND_DISPLAY"):
        if executable := shutil.which("wl-copy"):
            return (executable,)
    if os.environ.get("DISPLAY"):
        if executable := shutil.which("xclip"):
            return (executable, "-selection", "clipboard")
        if executable := shutil.which("xsel"):
            return (executable, "--clipboard", "--input")
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        if executable := shutil.which("clip.exe"):
            return (executable,)
    return None


def copy(data: bytes) -> bool:
    """Write bytes to the local OS clipboard without invoking a shell."""
    if os.name == "nt":
        return _copy_windows(data)
    writer = command()
    if writer is None:
        return False
    try:
        result = subprocess.run(
            writer,
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _copy_windows(data: bytes) -> bool:
    """Write bounded UTF-8 data as CF_UNICODETEXT using the Win32 API."""
    import ctypes

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if "\x00" in text:
        return False
    encoded = (text + "\x00").encode("utf-16-le")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
    user32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
    user32.SetClipboardData.restype = ctypes.c_void_p
    movable = 0x0002
    handle = kernel32.GlobalAlloc(movable, len(encoded))
    if not handle:
        return False
    owned_by_clipboard = False
    try:
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return False
        try:
            ctypes.memmove(pointer, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.OpenClipboard(None):
            return False
        try:
            if not user32.EmptyClipboard():
                return False
            if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT
                return False
            owned_by_clipboard = True
            return True
        finally:
            user32.CloseClipboard()
    finally:
        if not owned_by_clipboard:
            kernel32.GlobalFree(handle)
