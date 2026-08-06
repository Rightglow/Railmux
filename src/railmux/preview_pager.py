"""Render a transcript completely before presenting its final page."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

from railmux import transcript


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    mouse = False
    if "--mouse" in arguments:
        arguments.remove("--mouse")
        mouse = True

    # TemporaryFile is unnamed (or unlinked immediately) on the managed POSIX
    # runtimes.  The formatted derivative is seekable for less +G, never
    # becomes a reusable transcript cache, and disappears when this process
    # exits.  Provider JSONL files remain read-only.
    with tempfile.TemporaryFile(
        mode="w+", encoding="utf-8", newline="",
    ) as rendered:
        try:
            with redirect_stdout(rendered):
                transcript.main(["transcript", *arguments])
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else 1
        rendered.flush()
        rendered.seek(0)
        less_argv = ["less", "-R", "+G"]
        if mouse:
            less_argv.extend(("--mouse", "--wheel-lines=3"))
        environ = os.environ.copy()
        environ.update({
            "LESSSECURE": "1",
            "LESSHISTFILE": "-",
            "LESSOPEN": "",
            "LESSCLOSE": "",
        })
        try:
            return subprocess.call(less_argv, stdin=rendered, env=environ)
        except OSError as exc:
            print(f"Could not start read-only pager: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
