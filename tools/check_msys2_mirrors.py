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
    offset = 1024 * 1024
    request = urllib.request.Request(
        url,
        headers={
            "Accept-Encoding": "identity",
            "Range": f"bytes={offset}-{offset}",
            "User-Agent": "Railmux-MSYS2-mirror-health/1",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        status = getattr(response, "status", 200)
        if status != 206:
            raise RuntimeError(f"HTTP {status}")
        expected_range = f"bytes {offset}-{offset}/{MSYS2_ARCHIVE_SIZE}"
        actual_range = response.headers.get("Content-Range")
        if actual_range != expected_range:
            raise RuntimeError(
                f"unexpected Content-Range {actual_range!r}; "
                f"expected {expected_range!r}"
            )
        raw_length = response.headers.get("Content-Length")
        if raw_length is not None and int(raw_length) != 1:
            raise RuntimeError(f"unexpected Content-Length {raw_length!r}")
        final_url = response.geturl()
        if not final_url.startswith("https://"):
            raise RuntimeError(f"redirected outside HTTPS: {final_url}")
        if response.read(2) == b"":
            raise RuntimeError("range response was empty")
    return f"{label}: OK (HTTPS range resume supported)"


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
