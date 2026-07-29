"""Pure, re-entrant compatibility decisions for ``railmux ssh``.

This module does not prompt, spawn SSH, inspect tmux, or install packages.
Callers supply facts and feed consent answers back into :func:`decide`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from packaging.version import InvalidVersion, Version


@dataclass(frozen=True)
class CompatibilityFacts:
    local_version: str
    local_protocol: int
    remote_version: str
    remote_protocol: int
    remote_ready: bool
    remote_tmux: bool


@dataclass(frozen=True)
class CompatibilityDecision:
    action: str
    reason: str | None = None
    prompt: str | None = None
    install_version: str | None = None
    optional: bool = False
    warning: str | None = None


def _version_order(facts: CompatibilityFacts) -> int | None:
    try:
        local = Version(facts.local_version)
        remote = Version(facts.remote_version)
    except InvalidVersion:
        return None
    return (remote > local) - (remote < local)


def decide(
    facts: CompatibilityFacts,
    consents: Mapping[str, bool] | None = None,
) -> CompatibilityDecision:
    """Return the next side-effect-free action for the supplied facts.

    Consent keys are ``local_upgrade`` and ``remote_install``. Missing keys
    produce a prompt action, making the function safe to call again after each
    answer without hiding compatibility logic inside the UI layer.
    """
    answers = {} if consents is None else consents
    order = _version_order(facts)
    remote_newer = order == 1
    remote_older = order == -1

    if remote_newer and "local_upgrade" not in answers:
        protocol_note = (
            f" and requires SSH protocol v{facts.remote_protocol}"
            if facts.remote_protocol != facts.local_protocol
            else ""
        )
        return CompatibilityDecision(
            "prompt",
            prompt="local_upgrade",
            reason=(
                f"Remote Railmux {facts.remote_version} is newer than local "
                f"{facts.local_version}{protocol_note}."
            ),
        )
    if remote_newer and answers.get("local_upgrade"):
        return CompatibilityDecision(
            "upgrade_local",
            install_version=facts.remote_version,
        )

    if facts.remote_protocol > facts.local_protocol:
        if remote_newer:
            reason = (
                "the newer remote Railmux uses an incompatible SSH protocol; "
                "upgrade local Railmux and retry"
            )
        else:
            reason = (
                f"remote Railmux {facts.remote_version} reports newer SSH "
                f"protocol v{facts.remote_protocol}, but its package version "
                f"is not newer than local {facts.local_version}; refusing an "
                "unsafe automatic local downgrade. Install matching Railmux "
                "builds manually."
            )
        return CompatibilityDecision("error", reason=reason)
    install_reason: str | None = None
    optional = False
    if facts.remote_protocol < facts.local_protocol:
        if remote_newer:
            return CompatibilityDecision(
                "error",
                reason=(
                    "the newer remote Railmux uses an incompatible SSH "
                    "protocol; upgrade local Railmux and retry"
                ),
            )
        install_reason = (
            f"Remote Railmux {facts.remote_version} uses older SSH protocol "
            f"v{facts.remote_protocol}; local {facts.local_version} requires "
            f"v{facts.local_protocol}."
        )
    if install_reason is None and not facts.remote_tmux:
        return CompatibilityDecision("tmux_missing")

    install_version = facts.remote_version if remote_newer else facts.local_version
    if install_reason is None and not facts.remote_ready:
        install_reason = (
            f"Remote Railmux {facts.remote_version} is missing its SSH "
            "display dependency."
        )
    elif install_reason is None and remote_older:
        install_reason = (
            f"Remote Railmux {facts.remote_version} is older than local "
            f"{facts.local_version}, although SSH protocol "
            f"v{facts.local_protocol} is compatible."
        )
        optional = True

    if install_reason is not None:
        if "remote_install" not in answers:
            return CompatibilityDecision(
                "prompt",
                reason=install_reason,
                prompt="remote_install",
                install_version=install_version,
                optional=optional,
            )
        if answers["remote_install"]:
            return CompatibilityDecision(
                "install_remote",
                reason=install_reason,
                install_version=install_version,
                optional=optional,
            )
        if optional:
            return CompatibilityDecision(
                "attach",
                warning=(
                    f"continuing with compatible remote Railmux {facts.remote_version}"
                ),
            )
        return CompatibilityDecision(
            "error",
            reason=install_reason,
            install_version=install_version,
        )

    warning = None
    if remote_newer:
        warning = (
            f"continuing with local Railmux {facts.local_version} and "
            f"compatible remote {facts.remote_version}"
        )
    elif order is None and facts.remote_version != facts.local_version:
        warning = (
            f"remote Railmux {facts.remote_version} differs from local "
            f"{facts.local_version}, but SSH protocol "
            f"v{facts.local_protocol} is compatible"
        )
    return CompatibilityDecision("attach", warning=warning)
