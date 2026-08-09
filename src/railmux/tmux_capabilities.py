"""Shared tmux support and visual-fidelity capability policy."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass


TMUX_MINIMUM_SUPPORTED = (2, 7)
TMUX_WINDOWS_VISUAL_FIDELITY_RECOMMENDED = (3, 7)

_VERSION_RE = re.compile(
    r"^\s*(?:tmux\s+)?(?:\d+:)?(?:next-)?(\d+)\.(\d+)"
)


@dataclass(frozen=True)
class TmuxCapability:
    """Bounded, JSON-safe support classification for one tmux version."""

    version: str | None
    minimum_supported: str
    windows_visual_fidelity_recommended: str
    support: str
    windows_visual_fidelity: str

    def payload(
        self,
        *,
        source: str,
        verification: str,
    ) -> dict[str, str | None]:
        return {
            **asdict(self),
            "source": source,
            "verification": verification,
        }


def _display_version(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def parse_tmux_version(value: str | None) -> tuple[int, int] | None:
    """Parse tmux CLI or MSYS2 package versions without assuming suffix style."""
    if value is None:
        return None
    match = _VERSION_RE.match(value)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def classify_tmux_version(
    version: tuple[int, int] | None,
    *,
    display_version: str | None = None,
) -> TmuxCapability:
    """Classify core support separately from Windows visual fidelity."""
    minimum = _display_version(TMUX_MINIMUM_SUPPORTED)
    recommended = _display_version(TMUX_WINDOWS_VISUAL_FIDELITY_RECOMMENDED)
    if version is None or version == (0, 0):
        return TmuxCapability(
            version=None,
            minimum_supported=minimum,
            windows_visual_fidelity_recommended=recommended,
            support="unknown",
            windows_visual_fidelity="unknown",
        )
    rendered = display_version or _display_version(version)
    if version < TMUX_MINIMUM_SUPPORTED:
        return TmuxCapability(
            version=rendered,
            minimum_supported=minimum,
            windows_visual_fidelity_recommended=recommended,
            support="unsupported",
            windows_visual_fidelity="unsupported",
        )
    fidelity = (
        "full"
        if version >= TMUX_WINDOWS_VISUAL_FIDELITY_RECOMMENDED
        else "degraded"
    )
    return TmuxCapability(
        version=rendered,
        minimum_supported=minimum,
        windows_visual_fidelity_recommended=recommended,
        support="supported",
        windows_visual_fidelity=fidelity,
    )


def classify_tmux_text(value: str | None) -> TmuxCapability:
    """Classify a tmux CLI or package version while preserving its raw value."""
    return classify_tmux_version(
        parse_tmux_version(value),
        display_version=value,
    )
