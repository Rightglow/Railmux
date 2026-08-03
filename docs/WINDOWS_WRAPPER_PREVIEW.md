# Windows delegated-runtime preview

This document is the design and validation authority for the active
`windows-preview` branch. The branch starts from `main`; the abandoned native
ConPTY compositor is preserved only at
`archive/windows-conpty-deprecated` (`f1b8cb1`, `v0.4.0.dev2`).

## Decision

Windows Python is a bootstrap, not a second Railmux runtime. It may discover,
install, update, diagnose, translate arguments and paths, and enter a selected
runtime. The existing POSIX Railmux process inside that runtime remains the
only owner of tmux, providers, session discovery, restore state, previews,
layout, mouse routing, dialogs, and terminal rendering.

Runtime preference is:

1. An existing user-selected WSL distribution with Railmux installed.
2. A future private, managed MSYS2/tmux runtime installed with explicit
   consent when the user does not want WSL.

The preview does not run a Railmux SSH server or providers on native Windows.
`railmux ssh` launched from Windows continues to target Linux, macOS, or a
compatible Unix host from the delegated local runtime.

## Current vertical slice

`railmux.entrypoint` decides the platform before importing the POSIX CLI, so a
native Windows console script never imports `termios`. On Windows 3.10+, the
bootstrap:

1. handles root help and a labelled bootstrap `--version` without starting a
   runtime;
2. resolves only `%SystemRoot%\\System32\\wsl.exe` (or `Sysnative` for a
   32-bit process), never a same-named executable from the project or `PATH`;
3. probes only the default distribution, or the exact distribution selected
   by `RAILMUX_WSL_DISTRO`, so it cannot boot or guess among unrelated distros;
4. falls back to one fixed login-shell `command -v railmux` probe for standard
   `~/.local/bin`/pipx installs, validates the returned absolute path, and then
   hands the original argv directly to that executable with inherited
   stdin/stdout/stderr;
5. waits through a parent `KeyboardInterrupt` instead of killing `wsl.exe`,
   allowing the delegated process to receive Ctrl-C and restore its terminal.

Set `RAILMUX_WSL_DISTRO` to restrict this preview probe to one distribution.
This is an experimental diagnostic override, not yet a stable configuration
key. No failed probe installs software or intentionally edits a distribution;
probing can start the selected WSL distribution. Automatic WSL provisioning,
managed MSYS2, native-session migration, and runtime updates are not
implemented in this slice.

## Bootstrap safety boundaries

- Never use `shell=True` or interpolate user input into a command string. The
  sole login-shell probe is a fixed literal with no user-controlled fragments;
  the final handoff always uses the validated executable plus an argv list.
- Decode every captured subprocess stream explicitly; Windows locale defaults
  such as CP936 must not select the file or protocol encoding.
- Use a private, versioned directory for a managed runtime. Never modify a
  user-owned WSL/MSYS2 tree, system `PATH`, shell profile, provider history, or
  Windows-wide package state without a separate explicit action.
- Pin download sources and hashes. Stage and verify upgrades before atomically
  switching the runtime pointer; an interruption must leave the prior runtime
  usable.
- Keep prompts in the native bootstrap before terminal handoff. After handoff,
  the POSIX process owns terminal modes and restoration.
- Preserve argv, Unicode, exit status, Ctrl-C, and working-directory intent
  across the boundary. Path translation must be typed and command-specific,
  not global string replacement.

## Lessons retained from the ConPTY archive

The archive is not a code base, but its failures define useful tests:

- Mocked terminal buffers did not prove real Windows Terminal behavior.
  Release evidence must include authenticated providers and visible UI checks.
- Preview/open, resize/reflow, double-click, context menus, sidebar scrolling,
  responsive modals, and the complete bottom chrome are one parity surface.
  Passing startup alone proves almost nothing.
- Explicit UTF-8 reads/writes and atomic user-state replacement remain required
  even on a Chinese Windows locale.
- Window closure, reconnect, and resize must preserve the tmux-owned provider
  sessions and restore the terminal; the bootstrap must not invent a second
  lifecycle authority.
- A machine-readable parity ledger must fail when the stable feature inventory
  grows without a Windows-preview disposition.

These lessons are represented in `windows-wrapper-parity.toml` and its contract
test. No `winlocal`, ConPTY compositor, virtual screen, native provider daemon,
or parallel status/sidebar implementation may be copied into this branch.

## Delivery stages

1. **Existing WSL handoff** — package-safe dispatch, exact argv, explicit
   decoding, Windows CI, and manual Windows Terminal baseline comparison.
2. **Consent-based WSL setup** — select a distribution, provision a private
   venv, pin the Railmux version, and diagnose without silently upgrading the
   distribution.
3. **Managed MSYS2 spike** — verify official distribution/licensing, tmux
   persistence after terminal close, Python/provider compatibility, resize,
   mouse, clipboard, browser, SSH, and transactional updates before adopting
   it as a fallback.
4. **Product hardening** — native bootstrap doctor/config, signed/hash-pinned
   runtime acquisition, recovery, cleanup, full CI, and named manual evidence.

Do not publish a stable support claim until both the support-matrix checklist
and every applicable ledger scenario have real-platform evidence. WSL success
does not prove the managed-MSYS2 path.
