from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from railmux import __version__
from railmux import windows_msys2
from railmux.windows_install_log import InstallReporter
from railmux.windows_msys2 import (
    MSYS2_ARCHIVE_NAME,
    MSYS2_ARCHIVE_SHA256,
    MSYS2_ARCHIVE_SIZE,
    MSYS2_ARCHIVE_SOURCES,
    MSYS2_BASE_LINEAGE_SHA256,
    MSYS2_RUNTIME_ID,
    Msys2Runtime,
    RuntimeInstallError,
    download_adaptive,
    download_from_sources,
    download_verified,
    find_runtime,
    install_managed_runtime,
    managed_root,
    probe_runtime,
)
from railmux.windows_pacman import PacmanMirrorDecision


VERSION = __version__
LEGACY_VERSION = "0.4.0.dev10"
_ANSI_STYLE_RE = re.compile(r"\x1b\[[0-9;]*m")
_TEST_PACKAGE_INVENTORY = (
    b"python 3.12.13-1\npython-pip 26.1.2-1\ntmux 3.7.b-1\n"
)
_TEST_CONTENT_ID = hashlib.sha256(_TEST_PACKAGE_INVENTORY).hexdigest()


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
        status: int = 200,
        content_range: str | None = None,
    ):
        super().__init__(payload)
        self._final_url = final_url
        self.status = status
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        if content_range is not None:
            self.headers["Content-Range"] = content_range

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()

    def geturl(self):
        return self._final_url


def completed(argv, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def make_runtime(
    root: Path,
    *,
    managed: bool,
    version: str = VERSION,
    shared: bool = False,
) -> Msys2Runtime:
    bash = root / "usr" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.write_bytes(b"test")
    if managed:
        if shared:
            (root / "railmux-base.json").write_text(
                json.dumps({"schema": 1, "runtime": MSYS2_RUNTIME_ID}),
                encoding="utf-8",
            )
            (root / "railmux-base-content-v1.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "runtime": MSYS2_RUNTIME_ID,
                        "archive_sha256": MSYS2_BASE_LINEAGE_SHA256,
                        "content_id": _TEST_CONTENT_ID,
                        "package_count": 3,
                        "core_packages": {
                            "tmux": "3.7.b-1",
                            "python": "3.12.13-1",
                            "python-pip": "26.1.2-1",
                        },
                    }
                ),
                encoding="utf-8",
            )
            app_name = f"railmux-{version}"
            app_root = root / "opt" / "railmux" / "apps" / app_name
            app_root.mkdir(parents=True)
            (app_root / "railmux-app.json").write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "runtime": MSYS2_RUNTIME_ID,
                        "railmux": version,
                        "base_content_id": _TEST_CONTENT_ID,
                    }
                ),
                encoding="utf-8",
            )
            return Msys2Runtime(root, managed=True, app_name=app_name)
        (root / "railmux-runtime.json").write_text(
            json.dumps(
                {"schema": 1, "runtime": MSYS2_RUNTIME_ID, "railmux": version}
            ),
            encoding="utf-8",
        )
    return Msys2Runtime(root, managed=managed)


def add_marked_app(root: Path, version: str) -> Path:
    application = root / "opt" / "railmux" / "apps" / f"railmux-{version}"
    executable = application / "venv" / "bin" / "railmux"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    (application / "railmux-app.json").write_text(
        json.dumps({
            "schema": 2,
            "runtime": MSYS2_RUNTIME_ID,
            "railmux": version,
            "base_content_id": _TEST_CONTENT_ID,
        }),
        encoding="utf-8",
    )
    return application


def test_approved_archive_sources_are_https_and_share_one_pinned_artifact():
    assert MSYS2_ARCHIVE_NAME.endswith(".tar.xz")
    assert not MSYS2_ARCHIVE_NAME.endswith(".sfx.exe")
    assert MSYS2_ARCHIVE_SIZE == 53_466_096
    assert MSYS2_ARCHIVE_SHA256 == (
        "6b4a986a3ec4f1e40313bdf17903a6f5c854373d4230c40f14c5e35c4bac7fce"
    )
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

    rendered = _ANSI_STYLE_RE.sub("", progress.getvalue())
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


def test_range_probe_requires_exact_offset_and_reuses_its_bytes():
    payload = b"0123456789"
    requests = []
    ticks = iter((0.0, 0.5, 0.5))

    def opener(request, *, timeout):
        requests.append((request, timeout))
        return FakeResponse(
            payload,
            content_length=str(len(payload)),
            status=206,
            content_range="bytes 0-9/10",
        )

    probe = windows_msys2._probe_archive_source(
        "test",
        "https://example.invalid/archive.exe",
        len(payload),
        opener=opener,
        clock=lambda: next(ticks),
    )

    assert probe.data == payload
    assert probe.rate == 20.0
    assert requests[0][0].get_header("Range") == "bytes=0-9"
    assert requests[0][0].get_header("Accept-encoding") == "identity"


def test_range_probe_rejects_a_wrong_content_range():
    def opener(_request, *, timeout):
        return FakeResponse(
            b"0123456789",
            content_length="10",
            status=206,
            content_range="bytes 1-10/10",
        )

    with pytest.raises(RuntimeInstallError, match="wrong content range"):
        windows_msys2._probe_archive_source(
            "test",
            "https://example.invalid/archive.exe",
            10,
            opener=opener,
        )


def test_range_resume_requires_206_and_the_exact_remaining_offset():
    requests = []
    chunks = []

    def opener(request, *, timeout):
        requests.append(request)
        return FakeResponse(
            b"3456789",
            content_length="7",
            status=206,
            content_range="bytes 3-9/10",
        )

    windows_msys2._resume_archive_source(
        "test",
        "https://example.invalid/archive.exe",
        3,
        10,
        chunks.append,
        opener=opener,
    )

    assert b"".join(chunks) == b"3456789"
    assert requests[0].get_header("Range") == "bytes=3-9"


def test_adaptive_download_switches_to_a_materially_faster_source(tmp_path):
    payload = b"0123456789" * 10
    sources = (
        ("primary", "https://primary.invalid/archive"),
        ("fast", "https://fast.invalid/archive"),
        ("slow", "https://slow.invalid/archive"),
    )
    probes = {
        "primary": windows_msys2._ArchiveProbe(
            *sources[0], payload[:10], 100.0
        ),
        "fast": windows_msys2._ArchiveProbe(*sources[1], payload[:20], 1.0),
        "slow": windows_msys2._ArchiveProbe(*sources[2], payload[:10], 200.0),
    }
    resumes = []

    def probe(label, _url, _expected_size):
        return probes[label]

    def resume(label, _url, offset, expected_size, write_chunk):
        resumes.append((label, offset))
        write_chunk(payload[offset:expected_size])

    destination = tmp_path / "archive.exe"
    selected = download_adaptive(
        destination,
        hashlib.sha256(payload).hexdigest(),
        sources=sources,
        expected_size=len(payload),
        probe_source=probe,
        resume_source=resume,
    )

    assert selected == "fast"
    assert resumes == [("fast", 20)]
    assert destination.read_bytes() == payload


