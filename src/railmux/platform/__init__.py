"""Small operating-system ports used by Railmux launchers.

Product behavior does not belong in this package. Keeping platform checks at
this boundary lets Windows launchers coexist with the established POSIX tmux
runtime.
"""
from __future__ import annotations

import os
import sys
from typing import Sequence


WINDOWS_MIN_PYTHON = (3, 10)


def is_windows() -> bool:
    """Return whether this process is running on native Windows."""
    return os.name == "nt"


def python_support_error(
    *,
    windows: bool | None = None,
    version_info: Sequence[int] | None = None,
) -> str | None:
    """Return an actionable platform-specific Python version error."""
    on_windows = is_windows() if windows is None else windows
    version = sys.version_info if version_info is None else version_info
    if on_windows and tuple(version[:2]) < WINDOWS_MIN_PYTHON:
        found = ".".join(str(part) for part in version[:3])
        return (
            "Railmux requires Python 3.10 or newer on native Windows "
            f"(found {found}); Linux, macOS, and WSL retain Python 3.9 support"
        )
    return None


def require_supported_python() -> None:
    """Stop before platform-only imports when the interpreter is unsupported."""
    error = python_support_error()
    if error is not None:
        raise RuntimeError(error)

