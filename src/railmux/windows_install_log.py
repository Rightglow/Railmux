"""Bounded user output and complete UTF-8 logs for Windows runtime setup."""
from __future__ import annotations

import os
import re
import sys
import time
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
    ) -> None:
        self.path = path
        self.verbose = verbose
        self.stream = sys.stdout if stream is None else stream
        self._log: TextIO | None = None
        self._tail: deque[str] = deque(maxlen=20)
        self._reported_mirror_warning = False

    def __enter__(self) -> InstallReporter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.path.open("x", encoding="utf-8", newline="\n")
        self._write_log("Railmux Windows runtime installation\n")
        _prune_install_logs(self.path.parent)
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
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

    def command_started(self, label: str) -> None:
        self._tail.clear()
        self._reported_mirror_warning = False
        self._write_log(f"\n--- {label} ---\n")

    def command_output(self, output: bytes | str | None) -> None:
        if not output:
            return
        if isinstance(output, bytes):
            text = output.decode("utf-8", errors="replace")
        else:
            text = output
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        for raw_line in normalized.splitlines():
            line = _redact_line(raw_line)
            self._write_log(f"{line}\n")
            if line.strip():
                self._tail.append(line)
            if self.verbose:
                self._console(line)
            elif (
                not self._reported_mirror_warning
                and any(marker in line.lower() for marker in _MIRROR_WARNING_MARKERS)
            ):
                self.note(
                    "A package mirror failed; pacman is trying the next "
                    "approved source…"
                )
                self._reported_mirror_warning = True

    def command_failed(self, label: str, returncode: int) -> None:
        self.note(f"{label} failed with exit code {returncode}.")
        if self._tail:
            self.note("Last output:")
            for line in list(self._tail)[-8:]:
                if len(line) > _FAILURE_LINE_LIMIT:
                    line = f"{line[:_FAILURE_LINE_LIMIT]}… [truncated; see log]"
                self._console(f"        {line}")
        self.note(f"Full UTF-8 log: {self.path}")

    def finish(self) -> None:
        self._write_log("\nInstallation completed successfully.\n")
        self._console(f"Installation log: {self.path}")


def stream_process_output(
    process: object,
    reporter: InstallReporter,
) -> int:
    stdout: BinaryIO | None = getattr(process, "stdout", None)
    if stdout is not None:
        while True:
            chunk = stdout.readline()
            if not chunk:
                break
            reporter.command_output(chunk)
    return process.wait()
