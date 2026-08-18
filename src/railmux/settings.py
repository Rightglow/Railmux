"""App-mutable preferences in Railmux's single ``config.toml``.

Users and the Options UI share ``~/.config/railmux/config.toml``. TOMLKit
preserves comments, ordering, formatting, and unknown keys while Railmux
atomically updates only the settings it owns. Current-run choices stay in
memory; a next-launch layout profile is removed after it is consumed.
"""
from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from railmux.atomic_file import atomic_write_text
from railmux.config import default_config_path
from railmux.setting_contracts import bounds_for, choices_for


MANAGED_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "tmux": ("binary",),
    "claude": ("binary",),
    "codex": ("binary", "home", "auto_run"),
    "environment": ("locale",),
    "ui": ("layout_retention", "layout_profile"),
    "updates": ("auto_update",),
    "projects": ("show_empty_projects",),
    "live": ("poll_interval_ms", "agent_transport"),
    "interaction": ("history_lines", "claude_history", "path_open"),
    "ssh": ("history_lines", "claude_history", "path_open"),
}


def _config_path() -> Path:
    return default_config_path()


@dataclass(frozen=True)
class LayoutProfile:
    """Validated, size-independent outer-workspace geometry preference."""

    scope: str
    layout: str
    sidebar_permille: int
    primary_permille: int | None = None

    def to_toml(self) -> dict[str, object]:
        data: dict[str, object] = {
            "version": 1,
            "scope": self.scope,
            "layout": self.layout,
            "sidebar_permille": self.sidebar_permille,
        }
        if self.primary_permille is not None:
            data["primary_permille"] = self.primary_permille
        return data


def _plain(value: object) -> object:
    unwrap = getattr(value, "unwrap", None)
    return unwrap() if callable(unwrap) else value


def _decode_layout_profile(raw: object) -> LayoutProfile | None:
    plain = _plain(raw)
    if not isinstance(plain, dict) or len(plain) > 5 or plain.get("version") != 1:
        return None
    if not set(plain).issubset({
        "version", "scope", "layout", "sidebar_permille", "primary_permille",
    }):
        return None
    scope = plain.get("scope")
    layout = plain.get("layout")
    sidebar = plain.get("sidebar_permille")
    primary = plain.get("primary_permille")
    if scope not in {"always", "once"}:
        return None
    if layout not in {"single", "side-by-side", "stacked"}:
        return None
    if (not isinstance(sidebar, int) or isinstance(sidebar, bool)
            or not 50 <= sidebar <= 800):
        return None
    if primary is not None and (
        not isinstance(primary, int)
        or isinstance(primary, bool)
        or not 100 <= primary <= 900
    ):
        return None
    if layout == "single" and primary is not None:
        return None
    return LayoutProfile(scope, layout, sidebar, primary)


def _inline_table(values: dict[str, object]) -> tomlkit.items.InlineTable:
    result = tomlkit.inline_table()
    for key, value in values.items():
        result[key] = value
    return result


