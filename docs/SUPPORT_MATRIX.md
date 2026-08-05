# Railmux support matrix and validation contract

This document inventories the product behavior supported by the current tree
and the evidence required to extend that support to another provider, runtime
host, or terminal emulator. It is the maintainer-facing release checklist, not
a replacement for the user-facing [`README`](../README.md) or the implementation
invariants in [`ARCHITECTURE.md`](ARCHITECTURE.md).

Update this document in the same change that adds or removes a provider,
platform path, terminal integration, or user-visible feature. A feature is not
fully supported on a new surface merely because its happy path starts: every
applicable row below must either pass or carry an explicit limitation.

## Status language

| Status | Meaning |
|---|---|
| **Supported** | Part of the product contract, with automated coverage and/or the platform evidence named here. |
| **Conditional** | Supported when the stated terminal, dependency, or version capability is present. |
| **Field-validated** | Used successfully on real hardware, with portable logic covered in tests, but without a dedicated CI runner. |
| **Best effort** | Expected to work through a compatible POSIX interface but not claimed as a release target. |
| **Not supported** | Known to lack a required runtime adapter or product contract. |
| **Planned** | Intended follow-up only; not current behavior. |

Automated unit tests that mock an operating system branch prove the branch's
logic, not the real terminal or operating system. Likewise, a terminal emulator
is only the renderer: Windows Terminal running a WSL shell and Windows Terminal
running native PowerShell are different Railmux runtime platforms.

## Supported deployment surfaces

| Entry point | Local Railmux runtime | Remote runtime | Status | Current evidence and boundary |
|---|---|---|---|---|
| `railmux` | Linux | — | **Supported** | Linux CI, real private-tmux integration, and the tmux 2.7 floor job. |
| `railmux` | macOS | — | **Supported** | macOS CI and real private-tmux integration. |
| `railmux` | Windows WSL | — | **Supported** | Uses the Linux runtime; WSL clipboard and Windows Terminal launcher branches have unit coverage. No hosted real-WSL UI test yet. |
| `railmux ssh HOST` | Linux | Linux | **Supported** | Primary implementation and test surface; protocol, lifecycle, history, clipboard fallbacks, and reconnect are automated. |
| `railmux ssh HOST` | macOS | Linux | **Supported** | POSIX client path plus macOS CI; Terminal.app and common external-terminal launch behavior are covered. |
| `railmux ssh HOST` | Windows WSL, rendered by Windows Terminal | Linux | **Supported** | Linux TTY/SSH implementation with tested `clip.exe`, `wt.exe`, and `wsl.exe` integration. Real Windows Terminal interaction remains a manual release check. |
| `railmux ssh HOST` | Android Termux | Linux | **Field-validated** | Real phone use plus automated compact projection, SGR touch, soft-keyboard, cursor-focus, resize, and clipboard-fallback state tests. |
| Either entry point | Native Windows Python in PowerShell, CMD, or Windows Terminal | — or Linux | **Not supported** | The `main` package has no native runtime adapter; use the supported WSL path. Installing the package does not yet provision or enter a delegated runtime. |
| `railmux` or `railmux ssh HOST` | Native Windows bootstrap using managed MSYS2/tmux | — or Linux | **Planned** | The wrapper is developed only on `windows-preview`; the current `main` package does not install a runtime or claim native launch support. Railmux runs under MSYS2 while Windows-native providers retain the user's existing session/config directories. |
| `railmux ssh HOST` | Linux or macOS | macOS | **Conditional** | The remote helper is POSIX and macOS tmux is integration-tested, but there is no dedicated cross-host SSH end-to-end job. |
| Either entry point | Other Unix-like system | Unix-like system | **Best effort** | Requires Python 3.9+, tmux, a compatible TTY, and the documented commands; no release claim without platform evidence. |

The ordinary `ssh HOST` followed by remote `railmux` path depends primarily on
the local terminal and remote POSIX environment. It does not use Railmux's
local latest-state client, local history cache, semantic link handling, or
pane-bounded drag-to-copy.

## Dependency and terminal capability floors

