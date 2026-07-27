from pathlib import Path

import pytest

from tools.wheel_smoke import _installed_paths


@pytest.mark.parametrize(
    ("package_root", "script_dir"),
    (
        ("lib/python3.12/site-packages", "bin"),
        ("local/lib/python3.12/dist-packages", "local/bin"),
        ("Lib/site-packages", "Scripts"),
    ),
)
def test_installed_paths_supports_pip_prefix_schemes(
    tmp_path: Path,
    package_root: str,
    script_dir: str,
):
    prefix = tmp_path / "install"
    site_packages = prefix / package_root
    (site_packages / "railmux").mkdir(parents=True)
    executable = "railmux.exe" if script_dir == "Scripts" else "railmux"
    script = prefix / script_dir / executable
    script.parent.mkdir(parents=True)
    script.touch()

    assert _installed_paths(prefix) == (site_packages, script)
