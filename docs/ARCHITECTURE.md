# Railmux architecture invariants

This document records constraints that should survive implementation changes.
It is intentionally separate from the user-facing README. Read it before
changing providers, mode switching, outer tmux panes, previews, or restore
state.

## Railmux owns a dedicated tmux server

Every production launcher and remote display helper addresses the non-default
`railmux` tmux socket explicitly. Starting Railmux from a foreign tmux client
nests into that dedicated server after removing the inherited `TMUX` and
`TMUX_PANE` values only from the replacement process. The internal
`--inside-tmux` entry point fails closed unless its current Unix socket is the
same socket resolved through the dedicated `-L` label; a matching basename is
not proof of identity.

Once that boundary is validated, internal bare `tmux` commands deliberately
inherit `TMUX` and therefore remain scoped to the dedicated server. Commands
that run outside tmux, including every `railmux remote-server` query and its PTY
attach, must use the explicit socket argv helper. Startup swap recovery is
temporarily scoped to the already-proven dedicated socket before
`new-session -A`; it must never inspect or mutate the caller's/default server.
Tests use a randomized non-default socket and may kill only that exact private
server. Socket migration never edits provider rollouts or session files.

Upgrade compatibility is deliberately asymmetric. New sessions are created
only on the dedicated server, while pre-isolation sessions on tmux's `default`
server are inventoried read-only and rendered in the same Running sidebar.
Their internal identity includes the legacy server socket/PID and immutable
tmux session ID, so equal provider IDs and equal human-readable tmux names can
coexist without routing actions to each other. Cross-server display always uses
a nested `attach-session -f ignore-size`; it must first return any swap-owned
dedicated pane home. Automatic teardown never kills a legacy session. An
explicit user Kill may do so only after revalidating both pinned identities.
The nested wrapper carries a bounded source marker containing no socket path.
SSH history capture re-resolves its declared server scope, PID, immutable
session ID, and sole live pane before reading scrollback from the real source;
the wrapper's geometry remains the only pointer authority. This read-only path
must never resize, swap, send keys to, or otherwise mutate the legacy session.
This is a deprecated upgrade bridge, not a second supported storage model.
Remove `legacy_sessions.py`, the `_Running` legacy fields, and their routing
branches together after a documented compatibility window and after supported
installations no longer report default-server candidates through `doctor`.

The ordinary launcher retains a thin parent outside the attached tmux client,
and the SSH display server is already outside its private attach client. Each
performs one low-frequency, identity-pinned health probe and requires three
consecutive failures before declaring the dedicated server unresponsive. A
terminal failure stops only the owned client, restores saved terminal modes,
and records a bounded incident in the private runtime directory. It must never
kill/restart tmux, apport, or provider processes automatically. `railmux doctor`
reports current dedicated-server reachability and the privacy-safe last incident
without exposing socket paths or tmux/session identities. Intentional hard quit
is distinguished from an abrupt server disappearance by a private, exact
server-PID/session-ID sentinel that is consumed once and expires after 30
seconds. A committed soft quit publishes a separate exact 30-second intent
before pane teardown. It is non-consuming because every helper attached to the
same managed session must independently return the soft-quit result. Helpers
consult it before any post-exit tmux query, so destroying the managed session
cannot be misreported or queried concurrently by its closing SSH views. These
sentinels classify lifecycle only; they never authorize session mutation or
recovery.

Independent tmux mutations that form one startup or status-bar transaction are
sent through one tmux client, and an identical complete bar frame is not
rewritten. This is a performance invariant on runtimes where spawning each tmux
client is expensive, but it does not weaken option restoration or the
crash-safe binding leases. A compact terminal with only Railmux's sidebar has
no agent page to select or zoom, so its logical compact presentation must not
perform agent-page geometry probes.

Doctor collection has one versioned structured snapshot authority. Human text
and `doctor --json` are renderers of that same snapshot, not independent probe
paths. Stable JSON fields contain only bounded status/category values, numeric
versions and counts, booleans, coarse incident age, home-relative paths, or the
literal `<custom>`. Neither renderer may expose hostnames, usernames, session
or pane IDs, transcripts, environment values, configured commands, socket
paths, credentials, or raw custom paths.
The same snapshot includes one optional, local `railmux ssh` record from the
private runtime directory. A stable lock file protects atomic replacement;
the newest attach owns an opaque internal token, so an older client's final
write cannot overwrite it. Doctor exposes only package/protocol versions,
coarse age, bounded counters, and a fixed outcome. It never records the SSH
destination, arguments, session/pane identity, content, paths, or raw errors.
An `in_progress` record is not treated as a lifecycle authority: it may denote
a live connection or one that ended before recording its outcome.

The SSH display's headless terminal must implement every screen-content
operation that its private tmux client advertises through `TERM`; sending a
new keyframe cannot repair divergence already present in that server-side
model. The current `xterm-256color` compatibility layer extends pyte 0.8.2 with
parameterized scroll-up, scroll-down, and repeat-character (`CSI S/T/b`) and
uses the same model for live frames and styled history. Tests against a real
isolated tmux PTY must compare the reconstructed pane with tmux's own captured
state whenever that advertised capability boundary changes.

Local SSH history is an overlay, not a pause in the live screen model. Each
visible agent pane may own one immutable snapshot and offset; incoming live
rows are painted first and every intersecting frozen rectangle is repainted in
the same terminal write. Periodic prefetch may refresh routing but must not
move an existing viewport or replace a deeper cached timeline with its
300-line hot suffix. After a periodic snapshot is accepted, further periodic
captures are suppressed until a newer screen update can have made that cache
stale; route changes, reconnect, and bounded policy-recovery refreshes remain
immediate. For a stable pane, geometry, and history source, uniquely
aligned snapshots are merged into one newest-bounded timeline. Unaligned hot
captures are never spliced: the cache switches to the newest internally
contiguous suffix, then recovers older rows from a cumulative deep page. The
periodic routing capture retains 300 rows, enough to
enter history immediately and defer the 2,000-row cumulative request until the
viewport approaches the top of that coherent suffix. If byte budgeting or an
older peer supplies fewer than 300 rows while reporting older content, the
first wheel-up keeps the live pane visible until one cumulative response
arrives, then enters history atomically at the requested offset. A validated
deep response replaces its pane cache as one capture rather than merging two
style generations. Additional wheel-up ticks received during an initial
bounded request accumulate into its eventual offset. An existing frozen
viewport retains its immutable snapshot while that mutable cache changes.
Native Claude, local transcript, and
undecided history are separate sources and are never merged. Protocol v15 also
carries the opaque pane-local Codex history
generation; a changed non-content generation replaces that pane's cached
timeline instead of merging across a confirmed rewind. A rejected deep
response does not mutate the reusable cache. Deep
history begins with 2000 physical lines and requests cumulative 2000-line
expansions only as the viewport approaches the oldest loaded content.
Expansion stops at the local
`[ssh].history_lines`/CLI cap (default 10000, bounded to 2000-20000) or when the
server returns fewer lines than requested. This setting is local-only and is
not an in-TUI Options authority. A deep response may replace its previous
snapshot only when the visible multi-line anchor has one exact match, so both
live output and newly prepended history leave the viewport stationary. The
unmodified terminal Page Up/Down sequences move one visible page only when the
keyboard cursor resolves to a verified agent route; sidebar and modal
navigation remains remote. The server retains the newest suffix if styled
history reaches the protocol byte
budget; a byte-bound truncation is an effective end, never a helper failure.
Styled raw capture is decoded as one chronological terminal stream because
tmux may carry SGR foreground, background, and text attributes across physical
row boundaries. Each decoded row is then reset and re-encoded as an
independently paintable overlay row; parsing rows from a default style must not
drop inherited diff gutters or let a prior style leak past an explicit reset.
Input or bottom restores only the routed pane; layout uncertainty, resize,
sidebar input, and `Esc` fail closed by removing every incompatible overlay.
When the display helper creates the default Railmux session, it explicitly
disables the older remote tmux copy-mode coalescer because this local history
layer owns agent wheel input. Ordinary `ssh` followed by `railmux` retains the
copy-mode coalescer, and attaching to an existing Railmux session never changes
that session's setting.

