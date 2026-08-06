# Windows managed-MSYS2 preview

This is the design and validation authority for the active `windows-preview`
branch. The abandoned native ConPTY compositor and native-to-WSL delegation
experiments are frozen at `archive/windows-conpty-deprecated` (`v0.4.0.dev2`)
and `archive/windows-wsl-delegation-deprecated` (`v0.4.0.dev3`). Neither is a
fallback or a base for this implementation.

## Decision and ownership

Native Windows Python is only a bootstrap. With explicit consent it installs a
private MSYS2 base beneath `%LOCALAPPDATA%\Railmux\runtimes`, then hands the
original argv to a version-isolated Railmux application inside that base. The
MSYS2 release identifier is the base compatibility and refresh boundary; a new
Railmux development build reuses that base and installs only its own venv while
the identifier remains unchanged. That one POSIX application remains the owner
of layout, mouse routing, previews, dialogs, restore state, SSH display, and
provider lifecycle.

Because MSYS2 repositories roll independently of that archive identifier, the
base also owns a separate exact package-content identity: a SHA-256 of the
bounded sorted pacman inventory plus the recorded tmux, Python, and pip package
versions. dev24+ app markers bind to that identity. The released dev11-dev23
base/app markers remain readable for safe attachment and explicit Soft Quit;
`runtime status --verify` can report live package drift without silently
changing either authority.

The footprint is about 700 MB or more because Windows needs a complete,
internally consistent private MSYS2 compatibility base plus tmux and Python;
it is not the size of the Railmux Python code. This is a one-base cost rather
than a cost repeated for every preview build. A measured dev8 runtime used
561 MiB and its reusable verified base/package caches used about 119 MiB. The
private pacman configuration enables only `msys`, not mingw/clang SDK repos.

Codex and Claude Code remain the user's Windows-native executables. The child
`HOME` is explicitly mapped to `%USERPROFILE%`, the inherited Windows `PATH` is
retained, and native provider paths are translated only when Railmux reads
their metadata. Consequently the wrapper reads the same `.codex` and `.claude`
directories as providers launched directly from PowerShell. It does not copy,
migrate, or rewrite provider histories.

Windows startup maintains a Railmux-private validity index for Claude JSONL
files. The index stores only the encoded project directory, UUID filename,
inode, modification time, size, and a validity bit. A cached result is accepted
only when that exact signature still matches; a changed or appended file is
reopened read-only. The cache is disposable, uses a private atomic file, and is
never sufficient evidence to resume, delete, or otherwise mutate a provider
session.

The preview is intended to support local Railmux, the local side of
`railmux ssh` to a Linux, macOS, or compatible Unix host, and an ordinary
OpenSSH login to the
same Windows account followed by `railmux`. A desktop terminal and that SSH
login must converge on the same dedicated tmux workspace rather than create
per-terminal runtimes or session namespaces. Native Codex and Claude provider
processes remain in scope in all of those entry surfaces and retain the same
Windows-owned histories. A Linux or macOS preview client can also run
`railmux ssh --remote-platform windows user@windows` when that account already
has a compatible managed runtime. The option skips a POSIX-shell probe that
PowerShell cannot parse; `auto` can fall back to the same direct command but
may require authentication twice on password-only hosts. Windows runtime
installation and repair remain explicit native user operations, never remote
POSIX installer commands. WSL remains usable only when the user independently
opens a WSL shell and runs the ordinary POSIX product there.

## Bootstrap contract

- Windows requires Python 3.10 or newer. Packaging metadata must retain Python
  3.9 for POSIX because Python package metadata cannot express an OS-specific
  interpreter floor; the native entrypoint enforces the Windows floor before
  runtime discovery.
- `railmux --version`, `--help`, `runtime status`, and `doctor` do not install a
  runtime. When an exact private base candidate already exists, ordinary launch
  and `runtime install` state that only the versioned app layer is changing and
  continue without redundant confirmation. That automatic path passes a
  `reuse_only` authority into the serialized installer, so a validation race or
  damaged base can only fail and can never escalate into an unconfirmed full
  install. When no candidate exists, `runtime install` asks once for the roughly
  700 MB base operation and `N` cancels immediately; `runtime install --yes` is
  the explicit noninteractive authority. `runtime install --verbose` streams
  raw subprocess output in addition to retaining it in the install log.
