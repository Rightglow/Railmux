import pytest

from railmux.config import ConfigError, load_config
from railmux.setting_contracts import (
    HISTORY_MAX_LINES,
    HISTORY_MIN_LINES,
    MANAGED_HISTORY_POLICIES,
    SETTING_CONTRACTS,
    SettingContract,
)


def test_managed_history_contracts_pin_values_and_activation_boundaries():
    history = SETTING_CONTRACTS["interaction.history_lines"]
    claude = SETTING_CONTRACTS["interaction.claude_history"]
    path_open = SETTING_CONTRACTS["interaction.path_open"]

    assert (history.minimum, history.maximum) == (
        HISTORY_MIN_LINES,
        HISTORY_MAX_LINES,
    )
    assert history.effects == ("next_managed_history_client",)
    assert claude.choices == MANAGED_HISTORY_POLICIES
    assert claude.effects == (
        "next_managed_history_client",
        "next_history_refresh",
    )
    assert path_open.choices == frozenset({"ask", "internal", "external"})
    assert path_open.effects == ("next_path_click",)


def test_config_loader_enforces_the_declared_numeric_contract(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setitem(
        SETTING_CONTRACTS,
        "interaction.history_lines",
        SettingContract(
            "interaction.history_lines",
            ("next_managed_history_client",),
            minimum=3000,
            maximum=4000,
        ),
    )
    path = tmp_path / "config.toml"
    path.write_text("[interaction]\nhistory_lines = 2500\n")

    with pytest.raises(ConfigError, match="interaction.history_lines"):
        load_config(path)
