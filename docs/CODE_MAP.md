# Railmux code map

This is a navigation index for maintainers and coding agents. It is not a
behavioral specification. Follow the linked architecture and support-matrix
entries for authority; update this map when a module or test responsibility
moves, but do not copy invariants, defaults, timeouts, or release evidence here.

## Start from the symptom

| Symptom or task | First production entry points | Focused tests | Authority |
| --- | --- | --- | --- |
| Launch, detach, hard/soft quit | `entrypoint.main`, `cli.main`, `tmux_server`, `tmux_health` | `test_cli.py`, `test_tmux_server.py`, `test_app_quit.py`, focused real-tmux cases | Architecture: dedicated server, restart state |
| Projects/Sessions/Running rows | `ui/app.py`, `ui/projects_pane.py`, `ui/sessions_pane.py`, `ui/running_pane.py` | matching pane test plus `test_running_sort.py` | Architecture: disposable sidebar rows |
| Provider discovery or history identity | Claude: `discovery.py`, `session_cache.py`; Codex: `background_index.py`, `codex_index.py` | matching discovery/index test, then lineage/orphan tests when identity changes | Architecture: provider modes and immutable index generations |
| Launch, resume, delete, orphan, or lease | `ui/app.App`, `orphan_marker.py`, `session_lease.py`, `legacy_sessions.py` | `test_app_running_recovery.py`, `test_app_codex_placeholder.py`, `test_session_lease.py`, `test_codex_lineage.py`, `test_codex_delete.py` | Architecture: restart, identity, and liveness sections |
| Agent layout, focus, Target, compact mode | `ui/workspace.py`, `ui/app.App`, `tmux_ctl.py`, `tmux_binding_manager.py` | `test_agent_workspace.py`, `test_layout_profile.py`, `test_focus_highlight.py`, `test_fullscreen_binding.py` | Architecture: workspace, target, bindings, size, focus |
| Explicit transcript Preview | `ui/app.App`, `transcript.py`, `preview_pager.py` | `test_transcript.py`, `test_preview_pager.py`, Preview cases in UI tests | Architecture: display transports; interaction-preservation rules in `AGENTS.md` |
| Direct/local live scrolling | `mouse_manager.py`, `scroll_manager.py`, `tmux_ctl.py` | `test_scroll_manager.py`, focused tmux integration | Architecture: display transports and global bindings |
| `railmux ssh` screen fidelity | `fast_display_protocol.py`, `fast_display_client.py`, `fast_display_server.py` | `test_fast_display_protocol.py`, `test_fast_display_terminal.py`, `test_fast_display_server.py` | Architecture: SSH terminal model and protocol; support S IDs |
| `railmux ssh` history/rewind | `fast_display_history.py`, client/server history handlers, `ui/app.App._sync_codex_rewind_scrollback` | `test_fast_display_history.py`, `test_transcript.py`, `test_codex_lineage.py`, focused real-tmux cases | Architecture: SSH history overlay and generation authority |
| `railmux ssh` selection/path/clipboard | `fast_display_input.py`, client/server request handlers, `local_open.py`, `local_clipboard.py` | `test_fast_display_selection.py`, local-open/clipboard tests | Architecture: SSH input ownership; support S IDs |
| SSH compatibility, install, config, doctor | `ssh_preflight.py`, `remote_config.py`, `ssh_doctor.py`, client connection setup | `test_fast_display_bootstrap.py`, `test_remote_config.py`, `test_ssh_doctor.py` | Architecture: SSH compatibility handshake; support S IDs |
| WSL clipboard, browser, or terminal | `local_clipboard.py`, `local_open.py` | `test_local_clipboard.py`, `test_local_open.py` | Support matrix platform IDs |
| Native Windows dispatch | `entrypoint.py`, `windows_bootstrap.py`, `provider_paths.py` | `test_windows_bootstrap.py`, `test_provider_paths.py`, wrapper contract tests | Architecture: managed Windows ownership; support W/P IDs |
| Managed MSYS2 install/update/prune | `windows_msys2.py`, `windows_pacman.py`, `windows_install_log.py`, `windows_paths.py` | matching Windows module test; native archive/MSYS2 CI before a support claim | Windows runtime document and parity ledger |
| Windows cross-terminal attach | `cli._run_tmux_client_with_watchdog`, `windows_attach_relay.py`, `windows_tmux_lifecycle.py` | relay/lifecycle tests plus real Windows terminal validation | Architecture: managed Windows attach relay |
| Version, release, app-layer transition | `release_version.py`, `windows_ui_transition.py`, release workflow | release/version/transition/contract tests | `RELEASING.md` and Windows parity ledger |