The local SSH client recognizes Termux only from its local environment, never
from terminal dimensions or the remote host. Termux uses DECSET 1002 button
tracking plus SGR coordinates because its emulator does not implement DECSET
1003 any-event tracking; desktop clients retain 1003 for pointer hover. An
unmodified left press within one row of the provider's input coordinates,
inside the same verified live agent route, temporarily disables local mouse reporting so
Termux can open its soft keyboard on the next tap. Compact status navigation is
classified before this prompt gesture, so stale agent geometry can never turn
an `R`/`A1`/`A2` tap into a keyboard handoff. History overlays, sidebars,
modals, previews, and other panes fail closed. DEC cursor visibility
is presentation-only: Codex or Claude may hide its hardware cursor while
retaining exact input coordinates, so matching click and cursor routes plus
the bounded row distance remain authoritative. Mouse reporting resumes
as soon as a projected
soft keyboard is observed—the keyboard is already open and no longer needs
pointer ownership—or after the first keyboard input when no resize is
observable. When the projected viewport closes, the client toggles and
reasserts its DEC mouse modes rather than trusting Termux's retained logical
mode bit; a second bounded reassertion shortly after the close resize covers
Termux completing its native touch handoff after SIGWINCH. This returns drag
and click ownership to Railmux even when Termux keeps native touch handling
after the resize. A rapid keyboard reopen cancels that delayed reassertion.
The projection state remains
bounded so a missing or inexact keyboard-close resize cannot leave touch input
permanently owned by Termux. If that bounded timer expires while the keyboard
is still open, only a passive close-reassert marker remains; it captures no
input and is consumed by the later usable resize.
While this confirmed keyboard handoff owns the local transition, an exact
Termux focus-out is consumed instead of being forwarded as a real application
blur. The client reasserts focus-in at the keyboard projection, input, close,
or bounded recovery boundary only when the current remote application requested
terminal focus events. This keeps its prompt cursor visible without changing
desktop focus semantics or exposing a focus sequence to applications that did
not request one.
Desktop terminals never enter this path.

A native/local Railmux process on Termux has a different boundary: input over
an agent pane reaches tmux's root mouse table and never passes through the
sidebar's Python/Urwid input loop. On tmux 3.0+, the shared crash-safe binding
lease therefore wraps only tmux's exact stock `MouseDown1Pane` binding. The App
publishes one fail-closed window route consisting of the live Target agent pane
and three accepted pane-relative rows around its current application cursor.
Copy mode, a frozen selection peer, preview/help/tool content, a dead pane,
unknown geometry, a non-agent Target, or a custom user left-click binding leaves
the stock click path unchanged. Cursor visibility remains presentation-only.
tmux 2.7-2.9 lacks the `mouse_pane` and `mouse_y` format authority required by
the wrapper, so those supported servers retain byte-equivalent stock behavior
without local tap assistance.

An authoritative first press selects its explicit pane but is not forwarded to
the provider: disabling mouse after forwarding only the press can strand the
provider in a drag when Android owns its release. A background shell writes a
unique window nonce before turning the exact owning session's `mouse` option
off, shows the same “Tap the prompt again” hint, and restores `mouse on` after
eight seconds only if its nonce still owns the handoff. This watchdog survives
a killed App. The periodic geometry path restores early on the first same-width
keyboard-height contraction, normal/soft teardown restores any matching nonce,
and the later close projection toggles `mouse off; mouse on` so Termux observes
fresh DEC ownership. Route options are window-scoped; the tmux `mouse` option
is session-scoped, so the handoff is deliberately short and always restores to
the `on` state Railmux requires for the rest of that session.

Managed Claude Code panes may advertise a verified transcript source without
using it. The remote-workspace `[ssh].claude_history` policy is `ask`, `local`,
or `native`: `ask` makes the first upward wheel gesture open a local-only
keyboard/mouse dialog, `local` renders the bounded read-only transcript into
the ordinary history overlay, and `native` forwards wheel input to Claude Code.
The dialog offers persistent and current-invocation variants for both routes.
The client waits for the helper's applied-success response before changing
wheel ownership; a current-invocation choice is replayed after automatic
reconnect without changing the settings file. A later persistent Options
change supersedes that connection override and becomes authoritative on the
next history capture; it does not require a Railmux restart. The wait is
bounded; on timeout the client refreshes the authoritative remote policy
without clearing another pane's frozen viewport. The dialog, paired
mouse press/release suppression, and status never enter tmux or alter another
attached client.

The SSH compatibility handshake precedes every tmux lookup, session creation,
lock, PTY allocation, or attach. The remote reports a bounded package version,
private protocol version, SSH-extra readiness, and tmux availability, then
waits for an exact client acknowledgement. Equal protocol versions are the
compatibility authority; package versions need not be equal. A higher remote
package version is offered to the user as an explicit local upgrade before any
other version remedy. A missing, older-protocol, or dependency-incomplete
remote may be installed only after explicit consent and only in the remote user
environment. Installation normally selects the exact local version; repairing
the SSH dependency after a declined local upgrade preserves an already newer
remote version instead of downgrading it. Automatic setup may probe Python/pip
commands but must never run `sudo`, edit shell startup files, or install tmux.
When a remote Python rejects user-site installation, a second explicit consent
may create the fixed `~/.local/share/railmux/ssh-venv` and install there. The
bootstrap probes that path without PATH changes; failed setup never deletes or
replaces an existing environment.
The local upgrade uses its current Python environment and re-execs the original
`railmux ssh` invocation only after pip succeeds and a fresh process using that
same interpreter imports the requested exact version. Failure or an import
mismatch leaves tmux untouched and prints a reproducible manual command.

Before that cooked-mode handshake begins, the local client paints the same
terminal-native workspace-restoration surface as a direct launch. It is local
feedback only: it grants no protocol authority and is replaced by the first
validated display keyframe. Authentication, compatibility, installation, and
attach prompts remain cooked-mode interactions.

Protocol v15 reports a second bounded status after the attach boundary and
before the first binary display frame. Current helpers may coexist: a flock
serializes only immutable-session validation plus exact child-PID attach, and
is released before display service begins. Every helper sends heartbeats; 45
seconds without a complete input frame expires only that helper's lease and
stops only its exact private tmux child. A live protocol-v6 helper may still
hold the old flock for its lifetime. Replacement therefore requires local user
consent, validates the existing managed session before mutation, detaches only
clients re-enumerated under that immutable session ID, acquires the bounded
lock, repeats enumeration to close the attach race, and never kills a session,
pane, or provider process.
One BUSY status is treated as ordinary v8 attach contention: the local client
starts one fresh non-replacement helper before offering takeover. Only a second
BUSY is persistent enough to justify the destructive-sounding consent prompt.

Default-on automatic reconnect is a narrower post-attach path. It becomes eligible
only after at least one valid screen frame and only for an unexpected reaped
process status; local EOF/escape, native detach, soft quit, and hard quit are
terminal outcomes. A retry uses a fresh compatibility hello and ordinary
`replace_existing_client=false`, `existing_session_only=true` attach with
non-interactive SSH authentication. The internal existing-session boundary
validates rather than creates the managed outer session, so a network failure
racing with Soft Quit cannot resurrect the UI. It never enters install,
upgrade, confirmation, or takeover paths and never detaches or kills a tmux
client, session, pane, or provider. The bounded retry window exceeds the remote
helper's 45-second half-open lease, while each hello, attach wait, and backoff
also watches local stdin so `Ctrl-]`, `Ctrl-C`, or EOF restores the terminal
immediately. The full-window SSH argv appends a five-second OpenSSH server-alive
interval and three-miss limit after user arguments; OpenSSH's first-value
authority therefore bounds a default network black hole while preserving an
explicit user override. Railmux's protocol-critical `-T` (or remote config's
`-tt`) follows user SSH arguments so a copied tty flag cannot invert binary or
cooked transport. Reconnect similarly keeps its safety-critical `BatchMode=yes`
before user options while placing its bounded `ConnectTimeout` afterward, which
prevents prompting in raw mode without overriding an explicit user timeout.
Only frames painted by the current helper qualify another automatic retry. The
last valid frame and bounded history cache may remain
painted with a local reconnect status, but their cursor and pointer geometry
cease to be input authority immediately. A new decoder/model accepts only a
fresh keyframe; that keyframe removes the reconnect status and refreshes pane
routes, while cached pane content is reusable only after a fresh multi-line
timeline anchor. Before replacement, the local surface disables the old
helper's bracketed-paste and focus-event modes so the fresh keyframe must re-arm
them and can deliver focus-in to the new tmux client. A presentation-only
reference to the stale status row may survive until that keyframe solely to
place and colour local reconnect progress in status-right; it never restores
stale cursor, pointer, or routing authority. Reconnect status rendering must not
leave terminal autowrap pending or override the keyframe's final cursor state.
Non-interactive retry SSH diagnostics never write directly into the retained
alternate-screen surface; bounded Railmux status remains its only reconnect
feedback.

## Modes are registered providers, not a boolean

`railmux.modes.ModeRegistry` is the ordered source of shared mode metadata.
The application stores a stable `_active_mode_key`; `m` cycles registry order.
Do not reintroduce paired fields such as `claude_selection` / `codex_selection`
or a new `is_<provider>` boolean.

