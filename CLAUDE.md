# ccmgr

Terminal UI for Claude Code sessions — urwid left sidebar + tmux right pane.

## Non-obvious constraint

`_refresh()` (app.py) runs on a ~1s timer and rebuilds **every row widget** in all three
panes unconditionally — there is no dirty-check.  Therefore **never store transient
interaction state on row instances.**  Timers, click tracking, drag state, etc. must live
at class level and be keyed by row identity (`session_id`, `tmux_name`, `encoded_name`).

See `ClickableRow`'s class-level `_last_click_*` / `_pending_*` fields and the `click_key`
parameter for the canonical pattern.

## Soft restart contracts

Soft quit (`q → s`) preserves detached `cc-*` tmux sessions across process restarts.
These are the invariants that must hold for it to keep working:

### tmux session identity
- Session names: `cc-{App._safe_name(session_id, 16)}` — 16 alnum chars after `cc-`.
- If `_safe_name` or `_claude_session_name` changes, `_resolve_truncated_id` must be
  updated in lockstep so truncated names can still be mapped back to full UUIDs.

### _running dict
- Keys are **full session_id UUIDs** (36 chars), never truncated. `_discover_orphans`
  must resolve truncated tmux names → full UUID before inserting.
- `_running` is the sole registry of live sessions. Both the sessions pane
  (`running_ids`) and the running pane (`_update_running_pane`) read from it.

### Orphan discovery
- Relies on `tmux list-sessions -F '#{session_name}\t#{pane_current_path}'` and
  `cc-*` name prefix.  Any change to the tmux session naming scheme must update
  `_discover_orphans`.
- Project matching: `pane_current_path` must equal `Project.real_path` from
  `list_projects()`.  If project path resolution changes, update the matching.
- `_update_running_pane()` is called immediately after `_discover_orphans()` in
  `__init__` so discovered sessions are visible before the first poll tick.

### State file
- Path: `/tmp/ccmgr-state-{os.getuid()}.json`.
- Written by `_save_state()` every time the quit dialog opens.
- Consumed (deleted) by `_load_state()` on next startup.
- Current keys: `"project"` (encoded_name), `"session"` (session_id).
- New keys must have sensible defaults for old state files (missing key → skip, don't crash).

### Teardown
- `_teardown_tmux` is called exactly once, from `run()`'s `finally` block.
- `_soft_quit_flag` gates session killing.  Must be checked **before** the
  `kill_session` loop — don't add new cleanup code above the flag check that
  destroys user state.

### Status caching (session_cache.py)
- `SessionMeta.status == "busy"` is time-dependent (tool_use → blocked after 3 s).
  The cache must re-scan when it hits a "busy" entry whose mtime is >3 s old.
  Don't add caching that skips this re-check.