class Settings:
    """Validated mutable subset of the shared TOML configuration."""

    def __init__(self) -> None:
        self._path = _config_path()
        self._document = tomlkit.document()
        self._load()

    def _read_document(self):
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return tomlkit.document()
        except (OSError, UnicodeError):
            return None
        try:
            return tomlkit.parse(text)
        except TOMLKitError:
            return None

    def _load(self) -> None:
        document = self._read_document()
        if document is not None:
            self._document = document

    def _get(self, section_name: str, key: str) -> object | None:
        section = self._document.get(section_name)
        if not isinstance(section, MutableMapping):
            return None
        return _plain(section.get(key))

    @staticmethod
    def _table(document, section_name: str) -> MutableMapping | None:
        section = document.get(section_name)
        if section is None:
            section = tomlkit.table()
            document[section_name] = section
        return section if isinstance(section, MutableMapping) else None

    def _replace(self, document) -> bool:
        try:
            atomic_write_text(self._path, tomlkit.dumps(document))
        except OSError:
            return False
        self._document = document
        return True

    def _update_section(
        self,
        section_name: str,
        values: dict[str, Any],
        *,
        remove: tuple[str, ...] = (),
    ) -> bool:
        # Re-read immediately before every mutation. This preserves valid
        # manual edits and changes made by another Railmux process after this
        # instance started instead of rewriting a stale startup snapshot.
        updated = self._read_document()
        if updated is None:
            return False
        section = self._table(updated, section_name)
        if section is None:
            return False
        for key in remove:
            section.pop(key, None)
        for key, value in values.items():
            section[key] = value
        return self._replace(updated)

    def _remove_keys(self, keys: dict[str, tuple[str, ...]]) -> bool:
        """Remove only Railmux-owned keys while preserving every unknown key."""
        updated = self._read_document()
        if updated is None:
            return False
        for section_name, names in keys.items():
            section = updated.get(section_name)
            if not isinstance(section, MutableMapping):
                continue
            for name in names:
                section.pop(name, None)
            if not section:
                updated.pop(section_name, None)
        return self._replace(updated)

    def reset_keys(self, keys: dict[str, tuple[str, ...]]) -> bool:
        return self._remove_keys(keys)

    def reset_all(self) -> bool:
        return self._remove_keys(MANAGED_CONFIG_KEYS)

    # -- Program paths and locale --------------------------------------
    def set_program_binary(self, section: str, value: str) -> bool:
        if section not in {"tmux", "claude", "codex"} or not value.strip():
            return False
        return self._update_section(section, {"binary": value})

    def set_locale(self, value: str) -> bool:
        if not value.strip():
            return False
        return self._update_section("environment", {"locale": value})

    def set_history_lines(self, value: int) -> bool:
        minimum, maximum = bounds_for("interaction.history_lines")
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not minimum <= value <= maximum
        ):
            return False
        return self._update_section("interaction", {"history_lines": value})

    def set_ssh_history_lines(self, value: int) -> bool:
        """Compatibility wrapper for integrations using the 0.4.0 API."""
        return self.set_history_lines(value)

    # -- Codex auto-run --------------------------------------------------
    @property
    def codex_yolo_policy(self) -> str:
        policy = self._get("codex", "auto_run")
        return (
            policy if policy in choices_for("codex.auto_run") else "ask"
        )

    def set_codex_yolo_policy(self, policy: str) -> bool:
        if policy not in choices_for("codex.auto_run"):
            return False
        return self._update_section("codex", {"auto_run": policy})

    # -- Package updates -------------------------------------------------
    @property
    def update_policy(self) -> str:
        policy = self._get("updates", "auto_update")
        return (
            policy if policy in choices_for("updates.auto_update") else "ask"
        )

    def set_update_policy(self, policy: str) -> bool:
        if policy not in choices_for("updates.auto_update"):
            return False
        return self._update_section("updates", {"auto_update": policy})

    # -- Transport-managed Claude history -------------------------------
    @property
    def claude_history_policy(self) -> str:
        # The fast-display helper is a separate process and may persist the
        # first-scroll choice while the TUI is already running. Reload so a
        # later Options visit reflects that confirmed remote write.
        self._load()
        policy = self._get("interaction", "claude_history")
        if policy is None:
            policy = self._get("ssh", "claude_history")
        return (
            policy
            if isinstance(policy, str)
            and policy in choices_for("interaction.claude_history")
            else "ask"
        )

    def set_claude_history_policy(self, policy: str) -> bool:
        if policy not in choices_for("interaction.claude_history"):
            return False
        return self._update_section("interaction", {"claude_history": policy})

    # -- Semantic clicked paths -----------------------------------------
    @property
    def path_open_policy(self) -> str:
        self._load()
        policy = self._get("interaction", "path_open")
        if policy is None:
            policy = self._get("ssh", "path_open")
        return (
            policy
            if isinstance(policy, str)
            and policy in choices_for("interaction.path_open")
            else "ask"
        )

    def set_path_open_policy(self, policy: str) -> bool:
        if policy not in choices_for("interaction.path_open"):
            return False
        # Do not delete the legacy key here: another running 0.3.x process
        # may still rely on it. The canonical key wins immediately and reset
        # operations know about both spellings.
        return self._update_section("interaction", {"path_open": policy})

    # -- Saved outer-workspace geometry ---------------------------------
    @property
    def layout_save_policy(self) -> str:
        policy = self._get("ui", "layout_retention")
        return (
            policy if policy in choices_for("ui.layout_retention") else "ask"
        )

    @property
    def layout_profile(self) -> LayoutProfile | None:
        return _decode_layout_profile(self._get("ui", "layout_profile"))

    def set_layout_save_policy(
        self,
        policy: str,
        profile: LayoutProfile | None = None,
    ) -> bool:
        if policy not in choices_for("ui.layout_retention"):
            return False
        if profile is not None:
            decoded = _decode_layout_profile(profile.to_toml())
            if decoded != profile:
                return False
            if (policy == "always" and profile.scope != "always") or (
                policy == "ask" and profile.scope != "once"
            ) or policy == "never":
                return False
        values: dict[str, Any] = {"layout_retention": policy}
        remove: tuple[str, ...] = ()
        if profile is None:
            remove = ("layout_profile",)
        else:
            values["layout_profile"] = _inline_table(profile.to_toml())
        return self._update_section("ui", values, remove=remove)

    def consume_layout_profile(self, expected: LayoutProfile) -> bool:
        if expected.scope != "once":
            return False
        document = self._read_document()
        if document is None:
            return False
        section = document.get("ui")
        if not isinstance(section, MutableMapping):
            return False
        if _decode_layout_profile(section.get("layout_profile")) != expected:
            return False
        section.pop("layout_profile")
        return self._replace(document)