Each mode owns its sidebar view state through `_ModeViewState`, keyed by its
stable registry key. Project selection, and future filters/cursors, must be
resolved only against that mode's currently visible objects. A mode with no
projects must never retain another mode's project as a hidden action target.

Provider-specific backends remain responsible for discovery/indexing, launch,
resume/delete, transcript parsing, and status inference. Shared UI code should
branch on declared capabilities (`project_source`, `login_shell`, etc.), not on
the assumption that exactly Claude and Codex exist. Adding a truly new backend
will require a backend adapter, but must not require redesigning mode cycling or
per-mode state.

## Sidebar rows are disposable views

The periodic refresh publishes value snapshots to Projects, Sessions, and
Running panes. Each pane skips an unchanged snapshot but may discard and rebuild
all row widgets as soon as any rendered value changes. A row therefore has no
stable lifetime: never store timers, click tracking, drag state, or other
interaction authority on a row instance.

State that must survive refresh belongs on the pane/application or in a shared
controller keyed by stable identity (`encoded_name`, `session_id`, or exact
tmux name). `ClickableRow`'s class-level double-click state and `click_key` are
the reference pattern. Rendering caches are an optimization only and must not
become a second state authority.

Click intent must also survive controller redirects. In particular, opening a
Sessions row may discover that its provider is already live and redirect
through the Running action. Carry the explicit double-click intent through that
chain; `steal_focus=False` is not a substitute because ordinary single-click
selection uses the same value.

Portable soft-restart state writes the stable active `mode` key inside a
per-mode view map. The ownerless `codex_mode` boolean remains a read-only
migration fallback for Railmux 0.1.x files; it is never copied into new state.

User edits and app-mutable choices share the single atomic `config.toml`
authority. TOMLKit updates preserve comments, ordering, formatting, and unknown
keys; a parse or write failure leaves both disk and in-memory authority
unchanged. Layout retention and Codex auto-run expose `always`, `ask`, and
`never`; invalid values fail closed. Policy and profile updates are one write,
so UI state cannot disagree with launch or exit behavior. `ask` means once per
Railmux run for Codex auto-run and once per exit after an explicit layout
change for layout retention. Changing either policy never mutates a running
agent or the current pane geometry. A current-run YOLO choice remains in memory;
a next-launch-only layout profile is removed from the same TOML file only after
successful application.
Legal policy sets and activation boundaries are declared once in
`setting_contracts.py`: SSH history capacity applies to the next local SSH
invocation, persistent Claude history applies to the next remote history
refresh (and subsequent connections), and the remaining saved policies apply
only at their documented launch/exit boundary.

The standalone cooked-mode `railmux config` editor is another view of this same
TOML authority and dispatches before the tmux dependency check, so a broken or
missing tmux path cannot prevent its own repair. Category and setting resets
remove only declared Railmux keys and preserve unknown tables; malformed TOML
is replaced only after explicit consent and a sibling backup. Persistent menus
never store action-local `This time` choices. Program values remain argv-only:
the editor checks executability with a bounded version probe and never accepts
shell fragments. It never discovers or queries a tmux server while editing;
server/client compatibility is enforced at the next ordinary launch. Claude
and Codex use their configured executable directly. When both streams are
TTYs, the editor owns one alternate-screen lifetime and restores it in a
`finally` boundary. Navigation is state-driven: category pages redraw a compact
root navigator above their contents, setting pages use a breadcrumb, and one
action result survives in the footer until the next input. Back redraws only
the parent state, so neither stale child content nor menu history can enter the
caller's primary scrollback. Help and redirected streams never enter terminal
presentation mode.

`railmux config --remote HOST` uses a versioned remote-config capability that
is independent of the binary display protocol. Phase one reuses the bounded
remote executable/bootstrap ladder over `ssh -T`, reads one compatibility
hello, and terminates without sending the display start token. It may run only
bounded executable/version checks; it never discovers, creates, attaches,
resizes, detaches, or kills tmux. A missing/older remote package can be
installed only with the same user/private-venv consent boundaries as
`railmux ssh`. Phase two is a fresh `ssh -tt` process invoking the discovered
entry point's cooked `config --remote-context` command. The remote context
hides and preserves `ssh.history_lines`, which belongs to the initiating local
display client, while retaining remote-workspace Claude history and clicked
path policies. Public SSH-facing commands use grouped `--ssh-args` values with
one ordered argv authority across both phases. Group parsing uses bounded
POSIX quoting locally and never invokes a shell. The released singular
`--ssh-arg` remains a hidden exact-argv compatibility input.

The tmux executable is a process-wide authority. An absolute override must keep
the conventional `tmux` basename; Railmux prepends only its directory for
Python calls and embeds that argv-only executable into generated tmux
run-shell helpers, so the two paths cannot select different clients. After an
existing dedicated server is identity-validated, Railmux synchronizes only the
standard locale variables for future server children through the exact socket.
Existing panes and providers are never restarted. A
client/server protocol mismatch fails closed, preserves every session, and
directs the user to `railmux config`; stderr is classified into bounded error
categories rather than exposed raw.
Feature gates inside tmux query the proven server's `#{version}` rather than
the selected client's `tmux -V`. The bounded cache key includes the inherited
tmux socket/PID authority and configured client, so changing either cannot
reuse capabilities from another server lifecycle. Outside tmux, where there is
no proven server, the selected client's version remains the only available
preflight authority.
Identity-critical formats use printable, positionally parsed separators for
the tmux 2.7 floor: tmux 2.7 under `LC_ALL=C` rewrites literal tab delimiters
to underscores. Fixed identifiers are placed before any final free-form field,
and every parser fails closed on count, type, or identity mismatch.

Railmux does not expose arbitrary environment dictionaries. The optional
locale setting is `inherit` or one validated installed UTF-8 locale and applies
only to Railmux-owned future processes. Direct launch and the SSH remote helper
validate it before touching tmux. The SSH hello carries only bounded config
validity and configured-tmux booleans, allowing the local client and
`doctor --remote` to distinguish remote configuration repair from package or
system-tmux installation without disclosing paths or environment values.

Remote subcommands use `--remote HOST` as their public destination spelling.
The released `doctor --ssh HOST` spelling is a hidden compatibility alias only;
remove it when Railmux 0.4.0 is developed. It must not appear in help or new
documentation before then.

The three lists use horizontal labelled rules instead of independent boxes, so
adjacent section borders do not consume duplicate terminal rows. The sidebar is
deliberately flat: it has no decorative outer vertical rails, leaving the outer
tmux divider as the one structural vertical boundary. Stable section names are
uppercase; live item counts occupy a fixed right-hand slot, using
`visible/total` while filtered. Project-session counts likewise occupy a fixed
right-hand column so changing counts do not shift or truncate the project name.
Transient growth of the bottom Button Bar is charged to the Running section
rather than recomputing every weighted section, so More/​Less cannot move
Projects or Sessions. The Button Bar renders full actions as neutral filled
shortcut controls plus labels, collapses to filled single-key controls at
narrower widths, and keeps its mouse hit regions aligned with the visible
representation. `+` and `-` expand and collapse the second row without
requiring a pointer. A one-line filter temporarily removes that charge.

The focused section owns green upper and lower horizontal rules. When the next
section's title row doubles as that lower boundary, only the rule turns green
and the next title remains neutral. All other title rows and the final bottom
rule use the same subdued inactive gray. Weighted section heights are
deterministic for a given terminal size and must not change when focus moves.
The stable section name remains visible when dynamic title detail is truncated.
Wheel input over any title rule or the bottom rule is routed by pointer position
to that section's own `ListBox`.

## Restart state has two authorities

Instance-local recovery state lives under `XDG_RUNTIME_DIR` (or the existing
macOS-compatible `/tmp/railmux-UID` fallback). Its filename is derived from a
privacy-safe tmux server-lifetime digest plus the immutable outer pane ID. The
payload repeats that owner identity and is rejected unless it matches the live
instance. Session/window IDs are recorded as context but a move of the same
pane does not change ownership. Different panes, windows, sessions, and private
tmux servers therefore cannot overwrite or restore one another's local state.
The managed CLI session is the deliberate graceful-restart exception: its
controller pane exits with the session, so a private server-scoped handoff
points the replacement `railmux` session at that exact former owner. The
pointer is published only after the pane-owned snapshot validates, is accepted
only on the same tmux server after the former pane is dead, and is removed only
after restoration succeeds. Direct in-tmux instances retain strict
immutable-pane ownership and cannot consume this handoff.

