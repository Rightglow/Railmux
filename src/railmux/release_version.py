"""Narrow version grammar shared by managed Windows app-layer identities."""
from __future__ import annotations

import re
from functools import total_ordering
from typing import Any


class InvalidVersion(ValueError):
    """A version is outside Railmux's canonical persisted grammar."""


@total_ordering
class ProjectVersion:
    """Small standard-library ordering type for native bootstrap identities."""

    __slots__ = ("_key", "_value")

    def __init__(
        self,
        value: str,
        release: tuple[int, ...],
        stage: int,
        stage_number: int,
    ) -> None:
        normalized = release
        while len(normalized) > 1 and normalized[-1] == 0:
            normalized = normalized[:-1]
        self._value = value
        self._key = (normalized, stage, stage_number)

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"ProjectVersion({self._value!r})"

    def __hash__(self) -> int:
        return hash(self._key)

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, ProjectVersion):
            return NotImplemented
        return self._key == other._key

    def __lt__(self, other: Any) -> bool:
        if not isinstance(other, ProjectVersion):
            return NotImplemented
        return self._key < other._key


# Railmux app directories are security-sensitive persisted identities.  Accept
# only the spellings that this project publishes: a final, an RC, or a
# development release.  PEP 440 aliases, epochs, post releases, and local
# versions are deliberately excluded so equal versions have one path spelling.
_CANONICAL_INTEGER_PATTERN = r"(?:0|[1-9][0-9]*)"
PROJECT_VERSION_PATTERN = (
    rf"{_CANONICAL_INTEGER_PATTERN}"
    rf"(?:\.{_CANONICAL_INTEGER_PATTERN})*"
    rf"(?:rc{_CANONICAL_INTEGER_PATTERN}|\.dev{_CANONICAL_INTEGER_PATTERN})?"
)
PROJECT_VERSION_RE = re.compile(PROJECT_VERSION_PATTERN + r"\Z")
_PROJECT_VERSION_PARTS_RE = re.compile(
    rf"(?P<release>{_CANONICAL_INTEGER_PATTERN}"
    rf"(?:\.{_CANONICAL_INTEGER_PATTERN})*)"
    rf"(?:(?:rc(?P<rc>{_CANONICAL_INTEGER_PATTERN}))|"
    rf"(?:\.dev(?P<dev>{_CANONICAL_INTEGER_PATTERN})))?\Z"
)


def parse_project_version(value: str) -> ProjectVersion:
    """Return the canonical supported Railmux version or raise InvalidVersion."""
    match = _PROJECT_VERSION_PARTS_RE.fullmatch(value)
    if match is None:
        raise InvalidVersion(value)
    release_parts = match.group("release").split(".")
    stage_text = match.group("rc") or match.group("dev")
    numeric_parts = release_parts + ([stage_text] if stage_text else [])
    if any(
        len(part) > 1 and part.startswith("0")
        for part in numeric_parts
    ):
        raise InvalidVersion(
            f"non-canonical Railmux version spelling: {value!r}"
        )
    if match.group("dev") is not None:
        stage = 0
        stage_number = int(match.group("dev"))
    elif match.group("rc") is not None:
        stage = 1
        stage_number = int(match.group("rc"))
    else:
        stage = 2
        stage_number = 0
    return ProjectVersion(
        value,
        tuple(int(part) for part in release_parts),
        stage,
        stage_number,
    )


def is_project_version(value: object) -> bool:
    """Return whether *value* is one canonical published-version spelling."""
    if not isinstance(value, str):
        return False
    try:
        parse_project_version(value)
    except InvalidVersion:
        return False
    return True
