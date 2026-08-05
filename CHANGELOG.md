# Changelog

All notable changes to **railmux** will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0.dev25] - 2026-08-05

### Added

- Add fail-closed managed-Windows app-layer transitions. dev24+ UIs save state,
  return displayed provider panes, and exec an exact content-bound new app;
  released dev11-dev23 UIs are never killed for migration and instead remain
  available until the user chooses Soft Quit before the next launch.
- Record the exact private MSYS2 package inventory in a content-identity marker,
  bind new app layers to it, expose read-only verification through
  `railmux runtime status --verify`, and add explicit process-aware
  `railmux runtime prune` cleanup with dry-run and optional cache removal.
- Support `railmux config --remote` and `railmux doctor --remote` against an
  already-installed managed Windows preview. Both commands share and pin the
  POSIX/direct launch-family probe; `--remote-platform windows` skips the POSIX
  attempt, while remote Windows runtime installation remains deliberately
  manual.
- Extend privacy-safe local doctor output with bounded managed runtime, content,
  installed/running app, and transition status fields.

### Changed

- Include the complete POSIX/WSL `0.3.6` maintenance baseline.
- Make native Windows `doctor` and all noninteractive missing-runtime commands
  read-only: they report the explicit `runtime install` next step and never
  create even an app layer implicitly. Managed app venvs also refuse the POSIX
  self-upgrade path and direct users to the native PowerShell owner.
- Expand native Windows and real-MSYS2 CI coverage for runtime identity/drift,
  cooperative transitions, safe pruning, provider-path projection, config/doctor
  forwarding, direct Windows remote probing, and privacy contracts.

## [0.4.0.dev24] - 2026-08-05

### Fixed

- Mark the unpublished dev24 candidate whose release gate correctly stopped
  before packaging when the real-MSYS2 job tried to build the unrelated Ruff
  developer tool from source. Dev25 keeps the candidate code unchanged and
  narrows that runtime job to its actual SSH and pytest dependencies.

## [0.4.0.dev23] - 2026-08-05

### Added

- Allow a Linux or macOS `railmux ssh` client to enter a Windows preview host
  whose user-level managed MSYS2 runtime is already installed. Automatic mode
  retains the POSIX discovery ladder first and retries a shell-neutral direct
  launch; `--remote-platform windows` skips the incompatible POSIX probe and
  avoids a second password prompt on password-authenticated hosts.

### Changed

- Pin the detected remote launch family across attach retries and automatic
  reconnects. Windows preview servers identify their managed runtime in the
  bounded compatibility hello, and incompatible Windows versions now fail
  closed with PowerShell app-layer/runtime update commands instead of ever
  running the POSIX user-site or private-venv installers.

## [0.4.0.dev22] - 2026-08-05

### Fixed

- Keep ordinary Codex continuation and context-compaction children in styled
  raw `railmux ssh` history instead of misclassifying them as a rewind and
  showing Preview-formatted transcript text on wheel-up. Canonical transcript
  fallback now requires an explicit provider `thread_rolled_back` increment;
  released heuristic markers safely downgrade without changing provider data.

## [0.4.0.dev21] - 2026-08-05

### Changed

- Include the POSIX/WSL changes released in Railmux 0.3.6.dev5: the first quit
  chooser now exposes visible mouse buttons, while the final destructive
  confirmation remains keyboard-only and existing keyboard shortcuts are
  unchanged.

### Fixed

- Preserve the complete `railmux ssh` agent-history wheel distance when a
  terminal or RDP client batches several SGR reports into one input read, while
  painting only the final local viewport once. Forwarded sidebar/modal wheel
  bursts retain their existing bound.

## [0.4.0.dev20] - 2026-08-05

### Changed

- Include the POSIX/WSL fixes released through Railmux 0.3.6.dev4: a second
  hard-quit confirmation, coherent warm/deep SSH history transitions, and
  preserved cross-row terminal styles.

### Fixed

- Create a missing managed-Windows outer UI at the exact interactive entry
  terminal size, so the first frame no longer starts in tmux's detached
  80x24 default and visibly expands after direct or bridged attachment. A
  missing/non-TTY/invalid size retains the safe default, and existing sessions
  are never pre-resized.

## [0.4.0.dev19] - 2026-08-05

### Fixed

- Preserve the current managed-runtime identity when Soft Quit recreates the
  outer Windows UI on an older dedicated tmux server. The new pane can now
  revalidate its private state directory, reclaim an exactly marked unresolved
  provider, and install status-bar mouse bindings without weakening provider
  history or immutable-tmux identity checks. Only three bounded, non-secret
  Railmux runtime identifiers enter the tmux session environment. Completion
  of the deferred lease now also reprojects both left navigation and the
  current right-side Copy range, so the first visible status is clickable.
- Hide tmux's raw `open terminal failed: not a terminal` implementation detail
  when the Windows terminal bridge succeeds. A successful fallback now emits
  one terminal-aware Railmux info line; unavailable or failed bridges retain
  an actionable error and leave the workspace running.

## [0.4.0.dev18] - 2026-08-05

### Fixed

- Restore the managed Windows UI after Soft Quit when detached Codex or Claude
  sessions keep the dedicated tmux server alive in another Windows Terminal
  Services session. Railmux now creates the missing outer UI session detached
  on the same revalidated server before trying direct attach and its existing
  terminal bridge; provider processes and history files remain untouched.

## [0.4.0.dev17] - 2026-08-05

### Fixed

- Keep `railmux ssh` scrollback continuous when periodic hot captures cannot
  be aligned: never splice unrelated snapshots, preserve an already-frozen
  viewport, and request cumulative deep history from the newest continuous
  suffix.
- Batch independent startup/status-bar tmux commands, suppress identical bar
  rewrites, and skip compact-page geometry probes when only the sidebar exists.
  This removes Windows/MSYS2 process-spawn amplification without changing the
  provider/session discovery path.
- On the managed Windows runtime, prepare crash-safe wheel and shared
  function/status binding leases after the first interactive frame. Provider
  indexing and pane restoration remain synchronous and read-only; keyboard
  Mode/Layout controls remain available while mouse/status ownership finishes
  in the background.

## [0.4.0.dev16] - 2026-08-05

### Added

- Prevent concurrent writers when Claude Code or Codex history is shared
  between hosts: resume and confirmed delete use fail-closed provider-session
  leases, Codex rewind aliases are fenced as one lineage, remote ownership is
  shown as `running on HOST`, and detached providers retain their lease across
  Railmux Soft Quit.
- Revalidate live leases from operating-system lock state and keep an explicit
  warning on a Running row until lost protection is restored. Both providers,
  stopped-session deletion, mode-masking shared filesystems, stale holders, and
  multi-alias conflicts have regression coverage.

### Fixed

- Repaint only rows touched by SSH URL/path hover, drag selection, and click
  feedback instead of clearing the whole alternate screen, eliminating visible
  Windows Terminal flashes.
- Change the SSH client's hardware-cursor mode only when its actual visibility
  changes, preventing live 20 fps patches from repeatedly restarting terminal
  cursor animation after focus moves to an agent pane.
- Flush cross-host session-lease owner metadata before returning a claim, so a
  second NFS client that already observes the lock can immediately identify the
  owning host instead of showing the conservative `another host` fallback.
- Include the POSIX/WSL fixes released through Railmux 0.3.5 while preserving
  the managed-Windows runtime, native provider paths, and preview-only support
  boundary.

## [0.4.0.dev15] - 2026-08-04

### Added

- Allow the managed-Windows preview to reach one dedicated tmux workspace from
  both an interactive desktop terminal and an ordinary OpenSSH login to the
  same Windows account. Railmux keeps direct tmux attachment as the fast path;
  after a quick terminal-only rejection it may make one server-origin PTY
  bridge attempt pinned to the unchanged server and immutable session.
- Forward opaque terminal bytes, resize, heartbeat, and exit frames through a
  random same-user private endpoint. The bridge is an additional tmux client,
  never a second UI or workspace, and can stop only its own attach process.
  Failures leave Codex, Claude, and the existing tmux workspace running and add
  a privacy-safe `railmux doctor` incident category.

### Changed

- Keep routine proof-authorized Windows socket cleanup after normal exit
  silent. A pre-launch recovery remains visible and now describes the recovered
  unresponsive socket without implying that provider session files were read.

## [0.4.0.dev14] - 2026-08-04

### Fixed

- Apply the managed-MSYS2 abandoned-socket settle allowance to
  `railmux doctor` as well as the ordinary launcher. Windows diagnostics now
  report the authoritative `not running` result instead of a false
  `unresponsive` state;
  the POSIX doctor retains its existing one-second probe bound.

## [0.4.0.dev13] - 2026-08-04

### Fixed

- Allow managed-MSYS2 startup discovery five seconds to classify an abandoned
  tmux socket as having no live server. MSYS2 3.7b can return that authoritative
  result just after the previous two-second bound; Railmux now continues into
  tmux's normal safe `new-session` replacement instead of reporting a false
  unresponsive-server failure. POSIX discovery and explicit watchdog bounds
  are unchanged.

## [0.4.0.dev12] - 2026-08-04

### Fixed

- Keep managed-MSYS2 application wheels in a shared Railmux-private pip cache
  instead of discarding every completed download after an app-layer install.
  Preview upgrades now use a 60-second read timeout and, if installation does
  not complete, retry once in the same launch with the same cache and a
  120-second timeout. This fixes app-only upgrades failing on transient
  `files.pythonhosted.org` read timeouts without reinstalling the shared MSYS2,
  tmux, or Python base.

### Security

- Keep the new pip cache outside provider-owned directories and retain the
  existing marker-gated application publication: a failed retry leaves only an
  unpublished Railmux app directory for bounded cleanup on the next launch,
  while existing Codex/Claude session files and older app layers remain
  untouched.

## [0.4.0.dev11] - 2026-08-04

### Changed

- Reuse one verified private MSYS2/tmux/Python base across Windows preview
  upgrades while its pinned runtime identifier remains unchanged. Each Railmux
  build keeps a separate application venv published by an atomic validity
  marker, so normal preview upgrades no longer repeat the roughly 700 MB base
  installation.
- Adopt a complete dev4-dev10 Railmux-managed runtime in place after exact
  validation, preserving its legacy app for rollback and showing a compact
  three-phase app-layer upgrade instead of the seven-phase base install. A
  matching base upgrade continues without a redundant confirmation; the
  roughly 700 MB first installation still asks once unless `--yes` was given.

### Security

- Keep user-selected MSYS2 roots read-only and limit adoption to an exact
  Railmux-owned legacy directory, marker, runtime identifier, version, and
  executable probe. Provider histories remain outside the installation path
  and are never opened or modified by base reuse.
- Constrain confirmation-free upgrades to a `reuse_only` installer path that
  fails instead of escalating to a full base download if the candidate changes
  or cannot be validated.
- Validate separate same-owner base and versioned-application markers before
  granting MSYS2's narrow `noacl` permission exception.
- Treat the adopted base marker as the lasting discovery authority while
  retaining the old runtime marker only for rollback, so later rollback-state
  cleanup cannot orphan the shared base and trigger a duplicate installation.

## [0.4.0.dev10] - 2026-08-03

### Fixed

- Recover from the abandoned AF_UNIX pathname that MSYS2 tmux 3.7b can leave
  after the final Railmux session exits, so a subsequent Windows launch no
  longer reports an unresponsive server.
- Allow exact-owner restart state to persist under MSYS2's `noacl` permission
  projection, and make the same verified exception available to Railmux's
  startup caches and tmux binding/lock state so restore speed and clickable
  status controls do not silently degrade. Linux and macOS remain strict.

### Security

- Authorize Windows socket cleanup only when immutable server-wide inventories
  prove the managed outer UI is the sole session and pane, then require an
  unchanged same-user socket identity, an exit-settle interval, and two failed
  connection probes plus proof that the recorded tmux PID is gone. Provider
  histories are never opened or modified, and any live PID, provider, or
  unknown tmux session denies cleanup.
- Keep the MSYS2 permission exception limited to the real Cygwin/MSYS managed
  wrapper with an exact same-owner on-disk runtime marker, same-UID non-symlink
  state, and no group/world write bits; the user-owned managed runtime's
  Windows ACL remains the privacy boundary.

## [0.4.0.dev9] - 2026-08-03

### Added

- Add terminal-aware color and hierarchy to the Windows installer while
  retaining plain UTF-8 evidence logs and honoring `NO_COLOR`.
- Show read-only provider-index and tmux-recovery stages during Windows startup,
  then report slow restore timing in the status bar.
- Persist a private Windows-only Claude session-validity cache so unchanged
  histories do not need to be reopened on every Railmux process start.

### Changed

- Validate up to twelve evenly distributed packages from pacman's actual
  resolved transaction on every active mirror, catching sources that serve the
  database but return 403 for only some dependencies before the install starts.
- Explain that pacman fallback is per package and that completed, signature-
  checked downloads remain in Railmux's private cache across a retry.
- Use tmux 3.7's dedicated status control ranges and safely leased
  `MouseDown1ControlN` bindings for Mode, Layout, and status-copy actions.

### Fixed

- Make the Mode and Layout controls clickable under the tmux 3.7b runtime
  bundled by the Windows preview, while retaining `m` and `F8` fallbacks and an
  explicit warning if safe binding ownership is unavailable.

### Security

- Never write, migrate, truncate, or repair provider session JSONL files. The
  Windows validity cache contains only relative provider coordinates, exact
  file signatures, and a validity bit; it is atomically written with private
  permissions, can be deleted safely, and never authorizes agent restoration.
- Capture and durably persist every affected tmux control binding before
  replacement, and restore only bindings still bearing Railmux's exact lease.

## [0.4.0.dev8] - 2026-08-03

### Added

- Surface bounded extraction percentages, pacman repository/package milestones,
  and a 15-second heartbeat for every otherwise-silent installer subprocess.
- Retain the hash-verified MSYS2 base and signature-checked pacman package cache
  outside transactional staging so a failed attempt does not redownload
  completed artifacts.

### Changed

- Generate a Railmux-private pacman configuration containing only the `msys`
  repository required by tmux and Python, avoiding five unused mingw/clang
  database synchronizations.
