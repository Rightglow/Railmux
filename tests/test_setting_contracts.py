import pytest

from railmux.config import ConfigError, load_config
from railmux.setting_contracts import (
    SETTING_CONTRACTS,
    SettingContract,
    SSH_CLAUDE_HISTORY_POLICIES,
    SSH_HISTORY_MAX_LINES,
    SSH_HISTORY_MIN_LINES,
)


def test_ssh_setting_contracts_pin_values_and_activation_boundaries():
    history = SETTING_CONTRACTS["ssh.history_lines"]
    claude = SETTING_CONTRACTS["ssh.claude_history"]
    path_open = SETTING_CONTRACTS["interaction.path_open"]

    assert (history.minimum, history.maximum) == (
        SSH_HISTORY_MIN_LINES,
        SSH_HISTORY_MAX_LINES,
    )
    assert history.effects == ("next_ssh_invocation",)
    assert claude.choices == SSH_CLAUDE_HISTORY_POLICIES
    assert claude.effects == (
        "next_ssh_invocation",
        "next_remote_history_refresh",
    )
    assert path_open.choices == frozenset({"ask", "internal", "external"})
    assert path_open.effects == ("next_path_click",)


def test_config_loader_enforces_the_declared_numeric_contract(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setitem(
        SETTING_CONTRACTS,
        "ssh.history_lines",
        SettingContract(
            "ssh.history_lines",
            ("next_ssh_invocation",),
            minimum=3000,
            maximum=4000,
        ),
    )
    path = tmp_path / "config.toml"
    path.write_text("[ssh]\nhistory_lines = 2500\n")

    with pytest.raises(ConfigError, match="ssh.history_lines"):
        load_config(path)