The local schema may contain the right-pane target and validated running
bindings. It duplicates the current sidebar view so a shared portable
last-writer never changes an exact instance restart. Files are atomically
replaced as 0600 inside a verified user-owned 0700 runtime directory. Cleanup
is bounded and removes only recognized owners proven dead; unknown/newer state
and old but possibly-live private servers are retained.

Portable state lives beside `config.toml` and contains an active mode,
per-mode project/session selections and filters, plus an optional right-display
wish expressed only as provider mode, stable session ID, and project key. It
contains no tmux names, pane/process IDs, commands, environment values,
transcripts, or recovery authority. On restart Railmux may attach only after
the current tmux server independently rediscovers and validates that session as
live. If it is not live locally, the stable ID may select an existing transcript
for read-only preview but must never authorize resume, launch, kill, or process
adoption. A second node may therefore use it as a view default while ignoring
every node-local pane identity. The old fixed `railmux-state.json` has no owner
proof, so migration may extract only validated portable view fields;
right-pane and running-binding fields remain ignored and the legacy file is
left for manual cleanup.

Detached-session tmux stamps and swap-transport markers retain their own exact
lifetimes and validation. Runtime JSON is a cache and must not become a
competing authority for adopting, killing, or replacing an agent pane.

Legacy detached-session discovery still derives truncated tmux names with
`App._safe_name` and resolves them with `_resolve_truncated_id`. Their character
normalization and width must remain in lockstep; changing either side requires
updating the other and its recovery tests. Exact orphan and swap markers remain
the stronger authority and must never fall back to name resemblance.

New-session recovery uses `@railmux_orphan_v2`, a bounded session option written
onto an inert, finite-lifetime holder before the provider command is respawned
into its exact pane. Its schema contains only mode, placeholder key, immutable
tmux session/pane IDs, exact outer owner, normalized cwd, timestamp/random
token, resolution phase, and (after proof) provider UUID. Commands,
environment, prompts, transcripts, and credentials are forbidden.

The lifecycle is `launching -> unresolved -> resolved`. Startup may adopt only
a marker whose live immutable tmux objects and supported mode validate. A live
different outer owner fences concurrent Railmux windows; if that exact owner
pane is absent from a successful full-server snapshot, a new instance may take
over only after a crash-safe compare/write/readback owner claim. Snapshot or
claim failure stays unresolved, and concurrent claimants cannot both adopt. Linux
resolution requires descendant/open-rollout correlation where available; a
procfs error is ambiguity, not permission to guess. Without exact correlation,
only one candidate fenced by a complete pre-launch snapshot may resolve.

Resolution commits the marker's UUID before changing the in-memory registry,
so interruption is idempotent. Until that commit, attach and stop callbacks
carry the marker token and recheck live session/pane identity. Stopping an
unresolved entry may kill only that exact tmux identity and cannot delete a
provider file because no provider UUID is authorized.

A resumed Codex parent may close its rollout descriptor after `task_complete`
while background rollouts remain open in the same process. Recovery must not
interpret those sibling descriptors alone as a different writer: an exact
NUL-delimited process argv element equal to the stamped resume UUID preserves
the binding. A substring, rendered shell command, cwd match, or sibling rollout
without that exact argv evidence must retain the stale-writer veto.

Provider history roots are also the cross-host single-writer authority. When
`CODEX_HOME` or the Claude Code configuration/history root is shared, Railmux
stores rendezvous files under that same root and takes non-blocking advisory
locks before resume. They request private POSIX modes where the filesystem
supports them; type, ownership, and no-symlink checks remain authoritative on
mode-masking DrvFs/CIFS-style mounts. A Claude lease names its session UUID; a
Codex resume atomically covers every alias known at launch, while later rewinds
that the index can link retain that stable lineage anchor instead of
accumulating holder processes. The lock is authority and its bounded JSON owner
record is diagnostic only. It is flushed before the claim becomes usable so a
second NFS client that observes the lock can name its owner without waiting for
the provider-lifetime descriptor to close. An unlocked stale file is inactive,
while an unavailable lock service fails resume closed.

The acquired descriptors transfer to a small independent holder tied to the
exact provider pane PID and process-birth token. They therefore survive UI
Soft Quit, but are released by the operating system when that pane exits or the
holder dies. New sessions acquire their UUID immediately when placeholder
resolution proves it. A live UI periodically revalidates the advisory locks
rather than trusting its in-memory lease list, so it can replace an
unexpectedly dead holder while the exact provider pane remains alive; a sticky
Running-row warning remains visible until protection is restored. Confirmed
deletion also takes a fresh lease (or verifies the exact locally owned provider
pane) and refuses to touch history owned on another host. A remote owner may
annotate a Sessions row as `running on HOST`, but cannot enter the node-local
Running registry or authorize attach, kill, delete, or process adoption.
Node-local immutable tmux identity remains the authority for all of those
actions.

Lease files are deliberately persistent rendezvous points. An unlocked stale
record is inactive and consumes only one small file per provider UUID; unlinking
a locked pathname would permit two hosts to lock different inodes. The shared
provider root must therefore provide real cross-host POSIX `flock` semantics.
Mount options or filesystems that silently make locks node-local cannot be
detected reliably by Railmux and are outside this guarantee; an explicit lock
error always fails resume and delete closed.

## Session indexes publish immutable generations

The Codex history tree is owned by one `BackgroundCodexIndex` worker. Urwid
ticks only query its latest immutable `IndexSnapshot`; they must never call the
underlying `CodexIndex` tree walk or rollout parser. Repeated requests coalesce,
ordinary scans are rate-limited, and placeholder discovery may request a
shorter bounded interval without creating another worker or an unbounded scan
loop.

Each successful publication increments a generation and carries complete
frozen `SessionMeta` values. Do not reconstruct a selected subset of their
fields at this boundary: provider-specific fields such as attention state must
survive unchanged. Renames are a read-time overlay; delete uses a temporary ID
tombstone until a later generation confirms removal. Neither mutates a
published snapshot.

Compound operations pin one generation, including both query methods and
`current_snapshot()`. Startup requests the first scan before recovery, but an
exact live tmux marker/stamp must remain visible in Running even while the
index is still at generation zero. In particular, a resolved marker whose
immutable tmux session/pane identity validates stays resolved: live-writer UUID
and rewind-lineage checks require the first coherent index generation and may
not temporarily demote it to an unresolved placeholder. Such an entry is
provisional: the first coherent generation removes and strictly re-adopts it so
metadata can refine its label or reject a wrong cwd or writer.

History is a provider projection, not a dump of append-only terminal bytes.
For Codex, every preview resolves any lineage alias to the newest indexed
rewind rollout and renders that file alone: the rollout already contains the
retained prefix plus its active replacement suffix. For Claude Code, semantic
JSONL records are buffered within the bounded preview input, the newest
non-sidechain UUID is the active tip, and only its `parentUuid` ancestry is
rendered. Missing parents at a tail boundary stop traversal safely. Neither
projection rewrites provider data, and abandoned suffixes are not part of the
default history view.

A live Codex rewind or running-turn steering also advances the canonical
rollout while retaining the same provider pane. Railmux baselines the first
indexed rollout for each live entry, but a direct canonical child alone is not
branch evidence: Codex may create the same parent/child shape while
bootstrapping an ordinary resume. An explicit-resume generation is initially
unproved. Its first exact open child is adopted as a raw-history bootstrap
generation unless the parent was already born/adopted in this pane generation
or its indexed real-message count advanced after the baseline. Once adopted,
that child is proved for later direct transitions. This conversation cursor,
not rollout mtime or size, prevents startup/config writes from authorizing a
false rewind. Where procfs is available, every candidate child must additionally
be open in the exact pane process tree; a negative probe waits, while platforms
without procfs retain the same provider-link plus generation-evidence rule.
After revalidating the real pane identity across either its detached home or
swap display location, Railmux leaves the live terminal and retained tmux
scrollback untouched. A pane-local generation marker is normally a plain
rollout UUID, meaning the full-window SSH history manager must use styled raw
pane capture. Only this confirmed branch transition may write the evidence-gated
`canonical-v2:` prefix and permit a transcript projection that excludes the
abandoned suffix; the prefix and transcript locator must name the same rollout.
Released `canonical:` markers fail back to plain raw history on upgrade because
they were produced by the older, insufficient direct-child test.
The transcript locator alone is never authority to change scrolling format.
Direct local wheel input remains native tmux/provider scrolling, while Space or
context Preview remains the explicit formatted provider projection. This sends
no provider input, never resets or resizes the pane, never deletes tmux history,
never touches provider history, and never mutates a legacy-server session.