def test_adaptive_download_does_not_probe_mirrors_when_primary_is_fast(tmp_path):
    payload = b"0123456789" * 10
    sources = (
        ("primary", "https://primary.invalid/archive"),
        ("unused", "https://unused.invalid/archive"),
    )
    probed = []

    def probe(label, url, _expected_size):
        probed.append(label)
        if label != "primary":
            raise AssertionError("a fast primary must not load another mirror")
        return windows_msys2._ArchiveProbe(label, url, payload[:20], 0.01)

    def resume(_label, _url, offset, expected_size, write_chunk):
        write_chunk(payload[offset:expected_size])

    destination = tmp_path / "archive.exe"
    selected = download_adaptive(
        destination,
        hashlib.sha256(payload).hexdigest(),
        sources=sources,
        expected_size=len(payload),
        probe_source=probe,
        resume_source=resume,
    )

    assert selected == "primary"
    assert probed == ["primary"]
    assert destination.read_bytes() == payload


def test_adaptive_download_keeps_primary_when_others_are_not_materially_faster(
    tmp_path,
):
    payload = b"abcdefghij" * 10
    sources = (
        ("primary", "https://primary.invalid/archive"),
        ("similar", "https://similar.invalid/archive"),
    )
    probes = {
        "primary": windows_msys2._ArchiveProbe(
            *sources[0], payload[:10], 10.0
        ),
        "similar": windows_msys2._ArchiveProbe(
            *sources[1], payload[:20], 18.0
        ),
    }
    resumes = []

    def resume(label, _url, offset, expected_size, write_chunk):
        resumes.append((label, offset))
        write_chunk(payload[offset:expected_size])

    destination = tmp_path / "archive.exe"
    selected = download_adaptive(
        destination,
        hashlib.sha256(payload).hexdigest(),
        sources=sources,
        expected_size=len(payload),
        probe_source=lambda label, _url, _size: probes[label],
        resume_source=resume,
    )

    assert selected == "primary"
    assert resumes == [("primary", 10)]
    assert destination.read_bytes() == payload


def test_adaptive_failure_falls_back_to_an_ordinary_verified_download(
    tmp_path, monkeypatch
):
    destination = tmp_path / "archive.exe"
    sources = (
        ("first", "https://first.invalid/archive"),
        ("second", "https://second.invalid/archive"),
    )
    attempts = []

    def fail_adaptive(*_args, **_kwargs):
        destination.write_bytes(b"partial")
        raise RuntimeInstallError("range unsupported")

    def ordinary_download(url, path, _sha256):
        attempts.append(url)
        assert not path.exists()
        if "first" in url:
            raise RuntimeInstallError("first failed")
        path.write_bytes(b"verified")

    monkeypatch.setattr(windows_msys2, "download_adaptive", fail_adaptive)
    monkeypatch.setattr(windows_msys2, "download_verified", ordinary_download)

    selected = download_from_sources(
        destination,
        "unused",
        sources=sources,
    )

    assert selected == "second"
    assert attempts == [sources[0][1], sources[1][1]]
    assert destination.read_bytes() == b"verified"


def test_adaptive_download_preserves_offset_across_transfer_failure(tmp_path):
    payload = b"rail" * 25
    sources = (
        ("primary", "https://primary.invalid/archive"),
        ("fast", "https://fast.invalid/archive"),
    )
    probes = {
        "primary": windows_msys2._ArchiveProbe(
            *sources[0], payload[:10], 100.0
        ),
        "fast": windows_msys2._ArchiveProbe(*sources[1], payload[:20], 1.0),
    }
    resumes = []

    def resume(label, _url, offset, expected_size, write_chunk):
        resumes.append((label, offset))
        if label == "fast":
            write_chunk(payload[offset : offset + 10])
            raise RuntimeInstallError("connection lost")
        write_chunk(payload[offset:expected_size])

    destination = tmp_path / "archive.exe"
    selected = download_adaptive(
        destination,
        hashlib.sha256(payload).hexdigest(),
        sources=sources,
        expected_size=len(payload),
        probe_source=lambda label, _url, _size: probes[label],
        resume_source=resume,
    )

    assert selected == "fast"
    assert resumes == [("fast", 20), ("primary", 30)]
    assert destination.read_bytes() == payload


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
    assert 'exec "$executable" "$@"' in argv[4]
    assert argv[6] == "/opt/railmux/venv/bin/railmux"
    assert child["PATH"].startswith(str(runtime.root / "usr" / "bin"))
    assert child["HOME"] == r"C:\Users\用户"
    assert child["MSYS2_ARG_CONV_EXCL"] == "*"
    assert child["MSYS2_PATH_TYPE"] == "inherit"
    assert child["TERM"] == "xterm-256color"
    assert child["LANG"] == "C.UTF-8"
    assert child["LC_ALL"] == "C.UTF-8"
    assert child["PYTHONUTF8"] == "1"
    assert child["COLORTERM"] == "truecolor"
    assert "RAILMUX_MSYS2_RUNTIME_ID" not in child
    assert "TMUX" not in child
    assert parent["PATH"] == r"C:\Windows\System32"

    managed = make_runtime(tmp_path / "managed-msys", managed=True, shared=True)
    assert managed.environment(parent)["RAILMUX_MSYS2_RUNTIME_ID"] == (
        windows_msys2.MSYS2_RUNTIME_ID
    )
    assert managed.argv(["doctor"])[6] == (
        f"/opt/railmux/apps/railmux-{VERSION}/venv/bin/railmux"
    )


