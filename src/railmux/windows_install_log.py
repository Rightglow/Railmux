"""Bounded user output and complete UTF-8 logs for Windows runtime setup."""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
import codecs
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import BinaryIO, TextIO


_LOG_KEEP_COUNT = 5
_FAILURE_LINE_LIMIT = 500
_LOG_NAME_RE = re.compile(
    r"install-[0-9A-Za-z.+-]+-[0-9]{8}T[0-9]{6}Z-[0-9]+\.log\Z"
)
_URL_CREDENTIAL_RE = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:access_token|password|token)=)[^&\s]+"
)
_MIRROR_WARNING_MARKERS = (
    "too many errors from ",
    "failed retrieving file ",
)
_HARD_MIRROR_ERROR_RE = re.compile(
    r"failed retrieving file .* from ([^ ]+) : .*error: (?:403|404)",
    re.IGNORECASE,
)
_NETWORK_ERROR_MARKERS = (
    "failed retrieving file ",
    "operation too slow",
    "could not resolve host",
    "connection timed out",
    "failed to synchronize all databases",
)
_EXTRACTION_PERCENT_RE = re.compile(r"(?:^|\s)([0-9]{1,3})%\s")
_PACKAGE_COUNT_RE = re.compile(r"^Packages \(([0-9]+)\)")
_PACKAGE_DOWNLOAD_RE = re.compile(r"^\s*([^ ]+) downloading\.\.\.\s*$")
_PACKAGE_CHANGE_RE = re.compile(r"^(?:installing|upgrading) ([^ ]+)\.\.\.\s*$")
_TOTAL_DOWNLOAD_RE = re.compile(r"^Total Download Size:\s+(.+?)\s*$")
_HEARTBEAT_SECONDS = 15.0


def _redact_line(line: str) -> str:
    line = _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", line)
    return _SECRET_QUERY_RE.sub(r"\1<redacted>", line)


def install_log_path(
    environ: Mapping[str, str],
    *,
    version: str,
    clock: Callable[[], float] = time.time,
) -> Path:
    local_app_data = environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise OSError("LOCALAPPDATA is unavailable")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
    return (
        Path(local_app_data)
        / "Railmux"
        / "logs"
        / f"install-{version}-{stamp}-{os.getpid()}.log"
    )