While that first generation is pending, Codex Projects and Sessions display an
explicit `Indexing…` state with indeterminate section counts. Generation zero
must never be rendered as an authoritative empty list, filtered no-match, or
temporary empty Running view. A failed tmux probe, a generation with transient
errors, an unavailable initial source, or clean metadata that has not exposed
the actively-written rollout yet retains the provisional entry and instance
recovery file for a later generation.

Cold parsing is accelerated by a versioned, private, atomic metadata cache
under the user cache directory. It is an optimization, never a published
generation or recovery authority: a new process still walks the complete tree,
stats every rollout, and accepts a cached visible, hidden, or filtered result
only when its relative path and nanosecond-mtime/size signature match. The
cache is scoped to a hash of the resolved Codex sessions root; malformed,
oversized, wrong-root, unsafe-permission, or unknown-schema data is ignored.
Only a complete traversal may replace it, and changed/transient files retain
the existing retry rules. Parser semantic changes must bump the cache schema.

A failed or incomplete tree walk retains the last known-good generation. A
transient per-file error retains that file's cached metadata, publishes the
otherwise coherent generation with a bounded warning, and retries later. A
partial failure with no usable snapshot is not published as successful empty
state. Shutdown signals the daemon and waits only for a bounded interval, since
filesystem IO cannot be cancelled portably; a late worker may not publish
after close.

Claude's `SessionCache` remains a separate source. Its UI path scans only the
selected project directory, caps cold parsing to the newest entries, and parses
only changed files. Moving that bounded per-project source to another worker is
not required by the Codex whole-tree invariant, but any future worker must use
the same last-known-good generation rules.

`SessionMeta.message_count` is a provider-normalized logical conversation
count, not a raw JSONL-record count: exclude tool results and harness-injected
user context, and deduplicate provider records that share one assistant message
identity. `token_total` follows provider-reported usage. Codex token events are
cumulative, so the last valid total wins; Claude usage is summed once per
unique assistant message and includes reported input, output, cache-creation,
and cache-read tokens. The sidebar may compact these integers for display but
must retain the exact values in the immutable metadata snapshot.

## The agent workspace is independent of the sidebar mode

`AgentWorkspace` owns at most two `AgentSlot` objects: `primary` and
`secondary`. An agent slot owns every mutable fact about its outer display
pane: pane ID, attached background session, provider key, active session ID,
active project key, preview state, and preview restore target. Portable restart
state serializes only the stable provider/session/project subset of the active
slot; exact tmux display ownership remains instance-local.

The currently browsed sidebar mode and the providers displayed in agent slots
are independent. Switching the sidebar from Claude to Codex must not replace,
close, or reinterpret an already displayed Claude agent. Do not put display
pane fields back onto `App` as parallel scalars; the old `_right_pane_*`
properties exist only as compatibility shims backed by the primary slot.
An empty Projects or Sessions view must name the currently browsed provider and
offer its relevant new-project/session action; it must never retain content from
the previously browsed provider.

Exact-owner local restart state serializes the layout, both slot contents,
Target pane, keyboard focus, preview rollback target, and any live collapsed
secondary agent. Restoration validates every agent against current discovery,
then rebuilds primary, layout, secondary, Target, and focus in that order. A
content failure degrades to the branded empty surface without falsely claiming
the old agent; an unusable split degrades to single while retaining a validated
secondary agent in Running. Portable restoration deliberately remains a single
stable Target display wish and never carries tmux identity or process authority.

For a wide exact-owner restart with saved visible content, Railmux may create
only its inert display panes at the final saved boundaries before Urwid paints
the first frame. Provider validation, swap/attach, and focus restoration remain
in the ordinary deferred recovery path. A failed prelayout is a no-op fallback;
if deferred recovery produces no content, it removes only those owned empty
panes and returns to the full-width sidebar. With no saved visible agent—or in
compact presentation—the initial surface remains the sidebar. The first
deferred restore reuses a successfully created skeleton and is queued without
an artificial settle delay; it must not repeat topology creation or skip any
provider, tmux-identity, swap, or index-generation validation.

Ask Railmux is an explicit auxiliary display, not a provider-session recovery
authority. Opening static Help performs no provider work. The Ask action
materializes installed user documentation under the per-user XDG data tree and
opens a stable, Railmux-namespaced tmux help session for the current provider.
That session is excluded from `_running`, provider Projects views, launch
correlation, and persisted workspace content; a soft restart therefore leaves
the Target empty until the user explicitly reconnects from Help. Replacing the
Target display must not stop or reinterpret its prior agent. Codex help must
disable YOLO, use a read-only sandbox, and disable transcript persistence;
Claude help must use its safe customization boundary and non-editing plan
permissions. Hard quit may kill only help-session names actually used by that
App instance.

Read-only support must also be interruption-free. Codex combines its OS-level
`read-only` sandbox with `approval=never`: reads auto-run, while attempted
writes or network actions fail instead of escalating. Claude exposes only the
built-in `Read`, `Glob`, and `Grep` tools and bypasses prompts inside that closed
tool set; it receives neither Bash nor mutation tools. The versioned helper
identity safely replaces an older live helper when this policy changes.

User layout preference is a separate versioned settings profile containing
only layout name and sidebar/primary proportions in thousandths. It never
stores pane, process, socket, session, or window identity. `Always` retains the
latest successful explicit geometry; `This time` is consumed only after one
successful application. A terminal that cannot satisfy the saved split uses
responsive defaults for that run and must not overwrite the good profile
unless the user subsequently establishes new geometry. Failed F8 transitions
similarly restore the prior active ratios and acquire no persistence authority.

## Agent display transports preserve one ownership model

The default `swap` transport moves the real agent pane into the display window.
The `nested` transport runs a tmux client in the outer display pane and remains
both an explicit compatibility choice and the automatic fallback whenever swap
cannot be proven safe. Both are provider-neutral and are selected behind
`AgentDisplayTransport`; attach, preview, close, delete, liveness, and teardown
must not bypass that boundary with a destructive `respawn-pane`, `kill-pane`,
or `kill-session`.

In swap mode `AgentSlot.pane_id` is the pane physically visible in that slot.
It is the placeholder while idle/previewing and the real provider pane while
displayed. `SwapState` owns the immutable real-pane/PID, home window,
placeholder, display window, outer session, keeper, slot, and transaction phase.
The same real pane may be owned by only one slot.

An intentional session kill is a display transaction, not a raw
`kill-session`: the transport first returns a swap-owned real pane home or
replaces a nested attach client, then respawns the retained outer pane with the
idle surface and clears only that slot's content state. The caller may remove
the Running entry only after the exact tmux identity is confirmed dead. A
failed kill therefore leaves a truthful empty display slot and a still-live
Running entry that can be reopened; it must not collapse an explicitly chosen
dual layout. Natural provider exit follows the same visible-layout invariant:
the exited slot becomes the branded empty surface, while the other slot keeps
its position and Target remains on the same numbered pane.

Confirmed deletion is split at that same ownership boundary. The Urwid thread
closes the confirmation, paints a durable bounded
`Deleting “session name”…` status-right message,
returns any displayed pane, and freezes the revalidated tmux identity plus the
provider-history targets into one immutable task. One worker performs the
blocking exact kill, bounded writer-exit wait, and provider/filesystem cleanup;
it must not touch widgets, Running state, or mutable indexes. A later Urwid
refresh atomically consumes the result, invalidates the appropriate provider
views, and replaces progress with the final success, partial-failure, or error
message. A second delete cannot start while that task or its unpublished result
exists. Routine status TTL expiry must not replace that deleting message before
result publication, though a fresh warning or error retains its short severity
hold.

A real swap-owned pane may temporarily outlive an in-memory `_running` entry.
Refresh may re-adopt it only when the displayed pane id, pane PID, display
window, swap owner, provider session name, and persisted binding all agree.
This recovery never infers ownership from a session name and never launches,
resumes, moves, or kills a process.

Before a real pane moves, a detached tmux session group shares the outer window.
This keeper adds no pane or PTY and prevents a direct kill of the original outer
session from destroying a displayed agent. Versioned, slot-specific tmux window
user options record every transaction. Startup recovery may move only exact
marked identities; it must never infer ownership from a `cc-*`, `cx-*`, pane
title, or session-name resemblance.

Every swap is validate -> mark prepared -> move -> verify -> mark displayed.
Return is mark returning -> move home -> verify -> clear. A failed post-move
rollback retains its marker and keeper and forbids destructive fallback. An
external attached client, unsupported topology, incomplete identity, old tmux,
or unowned outer session uses nested display. Controlled preview, close, soft
quit, hard quit, and delete return the real pane before replacing a display
placeholder or killing its home session.

