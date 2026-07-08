# ccmgr

Terminal UI for Claude Code sessions — urwid left sidebar + tmux right pane.

## Non-obvious constraint

`_refresh()` (app.py) runs on a ~1s timer and rebuilds **every row widget** in all three
panes unconditionally — there is no dirty-check.  Therefore **never store transient
interaction state on row instances.**  Timers, click tracking, drag state, etc. must live
at class level and be keyed by row identity (`session_id`, `tmux_name`, `encoded_name`).

See `ClickableRow`'s class-level `_last_click_*` / `_pending_*` fields and the `click_key`
parameter for the canonical pattern.
