from __future__ import annotations

import io
import os
import sys

from railmux.windows_install_log import InstallReporter, install_log_path
from railmux.windows_msys2 import _run_checked


class Cp1252Stream(io.TextIOWrapper):
    def __init__(self):
        self.buffer_for_test = io.BytesIO()
        super().__init__(self.buffer_for_test, encoding="cp1252", errors="strict")

    def rendered(self):
        self.flush()
        return self.buffer_for_test.getvalue().decode("cp1252")


class TtyStream(io.StringIO):
    def isatty(self):
        return True


def test_compact_reporter_hides_raw_pacman_noise_but_keeps_utf8_log(tmp_path):
    stream = io.StringIO()
    path = tmp_path / "install-0.4.0.dev7-20260803T000000Z-1.log"

    with InstallReporter(path, verbose=False, stream=stream) as reporter:
        reporter.command_started("MSYS2 package installation")
        reporter.command_output(
            ":: Proceed with installation? [Y/n]\n"
            "warning: too many errors from mirror.msys2.org, skipping for "
            "the remainder of this transaction\n"
            "下载 tmux\n"
            "https://user:secret@example.invalid/a?token=secret\n"
        )

    rendered = stream.getvalue()
    assert "Proceed with installation" not in rendered
    assert "下载 tmux" not in rendered
    assert rendered.count("pacman is trying the next approved source") == 1
    log = path.read_text(encoding="utf-8")
    assert "Proceed with installation" in log
    assert "下载 tmux" in log
    assert "secret" not in log
    assert "https://<redacted>@example.invalid/a?token=<redacted>" in log


def test_verbose_reporter_streams_the_same_sanitized_output(tmp_path):
    stream = io.StringIO()
    path = tmp_path / "install-0.4.0.dev7-20260803T000000Z-2.log"

    with InstallReporter(path, verbose=True, stream=stream) as reporter:
        reporter.command_started("pacman")
        reporter.command_output(b"package \xe4\xb8\xad\xe6\x96\x87\rprogress\n")

    assert "package 中文\nprogress\n" in stream.getvalue()
    assert "package 中文\nprogress\n" in path.read_text(encoding="utf-8")


def test_verbose_reporter_never_fails_on_a_legacy_windows_console_codec(tmp_path):
    stream = Cp1252Stream()
    path = tmp_path / "install-0.4.0.dev7-20260803T000000Z-5.log"

    with InstallReporter(path, verbose=True, stream=stream) as reporter:
        reporter.phase(1, 1, "下载私有运行时")
        reporter.command_started("pacman")
        reporter.command_output("安装 中文…\n")

    assert "?" in stream.rendered()
    assert "安装 中文…" in path.read_text(encoding="utf-8")


