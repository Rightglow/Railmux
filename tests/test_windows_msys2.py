from __future__ import annotations

import hashlib
import io
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from railmux import windows_msys2
from railmux.windows_install_log import InstallReporter
from railmux.windows_msys2 import (
    MSYS2_ARCHIVE_NAME,
    MSYS2_ARCHIVE_SHA256,
    MSYS2_ARCHIVE_SIZE,
    MSYS2_ARCHIVE_SOURCES,
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


VERSION = "0.4.0.dev9"


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
    probe = MagicMock(return_value=completed([], stdout=b"railmux 0.4.0.dev9\n"))

    assert probe_runtime(runtime, version=VERSION, environ=environ, probe=probe)
    assert not probe_runtime(runtime, version="0.4.0.dev8", environ=environ, probe=probe)


def test_runtime_probe_retries_one_transient_cold_start_failure(tmp_path):
    runtime = make_runtime(tmp_path / "msys", managed=False)
    probe = MagicMock(
        side_effect=[
            completed([], returncode=1),
            completed([], stdout=b"railmux 0.4.0.dev9\n"),
        ]
    )

    assert probe_runtime(runtime, version=VERSION, environ={}, probe=probe)
    assert probe.call_count == 2


def test_each_preview_version_uses_a_separate_runtime_generation(tmp_path):
    environ = {"LOCALAPPDATA": str(tmp_path)}

    dev7 = managed_root(environ, version="0.4.0.dev7")
    dev8 = managed_root(environ, version="0.4.0.dev8")

    assert dev7 != dev8
    assert dev7 is not None and dev7.parent == dev8.parent


def test_explicit_user_runtime_is_probed_but_never_requires_managed_marker(tmp_path):
    root = tmp_path / "用户-owned-msys"
    runtime = make_runtime(root, managed=False)
    probe = MagicMock(return_value=completed([], stdout=b"railmux 0.4.0.dev9\n"))
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
            config = output / "msys64" / "etc" / "pacman.conf"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                "[options]\nParallelDownloads = 5\n"
                "[mingw64]\nInclude = /etc/pacman.d/mirrorlist.mingw\n"
                "[msys]\nInclude = /etc/pacman.d/mirrorlist.msys\n",
                encoding="utf-8",
            )
        return completed(argv)

    def probe(argv, *, env, timeout):
        return completed(argv, stdout=b"railmux 0.4.0.dev9\n")

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
        mirror_optimizer=lambda _root: PacmanMirrorDecision(
            None, None, False, (), ()
        ),
    )

    assert runtime == Msys2Runtime(
        managed_root(environ, version=VERSION), managed=True
    )
    assert runtime.bash.is_file()
    assert json.loads(
        (runtime.root / "railmux-runtime.json").read_text(encoding="utf-8")
    )["railmux"] == VERSION
    joined = [" ".join(command) for command in commands]
    assert any("-Syu --noconfirm" in command for command in joined)
    assert any("--needed tmux python python-pip" in command for command in joined)
    assert any("python -m venv /opt/railmux/venv" in command for command in joined)
    assert any('railmux[ssh]==$1' in command for command in joined)
    logs = list((Path(environ["LOCALAPPDATA"]) / "Railmux" / "logs").glob("*.log"))
    assert len(logs) == 1
    log = logs[0].read_text(encoding="utf-8")
    assert "[1/7] Preparing verified MSYS2" in log
    assert "--- MSYS2 base update ---" in log
    assert "Installation completed successfully" in log
    config = runtime.root / "etc" / "railmux-pacman.conf"
    assert "[msys]" in config.read_text(encoding="utf-8")
    assert "[mingw64]" not in config.read_text(encoding="utf-8")
    assert not list((Path(environ["LOCALAPPDATA"]) / "Railmux" / "runtimes").glob(".install-*"))


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
