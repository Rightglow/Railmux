# ccmgr — Claude Code session manager TUI

A terminal UI for navigating, resuming, and starting Claude Code sessions.
Lives in the left pane of a tmux window; the right pane shows the active Claude.

## Architecture

- **urwid TUI** — `Pile` sidebar (Projects / Sessions / Running), `Frame` + status bar
- **tmux** — each Claude session is a detached tmux session; ccmgr's right pane runs `tmux attach`
- **Polling** — `_refresh()` runs every ~1s, calling `list_projects` + `list_sessions` + tmux `list-sessions`

## Key constraint: polling rebuilds rows unconditionally

`_refresh()` rebuilds **every** row in all three panes on every tick — there is no dirty-check.
This means **do not store transient interaction state on row widget instances**.
Timers, click-tracking, drag state, etc. must live at class level (see `ClickableRow._last_click_*`)
or on the `App`/pane object. Row-identity keys (`session_id`, `tmux_name`, `encoded_name`) are
already available for this purpose.

Callbacks (`on_click`, `on_double_click`) and display data (`session`, `project`, `entry`) are
safe to pass as constructor arguments — they are re-derived from source data on every rebuild.

## Project structure

```
src/ccmgr/
├── cli.py               # argparse, SSH detection, auto-tmux wrapper
├── config.py             # TOML config (~/.config/ccmgr/config.toml)
├── discovery.py          # Scan ~/.claude/projects/*
├── favorites.py          # Star/pin persistence
├── launcher.py           # Build claude --resume / new-session commands
├── models.py             # Project, SessionMeta dataclasses
├── session_cache.py      # Cached session list per project
├── scroll_agent.py       # SSH scroll coalescing agent (detached tmux process)
├── scroll_manager.py     # Scroll coalescing lifecycle + crash recovery
├── tmux_ctl.py           # All tmux subprocess calls + scroll bindings
└── ui/
    ├── app.py            # Main App class — sidebar, focus, modals, lifecycle
    ├── _widgets.py       # ClickableRow (single/double/right click), focus helpers
    ├── projects_pane.py  # Project list with filter + select
    ├── sessions_pane.py  # Session list with status dots, star, preview
    ├── running_pane.py   # Running sessions list
    ├── modals.py         # All modal dialogs (help, info, rename, delete, context menu, path browser)
    ├── keymap.py         # Keybinding definitions + hint bar text
    └── statusbar.py      # Bottom status + help bar
```

## Mouse interactions

ClickableRow supports three mouse actions on every row:

| Action | Behavior |
|---|---|
| Left-click | `on_click` — preview / select (focus stays in current pane) |
| Double-click | `on_double_click` — open and steal focus |
| Right-click | `on_right_click` — context menu |

Double-click detection uses **class-level state** keyed by row identity (`click_key` parameter)
so that polling-driven row rebuilds between the two clicks do not reset the 500 ms window.

## Testing

```bash
python3 -m pytest -v    # 188 tests (as of 2026-07)
```

Tests use `unittest.mock` for tmux subprocess calls and urwid's `mouse_event` + `keypress`
for widget-level interaction tests. There is no integration test against a real tmux session.
