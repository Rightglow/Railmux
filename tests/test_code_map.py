import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
CODE_MAP = ROOT / "docs" / "CODE_MAP.md"


def _resolve_python_reference(reference: str) -> Path:
    if reference.startswith("tests/") or reference.startswith("test_"):
        return ROOT / "tests" / reference.removeprefix("tests/")
    if reference.startswith("src/"):
        return ROOT / reference
    return ROOT / "src" / "railmux" / reference


def test_code_map_python_references_exist():
    text = CODE_MAP.read_text(encoding="utf-8")
    references = set(re.findall(r"`([^`]*\.py)`", text))

    assert references
    missing = sorted(
        reference
        for reference in references
        if not _resolve_python_reference(reference).is_file()
    )
    assert missing == []


def test_code_map_remains_navigation_sized():
    lines = CODE_MAP.read_text(encoding="utf-8").splitlines()

    assert len(lines) <= 200