- The official MSYS2 `tar.xz` base release, filename, size, SHA-256, member
  count, and expanded regular-file size are pinned. Railmux extracts it in
  process without executing downloaded archive code. Every member must remain
  under the single `msys64` root; absolute/traversal paths, backslashes,
  duplicates, links, special files, staging reparse points, and unexpected
  inventory or expanded size fail closed before the temporary base can be
  published. The bootstrap samples the official GitHub release first. When
  its projected remaining time exceeds 60 seconds, it concurrently samples
  the MSYS2 repository and the MSYS2-listed TUNA and NJU mirrors, switches only
  for at least a 25% measured improvement, and otherwise continues the best
  available transfer. Probe bytes are reused, and an interrupted transfer can
  resume at the exact offset from another approved source. Servers must return
  strict HTTPS `206` responses with the expected `Content-Range`; if adaptive
  transfer is unavailable, the bootstrap falls back to ordinary full downloads
  in the approved order. A wrong final size or SHA-256 removes the archive, and
  every path must produce the same Railmux-pinned digest before extraction.
  Interactive downloads show bytes, total size, and percentage, while
  redirected logs receive bounded milestones. A verified base is retained in
  the Railmux-private cache so a future base generation can recover without
  downloading it again. Before pacman runs, the
  bootstrap concurrently samples the actual `msys.db` from the geo redirector,
  primary repository, TUNA, USTC, and NJU entries, but only when the exact URL
  is also present in the pinned runtime's official `mirrorlist.msys`. A source
  must return HTTPS and a bounded Zstandard database sample. Candidates within
  six hours of the newest measured `Last-Modified` value are fresh and eligible
  to become primary; the official first entry remains preferred unless another
  fresh entry is at least 25% faster. The
  staged private mirrorlist activates only successfully measured approved
  sources, ordered with stale candidates last; other official entries remain
  visible as inactive comments. The pool is measured again after the base
  upgrade because `pacman-mirrors` may replace the list. Railmux uses a derived
  private pacman configuration containing only the required `msys` repository,
  while the original configuration remains untouched. Pacman verifies packages
  with the bundled MSYS2 signing keyring and stores completed downloads in a
  Railmux-private cache outside transactional staging. Before the package
  transaction, Railmux asks pacman for its resolved dependency URLs and probes
  up to twelve evenly distributed real package names across every active
  source. A source that serves the database but blocks any sampled package is
  made inactive when another verified source remains. HTTP 403/404 hosts are
  excluded after a failed transaction; low-speed exhaustion triggers one retry
  that reuses the cache and disables pacman's low-speed abort. This preview does
  not yet freeze a repository snapshot, so package versions may advance between
  installations.
- Versioned Railmux application installs use a separate pip-managed cache at
  `%LOCALAPPDATA%\Railmux\cache\pip`, outside both the shared base and every
  provider directory. The first attempt uses a 60-second network timeout with five
  pip retries. If the command still does not complete, the same launch retries
  it once with the same cache, a 120-second network timeout, and five pip retries;
  successfully cached dependency wheels are therefore reusable across the
  recovery attempt and later preview versions. A failed final attempt never
  publishes the app marker, never falls back to reinstalling the base, and
  never opens or modifies Codex/Claude histories. The pip cache is disposable
  and may be deleted to recover from suspected cache damage while no Railmux
  installation is running.
- A new base renders seven stable phases rather than exposing all pacman noise;
  an upgrade that can reuse the exact base renders three phases and does not
  run archive download, extraction, pacman update, or package installation.
  Extraction percentages, repository/package milestones, and a 15-second
  heartbeat cover every long subprocess. Mirror fallback is
  summarized once, the console uses terminal-aware color while the log remains
  plain, printed `[Y/n]` prompts do not require input, and a command
  failure shows a bounded tail. Complete
  subprocess output is written explicitly as UTF-8 beneath
  `%LOCALAPPDATA%\Railmux\logs`; URL credentials and common secret query fields
  are redacted, unrelated files are never pruned, and at most five recognized
  install logs are retained. Legacy Windows console encodings affect only the
  best-effort display copy, not the UTF-8 evidence log.
