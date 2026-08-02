"""Private runtime-directory policy for POSIX and native Windows."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def runtime_base() -> Path:
    """Return the platform runtime root used for non-roaming live state."""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        return root / "Railmux" / "runtime"
    run_dir = os.environ.get("XDG_RUNTIME_DIR")
    if run_dir:
        return Path(run_dir)
    return Path(f"/tmp/railmux-{os.getuid()}")


def ensure_private_dir(path: Path) -> None:
    """Create and verify one private runtime directory.

    Windows runtime state lives below the current user's LocalAppData profile.
    The Windows daemon also requires a random 256-bit loopback token; this
    path is not the sole authentication authority.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("Railmux runtime state path is not a directory")
    if os.name == "nt":
        _harden_windows_directory(path)
    elif (
        info.st_uid != os.getuid() or info.st_mode & 0o077
    ):
        raise OSError("Railmux runtime state directory is not private")


def _harden_windows_directory(path: Path) -> None:
    """Protect runtime secrets with inheritable owner/System-only DACLs."""
    import ctypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        ctypes.c_int
    )
    advapi32.SetFileSecurityW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    advapi32.SetFileSecurityW.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    # Protected DACL: the object owner and LocalSystem receive inheritable
    # generic-all access; inherited profile or machine ACL entries are removed.
    sddl = "D:P(A;OICI;GA;;;OW)(A;OICI;GA;;;SY)"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise OSError(ctypes.get_last_error(), "could not build runtime DACL")
    try:
        if not advapi32.SetFileSecurityW(str(path), 0x00000004, descriptor):
            raise OSError(ctypes.get_last_error(), "could not protect runtime DACL")
    finally:
        kernel32.LocalFree(descriptor)
