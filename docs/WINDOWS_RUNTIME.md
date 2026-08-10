# Windows managed-MSYS2 runtime

This is the design and validation authority for Railmux's supported native
Windows adapter on `main`. The filename preserves durable links from the
development series; references to individual preview builds below are
historical evidence. The abandoned native ConPTY compositor and native-to-WSL delegation
experiments are frozen at `archive/windows-conpty-deprecated` (`v0.4.0.dev2`)
and `archive/windows-wsl-delegation-deprecated` (`v0.4.0.dev3`). Neither is a
fallback or a base for this implementation.

## Decision and ownership

Native Windows Python is only a bootstrap. With explicit consent it installs a
private MSYS2 base beneath `%LOCALAPPDATA%\Railmux\runtimes` for traditional
Python or `%USERPROFILE%\.railmux\windows\runtimes` for an MSIX-packaged
Python, then hands the original argv to a version-isolated Railmux application
inside that base. AppData writes made by packaged desktop applications may be
redirected into a package-private view that PowerShell and executable child
loaders do not share, so packaged Python must use the non-virtualized profile
location for the complete executable tree, caches, locks, and logs. A managed
runtime generation identifier, independent of the pinned MSYS2 archive date,
is the base compatibility boundary. A new Railmux version reuses that exact
generation and installs only its own venv; changed base requirements create a
new generation instead of adopting or mutating an older tree. That one POSIX
application remains the owner of layout, mouse routing, previews, dialogs,
restore state, SSH display, and provider lifecycle.

Because MSYS2 repositories roll independently of that archive identifier, the
base also owns a separate exact package-content identity: a SHA-256 of the
bounded sorted pacman inventory plus the recorded tmux, Python, and pip package
versions. Every app marker binds to that identity. The first release generation
requires a recorded tmux package version of 3.7 or newer; an older or
unparseable inventory is never published or reused. `runtime status --verify`
can report live package drift without silently changing either authority.

The footprint is about 700 MB or more because Windows needs a complete,
internally consistent private MSYS2 compatibility base plus tmux and Python;
it is not the size of the Railmux Python code. This is a one-base cost rather
than a cost repeated for every Railmux app layer. A measured dev8 runtime used
561 MiB and its reusable verified base/package caches used about 119 MiB. The
private pacman configuration enables only `msys`, not mingw/clang SDK repos.
An app layer is the much smaller version-isolated Railmux virtual environment:
the Railmux package plus its Python dependencies and marker. A native launch
whose exact app marker and executable probe already pass enters that layer
directly; it does not invoke the installer again. A newly installed Railmux
version creates one new app layer so an older running UI can transition or
roll back without mutating its files.

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

The managed runtime supports local Railmux, the local side of
`railmux ssh` to a Linux, macOS, or compatible Unix host, and an ordinary
OpenSSH login to the
same Windows account followed by `railmux`. A desktop terminal and that SSH
login must converge on the same dedicated tmux workspace rather than create
per-terminal runtimes or session namespaces. Native Codex and Claude provider
processes remain in scope in all of those entry surfaces and retain the same
Windows-owned histories. A Linux or macOS client can also run
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
- Traditional Python installations keep all Railmux-owned Windows state under
  `%LOCALAPPDATA%\Railmux`. A process with an AppX/MSIX package identity uses
  `%USERPROFILE%\.railmux\windows` instead, because new AppData writes can be
  virtualized away from `bash`, `pacman`, `tmux`, and PowerShell. Package
  identity is queried through the Windows API with a narrow executable-path
  fallback. Railmux never falls back to virtualized AppData when a packaged
  process lacks `USERPROFILE`. A fixed-size/SHA-256-verified base archive from
  the previous logical AppData cache may be copied into the new cache; staged
  runtimes, markers, app layers, and arbitrary files are never migrated.
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
  published. POSIX archive modes are not mapped to NTFS read-only attributes:
  all package-owned files and directories remain writable so pacman can
  transactionally replace them when the rolling repository is newer than the
  pinned base. The bootstrap samples the official GitHub release first. When
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
  source. Each probe transfers a bounded 256 KiB prefix and rejects both
  blocked package paths and sources below the bounded minimum throughput. A
  source that serves the database but fails any sampled package check is made
  inactive when another verified source remains. HTTP 403/404 hosts are
  excluded after a failed transaction; low-speed exhaustion triggers one retry
  that reuses the cache and disables pacman's low-speed abort. This preview does
  not yet freeze a repository snapshot, so package versions may advance between
  installations.
