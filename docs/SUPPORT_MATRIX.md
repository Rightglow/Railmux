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
| `railmux` or `railmux ssh HOST` | Native Windows Python bootstrap using managed MSYS2/tmux | — or Linux/macOS/Unix | **Supported** | Python 3.10+ owns a private verified runtime; Railmux runs under MSYS2 while Windows-native providers retain the user's existing session/config directories. Windows Terminal 1.24.10621+ is the supported full-screen renderer and 1.24.11911 is field-validated. Other native Windows terminal hosts are best effort. |
| Ordinary `ssh USER@WINDOWS`, then `railmux` | Managed MSYS2/tmux on native Windows | — | **Supported** | Desktop and OpenSSH entry share the same private runtime, provider histories, and dedicated workspace. A one-attempt server-origin PTY bridge handles Windows Terminal Services boundaries; this is distinct from Railmux's display protocol. |
| `railmux ssh HOST` | Linux or macOS | macOS | **Conditional** | The remote helper is POSIX and macOS tmux is integration-tested, but there is no dedicated cross-host SSH end-to-end job. |
| `railmux ssh --remote-platform windows HOST` | Linux or macOS | Native Windows managed MSYS2 | **Field-validated** | Real Linux-to-Windows protocol/config/doctor handshakes, attach, frame streaming, and clean local escape passed against Windows 10/OpenSSH. The matching Windows runtime must already be installed; updates are explicit user-level PowerShell operations, and `auto` can detect the same path at the cost of a second password prompt when public-key authentication is unavailable. Arbitrary Windows Python/MSYS2 installations are not adopted. |
| Either entry point | Other Unix-like system | Unix-like system | **Best effort** | Requires Python 3.9+, tmux, a compatible TTY, and the documented commands; no release claim without platform evidence. |

The ordinary `ssh HOST` followed by remote `railmux` path depends primarily on
the local terminal and remote runtime. On POSIX it attaches normally; the
managed Windows runtime may transparently bridge a healthy tmux workspace
across Windows Terminal Services sessions. This path does not use Railmux's
local latest-state client, local history cache, semantic link handling, or
pane-bounded drag-to-copy and is not the same as `railmux ssh HOST`.

## Dependency and terminal capability floors