Before a controlled return or compact parking swap, Railmux best-effort sizes
the detached home window from the visible real pane. The home currently owns
only an inert placeholder, so this preserves the provider's last visible PTY
geometry while it continues in the background and prevents a narrow block of
background output from appearing when the pane is displayed again. Sizing
failure is presentation-only and cannot weaken or abort the identity-pinned
return transaction; an independently attached session is never resized.

Nested display clears `TMUX` to avoid tmux's nested-client guard, so its attach
argv must retain the exact inherited server as `tmux -S <socket>`; a plain
`tmux attach-session` would silently fall through to the user's default server.
The inherited socket/PID is revalidated before the display pane is created.
After `respawn-pane` reports success, the child must also remain alive through
a short startup settle before the slot model commits focus or agent ownership.

Pane movement preserves each window's active pane (`swap-pane -d`). Only an
explicit user-intent path may select the agent display, so a single-click
preview or attach cannot undo the mouse-selected sidebar focus as a side effect
of returning or displaying a real pane.

Soft quit may release UI-only resources and return displayed panes home, but it
must branch before the detached-session kill loop. Hard-quit destruction must
remain below that explicit decision and behind two confirmation boundaries, so
one accidental Enter cannot kill live agents and adding teardown work cannot
silently turn a soft restart into loss of live agents. The second boundary
retains `y`/Enter as confirmation and returns `n`/`Esc` to the first choice.

User-requested exit paints a non-interactive progress surface before any
synchronous pane/session cleanup. Core cleanup runs while Urwid still owns the
sidebar, so the sidebar cannot disappear while the agent pane remains alive.
Core and outer-session phases are separately idempotent: the visible path may
complete core cleanup, while `run()`'s `finally` retries an interrupted phase
and performs only the remaining outer-session cleanup.

The swap floor is tmux 2.7. tmux 2.7 and 2.8 lack `resize-window`, so
their native swap geometry may reflow a long inline transcript. This is a
performance/visual limitation, not permission to alter provider history or
alternate-screen behavior. Full evidence and remaining gates are in
`docs/DENESTED_AGENT_PANE.md`.

## Dual-agent interaction target

The first version should remain bounded to two slots and preserve all existing
single-pane behavior:

### Focus and target terminology

These are separate state axes and their names are a durable product contract:

- **Focused pane / 焦点窗格** is the pane currently receiving keyboard input.
  When the sidebar is focused, neither agent pane is the Focused pane.
- **Target pane / 目标窗格** is the remembered agent pane where actions started
  from the sidebar take effect. Preview, open, running-session switching, F9,
  terminal placement, status, and attention routing all use it.
- While an agent pane is focused it is also the Target pane. Moving focus back
  to the sidebar clears agent focus but does not change the Target pane.
- `AgentWorkspace.target_slot_key`, `AgentWorkspace.target`, and `set_target()`
  are the model names; code and documentation must not use “active” to mean the
  remembered sidebar action target. The previously released `active_slot_key`,
  `active`, and `activate()` names remain thin compatibility views only.
- `AgentWorkspace` remains the Target authority. App-level Target transitions
  project its current outer pane ID into `@railmux_target_pane` solely for the
  managed `Ctrl-B Tab` binding; tmux pane history and the projection never
  become independent sources of Target state.

User-facing English should say **Target pane** and Chinese documentation should
say **目标窗格**. The compact status UI uses a workspace map rather than text:
`▣` means single; `◧`/`◨` mean side-by-side with P1/P2 targeted; `⬒`/`⬓` mean
stacked with P1/P2 targeted. Do not substitute “active pane / 活动窗格”,
“selected pane / 选中窗格”, or “last pane / 上一个窗格”: each conflates target
routing with focus, selection, or history.

- F8 creates an inert secondary slot before any session is chosen and advances
  through the layout cycle by selecting the next orientation that meets the
  minimum size. An unavailable side-by-side or stacked layout is skipped. If
  neither split fits when starting from single, F8 keeps single-pane layout and
  reports the size limit.
- Single-click and Enter on a running row attach its real provider pane;
  stopped-row click still opens history. `␣` and context Preview always open
  read-only canonical history, including for a running row. Wheel input over a
  displayed live agent never enters that viewer: direct Railmux preserves
  tmux/provider-native scrolling, while `railmux ssh` keeps exclusive ownership
  through its bounded per-pane history layer. Explicit Preview returns the real
  pane home first and normal viewer exit signals the controller to restore that
  exact live agent. Every action path uses the Target pane remembered from tmux
  focus. A confirmed live Codex rewind invalidates the same pane's SSH cache
  generation before its child rollout continues drawing.
- Cycling back to single removes only the outer secondary pane, remembers its
  exact instance-local tmux target, and never kills the detached agent session.
- The same background tmux session should not be attached in both slots.
- Layout names are `stacked` and `side-by-side`, avoiding ambiguous
  horizontal/vertical terminology.
- F8 is the only operation that changes the user's logical layout preference.
  A resize may temporarily project an undersized dual layout as Sidebar plus
  its current Target agent; the other agent returns home without being killed.
  The exact slot identities, focus, orientation, and proportions must return
  when both panes fit again. Entering compact presentation retains both outer
  page positions, but only the visible A1/A2 page contains its real swap-owned
  provider pane. Hidden providers are transactionally parked in their detached
  home windows while inert placeholders retain stable tmux targets for `R`,
  `A1`, and `A2`; switching pages zooms the target placeholder before swapping
  its provider back. This prevents tmux from narrowing a hidden provider PTY
  and permanently recording half-width cursor-addressed output. A bounded
  `railmux ssh` controller handshake performs that parking before the helper's
  compact `TIOCSWINSZ`; missing, nested, busy, or older controllers fail open
  to the established resize path and never stop or restart an agent. Compact
  size validation uses the outer zoomed viewport rather than a placeholder's
  hidden split rectangle; valid 40x12-or-larger compact geometry is not judged
  against the desktop pane recommendation. Compact changes presentation,
  never the logical layout. Returning wide reapplies the
  pre-compact proportions (or safe 20% sidebar and 50/50 agent defaults) rather
  than retaining the zoomed page's tmux reflow. Single-agent layout assigns
  about 30% of the outer width to the sidebar; either dual layout assigns about
  20%, clamped to at least 30 columns. Ratio changes are best-effort and must
  not make layout creation or recovery fail. Subsequent wide-window resizes
  must reapply both affected proportional dividers: the sidebar on width
  changes and the two-agent divider on its layout axis. Tmux's retained
  absolute cell width or height is never new ratio authority.
- A saved proportional profile may override those responsive ratios after pane
  topology exists. It is applied after exact workspace restoration, and only a
  successful explicit F8/divider operation may become newer preference
  authority. `[` and `]` resize the sidebar pane directly and never address an
  agent pane directionally; after either key, a dual layout assigns each agent
  half of the remaining region.
- A fresh wide restart with a restorable dual workspace creates both inert
  display slots, the final sidebar width, and the saved/default inner ratio in
  one tmux command queue before attaching either agent. This is a repaint
  optimization only: validation, swap ownership, fallback, and agent-session
  lifecycle remain unchanged. If exact geometry cannot be established, restore
  falls back to the ordinary incremental path.
- Narrow screens should prefer stacked panes because three side-by-side columns
  make agent TUIs unusably narrow.
- Railmux globally routes `F8` to the sidebar controller and cycles
  single → side-by-side → stacked even while an agent owns keyboard focus.
  `F9` similarly reaches the controller and uses the Target pane resolved
  from real tmux focus.
- A crash-safe managed prefix-table `Ctrl-B Tab` binding toggles directly
  between the sidebar controller and the projected Target pane. It must gate
  on the Railmux window before inspecting pane IDs, preserve any prior prefix
  Tab behavior elsewhere, no-op when no Target pane exists, and restore only
  bindings/options still owned by its transaction. Arrow navigation remains
  spatial and is never reinterpreted as a Target-preserving shortcut. An
  existing repeatable or annotated prefix-Tab binding cannot be wrapped
  faithfully by one server-global conditional binding, so Railmux leaves it
  untouched, reports the unavailable toggle, and keeps F8/F9 forwarding active.
- Each projected agent pane must be at least 50x12. Side-by-side is preferred
  only when both projected panes reach 80x20; otherwise the best valid layout
  wins, with stacked breaking a tie.