def test_managed_runtime_requires_utf8_marker_and_exact_package_version(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    runtime = make_runtime(root, managed=True, shared=True)
    probe = MagicMock(
        return_value=completed([], stdout=f"railmux {VERSION}\n".encode())
    )

    assert probe_runtime(runtime, version=VERSION, environ=environ, probe=probe)
    assert not probe_runtime(runtime, version="0.4.0.dev8", environ=environ, probe=probe)


def test_runtime_probe_retries_one_transient_cold_start_failure(tmp_path):
    runtime = make_runtime(tmp_path / "msys", managed=False)
    probe = MagicMock(
        side_effect=[
            completed([], returncode=1),
            completed([], stdout=f"railmux {VERSION}\n".encode()),
        ]
    )

    assert probe_runtime(runtime, version=VERSION, environ={}, probe=probe)
    assert probe.call_count == 2


def test_preview_versions_share_one_base_but_use_separate_app_layers(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}

    dev7 = managed_root(environ)
    dev8 = managed_root(environ)

    assert dev7 == dev8
    assert windows_msys2._app_name("0.4.0.dev7") != windows_msys2._app_name(
        "0.4.0.dev8"
    )


def test_explicit_user_runtime_is_probed_but_never_requires_managed_marker(tmp_path):
    root = tmp_path / "用户-owned-msys"
    runtime = make_runtime(root, managed=False)
    probe = MagicMock(
        return_value=completed([], stdout=f"railmux {VERSION}\n".encode())
    )
    environ = {"RAILMUX_MSYS2_ROOT": str(root), "USERPROFILE": r"C:\Users\u"}

    found = find_runtime(version=VERSION, environ=environ, probe=probe)

    assert found == runtime
    assert found is not None and not found.managed


def test_explicit_user_runtime_can_select_a_versioned_app_read_only(tmp_path):
    root = tmp_path / "user-owned-msys"
    make_runtime(root, managed=True, shared=True)
    expected_executable = (
        f"/opt/railmux/apps/railmux-{VERSION}/venv/bin/railmux"
    )

    def probe(argv, **_kwargs):
        return completed(
            argv,
            stdout=f"railmux {VERSION}\n".encode(),
            returncode=0 if argv[6] == expected_executable else 1,
        )

    found = find_runtime(
        version=VERSION,
        environ={"RAILMUX_MSYS2_ROOT": str(root)},
        probe=probe,
    )

    assert found == Msys2Runtime(
        root, managed=False, app_name=f"railmux-{VERSION}"
    )


def test_wrong_explicit_runtime_does_not_fall_back_to_or_modify_managed(tmp_path):
    requested = make_runtime(tmp_path / "requested", managed=False)
    environ = {
        "LOCALAPPDATA": str(tmp_path / "appdata"),
        "RAILMUX_MSYS2_ROOT": str(requested.root),
    }
    probe = MagicMock(return_value=completed([], returncode=1))

    assert find_runtime(version=VERSION, environ=environ, probe=probe) is None
    assert not managed_root(environ).exists()


def test_installer_never_mutates_or_bypasses_explicit_user_runtime(tmp_path):
    environ = {
        "LOCALAPPDATA": str(tmp_path / "appdata"),
        "RAILMUX_MSYS2_ROOT": str(tmp_path / "user-msys"),
    }

    with pytest.raises(RuntimeInstallError, match="user-owned runtime"):
        install_managed_runtime(version=VERSION, environ=environ)

    assert not Path(environ["LOCALAPPDATA"]).exists()


def test_reuse_only_never_escalates_to_a_full_base_install(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}

    @contextmanager
    def unlocked(_base):
        yield

    with pytest.raises(RuntimeInstallError, match="no full runtime installation"):
        install_managed_runtime(
            version=VERSION,
            environ=environ,
            reuse_only=True,
            downloader=lambda *_args: pytest.fail("must not download a base"),
            lock_factory=unlocked,
        )


def test_complete_but_temporarily_unverified_runtime_is_not_called_incomplete(
    tmp_path,
):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    make_runtime(root, managed=True, shared=True)

    @contextmanager
    def unlocked(_base):
        yield

    with pytest.raises(RuntimeInstallError, match="exact marker"):
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

    def extractor(archive, output, *, reporter):
        assert archive.name == MSYS2_ARCHIVE_NAME
        bash = output / "msys64" / "usr" / "bin" / "bash.exe"
        bash.parent.mkdir(parents=True)
        bash.write_bytes(b"fixture")
        config = output / "msys64" / "etc" / "pacman.conf"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "[options]\nParallelDownloads = 5\n"
            "[mingw64]\nInclude = /etc/pacman.d/mirrorlist.mingw\n"
            "[msys]\nInclude = /etc/pacman.d/mirrorlist.msys\n",
            encoding="utf-8",
        )

    def runner(argv, *, env, check):
        commands.append(argv)
        return completed(argv)

    def probe(argv, *, env, timeout):
        if "pacman -Q" in argv[4]:
            return completed(argv, stdout=_TEST_PACKAGE_INVENTORY)
        return completed(argv, stdout=f"railmux {VERSION}\n".encode())

    @contextmanager
    def unlocked(_base):
        yield

    runtime = install_managed_runtime(
        version=VERSION,
        environ=environ,
        downloader=downloader,
        runner=runner,
        extractor=extractor,
        probe=probe,
        lock_factory=unlocked,
        mirror_optimizer=lambda _root: PacmanMirrorDecision(
            None, None, False, (), ()
        ),
    )

    assert runtime == Msys2Runtime(
        managed_root(environ),
        managed=True,
        app_name=f"railmux-{VERSION}",
    )
    assert runtime.bash.is_file()
    assert json.loads(
        (runtime.root / "railmux-base.json").read_text(encoding="utf-8")
    )["runtime"] == MSYS2_RUNTIME_ID
    assert json.loads(
        (
            runtime.root
            / "opt"
            / "railmux"
            / "apps"
            / f"railmux-{VERSION}"
            / "railmux-app.json"
        ).read_text(encoding="utf-8")
    )["railmux"] == VERSION
    joined = [" ".join(command) for command in commands]
    base_updates = [command for command in joined if "-Syuu --noconfirm" in command]
    assert len(base_updates) == 2
    assert any("-Syu --noconfirm" in command for command in joined)
    assert any("--needed tmux python python-pip" in command for command in joined)
    assert any('python -m venv "$1/venv"' in command for command in joined)
    assert any('railmux[ssh]==$2' in command for command in joined)
    assert all(MSYS2_ARCHIVE_NAME not in command for command in joined)
    logs = list((Path(environ["LOCALAPPDATA"]) / "Railmux" / "logs").glob("*.log"))
    assert len(logs) == 1
    log = logs[0].read_text(encoding="utf-8")
    assert "[1/7] Preparing verified MSYS2" in log
    assert "--- MSYS2 base update pass 1 ---" in log
    assert "--- MSYS2 base update pass 2 ---" in log
    assert "Installation completed successfully" in log
    config = runtime.root / "etc" / "railmux-pacman.conf"
    assert "[msys]" in config.read_text(encoding="utf-8")
    assert "[mingw64]" not in config.read_text(encoding="utf-8")
    assert not list((Path(environ["LOCALAPPDATA"]) / "Railmux" / "runtimes").glob(".install-*"))


