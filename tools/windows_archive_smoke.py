"""Exercise the pinned MSYS2 archive with native Windows process loading."""
from __future__ import annotations

import io
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from railmux.windows_install_log import InstallReporter
from railmux.windows_msys2 import (
    MSYS2_ARCHIVE_SHA256,
    Msys2Runtime,
    _extract_msys2_archive,
    download_from_sources,
)


def main() -> int:
    if os.name != "nt":
        raise SystemExit("the Windows archive smoke test requires native Windows")
    with tempfile.TemporaryDirectory(prefix="railmux-archive-smoke-") as raw:
        stage = Path(raw)
        archive = stage / "msys2-base.tar.xz"
        download_from_sources(archive, MSYS2_ARCHIVE_SHA256)
        extraction = stage / "extract"
        extraction.mkdir()
        with InstallReporter(
            stage / "install.log", verbose=False, stream=io.StringIO()
        ) as reporter:
            _extract_msys2_archive(archive, extraction, reporter=reporter)
        runtime = Msys2Runtime(extraction / "msys64", managed=False)
        bashbug = runtime.root / "usr" / "bin" / "bashbug"
        readonly = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
        if getattr(bashbug.stat(), "st_file_attributes", 0) & readonly:
            raise SystemExit("POSIX archive mode became an NTFS read-only attribute")
        with bashbug.open("r+b") as stream:
            first = stream.read(1)
            stream.seek(0)
            stream.write(first)
        result = subprocess.run(
            [
                str(runtime.bash),
                "--noprofile",
                "--norc",
                "-c",
                "cygpath --version >/dev/null && pacman --version >/dev/null",
            ],
            env=runtime.environment(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        if result.returncode:
            output = result.stdout.decode("utf-8", errors="replace")[-2000:]
            raise SystemExit(
                f"extracted MSYS2 executables failed ({result.returncode}):\n{output}"
            )
    print("Native Windows archive extraction and executable loading passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