- Re-measure package mirrors after the base upgrade and keep only approved,
  successfully measured candidates active; stale measured sources remain last-
  resort fallbacks, while every other official entry remains visible but
  inactive in the private mirrorlist.
- Update the Windows consent estimate to about 700 MB or more of private disk
  after measuring a 561 MB runtime plus retained verified caches, and explain
  that this footprint is the complete private MSYS2 compatibility wrapper,
  including tmux and Python, rather than the Railmux Python package alone.

### Fixed

- Recover automatically when package mirrors return HTTP 403/404 or pacman's
  low-speed timeout: exclude hard-failing hosts, reuse completed packages, and
  retry once with relaxed low-speed handling so a slow working source can
  finish instead of discarding the entire installation.

### Security

- Keep mirror activation limited to exact approved HTTPS URLs present in the
  official private mirrorlist; cached archives retain pinned SHA-256 checks and
  cached packages retain pacman signature verification.

## [0.4.0.dev7] - 2026-08-03

### Added

- Measure fresh MSYS package databases from a bounded approved subset of the
  private runtime's official mirrorlist and promote a materially faster source
  before pacman runs, while retaining every official fallback.
- Present Windows runtime installation as seven stable phases, retain complete
  redacted UTF-8 subprocess logs, show a bounded failure tail, and add
  `railmux runtime install --verbose` for raw live output.

### Changed

- Clarify that the initial 50 MB base download is followed by required private
  MSYS2 updates and packages, and that pacman's printed confirmation needs no
  keyboard input.
- Bound both the number and displayed length of failure-tail lines while
  preserving the complete line in the diagnostic log.
- Extend advisory Windows-preview mirror CI to exercise the approved pacman
  database endpoints as well as the pinned base archive transports.

### Security

- Reorder only a staged Railmux-owned mirrorlist, require exact approved
  official HTTPS URLs and a fresh bounded Zstandard database sample, retain
  pacman signature verification, redact credentials from logs, and preserve
  the original official order whenever selection cannot be proven safe.

## [0.4.0.dev6] - 2026-08-03

### Changed

- Sample the preferred managed-MSYS2 archive source and, when its projected
  remaining time exceeds one minute, concurrently compare the other approved
  transports. Switch only for a material measured speedup; if all are slow,
  continue the best available download instead of failing.
- Reuse sampled bytes, preserve the exact partial offset across approved-source
  transfer failures, and make Windows-preview CI check Range-resume capability.

### Security

- Require exact HTTPS `206` and `Content-Range` responses for adaptive transfer,
  retain the pinned final size and SHA-256 checks, and fall back to ordinary
  verified full downloads when Range resume is unavailable.

## [0.4.0.dev5] - 2026-08-03

### Added

- Show bounded byte/total/percentage progress while the managed Windows MSYS2
  archive downloads, including useful byte milestones in redirected logs.

### Security

- Fall back across the official GitHub release, MSYS2 repository, TUNA, and
  NJU transports only after discarding the failed partial archive, and require
  every source to match the same pinned SHA-256 before extraction.

## [0.4.0.dev4] - 2026-08-03

### Added

- Replace the archived native-to-WSL experiment with an explicit,
  consent-based native Windows bootstrap into a private managed MSYS2/tmux
  runtime, keeping the established POSIX Railmux UI as the sole implementation.
- Share native Codex and Claude Code executables, credentials, configuration,
  and histories through the Windows user profile; translate native drive paths
  and Claude project keys when indexing existing sessions from MSYS2.
- Add Windows package dispatch and managed-runtime tests, a real-MSYS2 CI
  smoke, and a parity ledger that keeps unverified terminal behavior visible.

### Security

- Pin and hash-verify the official MSYS2 base archive, serialize and stage
  installs before atomic activation, use explicit UTF-8 for runtime state, and
  never modify system PATH, shell profiles, or a user-owned MSYS2 tree.

## [0.3.5] - 2026-08-04

### Added

- Prevent concurrent writers when Claude Code or Codex history is shared
  between hosts: resume and confirmed delete use fail-closed provider-session
  leases, Codex rewind aliases are fenced as one lineage, remote ownership is
  shown as `running on HOST`, and detached providers retain their lease across
  Railmux Soft Quit.
- Revalidate live leases from operating-system lock state and keep an explicit
  warning on a Running row until lost protection is restored. Both providers,
  stopped-session deletion, mode-masking shared filesystems, stale holders, and
  multi-alias conflicts have regression coverage.

### Fixed

- Repaint only rows touched by SSH URL/path hover, drag selection, and click
  feedback instead of clearing the whole alternate screen, eliminating visible
  Windows Terminal flashes.
- Change the SSH client's hardware-cursor mode only when its actual visibility
  changes, preventing live 20 fps patches from repeatedly restarting terminal
  cursor animation after focus moves to an agent pane.

## [0.3.5.dev1] - 2026-08-04

### Fixed

- Repaint only rows touched by SSH URL/path hover, drag selection, and click
  feedback instead of clearing the whole alternate screen, eliminating visible
  Windows Terminal flashes.
- Change the SSH client's hardware-cursor mode only when its actual visibility
  changes, preventing live 20 fps patches from repeatedly restarting terminal
  cursor animation after focus moves to an agent pane.

## [0.3.4] - 2026-08-04

### Fixed

- Restore live-pane scrolling after the 0.3.3 regression: direct Railmux keeps
  tmux/provider-native wheel behavior, while `railmux ssh` retains its bounded
  per-pane history manager and normally renders styled raw pane capture instead
  of formatting every Codex session like Preview. Only an exactly matched,
  confirmed rewind generation may use canonical transcript history to hide the
  abandoned suffix; regression tests lock down both sides of this boundary.
- Keep the direct-launch restoration surface clean by hiding the dedicated
  tmux server's stock status row until Railmux's managed UI and status bar are
  ready; initialization failures and soft-quit shells inherit the same hidden
  baseline instead of exposing an unmanaged `tmux` bar.

## [0.3.4.dev1] - 2026-08-04

### Fixed

- Restore live-pane scrolling after the 0.3.3 regression: direct Railmux keeps
  tmux/provider-native wheel behavior, while `railmux ssh` retains its bounded
  per-pane history manager and normally renders styled raw pane capture instead
  of formatting every Codex session like Preview. Only an exactly matched,
  confirmed rewind generation may use canonical transcript history to hide the
  abandoned suffix; regression tests lock down both sides of this boundary.

## [0.3.3] - 2026-08-04

### Fixed

- Keep a selected project and its Sessions list visible while a new Codex
  placeholder becomes a real conversation, and update Sessions and Running
  together with the resolved title, live route, status, and recency ordering.
- Preserve a swap-displayed agent's terminal geometry when returning it to its
  detached home, preventing narrow or discontinuous scrollback after the pane
  is shown again.
- Show the provider's canonical active history after Codex rewind or
  running-turn steering and Claude Code branch changes: retained conversation
  remains visible, abandoned prompt and interruption suffixes stay hidden, and
  the live tmux scrollback is no longer cleared during a branch transition.
- Keep single-click and Enter on a running session attached to its live
  conversation while wheel-up, Space, and context Preview open the canonical
  read-only history and restore the exact live pane when closed.
- Close delete confirmation immediately, perform tmux and provider-history
  cleanup in the background, and keep a durable bounded status such as
  `Deleting “session name”…` until the transaction finishes.
- Recognize tmux 3.2's stock `send -M` wheel binding spelling as equivalent to
  newer tmux's `send-keys -M`, restoring safe sidebar wheel forwarding without
  accepting custom root bindings.

### Changed

- Keep detailed SSH URL/path opening and pane-bounded clipboard instructions
  in the README while presenting their user-facing value more concisely on the
  project website.
- Harden release validation so POSIX/WSL final and development builds remain
  rooted in `main` and archived native-Windows preview ancestry cannot be
  published accidentally.

## [0.3.3.dev6] - 2026-08-04

### Fixed

- Preserve a live Codex pane's retained tmux scrollback across rewind and
  running-turn steering while resetting only the stale viewport; local preview
  and full-window SSH history now read the canonical rollout so retained
  conversation remains visible and the abandoned interruption suffix stays
  hidden.
- Name the exact target in the durable background-delete status, with bounded
  whitespace-normalized text such as `Deleting “session name”…`.

### Changed

- Keep detailed SSH URL/path and clipboard instructions in the README while
  reducing the website's repeated selection note to a concise overview of
  local URL/path opening and pane-bounded drag-to-copy.

## [0.3.3.dev5] - 2026-08-04

### Fixed

- Close the delete confirmation immediately and move tmux shutdown waits plus
  Claude Code/Codex history cleanup off the UI thread, while keeping a durable
  bottom-right `Deleting…` status until the background transaction finishes.

## [0.3.3.dev4] - 2026-08-03

### Fixed

- Reset a live Codex pane's visible terminal cache and tmux scrollback when its
  canonical rollout advances to a rewind child, so the abandoned turn and red
  interruption notice do not remain above the replacement conversation; SSH
  display protocol v15 carries the corresponding pane generation so its local
  history cache drops the same stale suffix.

## [0.3.3.dev3] - 2026-08-03

### Fixed

- Keep the selected project and Sessions list visible while a new Codex
  placeholder is promoted to its real conversation, avoiding a one-refresh
  flash of the empty “Select a project” state.
- Project live-session history from the provider's current branch: Codex uses
  the newest rewind rollout and Claude Code follows its active `parentUuid`
  chain, so abandoned prompt suffixes no longer replace or obscure retained
  conversation history.
- Route wheel-up, Space, and context Preview on a running session into that
  canonical read-only history while keeping single-click and Enter attached to
  the real live conversation; closing the viewer restores the exact live pane.

### Changed

- Freeze the abandoned ConPTY Windows preview at `v0.4.0.dev2`, move the active
  Windows experiment to a clean delegated-runtime wrapper branch beginning at
  `0.4.0.dev3`, and reject archived ancestry during publication.

## [0.3.3.dev2] - 2026-08-03

### Fixed

- Preserve a swap-displayed agent's visible geometry when returning it to its
  detached home, preventing background output from forming a narrow,
  discontinuous block when the session is shown and scrolled again.
- Resolve newly launched Codex sessions through the real pane after a swap, so
  Sessions and Running update together, the first click reopens the live
  conversation instead of a transcript preview, and the promoted active row
  receives its real title, status, and recency ordering immediately.

## [0.3.3.dev1] - 2026-08-02

### Changed

- Maintain independent PEP 440 development-release lanes for POSIX/WSL builds
  from `main` and native-Windows builds from `windows-preview`, with tag
  ancestry enforced before publication.

### Fixed

- Keep the isolated real-tmux soft-quit smoke test independent of published
  version-update prompts, so a newer PyPI release cannot alter CI behavior.
- Recognize tmux 3.2's stock `send -M` wheel binding spelling as equivalent to
  newer tmux's `send-keys -M`, restoring safe sidebar wheel forwarding without
  accepting custom root bindings.

## [0.3.2] - 2026-08-02

### Fixed

- Add the previously missing native/local Termux prompt-tap handoff: a
  fail-closed stock tmux left-click wrapper yields an authoritative agent input
  row to Android, restores mouse ownership on keyboard projection/close and
  teardown, and includes a process-independent nonce watchdog. This remains
  separate from the existing `railmux ssh` client path.
- Restore Termux prompt keyboard handoff when Codex or Claude retains exact
  input coordinates inside a verified live agent route while hiding the DEC
  hardware cursor, and cover the complete route/screen/tap-to-mouse-yield
  boundary in one regression test.
- Read Railmux state, provider JSONL, and captured diagnostic/version output
  with explicit UTF-8 policies so a non-UTF-8 process locale cannot crash
  startup or corrupt user data.

## [0.3.1] - 2026-08-01

### Changed

- Show transient, scrollback-free progress while remote configuration and
  local or remote doctor commands perform checks, without adding noise to
  redirected or JSON output, and label the remote config editor explicitly.
- Add restrained, `NO_COLOR`-aware hierarchy and outcome colours to the
  standalone configuration editor and human-readable doctor reports.
- Redraw the standalone configuration editor from explicit navigation state:
  category pages retain compact parent context, setting pages use breadcrumbs,
  Back restores a clean parent page, and action feedback remains visible until
  the next input.
- Surface local and remote `railmux config` setup in Quick start while keeping
  the complete editor and SSH provisioning details in the Configuration guide.

### Fixed

- Reassert Termux's last authoritative application cursor after returning to
  the app, without repainting the screen or changing remote focus behavior.

## [0.3.1.dev202608010] - 2026-08-01

### Changed

- Show transient, scrollback-free progress while remote configuration and
  local or remote doctor commands perform checks, without adding noise to
  redirected or JSON output, and label the remote config editor explicitly.
- Add restrained, `NO_COLOR`-aware hierarchy and outcome colours to the
  standalone configuration editor and human-readable doctor reports.
- Redraw the standalone configuration editor from explicit navigation state:
  category pages retain compact parent context, setting pages use breadcrumbs,
  Back restores a clean parent page, and action feedback remains visible until
  the next input.

### Fixed

- Reassert Termux's last authoritative application cursor after returning to
  the app, without repainting the screen or changing remote focus behavior.

## [0.3.0] - 2026-08-01

Railmux 0.3.0 is the first productized maturity baseline for the compatible
0.2.x feature arc: provider-aware Claude Code and Codex session management, a
responsive two-agent workspace, isolated tmux lifecycle and recovery, the
latest-state `railmux ssh` client, unified configuration and diagnostics, and
an explicit platform-support contract. The sections below list only changes
made after 0.2.21.

### Added

- Add a public security policy describing private reporting, supported
  versions, and Railmux's provider, SSH, terminal, clipboard, and semantic-link
  trust boundaries.
- Validate the package version against a non-empty curated changelog section
  before tagging, and expand isolated-wheel smoke coverage across every public
  command's help path without creating user state.

### Changed

- Make Railmux-owned atomic replacements durable before publication and
  best-effort durable at the parent-directory boundary, without failing a
  successful write on filesystems that reject directory synchronization.
