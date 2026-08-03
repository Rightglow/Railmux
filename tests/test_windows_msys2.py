from __future__ import annotations

import hashlib
import io
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from railmux import windows_msys2
from railmux.windows_msys2 import (
    MSYS2_ARCHIVE_NAME,
    MSYS2_ARCHIVE_SHA256,
    MSYS2_ARCHIVE_SIZE,
    MSYS2_ARCHIVE_SOURCES,
    MSYS2_RUNTIME_ID,
    Msys2Runtime,
    RuntimeInstallError,
    download_from_sources,
    download_verified,
    find_runtime,
    install_managed_runtime,
    managed_root,
    probe_runtime,
)


VERSION = "0.4.0.dev5"


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


class FakeResponse(io.BytesIO):
    def __init__(
        self,
        payload: bytes,
        *,
        content_length: str | None = None,
        final_url: str = "https://example.invalid/archive.exe",
    ):
        super().__init__(payload)
        self._final_url = final_url
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()

    def geturl(self):
        return self._final_url


def completed(argv, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def make_runtime(root: Path, *, managed: bool) -> Msys2Runtime:
    bash = root / "usr" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.write_bytes(b"test")
    if managed:
        (root / "railmux-runtime.json").write_text(
            json.dumps(
                {"schema": 1, "runtime": MSYS2_RUNTIME_ID, "railmux": VERSION}
            ),
            encoding="utf-8",
        )
    return Msys2Runtime(root, managed=managed)


def test_approved_archive_sources_are_https_and_share_one_pinned_artifact():
    assert MSYS2_ARCHIVE_SIZE == 52_820_994
    assert len(MSYS2_ARCHIVE_SOURCES) == 4
    assert len({url for _label, url in MSYS2_ARCHIVE_SOURCES}) == 4
    for label, url in MSYS2_ARCHIVE_SOURCES:
        assert label
        assert url.startswith("https://")
        assert url.endswith(f"/{MSYS2_ARCHIVE_NAME}")


def test_download_reports_bytes_and_percentage_on_a_terminal(tmp_path, monkeypatch):
    payload = b"a" * (2 * 1024 * 1024)
    progress = TtyBuffer()
    monkeypatch.setattr(windows_msys2.sys, "stderr", progress)
    monkeypatch.setattr(
        windows_msys2.urllib.request,
        "urlopen",
        lambda _url, timeout: FakeResponse(
            payload,
            content_length=str(len(payload)),
        ),
    )
    destination = tmp_path / "archive.exe"

    download_verified(
        "https://example.invalid/archive.exe",
        destination,
        hashlib.sha256(payload).hexdigest(),
    )

    rendered = progress.getvalue()
    assert "\r  example.invalid: 1.0 MiB / 2.0 MiB (50.0%)" in rendered
    assert "\r  example.invalid: 2.0 MiB / 2.0 MiB (100.0%)\n" in rendered
    assert destination.read_bytes() == payload


def test_download_hash_failure_removes_the_untrusted_archive(tmp_path, monkeypatch):
    payload = b"not the pinned archive"
    monkeypatch.setattr(
        windows_msys2.urllib.request,
        "urlopen",
        lambda _url, timeout: FakeResponse(
            payload,
            content_length=str(len(payload)),
        ),
    )
    destination = tmp_path / "archive.exe"

    with pytest.raises(RuntimeInstallError, match="SHA-256 verification"):
        download_verified(
            "https://example.invalid/archive.exe",
            destination,
            MSYS2_ARCHIVE_SHA256,
        )

    assert not destination.exists()


def test_download_rejects_an_https_downgrade_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        windows_msys2.urllib.request,
        "urlopen",
        lambda _url, timeout: FakeResponse(
            b"untrusted",
            final_url="http://mirror.invalid/archive.exe",
        ),
    )
    destination = tmp_path / "archive.exe"

    with pytest.raises(RuntimeInstallError, match="redirected outside HTTPS"):
        download_verified(
            "https://example.invalid/archive.exe",
            destination,
            MSYS2_ARCHIVE_SHA256,
        )

    assert not destination.exists()