| Capability | Contract |
|---|---|
| Python | 3.9 or newer on every machine running Railmux. |
| tmux core workspace | 2.7 or newer. CI compiles the checksum-pinned official tmux 2.7 release and boots a real Railmux frame. |
| Managed shell/Vim and nested pane-local SSH history marker | tmux 3.0 or newer; older tmux fails closed with a warning. |
| Clickable tmux status ranges and compact `[R][1][2]` labels | tmux 3.4 or newer; keyboard navigation remains portable. |
| Providers | Claude Code, Codex, or both on the machine that runs the provider process. |
| History preview | `less`; provider rollout/history files must remain readable. |
| Managed file viewer | Remote `vim` for supported files; missing Vim falls back without mutating the file. |
| Mouse | Terminal must report SGR-compatible mouse events to the application. Right-click and hover may require separate terminal settings. |
| Clipboard | Native writer where available, otherwise bounded OSC 52. Terminal policy may still reject OSC 52. |
| Colour and text | UTF-8 plus common SGR colour/style behavior. Uncommon terminal modes and exact rendering remain terminal-dependent. |

## Functional inventory

The IDs below are stable review handles. New work should cite the affected IDs
in its plan or pull request and add a new ID when it creates a genuinely new
product capability.

### Providers and sessions

| ID | Capability | Direct `railmux` | `railmux ssh` | Important boundary |
|---|---|---|---|---|
| P01 | Detect installed Claude Code and Codex CLIs independently | Supported | Supported remotely | Selecting a missing provider warns without stopping the workspace. |
| P02 | Discover resumable sessions created outside Railmux | Supported | Supported | One-shot `codex exec` and private Help sessions are filtered. |
| P03 | Project list, project selection, and new project/directory creation | Supported | Supported | Creation is explicit and never inferred from an arbitrary failed match. |
| P04 | Read-only history preview with provider-aware formatting | Supported | Supported | Latest 2,000 saved records are projected onto the provider's current branch; rewound suffixes and internal/encrypted reasoning are hidden. |
| P05 | Start a new session and resume an existing session | Supported | Supported | Starting, resuming, previewing, and switching never rewrite provider history; confirmed P09 deletion is the explicit exception. Shared provider roots use fail-closed per-session leases for both Claude Code and Codex so two hosts cannot resume one conversation concurrently. |
| P06 | Live open on click/Enter; live scrolling on wheel; canonical history on Space or Preview | Supported | Supported | Direct wheel input remains tmux/provider-native; `railmux ssh` owns bounded per-pane scrolling and normally uses styled raw pane capture. A confirmed Codex rewind/steer leaves the live pane untouched and marks only that SSH history generation canonical so the abandoned suffix stays hidden. |
| P07 | Session Info, rename, star, and copy title | Supported | Supported | Rename and favorites are Railmux metadata; copy depends on clipboard capability. |
| P08 | Kill a live provider while retaining history | Supported | Supported | Revalidates immutable tmux/provider identity before mutation. |
| P09 | Delete stopped provider history with confirmation | Supported | Supported | Unknown or changed live identity, a remote lease, or unavailable shared locking fails closed; confirmed cleanup runs in one background transaction with durable status-right progress. |
| P10 | Activity, blocked, attention, live, remote-owner, and unresolved status | Supported | Supported | Liveness, activity, and attention remain separate states. `running on HOST` is shared-storage ownership, not a locally attachable Running entry. |
| P11 | Per-provider Projects, Sessions, and Running filters | Supported | Supported | Current filters and selection survive soft restart. |
| P12 | Switch Claude Code/Codex mode without stopping the other provider | Supported | Supported | Each mode keeps independent sidebar view state. |

### Workspace, lifecycle, and configuration