- Make the README's repository assets render from PyPI, clarify confirmed
  provider-history deletion and Ask Railmux's fail-closed provider-version
  boundary, and retain the Beta support classification for the 0.x line.
- Pause product-site terminal recordings for reduced-motion visitors while
  keeping an explicit playback control, improve keyboard focus and small-text
  contrast, describe replayed provider material consistently, and align the
  browser terminal grid to remove extra gaps between box-drawing rows.

### Fixed

- Make the Termux “tap the prompt again” keyboard hint transient and
  interruptible, so it yields to later status messages and restores Railmux's
  normal status-right content after two seconds.
- Report persistent **Never** options as Never rather than conflating them with
  a one-time **No** choice.
- Give the long-form Help and Options interfaces consistent horizontal padding
  without expanding compact confirmation dialogs.

## [0.2.21] - 2026-08-01

### Added

- Add `railmux config`, a tmux-independent local and remote configuration
  editor for persistent behavior, executable paths, SSH history capacity, and
  locale settings, with validation and scoped reset actions.
- Add ordered `--ssh-args='...'` groups across `railmux ssh`, remote config,
  and remote doctor, and standardize the public remote spelling on
  `config/doctor --remote HOST`.
- Add a documented product support contract and a required CI job that builds
  tmux 2.7 from its checksum-pinned official source and exercises Railmux's
  real private-server integration path.

### Changed

- Run the interactive `railmux config` editor on a temporary alternate screen,
  restoring the caller's shell and scrollback unchanged on every exit path.
- Keep root help focused on public command discovery while each subcommand
  documents its complete interface; deprecated and internal compatibility
  arguments remain hidden.
- Refresh the README and product website around everyday session management,
  mouse/keyboard parity, Mode and Layout controls, remote usage, and clean
  recorded workflows.

### Fixed

- Keep automatic SSH reconnect progress visible in Railmux's bottom-right
  status area while the last frame remains frozen, re-anchor display, cursor,
  and history state after recovery, and bound black-holed connection detection
  with overridable OpenSSH keepalive defaults.
- Keep the current modal geometry while replacing quit or layout confirmation
  with exit progress, avoiding a transient mixed-window frame on slow displays.
- Align the website's More pointer with the real button, remove an unnecessary
  sidebar-focus click before switching Running sessions, and publish soft-quit
  progress as one complete recorded frame.
- Validate compact agent pages against their visible zoomed viewport instead
  of a hidden narrow split, preventing false pane-size errors on phone-sized
  terminals and in the product demo.
- Preserve an explicit SSH `ConnectTimeout` during automatic reconnect while
  keeping non-interactive authentication mandatory, and make Railmux's required
  PTY/no-PTY mode authoritative after user SSH arguments.
- Fail compact/fullscreen zoom operations closed when tmux cannot report the
  current zoom state, and restore the prior pane/zoom if a later select or zoom
  step fails, avoiding ownership guesses or partial fullscreen mutations.
- Return normal CLI error status from conflicting standalone-config arguments
  instead of leaking an unexpected `SystemExit` to embedded callers.
- Preserve the mobile prompt cursor in Termux and resolve and highlight every
  row of deeply wrapped URLs as one target.
- Keep Railmux compatible with tmux 2.7's format, binding, locale, and zoom
  behavior while reporting the tmux 3.0 requirement before managed terminal or
  Vim panes can be partially created.
- Classify tmux client/server version mismatches accurately, keep configured
  PATH and locale selection consistent, and preserve local-only SSH history
  capacity during remote configuration resets.

## [0.2.21.dev202608010] - 2026-08-01

### Added

- Add `railmux config`, a tmux-independent two-level editor for persistent
  behavior, tmux/Claude/Codex executable paths, SSH history capacity, and a
  scoped UTF-8 locale, with validation plus setting/category/global resets.
- Add `railmux config --remote HOST` with shared SSH discovery, consent-based
  user/private-venv setup, a separate cooked PTY, and no tmux attach or query.
- Add ordered `--ssh-args='...'` groups to `railmux ssh`, remote config, and
  remote doctor; older exact `--ssh-arg=VALUE` remains hidden compatibility.
- Standardize remote operations on `config/doctor --remote HOST`; the released
  `doctor --ssh HOST` remains a hidden compatibility alias until 0.4.0.
- Report bounded local and remote config, locale, and configured-tmux status in
  `railmux doctor` and the pre-attach SSH compatibility handshake.

### Fixed

- Keep root help focused on public command discovery, document each
  subcommand's own options, and hide deprecated compatibility or internal
  flags from `-h`/`--help` output.
- Classify tmux client/server version mismatch instead of hiding tmux stderr as
  an invalid server identity, and keep configured PATH/locale selection
  consistent for future children of the exact dedicated server without
  restarting or replacing live sessions.
- Keep the standalone config editor independent of live tmux-server discovery,
  and preserve the local-only SSH history capacity during remote config resets.
- Keep identity and remote-pane format parsing compatible with tmux 2.7 under
  the `C` locale, where tmux sanitizes tab delimiters in formatted output.

## [0.2.21.dev202607311] - 2026-07-31

### Added

- Build the checksum-pinned official tmux 2.7 release in CI and run the real
  private-server integration suite, including an actual first interactive
  Railmux frame, as a required compatibility-floor job.

### Fixed

- Keep the mobile prompt cursor visible in `railmux ssh` by treating Termux's
  soft-keyboard View handoff as local UI focus, suppressing its transient
  focus-out, and reasserting focus only for remote applications that requested
  terminal focus events. Desktop terminals and applications without focus
  reporting retain their existing input behavior.

- Re-anchor the local SSH display, cursor, and pane-history routes on the first
  fresh keyframe after automatic reconnect, preventing subsequent typing from
  scrolling retained pixels and restoring mouse-wheel history immediately.
- Resolve and highlight every row of a three-or-more-line soft-wrapped URL as
  one target, regardless of which wrapped row the user hovers or clicks.
- Accept and safely replay tmux 2.7's unescaped stock copy-mode wheel bindings,
  use its quoted right-click branch grammar, and preserve zoom across the
  pre-3.1 Prefix-Tab pane switch.
- Report the tmux 3.0 requirement for managed terminal/Vim panes explicitly
  instead of partially creating a pane that cannot carry safe identity metadata.

## [0.2.20] - 2026-07-30

### Fixed

- Re-arm bracketed-paste and focus-event modes across automatic SSH reconnects,
  and keep retry diagnostics from writing through the retained terminal
  surface, so typing after a recovered connection no longer corrupts the
  display.

## [0.2.19] - 2026-07-30

### Added

- Open clean-clicked HTTP(S) URLs from `railmux ssh` in the local browser and
  resolve remote paths read-only against the visible agent pane.
- Add one managed shell and tabbed Vim viewer per agent. Clicked paths can open
  inside Railmux or in a separate terminal through an Ask/Always policy, while
  `t`, `T`, F9, and layout changes preserve the exact managed processes.
- Add Termux touch handling for compact navigation and focused prompt keyboard
  access, plus directly clickable **Soft quit** and **Cancel** choices.
- Add `railmux doctor --ssh HOST`, privacy-safe SSH display diagnostics,
  visible startup stages, and a deterministic display wire-budget benchmark.

### Changed

- Enable bounded post-attach `railmux ssh` reconnection by default; use
  `--no-reconnect` for a one-invocation opt-out.
- Make workspace restoration event-driven and reuse a validated final pane
  skeleton, reducing startup reflow without weakening identity checks.
- Split SSH compatibility, input, selection, and history into independently
  tested state machines and avoid redundant periodic history captures until
  newer output can make them stale.

### Fixed

- Prevent `railmux ssh` compact resizes from leaving short half-width segments
  in agent history. Before a coordinated shrink, hidden swap-owned providers
  are parked in their detached home windows while inert placeholders absorb
  tmux's narrow reflow; page switches restore the exact live pane only after
  its target has the full compact viewport.
- Bound same-direction trackpad wheel packets accumulated during a busy remote
  repaint to one local-history row per terminal read, so scrolling down while
  an agent is producing output no longer jumps past the passage being read.
- Preserve intermediate Codex output across rewind or full-screen redraws when
  a validated deep history capture is larger than the unaligned hot cache,
  instead of retaining only the newest screen and hiding the text above it.
- Restore Termux taps, swipes, session activation, compact page navigation, and
  mouse control after soft-keyboard viewport changes.
- Reassemble wrapped URLs and remote paths across pane coordinates, exclude
  adjacent labels and Unicode prose, and keep hover/click routing correct in
  either visible agent pane.
- Keep compatibility, installation, update, and attach prompts on the
  recoverable startup surface; bound cancellation and first-frame failure
  without stopping the remote workspace.
- Keep local SSH status text authoritative for click-to-copy and recover exact
  tool owners for live UIs upgraded from an earlier package.

### Documentation

- Recommend `railmux ssh` in the website and Quick Start when ordinary remote
  redraws are not sufficiently smooth from macOS, Linux, or Windows WSL.

## [0.2.19.dev202607308] - 2026-07-30

### Fixed

- Restored Termux taps, swipes, compact navigation, and session activation by
  using the button-event mouse mode that Termux actually implements. Desktop
  clients retain any-event tracking for URL/path hover.

## [0.2.19.dev202607307] - 2026-07-30

### Fixed

- Kept Termux compact `R`/`A1`/`A2` taps on the mouse-navigation path even
  when a just-restored workspace still has stale agent-route geometry, instead
  of accidentally yielding the tap to the soft keyboard.

## [0.2.19.dev202607306] - 2026-07-30

### Fixed

- Reassert Termux mouse modes once more after the soft-keyboard close resize
  has settled, restoring history scrolling and sidebar clicks when Termux
  completes native touch ownership transfer after SIGWINCH.
- Show semantic URL/path hover in either visible agent pane while retaining
  the safety rule that an unfocused pane's first click only changes focus.

## [0.2.19.dev202607305] - 2026-07-30

### Fixed

- Stop clean-click URL recognition before adjacent Unicode punctuation and
  prose instead of sending the explanatory text to the local browser.
- Avoid manufacturing an empty managed shell for a first click that opens
  only Vim; quitting that viewer now returns directly to its agent.

### Changed

- Make F9 follow a focused managed terminal or Vim, and keep per-agent tool
  splits orthogonal to the outer layout: below side-by-side agents and to the
  right of stacked agents.

## [0.2.19.dev202607304] - 2026-07-30

### Fixed

- Reassert Termux mouse-reporting modes after the soft-keyboard viewport
  closes, returning click and drag control to Railmux instead of leaving
  native terminal touch handling active.
- Reassemble agent-rendered paths across bounded indented hard newlines,
  highlight every physical fragment, and open the complete file in the
  existing managed Vim rather than treating its first-row directory as the
  target.
- Expire the initial `railmux ssh` connection hint and dismiss it on the first
  user action so Railmux pane-focus and status messages are immediately
  visible.

### Changed

- Label the inside clicked-path destination as Railmux's managed Vim in both
  the first-use dialog and Options.

## [0.2.19.dev202607303] - 2026-07-30

### Fixed

- Restore Railmux mouse gestures as soon as Termux reports that its soft
  keyboard projection is open, bound the remaining projection state, and
  discard a missing initiating release so closing the keyboard cannot leave
  touch input owned by Termux or consume the next Railmux click.
- Reassemble clicked URLs and remote paths across bounded visual wraps, strip
  non-path labels before absolute paths, and apply the correct horizontal pane
  offset to hover and click coordinates.
- Make local SSH warnings and acknowledgements the authoritative clickable
  status-right source, so copying the displayed message cannot return an older
  tip or focus notification.
- Recover the exact primary or secondary agent slot from existing
  identity-checked Railmux pane markers when a live UI process predates the
  newer managed-tool owner options.

## [0.2.19.dev202607302] - 2026-07-30

### Added

- Add a per-agent managed shell and Vim surface: `t` returns to the shell,
  `T` returns to Vim, subsequent clicked files use native Vim tabs, and layout
  rebuilds park the exact processes without killing them.
- Let the first clicked remote path choose **Always** or **This time** for
  opening inside Railmux or in a separate terminal; expose the persistent
  Ask/Inside/Separate policy in Options.
- Highlight recognized URL/path text on local `railmux ssh` hover and briefly
  flash the token when it is clicked without forwarding pointer motion.

### Changed

- Advance the private SSH display protocol to v14 with bounded path-open
  choices and acknowledgements; managed tool panes are excluded from agent
  history and semantic-click routing.
- Enable bounded post-attach `railmux ssh` reconnection by default, with
  `--no-reconnect` as an explicit one-invocation opt-out.
- Clarify whether a clicked remote path is opening in Vim, opening a directory,
  or opening an unsupported file's containing directory, and document that
  the Vim capability is checked remotely.
- Add a concise VS Code/Cursor CJK input-method recovery note for stuck
  xterm.js composition focus and `Ctrl-Space` conflicts.

## [0.2.19.dev202607301] - 2026-07-30

### Added

- Let `railmux ssh` open a clean-clicked HTTP(S) URL in the local browser and
  resolve a clicked remote path read-only against the visible agent pane.
  Common code, text, log, and HTML files open in remote Vim inside a new local
  terminal; directories and unsupported files open their containing remote
  directory, with a copied SSH command as the safe launcher fallback.

### Changed

- Advance the private SSH display protocol to v13 with bounded typed path
  lookup messages. The server accepts only visible non-controller panes and
  never opens or mutates the requested path.

## [0.2.19.dev202607300] - 2026-07-30

### Added

- Let Termux users tap the focused Claude Code or Codex prompt to temporarily
  yield `railmux ssh` mouse tracking, then tap again to open the soft keyboard;
  tracking returns after input, keyboard close, or a bounded timeout.
- Make the safe **Soft quit** and **Cancel** choices in the quit confirmation
  directly clickable while retaining explicit keyboard shortcuts.

### Changed

- Start deferred project/workspace restoration without fixed 50/100 ms waits
  and reuse an already validated prelayout pane skeleton instead of attempting
  the same topology creation twice. Session, tmux identity, provider-index, and
  swap transaction validation remain unchanged.

## [0.2.19.dev202607292] - 2026-07-29

