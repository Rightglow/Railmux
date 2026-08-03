"""Platform-safe public entry point.

Keep this module free of POSIX-only imports.  The Windows preview must choose
its delegated runtime before importing the ordinary Railmux CLI, which owns
``termios`` and the tmux-backed application.
"""
from __future__ import annotations

import os
from collections.abc import Sequence


def main(
    argv: Sequence[str] | None = None,
    *,
    platform_name: str | None = None,
) -> int:
    current_platform = os.name if platform_name is None else platform_name
    if current_platform == "nt":
        from railmux.windows_bootstrap import main as windows_main

        return windows_main(argv)

    from railmux.cli import main as posix_main

    return posix_main(None if argv is None else list(argv))