| ID | Capability | Direct `railmux` | `railmux ssh` | Important boundary |
|---|---|---|---|---|
| W01 | Dedicated tmux server isolated from the user's default server | Supported | Supported | Legacy default-server sessions are a deprecated, read-only discovery bridge. |
| W02 | One or two agent slots with single, side-by-side, and stacked layouts | Supported | Supported | Hidden agents keep running; one live session cannot occupy both slots. |
| W03 | Independent keyboard Focus and sidebar action Target | Supported | Supported | Status preserves Target while gray borders indicate sidebar focus. |
| W04 | Divider adjustment and optional proportional layout retention | Supported | Supported | Saved profiles fail safe when a later terminal is too small. |
| W05 | Intermediate single-agent and compact one-page responsive layouts | Supported | Supported | Minimum 40x12; compact below 80 columns or 24 rows with hysteresis. |
| W06 | Keyboard, button bar, context menus, Help, and persistent Options | Supported | Supported | Mouse alternatives exist for every required workflow. |
| W07 | Safety-restricted Ask Railmux help agent | Supported | Supported | Read-only support workspace; no normal provider-history pollution. |
| W08 | One reusable managed shell and Vim viewer per agent slot | Conditional | Conditional | Requires tmux 3.0+; tools park safely across layout changes. |
| W09 | Detach one view, Soft Quit shared UI, and confirmed hard quit | Supported | Supported | Soft Quit leaves provider sessions alive; views of one UI are not independent workspaces. Hard quit requires two confirmations while retaining `y`/Enter semantics. |
| W10 | Soft restart and exact workspace/session recovery | Supported | Supported | Ambiguous identity becomes visible unresolved state rather than a guessed binding. |
| W11 | Multiple attached terminals | Conditional | Conditional | Shared focus/layout and tmux `smallest` geometry; simultaneous input can interfere. |
| W12 | Shared config file, standalone editor, and in-product persistent Options | Supported | Supported | `railmux config` works without tmux, uses a temporary interactive screen, validates program/locale overrides, and edits the same remote or local TOML authority; one-time confirmations stay action-local. |
| W13 | Local and remote privacy-safe diagnostics | Supported | Supported | `doctor --json` and text share one redacted snapshot authority. |
| W14 | Dedicated tmux watchdog and incident reporting | Supported | Supported | A client may exit after repeated failures but never kills/restarts tmux or a provider. |

### Full-window SSH client

| ID | Capability | Status | Important boundary |
|---|---|---|---|
| S01 | Pre-attach package/protocol/config/tmux compatibility handshake | Supported | Runs before tmux lookup, creation, lock, PTY allocation, or attach; invalid remote config and configured-tmux failures remain distinct. |
| S02 | Consent-based remote user install or private venv repair | Supported | Exact compatible package; never `sudo`, system package installation, or shell-profile edits. |
| S03 | Consent-based local upgrade when remote is newer | Supported on POSIX local runtimes | Re-execs only after the same interpreter imports the requested version. |
| S04 | Immediate restoring surface and bounded startup stages | Supported | First validated keyframe replaces it; setup prompts remain cooked-mode. |
| S05 | Coalesced latest-state keyframes and row patches | Supported | Slow display output must not flow-control the real provider pane. |
| S06 | Default-on bounded automatic reconnect | Supported | Only after a first frame; bottom-right retry status and display-only SSH keepalives bound silent outages; no install, takeover, session creation, detach, or provider mutation. |
| S07 | Heartbeat lease and stale-helper cleanup | Supported | Stops only the helper's exact private tmux client, never the workspace or agents. |
| S08 | Independent cached local history per agent pane | Supported | 300-line hot routing cache, atomic first entry from a 2,000-line cumulative capture when older rows exist, configurable 2,000–20,000 cap; unaligned captures are never spliced, and ordinary Codex history preserves styled raw pane capture without switching format merely because a Preview locator exists. |
| S09 | Page Up/Down through verified agent history | Supported | Sidebar and dialogs retain their ordinary keys. |
| S10 | Claude native history or styled local transcript policy | Supported | Choice can be persistent or current invocation; Codex normally uses tmux/history capture, with canonical transcript fallback gated by an exact confirmed branch marker. |
| S11 | Pane-bounded local drag selection and automatic copy | Supported | Visible physical rows only; no autoscroll, soft-wrap join, or cross-pane selection. |
| S12 | URL/path hover and clean-click recognition | Conditional | Hover needs mouse-motion reporting; open acts only in the already-focused agent route. |
| S13 | Open HTTP(S) URL in the local browser | Supported on named local platforms | The remote never receives the browser action. |
| S14 | Read-only remote path validation and managed Vim/open-directory action | Conditional | Unquoted paths without spaces; remote identity/path is revalidated before action. |
| S15 | Separate local terminal for remote path | Conditional | Terminal.app, common Linux terminals, and Windows Terminal from WSL; Termux copies the command. |
| S16 | Native clipboard writer with bounded OSC 52 fallback | Conditional | macOS `pbcopy`, Linux Wayland/X11 tools, WSL `clip.exe`, or terminal-approved OSC 52. |
| S17 | Bracketed paste and terminal focus-event projection | Supported | Only allowlisted modes cross the display protocol; modes are restored on every exit path. |
| S18 | Termux soft-keyboard projection, touch recovery, and prompt cursor | Field-validated | Android-specific behavior is entered only from Termux environment evidence. |
| S19 | Emergency local `Ctrl-]`, normal tmux detach, and lifecycle exit classification | Supported | Local escape never becomes provider input. |
| S20 | Consent-based `railmux config --remote HOST` | Supported on POSIX local runtimes | Two SSH phases; the probe never sends the display start token or touches a tmux server, and the cooked editor preserves local-only history capacity. |

