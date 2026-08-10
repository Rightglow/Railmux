"""Exercise the pinned MSYS2 archive with native Windows process loading."""
from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from railmux.windows_install_log import InstallReporter
from railmux.tmux_capabilities import parse_tmux_version
from railmux.windows_msys2 import (
    MSYS2_ARCHIVE_SHA256,
    Msys2Runtime,
    _bash_command,
    _extract_msys2_archive,
    _install_pinned_tmux,
    _prepare_pinned_tmux_package,
    _run_base_update_with_restarts,
    download_from_sources,
)
from railmux.windows_pacman import (
    PACMAN_MIRROR_SOURCES,
    PacmanMirrorDecision,
    _render_measured_pool,
    optimize_pacman_mirror,
    write_msys_only_pacman_config,
)


def _keep_forced_mirror(_root: Path) -> PacmanMirrorDecision:
    return PacmanMirrorDecision(None, None, False, (), ())


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
        write_msys_only_pacman_config(runtime.root)
        decision = optimize_pacman_mirror(runtime.root)
        forced_label = os.environ.get("RAILMUX_ARCHIVE_SMOKE_MIRROR", "").strip()
        if forced_label:
            matching = [
                server
                for label, server in PACMAN_MIRROR_SOURCES
                if label == forced_label
            ]
            if len(matching) != 1:
                raise SystemExit("unknown approved archive-smoke mirror label")
            mirrorlist = runtime.root / "etc" / "pacman.d" / "mirrorlist.msys"
            text = mirrorlist.read_text(encoding="utf-8")
            mirrorlist.write_text(
                _render_measured_pool(text, matching),
                encoding="utf-8",
                newline="\n",
            )
        selected = (
            forced_label
            or (
                decision.selected.label
                if decision.selected is not None
                else "official order"
            )
        )
        print(f"Measured production mirror selection: {selected}")
        mirror_optimizer = optimize_pacman_mirror
        if forced_label:
            mirror_optimizer = _keep_forced_mirror
        with InstallReporter(
            stage / "update.log", verbose=True, stream=sys.stdout
        ) as reporter:
            _run_base_update_with_restarts(
                runtime.root,
                cache=stage / "pacman-cache",
                env=runtime.environment(os.environ),
                reporter=reporter,
                runner=None,
                mirror_optimizer=mirror_optimizer,
            )
        package_cache = stage / "pacman-cache"
        tmux_package, tmux_source = _prepare_pinned_tmux_package(
            package_cache,
            downloader=None,
        )
        print(f"Verified pinned tmux artifact source: {tmux_source}")
        dependency = subprocess.run(
            _bash_command(
                runtime.root,
                'cache=$(cygpath -u "$1") && '
                "pacman --config /etc/railmux-pacman.conf "
                '--cachedir "$cache" -Syu --noconfirm --needed libevent',
                str(package_cache),
            ),
            env=runtime.environment(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
            check=False,
        )
        if dependency.returncode:
            output = dependency.stdout.decode("utf-8", errors="replace")[-2000:]
            raise SystemExit(f"tmux dependency installation failed:\n{output}")
        with InstallReporter(
            stage / "tmux.log", verbose=True, stream=sys.stdout
        ) as reporter:
            _install_pinned_tmux(
                runtime.root,
                tmux_package,
                cache=package_cache,
                env=runtime.environment(os.environ),
                reporter=reporter,
                runner=None,
            )
        result = subprocess.run(
            [
                str(runtime.bash),
                "--noprofile",
                "--norc",
                "-c",
                "cygpath --version >/dev/null && pacman --version >/dev/null "
                "&& tmux -V",
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
        tmux_output = result.stdout.decode("utf-8", errors="replace")
        if parse_tmux_version(tmux_output) != (3, 7):
            raise SystemExit(f"unexpected pinned tmux version: {tmux_output!r}")
    print("Native Windows archive extraction and executable loading passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
