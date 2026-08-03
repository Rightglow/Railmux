"""Translate provider-owned native Windows paths inside the MSYS2 preview."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path


_DRIVE_PATH = re.compile(r"^(?:\\\\\?\\)?([A-Za-z]):[\\/](.*)\Z")


def running_in_windows_wrapper(environ: Mapping[str, str] = os.environ) -> bool:
    """Return whether Railmux is running in its native-Windows MSYS2 wrapper."""
    return environ.get("RAILMUX_WINDOWS_RUNTIME") == "msys2"


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