- While an agent owns keyboard focus, native tmux borders show it in bright
  green. A side-by-side agent focus also enables inward border arrows so the
  shared Pane 1 / Pane 2 edge identifies its owner; arrows are omitted on tmux
  versions before 3.3. When Railmux regains focus, arrows are removed and all
  agent borders become gray. The status brand's one-cell workspace map remains
  visible across focus changes and its filled half names the Target pane without
  presenting it as current input focus. A single layout uses `▣` because P1 is
  the only possible target. While side-by-side Pane 1 has keyboard focus, the
  hint bar includes `C-b → Pane 2`; Pane 2 shows `C-b ← Pane 1`. Direct P1/P2
  focus changes refresh that hint with the workspace map and briefly confirm
  `Agent Pane 1 focused` or `Agent Pane 2 focused`. Teardown restores the exact
  inherited or explicit `pane-border-indicators` window option. Border
  colours and indicators form one applied state: if either tmux update fails,
  the periodic refresh retries both until the visible focus state converges.
  Hint-bar directions follow geometry: left/right names side-by-side neighbors,
  up/down names stacked neighbors, and `Ctrl-B Tab` always names the direct
  Sidebar/Target route.

Attach/resume, replacement, display-transport ownership, duplicate prevention,
close/rotate, per-pane size checks, preview/restore, terminal placement,
liveness, status/attention targeting, scrolling, F9, persistence selection, and
teardown operate on explicit slots. Direct agent focus is resolved from tmux's
active pane while the sidebar is unfocused and from `pane_last` when focus
returns. A direct P1/P2 focus change must repaint the workspace map when that
resolution changes the Target pane; it must not wait for sidebar focus to
return. Terminal `focus in`/`focus out` reports are advisory because hosts may
deliver them after a programmatic pane transition; both event handling and the
normal refresh converge on tmux's actual active pane. Preview/open actions use
that Target pane; the primary compatibility entry points remain only for
established single-pane integrations.

If secondary disappears, restore its live target into the same orientation or
collapse truthfully to single. If primary disappears while secondary survives,
return secondary home before rebuilding primary or promoting the survivor; do
not relabel slot-specific swap ownership in memory. A recovery ambiguity must
leave the agent in Running rather than destroy a pane. Soft restart persists the
full exact-owner workspace after bounded field validation; shared portable state
continues to restore only one stable display wish into primary. When exact
restart state says an agent owned keyboard focus, the first sidebar frame
suppresses its temporary focused-row decoration until the target pane restore
settles; a failed restore reveals the real sidebar focus instead of retaining a
false inactive state.

## Global bindings preserve user tmux configuration

F8/F9 are root-table bindings, so Railmux manages them as a server-wide,
crash-safe transaction rather than unconditionally overwriting and unbinding
them. The wrapper is shared by every Railmux instance on the server and reads a
window-local `@railmux_controller_pane` option at keypress time. It forwards
only inside a Railmux window; elsewhere it replays the exact captured command,
or sends the function key through when it was originally unbound. Each owner
sets and conditionally clears only its own controller option. The final live
owner restores each original binding only while that key still carries the
transaction marker, so a user tmux configuration reload takes precedence.
Dead owners and interrupted installs are repaired by the next instance under a
non-blocking, server-keyed runtime lock.

`MouseDown3Pane` shares that controller-scoped transaction. Inside a Railmux
window, the mouse-aware controller pane is selected by pointer location before
the event is forwarded, matching tmux's stock left-click routing and allowing
an unfocused sidebar to receive its context-menu click. The same wrapper
consumes right-click over sibling agent panes instead of flashing tmux's
unrelated stock pane menu. Other windows replay the exact prior right-click
command. Teardown restores it only while Railmux's marker still owns the
binding, so a user configuration reload remains newer authority.

On tmux 3.4+, `MouseDown1Status` is part of the same lease. Closed user ranges
route compact `R`/`1`/`2` controls to exact pane IDs and route Mode, Layout,
and status-copy actions to private F5/F7/F6 inputs on the controller. The
private keys are handled before modal dispatch, so a click cannot type `m` into
an active editor or mutate layout behind a dialog. Action success is visible
through the changed label/layout plus a transient acknowledgement. Status-copy
acknowledgement temporarily replaces only status-right with its own success
colour, then restores the exact copied tip/info/warning/error; it does not
mutate the copied source. Older tmux keeps
the same display and keyboard shortcuts without installing mouse ranges.

On tmux 3.0+, the lease may also own the stock-only `MouseDown1Pane` Termux
wrapper described above. Its backup is made durable before installation, a
v7 lease upgrades in place to v8, multiple Railmux owners share one wrapper,
and final teardown restores the original only while the exact marker remains.
A user configuration reload or any pre-existing custom left-click binding wins.

The same shared lease owns one indexed `pane-mode-changed` hook on tmux 3.0+.
In a dual-agent layout, entering copy-mode through ordinary mouse selection or
`Ctrl-B [` freezes only the sibling agent pane's display by putting it in
copy-mode; the provider process and PTY continue and buffered output appears
when selection ends. The sidebar is never a freeze target. Pane-local markers
carry an exact controller-and-slot selection key, the sibling pane ID, and the
key that owns an automatic freeze. Hook recursion is ignored, a pane already in
user-controlled copy-mode is never claimed, and cleanup cancels only a freeze
whose exact marker is still owned. Nested transport projects the same key onto
both its visible outer attach pane and inner provider pane; swap transport
projects only the physically visible real pane. Layout changes and teardown
release markers before moving panes, while periodic reconciliation heals a
missed hook after interruption. tmux 2.7-2.9 keeps its existing selection
behavior because configurable hooks and pane-local options are unavailable.

The `railmux ssh` client owns a different mouse boundary before input reaches
the remote tmux client. A plain left press over a known agent route begins a
local click candidate. If it is released without motion, the original
press/release pair is replayed in order so pane focus, preview, and double-click
semantics remain authoritative remotely. Once motion is reported, the local
client pins the gesture to that one pane and immutable visible-row snapshot,
clamps it at the pane border, paints a reverse-video selection, and copies the
selected UTF-8 text to the initiating machine on release. The highlight clears
after a short acknowledgement interval so immutable captured text cannot cover
subsequent live output indefinitely. Its local copy acknowledgement temporarily
replaces only status-right, retains the captured status background, and never
erases the Railmux/mode/layout controls in status-left. Native clipboard writers
are preferred, with bounded OSC 52 as fallback. The first version is
deliberately physical-line and visible-viewport only: it does not autoscroll,
join application soft wraps, or cross pane borders. Keyboard input, resize,
reconnect, and changed route geometry invalidate the local selection.

This client-owned drag never reaches tmux, so stock `MouseDrag1Pane` cannot
enter copy-mode accidentally. Non-left sidebar and status gestures are still
forwarded, terminal-native selection overrides never enter the client, and the
opaque keyboard sequence `Ctrl-B [` remains the explicit copy-mode path.

A semantic hover candidate may be highlighted in either visible agent route,
but only a clean click in the already-focused agent route may become a
client-owned semantic open. Bounded visible `http://` and `https://` tokens
are opened locally and never sent to the remote shell. A bounded Unix-style
path sends a typed protocol request containing only the visible pane ID and raw
token. Besides terminal soft wraps, the recognizer can join a bounded path-only
continuation that Codex or Claude rendered as a real indented newline; every
physical fragment remains a separate highlight segment. This prevents an
existing directory at the end of the first row from winning over the intended
file on the next row. It does not join adjacent list items or indented prose.
The server accepts only a currently visible, non-controller agent pane, resolves
the correct provider pane's current working directory (including
identity-validated nested transport), and returns only a readable absolute
path plus file/directory/other classification. It never opens or mutates the
path.
The local launcher constructs an argv-only SSH invocation without `shell=True`;
supported text files, including HTML, use remote Vim, and all other cases enter
the containing directory. Unsupported local terminal launchers copy the
shell-quoted command instead. Clean clicks in an unfocused agent continue to
mean focus, and drags continue to mean selection, so semantic recognition
cannot replace either established gesture.

Protocol v15 separates path validation from the requested destination. The
first response includes the remote workspace's Ask/Inside/Separate policy; an
Inside or Separate choice is returned in a bounded typed request and the
server revalidates pane identity, current working directory, path type, and
access before taking action. A persistent choice updates only the shared
remote `config.toml`.

Inside-Railmux tools are session-scoped tmux processes, with at most one shell
and one Vim viewer for each agent slot. Pane ID, pane PID, session ID, and
window ID are recorded together. The inactive process is kept in a
Railmux-marked private parking session and swapped into the outer layout only
after those identities revalidate. Layout rebuilds park visible tools first;
failed identity or swap checks stop the transition rather than killing or
guessing at a pane. A viewer opened before any explicit terminal does not
manufacture a hidden shell: quitting that Vim returns to the owning agent,
whereas quitting a viewer after a shell exists restores that shell. Tool
splits remain orthogonal to the outer dual-agent layout: side-by-side agent
columns place tools below, while stacked agent rows place tools on the right.
F9 zooms the focused tool when one owns focus, otherwise it retains the Target
agent behavior. Tool panes carry a pane-local marker so the SSH history and
semantic-click router can exclude them without extra polling subprocesses.
Pane-local user options require tmux 3.0, so tmux 2.7/2.8 retain the core
workspace but fail closed with an explicit warning instead of creating an
unidentifiable managed terminal or Vim pane. For the same reason, nested
transport cannot publish its per-pane SSH history source on those versions;
swap-backed agent history remains available.

