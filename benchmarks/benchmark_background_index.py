#!/usr/bin/env python3
"""Synthetic local benchmark for Phase 5 background session indexing.

This is not an NFS benchmark.  ``--stat-delay-ms`` and ``--open-delay-ms``
inject local delays to approximate metadata/content latency while keeping the
dataset and work counters reproducible.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import railmux.codex_index as codex_module
from railmux.background_index import BackgroundCodexIndex
from railmux.codex_index import CodexIndex, SCAN_ERROR


def _record(session_id: str, cwd: Path, messages: int) -> str:
    rows = [{
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": str(cwd), "source": "cli"},
    }]
    for number in range(messages):
        role = "user" if number % 2 == 0 else "assistant"
        rows.append({
            "type": "response_item",
            "payload": {
                "type": "message", "role": role,
                "content": [{"type": "input_text", "text": f"message {number}"}],
            },
        })
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def _measure_query(index: BackgroundCodexIndex, iterations: int = 1000) -> float:
    started = time.perf_counter()
    for _ in range(iterations):
        index.all_cwds(refresh=False)
    return (time.perf_counter() - started) * 1000.0 / iterations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=500)
    parser.add_argument("--large-files", type=int, default=3)
    parser.add_argument("--large-messages", type=int, default=10000)
    parser.add_argument("--stat-delay-ms", type=float, default=0.1)
    parser.add_argument("--open-delay-ms", type=float, default=0.2)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="railmux-index-bench-") as temp:
        root = Path(temp)
        sessions = root / "sessions" / "2026" / "07" / "17"
        sessions.mkdir(parents=True)
        project = root / "project"
        project.mkdir()
        paths = []
        for number in range(args.files):
            session_id = str(uuid.UUID(int=number + 1))
            path = sessions / f"rollout-{number:06d}-{session_id}.jsonl"
            messages = args.large_messages if number < args.large_files else 2
            path.write_text(_record(session_id, project, messages), encoding="utf-8")
            paths.append(path)

        scanner = CodexIndex(root)
        background = BackgroundCodexIndex(
            root, scanner=scanner, min_interval_s=0)
        original_stat = Path.stat
        original_open = Path.open

        def slow_stat(path, *call_args, **call_kwargs):
            if args.stat_delay_ms:
                time.sleep(args.stat_delay_ms / 1000.0)
            return original_stat(path, *call_args, **call_kwargs)

        def slow_open(path, *call_args, **call_kwargs):
            if args.open_delay_ms and str(path).endswith(".jsonl"):
                time.sleep(args.open_delay_ms / 1000.0)
            return original_open(path, *call_args, **call_kwargs)

        def generation(label: str, expected: int) -> dict:
            requested_at = time.perf_counter()
            background.refresh(force=True)
            if not background.wait_for_generation(expected, 120.0):
                raise RuntimeError(f"{label} generation timed out")
            published_at = time.perf_counter()
            snapshot = background.current_snapshot()
            report = snapshot.report
            return {
                "generation": snapshot.generation,
                "publication_delay_ms": (published_at - requested_at) * 1000.0,
                "scan_duration_ms": report.duration_s * 1000.0 if report else None,
                "paths_seen": report.paths_seen if report else None,
                "stat_count": report.stat_count if report else None,
                "parse_count": report.parse_count if report else None,
                "ui_query_ms": _measure_query(background),
            }

        try:
            with patch.object(Path, "stat", slow_stat), patch.object(Path, "open", slow_open):
                cold = generation("cold", 1)
                unchanged = generation("unchanged", 2)
                with original_open(paths[-1], "a", encoding="utf-8") as stream:
                    stream.write(json.dumps({
                        "type": "response_item",
                        "payload": {
                            "type": "message", "role": "user",
                            "content": [{
                                "type": "input_text", "text": "active append",
                            }],
                        },
                    }) + "\n")
                appended = generation("append", 3)

            real_scan = codex_module._scan_codex_session
            failing = paths[0]
            failed_once = False

            def transient_scan(path):
                nonlocal failed_once
                if path == failing and not failed_once:
                    failed_once = True
                    return SCAN_ERROR
                return real_scan(path)

            failing.touch()
            before = background.generation
            with patch.object(codex_module, "_scan_codex_session", transient_scan):
                transient = generation("transient", before + 1)
            transient["warning"] = background.take_warning()

            permission_path = paths[1]
            permission_path.touch()
            permission_failed = False

            def permission_stat(path, *call_args, **call_kwargs):
                nonlocal permission_failed
                if path == permission_path and not permission_failed:
                    permission_failed = True
                    raise PermissionError("injected benchmark failure")
                return original_stat(path, *call_args, **call_kwargs)

            before = background.generation
            with patch.object(Path, "stat", permission_stat):
                permission = generation("permission", before + 1)
            permission["warning"] = background.take_warning()

            output = {
                "environment": {
                    "filesystem": "local temporary directory",
                    "delay_model": "injected local stat/open sleeps; not NFS evidence",
                    "files": args.files,
                    "large_files": args.large_files,
                    "large_messages_each": args.large_messages,
                    "stat_delay_ms": args.stat_delay_ms,
                    "open_delay_ms": args.open_delay_ms,
                },
                "cold": cold,
                "unchanged": unchanged,
                "one_appended_rollout": appended,
                "transient_parse_failure": transient,
                "transient_permission_failure": permission,
            }
            print(json.dumps(output, indent=2, sort_keys=True))
        finally:
            background.close()


if __name__ == "__main__":
    main()