def _prune_install_logs(directory: Path, *, keep: int = _LOG_KEEP_COUNT) -> None:
    candidates: list[Path] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_file() and _LOG_NAME_RE.fullmatch(entry.name):
            candidates.append(entry)
    try:
        candidates.sort(
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
    except OSError:
        return
    for stale in candidates[max(keep, 1) :]:
        try:
            stale.unlink()
        except OSError:
            continue


class InstallReporter:
    """Show stable phases while retaining complete command output."""

    def __init__(
        self,
        path: Path,
        *,
        verbose: bool,
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self.verbose = verbose
        self.stream = sys.stdout if stream is None else stream
        self._log: TextIO | None = None
        self._tail: deque[str] = deque(maxlen=20)
        self._reported_mirror_warning = False
        self._clock = clock
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending_output = ""
        self._progress_kind: str | None = None
        self._progress_detail = "working"
        self._command_started_at = 0.0
        self._last_heartbeat = 0.0
        self._last_extraction_percent = -5
        self._package_total: int | None = None
        self._package_downloaded = 0
        self._package_changed = 0
        self._package_step = 1
        self._network_failure = False
        self._hard_failed_hosts: set[str] = set()

    def __enter__(self) -> InstallReporter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.path.open("x", encoding="utf-8", newline="\n")
        self._write_log("Railmux Windows runtime installation\n")
        _prune_install_logs(self.path.parent)
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.command_output_finished()
        if self._log is not None:
            self._log.close()
            self._log = None

    def _write_log(self, text: str) -> None:
        if self._log is None:
            return
        self._log.write(text)
        self._log.flush()

    def _console(self, text: str, *, flush: bool = True) -> None:
        try:
            print(text, file=self.stream, flush=flush)
        except UnicodeEncodeError:
            encoding = getattr(self.stream, "encoding", None) or "utf-8"
            safe = text.encode(encoding, errors="replace").decode(encoding)
            print(safe, file=self.stream, flush=flush)

    def phase(self, number: int, total: int, message: str) -> None:
        rendered = f"[{number}/{total}] {message}"
        self._console(rendered)
        self._write_log(f"\n{rendered}\n")

    def done(self, detail: str | None = None) -> None:
        rendered = "      done" + (f" · {detail}" if detail else "")
        self._console(rendered)
        self._write_log(f"{rendered}\n")

    def note(self, message: str) -> None:
        rendered = f"      {message}"
        self._console(rendered)
        self._write_log(f"{rendered}\n")

    def command_started(self, label: str, *, progress: str | None = None) -> None:
        self.command_output_finished()
        self._tail.clear()
        self._reported_mirror_warning = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending_output = ""
        self._progress_kind = progress
        self._progress_detail = label
        self._command_started_at = self._clock()
        self._last_heartbeat = self._command_started_at
        self._last_extraction_percent = -5
        self._package_total = None
        self._package_downloaded = 0
        self._package_changed = 0
        self._package_step = 1
        self._network_failure = False
        self._hard_failed_hosts.clear()
        self._write_log(f"\n--- {label} ---\n")

    def command_output(self, output: bytes | str | None) -> None:
        if not output:
            return
        if isinstance(output, bytes):
            text = self._decoder.decode(output)
        else:
            text = output
        self._pending_output += text
        parts = re.split(r"\r\n|\r|\n", self._pending_output)
        self._pending_output = parts.pop()
        for raw_line in parts:
            self._consume_output_line(raw_line)
        if len(self._pending_output) > 64 * 1024:
            self._consume_output_line(self._pending_output)
            self._pending_output = ""

    def command_output_finished(self) -> None:
        final = self._decoder.decode(b"", final=True)
        if final:
            self._pending_output += final
        if self._pending_output:
            self._consume_output_line(self._pending_output)
            self._pending_output = ""

    def _consume_output_line(self, raw_line: str) -> None:
        line = _redact_line(raw_line)
        self._write_log(f"{line}\n")
        if line.strip():
            self._tail.append(line)
        lowered = line.lower()
        if any(marker in lowered for marker in _NETWORK_ERROR_MARKERS):
            self._network_failure = True
        hard_failure = _HARD_MIRROR_ERROR_RE.search(line)
        if hard_failure is not None:
            self._hard_failed_hosts.add(hard_failure.group(1).lower())
        if self.verbose:
            self._console(line)
        elif (
            not self._reported_mirror_warning
            and any(marker in lowered for marker in _MIRROR_WARNING_MARKERS)
        ):
            self.note(
                "A package mirror failed; pacman is trying the next "
                "approved source…"
            )
            self._reported_mirror_warning = True
        if not self.verbose:
            self._update_progress(line)

    def _update_progress(self, line: str) -> None:
        if self._progress_kind == "extract":
            matches = _EXTRACTION_PERCENT_RE.findall(line)
            if not matches:
                return
            percent = min(int(matches[-1]), 100)
            self._progress_detail = f"extracting private runtime: {percent}%"
            if percent >= self._last_extraction_percent + 5 or percent == 100:
                self.note(f"Extracting private runtime: {percent}%")
                self._last_extraction_percent = percent
            return
        if self._progress_kind != "pacman":
            return
        package_count = _PACKAGE_COUNT_RE.match(line)
        if package_count is not None:
            self._package_total = int(package_count.group(1))
            self._package_step = max(1, self._package_total // 10)
            self._progress_detail = f"preparing {self._package_total} packages"
            return
        total_download = _TOTAL_DOWNLOAD_RE.match(line)
        if total_download is not None:
            count = self._package_total or "the required"
            self.note(
                f"Package transaction: {count} packages, "
                f"{total_download.group(1)} download."
            )
            return
        download = _PACKAGE_DOWNLOAD_RE.match(line)
        if download is not None:
            name = download.group(1)
            if self._package_total is None:
                self._progress_detail = f"refreshing repository {name}"
                self.note(f"Refreshing repository: {name}")
                return
            self._package_downloaded += 1
            total = self._package_total
            self._progress_detail = (
                f"downloading package {self._package_downloaded}/{total}: {name}"
            )
            if (
                self._package_downloaded == 1
                or self._package_downloaded == total
                or self._package_downloaded % self._package_step == 0
            ):
                self.note(
                    f"Downloading packages: {self._package_downloaded}/{total} "
                    f"({name})"
                )
            return
        change = _PACKAGE_CHANGE_RE.match(line)
        if change is not None and self._package_total is not None:
            self._package_changed += 1
            total = self._package_total
            name = change.group(1)
            self._progress_detail = (
                f"installing package {self._package_changed}/{total}: {name}"
            )
            if (
                self._package_changed == 1
                or self._package_changed == total
                or self._package_changed % self._package_step == 0
            ):
                self.note(
                    f"Installing packages: {self._package_changed}/{total} "
                    f"({name})"
                )
            return
        if line.strip() == "checking package integrity...":
            self._progress_detail = "verifying package signatures"
            self.note("Verifying downloaded package signatures…")

    @property
    def command_had_network_failure(self) -> bool:
        return self._network_failure

    @property
    def hard_failed_mirror_hosts(self) -> frozenset[str]:
        return frozenset(self._hard_failed_hosts)

    def heartbeat(self) -> None:
        now = self._clock()
        if now - self._last_heartbeat < _HEARTBEAT_SECONDS:
            return
        elapsed = max(0, int(now - self._command_started_at))
        self.note(f"Still working — {self._progress_detail} ({elapsed}s elapsed)")
        self._last_heartbeat = now

    def command_failed(self, label: str, returncode: int) -> None:
        self.command_output_finished()
        self.note(f"{label} failed with exit code {returncode}.")
        if self._tail:
            self.note("Last output:")
            for line in list(self._tail)[-8:]:
                if len(line) > _FAILURE_LINE_LIMIT:
                    line = f"{line[:_FAILURE_LINE_LIMIT]}… [truncated; see log]"
                self._console(f"        {line}")
        self.note(f"Full UTF-8 log: {self.path}")

    def command_succeeded(self) -> None:
        self.command_output_finished()
        if self._progress_kind == "extract" and self._last_extraction_percent < 100:
            self.note("Extracting private runtime: 100%")
            self._last_extraction_percent = 100

    def finish(self) -> None:
        self._write_log("\nInstallation completed successfully.\n")
        self._console(f"Installation log: {self.path}")


def stream_process_output(
    process: object,
    reporter: InstallReporter,
) -> int:
    stdout: BinaryIO | None = getattr(process, "stdout", None)
    if stdout is not None:
        chunks: queue.Queue[bytes | BaseException | object] = queue.Queue()
        finished = object()

        def read_output() -> None:
            try:
                read = getattr(stdout, "read1", stdout.read)
                while True:
                    chunk = read(4096)
                    if not chunk:
                        break
                    chunks.put(chunk)
            except BaseException as exc:  # forwarded to the owning thread
                chunks.put(exc)
            finally:
                chunks.put(finished)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        capture_error: BaseException | None = None
        while True:
            try:
                item = chunks.get(timeout=1.0)
            except queue.Empty:
                reporter.heartbeat()
                continue
            if item is finished:
                break
            if isinstance(item, BaseException):
                capture_error = item
                continue
            reporter.command_output(item)
            reporter.heartbeat()
        reader.join()
        reporter.command_output_finished()
        if capture_error is not None:
            raise OSError("could not capture installer output") from capture_error
    return process.wait()
