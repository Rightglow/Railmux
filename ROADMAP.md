# Railmux roadmap

This is a set of design candidates, not a release commitment. Architecture
invariants already agreed are recorded in `docs/ARCHITECTURE.md`.

## Follow-up candidates

### Maintainability and diagnostics after 0.3

Incrementally extract recovery/watchdog, restore/layout, tmux-status, and SSH
connection-state responsibilities from the largest UI and display-client
modules. Keep each extraction behavior-preserving and independently protected
by the existing lifecycle and protocol tests; do not replace the modules in one
rewrite.

Evaluate an opt-in, bounded, redacted debug log for field diagnosis. It must
never record hostnames, usernames, session or pane identities, transcripts,
environment values, configured commands, credentials, socket paths, or raw
private paths, and should share the privacy contract already used by
`railmux doctor`.

Audit `atomic_write_text` separately before preserving an existing destination
mode. It currently inherits `mkstemp`'s private `0600` mode, including for the
explicit Claude-history deletion path; changing that behavior needs targeted
permission semantics rather than a repository-wide chmod guess.

### Managed Windows runtime follow-ups

Windows Terminal running a WSL distribution already uses Railmux's supported
Linux path, but it is not the native-Windows product: its Linux providers do
not naturally share the user's Windows Codex/Claude installation, credentials,
paths, and histories. The shipped native adapter uses a thin bootstrap
that provisions a private managed MSYS2/tmux runtime and hands local Railmux or
the local side of `railmux ssh` to the unchanged POSIX application. Providers
remain Windows-native and keep using the existing Windows user directories.

The bootstrap owns consent, downloads, integrity checks, updates, path and
argument translation, and terminal handoff. It must not grow another terminal
emulator, compositor, provider host, or independent copy of Railmux UI state.
The earlier ConPTY implementation is frozen at `v0.4.0.dev2` on
`archive/windows-conpty-deprecated` and is not a base for new work. The
WSL-delegation experiment is frozen at `v0.4.0.dev3` on
`archive/windows-wsl-delegation-deprecated` for the same reason. The
complete functional baseline and acceptance checklist live in
`docs/SUPPORT_MATRIX.md`; mocked Windows branches and ordinary WSL evidence are
not sufficient to claim the managed-MSYS2 launch experience.

Remaining work is bounded maintenance evidence: exercise a genuinely
non-ASCII Windows profile path, revalidate the fail-closed pacman core-restart
handoff when a real host next has a pending core update, and decide whether a
future runtime generation should snapshot repository package versions rather
than retaining signed rolling repositories plus an exact installed-content
identity. None of these changes the shipped ownership model.

### Dual-agent workspace follow-ups

Railmux 0.2 ships the bounded two-slot workspace, layout cycling, Target-pane
routing, responsive sidebar sizing, native focus presentation, and full managed
soft-restart recovery described in `docs/ARCHITECTURE.md`. Explicitly swapping
the physical primary/secondary positions is deferred because it does not yet
solve a demonstrated workflow problem. Revisit only if field use shows that the
current spatial navigation is insufficient. The 50x12 minimum and 80x20
preferred pane thresholds may be tuned from real-agent feedback without
changing the workspace model.

### Default swap transport follow-up

`swap` is the shipped default; `nested` remains an explicit compatibility
choice and the automatic fallback whenever exact pane ownership cannot be
proven. Its transaction and recovery invariants are authoritative in
`docs/ARCHITECTURE.md`, with reproducible evidence in
`docs/DENESTED_AGENT_PANE.md`. Remaining work is field evidence, not a second
default-selection experiment:

- Acceptable real-provider geometry/reflow, especially on tmux 2.7/2.8 where
  `resize-window` is unavailable and with long inline Codex transcripts.
- How much Claude Code improves when de-nested, since its alternate-screen,
  application-owned mouse path cannot use Codex's copy-mode batching unchanged.
- Same-link SSH measurements for first wheel paint, burst drain, sustained
  output, clipboard/mouse behavior, and CPU. A reproducible local synthetic
  server benchmark now places swap close to direct and consistently ahead of
  nested marker observation. It cannot observe client terminal paint or real
  providers, so it must not be cited as proof of perceived latency.
- Whether real measurements justify changing the current 100 ms copy-mode
  coalescing frame. Do not infer a redraw budget from ping or SSH/TCP metrics:
  the server cannot observe local terminal paint.

### Codex interrupt transcript replay

Codex currently consolidates an incomplete streamed answer after Esc by
clearing and rebuilding its canonical inline transcript. Railmux does not see
or forward that Esc, and the attach-time pre-sizing path is not involved, but a
nested tmux client can make the upstream rebuild visibly sweep from old content
back to the prompt.

Do not silently force alternate-screen mode or truncate Codex history to hide
this: both change native scrollback/copy behavior. Possible experiments are an
explicit, documented Codex reflow-row limit, a future tmux version with proven
application synchronized-output support, and the existing swap transport.
Any workaround must remain opt-in until its history tradeoff and Codex
version compatibility are clear.

### SSH drag-selection edge autoscroll

Consider adding edge-triggered autoscroll when a pointer drag selection reaches
the top or bottom of the local `railmux ssh` viewport. This is a candidate for
a post-0.4.0 maintenance release, not a 0.4.0 release requirement. Ordinary
local/tmux selection and live scrolling must remain unchanged; the SSH client
must not switch into remote tmux copy mode to implement it.

The selection must be an immutable client-side snapshot established at mouse
down. The remote agent and PTY may continue producing output, and the client
must continue draining new frames, but post-snapshot output must not enter the
selection or make a downward drag chase a moving tail. Historical pages may be
added only when they align uniquely with the frozen pane, geometry, history
generation, visible anchor, and mouse-down tail. Ambiguous alignment or
truncated history must stop scrolling rather than copy unrelated text.

Resize, reconnect, pane replacement, layout/geometry changes, keyboard input,
and a lost mouse-release watchdog must cancel the snapshot and repaint the
latest live screen. A future implementation needs focused protocol and model
tests for output arriving during selection, both scroll directions, bounded
history exhaustion, cancellation paths, and release-time repaint, followed by
real Windows, macOS, Linux, and touch-terminal checks. Do not treat simple
coordinate clamping or a timer-driven scroll loop as sufficient evidence.

### Provider adapters

The mode registry now supports a third stable mode and independent view state.
Extract backend operations behind a provider adapter before adding a provider
whose discovery/launch/delete model differs from both existing backends.