### Added

- Show live connection, compatibility, attach, and first-frame stages beneath
  the recoverable `Restoring your workspace` SSH startup screen.
- Add `railmux doctor --ssh HOST` as a bounded read-only compatibility probe
  that never attaches, creates, resizes, replaces, installs, or upgrades a
  remote workspace and omits the host from its report.
- Add a deterministic SSH display wire-budget benchmark to CI, protecting
  compressed keyframes, small row patches, and bounded history requests
  without flaky wall-clock thresholds.

### Fixed

- Bound an accepted display's first frame to 30 seconds, restoring the local
  terminal with an actionable error while leaving the remote Railmux session
  and agents intact.
- Verify that opt-in reconnect cannot affect the initial SSH command or attach
  path, and bound the replacement transport's first frame independently.

## [0.2.19.dev202607291] - 2026-07-29

### Fixed

- Keep `railmux ssh` compatibility and update prompts visibly on the
  recoverable startup screen instead of switching terminal buffers underneath
  `Restoring your workspace`.
- Show installation output after consent, restore the primary terminal before
  restarting an upgraded local client, and repaint the startup screen only
  when setup has finished.
- Let `Ctrl-C` cancel a stalled remote setup cleanly while restoring the local
  terminal and leaving the remote Railmux session and agents intact.

## [0.2.19.dev202607290] - 2026-07-29

### Added

- Include a privacy-safe summary of the most recent `railmux ssh` connection
  in `railmux doctor`, with bounded frame, transfer, reconnect, and local
  history counters but no host, session, path, content, or raw-error data.
- Declare shared setting validation and activation boundaries for local SSH
  history, Claude history ownership, update policy, Codex auto-run, and layout
  retention.

### Changed

- Split terminal input, pane-bounded selection, and local history state out of
  the SSH client lifecycle so each state machine can be tested independently.
- Resolve SSH package/protocol compatibility through a pure, re-entrant
  decision layer while retaining exact protocol equality as the compatibility
  authority.
- Stop repeating the 300-line periodic history capture after an accepted
  snapshot until newer screen output can have made it stale; route changes,
  reconnects, and policy recovery still refresh immediately.

### Fixed

- Verify that a fresh process using the current Python actually imports an
  accepted update before restarting, avoiding restarts into the wrong
  `site-packages`.
- Prevent an older failed SSH startup from overwriting diagnostics owned by a
  newer attached client.

## [0.2.18] - 2026-07-29

### Added

- Let `Page Up` and `Page Down` move one visible page through the smooth local
  `railmux ssh` history when the keyboard cursor is inside an agent pane,
  without taking over sidebar or dialog navigation.

### Changed

- Advance the private SSH display protocol to v12 so local and remote helpers
  agree on full-width styled history semantics.
- Persist signature-validated Codex metadata in a private atomic cache, show
  explicit startup/indexing feedback, and avoid reparsing unchanged rollouts.
- Move touchpad and wheel history by exactly one row per terminal event while
  retaining cumulative 2,000-line background expansion up to the configured
  local limit.

### Fixed

- Preserve red, green, gray, and other styled backgrounds through the full
  width of captured history rows instead of ending them after the last visible
  character.
- Preserve bounded deep history across periodic prefetches and safe automatic
  reconnects, while removing obsolete blank tails left by temporary
  full-screen Codex `/btw` views.
- Prevent automatic reconnect from recreating a workspace that was Soft Quit,
  and require a fresh painted frame before another reconnect attempt.
- Keep live Codex sessions resolved throughout cold indexing, apply persistent
  Claude history changes immediately, and keep remote prompts separate from
  the local startup surface.

## [0.2.18.dev202607293] - 2026-07-29

### Changed

- Keep `railmux ssh` startup feedback on a recoverable alternate screen,
  delaying interactive mouse and cursor modes until the first real frame.
- Advance smooth local history by exactly one row for each terminal wheel
  event.

### Fixed

- Preserve bounded local history safely across automatic reconnects without
  allowing a retry to recreate a Railmux UI that was intentionally Soft Quit.
- Remove obsolete blank history tails left by temporary full-screen Codex
  `/btw` views.
- Require a fresh painted frame before another automatic reconnect attempt,
  and keep interactive remote prompts from interleaving with startup output.

## [0.2.18.dev202607292] - 2026-07-29

### Changed

- Persist signature-validated Codex metadata in a private atomic cache so
  subsequent Railmux processes stat the history tree but parse only new or
  changed rollouts.
- Paint `Restoring your workspace` locally as soon as `railmux ssh` starts,
  instead of leaving the terminal apparently idle during remote preflight.

## [0.2.18.dev202607291] - 2026-07-29

### Fixed

- Keep exact live Codex sessions resolved during the cold startup index instead
  of briefly showing rewind descendants as `unresolved`, then strictly
  revalidate them against the first complete index generation.
- Show an explicit Codex `Indexing…` state instead of temporarily empty
  Projects and Sessions panes while the initial background scan is pending.

## [0.2.18.dev202607290] - 2026-07-29

### Changed

- Clarify in Options whether each setting affects exit, future agent launches,
  the next outer launch, or the current SSH history refresh.

### Fixed

- Preserve already-fetched `railmux ssh` history across shallow periodic
  prefetches and shrinking tmux/Claude transcript suffixes, while keeping the
  cache bounded by the configured local history limit.
- Apply persistent Claude history changes from Options to the active
  `railmux ssh` connection instead of leaving an earlier “this time” choice in
  control until restart.

## [0.2.17] - 2026-07-29

### Changed

- Enlarge the website's New Project, Help, and Session Menu recordings into
  readable full-width rows, and demonstrate the session menu over a real Codex
  history-preview pane instead of an empty full-width sidebar.

### Fixed

- Check for local Railmux updates before `railmux ssh` connects, preserving the
  complete SSH command when an accepted update restarts the client. Internal
  remote display servers remain non-interactive and never check for updates.

## [0.2.16] - 2026-07-29

### Added

- Let `railmux ssh` users drag within one visible Claude Code or Codex pane to
  highlight and automatically copy text on the local machine without entering
  tmux copy-mode; ordinary clicks and double-clicks remain remote UI actions.
- Add a real, non-destructive Codex session context-menu recording to the
  product website, balance the existing Claude/Codex demonstrations, and
  document local SSH selection across macOS, Linux, and WSL.

### Changed

- Establish saved wide single- or dual-agent pane boundaries at their final
  proportions before Urwid's first frame, then fill them through the existing
  validated restore path. Startups with no saved visible agent still open
  directly into the full-width sidebar.
- Make the website's session-resume cue visibly animate both clicks, and stop
  presenting keyboard-only Quit confirmations as mouse-clickable actions.
- Streamline the README quick start around prerequisites, installation, and
  SSH; add macOS-friendly user-script `PATH` recovery and explain that
  persistent first-run choices remain editable in **Options**.

### Fixed

- Collapse same-project Codex rewind forks into one current session row while
  retaining every provider UUID for exact recovery, resume, status, rename,
  favorite, and deletion operations.
- Treat a live Codex writer's newest rewind fork as the same persisted running
  session during soft restart, instead of degrading a resolved binding to
  `unresolved` or allowing a duplicate process to start.
- Keep local `railmux ssh` drag-copy feedback in status-right so it no longer
  erases the Railmux, mode, and layout controls on the left.
- Suppress transient full-width sidebar and row-focus flashes while a saved
  agent workspace restores; failed prelayout removes only Railmux-owned empty
  panes and retains the established recovery fallback.
- Honor direct website section links such as `/#install` after the React
  application mounts, with fixed-navigation spacing and browser regression
  coverage.

## [0.2.16.dev202607291] - 2026-07-29

### Fixed

- Keep local `railmux ssh` drag-copy feedback in status-right so it no longer
  erases the Railmux, mode, and layout controls on the left.
- Suppress the transient sidebar row-focus flash while a soft restart restores
  an agent-focused workspace.

## [0.2.16.dev202607290] - 2026-07-29

### Added

- Let `railmux ssh` users drag within one visible agent pane to highlight and
  automatically copy text on the local machine without entering tmux
  copy-mode; ordinary clicks and double-clicks remain remote UI actions.

### Changed

- Make the website's session-resume cue visibly animate both clicks, and stop
  presenting keyboard-only Quit confirmations as mouse-clickable actions.
- Streamline the README quick start around prerequisites, installation, and
  SSH; add macOS-friendly user-script `PATH` recovery and explain that
  persistent first-run choices remain editable in **Options**.

### Fixed

- Collapse same-project Codex rewind forks into one current session row while
  retaining every provider UUID for exact recovery, resume, status, rename,
  favorite, and deletion operations.
- Treat a live Codex writer's newest rewind fork as the same persisted running
  session during soft restart, instead of degrading a resolved binding to
  `unresolved` or allowing a duplicate process to start.
- Honor direct website section links such as `/#install` after the React
  application mounts, with fixed-navigation spacing and browser regression
  coverage.

## [0.2.15] - 2026-07-28

### Added

- Make the tmux 3.4+ bottom status bar actionable: switch provider Mode,
  rotate Layout, change compact pages, and copy the complete right-side
  status message with native macOS, Wayland, X11, or WSL clipboard support
  plus a bounded OSC 52 fallback.
- Offer persistent or one-run Claude Code wheel-history choices over
  `railmux ssh`, allowing users to choose Railmux's smooth local transcript
  or Claude Code's native clickable history.
- Add `Copy title` to session context menus and aggregate live Codex subagent
  activity into the visible parent session instead of exposing duplicate
  rollout sessions.

### Changed

- Advance the private SSH display protocol to v11 for scoped history choices
  and clipboard forwarding, while suppressing redundant mismatch errors after
  guided version handling.
- Restore saved wide dual-agent workspaces at their final pane geometry before
  filling agent content, reducing startup reflow without changing session
  ownership or recovery fallbacks.
- Refresh the product website, recordings, and repository front page with
  source-authentic startup, session-switching, responsive-layout, and
  Soft Quit demonstrations.

### Fixed

- Restore agent-pane opening on Railmux's dedicated tmux server by preserving
  the exact inherited socket through nested attach; failed display clients now
  clean up safely and report the transport reason without stopping the agent.
- Keep Mode, Layout, compact-page, and status-copy clicks correctly scoped
  across editors, modals, focus changes, and tmux's user-range size limit.
- Preserve resumed Codex parent bindings while background rollouts remain
  active, and avoid repeated UI-thread process walks during status discovery.
- Keep failed Claude history preference writes from replacing the active
  runtime choice, and hide the unavailable-transcript pseudo-status before a
  new Claude session writes its first record.
- Validate wheel artifacts in an isolated dependency prefix without modifying
  the invoking development or CI environment.

## [0.2.15.dev202607284] - 2026-07-28

### Changed

- Restore a saved wide dual-agent workspace by creating its final sidebar,
  Pane 1, and Pane 2 geometry in one tmux command queue before filling either
  agent pane, reducing visible startup reflow without changing session
  ownership or recovery fallbacks.
- Slow the website workspace-control recording so each Mode, Layout, Quit, and
  Soft Quit input remains readable before the next action.

## [0.2.15.dev202607283] - 2026-07-28

### Added

- Complete explicit clipboard requests from `railmux ssh` with the local
  operating-system writer on macOS, Wayland, X11, or WSL, retaining bounded
  OSC 52 as a fallback when no native helper is available.

### Changed

- Align every session context-menu shortcut in one column and show successful
  status-copy acknowledgement in the clickable right status area with its own
  transient colour before restoring the exact copied message.
- Make the website workflow launch a genuinely empty second conversation,
  keep the successful Soft Quit surface visible, and require the dual-agent
  exhibit to show its live Codex session in Running.

### Fixed

- Prevent wheel-package smoke validation from uninstalling dependencies in the
  invoking development or CI environment while it populates its isolated
  package prefix.

## [0.2.15.dev202607282] - 2026-07-28

### Added

- Make the bottom status bar actionable on tmux 3.4+: click the provider name
  to switch Mode, the layout glyph to rotate Layout, compact `R`/`1`/`2` to
  change pages, or the right-side tip/info/warning/error text to copy its
  complete untruncated source.
- Show transient click confirmation without replacing the current sticky
  status message, and explain when a modal must be closed before Mode or
  Layout can change.

### Fixed

- Restore agent-pane opening on Railmux's dedicated tmux server by carrying
  the exact inherited socket through nested attach instead of falling through
  to the user's default server.
- Reject a nested display client that exits during its startup settle, clean
  up dead `remain-on-exit` panes, and surface the exact transport failure
  reason while leaving the underlying agent session alive.
- Keep status clicks out of filter and rename editors by routing Mode and
  Layout through private function keys, and keep every user-range action
  within tmux's 15-byte limit so right-side copy works in real terminals.
- Add Mode and Layout controls to compact status bars, preserve readable
  compact error colours and exact right-message space, and reinstall the
  shared click binding safely when upgrading a live lease.
- Suppress the full-width `Local session transcript is not available yet`
  pseudo-status while a new Claude Code transcript has not written its first
  record.

## [0.2.15.dev202607280] - 2026-07-28

### Added

- Offer four first-scroll choices for Claude Code over `railmux ssh`: always
  or this-time smooth local history, and always or this-time native clickable
  history. Temporary choices survive automatic transport reconnects without
  changing the remote setting.
- Add `Copy title` to session context menus, including Unicode-safe clipboard
  forwarding through the SSH display protocol.
- Aggregate live Codex subagent rollout activity into its visible parent
  session status without exposing duplicate subagent sessions in the sidebar.

### Changed

- Advance the private SSH display protocol to v11 for scoped Claude history
  choices and remote session-title clipboard forwarding.
- Render deterministic website agent content with semantic terminal colours
  and verify the visible browser result instead of generator-specific ANSI
  constants.
- Avoid duplicate lint, pytest, and real-tmux jobs after the reusable
  cross-platform release test workflow has already passed.

### Fixed

- Preserve a resumed Codex parent binding when its completed rollout closes
  but the exact resume UUID remains in the live process argv and background
  rollouts are still open; ambiguous and unavailable identity probes continue
  to fail closed.