def test_download_falls_back_after_removing_a_partial_archive(tmp_path):
    destination = tmp_path / "archive.exe"
    sources = (
        ("first", "https://first.invalid/a"),
        ("second", "https://second.invalid/a"),
    )
    attempted = []

    def downloader(url, target, sha256):
        attempted.append((url, sha256))
        if "first" in url:
            target.write_bytes(b"partial")
            raise RuntimeInstallError("could not download the pinned MSYS2 runtime")
        assert not target.exists()
        target.write_bytes(b"verified")

    selected = download_from_sources(
        destination,
        MSYS2_ARCHIVE_SHA256,
        sources=sources,
        downloader=downloader,
    )

    assert selected == "second"
    assert [url for url, _sha256 in attempted] == [url for _label, url in sources]
    assert {sha256 for _url, sha256 in attempted} == {MSYS2_ARCHIVE_SHA256}
    assert destination.read_bytes() == b"verified"


def test_download_fails_closed_after_every_approved_source(tmp_path):
    destination = tmp_path / "archive.exe"
    sources = (
        ("first", "https://first.invalid/a"),
        ("second", "https://second.invalid/a"),
    )

    def downloader(_url, target, _sha256):
        target.write_bytes(b"untrusted")
        raise RuntimeInstallError("archive failed verification")

    with pytest.raises(RuntimeInstallError, match="any approved source"):
        download_from_sources(
            destination,
            MSYS2_ARCHIVE_SHA256,
            sources=sources,
            downloader=downloader,
        )

    assert not destination.exists()


def test_handoff_preserves_argv_and_uses_child_only_msys_environment(tmp_path):
    runtime = make_runtime(tmp_path / "msys", managed=False)
    arguments = ["ssh", "user@example", "--ssh-args=-J jump host", "开发"]
    parent = {
        "PATH": r"C:\Windows\System32",
        "USERPROFILE": r"C:\Users\用户",
        "WT_SESSION": "terminal",
        "TMUX": "untrusted-parent",
        "TERM": "dumb",
        "LANG": "zh_CN.GBK",
    }

    argv = runtime.argv(arguments)
    child = runtime.environment(parent)

    assert argv[-len(arguments) :] == arguments
    assert argv[:4] == [
        str(runtime.bash),
        "--noprofile",
        "--norc",
        "-c",
    ]
    assert 'exec /opt/railmux/venv/bin/railmux "$@"' in argv[4]
    assert child["PATH"].startswith(str(runtime.root / "usr" / "bin"))
    assert child["HOME"] == r"C:\Users\用户"
    assert child["MSYS2_ARG_CONV_EXCL"] == "*"
    assert child["MSYS2_PATH_TYPE"] == "inherit"
    assert child["TERM"] == "xterm-256color"
    assert child["LANG"] == "C.UTF-8"
    assert child["LC_ALL"] == "C.UTF-8"
    assert child["PYTHONUTF8"] == "1"
    assert child["COLORTERM"] == "truecolor"
    assert "TMUX" not in child
    assert parent["PATH"] == r"C:\Windows\System32"


def test_managed_runtime_requires_utf8_marker_and_exact_package_version(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ, version=VERSION)
    assert root is not None
    runtime = make_runtime(root, managed=True)
    probe = MagicMock(return_value=completed([], stdout=b"railmux 0.4.0.dev5\n"))

    assert probe_runtime(runtime, version=VERSION, environ=environ, probe=probe)
    assert not probe_runtime(runtime, version="0.4.0.dev6", environ=environ, probe=probe)


def test_runtime_probe_retries_one_transient_cold_start_failure(tmp_path):
    runtime = make_runtime(tmp_path / "msys", managed=False)
    probe = MagicMock(
        side_effect=[
            completed([], returncode=1),
            completed([], stdout=b"railmux 0.4.0.dev5\n"),
        ]
    )

    assert probe_runtime(runtime, version=VERSION, environ={}, probe=probe)
    assert probe.call_count == 2


