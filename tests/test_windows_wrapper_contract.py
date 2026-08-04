from pathlib import Path
import re

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9
    import tomli as tomllib


ROOT = Path(__file__).parents[1]


def _ledger():
    return tomllib.loads(
        (ROOT / "docs" / "windows-wrapper-parity.toml").read_text(
            encoding="utf-8"
        )
    )


def test_windows_wrapper_ledger_tracks_every_stable_feature():
    matrix = (ROOT / "docs" / "SUPPORT_MATRIX.md").read_text(encoding="utf-8")
    stable_ids = set(re.findall(r"^\| ([PWS]\d{2}) \|", matrix, re.MULTILINE))
    ledger = _ledger()

    assert stable_ids
    assert set(ledger["features"]) == stable_ids
    assert set(ledger["features"].values()) <= {
        "delegated-posix",
        "runtime-bridge",
        "not-applicable",
    }


def test_preview_package_is_one_posix_ui_without_archived_backends():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    module_entrypoint = (ROOT / "src" / "railmux" / "__main__.py").read_text(
        encoding="utf-8"
    )

    assert 'railmux = "railmux.entrypoint:main"' in project
    assert "from railmux.entrypoint import main" in module_entrypoint
    assert "pywinpty" not in project
    assert not (ROOT / "src" / "railmux" / "winlocal").exists()
    assert "wsl" not in (ROOT / "src" / "railmux" / "windows_bootstrap.py").read_text(
        encoding="utf-8"
    ).lower()


def test_ledger_retains_high_risk_real_terminal_scenarios():
    ledger = _ledger()
    scenario_ids = {scenario["id"] for scenario in ledger["scenarios"]}
    assert {
        "WW-PREVIEW-OPEN",
        "WW-RESIZE-REFLOW",
        "WW-MOUSE-CHROME",
        "WW-LIFECYCLE",
        "WW-ENTRY-SURFACES",
        "WW-SSH-POSIX",
    } <= scenario_ids
    feature_ids = set(ledger["features"])
    scenario_features = {
        feature
        for scenario in ledger["scenarios"]
        for feature in scenario["features"]
    }
    assert scenario_features <= feature_ids
    assert all(scenario["evidence"] for scenario in ledger["scenarios"])
    runtime_bridges = {
        feature
        for feature, disposition in ledger["features"].items()
        if disposition == "runtime-bridge"
    }
    assert runtime_bridges <= scenario_features