- Cache pane-aware procfs rollout correlation briefly and publish filtered
  subagent statuses from the background index, avoiding repeated UI-thread
  process walks and ineffective lookups into the visible-only index.
- Keep failed persistent Claude history choices from replacing the active
  runtime override, replay the original wheel only for native history, and
  suppress the redundant raw protocol-mismatch error after version handling.
- Isolate teardown tests from the developer's real tmux server and remove
  temporary implementation details from website and context-menu assertions.
- Install declared runtime and SSH-extra dependencies inside the wheel-smoke
  prefix, so the final publish gate validates `railmux ssh` without relying on
  packages inherited from an earlier test job.

## [0.2.14] - 2026-07-28

### Added

- Serve Claude Code history through Railmux's cached, read-only local
  transcript overlay when the provider's alternate screen has no tmux
  scrollback. Generic mouse-aware terminal applications retain their native
  wheel handling.
- Add a persistent Claude history preference to Options and the shared config.
  The default first upward scroll asks between Railmux's smooth local
  transcript and Claude Code's native clickable history.
- Add explicit SSH history capabilities for transcript-backed panes and
  remaining older rows, with a private protocol v10 upgrade boundary.
- Validate exact same-user Claude transcript locators without following final
  symlinks, and recover already-running pre-v10 sessions from Railmux-owned
  binding metadata.
- Show real compact `[R]` and `[1]` status-bar clicks in the product site's
  portrait-phone recording.

### Changed

- Hold the real `Restoring your workspace` hero frame long enough to read, and
  render demo-agent answer bodies with native-looking weight and inline-code
  colour instead of inheriting the title style.
- Cache up to 20,000 wrapped transcript rows per file identity and pane width;
  periodic 300-line prefetches reuse that cache while explicit deep requests
  refresh changed transcripts.

### Fixed

- Let `railmux ssh` keep managed Claude Code wheel input in its smooth local
  transcript by choice instead of unconditionally opening the provider's
  laggier history view and showing its `Ctrl+End … Bottom` prompt.
- Preserve stable cumulative history suffixes as the client expands from its
  hot cache toward the configured limit, and report completion explicitly
  rather than treating every short page as exhausted.
- Bound remote history-preference confirmation waits, preserve another pane's
  frozen history while policy routes refresh, and suppress both halves of a
  locally handled dialog click across reconnect.

## [0.2.13] - 2026-07-28

### Added

- Add separate single-agent, dual-agent, and product-entry-point website
  recordings. The guided flow now dwells on stopped-session preview before
  resume, while New Project and Help are shown from the real Railmux UI.
- Add an audited Codex source-analysis capture and a sixth website recording
  that demonstrates More, provider Mode, F8 Layout, and the real
  Quit/Soft Quit confirmation without stopping any session.
- Explain on the product site that Railmux discovers interactive Claude Code
  and Codex history created outside Railmux while filtering `codex exec` and
  subagent rollouts.
- Add a responsive product website for Railmux, including deterministic
  desktop and compact-workspace previews, automated browser screenshots, and a
  GitHub Pages deployment workflow.
- Add a credential-free website recorder that launches the real Railmux UI in
  an isolated tmux server, publishes its asciicast through an embedded terminal
  player, and regenerates the recording during every Pages build.
- Add a source-authentic compact recording for the small-screen product
  section, recorded through Railmux's real responsive layout and page controls.
- Add a dedicated 160×38 guided desktop workflow with durable keyboard labels,
  a cell-aligned mouse pointer, and two audited real Claude Code source-analysis
  runs captured without provider session persistence. It demonstrates
  single-click history preview, Enter resume, and return through Running;
  compact projection appears only in the phone demo.
- Let `+` and `-` expand and collapse the Button Bar's secondary action row
  directly from the keyboard.
- Show a one-time local info message when an SSH history viewport reaches the
  complete session history or its configured local line limit.

### Changed

- Advance the private SSH display protocol to v9 so history snapshots can
  advertise whether an empty pane safely accepts application-level wheel
  input.
- Rework the deterministic agent pane around Claude Code's native
  prompt/response/input structure, switch the compact demo to a representative
  46×38 portrait-phone geometry, and loop control-free recordings after a held
  final frame.
- Begin the hero recording with Railmux's real `Restoring your workspace`
  surface, include Claude Code's recorded version/model/cwd identity block,
  and distinguish Claude Code and Codex in the dual-agent demo.
- Clarify that Ask Railmux opens a dedicated read-only support session in
  Railmux's own help workspace, and document provider/layout controls plus the
  distinct Detach, Soft Quit, and Quit lifecycle choices on the product site.
- Refresh the terminal UI hierarchy with right-aligned sidebar and project
  counts, stable uppercase section labels, and responsive neutral shortcut
  controls in the clickable Button Bar.
- Flatten the sidebar to horizontal section rules without decorative vertical
  rails, tighten session-title spacing, and use smaller status glyphs without
  changing their lifecycle colours or meanings.
- Raise the default local `railmux ssh` history cap from 5000 to 10000 lines;
  explicit `[ssh].history_lines` and CLI overrides remain unchanged.
- Keep the product-site terminal previews aligned with the real flat sidebar,
  shared tmux dividers, Hint Bar, Button Bar, status bar, compact controls,
  Running section, and history default.
- Replace the product site's illustrative hero and feature mockups with real
  desktop and mobile terminal captures, surface recorded keyboard actions in
  the guided workflow, and clarify the remote-to-local fast SSH data flow.
- Give the hero, sidebar evidence, guided workflow, and mobile recording
  distinct jobs instead of replaying the desktop sequence in multiple places.

### Fixed

- Forward wheel input to a mouse-aware agent such as Claude Code when its
  alternate screen has no tmux scrollback, while retaining Railmux's local,
  copy-mode-safe history path for ordinary agent panes.
- Clear every synthetic Claude input row before repainting it so the website
  recording shows one separator above and below the bare prompt instead of
  stale horizontal-rule fragments on the input line.
- Repaint deterministic agent demos on terminal resize so switching to a
  dual-agent layout cannot leave full-width prompt rules reflowed across panes.
- Suppress tmux's default right-click menu over Railmux agent panes while
  retaining context-menu forwarding in the mouse-aware sidebar and preserving
  the user's original binding in unrelated tmux windows.
- Leave one trailing cell after right-aligned status text so info, warning,
  error, and tip messages do not visually touch the terminal edge.
- Remove build-host names, private tmux labels, and truncated temporary paths
  from generated website casts while rejecting incomplete scripted recordings.
- Reject credential-like transcript content and test the real responsive
  presentation policy so a desktop website recording cannot silently regress
  to compact mode.

## [0.2.12] - 2026-07-27

### Changed

- Let deep `railmux ssh` history pages tolerate a small number of changing
  agent status rows while retaining strict pane, geometry, generation, and
  unique-anchor checks against unrelated or ambiguous content.
- Start a missing remote Railmux session for `railmux ssh` without the older
  tmux copy-mode scroll coalescer; the fast client's local history layer remains
  the sole owner of agent wheel input. Ordinary `ssh` followed by `railmux` is
  unchanged.
- Make the wheel smoke verifier discover both `site-packages` and Debian-style
  `dist-packages`, including pip's `local/` prefix layout.

### Fixed

- Retry a deep history page on a later upward wheel gesture when its response
  was lost under display backpressure, while rejecting any late response from
  the expired request.
- Invalidate stale agent history routes immediately when keyboard navigation
  in the Railmux sidebar or the default tmux prefix can switch the displayed
  session, without adding work to ordinary agent input or terminal focus
  reports.
- Forward vertical and horizontal wheel events on the compact status row
  without cancelling frozen agent history viewports; genuine status-row
  clicks continue to change pages normally.

## [0.2.12.dev202607270] - 2026-07-27

### Changed

- Let deep `railmux ssh` history pages tolerate a small number of changing
  agent status rows while retaining strict pane, geometry, generation, and
  unique-anchor checks against unrelated or ambiguous content.

### Fixed

- Retry a deep history page on a later upward wheel gesture when its response
  was lost under display backpressure, while rejecting any late response from
  the expired request.
- Invalidate stale agent history routes immediately when keyboard navigation
  in the Railmux sidebar or the default tmux prefix can switch the displayed
  session, without adding work to ordinary agent input or terminal focus
  reports.
- Forward vertical and horizontal wheel events on the compact status row
  without cancelling frozen agent history viewports; genuine status-row
  clicks continue to change pages normally.

## [0.2.11] - 2026-07-24

### Added

- Let `railmux ssh` continue loading older agent history in cumulative
  2000-line pages. The local cap defaults to 5000 lines and can be set to
  2000-20000 with `[ssh].history_lines` or a one-connection
  `--history-lines` override.

### Changed

- Advance the private SSH display protocol to v8 for bounded 20000-line
  history requests. Deep captures preserve the visible anchor and stop safely
  at the styled-response byte boundary instead of failing the remote helper.
- Make `railmux ssh` history scrolling move one line for isolated touchpad
  ticks, then accelerate progressively only during a continuous same-pane,
  same-direction gesture.

### Fixed

- Prevent transitional touchpad wheel events and agent-border scrolling in
  `railmux ssh` from leaking into tmux copy-mode and consequently freezing both
  panes through dual-agent selection isolation.

## [0.2.11.dev202607240] - 2026-07-24

### Added

- Let `railmux ssh` continue loading older agent history in cumulative
  2000-line pages. The local cap defaults to 5000 lines and can be set to
  2000-20000 with `[ssh].history_lines` or a one-connection
  `--history-lines` override.

### Changed

- Advance the private SSH display protocol to v8 for bounded 20000-line
  history requests. Deep captures preserve the visible anchor and stop safely
  at the styled-response byte boundary instead of failing the remote helper.

### Fixed

- Prevent transitional touchpad wheel events and agent-border scrolling in
  `railmux ssh` from leaking into tmux copy-mode and consequently freezing both
  panes through dual-agent selection isolation.

## [0.2.10] - 2026-07-23

### Added

- Add opt-in `railmux ssh --reconnect`. After an established display loses its
  connection unexpectedly, the client keeps the last frame visible and retries
  ordinary non-replacement attaches for up to 60 seconds. Reconnect remains
  locally cancellable, uses non-interactive authentication, and never retries
  detach, soft quit, hard quit, or a deliberate local disconnect.
- Add `railmux doctor --json`, a versioned machine-readable rendering of the
  same privacy-safe diagnostic snapshot used by the human report.

### Changed

- Move responsive sidebar, dual-pane fit, and terminal-size classification into
  pure workspace policy functions, leaving tmux commands and lifecycle
  authority in the application controller.
- Install and exercise each built wheel from an isolated offline prefix during
  normal and release CI, including the console entry point, SSH extra import,
  and structured doctor output.
- Consolidate compact/mobile behavior as a completed foundation in the design
  roadmap and document automatic reconnect and diagnostic privacy invariants.

## [0.2.9] - 2026-07-23

### Fixed

- Reapply both responsive dividers when a wide terminal changes size. Returning
  from compact mode now keeps the sidebar and both agent panes at their active
  proportional layout instead of allowing tmux to retain one agent pane's old
  absolute width; stacked layouts receive the equivalent height correction.
- Make `[` and `]` move only the sidebar divider and rebalance the remaining
  dual-agent region evenly, instead of allowing a directional tmux resize to
  move the Agent 1/2 divider.
- Synchronize Projects and Sessions when mouse or tmux focus moves directly
  from one agent pane to the other, so the sidebar follows the newly targeted
  running session instead of updating only its Running highlight.

## [0.2.8] - 2026-07-23

### Fixed

- Keep the compact **Railmux**, **A1**, and **A2** status buttons clickable on
  long-running tmux servers whose pane IDs have multiple digits. Railmux now
  escapes pane IDs through tmux's status-line time formatting, and the real
  tmux regression test uses a multi-digit target without the stock status-click
  fallback.

## [0.2.7] - 2026-07-23

### Added

- Check PyPI once before a normal outer Railmux launch and offer
  **Always**, **This time**, **No**, or **Never** when a newer stable release
  exists.
  The persistent **Railmux updates** option uses **Always**,
  **Ask every time**, or **Never** in the shared `config.toml`; offline checks,
  failed installs, non-interactive launches, and editable source installs
  cannot prevent startup or overwrite source work.

### Changed

- Keep compatible `railmux ssh` upgrade prompts focused on the local and remote
  package versions; mention the SSH protocol only when the newer remote
  actually requires a different protocol.
- Let `Enter`, `Space`, or a click on an already-selected Options value confirm
  and close the screen instead of appearing to do nothing.
- Give the layout-retention and Codex auto-run prompts the same complete
  lifetime choice: one-time acceptance, persistent acceptance, one-time
  rejection, or persistent rejection.

### Fixed

- Reconcile focused-pane border cache state with tmux before verifying its
  colours, and restore a Focus-In event when `railmux ssh` re-enables terminal
  focus reporting after reconnect. Together these prevent an active agent pane
  and its input UI from remaining incorrectly gray.
- Deliver SIGWINCH after resizing the private `railmux ssh` tmux client and map
  transitional mouse input against the frame actually on screen. Compact
  status buttons therefore keep their correct bottom-row hit targets after a
  resize, and supported compact geometry now shows guidance instead of a
  misleading cramped-layout warning.
- Add an intermediate responsive view before compact mode: when a dual layout
  no longer fits, keep the sidebar and current Target agent attached while the
  other agent continues in Running, then restore both original slots and their
  split/stacked topology once space returns. Entering compact now reconstructs
  both physical agent panes first, including after a narrow soft restart, so
  every visible `R`/`A1`/`A2` page remains a real selectable target without
  stopping either agent.
- Snapshot the live wide-view divider ratios before compact zoom and replay
  both the sidebar and Agent 1/2 split when returning to a larger terminal.
  A missing snapshot falls back to safe 20% sidebar and 50/50 agent
  proportions instead of retaining the currently zoomed page's absolute width.
- Mark only the private `railmux ssh` tmux client as RGB-capable on tmux 3.2+,
  preventing live red/green diff backgrounds from collapsing to the same gray
  while leaving history capture and other attached terminals untouched.