- Installation is serialized. A new base is staged, exactly probed, and
  atomically renamed. A versioned Railmux venv is built at its final POSIX path
  so console-script shebangs never retain a temporary path; it remains
  undiscoverable until an exact probe succeeds and its marker is atomically
  published. A crash can therefore leave only a markerless Railmux-owned app
  directory, which the next locked installer removes before retrying while
  explicitly reporting that provider data lives elsewhere. dev11 can
  adopt a released dev4-dev10 private runtime as its shared base only after its
  exact owner marker, directory name, runtime identifier, and installed
  Railmux version all probe successfully. Adoption adds a base marker and a
  new versioned app directory in place; it does not download, copy, upgrade, or
  relocate the existing MSYS2 files, and it preserves the legacy venv and
  marker for rollback. After adoption, the base marker is the durable discovery
  authority, so removing rollback state cannot make Railmux orphan and
  redownload the base. Incomplete final directories fail closed and are never
  silently removed. User-selected `RAILMUX_MSYS2_ROOT` runtimes are probed
  read-only and never provisioned, adopted, or updated.
- Versioned app layers are deliberately retained for preview rollback and are
  never pruned during install or launch. Their growth is the application-layer
  size (22.4 MiB in the measured dev11 environment), not another MSYS2 base.
  `runtime prune` is the only cleanup authority: it inventories process argv,
  fails closed on ambiguity, retains the installed version, immediately
  previous version, and every process-proven in-use layer, and removes only
  exact content-bound marked app directories after replanning under the install
  lock. `--dry-run` is read-only; cache removal requires `--caches`; provider
  roots are outside every candidate and are never inspected or removed.
- No system `PATH`, shell profile, Windows package manager, user-owned MSYS2,
  or provider history is modified. All captured text and marker files use an
  explicit UTF-8 codec; CP936/GBK is never an implicit file encoding.
- The handoff uses an argv list and a fixed shell literal. A `$0` sentinel
  prevents dropping the first argument, and MSYS argument conversion is
  disabled at the Windows boundary then restored before native providers run.
- The parent waits through Ctrl-C instead of killing the child. tmux owns
  persistence after detach or outer-window closure.
- Installing a new wheel never makes an already-running outer UI appear
  upgraded. dev24+ controllers publish their exact content-bound app identity
  only after MainLoop is usable. A detached older dev24+ UI cooperatively saves
  state, returns displayed provider panes, releases UI-only leases, and execs
  the validated new absolute app after a nonce-bound pane-local request.
  Released dev11-dev23 controllers remain untouched until the user chooses
  Soft Quit; the next launch creates the dev24 UI. Attached, ambiguous, or
  racing state is likewise left untouched. Neither path kills the tmux
  server, a provider session/process, or Codex/Claude history.
- The ordinary label-selected tmux attach remains the fast path. If Windows
  rejects only that terminal attachment while the same server and immutable
  Railmux session remain healthy, Railmux makes one fail-closed transparent
  bridge attempt. If Soft Quit preserved detached providers but removed the
  outer UI, Railmux first creates only that missing outer session detached on
  the revalidated existing server; it then uses the same direct-first and
  immutable-session bridge checks. If both entry streams are real TTYs, the
  new session is created at their exact bounded dimensions before its first
  frame; Railmux does not invent dimensions or pre-resize an existing session.
  That new outer session receives only the
  current bounded runtime kind, runtime ID, and Railmux app-layer ID through
  tmux `-e`; credentials, provider variables, and the rest of the launcher
  environment are excluded. The on-disk base/app markers remain independent
  authority before MSYS2's NTFS privacy projection is accepted. The helper is
  spawned by that exact server,
  owns an additional PTY-backed tmux client in the server's Windows Terminal
  Services session, and forwards opaque bytes, resize, heartbeat, and exit
  frames to the entry terminal. It never mirrors the UI, stores an origin preference,
  detaches existing clients, or opens provider histories. A random same-user
  private endpoint uses an independent random name and nonce challenge and is
  removed by its creator. A later launch may clean a bounded old same-owner
  endpoint only after it is unconnectable and its identity remains unchanged.
  Output is suppressed at the `run-shell` boundary,
  sends are time-bounded, and the helper drains tmux's terminal-restore tail
  before reporting exit. Failure leaves the shared workspace running and is
  exposed as a bounded `doctor` incident. A successful bridge suppresses the
  raw direct-client terminal error and prints one terminal-aware Railmux info
  line; a failed or unavailable bridge retains an actionable Railmux error.
