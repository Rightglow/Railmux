"""Ordered, argv-only argparse actions for Railmux SSH launchers."""
from __future__ import annotations

import argparse
import shlex


_MAX_SSH_ARGUMENTS = 128
_MAX_SSH_ARGUMENT_BYTES = 32 * 1024


def _validated(values: list[str]) -> list[str]:
    if not values or any(not value for value in values):
        raise ValueError("SSH arguments cannot be empty")
    if any("\0" in value or "\n" in value or "\r" in value for value in values):
        raise ValueError("SSH arguments cannot contain NUL or newlines")
    return values


def split_ssh_argument_group(value: str) -> list[str]:
    """Split one explicitly grouped value without executing a shell."""
    try:
        if "\0" in value or "\n" in value or "\r" in value:
            raise ValueError("SSH arguments cannot contain NUL or newlines")
        return _validated(shlex.split(value, posix=True))
    except ValueError as exc:
        raise ValueError(f"invalid grouped SSH arguments: {exc}") from exc


def _extend(namespace: argparse.Namespace, dest: str, values: list[str]) -> None:
    current = list(getattr(namespace, dest, None) or ())
    combined = [*current, *_validated(values)]
    if len(combined) > _MAX_SSH_ARGUMENTS:
        raise ValueError(f"at most {_MAX_SSH_ARGUMENTS} SSH arguments are allowed")
    encoded_size = sum(len(value.encode("utf-8")) + 1 for value in combined)
    if encoded_size > _MAX_SSH_ARGUMENT_BYTES:
        raise ValueError("SSH arguments are too large")
    setattr(namespace, dest, combined)


class AppendSshArgument(argparse.Action):
    """Append one exact SSH argv item while preserving option order."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        try:
            _extend(namespace, self.dest, [values])
        except (TypeError, UnicodeError, ValueError) as exc:
            raise argparse.ArgumentError(self, str(exc)) from exc


class ExtendSshArguments(argparse.Action):
    """Append one shell-like group as argv only, preserving option order."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        try:
            _extend(namespace, self.dest, split_ssh_argument_group(values))
        except (TypeError, UnicodeError, ValueError) as exc:
            raise argparse.ArgumentError(self, str(exc)) from exc
