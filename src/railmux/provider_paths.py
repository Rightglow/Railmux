"""Translate provider-owned native Windows paths inside the MSYS2 preview."""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from pathlib import Path


_DRIVE_PATH = re.compile(r"^(?:\\\\\?\\)?([A-Za-z]):[\\/](.*)\Z")
_MANAGED_RUNTIME_MARKER = Path("/railmux-runtime.json")


def running_in_windows_wrapper(environ: Mapping[str, str] = os.environ) -> bool:
    """Return whether Railmux is running in its native-Windows MSYS2 wrapper."""
    return environ.get("RAILMUX_WINDOWS_RUNTIME") == "msys2"


def running_in_managed_windows_wrapper(
    environ: Mapping[str, str] = os.environ,
) -> bool:
    """Verify the installed MSYS2 runtime, not merely its environment hint."""
    if sys.platform != "cygwin" or not running_in_windows_wrapper(environ):
        return False
    marker = _MANAGED_RUNTIME_MARKER
    try:
        info = marker.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or info.st_size > 4096
        ):
            return False
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    from railmux import __version__
    runtime_id = environ.get("RAILMUX_MSYS2_RUNTIME_ID")
    return isinstance(runtime_id, str) and bool(runtime_id) and payload == {
        "schema": 1,
        "runtime": runtime_id,
        "railmux": __version__,
    }


def private_mode_is_safe(mode: int) -> bool:
    """Accept strict POSIX modes or the verified MSYS2 noacl projection."""
    return not mode & 0o077 or (
        running_in_managed_windows_wrapper() and not mode & 0o022
    )


def provider_path(
    raw: str,
    *,
    environ: Mapping[str, str] = os.environ,
) -> Path:
    """Return a POSIX-visible path for provider metadata.

    Native providers write Windows paths into session metadata. Railmux runs
    under MSYS2, where the same locations are mounted as ``/c/...`` (or
    ``//server/share/...`` for UNC paths). Outside the wrapper, preserve the
    existing POSIX behavior exactly.
    """
    if not running_in_windows_wrapper(environ):
        return Path(raw)

    match = _DRIVE_PATH.match(raw)
    if match is not None:
        drive, tail = match.groups()
        return Path(f"/{drive.lower()}/{tail.replace(chr(92), '/')}")

    if raw.startswith("\\\\"):
        return Path("//" + raw[2:].replace("\\", "/"))

    return Path(raw.replace("\\", "/"))