- MSYS2 projects NTFS ACLs as 0644/0755 even when POSIX chmod requests
  0600/0700. Railmux accepts that representation only under the real
  Cygwin/MSYS managed wrapper after verifying separate same-owner on-disk base
  and application markers with exact runtime/Railmux versions, for same-UID
  non-symlink files/directories without group/world write bits. The user-owned
  managed runtime's Windows ACL remains the private-state boundary. Linux and
  macOS retain strict modes.
- When the last tmux session exits, MSYS2 3.7b may leave a socket pathname that
  makes the next client hang. Railmux writes cleanup authority only after exact
  server-wide session and pane inventories prove the outer UI is alone. After
  a settle interval and two failed endpoint probes, it may remove only the
  unchanged same-user socket after the recorded server PID is proven gone.
  Routine post-exit cleanup is silent; a pre-launch recovery prints one info
  message that Codex/Claude session files were untouched. Any
  live PID, provider or unknown session denies this cleanup; a failed outer
  teardown revokes the proof immediately.
- Startup repaints the restoring surface with separate read-only indexing and
  tmux-reconnection stages. A slow startup reports its measured initialization
  time and the private-cache boundary after the first frame.
- Provider indexing, immutable pane recovery, and the first interactive
  sidebar frame remain synchronous. The managed runtime prepares only its
  crash-safe root-wheel and shared function/status binding leases on a
  non-daemon background worker; their tmux queries do not read provider
  histories, UI state is
  projected back on the Urwid thread, and an early exit leaves lease cleanup
  with exactly one owner. Keyboard Mode/Layout paths remain available during
  that bounded setup window. Completion invalidates the pre-lease bar cache
  and reprojects both left navigation and the unchanged current right-side
  Copy range, so the first status text does not wait for a later tip to become
  clickable.
- tmux 3.7+ status actions use dedicated `control|N` ranges and crash-safe
  shared bindings. Railmux captures existing root-table bindings before
  replacement, restores only its own lease, and shows the `m`/`F8` keyboard
  alternatives if status-click ownership cannot be established safely.

Git for Windows is built from a maintained subset/fork of MSYS2, but its normal
Git Bash installation does not provide the complete `pacman` + `tmux` runtime
Railmux needs. Reusing it would also couple Railmux to another application's
update lifecycle. The preview therefore uses a separately owned MSYS2 tree.

## Evidence and release boundary

The machine-readable feature ledger is `windows-wrapper-parity.toml`. It makes
every stable support-matrix ID explicit and preserves manual checks for the
visual and terminal behavior that unit tests cannot prove.

Windows-preview CI performs an advisory one-byte HTTPS Range request against
the exact pinned artifact on every approved transport and validates its exact
`Content-Range`. It also reads the same bounded pacman database sample from
every approved package candidate. It intentionally does not use ICMP ping and
does not block product tests when an external mirror has a transient outage;
runtime integrity remains the SHA-256 and pacman package-signature checks, not
the CI capability probe.
The blocking Windows jobs separately exercise the native 3.10/3.13 bootstrap
and a real MSYS2/tmux/Python runtime. The MSYS2 job records an exact package
identity, writes content-bound managed markers, and runs the Windows UI
transition, provider-path, local/remote config, local/remote doctor, and
privacy-safe diagnostics suites from the runtime-owned app venv.

