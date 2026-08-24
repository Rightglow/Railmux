"""Bounded clipboard-image capture and private provider-visible storage."""

from __future__ import annotations

import hashlib
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from railmux.provider_paths import running_in_managed_windows_wrapper


MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_DIMENSION = 32768
MAX_IMAGE_PIXELS = 100_000_000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GENERATED_IMAGE_RE = re.compile(
    r"^railmux-image-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\.png$"
)
_IMAGE_RETENTION_SECONDS = 24 * 60 * 60
_MAX_RETAINED_IMAGES = 32
_MAX_RETAINED_BYTES = 256 * 1024 * 1024
_CLIPBOARD_TIMEOUT = 4.0
IMAGE_PASTE_KEYS = (b"\x16", b"\x1bv", b"\x1bV")


class ImagePasteError(RuntimeError):
    """An explicit image paste could not be completed safely."""


@dataclass(frozen=True)
class ClipboardImage:
    """One normalized PNG read from the user's local clipboard."""

    data: bytes
    width: int
    height: int

    @property
    def digest(self) -> bytes:
        return hashlib.sha256(self.data).digest()


def split_shortcuts(data: bytes) -> tuple[bytes, ...]:
    """Split explicit image shortcuts without interpreting pasted text."""
    parts: list[bytes] = []
    start = 0
    while start < len(data):
        matches = [
            (index, key)
            for key in IMAGE_PASTE_KEYS
            if (index := data.find(key, start)) >= 0
        ]
        if not matches:
            parts.append(data[start:])
            break
        index, key = min(matches, key=lambda item: (item[0], -len(item[1])))
        if index > start:
            parts.append(data[start:index])
        parts.append(key)
        start = index + len(key)
    return tuple(part for part in parts if part)


def validate_png(data: bytes) -> tuple[int, int]:
    """Validate the bounded PNG envelope needed by provider image loaders."""
    if not 33 <= len(data) <= MAX_IMAGE_BYTES:
        raise ImagePasteError("clipboard image is empty or exceeds 16 MiB")
    if data[:8] != _PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise ImagePasteError("clipboard image is not a valid PNG")
    ihdr_size = struct.unpack(">I", data[8:12])[0]
    if ihdr_size != 13:
        raise ImagePasteError("clipboard image has an invalid PNG header")
    width, height = struct.unpack(">II", data[16:24])
    if (
        width == 0
        or height == 0
        or width > MAX_IMAGE_DIMENSION
        or height > MAX_IMAGE_DIMENSION
        or width * height > MAX_IMAGE_PIXELS
    ):
        raise ImagePasteError("clipboard image dimensions are not supported")
    bit_depth, color_type, compression, filtering, interlace = data[24:29]
    valid_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if (
        color_type not in valid_depths
        or bit_depth not in valid_depths[color_type]
        or compression != 0
        or filtering != 0
        or interlace not in (0, 1)
    ):
        raise ImagePasteError("clipboard image has unsupported PNG parameters")
    offset = 8
    saw_idat = False
    saw_iend = False
    chunks = 0
    while offset < len(data):
        if len(data) - offset < 12:
            raise ImagePasteError("clipboard image has a truncated PNG chunk")
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + size
        if chunk_end > len(data):
            raise ImagePasteError("clipboard image has a truncated PNG chunk")
        expected_crc = struct.unpack(">I", data[chunk_end - 4 : chunk_end])[0]
        actual_crc = zlib.crc32(data[offset + 4 : chunk_end - 4]) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise ImagePasteError("clipboard image failed PNG integrity validation")
        chunks += 1
        if chunks > 4096:
            raise ImagePasteError("clipboard image has too many PNG chunks")
        if chunks == 1 and (chunk_type != b"IHDR" or size != 13):
            raise ImagePasteError("clipboard image has an invalid PNG header")
        if chunk_type == b"IDAT":
            saw_idat = True
        if chunk_type == b"IEND":
            if size != 0 or chunk_end != len(data):
                raise ImagePasteError("clipboard image has an invalid PNG ending")
            saw_iend = True
        offset = chunk_end
    if not saw_idat or not saw_iend:
        raise ImagePasteError("clipboard image is incomplete")
    return width, height