def test_dev11_adopts_verified_dev10_base_without_downloading_or_copying(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path), "USERPROFILE": r"C:\Users\u"}
    base = Path(environ["LOCALAPPDATA"]) / "Railmux" / "runtimes"
    legacy_root = (
        base / MSYS2_RUNTIME_ID / f"railmux-{LEGACY_VERSION}"
    )
    make_runtime(
        legacy_root,
        managed=True,
        version=LEGACY_VERSION,
        shared=False,
    )
    original_marker = (legacy_root / "railmux-runtime.json").read_bytes()
    commands = []

    current_executable = (
        f"/opt/railmux/apps/railmux-{VERSION}/venv/bin/railmux"
    )

    def probe(argv, *, env, timeout):
        if "pacman -Q" in argv[4]:
            return completed(argv, stdout=_TEST_PACKAGE_INVENTORY)
        if argv[6] == current_executable:
            return completed(argv, stdout=f"railmux {VERSION}\n".encode())
        if argv[6] == "/opt/railmux/venv/bin/railmux":
            return completed(argv, stdout=f"railmux {LEGACY_VERSION}\n".encode())
        return completed(argv, returncode=1)

    def runner(argv, *, env, check):
        commands.append(argv)
        return completed(argv)

    @contextmanager
    def unlocked(_base):
        yield

    runtime = install_managed_runtime(
        version=VERSION,
        environ=environ,
        downloader=lambda *_args: pytest.fail("MSYS2 must not be downloaded"),
        runner=runner,
        probe=probe,
        lock_factory=unlocked,
        mirror_optimizer=lambda _root: pytest.fail("pacman must not run"),
    )

    assert runtime.root == legacy_root
    assert runtime.app_name == f"railmux-{VERSION}"
    assert (legacy_root / "railmux-runtime.json").read_bytes() == original_marker
    assert windows_msys2._base_marker_matches(legacy_root)
    assert windows_msys2._app_marker_matches(
        legacy_root,
        app_name=f"railmux-{VERSION}",
        version=VERSION,
    )
    assert all("pacman" not in " ".join(command) for command in commands)
    assert len(commands) == 2
    final_posix = f"/opt/railmux/apps/railmux-{VERSION}"
    assert commands[0][-1] == final_posix
    pip_cache = str(Path(environ["LOCALAPPDATA"]) / "Railmux" / "cache" / "pip")
    assert commands[1][-5:] == [final_posix, VERSION, pip_cache, "60", "5"]
    assert '--cache-dir "$cache"' in commands[1][4]
    assert "--no-cache-dir" not in commands[1][4]
    assert find_runtime(version=VERSION, environ=environ, probe=probe) == runtime
    assert probe_runtime(
        Msys2Runtime(legacy_root, managed=True),
        version=LEGACY_VERSION,
        environ=environ,
        probe=probe,
    )
    (legacy_root / "railmux-runtime.json").unlink()
    assert find_runtime(version=VERSION, environ=environ, probe=probe) == runtime
    log = next(
        (Path(environ["LOCALAPPDATA"]) / "Railmux" / "logs").glob("*.log")
    ).read_text(encoding="utf-8")
    assert "[1/3] Reusing verified MSYS2" in log
    assert "will not be downloaded, copied, or upgraded" in log


def test_shared_base_app_download_retries_with_private_cache_and_longer_timeout(
    tmp_path,
):
    environ = {"LOCALAPPDATA": str(tmp_path), "USERPROFILE": r"C:\Users\u"}
    root = managed_root(environ)
    assert root is not None
    previous_version = "0.4.0.dev11"
    make_runtime(
        root,
        managed=True,
        version=previous_version,
        shared=True,
    )
    previous_marker = (
        root
        / "opt"
        / "railmux"
        / "apps"
        / f"railmux-{previous_version}"
        / "railmux-app.json"
    )
    original_marker = previous_marker.read_bytes()
    commands = []
    package_attempts = 0

    def runner(argv, *, env, check):
        nonlocal package_attempts
        commands.append(argv)
        if "pip install" in argv[4]:
            package_attempts += 1
            if package_attempts == 1:
                return completed(
                    argv,
                    returncode=2,
                    stderr=b"ReadTimeoutError: files.pythonhosted.org\n",
                )
        return completed(argv)

    def probe(argv, **_kwargs):
        executable = argv[6]
        version = VERSION if executable.endswith(
            f"/railmux-{VERSION}/venv/bin/railmux"
        ) else previous_version
        return completed(argv, stdout=f"railmux {version}\n".encode())

    @contextmanager
    def unlocked(_base):
        yield

    runtime = install_managed_runtime(
        version=VERSION,
        environ=environ,
        runner=runner,
        probe=probe,
        lock_factory=unlocked,
    )

    cache = Path(environ["LOCALAPPDATA"]) / "Railmux" / "cache" / "pip"
    final_posix = f"/opt/railmux/apps/railmux-{VERSION}"
    assert runtime.app_name == f"railmux-{VERSION}"
    assert package_attempts == 2
    assert len(commands) == 3
    assert commands[1][-5:] == [final_posix, VERSION, str(cache), "60", "5"]
    assert commands[2][-5:] == [final_posix, VERSION, str(cache), "120", "5"]
    assert cache.is_dir()
    assert previous_marker.read_bytes() == original_marker
    assert windows_msys2._app_marker_matches(
        root, app_name=f"railmux-{VERSION}", version=VERSION
    )
    log = next(
        (Path(environ["LOCALAPPDATA"]) / "Railmux" / "logs").glob("*.log")
    ).read_text(encoding="utf-8")
    assert "retrying once with the Railmux-private cache" in log
    assert "120-second network timeout" in log


def test_shared_base_failed_package_recovery_never_publishes_or_escalates(
    tmp_path,
):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    previous_version = "0.4.0.dev11"
    make_runtime(root, managed=True, version=previous_version, shared=True)
    previous_marker = (
        root
        / "opt"
        / "railmux"
        / "apps"
        / f"railmux-{previous_version}"
        / "railmux-app.json"
    )
    original_marker = previous_marker.read_bytes()
    commands = []

    def runner(argv, *, env, check):
        commands.append(argv)
        if "pip install" in argv[4]:
            return completed(
                argv,
                returncode=2,
                stderr=b"ReadTimeoutError: files.pythonhosted.org\n",
            )
        return completed(argv)

    @contextmanager
    def unlocked(_base):
        yield

    with pytest.raises(RuntimeInstallError, match="exit code 2"):
        install_managed_runtime(
            version=VERSION,
            environ=environ,
            runner=runner,
            probe=lambda argv, **_kwargs: completed(
                argv, stdout=f"railmux {previous_version}\n".encode()
            ),
            lock_factory=unlocked,
            downloader=lambda *_args: pytest.fail("must not download a base"),
            mirror_optimizer=lambda _root: pytest.fail("pacman must not run"),
        )

    app = root / "opt" / "railmux" / "apps" / f"railmux-{VERSION}"
    assert len(commands) == 3
    assert sum("pip install" in command[4] for command in commands) == 2
    assert all("pacman" not in command[4] for command in commands)
    assert not (app / "railmux-app.json").exists()
    assert previous_marker.read_bytes() == original_marker