On 2026-08-03 a Windows 10 19045 / PowerShell 5.1 / Python 3.12.10 spike proved
that MSYS2 maps `HOME` to the same Windows profile, finds native Codex 0.146.0
and Claude Code 2.1.220 through inherited `PATH`, launches both in detached tmux
3.7b panes, preserves a detached Claude pane across closing and reopening the
outer shell, and starts the complete POSIX Railmux UI. Authentication-specific
restore, preview, resize, mouse, menus, clipboard/browser bridges, and
`railmux ssh` remain release-specific manual checks and are not inferred from
that spike.

On the same host, the dev10 candidate reproduced MSYS2 3.7b leaving an
unresponsive AF_UNIX pathname after both hard and soft quit with zero provider
sessions. The guarded sole-session/sole-pane cleanup removed the unchanged
pathname, reported its read-only provider-data boundary, and the same isolated
label launched again successfully. A separate live label and the pre-existing
default server were left untouched.

dev11 automation proves that a complete dev10-owned runtime is adopted in
place, its legacy marker is byte-for-byte unchanged, only the two app-install
commands target the final versioned POSIX path, interrupted markerless app
layers recover without touching provider data, dev10 rollback remains
discoverable, and later discovery selects the versioned dev11 app. Real
Windows validation must still cover the exact dev10 upgrade on the user's
machine.

On 2026-08-04 the dedicated Windows 10 19045 / PowerShell 5.1 / Python 3.12.10
host upgraded its installed dev9 runtime through the three-phase dev11 path.
The first deliberately offline package lookup left a markerless app layer; the
next run removed only that unpublished directory, reused the same base without
archive, extraction, pacman, copy, or relocation, and completed successfully
from the locally supplied wheel. `runtime status` and the real MSYS `doctor`
handoff reported dev11, tmux 3.7b, native Codex 0.146.0, and native Claude Code
2.1.220. The complete adopted tree measured 584.4 MiB, of which the dev11 app
layer was 22.4 MiB, and the legacy dev9 marker remained unchanged.

dev12 automation reproduces the field-reported app-layer PyPI read timeout
after successful dev11 base reuse. It proves that venv creation runs once, the
package command retries once with the same external pip cache and a longer
timeout, the dev11 app marker remains byte-for-byte unchanged, and dev12 is
published only after its exact executable probe succeeds. On 2026-08-04 the
dedicated Windows 10 host then installed the local dev12 candidate through the
three-phase shared-base path with the real MSYS `cygpath` cache conversion and
without archive, extraction, or pacman work. Its MSYS doctor reported dev12,
Python 3.12.13, tmux 3.7b, and both native providers; the classified retry
itself remains to be field-validated on the affected user network.

The same host later reproduced a dev12 launch failure with an abandoned
dedicated socket but no live managed-runtime tmux process. A direct tmux
identity probe returned the authoritative `no server running` result after
2.09 seconds, just beyond Railmux's former 2.0-second startup bound; an isolated
label then proved that `new-session` safely replaced the same endpoint in 4.20
seconds. The released dev13 launcher gives only managed-Windows default startup
discovery a five-second bound and successfully entered the real TUI across the
same field-shaped abandoned endpoint before a normal `C-b d` detach. That
validation also exposed `doctor`'s separate explicit one-second probe; dev14
routes only the managed-Windows doctor through the same settle allowance.
Explicit health/watchdog probes, POSIX launcher and doctor bounds, and the
proof-gated socket unlink authority remain unchanged.

The dev15 candidate was installed on the same Windows 10 host through exact
shared-base reuse. An ordinary OpenSSH login followed by `railmux` entered and
detached the existing dedicated workspace. A separate forced bridge smoke used
the released app layout and real MSYS2 AF_UNIX/tmux paths: the pinned server
started the versioned helper, completed the nonce/HMAC handshake, attached an
additional PTY client, transported the live UI, detached with `C-b d`, drained
terminal restoration, and returned zero. `doctor` still reported the same
healthy server afterward. The automatic fallback from an interactive Windows
Terminal session to an SSH-origin server (and the reverse origin) remains a
manual dev15 check because SSH access alone cannot create the desktop Terminal
Services side of that boundary.

