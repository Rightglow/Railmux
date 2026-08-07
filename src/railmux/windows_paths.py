"""Stable native-Windows storage paths for the managed runtime.

Microsoft Store and other MSIX-packaged Python interpreters can virtualize new
writes beneath AppData.  Railmux's runtime contains executables that must be
opened by child processes outside that virtualized view, so packaged Python
uses a user-profile directory instead.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Mapping
from pathlib import Path


_ERROR_INSUFFICIENT_BUFFER = 122
_APPMODEL_ERROR_NO_PACKAGE = 15700
_PACKAGED_PROFILE_DIRECTORY = ".railmux"
_PACKAGED_WINDOWS_DIRECTORY = "windows"


def _has_windows_package_identity() -> bool:
    """Return whether this process has an MSIX/AppX package identity."""
    if os.name != "nt":
        return False
    try:
        get_family = ctypes.windll.kernel32.GetCurrentPackageFamilyName
        length = ctypes.c_uint32(0)
        result = int(get_family(ctypes.byref(length), None))
    except (AttributeError, OSError, TypeError, ValueError):
        return _looks_like_packaged_executable(sys.executable)
    if result == _APPMODEL_ERROR_NO_PACKAGE:
        return False
    if result == _ERROR_INSUFFICIENT_BUFFER and length.value > 0:
        return True
    # A Store Python launcher and interpreter both have characteristic physical
    # paths.  Keep this fallback fail-safe if the package API is unavailable or
    # returns an unexpected platform error.
    return _looks_like_packaged_executable(sys.executable)


def _looks_like_packaged_executable(executable: str) -> bool:
    normalized = executable.replace("/", "\\").casefold()
    return "\\windowsapps\\" in normalized or (
        "\\appdata\\local\\packages\\" in normalized and "\\localcache\\" in normalized
    )


def managed_windows_data_root(environ: Mapping[str, str]) -> Path | None:
    """Select one data root visible to both Python and MSYS2 child processes."""
    if _has_windows_package_identity():
        user_profile = environ.get("USERPROFILE", "").strip()
        if not user_profile:
            return None
        return (
            Path(user_profile)
            / _PACKAGED_PROFILE_DIRECTORY
            / _PACKAGED_WINDOWS_DIRECTORY
        )
    local_app_data = environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return None
    return Path(local_app_data) / "Railmux"


def legacy_local_app_data_root(environ: Mapping[str, str]) -> Path | None:
    """Return the pre-packaged-Python-fix location for read-only cache reuse."""
    local_app_data = environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return None
    return Path(local_app_data) / "Railmux"