- Keep compact status-page clicks working through `railmux ssh` after resize,
  local-history routing, and page changes by recognizing the painted
  navigation row as remote tmux chrome and invalidating stale pointer routes.
- Preserve exact live Running entries while a responsive single-agent
  projection temporarily hides one slot, fenced by immutable tmux session
  identity so a reused name can never be adopted.

## [0.2.6] - 2026-07-23

### Fixed

- Make the focused agent's green tmux border self-heal when a detach, reattach,
  or independent option restore leaves the real border styles out of sync with
  Railmux's cached focus state.
- Keep compact status-bar page clicks working on tmux 3.7 and newer. User
  status ranges intentionally have no implicit mouse pane target; the managed
  binding now evaluates in the current client/window context and selects only
  the explicit, validated `%N` range argument.
- Make the cross-platform real-tmux click test wait for the client-specific
  status range to paint and drive its PTY without relying on BSD `script(1)` or
  one negotiated mouse encoding.
- Gate PyPI publication on the reusable full test workflow, including the
  Python 3.9–3.13 matrix and real tmux integration tests on Linux and macOS.

## [0.2.5] - 2026-07-23

### Added

- Add a responsive compact workspace for phones and other terminals with fewer
  than 80 columns or 24 rows. It presents the existing sidebar and up to two
  agent panes as full-window `[R]`, `[1]`, and `[2]` pages, keeps F8 layout
  state intact, and returns to the wide workspace only after both dimensions
  clear a hysteresis threshold. Large portrait monitors therefore keep the
  normal multi-pane UI.
- Make compact status navigation clickable on tmux 3.4 and newer, with
  crash-safe shared binding ownership and exact restoration of a user's prior
  binding. `Ctrl-B Tab` remains the portable fallback on older tmux versions.
- Let `railmux ssh` survive a mobile soft keyboard temporarily reducing the
  reported terminal below 12 rows. The local client keeps the remote logical
  size stable and shows a bottom-anchored projection containing the status and
  input area, then repaints the full display when the keyboard closes.
- Offer to upgrade an older remote Railmux to the local version even when its
  private SSH protocol remains compatible; declining continues safely with the
  compatible helper.

### Fixed

- Preserve pre-existing F9 zoom across compact-mode transitions and full-sidebar
  Help or Options screens, and prevent compact modal geometry from overwriting
  a saved wide-layout profile.
- Keep compact status content within the available width, preserve zoom while
  switching pages, and avoid rejecting valid phone landscapes such as
  105 columns by 20 rows.
- Reject terminals narrower than 40 columns immediately instead of waiting
  indefinitely for a soft-keyboard recovery that cannot increase their width.

## [0.2.4] - 2026-07-22

### Added

- Add an explicit **Ask Railmux** action to Help. It opens or reuses a separate
  support session for the current provider with conservative permissions and
  local README context, while leaving the user's current agent alive. Static
  Help remains token-free, and the private help workspace stays out of normal
  Projects and Running views. Documentation reads now auto-run without approval
  prompts: Codex remains OS-sandboxed read-only, while Claude receives only its
  built-in read/search tools.

### Fixed

- Keep an explicitly selected two-agent layout when either provider (including
  Ask Railmux) exits by rebuilding that numbered slot as an empty launch pane,
  without moving the surviving agent. Re-adopt a displayed swap-owned agent
  into Running when only its in-memory row was lost, gated by exact pane,
  process, window, swap, session, and persisted-binding identity.
- Classify SSH soft quit from an exact pre-teardown intent so destroying the
  managed tmux session cannot be mistaken for an unexpected server loss or
  trigger closing remote views to query tmux concurrently with teardown.
- Make restored sidebar filters visible in section titles and pre-fill their
  `/` editor, provide actionable no-match text, and let `Ctrl-U` reliably clear
  Projects, Sessions, and Running filters. Refresh the idle-tip pool around
  non-obvious, high-value behavior instead of visible shortcut reminders.
- Prevent reported mouse drags over `railmux ssh` agent panes from entering
  tmux copy-mode accidentally while preserving clicks for focus, sidebar mouse
  behavior, terminal-native selection, and explicit `Ctrl-B [` copy-mode.

## [0.2.3] - 2026-07-22

### Added

- Add a full-sidebar Options screen, available with `o` or More → Options, for
  keyboard and mouse control of persistent layout-retention and Codex auto-run
  policies (`Always`, `Ask every time`, or `No`). The UI and manual edits share
  the single `config.toml`; app updates preserve comments and unknown keys.

### Fixed

- Let `railmux ssh` recover when remote policy rejects a per-user pip install:
  after a second explicit confirmation it creates a private managed venv,
  installs the matching SSH package there, and continues without sudo or
  system-Python changes.
- Size the layout-retention exit prompt from its actual wrapped content so
  narrow terminals keep every action visible.
- Charge More's optional second Button Bar row only to the bottom Running
  section, keeping Projects and Sessions stable when More/​Less is toggled.
- Clarify that Soft Quit closes the shared Railmux UI for all attached views;
  the quit prompt now warns when multiple terminals are attached, while native
  `Ctrl-B :detach-client -a` keeps the current terminal and detaches every other
  client without stopping agents. Clicking Detach with an ambiguous
  multi-client target now raises a prominent warning directing that terminal
  to native `Ctrl-B d`.

## [0.2.2] - 2026-07-22

### Added

- Save custom outer-workspace layout proportions after an explicit F8 or
  divider change. Exit choices are `Always`, `This time` (the next launch
  only), and `No`; profiles are versioned, contain no tmux identities, scale to
  the next terminal size, and fall back without overwriting a good preference
  when a split cannot fit. Codex auto-run now offers parallel persistent,
  current-run, and safe-off choices.
- Replace the always-visible Mode control with a responsive More/Less Button
  Bar. Its optional second row currently exposes Mode and F8 Layout while the
  keyboard shortcuts remain global.
- Allow multiple current `railmux ssh` display helpers to view the same managed
  workspace. The shared tmux window uses `window-size=smallest`; helpers
  serialize only their validation/attach boundary and register their exact
  child client PID.

### Fixed

- Recover from half-open SSH connections without touching the Railmux session
  or agents. Protocol v7 adds a post-attach status and heartbeat lease; expiry
  stops only that helper's private tmux client. A bounded, explicitly confirmed
  replacement path is offered only after an ordinary v7 retry remains busy; it
  detaches clients holding the protocol-v6 lifetime lock, then revalidates the
  immutable managed session before attaching once.
- Avoid ambiguous clickable Detach behavior with multiple tmux clients by
  directing the user to `Ctrl-B d`, which tmux can scope to the issuing client.

## [0.2.1] - 2026-07-21

### Added

- Productize `railmux ssh` startup with a protocol-v6 compatibility handshake
  that completes before the remote helper attaches to tmux. A missing or older
  remote Railmux can be installed into the remote user environment, with
  explicit local consent, using the first usable Python/pip pair and the exact
  local package version plus the `ssh` extra; Railmux never invokes `sudo` or
  installs tmux. A newer remote version instead offers to upgrade the local
  installation through its current Python and restarts the original command.
  Compatible package versions may differ when their private protocol matches,
  while failures print exact manual recovery commands.
- Add low-frequency, identity-pinned tmux watchdogs outside both the ordinary
  attach client and SSH attach client. Three consecutive failures
  restore the terminal, stop only the owned client, and persist a privacy-safe
  incident for `railmux doctor`; tmux, apport, provider processes, and rollout
  files are never killed or modified automatically. Both observers use a
  short-lived, exact, one-shot clean-exit marker to distinguish intentional
  hard quit from an abrupt tmux disappearance.
- Add a `railmux ssh HOST` latest-state display transport with
  compressed row patches, dynamic resize, native tmux keyboard and SGR mouse
  forwarding, a periodically refreshed 300-line pane-history hot cache with a
  2000-line background fill, safe SGR colour preservation, synchronized
  bracketed-paste/focus-event modes, safe local `Ctrl-]` disconnect, automatic
  startup of the default remote Railmux session, and
  detach/soft-quit/hard-quit classification. Its internal `railmux
  remote-server` entry point and protocol remain private.

### Fixed

- Let each agent pane in the SSH display retain its own immutable
  local-history viewport. Live patches are now painted continuously and
  composed with every frozen pane in one terminal update, so the sidebar and
  unfrozen agents remain live; changing focus, typing, or reaching the bottom
  restores only the affected pane, while `Esc` and layout changes still restore
  all panes safely. Deep-history responses anchor only to a unique exact match
  of the visible lines instead of jumping when remote output advances.
- Keep the SSH display's headless terminal synchronized when
  tmux uses xterm's parameterized scroll-up, scroll-down, or
  repeat-character operations. The bounded pyte compatibility layer now
  implements `CSI S`, `CSI T`, and `CSI b` for both live frames and styled
  history; a real isolated tmux PTY regression compares the reconstructed
  pane with `capture-pane` after the previously dropped scroll operation.
- Accept and safely ignore private device-status queries emitted by tmux on
  macOS, avoiding a pyte 0.8.2 dispatch error before the first screen frame.
- Isolate all Railmux workspaces and `railmux ssh` server-side tmux commands on
  a dedicated non-default socket, including launches from an outer tmux. The
  internal entry point now validates the full Unix socket identity, startup
  recovery is explicitly scoped to that server, fast-display locks include the
  socket identity, and inherited foreign `TMUX` metadata cannot route Railmux
  commands into the user's default server.
- Keep pre-isolation Railmux sessions from tmux's historical default server in
  the same Running sidebar after upgrade. They are discovered read-only,
  labelled with a restart recommendation, displayed through an identity-pinned
  nested client with `ignore-size`, preserved by automatic/hard-exit cleanup,
  and killed only after an explicit user action revalidates the old server PID
  and immutable tmux session ID.
- Restore local SSH history prefetch for nested and legacy displays by reading
  scrollback from the exact revalidated source pane while retaining the outer
  wrapper's screen geometry. Ordinary cross-agent focus clicks now preserve the
  warm 300-line cache instead of briefly routing wheel input back through tmux.
- Route sidebar wheel events directly to Railmux instead of batching them
  behind a rejected pane-history request, keep agent scrolling exclusively in
  the local history layer, and prevent reported clicks or drags from discarding
  a visible history viewport.
- Make SSH history routing zoom-aware and generation-gated, so
  F8/F9, Help, modal transitions, resize, and late history responses cannot
  target hidden/stale pane rectangles. Cross-pane/sidebar clicks now repaint
  and forward normally, wheels cannot move a non-hovered history pane, and
  short remote-only wheel bursts are bounded without changing ordinary
  Railmux tmux bindings.
- Keep text selection stable when the other agent pane is producing frequent
  output. Entering tmux copy-mode by mouse or `Ctrl-B [` now freezes only the
  sibling agent pane's display until selection ends; its process and PTY output
  continue normally, and the sidebar is never frozen.

## [0.2.0] - 2026-07-20

### Changed

- Document terminal-side right-click and F8/F9 forwarding, including the iTerm2
  Pointer setting required for Railmux's context menu.
- Wrap quit-confirmation choices to stay readable in a narrow sidebar, and
  document the bottom-left layout/Target-pane indicator in Help and README.
- Preserve idle agent sessions created by Railmux 0.1.3 and earlier, before
  durable tmux identity markers:
  conservatively migrate only detached, single-pane sessions whose immutable
  tmux identities, historical Railmux name, cwd, and launch command all agree,
  then install the current v2 marker plus compatibility binding for subsequent
  soft restarts and exact resolved-ID promotion.
- Give the two-line Sessions list half of the sidebar's vertical allocation,
  changing the Projects / Sessions / Running weights from 2:3:2 to 2:4:2.
- Replace the relative-age prefix on live Sessions rows with their current
  `idle`, `busy`, or `blocked` state, with actionable aborts shown as `aborted`.
- Replace branch and file-size metadata in both Claude Code and Codex session
  rows with compact logical-message and token counts. Keep this second line
  visually secondary and non-bold even while its row is focused or selected.
- Exclude tool results, harness-injected prompts, and duplicate Claude
  streaming records from logical-message counts; deduplicate Claude usage by
  provider message and include reported cache creation/read tokens.
- Add a second agent pane through `F8`, which can create an empty
  Pane 2 and cycles single, side-by-side, and stacked layouts globally while
  keeping the collapsed Pane 2 agent running. Any split orientation that cannot
  meet the minimum pane size is skipped, so the cycle uses only the layouts the
  current window supports. Empty agent panes now use a centered, resize-aware
  Railmux surface with compact interaction guidance; startup restoration uses
  the same visual language.
- Keep the sidebar at roughly 30% in single-agent layout and compact it to 20%
  in either dual-agent layout, with a 30-column floor. Returning to single
  restores the wider navigator, and ratio updates remain best-effort.
- Align `␣` and right-click Preview with single-click: preview stopped sessions,
  but switch/attach running sessions while sidebar focus stays put. Double-click
  and Enter open in the agent pane remembered from tmux focus and transfer
  focus. While the sidebar is active in a dual layout, agent borders return to
  honest gray and the status brand's compact workspace map identifies the
  exact neutral Target pane.
  Single-agent sidebar focus also uses a continuous gray divider, removing a
  stale per-pane target format that could leave half the line dim green after
  restart.
- Show a persistent one-cell workspace map after the provider name: `▣` for
  single, `◧`/`◨` for side-by-side, and `⬒`/`⬓` for stacked. The filled half
  identifies the Target pane across focus changes, including direct mouse
  movement between P1 and P2 without returning through the sidebar.
- Add `Ctrl-B Tab` as a direct Sidebar/Target-pane toggle so keyboard users can
  return from Pane 2 without passing through Pane 1 and changing the Target.
  Preserve any existing prefix-Tab binding outside Railmux, and make agent hints
  follow left/right side-by-side or up/down stacked geometry. Bindings that
  cannot be replayed faithfully are left untouched without disabling F8/F9.
- Establish **Target pane / 目标窗格** as the canonical name for the remembered
  agent pane where sidebar actions take effect, distinct from the **Focused
  pane / 焦点窗格** that currently receives keyboard input. The workspace model
  uses `target_slot_key`, `target`, and `set_target()` consistently; the
  previously released `active*` names remain compatibility views only.