def _powershell_command() -> tuple[str, ...] | None:
    candidates = ("powershell.exe", "pwsh.exe")
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable:
            return (executable,)
    if os.environ.get("WSL_INTEROP"):
        legacy = Path(
            "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        if legacy.is_file():
            return (str(legacy),)
    return None


_POWERSHELL_IMAGE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
if (-not [System.Windows.Forms.Clipboard]::ContainsImage()) { exit 3 }
$image = [System.Windows.Forms.Clipboard]::GetImage()
if ($null -eq $image) { exit 3 }
$stream = New-Object System.IO.MemoryStream
try {
  $image.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
  $bytes = $stream.ToArray()
  if ($bytes.Length -gt 16777216) { exit 4 }
  $stdout = [Console]::OpenStandardOutput()
  $stdout.Write($bytes, 0, $bytes.Length)
  $stdout.Flush()
} finally {
  $stream.Dispose()
  $image.Dispose()
}
""".strip()


_MACOS_IMAGE_SCRIPT = r"""
on run argv
    set outputFile to POSIX file (item 1 of argv)
    set imageData to the clipboard as «class PNGf»
    set outputHandle to open for access outputFile with write permission
    try
        set eof outputHandle to 0
        write imageData to outputHandle
    on error errorMessage number errorNumber
        close access outputHandle
        error errorMessage number errorNumber
    end try
    close access outputHandle
end run
""".strip()


def _read_windows_clipboard() -> bytes | None:
    command = _powershell_command()
    if command is None:
        return None
    try:
        completed = subprocess.run(
            (*command, "-NoLogo", "-NoProfile", "-NonInteractive", "-Sta", "-Command", _POWERSHELL_IMAGE_SCRIPT),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_CLIPBOARD_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImagePasteError("could not read the Windows image clipboard") from exc
    if completed.returncode == 3:
        return None
    if completed.returncode == 4:
        raise ImagePasteError("clipboard image exceeds 16 MiB")
    if completed.returncode != 0:
        raise ImagePasteError("could not read the Windows image clipboard")
    return completed.stdout


def _read_macos_clipboard() -> bytes | None:
    pngpaste = shutil.which("pngpaste")
    if pngpaste:
        return _bounded_command_output((pngpaste, "-"), "macOS")
    osascript = shutil.which("osascript")
    if osascript is None:
        return None
    fd, raw_path = tempfile.mkstemp(prefix="railmux-clipboard-", suffix=".png")
    os.close(fd)
    path = Path(raw_path)
    try:
        completed = subprocess.run(
            (osascript, "-e", _MACOS_IMAGE_SCRIPT, str(path)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_CLIPBOARD_TIMEOUT,
            check=False,
        )
        if completed.returncode != 0:
            return None
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ImagePasteError("clipboard image exceeds 16 MiB")
        return path.read_bytes()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImagePasteError("could not read the macOS image clipboard") from exc
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _read_linux_clipboard() -> bytes | None:
    commands: list[tuple[str, ...]] = []
    last_error: ImagePasteError | None = None
    wl_paste = shutil.which("wl-paste")
    if wl_paste:
        commands.append((wl_paste, "--no-newline", "--type", "image/png"))
    xclip = shutil.which("xclip")
    if xclip:
        commands.append((xclip, "-selection", "clipboard", "-t", "image/png", "-o"))
    for command in commands:
        try:
            data = _bounded_command_output(command, "Linux")
        except ImagePasteError as exc:
            last_error = exc
            continue
        if data:
            return data
    if last_error is not None:
        raise last_error
    return None


def _bounded_command_output(command: tuple[str, ...], platform_name: str) -> bytes | None:
    """Read a POSIX clipboard helper without unbounded pipe accumulation."""
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ImagePasteError(
            f"could not read the {platform_name} image clipboard"
        ) from exc
    assert process.stdout is not None
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + _CLIPBOARD_TIMEOUT
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ImagePasteError(
                    f"could not read the {platform_name} image clipboard"
                )
            readable, _, _ = select.select([process.stdout], [], [], remaining)
            if not readable:
                raise ImagePasteError(
                    f"could not read the {platform_name} image clipboard"
                )
            chunk = os.read(process.stdout.fileno(), 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise ImagePasteError("clipboard image exceeds 16 MiB")
            chunks.append(chunk)
        return b"".join(chunks) if process.wait(timeout=0.5) == 0 else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImagePasteError(
            f"could not read the {platform_name} image clipboard"
        ) from exc
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass


def read_clipboard_png() -> ClipboardImage | None:
    """Read a local clipboard image without interpreting clipboard text."""
    if running_in_managed_windows_wrapper() or os.environ.get("WSL_INTEROP"):
        data = _read_windows_clipboard()
    elif sys.platform == "darwin":
        data = _read_macos_clipboard()
    else:
        data = _read_linux_clipboard()
    if data is None:
        return None
    width, height = validate_png(data)
    return ClipboardImage(data, width, height)


def _image_root() -> Path:
    if running_in_managed_windows_wrapper():
        return Path.home() / ".railmux" / "windows" / "images"
    return Path.home() / ".cache" / "railmux" / "images"


def _prepare_root() -> Path:
    root = _image_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = root.lstat()
    except OSError as exc:
        raise ImagePasteError("could not inspect the private image directory") from exc
    if root.is_symlink() or not root.is_dir():
        raise ImagePasteError("private image directory is not a safe directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ImagePasteError("private image directory is owned by another user")
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _prune_generated_images(root: Path, *, keep: Path | None = None) -> None:
    now = time.time()
    candidates: list[tuple[float, int, Path]] = []
    try:
        entries = tuple(root.iterdir())
    except OSError:
        return
    for path in entries:
        if not _GENERATED_IMAGE_RE.fullmatch(path.name) or path == keep:
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if now - info.st_mtime > _IMAGE_RETENTION_SECONDS:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        candidates.append((info.st_mtime, info.st_size, path))
    candidates.sort(reverse=True)
    retained_bytes = 0
    for index, (_mtime, size, path) in enumerate(candidates):
        retained_bytes += size
        if index < _MAX_RETAINED_IMAGES and retained_bytes <= _MAX_RETAINED_BYTES:
            continue
        try:
            path.unlink()
        except OSError:
            pass


def store_png(data: bytes) -> Path:
    """Atomically store one validated PNG under a generated private name."""
    validate_png(data)
    root = _prepare_root()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"railmux-image-{stamp}-{os.urandom(8).hex()}.png"
    final = root / name
    temporary = root / f".{name}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, final)
        try:
            final.chmod(0o600)
        except OSError:
            pass
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ImagePasteError("could not store the private clipboard image") from exc
    _prune_generated_images(root, keep=final)
    return final


def provider_path(path: Path) -> str:
    """Return the path syntax understood by the native provider process."""
    if not running_in_managed_windows_wrapper():
        return str(path)
    cygpath = shutil.which("cygpath")
    if cygpath is None:
        raise ImagePasteError("could not translate the image path for Windows")
    try:
        translated = subprocess.check_output(
            (cygpath, "-w", str(path)),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ImagePasteError("could not translate the image path for Windows") from exc
    if not translated or "\x00" in translated or "\r" in translated or "\n" in translated:
        raise ImagePasteError("Windows returned an invalid image path")
    return translated