def test_each_preview_version_uses_a_separate_runtime_generation(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}

    dev4 = managed_root(environ, version="0.4.0.dev4")
    dev5 = managed_root(environ, version="0.4.0.dev5")

    assert dev4 != dev5
    assert dev4 is not None and dev4.parent == dev5.parent


def test_explicit_user_runtime_is_probed_but_never_requires_managed_marker(tmp_path):
    root = tmp_path / "用户-owned-msys"
    runtime = make_runtime(root, managed=False)
    probe = MagicMock(return_value=completed([], stdout=b"railmux 0.4.0.dev5\n"))
    environ = {"RAILMUX_MSYS2_ROOT": str(root), "USERPROFILE": r"C:\Users\u"}

    found = find_runtime(version=VERSION, environ=environ, probe=probe)

    assert found == runtime
    assert found is not None and not found.managed


def test_wrong_explicit_runtime_does_not_fall_back_to_or_modify_managed(tmp_path):
    requested = make_runtime(tmp_path / "requested", managed=False)
    environ = {
        "LOCALAPPDATA": str(tmp_path / "appdata"),
        "RAILMUX_MSYS2_ROOT": str(requested.root),
    }
    probe = MagicMock(return_value=completed([], returncode=1))

    assert find_runtime(version=VERSION, environ=environ, probe=probe) is None
    assert not managed_root(environ, version=VERSION).exists()


def test_installer_never_mutates_or_bypasses_explicit_user_runtime(tmp_path):
    environ = {
        "LOCALAPPDATA": str(tmp_path / "appdata"),
        "RAILMUX_MSYS2_ROOT": str(tmp_path / "user-msys"),
    }

    with pytest.raises(RuntimeInstallError, match="user-owned runtime"):
        install_managed_runtime(version=VERSION, environ=environ)

    assert not Path(environ["LOCALAPPDATA"]).exists()


def test_complete_but_temporarily_unverified_runtime_is_not_called_incomplete(
    tmp_path,
):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ, version=VERSION)
    assert root is not None
    make_runtime(root, managed=True)

    @contextmanager
    def unlocked(_base):
        yield

    with pytest.raises(RuntimeInstallError, match="present but could not be verified"):
        install_managed_runtime(
            version=VERSION,
            environ=environ,
            probe=lambda argv, **_kwargs: completed(argv, returncode=1),
            lock_factory=unlocked,
        )


def test_install_is_staged_and_activated_only_after_exact_probe(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path), "USERPROFILE": r"C:\Users\u"}
    commands = []

    def downloader(_url, destination, _sha256):
        assert destination.name == MSYS2_ARCHIVE_NAME
        destination.write_bytes(b"verified fixture")

    def runner(argv, *, env, check):
        commands.append(argv)
        if argv[0].endswith(MSYS2_ARCHIVE_NAME):
            output = Path(next(arg[2:] for arg in argv if arg.startswith("-o")))
            bash = output / "msys64" / "usr" / "bin" / "bash.exe"
            bash.parent.mkdir(parents=True)
            bash.write_bytes(b"fixture")
        return completed(argv)

    def probe(argv, *, env, timeout):
        return completed(argv, stdout=b"railmux 0.4.0.dev5\n")

    @contextmanager
    def unlocked(_base):
        yield

    runtime = install_managed_runtime(
        version=VERSION,
        environ=environ,
        downloader=downloader,
        runner=runner,
        probe=probe,
        lock_factory=unlocked,
    )

    assert runtime == Msys2Runtime(
        managed_root(environ, version=VERSION), managed=True
    )
    assert runtime.bash.is_file()
    assert json.loads(
        (runtime.root / "railmux-runtime.json").read_text(encoding="utf-8")
    )["railmux"] == VERSION
    joined = [" ".join(command) for command in commands]
    assert any("pacman -Syu --noconfirm" in command for command in joined)
    assert any("--needed tmux python python-pip" in command for command in joined)
    assert any("python -m venv /opt/railmux/venv" in command for command in joined)
    assert any('railmux[ssh]==$1' in command for command in joined)
    assert not list((Path(environ["LOCALAPPDATA"]) / "Railmux" / "runtimes").glob(".install-*"))
