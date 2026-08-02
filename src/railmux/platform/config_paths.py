"""Per-user configuration paths without changing the POSIX layout."""
from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    if os.name == "nt":
        roaming = os.environ.get("APPDATA")
        root = Path(roaming) if roaming else Path.home() / "AppData" / "Roaming"
        return root / "Railmux"
    return Path.home() / ".config" / "railmux"


def data_dir() -> Path:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        return root / "Railmux" / "data"
    raw = os.environ.get("XDG_DATA_HOME")
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate / "railmux"
    return Path.home() / ".local" / "share" / "railmux"


def default_config_path() -> Path:
    return config_dir() / "config.toml"
