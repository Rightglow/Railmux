#!/usr/bin/env python3
"""Deterministic wire-budget benchmark for the Railmux SSH display.

This intentionally avoids wall-clock thresholds: shared CI runners are noisy.
The check protects the properties users feel directly—one changed row stays a
small patch, keyframes remain compressed, and protocol limits stay bounded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Sequence

from railmux.fast_display_protocol import (
    MAX_HISTORY_LINES,
    MAX_PREFETCH_HISTORY_LINES,
    ScreenUpdate,
    ServerMessageDecoder,
    UpdateKind,
    encode_update,
)


@dataclass(frozen=True)
class BenchmarkResult:
    width: int
    height: int
    raw_screen_bytes: int
    keyframe_wire_bytes: int
    one_row_patch_wire_bytes: int
    keyframe_compression_ratio: float
    patch_to_keyframe_ratio: float
    default_prefetch_lines: int
    maximum_history_lines: int
    checks_passed: bool


def _row(index: int, width: int, generation: int = 0) -> bytes:
    seed = hashlib.sha256(f"{generation}:{index}".encode()).hexdigest()
    label = f"{index:03d} \x1b[38;5;70m{seed}\x1b[0m "
    return (label + seed * 3).encode()[:width]


def collect_benchmark(width: int = 120, height: int = 40) -> BenchmarkResult:
    rows = tuple((index, _row(index, width)) for index in range(height))
    keyframe = ScreenUpdate(
        UpdateKind.KEYFRAME,
        1,
        width,
        height,
        0,
        0,
        True,
        rows,
    )
    patch = ScreenUpdate(
        UpdateKind.PATCH,
        2,
        width,
        height,
        0,
        height - 1,
        True,
        ((height - 1, _row(height - 1, width, generation=1)),),
    )
    keyframe_wire = encode_update(keyframe)
    patch_wire = encode_update(patch)
    decoded = ServerMessageDecoder().feed(keyframe_wire + patch_wire)
    raw_bytes = sum(len(row) for _index, row in rows)
    checks = (
        decoded == [keyframe, patch]
        and len(keyframe_wire) < raw_bytes
        and len(patch_wire) <= 512
        and len(patch_wire) * 4 < len(keyframe_wire)
        and MAX_PREFETCH_HISTORY_LINES <= 300
        and MAX_HISTORY_LINES <= 20000
    )
    return BenchmarkResult(
        width=width,
        height=height,
        raw_screen_bytes=raw_bytes,
        keyframe_wire_bytes=len(keyframe_wire),
        one_row_patch_wire_bytes=len(patch_wire),
        keyframe_compression_ratio=round(len(keyframe_wire) / raw_bytes, 4),
        patch_to_keyframe_ratio=round(len(patch_wire) / len(keyframe_wire), 4),
        default_prefetch_lines=MAX_PREFETCH_HISTORY_LINES,
        maximum_history_lines=MAX_HISTORY_LINES,
        checks_passed=checks,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = collect_benchmark()
    if args.json:
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
    else:
        print(
            "Railmux SSH display wire budget: "
            f"keyframe={result.keyframe_wire_bytes} B "
            f"({result.keyframe_compression_ratio:.1%} of raw), "
            f"one-row patch={result.one_row_patch_wire_bytes} B "
            f"({result.patch_to_keyframe_ratio:.1%} of keyframe)"
        )
    return 1 if args.check and not result.checks_passed else 0


if __name__ == "__main__":
    raise SystemExit(main())