| Capability | Contract |
|---|---|
| Python | 3.9 or newer on POSIX/WSL. The native Windows bootstrap requires 3.10+; the managed MSYS2 process currently uses 3.12+. |
| tmux core workspace | 2.7 or newer. CI compiles the checksum-pinned official tmux 2.7 release and boots a real Railmux frame. |
| Native Windows synchronized provider redraw | The Railmux-owned MSYS2 generation installs and validates tmux 3.7 or newer before activation. Users do not maintain this private tmux. Windows Terminal entries use a private PTY that leaves text/frame content byte-exact, coalesces observed high-frequency cursor visibility, and restores a proven quiet input anchor inside consecutive synchronized paints while preserving provider animation and Working timers; explicit input, resize, relative/unknown cursor movement, and output quiet retain fail-safe authority. User config/history is unchanged. The shared macOS/Linux/WSL core floor remains tmux 2.7. |
| Managed shell/Vim and nested pane-local SSH history marker | tmux 3.0 or newer; older tmux fails closed with a warning. |
| Clickable tmux status ranges and compact `[R][1][2]` labels | tmux 3.4 or newer; keyboard navigation remains portable. |
| Native Windows full-screen renderer | Windows Terminal 1.24.10621 or newer. This is a host requirement, not a `pip` dependency. `WT_SESSION` is the conservative 0.4 capability gate; `TERM=xterm-256color` alone does not identify a terminal product or synchronized-output support. conhost, IDE terminals, and third-party native Windows terminals are best effort until separately field-validated. |
| Native Windows `railmux ssh` repaint | A managed MSYS2 client with the validated Windows Terminal marker commits each complete changed-row/overlay/status/cursor repaint inside one DEC synchronized-output boundary. The opaque marker is neither persisted nor transmitted; other terminal hosts keep the ordinary byte-exact repaint path. |
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
| P03 | Project list, selection, favorites, absolute-path copy, Info, Term, and new project/directory creation | Supported | Supported | Project favorites are Railmux path metadata, separate from session favorites; creation is explicit and never inferred from an arbitrary failed match. |
| P04 | Read-only history preview with provider-aware formatting | Supported | Supported | Latest 2,000 saved records are projected onto the provider's current branch; rewound suffixes and internal/encrypted reasoning are hidden. |
| P05 | Start a new session and resume an existing session | Supported | Supported | Starting, resuming, previewing, and switching never rewrite provider history; confirmed P09 deletion is the explicit exception. Shared provider roots use fail-closed per-session leases for both Claude Code and Codex so two hosts cannot resume one conversation concurrently. Codex repair intent stays stable across asynchronous holder startup. A just-created managed-Windows pane waits briefly for one stable native process-birth identity and gives the helper a bounded cold-start window; replacement, ambiguity, or timeout still refuses launch. |
| P06 | Live open on click/Enter; live scrolling on wheel; canonical history on Space or Preview | Supported | Supported | Direct wheel input remains tmux/provider-native; `railmux ssh` owns bounded per-pane scrolling, preserves every local wheel tick across adjacent terminal reads while painting on a non-sliding 60 Hz deadline, uses one row per POSIX/macOS tick and the native three-row step on managed Windows, and normally uses styled raw pane capture. Only an exact open Codex child with a newer provider-emitted rollback count may mark that SSH history generation canonical; ordinary continuation/compaction and released heuristic markers remain raw. |
| P07 | Session Info, rename, star, and copy title | Supported | Supported | Rename and favorites are Railmux metadata; copy depends on clipboard capability. |
| P08 | Kill or normally exit a live provider while retaining history | Supported | Supported | Revalidates immutable tmux/provider identity before mutation. A dead `remain-on-exit` pane is diagnostic residue, not a live provider, and is removed only by exact pane identity. |
| P09 | Delete stopped provider history with confirmation | Supported | Supported | Unknown or changed live identity, a remote lease, or unavailable shared locking fails closed; confirmed cleanup runs in one background transaction with durable status-right progress. |
| P10 | Activity, blocked, attention, live, remote-owner, and unresolved status | Supported | Supported | Liveness, activity, and attention remain separate states. `running on HOST` is shared-storage ownership, not a locally attachable Running entry. Normal provider exit removes Running before lease/status validation rather than showing a missing-identity warning. |
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
| W09 | Detach one view, Soft Quit shared UI, and confirmed hard quit | Supported | Supported | Soft Quit leaves provider sessions alive; managed Windows recreates a missing outer UI on the same revalidated dedicated server and exact available entry-TTY size before direct/bridged reattach. Views of one UI are not independent workspaces. The first chooser has visible mouse buttons; its hard-quit button only opens a keyboard-only final confirmation, while both steps retain `y`/Enter semantics. |
| W10 | Soft restart and exact workspace/session recovery | Supported | Supported | Ambiguous identity becomes visible unresolved state rather than a guessed binding. Actual Linux procfs may supply rollout-FD proof; managed Windows treats projected MSYS2 procfs as non-authoritative and permits only a completely fenced single-candidate promotion. |
| W11 | Multiple attached terminals | Conditional | Conditional | Shared focus/layout and tmux `smallest` geometry; simultaneous input can interfere. |
| W12 | Shared config file, standalone editor, and in-product persistent Options | Supported | Supported | `railmux config` works without tmux, uses a temporary interactive screen, validates program/locale overrides, and edits the same remote or local TOML authority; one-time confirmations stay action-local. |
| W13 | Local and remote privacy-safe diagnostics | Supported | Supported | `doctor --json` and text share one redacted snapshot authority. |
| W14 | Dedicated tmux watchdog and incident reporting | Supported | Supported | A client may exit after repeated failures but never kills/restarts tmux or a provider. |

### Full-window SSH client