Use symbols, not line numbers, in issues and documentation. Line numbers move
too easily; file and symbol names also make stale map entries detectable.

## Runtime entry chains

### POSIX and WSL local launch

```text
railmux.entrypoint:main
  -> railmux.cli:main
  -> dedicated tmux server / outer controller pane
  -> railmux.ui.app:main
  -> App
```

WSL follows this POSIX chain. Its narrow host-integration branches live in
`local_open.py` and `local_clipboard.py`; it is not the native Windows adapter.

### Native Windows launch

```text
railmux.entrypoint:main
  -> railmux.windows_bootstrap:main
  -> validated private managed MSYS2 runtime and versioned app layer
  -> the same railmux.cli / App implementation used on POSIX
```

`entrypoint.py` must decide native Windows before importing the POSIX CLI.
Native Python owns discovery, consent, installation/update, path translation,
and handoff only. Do not add a second UI, provider host, or session model there.

### Full-window SSH launch

```text
railmux.cli:main (ssh subcommand)
  -> fast_display_client
  -> ssh_preflight compatibility hello
  -> remote railmux remote-server
  -> private tmux attach client
  -> fast_display_server frames / input / history messages
```

The local client owns terminal presentation and bounded per-pane history. The
remote helper owns tmux identity checks and capture. Neither side gains provider
history mutation authority from the display protocol.

## State ownership

| State | Owner | Do not substitute |
| --- | --- | --- |
| User configuration | atomic `config.toml` helpers | row widgets or cached projections |
| Active provider and sidebar view | `App` mode view state | provider-specific booleans |
| Agent slots, layout, Target | `AgentWorkspace` plus App presentation coordination | physical pane position alone |
| Live provider/tmux identity | Running/recovery state in `App` and immutable tmux/provider facts | human-readable names or transcript paths |
| Codex discovery generations | `BackgroundCodexIndex` snapshots | synchronous UI tree walking |
| Claude discovery cache | `session_cache.py` / discovery results | Codex lineage assumptions |
| Restart restoration | `restart_state.py` exact-owner and portable authorities | one ownerless state file |
| Pre-launch recovery | `orphan_marker.py` plus exact correlation | cwd/name heuristics |
| Cross-process/session ownership | `session_lease.py` | UI status or stale owner text |
| Swap transaction | `display_transport.AgentDisplayTransport` | current pane geometry alone |
| Explicit Preview | transcript renderer and Railmux-owned pane | live pane scrollback |
| Direct live scrolling | provider/tmux-native path | transcript availability |
| SSH screen model | client terminal surface fed by server frames | Preview rendering |
| SSH history cache/view | `fast_display_history.LocalHistoryView` | remote tmux copy mode |
| SSH pointer and selection | latest validated route generation in the client | cached content generation |
| Managed Windows base/app identity | signed package state and immutable runtime/app markers | directory presence or version text alone |

## Focused verification

Start with the row matching the symptom. Run the exact test module or node while
iterating, then expand across the boundary that changed. Examples:

```bash
python -m pytest -q tests/test_fast_display_history.py
python -m pytest -q tests/test_fast_display_bootstrap.py tests/test_remote_config.py tests/test_ssh_doctor.py
python -m pytest -q tests/test_session_lease.py tests/test_orphan_recovery.py tests/test_codex_lineage.py
python -m pytest -q tests/test_windows_bootstrap.py tests/test_windows_msys2.py tests/test_windows_pacman.py
```

Before delivery, run the repository checks in `CONTRIBUTING.md`. Run the
opt-in private-socket tmux suite when tmux identity, layout, bindings, display,
history capture, or lifecycle behavior changes. Mocked Windows branches protect
logic but do not establish native-Windows support; support claims require the
real-platform jobs and manual evidence named by `SUPPORT_MATRIX.md` and the
Windows parity ledger.

For a regression, preserve both sides of the boundary. A rewind-history fix,
for example, needs the confirmed-rewind case and an ordinary unrewound styled
live-history case. A Windows adapter fix also needs the unchanged POSIX path.

## Compatibility inventory

Compatibility code must correspond to released or real persisted/live state.
Record its introduction, authority, removal criterion, and earliest removal
version in the relevant architecture section or structured ledger. Current
high-impact bridges include:

- read-only discovery and explicit routing for pre-dedicated-server sessions;
- ownerless/old restart-state migration into portable view state only;
- released managed-Windows base, app-layer, and UI-transition markers;
- older SSH protocol peers where the architecture explicitly permits them.

Do not add speculative migration paths. When a criterion is met, remove the
implementation, routing fields, tests, and documentation together.
