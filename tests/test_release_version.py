import subprocess
import sys
from pathlib import Path

import pytest

from railmux import __version__, provider_paths, windows_msys2
from railmux import windows_ui_transition
from railmux.release_version import (
    InvalidVersion,
    is_project_version,
    parse_project_version,
)
from tools.release_notes import normalize_version


ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "version",
    ["0.4.0.dev35", "0.4.0.dev36", "0.4.0rc1", "0.4.0", "1.2.3"],
)
def test_every_persisted_version_consumer_accepts_release_spellings(version):
    app = f"railmux-{version}"

    assert is_project_version(version)
    assert windows_msys2._VERSION_RE.fullmatch(version)
    assert windows_ui_transition._APP_RE.fullmatch(app)
    assert provider_paths._MANAGED_APP_ID.fullmatch(app)
    assert normalize_version(f"v{version}") == version


@pytest.mark.parametrize(
    "version",
    [
        "0.4.0-rc1",
        "0.4.0RC1",
        "0.4.0rc01",
        "0.4.0.dev01",
        "00.4.0",
        "0.04.0",
        "0.4.00",
        "0.4.0a1",
        "0.4.0b1",
        "0.4.0.post1",
        "0.4.0+local",
        "1!0.4.0",
    ],
)
def test_persisted_version_grammar_rejects_aliases_and_unsupported_forms(version):
    with pytest.raises(InvalidVersion):
        parse_project_version(version)
    app = f"railmux-{version}"
    assert windows_msys2._VERSION_RE.fullmatch(version) is None
    assert windows_ui_transition._APP_RE.fullmatch(app) is None
    assert provider_paths._MANAGED_APP_ID.fullmatch(app) is None


def test_release_order_matches_dev_to_rc_to_final():
    versions = ["0.4.0", "0.4.0.dev35", "0.4.0rc1", "0.4.0.dev36"]

    assert sorted(versions, key=windows_msys2._version_key) == [
        "0.4.0.dev35",
        "0.4.0.dev36",
        "0.4.0rc1",
        "0.4.0",
    ]


def test_release_order_crosses_the_next_development_base():
    versions = ["0.4.1.dev1", "0.4.0", "0.4.0rc2", "0.4.0.dev99"]

    assert sorted(versions, key=parse_project_version) == [
        "0.4.0.dev99",
        "0.4.0rc2",
        "0.4.0",
        "0.4.1.dev1",
    ]


def test_package_version_uses_its_canonical_persisted_spelling():
    assert str(parse_project_version(__version__)) == __version__


def test_native_bootstrap_version_command_does_not_import_packaging():
    code = f"""
import importlib.abc
import sys
sys.path.insert(0, {str(ROOT / 'src')!r})
class BlockPackaging(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'packaging' or fullname.startswith('packaging.'):
            raise ModuleNotFoundError(fullname)
        return None
sys.meta_path.insert(0, BlockPackaging())
from railmux.windows_bootstrap import main
raise SystemExit(main(['--version'], version_info=(3, 10)))
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("railmux 0.4.0")


def test_in_use_inventory_retains_an_rc_app_layer(tmp_path):
    output = (
        b"/opt/railmux/apps/railmux-0.4.0rc1/venv/bin/railmux\0"
        b"/opt/railmux/apps/railmux-0.4.0.dev36/venv/bin/railmux\0"
    )

    result = windows_msys2._in_use_app_names(
        tmp_path,
        environ={},
        probe=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=output, stderr=b""
        ),
    )

    assert result == frozenset(
        {"railmux-0.4.0rc1", "railmux-0.4.0.dev36"}
    )