- Disambiguate the shared green border in the side-by-side layout with inward
  tmux arrows that point at the exact focused agent pane. Directional markers
  are limited to agent focus, restore the prior window option on teardown, and
  degrade to colour-only borders on tmux versions older than 3.3. When Pane 1
  has focus, the hint bar shows `C-b → Pane 2`; Pane 2 names the matching
  `C-b ← Pane 1` route instead of calling it a direct return to Railmux.
- Retry partial tmux focus-border and directional-indicator updates during the
  normal refresh loop instead of caching them, preventing stale or missing
  green focus borders and old half-gray/half-green single-pane dividers.
- Resolve the Target pane from real tmux focus (including the last pane
  when returning to the sidebar). F9, transcript preview, terminal placement,
  status/attention targeting, scrolling, and soft-restart display selection no
  longer silently default to Pane 1. Moving directly between agent panes now
  briefly confirms `Agent Pane 1 focused` or `Agent Pane 2 focused`.
- Reconcile liveness and outer-pane disappearance across both slots and both
  providers. A lost Pane 2 collapses or rebuilds safely; if Pane 1 disappears,
  Railmux returns Pane 2 home before rebuilding or promoting its surviving
  agent, preserving slot-specific swap ownership.
- Manage the server-global F8/F9 wrappers as a crash-safe, multi-instance
  transaction. They forward only in Railmux windows, preserve prior behavior
  elsewhere, restore exact per-key originals on final teardown, and defer to
  any newer user tmux configuration.
- Restore the complete exact-owner agent workspace after a soft restart:
  layout, both validated pane contents, Target pane, keyboard focus, preview
  rollback target, and a collapsed secondary agent. Portable state remains a
  single stable display wish with no tmux process authority; invalid content or
  newly constrained geometry degrades to branded empty or single-pane UI while
  live agents remain discoverable in Running. Graceful restarts of the managed
  `railmux` tmux session now explicitly hand this snapshot to its replacement
  controller pane, whose immutable pane ID necessarily changes on relaunch.

### Fixed

- Reconcile terminal focus reports with tmux's actual active pane on every
  refresh, preventing a delayed `focus in` after a Pane 2 open/new-session
  action from leaving every agent border gray.
- Route right-click through a crash-safe, Railmux-window-only tmux wrapper that
  first selects the pane under the pointer, so an unfocused sidebar can open
  its context menu. Preserve and restore the exact prior right-click binding
  everywhere else.
- Size Rename from its wrapped title, keep modal action legends visible, make
  information popups scrollable, and clamp every overlay inside cramped
  sidebar dimensions.
- Keep each key-and-action hint together on one auto-flip page instead of
  separating combinations such as `C-b ←` from their destination.
- Route every displayed-session kill through the display transport, including
  ordinary resolved sessions. Swap panes now return home and nested clients
  detach before the exact tmux session is killed; the affected slot remains in
  the chosen dual-pane layout as a usable empty pane, failed kills stay in the
  Running registry, and stale display markers can no longer cascade errors.
- Quote and expand the controller pane correctly in the global F8/F9 tmux
  wrapper, preventing `-t expects an argument` when cycling layouts.

## [0.1.3] - 2026-07-17

### Changed

- Replace the three stacked sidebar boxes with labelled horizontal section
  rules inside one pair of shared vertical rails, reclaiming two rows while
  preserving pointer-local wheel routing. The focused section owns green upper
  and lower rules plus matching segments on both rails; focus changes no longer
  shift section heights, and narrow layouts keep every stable section name
  visible. Inactive section names and rules share one subdued gray when focus
  moves to the agent, while pinned-row separators remain secondary chrome. A
  shared lower boundary does not recolour the next section's title. Green corner
  glyphs join focused rail segments to their horizontal boundaries without
  visually overrunning them. Neutral outer corners and internal junctions also
  close the inactive frame cleanly, and the final rule uses the same inactive
  gray.
- Give modal action keys one shared high-contrast treatment across rename,
  quit, info, auto-run, help, path-browser, kill, and delete workflows. Rename
  now accepts `Ctrl-U` to clear a non-empty title without closing the popup,
  and visible Enter labels use the compact `↵` symbol.

### Fixed

- Preserve the active tmux pane during swap-transport moves, so a single click
  on a Sessions row no longer returns keyboard focus to the agent pane while
  previewing or attaching the selected session.

## [0.1.2] - 2026-07-17

### Added

- Add in-memory Running-pane filtering with plain fuzzy search, an optional
  `project:<name>` restriction, provider-aware empty states, per-mode queries,
  and exact tmux-identity focus retention across refreshes and sorting.
- Persist a bounded, versioned tmux marker before each new provider process
  starts. If Railmux exits before the provider exposes its UUID, restart now
  restores an explicit unresolved Running entry whose exact pane can be opened
  or stopped without guessing at or deleting provider history.
- Split soft-restart persistence into a portable per-mode sidebar view and
  exact-owner runtime recovery files, including isolated real-tmux coverage for
  multiple windows, sessions, and same-named sessions on private servers.
  On the one-time upgrade from the ownerless legacy file, only view preferences
  migrate; recovery bindings remain untouched and are not treated as authority.
- Add a source-tree-only, repeatable private-tmux benchmark for direct, nested,
  and swap output pipelines, A/B server-side switch timing, aggregate Linux CPU
  ticks, and diagnostic scroll-scheduling models. Document raw local results
  and their strict limitation: marker observation is not terminal paint or a
  real-provider/SSH measurement.
- Add a de-nested agent display transport using
  transactional cross-session pane swaps, durable tmux recovery markers, and a
  zero-extra-pane session-group keeper. It returns real panes before preview,
  close, quit, or delete; repairs interrupted swaps; preserves agents across a
  direct outer-session kill; and falls back to nested attach for independent
  clients, unsupported topology, unmanaged sessions, or failed validation.
- Extend isolated real-tmux smoke coverage with swap/home, A/B switching,
  direct outer-session kill recovery, and independent-client fallback. The
  implementation path is verified on Linux with tmux 2.7 and 3.4 and remains in
  the existing Linux/macOS CI matrix.
- Added a provider-derived attention state independent of tmux liveness and
  idle/busy/blocked activity. Sessions and Running rows use a separate `!`
  badge, info popups show sanitized details, and active errors receive a concise
  retry-aware status message without changing attach/preview actions.
- Added provider-neutral mode and at-most-two-slot agent-workspace foundations,
  plus internal architecture/roadmap guidance for future providers and dual
  agent panes. Current releases still expose the original single-agent layout.
- Warn when the outer workspace is below the recommended 120x30 layout size, or an
  individual agent pane is below 80x20, with stronger non-blocking warnings
  below 80x20 and 50x12 respectively.
- Missing-`tmux` startup checks now offer an explicit, default-no installation
  prompt for Homebrew on macOS and `apt-get` on Debian/Ubuntu/WSL. Other common
  Linux package managers receive an actionable manual command, while
  non-interactive launches never attempt to modify the system.
- Add `railmux --doctor`, a privacy-safe diagnostic report for provider,
  terminal, tmux, configuration, and data-directory health that works even
  when tmux is unavailable.
- Add provider-aware project/session onboarding text and non-blocking,
  path-safe warnings when the active mode's executable is unavailable.
- Add an isolated real-tmux smoke test on Linux and macOS CI, alongside Ruff
  lint and package build validation gates.

### Changed

- Make the validated `swap` display the default for managed Railmux sessions;
  `nested` remains an explicit compatibility choice and automatic safe fallback.
- Show an immediate startup surface while initial provider and tmux discovery
  runs, reuse the already-built project snapshot during orphan recovery, and
  avoid leaving a newly-created terminal pane apparently blank.
- Size destructive confirmation dialogs from their wrapped content, cap long
  bodies to a scrollable viewport, and render their action keys with an
  explicit high-contrast style.
- Make the one-line Button Bar responsive at narrow sidebar widths and paint a
  short pressed state before synchronous actions, so remote clicks receive an
  immediate visual acknowledgement without adding another focusable widget.
- Keep mode switching in the Button Bar and remove its duplicate `m Mode`
  entry from the context-sensitive Hint Bar; the `m` keyboard shortcut remains.
- Clarify the final `railmux --doctor` privacy note and remind users to review
  the redacted report before sharing it.
- Group blocked Running sessions ahead of other activity states during the
  existing throttled recency sort, without changing status-dot semantics or
  causing per-poll row movement.
- Move Codex history tree walking and rollout parsing off the UI thread into a
  single rate-limited worker. Sidebar refreshes now read immutable generation
  snapshots, coalesce repeated requests, retain the last good view on scan
  failure, and bound shutdown even when filesystem IO is stuck.
- Raise copy-mode wheel coalescing from 2 FPS to 10 FPS over SSH while keeping
  the immediate leading update, native scroll distance, and both nested and
  swap transport lifecycles unchanged.
- Use one grass-green focus system (`#5FAF00`): bright pane chrome and tmux
  status bar, a deep-green cursor row, a neutral slate persistent target, and
  grass-green live-session titles. Red/yellow/green status dots retain their
  meaning across cursor and target backgrounds, while stopped sessions use a
  neutral hollow marker. True-colour terminals receive the exact accent and
  other terminals use an automatically downsampled fallback.
- Use provider-neutral product copy throughout the shared UI and expand the
  README with status badges, a quick-start path, diagnostics guidance, and a
  reserved demo-GIF slot.

### Fixed

- Resolve a tmux topology target back to its actual session name when callers
  use an immutable `$id`, so a recovered marked Running entry is not falsely
  rejected as having changed identity.
- Remove the obsolete in-pane error row above the Button Bar. Errors now use
  the full-width tmux status bar exclusively, like warnings, tips, and other
  status messages, without resizing the sidebar footer.
- Keep the tmux server lifetime identity stable when its socket metadata is
  touched by a later client, and safely migrate exact legacy markers on the
  same live server. Soft restart no longer hides a surviving resolved Claude
  session from the Running pane.
- Paint a clicked session as the sole active sidebar target before beginning
  the synchronous agent transport transaction, so the previous session cannot
  linger as a second grey selection. Failed attaches restore the confirmed old
  target or reconcile to the transport's retained recovery state.
- Restore the most recently displayed stable agent session or transcript after
  a soft restart even when the outer tmux pane is recreated. Portable state
  carries only provider/session/project view identity: live processes must be
  rediscovered and validated locally, otherwise Railmux opens a read-only
  preview and never resumes or launches a provider implicitly.
- Keep double-click intent intact when a Sessions row redirects through an
  already-running entry, preventing the delayed right-pane focus transfer from
  being cancelled and bouncing back to the sidebar.
- Recreate a failed scroll helper against the exact displayed pane in swap
  mode, and restore copy-mode wheel bindings per key so a user tmux reload is
  preserved without leaving other wrappers pointed at a dead helper.
- Keep delete/kill confirmation controls visible for long ASCII or CJK session
  names by showing the name once in a scrollable body, pinning the action keys,
  and allocating more vertical space in the narrow sidebar.
- Deliver both macOS trackpad and mouse-wheel directions to every scrollable
  sidebar list, even when the pointer is over pane chrome or keyboard focus is
  elsewhere. Server-global tmux bindings are shared crash-safely, installed
  only over stock behavior, and restored without overwriting later user config.
- Keep an `Exiting…` progress surface visible while synchronous tmux cleanup
  runs, and split teardown into idempotent core/outer phases so the sidebar no
  longer disappears before the agent pane or repeats destructive cleanup.
- Preserve exact Codex sessions in Running across a soft restart while the
  background history index publishes its first generation. Startup recovery
  now pins one immutable generation, shows exact provisional entries instead
  of a false empty list, and revalidates them without dropping them on transient
  index/tmux failures or temporary rollout visibility delays.

- Close the crash window in which a new provider could outlive Railmux before
  receiving recovery metadata. Placeholder resolution now uses Linux rollout
  file-descriptor correlation when available, stays unresolved on ambiguity,
  commits the exact UUID to tmux before re-keying memory, and revalidates
  immutable tmux identity before unresolved attach or kill actions.

- Prevent simultaneous Railmux instances from overwriting or restoring one
  another's right pane and running bindings. Local state is namespaced by the
  tmux server lifetime and immutable outer pane, atomically written with
  restrictive permissions, and stale cleanup removes only owners proven dead.

- Read the containing tmux window rather than the narrow Urwid sidebar when
  evaluating workspace dimensions, so a restored split no longer reports a
  full-screen terminal as critically small. Rechecks are resize-event driven
  instead of adding a tmux query to every poll tick.
- Paint both tmux border styles together so the two-pane shared divider changes
  as one continuous line instead of showing only its lower half in focus green.
- Pre-size detached agent tmux windows to the exact outer pane dimensions before
  attach, preventing an immediate Codex resize from visibly replaying/reflowing
  long history when switching running sessions.
- Check for the `tmux` executable before every TUI startup path, including an
  inherited or explicitly forced inside-tmux launch, instead of entering a TUI
  whose controls cannot work when `TMUX` is set but the binary is absent.
- Remember each agent mode's project selection independently. Switching through
  a mode with no projects no longer leaves a hidden actionable project or loses
  the previous mode's Sessions view after the next refresh tick; deleted
  remembered projects fall back only to a currently visible project.
- Report malformed or invalid configuration as a concise actionable error
  instead of exposing a Python traceback.

## [0.1.1] - 2026-07-15

### Added

- New Project now works in Codex mode and can create missing relative,
  absolute, or `~`-based directories before launching the first session.
- Status bar now cycles short idle tips when there's no active message, and
  soft-wraps long messages across both lines (ellipsis only past two lines)
  instead of clipping at line one.
- Hint bar is now context-sensitive: it lists only the action keys valid for
  the focused sidebar pane (Projects / Sessions / Running), sourced from the
  keymap so it can't drift from dispatch. Project/session filtering also matches
  fuzzily instead of requiring a contiguous substring.

### Changed

- Stopped-session preview remains a zero-extra-dependency `less` viewer, but
  now identifies itself as read-only, documents its recent-record window,
  shows abbreviated Claude tool results and plaintext Codex reasoning
  summaries, and filters Codex-injected system context from user turns.