## Terminal emulator validation

No terminal-specific branch should be added merely from its name. Validate the
capabilities it exposes, then record unavoidable product differences.

| Terminal family | Current claim | Manual checks still relevant |
|---|---|---|
| Terminal.app | Supported for direct and macOS SSH client use | Mouse/right-click policy, Shift-drag native selection, no assumed OSC 52. |
| iTerm2 | Supported | Application mouse reporting and OSC 52 permission may need user settings. |
| VS Code/Cursor xterm.js | Supported | Right-click behavior and CJK composition focus are editor settings; test paste and mouse forwarding. |
| kitty, WezTerm, Alacritty, foot | Compatible/conditional | SGR mouse, OSC 52 policy, function keys, colours, resize, and external launcher where applicable. |
| Windows Terminal with WSL | Supported | WSL runtime, `clip.exe`, `wt.exe`, function-key conflicts, mouse, paste, resize, and OSC 52. |
| Windows Terminal launched by the native Windows bootstrap | Planned | Must pass the delegated-runtime acceptance checklist below before this row changes. |
| Termux | Field-validated | Compact navigation, keyboard open/close, cursor, post-keyboard touch, rotation boundary, history, and reconnect. |

For every newly claimed terminal, manually verify alternate-screen entry/exit,
cursor visibility, UTF-8 and wide characters, 16/256/true colour fallback,
bracketed paste, focus events, SGR press/release/wheel/motion, function keys,
resize, status clicks where tmux supports them, clipboard policy, and terminal
restoration after `Ctrl-]`, detach, error, and network loss.

## Adding another provider

The current provider registry can name a third mode, but a provider is not
complete until all applicable behavior below has a real adapter and tests.
OpenCode is therefore **planned**, not currently supported.

1. Detect the CLI and produce a safe launch/resume command without a shell
   interpolation boundary.
2. Discover projects and resumable sessions created both inside and outside
   Railmux; explicitly filter one-shot/non-resumable work.
3. Extract immutable session identity, title, timestamps, cwd/project, and
   provider-specific history without modifying provider files.
4. Correlate a live process and tmux pane to its session, including unresolved
   launch and interrupted-recovery behavior.
5. Implement new, resume, preview, rename, star, info, copy-title, kill, and
   confirmed delete semantics—or mark a capability unavailable in the UI.
6. Map activity, blocked approval, attention/error, stopped, and unresolved
   states without conflating them.
7. Define normal scrolling, alternate-screen behavior, transcript fallback,
   mouse ownership, paste, focus events, and `railmux ssh` history strategy.
8. Provide a safety-restricted Help-agent command or explicitly leave Ask
   unavailable for that mode.
9. Verify dual slots, layout changes, compact mode, tools, detach, soft restart,
   watchdog exit, and automatic reconnect while the provider is busy.
10. Add fixture/unit coverage, real CLI smoke where licensing/auth permits,
    README controls and limitations, this matrix, changelog, and website/demo
    updates.

