"""Agent-mode definitions and cycling.

The UI must not encode "Claude vs Codex" as a boolean.  A stable string key
identifies each mode, while this registry owns display labels and the small set
of capabilities the shared UI needs.  Adding a provider still requires its
backend implementation, but it must not require another pair of mode fields or
another two-way toggle.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class ProjectSource(str, Enum):
    """Backend used to discover a mode's projects and sessions."""

    CLAUDE = "claude"
    CODEX = "codex"


@dataclass(frozen=True)
class AgentMode:
    """Provider-neutral metadata needed by the shared application shell."""

    key: str
    label: str
    tmux_prefix: str
    session_type: str
    project_source: ProjectSource
    login_shell: bool = False
    prompt_for_auto_run: bool = False


class ModeRegistry:
    """Ordered mode registry used for lookup, restore, and ``m`` cycling."""

    def __init__(self, modes: Iterable[AgentMode], *, default_key: str) -> None:
        ordered = tuple(modes)
        by_key = {mode.key: mode for mode in ordered}
        if not ordered:
            raise ValueError("at least one agent mode is required")
        if len(by_key) != len(ordered):
            raise ValueError("agent mode keys must be unique")
        if default_key not in by_key:
            raise ValueError(f"unknown default mode: {default_key}")
        prefixes = [mode.tmux_prefix for mode in ordered]
        if any(not prefix for prefix in prefixes) or len(set(prefixes)) != len(prefixes):
            raise ValueError("agent mode tmux prefixes must be non-empty and unique")
        self._ordered = ordered
        self._by_key = by_key
        self._by_prefix = tuple(sorted(
            ordered, key=lambda item: len(item.tmux_prefix), reverse=True))
        self.default_key = default_key

    @property
    def modes(self) -> tuple[AgentMode, ...]:
        return self._ordered

    def get(self, key: str) -> AgentMode:
        return self._by_key[key]

    def resolve(self, key: object, *, fallback: str | None = None) -> AgentMode:
        if isinstance(key, str) and key in self._by_key:
            return self._by_key[key]
        return self._by_key[fallback or self.default_key]

    def next_key(self, key: str) -> str:
        current = self.resolve(key)
        index = self._ordered.index(current)
        return self._ordered[(index + 1) % len(self._ordered)].key

    def for_tmux_name(self, tmux_name: str) -> AgentMode | None:
        # Longest first makes overlapping future prefixes deterministic.
        return next(
            (mode for mode in self._by_prefix
             if tmux_name.startswith(mode.tmux_prefix)),
            None,
        )


CLAUDE_MODE = AgentMode(
    key="claude",
    label="Claude Code",
    tmux_prefix="cc-",
    session_type="claude",
    project_source=ProjectSource.CLAUDE,
)

CODEX_MODE = AgentMode(
    key="codex",
    label="Codex",
    tmux_prefix="cx-",
    session_type="codex",
    project_source=ProjectSource.CODEX,
    login_shell=True,
    prompt_for_auto_run=True,
)

DEFAULT_MODE_REGISTRY = ModeRegistry(
    (CLAUDE_MODE, CODEX_MODE), default_key=CLAUDE_MODE.key)
