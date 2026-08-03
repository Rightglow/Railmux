from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10 CI
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs" / "SUPPORT_MATRIX.md"
LEDGER = ROOT / "docs" / "windows-preview-parity.toml"
VALID_DISPOSITIONS = {
    "shared-contract",
    "native-adapter",
    "native-equivalent",
    "limited",
    "not-applicable",
}
REQUIRED_SCENARIOS = {
    "NW-PREVIEW-OPEN",
    "NW-TOPOLOGY-RESIZE",
    "NW-DOUBLE-CLICK-REDRAW",
    "NW-RESPONSIVE-MODAL",
    "NW-BOTTOM-CHROME",
    "NW-REAL-PROVIDERS",
}


def _ledger():
    with LEDGER.open("rb") as stream:
        return tomllib.load(stream)


def test_every_stable_feature_has_a_windows_preview_disposition():
    matrix_text = MATRIX.read_text(encoding="utf-8")
    functional_inventory = matrix_text.split(
        "## Functional inventory", 1
    )[1].split("## Terminal emulator validation", 1)[0]
    first_cells = re.findall(
        r"^\|\s*([^|]+?)\s*\|", functional_inventory, re.M
    )
    data_cells = [
        cell for cell in first_cells if cell not in {"ID", "---"}
    ]
    assert all(re.fullmatch(r"[A-Z]+[0-9]+", cell) for cell in data_cells)
    inventory = set(data_cells)
    assert len(inventory) == len(data_cells)
    features = _ledger()["features"]
    limitations = _ledger()["limitations"]

    assert set(features) == inventory
    assert set(features.values()) <= VALID_DISPOSITIONS
    expected_limitations = {
        feature_id for feature_id, disposition in features.items()
        if disposition in {"limited", "not-applicable"}
    }
    assert set(limitations) == expected_limitations


def test_windows_interaction_scenarios_keep_live_automation_or_manual_evidence():
    scenarios = _ledger()["scenarios"]
    assert {scenario["id"] for scenario in scenarios} >= REQUIRED_SCENARIOS
    known_features = set(_ledger()["features"])

    for scenario in scenarios:
        assert set(scenario["features"]) <= known_features
        assert scenario["automation"] or scenario["manual"].strip()
        for reference in scenario["automation"]:
            relative, node = reference.split("::", 1)
            source = ROOT / relative
            assert source.is_file(), reference
            assert re.search(
                rf"^def {re.escape(node)}\(",
                source.read_text(encoding="utf-8"),
                re.M,
            ), reference
