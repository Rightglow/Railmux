"""Best-effort native clipboard writers for explicit user copy actions."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def command() -> tuple[str, ...] | None:
    """Return a trusted platform clipboard command, if one is available."""
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
