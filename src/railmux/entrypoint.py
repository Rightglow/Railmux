"""Platform-safe public console entry point."""
from __future__ import annotations

import os
from collections.abc import Sequence


def main(
    argv: Sequence[str] | None = None,
    *,
    platform_name: str = os.name,
) -> int:
    """Dispatch before importing modules that require a POSIX terminal."""
    if platform_name == "nt":
        from railmux.windows_bootstrap import main as windows_main

        return windows_main(argv)

    from railmux.cli import main as posix_main

    return posix_main(list(argv) if argv is not None else None)