## Adding a local operating system

The Windows preview uses a native bootstrap to enter one POSIX Railmux runtime,
not a second Console/ConPTY UI implementation. The remote helper can remain
POSIX. Every item below must pass before the native Windows launch experience
is labelled supported.

1. `pip install`, package import, CLI parsing, bootstrap configuration, version
   check, update, and privacy-safe diagnostics work under supported Windows
   Python without importing POSIX-only Railmux modules before handoff.
2. Offer an explicit, cancellable managed-MSYS2 installation with pinned
   sources, integrity verification, private ownership, and no system-wide PATH
   or shell-profile edits. Never silently adopt or modify a user-owned MSYS2.
3. Keep one versioned runtime authority and make interrupted installation or
   upgrade transactional and recoverable. Never overwrite user-owned MSYS2
   files.
4. Translate Windows paths, Unicode arguments, environment, exit status, and
   Ctrl-C exactly across the handoff without `shell=True` or command-string
   interpolation.
5. Launch the existing POSIX `railmux` or `railmux ssh` entry point inside the
   selected runtime. tmux, session discovery, restore, previews, layout, and UI
   state remain owned there rather than mirrored natively; provider processes
   are the Windows-native CLIs and use their existing Windows histories.
6. Validate UTF-8/CJK input, IME composition, bracketed paste, arrows, Page
   Up/Down, function keys, tmux prefix sequences, and terminal cleanup.
7. Validate resize/reflow, SGR mouse press/release/wheel/drag/hover,
   double-click, status clicks, context menus, local history routing, semantic
   URL/path clicks, and selection copy against the POSIX baseline.
8. Define clipboard, browser, and separate-terminal bridges for each runtime;
   keep bounded OSC 52 as a policy-dependent fallback and quote remote SSH/Vim
   commands without `shell=True` or command-string interpolation.
9. Prove local session restore and `railmux ssh` to Linux/macOS/Unix hosts.
   Running providers or an SSH server on native Windows remains out of scope.
10. Add unconditional native Windows bootstrap/import/package CI plus a real
    managed-MSYS2 runtime smoke. If hosted CI cannot exercise the runtime,
    record a named and dated real Windows Terminal pass here for every release
    that claims it. Mocked OS branches and ordinary WSL evidence cannot close
    the managed-MSYS2 path.

## Release closure checklist

For a change that affects a provider, platform, terminal, transport, or shared
feature:

1. Identify affected inventory IDs and deployment surfaces.
2. Run Ruff, the full Python suite, build/twine/wheel smoke, and the private
   real-tmux integration suite.
3. Run the pinned tmux 2.7 job when core behavior changes; exercise tmux 3.0
   and 3.4 capability branches when tools or clickable status change.
4. Add the applicable OS/terminal/provider automated evidence and record any
   remaining manual validation honestly.
5. Recheck direct Railmux and `railmux ssh`; a transport-only enhancement must
   not silently change the shared remote workspace.
6. Verify lifecycle safety: detach, Soft Quit, hard quit, watchdog failure,
   reconnect, and terminal restoration must not kill or corrupt provider
   histories.
7. Update the user contract in `README.md` only when observable behavior or
   limitations change; update architecture invariants when ownership or safety
   changes; update this matrix whenever support scope changes.
8. Update changelog and website/demo material for user-visible features, then
   perform the repository closure review described in [`AGENTS.md`](../AGENTS.md).

## Related authorities

- [`README.md`](../README.md): installation, controls, workflows, configuration,
  and user-facing troubleshooting.
- [`ARCHITECTURE.md`](ARCHITECTURE.md): current ownership, safety, recovery,
  display, layout, and provider invariants.
- [`DENESTED_AGENT_PANE.md`](DENESTED_AGENT_PANE.md): transport experiments,
  tmux-version evidence, and remaining SSH display limitations.
- [`BACKGROUND_SESSION_INDEX.md`](BACKGROUND_SESSION_INDEX.md): Codex index
  evidence and measurement limits.
- [`ROADMAP.md`](../ROADMAP.md): open product candidates rather than shipped
  support claims.
