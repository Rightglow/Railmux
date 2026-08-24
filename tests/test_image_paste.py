from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from railmux import image_paste


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_png_validation_accepts_complete_bounded_image_and_rejects_bad_crc():
    assert image_paste.validate_png(_PNG) == (1, 1)

    corrupted = bytearray(_PNG)
    corrupted[-5] ^= 1
    with pytest.raises(image_paste.ImagePasteError, match="integrity"):
        image_paste.validate_png(bytes(corrupted))


def test_image_shortcut_split_preserves_all_non_shortcut_bytes():
    assert image_paste.split_shortcuts(b"before\x1bvafter\x16") == (
        b"before",
        b"\x1bv",
        b"after",
        b"\x16",
    )
    assert image_paste.split_shortcuts(b"ordinary text") == (b"ordinary text",)


def test_windows_clipboard_reader_returns_png_without_shell_interpolation(monkeypatch):
    calls = []
    monkeypatch.setattr(image_paste, "_powershell_command", lambda: ("pwsh.exe",))

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=_PNG)

    monkeypatch.setattr(image_paste.subprocess, "run", run)

    assert image_paste._read_windows_clipboard() == _PNG
    assert calls[0][0][0] == "pwsh.exe"
    assert calls[0][0][1:5] == (
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Sta",
    )
    assert calls[0][1]["timeout"] == image_paste._CLIPBOARD_TIMEOUT


def test_store_png_uses_private_generated_file_and_never_reuses_user_name(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "images"
    unrelated = root / "keep-me.txt"
    root.mkdir()
    unrelated.write_text("mine", encoding="utf-8")
    monkeypatch.setattr(image_paste, "_image_root", lambda: root)

    stored = image_paste.store_png(_PNG)

    assert stored.parent == root
    assert image_paste._GENERATED_IMAGE_RE.fullmatch(stored.name)
    assert stored.read_bytes() == _PNG
    assert stored.stat().st_mode & 0o777 == 0o600
    assert unrelated.read_text(encoding="utf-8") == "mine"