- An ordinary version-only upgrade first builds the unpublished app layer
  without network access: it copies only the known pure-Python dependency roots
  from the newest marked app that still probes as its exact version, and copies
  the currently executing native Railmux package into the new venv. Source
  trees must contain no link/reparse point and remain within bounded file-count
  and byte limits. `pip check`, explicit dependency imports, exact version
  validation, and the normal executable probe all precede marker publication.
  If no eligible prior layer exists or any local check fails, the unpublished
  venv is deleted and recreated through the separate pip-managed cache under
  the selected Railmux-owned Windows data root. That fallback uses a 60-second
  network timeout with five pip retries, then one 120-second recovery attempt
  with the same cache. A failed final attempt never publishes the app marker,
  never falls back to reinstalling the base, and never opens or modifies
  Codex/Claude histories. The pip cache is disposable and may be deleted while
  no Railmux installation is running.
- MSYS2 repositories roll independently of the pinned base archive. Fresh
  staging therefore performs a complete package update, installs tmux, Python,
  and pip noninteractively, records the exact sorted package inventory, and
  rejects the tree unless tmux parses as 3.7 or newer. Codex synchronized
  application frames are consequently part of the supported native-Windows
  rendering contract. The global macOS/Linux/WSL tmux requirement stays at
  2.7; only the Railmux-owned Windows generation has the higher floor.
- A published base is never upgraded in place. A pacman core transition can
  temporarily replace DLLs needed by the updater itself, so mutating the base
  that owns a live detached tmux server would turn a visual improvement into a
  session-availability risk. Fresh installations use a disposable staging tree
  and publish only after package-floor, marker, application, and executable
  verification.
- A new base renders seven stable phases rather than exposing all pacman noise;
  an upgrade that can reuse the exact base renders three phases and does not
  run archive download, extraction, pacman update, or package installation.
  Extraction percentages, repository/package milestones, and a 15-second
  heartbeat cover every long subprocess. Mirror fallback is
  summarized once, the console uses terminal-aware color while the log remains
  plain, printed `[Y/n]` prompts do not require input, and a command
  failure shows a bounded tail. Complete
  subprocess output is written as UTF-8 with a Windows-compatible signature
  beneath the selected
  Railmux-owned Windows data root's `logs` directory; URL credentials and
  common secret query fields are redacted, unrelated files are never pruned,
  and at most five recognized install logs are retained. Legacy Windows console
  encodings affect only the best-effort display copy, not the UTF-8 evidence
  log. `runtime status` reports the effective data root and log directory in
  both human and JSON output, so Store/MSIX Python users are not directed to
  the intentionally unused virtualized `%LOCALAPPDATA%` path.
- Installation is serialized. A new base is staged, exactly probed, and
  atomically renamed. A versioned Railmux venv is built at its final POSIX path
  so console-script shebangs never retain a temporary path; it remains
  undiscoverable until an exact probe succeeds and its marker is atomically
  published. A crash can therefore leave only a markerless Railmux-owned app
  directory, which the next locked installer removes before retrying while
  explicitly reporting that provider data lives elsewhere. Incomplete final
  directories fail closed and are never silently removed. User-selected
  `RAILMUX_MSYS2_ROOT` runtimes are probed read-only and never provisioned,
  adopted, updated, or removed.
- Versioned app layers are deliberately retained for rollback and are
  never pruned during install or launch. Their growth is the application-layer
  size (22.4 MiB in the measured dev11 environment), not another MSYS2 base.
  `runtime prune` is the only cleanup authority: it inventories process argv,
  fails closed on ambiguity, retains the installed version, immediately
  previous version, and every process-proven in-use layer, and removes only
  exact content-bound marked app directories after replanning under the install
  lock. `--dry-run` is read-only; cache removal requires `--caches`; provider
  roots are outside every candidate and are never inspected or removed.