def test_non_network_package_failure_is_not_retried(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    previous_version = "0.4.0.dev11"
    make_runtime(root, managed=True, version=previous_version, shared=True)
    commands = []

    def runner(argv, *, env, check):
        commands.append(argv)
        if "pip install" in argv[4]:
            return completed(
                argv,
                returncode=1,
                stderr=b"No matching distribution found\n",
            )
        return completed(argv)

    @contextmanager
    def unlocked(_base):
        yield

    with pytest.raises(RuntimeInstallError, match="exit code 1"):
        install_managed_runtime(
            version=VERSION,
            environ=environ,
            runner=runner,
            probe=lambda argv, **_kwargs: completed(
                argv, stdout=f"railmux {previous_version}\n".encode()
            ),
            lock_factory=unlocked,
        )

    assert len(commands) == 2
    assert sum("pip install" in command[4] for command in commands) == 1


def test_pip_cache_reparse_point_is_rejected_before_app_commands(
    tmp_path, monkeypatch
):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    previous_version = "0.4.0.dev11"
    make_runtime(root, managed=True, version=previous_version, shared=True)
    app = root / "opt" / "railmux" / "apps" / f"railmux-{VERSION}"
    app.mkdir()
    unpublished = app / "unpublished"
    unpublished.write_bytes(b"preserve until cache validation passes")
    cache = Path(environ["LOCALAPPDATA"]) / "Railmux" / "cache" / "pip"
    cache.mkdir(parents=True)
    original_lstat = Path.lstat
    original_is_symlink = Path.is_symlink

    def fake_lstat(path):
        if path == cache:
            return SimpleNamespace(st_file_attributes=0x400)
        return original_lstat(path)

    def fake_is_symlink(path):
        if path == cache:
            return False
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    @contextmanager
    def unlocked(_base):
        yield

    with pytest.raises(RuntimeInstallError, match="link or reparse point"):
        install_managed_runtime(
            version=VERSION,
            environ=environ,
            runner=lambda *_args, **_kwargs: pytest.fail(
                "app commands must not start"
            ),
            probe=lambda argv, **_kwargs: completed(
                argv, stdout=f"railmux {previous_version}\n".encode()
            ),
            lock_factory=unlocked,
        )

    assert unpublished.read_bytes() == b"preserve until cache validation passes"


def test_mismatched_legacy_marker_is_never_an_adoption_candidate(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    base = Path(environ["LOCALAPPDATA"]) / "Railmux" / "runtimes"
    root = base / MSYS2_RUNTIME_ID / f"railmux-{LEGACY_VERSION}"
    make_runtime(root, managed=True, version="0.4.0.dev9")

    assert windows_msys2.reusable_managed_base_candidate(environ) is None

    @contextmanager
    def unlocked(_base):
        yield

    with pytest.raises(RuntimeInstallError, match="no full runtime installation"):
        install_managed_runtime(
            version=VERSION,
            environ=environ,
            reuse_only=True,
            downloader=lambda *_args: pytest.fail("must not download"),
            lock_factory=unlocked,
        )


def test_newest_legacy_candidate_uses_semantic_preview_order(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    base = Path(environ["LOCALAPPDATA"]) / "Railmux" / "runtimes"
    for version in ("0.4.0.dev9", "0.4.0.dev10"):
        make_runtime(
            base / MSYS2_RUNTIME_ID / f"railmux-{version}",
            managed=True,
            version=version,
        )

    candidate = windows_msys2.reusable_managed_base_candidate(environ)

    assert candidate is not None
    assert candidate[0].name == "railmux-0.4.0.dev10"
    assert candidate[1] == "0.4.0.dev10"


def test_marked_shared_base_with_missing_bash_fails_actionably(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    runtime = make_runtime(root, managed=True, shared=True)
    runtime.bash.unlink()

    @contextmanager
    def unlocked(_base):
        yield

    with pytest.raises(RuntimeInstallError, match="bash.exe is missing"):
        install_managed_runtime(
            version=VERSION,
            environ=environ,
            reuse_only=True,
            lock_factory=unlocked,
        )


def test_markerless_interrupted_app_layer_is_rebuilt_safely(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    make_runtime(
        root,
        managed=True,
        version=LEGACY_VERSION,
        shared=True,
    )
    app = root / "opt" / "railmux" / "apps" / f"railmux-{VERSION}"
    app.mkdir()
    abandoned = app / "partial-download"
    abandoned.write_bytes(b"unpublished")
    commands = []

    def runner(argv, *, env, check):
        commands.append(argv)
        return completed(argv)

    def probe(argv, **_kwargs):
        version = VERSION if argv[6].endswith(
            f"/railmux-{VERSION}/venv/bin/railmux"
        ) else LEGACY_VERSION
        return completed(argv, stdout=f"railmux {version}\n".encode())

    @contextmanager
    def unlocked(_base):
        yield

    runtime = install_managed_runtime(
        version=VERSION,
        environ=environ,
        runner=runner,
        probe=probe,
        lock_factory=unlocked,
    )

    assert runtime.app_name == f"railmux-{VERSION}"
    assert not abandoned.exists()
    assert windows_msys2._app_marker_matches(
        root, app_name=f"railmux-{VERSION}", version=VERSION
    )
    assert len(commands) == 2
    log = next(
        (Path(environ["LOCALAPPDATA"]) / "Railmux" / "logs").glob("*.log")
    ).read_text(encoding="utf-8")
    assert "provider session files are outside this directory" in log


def test_incomplete_canonical_base_blocks_legacy_adoption(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    base = Path(environ["LOCALAPPDATA"]) / "Railmux" / "runtimes"
    canonical = base / "shared" / MSYS2_RUNTIME_ID
    canonical.mkdir(parents=True)
    legacy = base / MSYS2_RUNTIME_ID / f"railmux-{LEGACY_VERSION}"
    make_runtime(legacy, managed=True, version=LEGACY_VERSION)

    @contextmanager
    def unlocked(_base):
        yield

    with pytest.raises(RuntimeInstallError, match="canonical shared MSYS2"):
        install_managed_runtime(
            version=VERSION,
            environ=environ,
            reuse_only=True,
            probe=lambda argv, **_kwargs: completed(
                argv, stdout=f"railmux {LEGACY_VERSION}\n".encode()
            ),
            lock_factory=unlocked,
        )


def test_verified_base_archive_cache_avoids_a_second_download(tmp_path, monkeypatch):
    payload = b"verified cached archive"
    monkeypatch.setattr(windows_msys2, "MSYS2_ARCHIVE_SIZE", len(payload))
    monkeypatch.setattr(
        windows_msys2,
        "MSYS2_ARCHIVE_SHA256",
        hashlib.sha256(payload).hexdigest(),
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    archive = cache / MSYS2_ARCHIVE_NAME
    archive.write_bytes(payload)

    selected, source = windows_msys2._prepare_cached_archive(
        cache,
        downloader=lambda *_args: pytest.fail("cache should avoid downloading"),
    )

    assert selected == archive
    assert source == "verified local cache"


def _write_test_tar(path: Path, entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
    with tarfile.open(path, mode="w:xz") as archive:
        for member, payload in entries:
            archive.addfile(member, io.BytesIO(payload) if member.isreg() else None)


def _tar_directory(name: str) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = 0o755
    return member, b""


def _tar_file(name: str, payload: bytes) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    member.mode = 0o755 if name.endswith(".exe") else 0o644
    return member, payload


def test_tar_xz_extraction_is_internal_bounded_and_progress_visible(
    tmp_path,
    monkeypatch,
):
    entries = [
        _tar_directory("msys64"),
        _tar_directory("msys64/usr"),
        _tar_file("msys64/usr/bash.exe", b"verified bash"),
        _tar_file("msys64/usr/read-only-upstream", b"must stay writable"),
    ]
    entries[-1][0].mode = 0o444
    archive = tmp_path / MSYS2_ARCHIVE_NAME
    _write_test_tar(archive, entries)
    monkeypatch.setattr(windows_msys2, "MSYS2_ARCHIVE_MEMBER_COUNT", 4)
    monkeypatch.setattr(
        windows_msys2,
        "MSYS2_ARCHIVE_UNPACKED_SIZE",
        len(b"verified bash") + len(b"must stay writable"),
    )
    destination = tmp_path / "stage"
    destination.mkdir()
    log = tmp_path / "install.log"
    output = io.StringIO()

    with InstallReporter(log, verbose=False, stream=output) as reporter:
        windows_msys2._extract_msys2_archive(archive, destination, reporter=reporter)

    assert (destination / "msys64" / "usr" / "bash.exe").read_bytes() == (
        b"verified bash"
    )
    package_owned = destination / "msys64" / "usr" / "read-only-upstream"
    if os.name == "nt":
        readonly = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
        assert not getattr(package_owned.stat(), "st_file_attributes", 0) & readonly
    else:
        assert stat.S_IMODE(package_owned.stat().st_mode) == 0o444
    rendered = output.getvalue()
    assert "Extracting private runtime: 1/4 files (25%)" in rendered
    assert "Extracting private runtime: 4/4 files (100%)" in rendered
    assert "MSYS2 archive extraction" in log.read_text(encoding="utf-8")


def test_native_windows_extraction_clears_posix_read_only_modes(
    tmp_path, monkeypatch,
):
    path = tmp_path / "package-owned"
    path.write_bytes(b"fixture")
    applied = []
    monkeypatch.setattr(windows_msys2, "_NATIVE_WINDOWS", True)
    monkeypatch.setattr(
        windows_msys2.os, "chmod", lambda target, mode: applied.append((target, mode))
    )

    windows_msys2._apply_archive_mode(path, 0o444)

    assert applied == [(path, stat.S_IWRITE)]


@pytest.mark.parametrize(
    ("member", "message"),
    [
        (_tar_file("../outside", b"bad"), "unsafe path"),
        (_tar_file("/absolute", b"bad"), "unsafe path"),
        (_tar_file(r"msys64\..\outside", b"bad"), "unsafe path"),
    ],
)
def test_tar_xz_extraction_rejects_paths_outside_staging(
    tmp_path,
    monkeypatch,
    member,
    message,
):
    archive = tmp_path / MSYS2_ARCHIVE_NAME
    _write_test_tar(archive, [member])
    monkeypatch.setattr(windows_msys2, "MSYS2_ARCHIVE_MEMBER_COUNT", 1)
    monkeypatch.setattr(windows_msys2, "MSYS2_ARCHIVE_UNPACKED_SIZE", 3)
    destination = tmp_path / "stage"
    destination.mkdir()

    with InstallReporter(
        tmp_path / "install.log", verbose=False, stream=io.StringIO()
    ) as reporter:
        with pytest.raises(RuntimeInstallError, match=message):
            windows_msys2._extract_msys2_archive(
                archive, destination, reporter=reporter
            )

    assert not (tmp_path / "outside").exists()


def test_tar_xz_extraction_rejects_links_and_special_files(tmp_path, monkeypatch):
    member = tarfile.TarInfo("msys64/link")
    member.type = tarfile.SYMTYPE
    member.linkname = "../outside"
    archive = tmp_path / MSYS2_ARCHIVE_NAME
    _write_test_tar(archive, [_tar_directory("msys64"), (member, b"")])
    monkeypatch.setattr(windows_msys2, "MSYS2_ARCHIVE_MEMBER_COUNT", 2)
    monkeypatch.setattr(windows_msys2, "MSYS2_ARCHIVE_UNPACKED_SIZE", 0)
    destination = tmp_path / "stage"
    destination.mkdir()

    with InstallReporter(
        tmp_path / "install.log", verbose=False, stream=io.StringIO()
    ) as reporter:
        with pytest.raises(RuntimeInstallError, match="unsupported link"):
            windows_msys2._extract_msys2_archive(
                archive, destination, reporter=reporter
            )


def test_tar_xz_extraction_rejects_changed_inventory_without_publishing(
    tmp_path,
    monkeypatch,
):
    archive = tmp_path / MSYS2_ARCHIVE_NAME
    _write_test_tar(archive, [_tar_directory("msys64")])
    monkeypatch.setattr(windows_msys2, "MSYS2_ARCHIVE_MEMBER_COUNT", 2)
    monkeypatch.setattr(windows_msys2, "MSYS2_ARCHIVE_UNPACKED_SIZE", 0)
    destination = tmp_path / "stage"
    destination.mkdir()

    with InstallReporter(
        tmp_path / "install.log", verbose=False, stream=io.StringIO()
    ) as reporter:
        with pytest.raises(RuntimeInstallError, match="member count changed"):
            windows_msys2._extract_msys2_archive(
                archive, destination, reporter=reporter
            )

    assert (destination / "msys64").is_dir()


def test_pacman_network_failure_retries_with_cache_and_relaxed_timeout(tmp_path):
    root = tmp_path / "msys64"
    bash = root / "usr" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.write_bytes(b"fixture")
    mirrorlist = root / "etc" / "pacman.d" / "mirrorlist.msys"
    mirrorlist.parent.mkdir(parents=True)
    tuna = "https://mirrors.tuna.tsinghua.edu.cn/msys2/msys/$arch/"
    repo = "https://repo.msys2.org/msys/$arch/"
    mirrorlist.write_text(
        f"Server = {tuna}\nServer = {repo}\n",
        encoding="utf-8",
    )
    attempts = []

    def runner(argv, *, env, check):
        attempts.append(argv)
        if len(attempts) == 1:
            return completed(
                argv,
                returncode=1,
                stdout=(
                    b"error: failed retrieving file 'python.pkg.tar.zst' from "
                    b"mirrors.tuna.tsinghua.edu.cn : The requested URL returned "
                    b"error: 403\n"
                    b"error: failed retrieving file 'tmux.pkg.tar.zst' from "
                    b"repo.msys2.org : Operation too slow.\n"
                ),
            )
        return completed(argv)

    path = tmp_path / "install.log"
    with InstallReporter(path, verbose=False, stream=io.StringIO()) as reporter:
        windows_msys2._run_pacman_with_recovery(
            root,
            packages=True,
            cache=tmp_path / "package-cache",
            env={},
            reporter=reporter,
            runner=runner,
            label="packages",
            mirror_optimizer=lambda _root: PacmanMirrorDecision(
                None, None, False, (), ()
            ),
        )

    assert len(attempts) == 2
    assert "--disable-download-timeout" not in attempts[0][4]
    assert "--disable-download-timeout" in attempts[1][4]
    assert str(tmp_path / "package-cache") == attempts[1][-1]
    rendered = mirrorlist.read_text(encoding="utf-8")
    assert f"# Railmux inactive: Server = {tuna}" in rendered
    assert f"Server = {repo}" in rendered


def test_base_update_restarts_after_confirmed_core_shutdown(tmp_path):
    attempts = []
    restart = (
        b"upgrading msys2-runtime...\n"
        b":: To complete this update all MSYS2 processes including this "
        b"terminal will be closed. Confirm to proceed [Y/n]\n"
    )

    def runner(argv, *, env, check):
        attempts.append(argv)
        update_attempts = [item for item in attempts if "-Syuu" in item[4]]
        if len(update_attempts) == 1 and "-Syuu" in argv[4]:
            return completed(argv, returncode=0xC0000135, stdout=restart)
        return completed(argv, stdout=b"there is nothing to do\n")

    stream = io.StringIO()
    with InstallReporter(
        tmp_path / "install.log", verbose=False, stream=stream
    ) as reporter:
        windows_msys2._run_base_update_with_restarts(
            tmp_path / "msys64",
            cache=tmp_path / "cache",
            env={},
            reporter=reporter,
            runner=runner,
            mirror_optimizer=lambda _root: PacmanMirrorDecision(
                None, None, False, (), ()
            ),
        )

    updates = [argv for argv in attempts if "-Syuu --noconfirm" in argv[4]]
    assert len(updates) == 2
    assert updates[0][0] == updates[1][0]
    assert windows_msys2._PRIVATE_GPG_SHUTDOWN_COMMAND in attempts[-1][4]
    rendered = stream.getvalue()
    assert "closed its own process as expected" in rendered
    assert "failed with exit code" not in rendered


def test_base_update_does_not_mask_unconfirmed_missing_dll(tmp_path, monkeypatch):
    monkeypatch.setattr(windows_msys2, "_NATIVE_WINDOWS", True)
    attempts = []

    def runner(argv, *, env, check):
        attempts.append(argv)
        if windows_msys2._PRIVATE_GPG_SHUTDOWN_COMMAND in argv[4]:
            return completed(argv)
        return completed(
            argv,
            returncode=0xC0000135,
            stdout=b"upgrading msys2-runtime...\n",
        )

    stream = io.StringIO()
    with InstallReporter(
        tmp_path / "install.log", verbose=False, stream=stream
    ) as reporter:
        with pytest.raises(RuntimeInstallError, match="3221225781"):
            windows_msys2._run_base_update_with_restarts(
                tmp_path / "msys64",
                cache=tmp_path / "cache",
                env={},
                reporter=reporter,
                runner=runner,
                mirror_optimizer=lambda _root: PacmanMirrorDecision(
                    None, None, False, (), ()
                ),
            )

    updates = [argv for argv in attempts if "-Syuu --noconfirm" in argv[4]]
    assert len(updates) == 1
    assert "outside a confirmed MSYS2 core-update restart" in stream.getvalue()


def test_base_update_restart_loop_is_bounded(tmp_path):
    attempts = []
    restart = (
        b":: To complete this update all MSYS2 processes including this "
        b"terminal will be closed. Confirm to proceed [Y/n]\n"
    )

    def runner(argv, *, env, check):
        attempts.append(argv)
        if windows_msys2._PRIVATE_GPG_SHUTDOWN_COMMAND in argv[4]:
            return completed(argv)
        return completed(argv, stdout=restart)

    with InstallReporter(
        tmp_path / "install.log", verbose=False, stream=io.StringIO()
    ) as reporter:
        with pytest.raises(RuntimeInstallError, match="repeatedly requested"):
            windows_msys2._run_base_update_with_restarts(
                tmp_path / "msys64",
                cache=tmp_path / "cache",
                env={},
                reporter=reporter,
                runner=runner,
                mirror_optimizer=lambda _root: PacmanMirrorDecision(
                    None, None, False, (), ()
                ),
            )

    updates = [argv for argv in attempts if "-Syuu --noconfirm" in argv[4]]
    assert len(updates) == windows_msys2._MAX_BASE_UPDATE_PASSES


def test_completed_package_cache_count_ignores_signatures_partials_and_links(tmp_path):
    (tmp_path / "python.pkg.tar.zst").write_bytes(b"package")
    (tmp_path / "python.pkg.tar.zst.sig").write_bytes(b"signature")
    (tmp_path / "tmux.pkg.tar.zst.part").write_bytes(b"partial")
    try:
        (tmp_path / "linked.pkg.tar.zst").symlink_to(
            tmp_path / "python.pkg.tar.zst"
        )
    except OSError:
        pass

    assert windows_msys2._completed_package_cache_count(tmp_path) == 1


def test_transaction_mirror_validation_reports_excluded_sources(
    tmp_path, monkeypatch
):
    decision = SimpleNamespace(
        active_servers=("https://repo.msys2.org/msys/$arch/",),
        failures=(("TUNA mirror", "package returned HTTP 403"),),
        changed=True,
        package_names=("tmux.pkg.tar.zst", "python.pkg.tar.zst"),
    )
    monkeypatch.setattr(
        windows_msys2,
        "_resolved_transaction_package_urls",
        lambda *_args, **_kwargs: (
            "https://repo.msys2.org/msys/x86_64/tmux.pkg.tar.zst",
            "https://repo.msys2.org/msys/x86_64/python.pkg.tar.zst",
        ),
    )
    monkeypatch.setattr(
        windows_msys2,
        "validate_transaction_package_mirrors",
        lambda *_args, **_kwargs: decision,
    )
    stream = io.StringIO()
    path = tmp_path / "install.log"

    with InstallReporter(path, verbose=False, stream=stream) as reporter:
        windows_msys2._validate_transaction_mirrors(
            tmp_path,
            env={},
            reporter=reporter,
            runner=None,
        )

    assert "Verified 2 transaction package samples across 1 sources" in (
        stream.getvalue())
    assert "excluded 1" in stream.getvalue()


def test_runtime_status_verifies_exact_package_identity(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    make_runtime(root, managed=True, shared=True)

    snapshot = windows_msys2.managed_runtime_status(
        version=VERSION,
        environ=environ,
        verify=True,
        probe=lambda argv, **_kwargs: completed(
            argv, stdout=_TEST_PACKAGE_INVENTORY),
    )

    assert snapshot["status"] == "ready"
    assert snapshot["current_app"] is True
    assert snapshot["content_identity"] == _TEST_CONTENT_ID
    assert snapshot["content_verification"] == "match"


def test_runtime_status_reports_package_drift_without_mutating(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    make_runtime(root, managed=True, shared=True)
    drifted = (
        b"python 3.12.99-1\npython-pip 26.1.2-1\ntmux 3.7.b-1\n"
    )

    snapshot = windows_msys2.managed_runtime_status(
        version=VERSION,
        environ=environ,
        verify=True,
        probe=lambda argv, **_kwargs: completed(argv, stdout=drifted),
    )

    assert snapshot["content_verification"] == "drift"
    marker = json.loads(
        (root / "railmux-base-content-v1.json").read_text(encoding="utf-8")
    )
    assert marker["content_id"] == _TEST_CONTENT_ID


def test_runtime_status_distinguishes_reusable_base_from_current_app(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    make_runtime(root, managed=True, shared=True)

    snapshot = windows_msys2.managed_runtime_status(
        version="0.4.0.dev99", environ=environ)

    assert snapshot["status"] == "base_ready"
    assert snapshot["current_app"] is False


def test_prune_retains_current_previous_and_process_proven_layers(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    make_runtime(root, managed=True, shared=True)
    old20 = add_marked_app(root, "0.4.0.dev20")
    old21 = add_marked_app(root, "0.4.0.dev21")
    old22 = add_marked_app(root, "0.4.0.dev22")
    in_use = b"/opt/railmux/apps/railmux-0.4.0.dev21/venv/bin/railmux\0"

    plan = windows_msys2.plan_managed_runtime_prune(
        version=VERSION,
        environ=environ,
        probe=lambda argv, **_kwargs: completed(argv, stdout=in_use),
    )

    assert plan.remove_apps == (old20,)
    assert old21.name in plan.retained_apps
    assert old22.name in plan.retained_apps
    assert f"railmux-{VERSION}" in plan.retained_apps


def test_prune_ignores_unmarked_directories_and_fails_closed_on_process_probe(
    tmp_path,
):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    make_runtime(root, managed=True, shared=True)
    add_marked_app(root, "0.4.0.dev22")
    unmarked = root / "opt" / "railmux" / "apps" / "railmux-0.4.0.dev19"
    unmarked.mkdir(parents=True)

    with pytest.raises(RuntimeInstallError, match="nothing was removed"):
        windows_msys2.plan_managed_runtime_prune(
            version=VERSION,
            environ=environ,
            probe=lambda argv, **_kwargs: completed(argv, returncode=1),
        )

    assert unmarked.is_dir()


def test_prune_refuses_linked_application_parent(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    make_runtime(root, managed=True, shared=True)
    external = tmp_path / "external-opt"
    (external / "railmux" / "apps").mkdir(parents=True)
    original_opt = root / "opt"
    original_backup = root / "opt-original"
    original_opt.rename(original_backup)
    original_opt.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeInstallError, match="nothing was removed"):
        windows_msys2.plan_managed_runtime_prune(
            version=VERSION,
            environ=environ,
            probe=lambda argv, **_kwargs: completed(argv, stdout=b""),
        )

    assert external.is_dir()


def test_apply_prune_replans_under_lock_before_deleting(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    make_runtime(root, managed=True, shared=True)
    old20 = add_marked_app(root, "0.4.0.dev20")
    add_marked_app(root, "0.4.0.dev22")

    def no_processes(argv, **_kwargs):
        return completed(argv, stdout=b"")

    plan = windows_msys2.plan_managed_runtime_prune(
        version=VERSION, environ=environ, probe=no_processes)

    @contextmanager
    def locked(_base):
        yield

    applied = windows_msys2.apply_managed_runtime_prune(
        plan,
        version=VERSION,
        environ=environ,
        probe=no_processes,
        lock_factory=locked,
    )

    assert applied.remove_apps == (old20,)
    assert not old20.exists()


def test_apply_prune_keeps_original_when_atomic_isolation_fails(
    tmp_path, monkeypatch,
):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    make_runtime(root, managed=True, shared=True)
    old20 = add_marked_app(root, "0.4.0.dev20")
    add_marked_app(root, "0.4.0.dev22")
    def no_processes(argv, **_kwargs):
        return completed(argv, stdout=b"")
    plan = windows_msys2.plan_managed_runtime_prune(
        version=VERSION, environ=environ, probe=no_processes)

    @contextmanager
    def locked(_base):
        yield

    monkeypatch.setattr(
        windows_msys2.os, "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("busy")),
    )
    with pytest.raises(RuntimeInstallError, match="left untouched"):
        windows_msys2.apply_managed_runtime_prune(
            plan,
            version=VERSION,
            environ=environ,
            probe=no_processes,
            lock_factory=locked,
        )

    assert old20.is_dir()


def test_apply_prune_reports_quarantined_partial_cleanup(tmp_path, monkeypatch):
    environ = {"LOCALAPPDATA": str(tmp_path)}
    root = managed_root(environ)
    assert root is not None
    make_runtime(root, managed=True, shared=True)
    old20 = add_marked_app(root, "0.4.0.dev20")
    add_marked_app(root, "0.4.0.dev22")
    def no_processes(argv, **_kwargs):
        return completed(argv, stdout=b"")
    plan = windows_msys2.plan_managed_runtime_prune(
        version=VERSION, environ=environ, probe=no_processes)

    @contextmanager
    def locked(_base):
        yield

    monkeypatch.setattr(
        windows_msys2.shutil, "rmtree",
        lambda *_args: (_ for _ in ()).throw(OSError("busy")),
    )
    with pytest.raises(RuntimeInstallError, match="isolated from use"):
        windows_msys2.apply_managed_runtime_prune(
            plan,
            version=VERSION,
            environ=environ,
            probe=no_processes,
            lock_factory=locked,
        )

    assert not old20.exists()
    assert len(list(old20.parent.glob(".railmux-prune-railmux-0.4.0.dev20-*"))) == 1
