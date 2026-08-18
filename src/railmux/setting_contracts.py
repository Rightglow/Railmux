"""Authoritative validation and activation contracts for user settings.

The TOML loader and the mutable Options facade intentionally have different
failure policies, but they must agree on legal values and when a saved value
can affect a running process.
"""

from __future__ import annotations

from dataclasses import dataclass


OPTION_POLICIES = frozenset({"always", "ask", "never"})
MANAGED_HISTORY_POLICIES = frozenset({"ask", "local", "native"})
PATH_OPEN_POLICIES = frozenset({"ask", "internal", "external"})
HISTORY_MIN_LINES = 2000
HISTORY_MAX_LINES = 20000
HISTORY_DEFAULT_LINES = 10000

# Compatibility exports for third-party configuration helpers written against
# the released 0.4.0 names. New code and persisted settings use the
# transport-neutral names below.
SSH_CLAUDE_HISTORY_POLICIES = MANAGED_HISTORY_POLICIES
SSH_HISTORY_MIN_LINES = HISTORY_MIN_LINES
SSH_HISTORY_MAX_LINES = HISTORY_MAX_LINES
SSH_HISTORY_DEFAULT_LINES = HISTORY_DEFAULT_LINES


@dataclass(frozen=True)
class SettingContract:
    """One stable setting surface and its observable activation boundary."""

    key: str
    effects: tuple[str, ...]
    choices: frozenset[str] | None = None
    minimum: int | None = None
    maximum: int | None = None


SETTING_CONTRACTS = {
    "interaction.history_lines": SettingContract(
        "interaction.history_lines",
        ("next_managed_history_client",),
        minimum=HISTORY_MIN_LINES,
        maximum=HISTORY_MAX_LINES,
    ),
    "interaction.claude_history": SettingContract(
        "interaction.claude_history",
        ("next_managed_history_client", "next_history_refresh"),
        choices=MANAGED_HISTORY_POLICIES,
    ),
    "interaction.path_open": SettingContract(
        "interaction.path_open",
        ("next_path_click",),
        choices=PATH_OPEN_POLICIES,
    ),
    # Released 0.3.x/0.4.0 prereleases wrote this key. Keep it as a validated
    # read alias while new writes converge on the transport-neutral setting.
    "ssh.path_open": SettingContract(
        "ssh.path_open",
        ("next_path_click",),
        choices=PATH_OPEN_POLICIES,
    ),
    # Railmux 0.4.0 persisted managed history under [ssh]. Keep both spellings
    # as validated read aliases until a later major-version cleanup.
    "ssh.history_lines": SettingContract(
        "ssh.history_lines",
        ("next_managed_history_client",),
        minimum=HISTORY_MIN_LINES,
        maximum=HISTORY_MAX_LINES,
    ),
    "ssh.claude_history": SettingContract(
        "ssh.claude_history",
        ("next_managed_history_client", "next_history_refresh"),
        choices=MANAGED_HISTORY_POLICIES,
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
