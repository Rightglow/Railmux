from types import SimpleNamespace
import ctypes

from railmux import windows_paths


def test_unpacked_python_keeps_local_app_data_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(windows_paths, "_has_windows_package_identity", lambda: False)
    local = tmp_path / "AppData" / "Local"

    root = windows_paths.managed_windows_data_root(
        {"LOCALAPPDATA": str(local), "USERPROFILE": str(tmp_path)}
    )

    assert root == local / "Railmux"


def test_packaged_python_avoids_virtualized_app_data(tmp_path, monkeypatch):
    monkeypatch.setattr(windows_paths, "_has_windows_package_identity", lambda: True)
    local = tmp_path / "AppData" / "Local"

    root = windows_paths.managed_windows_data_root(
        {"LOCALAPPDATA": str(local), "USERPROFILE": str(tmp_path)}
    )

    assert root == tmp_path / ".railmux" / "windows"
    assert local not in root.parents


def test_packaged_python_never_falls_back_to_virtualized_app_data(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(windows_paths, "_has_windows_package_identity", lambda: True)

    assert (
        windows_paths.managed_windows_data_root({"LOCALAPPDATA": str(tmp_path)}) is None
    )


def test_packaged_executable_path_fallback_is_narrow():
    assert windows_paths._looks_like_packaged_executable(
        r"C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12\python.exe"
    )
    assert windows_paths._looks_like_packaged_executable(
        r"C:\Users\u\AppData\Local\Packages\Python.Package\LocalCache\python.exe"
    )
    assert not windows_paths._looks_like_packaged_executable(
        r"C:\Users\u\AppData\Local\Programs\Python\Python312\python.exe"
    )


def test_windows_package_identity_api_distinguishes_packaged_process(monkeypatch):
    def get_family(length, _buffer):
        ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32)).contents.value = 64
        return 122

    monkeypatch.setattr(windows_paths.os, "name", "nt")
    monkeypatch.setattr(
        windows_paths.ctypes,
        "windll",
        SimpleNamespace(
            kernel32=SimpleNamespace(GetCurrentPackageFamilyName=get_family)
        ),
        raising=False,
    )

    assert windows_paths._has_windows_package_identity()


def test_windows_package_identity_api_preserves_unpacked_python(monkeypatch):
    monkeypatch.setattr(windows_paths.os, "name", "nt")
    monkeypatch.setattr(
        windows_paths.ctypes,
        "windll",
        SimpleNamespace(
            kernel32=SimpleNamespace(
                GetCurrentPackageFamilyName=lambda _length, _buffer: 15700
            )
        ),
        raising=False,
    )

    assert not windows_paths._has_windows_package_identity()