def test_reporter_colors_only_the_interactive_console_not_utf8_log(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = TtyStream()
    path = tmp_path / "install-0.4.0.dev9-20260803T000000Z-9.log"

    with InstallReporter(path, verbose=False, stream=stream) as reporter:
        reporter.phase(1, 7, "Preparing runtime")
        reporter.note("Trying another mirror", level="warning")
        reporter.done("verified")
        reporter.finish()

    assert "\033[" in stream.getvalue()
    assert "\033[" not in path.read_text(encoding="utf-8")


def test_reporter_honors_no_color(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    stream = TtyStream()
    path = tmp_path / "install-0.4.0.dev9-20260803T000000Z-10.log"

    with InstallReporter(path, verbose=False, stream=stream) as reporter:
        reporter.phase(1, 7, "Preparing runtime")

    assert "\033[" not in stream.getvalue()


def test_reporter_failure_shows_a_bounded_tail_and_log_path(tmp_path):
    stream = io.StringIO()
    path = tmp_path / "install-0.4.0.dev7-20260803T000000Z-3.log"

    with InstallReporter(path, verbose=False, stream=stream) as reporter:
        reporter.command_started("pacman")
        reporter.command_output("\n".join(f"line {number}" for number in range(30)))
        reporter.command_failed("pacman", 1)

    rendered = stream.getvalue()
    assert "line 21" not in rendered
    assert "line 22" in rendered
    assert "line 29" in rendered
    assert str(path) in rendered


def test_failure_truncates_long_console_line_but_not_utf8_log(tmp_path):
    path = tmp_path / "install-0.4.0.dev7-20260803T000000Z-6.log"
    stream = io.StringIO()
    long_line = "failure " + "x" * 2_000

    with InstallReporter(path, verbose=False, stream=stream) as reporter:
        reporter.command_started("package installation")
        reporter.command_output(long_line)
        reporter.command_failed("package installation", 1)

    rendered = stream.getvalue()
    assert "[truncated; see log]" in rendered
    assert len(max(rendered.splitlines(), key=len)) < 600
    assert long_line in path.read_text(encoding="utf-8")


def test_reporter_retains_only_five_owned_logs(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    for number in range(6):
        path = logs / f"install-0.4.0.dev7-20260803T00000{number}Z-{number}.log"
        path.write_text(str(number), encoding="utf-8")
        os.utime(path, ns=(number + 1, number + 1))
    unrelated = logs / "notes.log"
    unrelated.write_text("keep", encoding="utf-8")
    current = logs / "install-0.4.0.dev7-20260803T000006Z-99.log"

    with InstallReporter(current, verbose=False, stream=io.StringIO()):
        pass

    owned = sorted(logs.glob("install-*.log"))
    assert len(owned) == 5
    assert current in owned
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_log_path_is_private_versioned_and_utc(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getpid", lambda: 42)

    path = install_log_path(
        {"LOCALAPPDATA": str(tmp_path)},
        version="0.4.0.dev7",
        clock=lambda: 0,
    )

    assert path == (
        tmp_path
        / "Railmux"
        / "logs"
        / "install-0.4.0.dev7-19700101T000000Z-42.log"
    )


def test_default_command_runner_uses_no_stdin_and_captures_utf8(tmp_path):
    stream = io.StringIO()
    path = tmp_path / "install-0.4.0.dev7-20260803T000000Z-4.log"

    with InstallReporter(path, verbose=False, stream=stream) as reporter:
        _run_checked(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stdout.buffer.write('captured 中文\\n'.encode('utf-8'))"
                ),
            ],
            env=os.environ,
            reporter=reporter,
            label="fixture",
        )

    assert "captured 中文" not in stream.getvalue()
    assert "captured 中文" in path.read_text(encoding="utf-8")


def test_compact_reporter_surfaces_extraction_and_package_progress(tmp_path):
    stream = io.StringIO()
    path = tmp_path / "install-0.4.0.dev8-20260803T000000Z-7.log"

    with InstallReporter(path, verbose=False, stream=stream) as reporter:
        reporter.command_started("extract", progress="extract")
        reporter.command_output(b"  0% 100 - first\r  7% 200 - second\r")
        reporter.command_output(b" 42% 300 - third\r100% 400 - final\n")
        reporter.command_started("pacman", progress="pacman")
        reporter.command_output(
            "Packages (48) a b c\n"
            "Total Download Size: 57.40 MiB\n"
            " python-3.12.pkg.tar.zst downloading...\n"
            "checking package integrity...\n"
            "installing python...\n"
        )

    rendered = stream.getvalue()
    assert "Extracting private runtime: 0%" in rendered
    assert "Extracting private runtime: 42%" in rendered
    assert "Extracting private runtime: 100%" in rendered
    assert "Package transaction: 48 packages, 57.40 MiB download." in rendered
    assert "Downloading packages: 1/48" in rendered
    assert "Verifying downloaded package signatures" in rendered
    assert "Installing packages: 1/48" in rendered


def test_reporter_heartbeat_and_network_failure_classification(tmp_path):
    stream = io.StringIO()
    now = [0.0]
    path = tmp_path / "install-0.4.0.dev8-20260803T000000Z-8.log"

    with InstallReporter(
        path,
        verbose=False,
        stream=stream,
        clock=lambda: now[0],
    ) as reporter:
        reporter.command_started("pacman", progress="pacman")
        reporter.command_output("Packages (2) python tmux\n python downloading...\n")
        now[0] = 16.0
        reporter.heartbeat()
        reporter.command_output(
            "error: failed retrieving file 'python.pkg.tar.zst' from "
            "bad.example : The requested URL returned error: 403\n"
            "error: failed retrieving file 'tmux.pkg.tar.zst' from "
            "slow.example : Operation too slow. Less than 1 bytes/sec\n"
        )

        assert reporter.command_had_network_failure
        assert reporter.hard_failed_mirror_hosts == {"bad.example"}

    assert "Still working" in stream.getvalue()
    assert "downloading package 1/2" in stream.getvalue()
