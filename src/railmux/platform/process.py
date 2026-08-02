"""Platform process argv rules shared by provider launchers."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence


def provider_argv(
    executable: str,
    arguments: Sequence[str],
    *,
    windows: bool | None = None,
) -> tuple[str, ...]:
    """Return an argv-only launch for native executables and npm shims."""
    on_windows = os.name == "nt" if windows is None else windows
    if on_windows and executable.lower().endswith((".cmd", ".bat")):
        command = subprocess.list2cmdline([executable, *arguments])
        return ("cmd.exe", "/d", "/s", "/c", command)
    return (executable, *arguments)