| ID | Capability | Status | Important boundary |
|---|---|---|---|
| S01 | Pre-attach package/protocol/config/tmux compatibility handshake | Supported | Runs before tmux lookup, creation, lock, PTY allocation, or attach; invalid remote config and configured-tmux failures remain distinct. POSIX discovery remains the default; an explicit or detected managed Windows host uses a shell-neutral direct command. |
| S02 | Consent-based remote user install or private venv repair | Supported | Exact compatible package; never `sudo`, system package installation, or shell-profile edits. Explicitly approved POSIX installs have a visible 300-second install-plus-handshake bound and distinguish timeout from installer exit. Managed Windows remotes deliberately fail closed with manual user-level pip/runtime commands rather than receiving the POSIX installer. |
| S03 | Consent-based local upgrade when remote is newer | Supported on POSIX local runtimes | Re-execs only after the same interpreter imports the requested version. Managed Windows app layers are immutable and instead show native PowerShell/runtime update instructions. |
| S04 | Immediate restoring surface and bounded startup stages | Supported | First validated keyframe replaces it; setup prompts remain cooked-mode. |
| S05 | Coalesced latest-state keyframes and row patches | Supported | Slow display output must not flow-control the real provider pane. |
| S06 | Default-on bounded automatic reconnect | Supported | Only after a first frame; bottom-right retry status and display-only SSH keepalives bound silent outages; no install, takeover, session creation, detach, or provider mutation. |
| S07 | Heartbeat lease and stale-helper cleanup | Supported | Stops only the helper's exact private tmux client, never the workspace or agents. |
| S08 | Independent cached local history per agent pane | Supported | 300-line coherent routing/history cache for immediate first scroll, asynchronous 2,000-line cumulative pages near its top, configurable 2,000–20,000 cap; one coherent tmux result validates controller identity with pane geometry, short/incomplete caches still enter atomically, unaligned captures are never spliced, and ordinary Codex history preserves cross-row SGR state from styled raw pane capture without switching format merely because a Preview locator exists. |
| S09 | Page Up/Down through verified agent history | Supported | Sidebar and dialogs retain their ordinary keys. |
| S10 | Claude native history or styled local transcript policy | Supported | Choice can be persistent or current invocation; Codex normally uses tmux/history capture, with canonical transcript fallback gated by exact provider rollback evidence plus a matching branch marker. |
| S11 | Pane-bounded local drag selection and automatic copy | Supported | Visible physical rows only; no autoscroll, soft-wrap join, or cross-pane selection. |
| S12 | URL/path hover and clean-click recognition | Conditional | Hover needs mouse-motion reporting; open acts only in the already-focused agent route. |
| S13 | Open HTTP(S) URL in the local browser | Supported on named local platforms | The remote never receives the browser action. |
| S14 | Read-only remote path validation and managed Vim/open-directory action | Conditional | Unquoted paths without spaces; remote identity/path is revalidated before action. |
| S15 | Separate local terminal for remote path | Conditional | Terminal.app, common Linux terminals, and Windows Terminal from WSL; Termux copies the command. |
| S16 | Native clipboard writer with bounded OSC 52 fallback | Conditional | macOS `pbcopy`, Linux Wayland/X11 tools, WSL `clip.exe`, or terminal-approved OSC 52. |
| S17 | Bracketed paste and terminal focus-event projection | Supported | Only allowlisted modes cross the display protocol; modes are restored on every exit path. A bracketed paste remains opaque across local/wire read boundaries, including embedded Railmux escape, Page, focus-like, and mouse-shaped bytes. |
| S18 | Termux soft-keyboard projection, touch recovery, and prompt cursor | Field-validated | Android-specific behavior is entered only from Termux environment evidence. |
| S19 | Emergency local `Ctrl-]`, normal tmux detach, and lifecycle exit classification | Supported | Local escape never becomes provider input. During an established display, `Ctrl-C` remains remote input even when Windows surfaces it as a native console signal; setup and reconnect retain local cancellation. |
| S20 | Consent-based `railmux config --remote HOST` | Supported on POSIX and managed-Windows local runtimes | Two SSH phases pin one POSIX/direct launch family; the probe never sends the display start token or touches a tmux server, the cooked editor preserves local-only history capacity, and Windows remotes require a preinstalled compatible managed runtime. |

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
| Windows Terminal launched by the native Windows bootstrap | Field-validated | Windows 10 and Windows 11 owner validation plus unconditional bootstrap/archive/runtime CI; keep resize, mouse, clipboard, IME, and terminal restoration in the release checklist. |
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

Railmux for Windows uses a native bootstrap to enter one POSIX Railmux runtime,
not a second Console/ConPTY UI implementation. The checklist below is the
continuing release contract for that supported surface; every affected item
must remain covered when the adapter, shared UI, or terminal behavior changes.

