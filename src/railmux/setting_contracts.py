"""Authoritative validation and activation contracts for user settings.

The TOML loader and the mutable Options facade intentionally have different
failure policies, but they must agree on legal values and when a saved value
can affect a running process.
"""

from __future__ import annotations

from dataclasses import dataclass


OPTION_POLICIES = frozenset({"always", "ask", "never"})
SSH_CLAUDE_HISTORY_POLICIES = frozenset({"ask", "local", "native"})
SSH_HISTORY_MIN_LINES = 2000
SSH_HISTORY_MAX_LINES = 20000
SSH_HISTORY_DEFAULT_LINES = 10000


@dataclass(frozen=True)
class SettingContract:
    """One stable setting surface and its observable activation boundary."""

    key: str
    effects: tuple[str, ...]
    choices: frozenset[str] | None = None
    minimum: int | None = None
    maximum: int | None = None


SETTING_CONTRACTS = {
    "ssh.history_lines": SettingContract(
        "ssh.history_lines",
        ("next_ssh_invocation",),
        minimum=SSH_HISTORY_MIN_LINES,
        maximum=SSH_HISTORY_MAX_LINES,
    ),
    "ssh.claude_history": SettingContract(
        "ssh.claude_history",
        ("next_ssh_invocation", "next_remote_history_refresh"),
        choices=SSH_CLAUDE_HISTORY_POLICIES,
    ),
    "updates.auto_update": SettingContract(
        "updates.auto_update",
        ("next_railmux_invocation",),
        choices=OPTION_POLICIES,
    ),
    "codex.auto_run": SettingContract(
        "codex.auto_run",
        ("next_agent_launch",),
        choices=OPTION_POLICIES,
    ),
    "ui.layout_retention": SettingContract(
        "ui.layout_retention",
        ("next_soft_or_fresh_launch",),
        choices=OPTION_POLICIES,
    ),
}


def choices_for(key: str) -> frozenset[str]:
    """Return the declared legal choices for one enumerated setting."""
    choices = SETTING_CONTRACTS[key].choices
    if choices is None:
        raise ValueError(f"{key} does not declare choices")
    return choices


def bounds_for(key: str) -> tuple[int, int]:
    """Return the inclusive declared bounds for one numeric setting."""
    contract = SETTING_CONTRACTS[key]
    if contract.minimum is None or contract.maximum is None:
        raise ValueError(f"{key} does not declare numeric bounds")
    return contract.minimum, contract.maximum
