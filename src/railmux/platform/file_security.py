"""Portable checks for files stored below the current user's profile."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def private_regular_file(info: os.stat_result) -> bool:
    if not stat.S_ISREG(info.st_mode):
        return False
    if os.name == "nt":
        return True
    return info.st_uid == os.getuid() and not info.st_mode & 0o077


def prepare_private_directory(path: Path, *, tighten: bool = True) -> bool:
    """Validate/tighten a profile directory under the platform ACL model."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            return False
        if os.name == "nt":
            return True
        if info.st_uid != os.getuid():
            return False
        if info.st_mode & 0o077:
            if not tighten:
                return False
            os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o077)
        return True
    except OSError:
        return False
