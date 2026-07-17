# Background session-index evidence

Phase 5 moves Codex's date-tree walk, file stat calls, and changed-rollout
parsing into one background worker. The Urwid thread reads only the last
published immutable generation. Claude retains its bounded selected-project
cache; it does not perform a whole-history tree walk on normal ticks.

## Reproduction

```bash
PYTHONPATH=src python benchmarks/benchmark_background_index.py \
  --files 500 --large-files 3 --large-messages 10000 \
  --stat-delay-ms 0.1 --open-delay-ms 0.2
```

The run below used Python 3.12 on Linux on 2026-07-17. Its dataset lived in a
local temporary directory. The delays are injected with `time.sleep`; they are
useful for separating UI-query cost from scan cost but are **not NFS evidence**.

| Scenario | Scan / publication | UI snapshot query | stat / parse work |
|---|---:|---:|---:|
| Cold, 500 files and 30,000 large-file messages | 625.6 / 626.1 ms | 0.281 ms | 500 / 500 |
| Unchanged tree | 88.0 / 88.5 ms | 0.253 ms | 500 / 0 |
| One appended active rollout | 89.5 / 89.9 ms | 0.238 ms | 500 / 1 |
| Injected transient parse failure | 7.0 / 7.5 ms | 0.236 ms | 500 / 1 |
| Injected transient permission failure | 64.7 / 65.2 ms | 0.232 ms | 500 / 1 |

The synchronous work has not disappeared: unchanged scans still stat all 500
paths, and a cold scan still parses every rollout. The improvement is that this
work is isolated from the UI thread. Transient-failure runs published a
generation containing cached live metadata. The first injected failure exposed
the bounded warning `Codex session scan skipped 1 transient file error(s)`;
the immediately following equivalent permission warning was deduplicated.

Publication delay here is measured from a forced request until the generation
is observable. Terminal paint, real NFS behavior, macOS filesystem behavior,
and provider write races require separate runtime evidence; this synthetic run
does not claim them.