- History preview now sanitizes terminal control sequences, quotes the Python
  executable, disables `less` shell/editor/log/history features, and treats an
  early pager exit as a normal broken pipe instead of showing a traceback.
- Status-bar messages are levelled (info / warn / error) with distinct colours,
  and one-shot messages ("→ opened X", "Renamed to: …", "Killed: …") now persist
  for a level-dependent time (errors are sticky) instead of being overwritten by
  the next poll tick — fixing messages that previously flashed by unreadably.
- User-facing text now says "agent" instead of "Claude" where either agent may
  run (session counts, the right pane, error messages, tips, help) now that
  Codex sessions are supported; "Claude mode" / "Codex mode" toggle labels are
  kept as-is. Fullscreen toggle is F9 across the hint bar, help, and the tmux
  binding (previously the binding and the displayed key had drifted apart).
- Pane focus now follows the actual tmux input target: the sidebar drops focus
  styling while another pane is active, while the selected conversation and
  status colours remain visible. Shared tmux dividers now switch as one solid
  colour instead of mixing active and inactive segments.
- Removed the redundant `[LIVE]` badge; running state, status dots, and relative
  activity time remain the session activity indicators.
- Project and running-session single clicks now act immediately; initial session
  metadata loading, right-pane restoration, and scroll-acceleration setup are
  deferred until after the first sidebar frame so startup and pane switching
  remain responsive.
- Unchanged project, session, and running-session snapshots no longer rebuild
  their rows every poll, reducing terminal redraws on SSH while still updating
  relative-time labels when their displayed value changes. Live child-process
  probes are shared between both session views during each refresh.
- Running-session and pane liveness now share one on-demand tmux server
  snapshot, with targeted probes retained as a failure fallback. Codex session
  metadata is scanned once per poll and reused across the project, session, and
  running views.
- Project counts and global recency ordering now use a three-second snapshot,
  while selected-session and running status keep their original poll cadence.
  Placeholder resolution, deletion, and rename still force immediate discovery.
- Live child-process checks reuse pane PIDs from the tmux server snapshot
  instead of launching another tmux query per pending session.
- Raised the minimum supported Urwid version to 2.6.16 for focus reporting.

### Fixed

- Keep Codex turns busy until an explicit lifecycle end event. Intermediate
  assistant messages, completed tools, and continued reasoning no longer make
  a still-running turn flash green; legacy rollouts retain last-role fallback.
- Do not apply Claude's child-process status heuristic to Codex, whose wrapper,
  native client, and MCP/code-mode children are permanent and cannot identify
  an approval wait.
- Delay Codex's stale pending-tool red indicator from 10 seconds to two minutes,
  so ordinary builds, SSH commands, and other long tools do not demand user
  attention prematurely.
- Hide the optional in-pane launch-error row completely while empty, detect an
  immediately vanished tmux agent session as a launch failure, and sanitize
  captured subprocess errors before displaying them.
- Start the idle-tip cadence when the first post-message tip is rendered, so a
  following refresh tick cannot replace it before a full rotation interval.
- Keep Claude project counts synchronized when a startup stub becomes a real
  conversation or the last JSONL is deleted; empty projects are hidden by
  default and can be shown with `[projects] show_empty_projects = true`.
- Make unresolved New Project entries actionable from the Running-pane context
  menu, and wait for the agent writer to exit before deleting Claude history so
  shutdown cannot recreate a visible title-only stub.
- Unknown child-process probe results now fall back to JSONL-derived status.
- Removed stale project selection when its project disappears during refresh.
- Preserve soft-quit state until deferred right-pane restoration completes.
- Defer right-pane focus until tmux's late DoubleClick1Pane binding completes,
  preventing focus from bouncing back to the sidebar.
- Pre-paint the right-pane focus state as soon as a double-click is detected,
  so the sidebar highlight and center divider switch together while the real
  tmux focus transfer remains safely delayed.
- Keep status-bar truncation within a one-column viewport and clarify that F9
  targets the agent pane.
- Keep session metadata caches scoped by project and key them by nanosecond
  mtime plus size, ensuring appends during a Claude or Codex scan are picked up
  on the next poll.
- Persist soft-quit state, favorites, and the project path cache with atomic
  replacement, including creation of a missing fallback runtime directory.
- Retry history cleanup when Claude appends concurrently and never replace a
  history file whose signature changed during the read.

## [0.1.0] - 2026-07-14

### Added

- Initial PyPI release under the Railmux name.

[Unreleased]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev25...HEAD
[0.4.0.dev25]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev23...v0.4.0.dev25
[0.4.0.dev24]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev23...v0.4.0.dev24
[0.4.0.dev23]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev22...v0.4.0.dev23
[0.4.0.dev22]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev21...v0.4.0.dev22
[0.4.0.dev21]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev20...v0.4.0.dev21
[0.4.0.dev20]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev19...v0.4.0.dev20
[0.4.0.dev19]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev18...v0.4.0.dev19
[0.4.0.dev18]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev17...v0.4.0.dev18
[0.4.0.dev17]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev16...v0.4.0.dev17
[0.4.0.dev16]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev15...v0.4.0.dev16
[0.4.0.dev15]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev14...v0.4.0.dev15
[0.4.0.dev14]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev13...v0.4.0.dev14
[0.4.0.dev13]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev12...v0.4.0.dev13
[0.4.0.dev12]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev11...v0.4.0.dev12
[0.4.0.dev11]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev10...v0.4.0.dev11
[0.4.0.dev10]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev9...v0.4.0.dev10
[0.4.0.dev9]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev8...v0.4.0.dev9
[0.4.0.dev8]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev7...v0.4.0.dev8
[0.4.0.dev7]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev6...v0.4.0.dev7
[0.4.0.dev6]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev5...v0.4.0.dev6
[0.4.0.dev5]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev4...v0.4.0.dev5
[0.4.0.dev4]: https://github.com/Rightglow/Railmux/compare/v0.4.0.dev3...v0.4.0.dev4
[0.3.6]: https://github.com/Rightglow/Railmux/compare/v0.3.5...v0.3.6
[0.3.6.dev6]: https://github.com/Rightglow/Railmux/compare/v0.3.6.dev5...v0.3.6.dev6
[0.3.6.dev5]: https://github.com/Rightglow/Railmux/compare/v0.3.6.dev4...v0.3.6.dev5
[0.3.6.dev4]: https://github.com/Rightglow/Railmux/compare/v0.3.6.dev3...v0.3.6.dev4
[0.3.6.dev3]: https://github.com/Rightglow/Railmux/compare/v0.3.6.dev2...v0.3.6.dev3
[0.3.6.dev2]: https://github.com/Rightglow/Railmux/compare/v0.3.6.dev1...v0.3.6.dev2
[0.3.6.dev1]: https://github.com/Rightglow/Railmux/compare/v0.3.5...v0.3.6.dev1
[0.3.5]: https://github.com/Rightglow/Railmux/compare/v0.3.4...v0.3.5
[0.3.5.dev1]: https://github.com/Rightglow/Railmux/compare/v0.3.4...v0.3.5.dev1
[0.3.4]: https://github.com/Rightglow/Railmux/compare/v0.3.3...v0.3.4
[0.3.4.dev1]: https://github.com/Rightglow/Railmux/compare/v0.3.3...v0.3.4.dev1
[0.3.3]: https://github.com/Rightglow/Railmux/compare/v0.3.2...v0.3.3
[0.3.3.dev6]: https://github.com/Rightglow/Railmux/compare/v0.3.3.dev5...v0.3.3.dev6
[0.3.3.dev5]: https://github.com/Rightglow/Railmux/compare/v0.3.3.dev4...v0.3.3.dev5
[0.3.3.dev4]: https://github.com/Rightglow/Railmux/compare/v0.3.3.dev3...v0.3.3.dev4
[0.3.3.dev3]: https://github.com/Rightglow/Railmux/compare/v0.3.3.dev2...v0.3.3.dev3
[0.3.3.dev2]: https://github.com/Rightglow/Railmux/compare/v0.3.3.dev1...v0.3.3.dev2
[0.3.3.dev1]: https://github.com/Rightglow/Railmux/compare/v0.3.2...v0.3.3.dev1
[0.3.2]: https://github.com/Rightglow/Railmux/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/Rightglow/Railmux/compare/v0.3.0...v0.3.1
[0.3.1.dev202608010]: https://github.com/Rightglow/Railmux/compare/v0.3.0...v0.3.1.dev202608010
[0.3.0]: https://github.com/Rightglow/Railmux/compare/v0.2.21...v0.3.0
[0.2.21]: https://github.com/Rightglow/Railmux/compare/v0.2.20...v0.2.21
[0.2.21.dev202608010]: https://github.com/Rightglow/Railmux/compare/v0.2.21.dev202607311...v0.2.21.dev202608010
[0.2.21.dev202607311]: https://github.com/Rightglow/Railmux/compare/v0.2.20...v0.2.21.dev202607311
[0.2.20]: https://github.com/Rightglow/Railmux/compare/v0.2.19...v0.2.20
[0.2.19]: https://github.com/Rightglow/Railmux/compare/v0.2.18...v0.2.19
[0.2.19.dev202607308]: https://github.com/Rightglow/Railmux/compare/v0.2.19.dev202607307...v0.2.19.dev202607308
[0.2.19.dev202607307]: https://github.com/Rightglow/Railmux/compare/v0.2.19.dev202607306...v0.2.19.dev202607307
[0.2.19.dev202607306]: https://github.com/Rightglow/Railmux/compare/v0.2.19.dev202607305...v0.2.19.dev202607306
[0.2.19.dev202607305]: https://github.com/Rightglow/Railmux/compare/v0.2.19.dev202607304...v0.2.19.dev202607305
[0.2.19.dev202607304]: https://github.com/Rightglow/Railmux/compare/v0.2.19.dev202607303...v0.2.19.dev202607304
[0.2.19.dev202607303]: https://github.com/Rightglow/Railmux/compare/v0.2.19.dev202607302...v0.2.19.dev202607303
[0.2.19.dev202607302]: https://github.com/Rightglow/Railmux/compare/v0.2.19.dev202607301...v0.2.19.dev202607302
[0.2.19.dev202607301]: https://github.com/Rightglow/Railmux/compare/v0.2.19.dev202607300...v0.2.19.dev202607301
[0.2.19.dev202607300]: https://github.com/Rightglow/Railmux/compare/v0.2.19.dev202607292...v0.2.19.dev202607300
[0.2.19.dev202607292]: https://github.com/Rightglow/Railmux/compare/v0.2.19.dev202607291...v0.2.19.dev202607292
[0.2.19.dev202607291]: https://github.com/Rightglow/Railmux/compare/v0.2.19.dev202607290...v0.2.19.dev202607291
[0.2.19.dev202607290]: https://github.com/Rightglow/Railmux/compare/v0.2.18...v0.2.19.dev202607290
[0.2.18]: https://github.com/Rightglow/Railmux/compare/v0.2.17...v0.2.18
[0.2.18.dev202607293]: https://github.com/Rightglow/Railmux/compare/v0.2.18.dev202607292...v0.2.18.dev202607293
[0.2.18.dev202607292]: https://github.com/Rightglow/Railmux/compare/v0.2.18.dev202607291...v0.2.18.dev202607292
[0.2.18.dev202607291]: https://github.com/Rightglow/Railmux/compare/v0.2.18.dev202607290...v0.2.18.dev202607291
[0.2.18.dev202607290]: https://github.com/Rightglow/Railmux/compare/v0.2.17...v0.2.18.dev202607290
[0.2.17]: https://github.com/Rightglow/Railmux/compare/v0.2.16...v0.2.17
[0.2.16]: https://github.com/Rightglow/Railmux/compare/v0.2.15...v0.2.16
[0.2.16.dev202607291]: https://github.com/Rightglow/Railmux/compare/v0.2.16.dev202607290...v0.2.16.dev202607291
[0.2.16.dev202607290]: https://github.com/Rightglow/Railmux/compare/v0.2.15...v0.2.16.dev202607290
[0.2.15]: https://github.com/Rightglow/Railmux/compare/v0.2.14...v0.2.15
[0.2.15.dev202607284]: https://github.com/Rightglow/Railmux/compare/v0.2.15.dev202607283...v0.2.15.dev202607284
[0.2.15.dev202607283]: https://github.com/Rightglow/Railmux/compare/v0.2.15.dev202607282...v0.2.15.dev202607283
[0.2.15.dev202607282]: https://github.com/Rightglow/Railmux/compare/v0.2.15.dev202607280...v0.2.15.dev202607282
[0.2.15.dev202607280]: https://github.com/Rightglow/Railmux/compare/v0.2.14...v0.2.15.dev202607280
[0.2.14]: https://github.com/Rightglow/Railmux/compare/v0.2.13...v0.2.14
[0.2.13]: https://github.com/Rightglow/Railmux/compare/v0.2.12...v0.2.13
[0.2.12]: https://github.com/Rightglow/Railmux/compare/v0.2.11...v0.2.12
[0.2.12.dev202607270]: https://github.com/Rightglow/Railmux/compare/v0.2.11...v0.2.12.dev202607270
[0.2.11]: https://github.com/Rightglow/Railmux/compare/v0.2.10...v0.2.11
[0.2.11.dev202607240]: https://github.com/Rightglow/Railmux/compare/v0.2.10...v0.2.11.dev202607240
[0.2.10]: https://github.com/Rightglow/Railmux/compare/v0.2.9...v0.2.10
[0.2.9]: https://github.com/Rightglow/Railmux/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/Rightglow/Railmux/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/Rightglow/Railmux/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/Rightglow/Railmux/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/Rightglow/Railmux/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/Rightglow/Railmux/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/Rightglow/Railmux/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/Rightglow/Railmux/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Rightglow/Railmux/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Rightglow/Railmux/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/Rightglow/Railmux/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Rightglow/Railmux/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Rightglow/Railmux/releases/tag/v0.1.1
[0.1.0]: https://pypi.org/project/railmux/0.1.0/