- `runtime uninstall` is the explicit full software-removal authority before
  `pip uninstall railmux`. It refuses user-owned overrides, requires an exact
  current-generation base marker, proves no other process is visible in that
  private MSYS2 generation, repeats the proof under the install lock, and
  atomically renames the complete base and private package cache before
  deleting either tree. A live executable/DLL handle or ambiguous inventory
  therefore fails closed without a partial in-place deletion. Install logs and
  normal Railmux workspace state remain for diagnostics; `.codex`, `.claude`,
  provider histories, and user-owned MSYS2 trees are outside every candidate.
- No system `PATH`, shell profile, Windows package manager, user-owned MSYS2,
  or provider history is modified. All captured text and marker files use an
  explicit UTF-8 codec; CP936/GBK is never an implicit file encoding.
- The handoff uses an argv list and a fixed shell literal. A `$0` sentinel
  prevents dropping the first argument, and MSYS argument conversion is
  disabled at the Windows boundary then restored before native providers run.
- The parent waits through Ctrl-C instead of killing the child. tmux owns
  persistence after detach or outer-window closure.
- Installing a new wheel never makes an already-running outer UI appear
  upgraded. Controllers publish their exact content-bound app identity only
  after MainLoop is usable. A detached older UI cooperatively saves
  state, returns displayed provider panes, releases UI-only leases, and execs
  the validated new absolute app after a nonce-bound pane-local request.
  An unidentified, attached, ambiguous, or racing controller is left untouched
  and requires a normal Soft Quit. Neither path kills the tmux
  server, a provider session/process, or Codex/Claude history.
- The ordinary label-selected tmux attach remains the fast path. If Windows
  rejects only that terminal attachment while the same server and immutable
  Railmux session remain healthy, Railmux makes one fail-closed transparent
  bridge attempt. While a direct attach remains alive, the existing launcher
  watchdog compares the exact entry TTY dimensions and sends `SIGWINCH` only
  to its own tmux client when either dimension changes. This repairs a missed
  MSYS2 native-console resize without directly resizing the shared window or
  bypassing tmux's `smallest` multi-client policy. If Soft Quit preserved
  detached providers but removed the
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

Windows runtime CI performs an advisory one-byte HTTPS Range request against
the exact pinned artifact on every approved transport and validates its exact
`Content-Range`. It also reads the same bounded pacman database sample from
every approved package candidate. It intentionally does not use ICMP ping and
does not block product tests when an external mirror has a transient outage;
runtime integrity remains the SHA-256 and pacman package-signature checks, not
the CI capability probe.
The blocking Windows jobs separately exercise the native 3.10/3.13 bootstrap
and a real MSYS2/tmux/Python runtime. The archive job installs the exact pinned
tmux package with pacman's signature verification, while the MSYS2 job records
an exact package identity, requires tmux 3.7+, writes content-bound managed
markers, and runs the Windows UI
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


### Historical preview evidence

The published dev10-dev36 preview series established the released migration
boundaries represented by the current markers and parity ledger. In summary:

- dev10-dev19 exercised abandoned-socket recovery, shared-base app layers,
  bounded pip retry, slow stale-socket classification, the cross-terminal
  relay, session leases, startup batching, Soft Quit recreation, and exact
  provider-marker recovery.
- dev24 added immutable package-content identity, cooperative app-layer
  transition, process-aware pruning, native local/remote config and doctor, and
  Windows-origin SSH coverage.
- dev26-dev32 replaced executable archive extraction with bounded in-process
  extraction, kept package paths writable, added restart-journal authority for
  pacman core transactions, moved packaged-Python state outside virtualized
  AppData, measured mirrors, and made verified local app-layer reuse the normal
  version-only upgrade path.
- dev33-dev35 removed avoidable Preview and live-switch process amplification
  while retaining the identity-pinned swap transaction and independent
  transcript authority.
- dev36 bridged the historical preview version grammar into the app identity
  used by the 0.4 release-candidate line.

The full chronological investigation remains available in Git history and the
published preview tags; it is not required context for ordinary maintenance.
The structured parity ledger retains each feature disposition, automated gate,
and manual evidence requirement.

On 2026-08-07 the dedicated Windows 10 host completed the exact
dev35-to-dev36-to-rc1 transition without replacing its verified 96-package
base. Both new layers attached and detached normally; `doctor` reported the
rc1 UI ready, content verification matched, and a prune dry-run retained all
three transition layers without deleting anything.