The dev16 candidate reused that base and installed only its versioned app
layer. Both the native bootstrap and managed-MSYS2 executable reported dev16;
the real MSYS process-birth probe, advisory-lock holder transfer, owner lookup,
and automatic release after provider-process exit all passed. An ordinary
OpenSSH login entered the existing dedicated workspace through the dev15 bridge
and detached normally, after which `doctor` still reported the same healthy
tmux server. A separate two-host test on the shared scratch filesystem held the
lease on `ipp2-1773`, identified that host immediately from `computelab-304`,
and rejected the second claim. This proves the tested filesystem's cross-host
`flock` behavior and the owner-record flush, not arbitrary NFS/CIFS mount
semantics. End-to-end Windows-origin `railmux ssh` mouse motion and authenticated
provider resume remain manual dev16 checks.

The dev17 candidate was installed into a new isolated tmux label on the same
Windows 10 19045 / managed-MSYS2 host. Subprocess profiling proved an empty
provider set (`index 0.0s`) and immutable pane recovery of 0.6s; repeated tmux
client process creation, not provider-history parsing, dominated the older
startup. Batching independent mutations and preparing only crash-safe
wheel/function/status leases after first paint reduced the controlled app
restore measurement from 9.4s before batching, through 4.6s after batching, to
2.3s for the final candidate. The resulting live root table contained the
owned F8/F9, status-control, right-click, and wheel bindings, and detaching left
the isolated workspace healthy. This measures an empty-session SSH-origin
terminal on that host; desktop Windows Terminal pointer behavior and
authenticated provider restore remain manual checks.

The dev18 candidate reproduced the field failure on the same Windows 10 host:
Soft Quit removed the outer `railmux` session while a detached provider tmux
session kept the dedicated server alive. The next candidate launch created
only the missing outer UI detached through that unchanged server, resolved its
immutable session ID, entered it from OpenSSH, and detached normally. The
provider tmux session survived both launches and no provider/session file was
opened for mutation. Automatic bridge selection from the desktop Windows
Terminal side of the Terminal Services boundary remains a manual check.

The dev19 candidate reused that live provider and dedicated server after the
outer UI had disappeared. The detached create carried only the exact managed
runtime/app IDs, the new process revalidated its NTFS-projected private state,
and the unresolved Codex marker moved from the proven-dead outer pane to the
new immutable pane without reading or changing provider history. The first
interactive status bar contained current tmux 3.7 control ranges; F6 copied the
full status source into tmux's clipboard buffer and produced its transient
acknowledgement. The provider survived repeated outer recreation and the final
view detached normally. Automated tests cover raw direct-error suppression,
terminal-aware bridge status, and the unavailable-bridge error; desktop-side
Windows Terminal clipboard receipt remains manual.

dev24 automation adds fail-closed cooperative app-layer transition tests,
exact package identity/drift tests, process-aware prune tests, native forwarding
of every local/remote config and doctor form, and POSIX/direct remote probe
coverage. A real Windows 10 candidate then verified the exact 96-package base
identity, schema-4 local doctor, local config, a 2.9-second empty-workspace
restore, detach/reattach, Linux-to-Windows SSH/config/doctor, and native
Windows-to-Windows SSH/config/doctor through OpenSSH. These checks do not
promote the preview to stable support: the exact dev23-to-dev24 Soft Quit
boundary, Windows Terminal visual/input checklist, Windows-to-POSIX endpoints,
and macOS-origin remote paths remain release-specific manual checks.

dev26 replaces the MSYS2 SFX execution path after a fresh Windows 11/Python
3.12 install reproduced the SFX's `cannot find sfx` exit despite an exact
size/SHA-verified cache. Automation exercises real `tar.xz` decoding, bounded
inventory and progress, traversal/backslash rejection, link rejection, and
the no-archive-execution boundary. The full pinned official archive was also
extracted on Linux with the production extractor and matched all 16,485
members and 289,361,533 regular-file bytes. The production dev26 wheel then
repeated that exact full extraction under native Windows 10/Python 3.12,
produced `usr/bin/bash.exe`, and reported bounded 5% file-count milestones.
The subsequent seven-phase package/app installation remains a release-candidate
field check before this evidence can promote support.

Only `0.4.0.dev4+` development releases may be cut from `windows-preview`.
This document is not a stable-support claim, and the repository README and
website remain unchanged until the manual checklist is complete.
