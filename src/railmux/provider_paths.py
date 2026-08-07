"""Translate provider-owned native Windows paths inside the MSYS2 preview."""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

from railmux.release_version import PROJECT_VERSION_PATTERN


_DRIVE_PATH = re.compile(r"^(?:\\\\\?\\)?([A-Za-z]):[\\/](.*)\Z")
_MANAGED_BASE_MARKER = Path("/railmux-base.json")
_MANAGED_BASE_CONTENT_MARKER = Path("/railmux-base-content-v1.json")
_MANAGED_APP_ROOT = Path("/opt/railmux/apps")
_MANAGED_APP_ID = re.compile(
    rf"railmux-{PROJECT_VERSION_PATTERN}\Z"
)


def running_in_windows_wrapper(environ: Mapping[str, str] = os.environ) -> bool:
    """Return whether Railmux is running in its native-Windows MSYS2 wrapper."""
    return environ.get("RAILMUX_WINDOWS_RUNTIME") == "msys2"


def running_in_managed_windows_wrapper(
    environ: Mapping[str, str] = os.environ,
) -> bool:
    """Verify the installed MSYS2 runtime, not merely its environment hint."""
    if sys.platform != "cygwin" or not running_in_windows_wrapper(environ):
        return False
    runtime_id = environ.get("RAILMUX_MSYS2_RUNTIME_ID")
    app_id = environ.get("RAILMUX_MSYS2_APP_ID")
    if (
        not isinstance(runtime_id, str)
        or not runtime_id
        or not isinstance(app_id, str)
        or _MANAGED_APP_ID.fullmatch(app_id) is None
    ):
        return False

    def read_safe_marker(marker: Path) -> object | None:
        try:
            info = marker.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
                or info.st_size > 4096
            ):
                return None
            return json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

    base_payload = read_safe_marker(_MANAGED_BASE_MARKER)
    app_payload = read_safe_marker(
        _MANAGED_APP_ROOT / app_id / "railmux-app.json"
    )
    from railmux import __version__

    base_valid = base_payload == {
        "schema": 1,
        "runtime": runtime_id,
    }
    legacy_app = app_payload == {
        "schema": 1,
        "runtime": runtime_id,
        "railmux": __version__,
    }
    content_payload = read_safe_marker(_MANAGED_BASE_CONTENT_MARKER)
    content_id = (
        content_payload.get("content_id")
        if isinstance(content_payload, dict)
        and content_payload.get("schema") == 1
        and content_payload.get("runtime") == runtime_id
        else None
    )
    exact_app = bool(
        isinstance(content_id, str)
        and re.fullmatch(r"[0-9a-f]{64}", content_id)
        and app_payload == {
            "schema": 2,
            "runtime": runtime_id,
            "railmux": __version__,
            "base_content_id": content_id,
        }
    )
    return base_valid and (legacy_app or exact_app)


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