1. `pip install`, package import, CLI parsing, bootstrap configuration, version
   check, update, and privacy-safe diagnostics work under supported Windows
   Python without importing POSIX-only Railmux modules before handoff. An
   AppX/MSIX-packaged Python must place executable runtime state where native
   PowerShell and MSYS2 child processes see the same non-virtualized files;
   traditional Python must retain its existing LocalAppData runtime.
2. Offer an explicit, cancellable managed-MSYS2 installation with approved
   pinned sources, one fixed integrity digest across safe source fallback,
   bounded speed probes and exact-offset resume, visible bounded download
   progress, fresh package-mirror selection, persistent verified download
   caches, network-failure recovery, bounded phase progress/heartbeats,
   complete UTF-8 diagnostic logs, private ownership, and no system-wide PATH
   or shell-profile edits. Never silently adopt or modify a user-owned MSYS2.
   A storage-location transition may reuse only a base archive that still
   matches its pinned size and SHA-256; it must not migrate incomplete staging
   or executable runtime trees across visibility boundaries.
   Tar POSIX modes must never become NTFS read-only attributes on
   package-owned paths; pacman must be able to replace every staged base file.
   Run MSYS2's full upgrade from fresh Windows-launched processes at least
   twice. Treat termination as a successful core-runtime handoff only after the
   exact pacman restart announcement, bound the restart loop, and require a
   final pass that does not request another restart before publishing the base.
   Stop GnuPG daemons by the private pacman keyring home before staging
   activation; never use a machine-wide process-name or loaded-module kill.
3. Key one private shared base authority by a generation identifier independent
   of the pinned MSYS2 archive date, record an exact package-content identity
   for rolling repository results, bind each new app marker to it, and keep
   Railmux application venvs version-isolated beneath it. Require tmux 3.7 or
   newer in the staged inventory before publishing the Windows generation.
   Resolve tmux from an exact hash-pinned package and detached signature rather
   than trusting rolling mirror metadata to select its version.
   Make base creation and app upgrades transactional and recoverable; bump the
   generation whenever its required contents change. Never adopt or overwrite
   user-owned MSYS2 files. Before entering a changed generation, detect
   marker-proven older-generation processes and native provider descendants
   still attributable through the same process snapshot;
   busy or ambiguous state must fail closed with exit/restart guidance rather
   than duplicate a provider writer or adopt the older tmux server. Explicit
   uninstall must re-prove that the private
   generation is idle, atomically isolate only Railmux-owned runtime/cache
   trees, and leave provider histories untouched.
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
9. Prove local session restore, `railmux ssh` to Linux/macOS/Unix hosts, and
   ordinary OpenSSH login to the same Windows account followed by `railmux`.
   Desktop and SSH entry surfaces must attach the same workspace even when
   Windows assigns different Terminal Services sessions. A Linux/macOS
   display client may run the remote server only through a preinstalled,
   version-compatible managed Windows runtime; automatic Windows
   install and arbitrary native runtimes remain out of scope. Remote config and
   read-only doctor use the same pinned direct launch family once that runtime
   is present.
10. Add unconditional native Windows bootstrap/import/package CI, full pinned
    archive extraction plus measured-mirror two-pass base-update and native
    executable-loading CI, durable core-transaction restart evidence, and a real
    managed-MSYS2 runtime smoke. If hosted CI cannot exercise the runtime,
   record a named and dated real Windows Terminal pass here for every release
   that changes the corresponding manual behavior. Mocked OS branches and
   ordinary WSL evidence cannot close the managed-MSYS2 path.

The release owner reported the complete dev35 Windows manual checklist passing
on 2026-08-07, including the final click/Preview/running-session performance
changes. The dedicated Windows 10 host then completed the exact version-boundary
transition (`dev35` → `dev36` → `rc1`), reused its verified 96-package base,
attached and detached both new layers, reported the rc1 UI ready through
`doctor`, matched its content verification, and retained dev35/dev36/rc1 in a
non-mutating prune dry-run. Unchanged terminal interactions were not repeated
mechanically. Rc7 replaces that preview base with the first release generation;
its fresh-install, tmux 3.7 package-floor, and uninstall checks remain the named
real-Windows RC gate in the parity ledger.

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
