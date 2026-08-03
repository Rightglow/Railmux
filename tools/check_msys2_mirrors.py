#!/usr/bin/env python3
"""Advisory HTTPS health check for the pinned MSYS2 archive transports."""

from __future__ import annotations

import concurrent.futures
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from railmux.windows_msys2 import (  # noqa: E402
    MSYS2_ARCHIVE_SIZE,
    MSYS2_ARCHIVE_SOURCES,
)


def check_source(source: tuple[str, str]) -> str:
    label, url = source
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "Railmux-MSYS2-mirror-health/1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        status = getattr(response, "status", 200)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        raw_length = response.headers.get("Content-Length")
        if raw_length is None or int(raw_length) != MSYS2_ARCHIVE_SIZE:
            raise RuntimeError(
                f"unexpected Content-Length {raw_length!r}; "
                f"expected {MSYS2_ARCHIVE_SIZE}"
            )
        final_url = response.geturl()
        if not final_url.startswith("https://"):
            raise RuntimeError(f"redirected outside HTTPS: {final_url}")
    return f"{label}: OK ({MSYS2_ARCHIVE_SIZE} bytes)"


def main() -> int:
    failures = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(MSYS2_ARCHIVE_SOURCES)
    ) as pool:
        futures = {
            pool.submit(check_source, source): source[0]
            for source in MSYS2_ARCHIVE_SOURCES
        }
        for future in concurrent.futures.as_completed(futures):
            label = futures[future]
            try:
                print(future.result())
            except Exception as exc:
                failures.append(label)
                print(f"{label}: FAILED ({exc})", file=sys.stderr)
    if failures:
        print(
            "Unavailable or changed MSYS2 sources: " + ", ".join(sorted(failures)),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