Vertical wheel input also fails closed while agent geometry is unknown
and on the one-cell tmux border around a known agent; losing one transitional
wheel tick is preferable to leaking stock `WheelUpPane`, entering copy-mode,
and activating sibling selection isolation. An authoritative empty route set
still forwards Help/modal and sidebar scrolling. This is client-side input
policy and does not mutate shared remote tmux bindings used by ordinary
attached terminals.

tmux routes wheel events by pointer location rather than keyboard focus. Each
sidebar pane therefore consumes buttons 4/5 at its outer widget boundary and
routes them to its own `ListBox`, including events over titles, borders,
dividers, and pinned action rows.

For tmux to deliver both directions to Urwid, Railmux temporarily wraps the
server-global root `WheelUpPane` and `WheelDownPane` bindings. This is allowed
only on tmux 2.7+ when the root bindings match stock behavior; a custom binding
disables forwarding without mutation. Stock-command recognition accepts both
the `send -M` spelling emitted by tmux 3.2 and the canonical `send-keys -M`
spelling emitted by newer releases. All Railmux panes on one tmux server
share a versioned transaction in the private runtime directory, keyed by the
server lifetime and owned by immutable pane IDs. The final live owner restores
only per-key wrappers still carrying its random marker, so a user configuration
reload always wins. A later instance may prune dead owners and repair or remove
an interrupted transaction, but must never infer ownership from command shape
alone.

Copy-mode coalescing follows the same user-configuration rule. Its helper keeps
the exact currently displayed pane target, including a real pane moved by the
swap transport, and must reuse that target if the helper is recreated. On
teardown, restore each copy-mode wheel binding independently only while it
still targets that exact helper pane; a binding changed by the user is newer
authority and must remain untouched.

## Size and attach invariants

The current single-pane layout recommends at least 120x30 cells and treats
anything below 80x20 as critically cramped. Size warnings must never trap a
remote user or disable resize/quit controls. "Outer size" means the containing
tmux window, not Urwid's TTY size: after a split the latter is only the narrow
sidebar. Read the window size at startup and on terminal resize events rather
than polling tmux every second.

A managed window shared by multiple attached terminals uses tmux
`window-size=smallest` (tmux before 2.9 already has equivalent behavior). This
prevents activity-driven geometry jumps and clipped small clients, but it does
not provide per-client focus, layout, proportions, or dimensions: one tmux
window has one compositor geometry. Sidebar-originated Detach must refuse an
ambiguous multi-client target and direct the user to native `Ctrl-B d`.
Every attached terminal views the same Railmux UI process, so Soft Quit ends
that UI for all views while preserving detached agent sessions. Native
`detach-client -a`, issued by the client to retain, is the non-destructive
exclusive-view operation; it is not part of Soft Quit teardown.
Modern tmux receives a small bounded retry budget before failure to set this
policy aborts the new attach; an unverified shared-size policy must not silently
degrade to activity-sensitive geometry.

Each agent display pane independently recommends 80x20 and treats anything
below 50x12 as critically cramped. Check it after attach, explicit divider
movement, and terminal-size transitions; do not poll tmux for dimensions every
second when the outer size is unchanged.

Modal overlays must remain inside the current sidebar pane after responsive
scaling. Long editable or read-only content scrolls within the modal while its
action legend remains visible; confirmation heights continue to derive from
wrapped content and clamp to the available terminal rows.

Idle tips are reserved for valuable behavior that is not already obvious from
the visible Hint Bar, Button Bar, or current screen. Every tip must be concise,
actionable, and true in every context where it can appear. Redundant shortcut
reminders, marketing copy, and facts already exposed by the UI do not belong in
the rotating pool. Persistent but otherwise hidden state, recovery paths, and
cross-pane consequences are appropriate tip material.

Detached agent sessions commonly begin at 80x24. Before attaching a nested
tmux client, create/identify the outer display pane, read its exact dimensions,
and best-effort resize the inner session window to match. This ordering avoids
an attach-time resize that can make Codex visibly replay or reflow long history.
Failure to pre-size is non-fatal: attach must retain its previous fallback.
Never pre-size a session that already has another attached client, because that
would resize an independently viewed workspace.

## Focus colour semantics

Grass green (`#5FAF00`, with an automatically downsampled terminal fallback)
means keyboard/pane focus: bright on pane chrome and deep behind the current
cursor row. Give pane bodies an explicit neutral attribute so the outer focus
map cannot colour ordinary text. A persistent right-pane target uses slate,
live tmux rows use grass-green bold titles, and green/yellow/red agent status
dots retain their colours on normal, cursor, and target backgrounds. Stopped
sessions use a neutral hollow marker instead of a stale lifecycle colour.

With exactly two outer tmux panes, tmux intentionally assigns the active-border
colour to only half of their shared divider. The single-agent layout therefore
sets active and inactive border styles to the same green while the agent is
focused, producing one continuous line, and sets both gray for the sidebar.
In a dual-agent layout, inactive borders stay gray and the active border turns
green. Stacked panes use the resulting horizontal divider and matching left
segment directly. Side-by-side Pane 1 necessarily colours both adjacent shared
borders, so tmux 3.3+ adds arrows pointing inward at the exact active pane.
When the sidebar owns focus, arrows are removed, every dual-agent border is
gray, and the status brand's filled layout glyph names the remembered target.
Glyph and colour changes must preserve that distinction between keyboard focus
and the remembered Target pane across supported tmux/terminal combinations.

## Liveness, activity, and attention are separate axes

Detached tmux/process ownership determines whether a session is running.
Provider lifecycle records determine conversational activity (`idle`, `busy`,
or `blocked`). An optional attention value records the last actionable terminal
outcome without changing either of those facts. Provider errors and aborts must
never prune a live registry entry or reuse the red blocked dot.

For Codex rollouts with lifecycle events, only `task_complete`, `turn_aborted`,
or `thread_rolled_back` ends an active turn; intermediate assistant messages and
tool results remain busy. Older rollouts without lifecycle records fall back to
the last user/assistant message. Codex does not persist a reliable approval-wait
signal, so a pending tool must remain unchanged for two minutes before the
session becomes blocked. This delay avoids classifying ordinary long-running
commands as approval waits.

On procfs systems, a live Codex process may keep working in background threads
after its parent rollout emits `task_complete`. Exact rollout-file descriptors
held by the real pane process tree associate those threads with the parent
Running entry; any busy associated rollout keeps the aggregate session busy,
and an associated blocked rollout applies only when no sibling is busy. This
correlation must follow a pane moved by the swap transport and must never infer
ownership from cwd, title, or rollout recency. Where procfs is unavailable or
the probe fails, the parent rollout remains the status authority.

New-session placeholder resolution follows that same swap-aware real-pane
identity. Once the exact rollout becomes visible in the pinned index
generation, its title, provider status, attention, and activity timestamp are
promoted to Running together and that one-time identity transition may bypass
the ordinary reorder throttle. Promotion retains the current Projects-pane
object by normalized cwd rather than importing a provider metadata key, so the
selected project and its Sessions snapshot cannot disappear for one refresh.
A Sessions row must not appear stopped merely because the named home session
currently contains the swap placeholder.

Subagent rollouts remain filtered from visible session lists, but the Codex
index worker publishes their UUID-to-status values in the same immutable
generation as visible metadata. The UI therefore performs only snapshot
lookups after process correlation; it never parses rollout JSONL on the Urwid
thread. Process-tree/fd correlation is cached briefly by tmux name and real
pane identity so normal status refreshes do not repeatedly walk procfs.

Attention summaries come only from dedicated provider error/lifecycle fields and
must be short and sanitized. Never classify an error from user prompts,
assistant messages, tool output, or titles. A newer turn start and a newer
successful turn clear stale attention. User interrupts and explicit rollbacks
are not provider failures.

Current observed Codex rollouts do not persist a reliable capacity or rate-limit
reason. Such lifecycle errors remain generic unless a dedicated provider field
supplies a safe category; message text must never be used to guess one.

Running-pane filtering is a view over the live registry, never a mutation of
that registry. The pane retains the complete provider-scoped entry snapshot,
keys focus and callbacks by exact tmux session name, and performs fuzzy/project
matching only against indexed display metadata. Filter edits therefore cannot
hide a session from liveness management or trigger transcript I/O.
