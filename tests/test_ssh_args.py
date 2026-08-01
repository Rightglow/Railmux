from __future__ import annotations

import argparse

import pytest

from railmux.ssh_args import (
    AppendSshArgument,
    ExtendSshArguments,
    split_ssh_argument_group,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ssh-arg",
        action=AppendSshArgument,
        dest="ssh_arg",
        default=[],
    )
    parser.add_argument(
        "--ssh-args",
        action=ExtendSshArguments,
        dest="ssh_arg",
    )
    return parser


def test_group_supports_shell_quoting_without_shell_execution():
    assert split_ssh_argument_group(
        '-J jump -p 2222 -o "ProxyCommand=ssh -W %h:%p gateway"'
    ) == [
        "-J",
        "jump",
        "-p",
        "2222",
        "-o",
        "ProxyCommand=ssh -W %h:%p gateway",
    ]


def test_exact_and_grouped_arguments_preserve_command_line_order():
    args = _parser().parse_args([
        "--ssh-arg=-F",
        "--ssh-args=config -J jump",
        "--ssh-arg=ProxyCommand=ssh -W %h:%p gateway",
    ])

    assert args.ssh_arg == [
        "-F",
        "config",
        "-J",
        "jump",
        "ProxyCommand=ssh -W %h:%p gateway",
    ]


def test_group_rejects_unclosed_quotes():
    with pytest.raises(SystemExit):
        _parser().parse_args(["--ssh-args='unterminated"])


def test_group_rejects_newlines():
    with pytest.raises(SystemExit):
        _parser().parse_args(["--ssh-args=-J jump\n-o bad"])
