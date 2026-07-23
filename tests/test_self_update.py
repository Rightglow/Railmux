"""Consent, safety, and failure behavior for launcher self-updates."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from railmux import self_update


class _Response:
    def __init__(self, payload: object) -> None:
        self._raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self._raw


def test_latest_release_returns_only_newer_stable_version(monkeypatch):
    monkeypatch.setattr(self_update, "__version__", "1.2.3")

    def respond(version):
        monkeypatch.setattr(
            self_update.urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: _Response(
                {"info": {"version": version}}
            ),
        )
        return self_update.latest_release()

    assert respond("1.2.4") == "1.2.4"
    assert respond("1.2.3") is None
    assert respond("1.2.4rc1") is None
    assert respond("not-a-version") is None


def test_latest_release_failure_is_silent(monkeypatch):
    monkeypatch.setattr(
        self_update.urllib.request,
        "urlopen",
        MagicMock(side_effect=OSError("offline")),
    )

    assert self_update.latest_release() is None


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("always", "always"),
        ("T", "this_time"),
        ("no", "no"),
        ("", "no"),
        ("never", "never"),
        ("v", "never"),
    ],
)
def test_prompt_choices(monkeypatch, answer, expected):
    monkeypatch.setattr("builtins.input", lambda _question: answer)

    assert self_update._prompt("1.0", "2.0") == expected


def test_never_policy_skips_network(monkeypatch):
    settings = MagicMock(update_policy="never")
    check = MagicMock()
    monkeypatch.setattr(self_update, "latest_release", check)

    self_update.maybe_upgrade_before_launch((), settings)

    check.assert_not_called()


def test_ask_no_skips_only_this_launch(monkeypatch):
    settings = MagicMock(update_policy="ask")
    install = MagicMock()
    monkeypatch.setattr(self_update, "latest_release", lambda: "9.0")
    monkeypatch.setattr(self_update, "installation_is_editable", lambda: False)
    monkeypatch.setattr(self_update.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(self_update, "_prompt", lambda *_args: "no")
    monkeypatch.setattr(self_update.subprocess, "run", install)

    self_update.maybe_upgrade_before_launch(("--project", "/work"), settings)

    settings.set_update_policy.assert_not_called()
    install.assert_not_called()


def test_ask_never_persists_and_does_not_install(monkeypatch):
    settings = MagicMock(update_policy="ask")
    settings.set_update_policy.return_value = True
    install = MagicMock()
    monkeypatch.setattr(self_update, "latest_release", lambda: "9.0")
    monkeypatch.setattr(self_update, "installation_is_editable", lambda: False)
    monkeypatch.setattr(self_update.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(self_update, "_prompt", lambda *_args: "never")
    monkeypatch.setattr(self_update.subprocess, "run", install)

    self_update.maybe_upgrade_before_launch((), settings)

    settings.set_update_policy.assert_called_once_with("never")
    install.assert_not_called()


def test_ask_this_time_updates_without_persisting(monkeypatch):
    settings = MagicMock(update_policy="ask")
    monkeypatch.setattr(self_update, "latest_release", lambda: "9.0")
    monkeypatch.setattr(self_update, "installation_is_editable", lambda: False)
    monkeypatch.setattr(self_update.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(self_update, "_prompt", lambda *_args: "this_time")
    monkeypatch.setattr(
        self_update.subprocess, "run", lambda *_args, **_kwargs: MagicMock(
            returncode=0
        )
    )

    class Restarted(Exception):
        pass

    def restart(raw_args):
        assert raw_args == ("--project", "/work")
        raise Restarted

    monkeypatch.setattr(self_update, "_restart", restart)

    with pytest.raises(Restarted):
        self_update.maybe_upgrade_before_launch(
            ("--project", "/work"), settings
        )
    settings.set_update_policy.assert_not_called()


def test_ask_always_persists_before_update(monkeypatch):
    calls = []
    settings = MagicMock(update_policy="ask")
    settings.set_update_policy.side_effect = (
        lambda policy: calls.append(("save", policy)) or True
    )
    monkeypatch.setattr(self_update, "latest_release", lambda: "9.0")
    monkeypatch.setattr(self_update, "installation_is_editable", lambda: False)
    monkeypatch.setattr(self_update.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(self_update, "_prompt", lambda *_args: "always")
    monkeypatch.setattr(
        self_update.subprocess,
        "run",
        lambda *_args, **_kwargs: calls.append(("install", "9.0"))
        or MagicMock(returncode=1),
    )

    self_update.maybe_upgrade_before_launch((), settings)

    assert calls == [("save", "always"), ("install", "9.0")]


def test_always_updates_without_prompt(monkeypatch):
    settings = MagicMock(update_policy="always")
    prompt = MagicMock()
    monkeypatch.setattr(self_update, "latest_release", lambda: "9.0")
    monkeypatch.setattr(self_update, "installation_is_editable", lambda: False)
    monkeypatch.setattr(self_update, "_prompt", prompt)
    monkeypatch.setattr(
        self_update.subprocess, "run", lambda *_args, **_kwargs: MagicMock(
            returncode=1
        )
    )

    self_update.maybe_upgrade_before_launch((), settings)

    prompt.assert_not_called()


def test_ask_without_tty_does_not_install(monkeypatch):
    settings = MagicMock(update_policy="ask")
    install = MagicMock()
    monkeypatch.setattr(self_update, "latest_release", lambda: "9.0")
    monkeypatch.setattr(self_update, "installation_is_editable", lambda: False)
    monkeypatch.setattr(self_update.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(self_update.subprocess, "run", install)

    self_update.maybe_upgrade_before_launch((), settings)

    install.assert_not_called()


def test_editable_source_is_not_prompted_or_replaced(monkeypatch, capsys):
    settings = MagicMock(update_policy="ask")
    prompt = MagicMock()
    install = MagicMock()
    monkeypatch.setattr(self_update, "latest_release", lambda: "9.0")
    monkeypatch.setattr(self_update, "installation_is_editable", lambda: True)
    monkeypatch.setattr(self_update, "_prompt", prompt)
    monkeypatch.setattr(self_update.subprocess, "run", install)

    self_update.maybe_upgrade_before_launch((), settings)

    prompt.assert_not_called()
    install.assert_not_called()
    settings.set_update_policy.assert_not_called()
    assert "editable source installation" in capsys.readouterr().err


def test_failed_install_prints_manual_command_and_continues(monkeypatch, capsys):
    settings = MagicMock(update_policy="always")
    restart = MagicMock()
    monkeypatch.setattr(self_update, "latest_release", lambda: "9.0")
    monkeypatch.setattr(self_update, "installation_is_editable", lambda: False)
    monkeypatch.setattr(
        self_update.subprocess, "run", lambda *_args, **_kwargs: MagicMock(
            returncode=1
        )
    )
    monkeypatch.setattr(self_update, "_restart", restart)

    self_update.maybe_upgrade_before_launch((), settings)

    restart.assert_not_called()
    stderr = capsys.readouterr().err
    assert "continuing with the installed version" in stderr
    assert "railmux==9.0" in stderr


def test_upgrade_argv_uses_user_site_only_outside_venv(monkeypatch):
    monkeypatch.setattr(self_update.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(self_update.sys, "prefix", "/usr")
    monkeypatch.setattr(self_update.sys, "base_prefix", "/usr")
    assert self_update.upgrade_argv("2.0") == [
        "/usr/bin/python3", "-m", "pip", "install", "--user", "--upgrade",
        "railmux==2.0",
    ]

    monkeypatch.setattr(self_update.sys, "prefix", "/work/.venv")
    assert self_update.upgrade_argv("2.0") == [
        "/usr/bin/python3", "-m", "pip", "install", "--upgrade",
        "railmux==2.0",
    ]
