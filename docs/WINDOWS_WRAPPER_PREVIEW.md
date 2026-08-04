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

The preview supports local Railmux and the local side of `railmux ssh` to a
Linux, macOS, or compatible Unix host. Providers and SSH servers running on
native Windows remain out of scope. WSL remains usable only when the user
independently opens a WSL shell and runs the ordinary POSIX product there.

## Bootstrap contract

- Windows requires Python 3.10 or newer. Packaging metadata must retain Python
  3.9 for POSIX because Python package metadata cannot express an OS-specific
  interpreter floor; the native entrypoint enforces the Windows floor before
  runtime discovery.
- `railmux --version`, `--help`, and `runtime status` do not install or enter a
  runtime. When an exact private base candidate already exists, ordinary launch
  and `runtime install` state that only the versioned app layer is changing and
  continue without redundant confirmation. That automatic path passes a
  `reuse_only` authority into the serialized installer, so a validation race or
  damaged base can only fail and can never escalate into an unconfirmed full
  install. When no candidate exists, `runtime install` asks once for the roughly
  700 MB base operation and `N` cancels immediately; `runtime install --yes` is
  the explicit noninteractive authority. `runtime install --verbose` streams
  raw subprocess output in addition to retaining it in the install log.
- The official MSYS2 self-extracting base release, filename, size, and SHA-256
  are pinned. The bootstrap samples the official GitHub release first. When
  its projected remaining time exceeds 60 seconds, it concurrently samples
  the MSYS2 repository and the MSYS2-listed TUNA and NJU mirrors, switches only
  for at least a 25% measured improvement, and otherwise continues the best
  available transfer. Probe bytes are reused, and an interrupted transfer can
  resume at the exact offset from another approved source. Servers must return
  strict HTTPS `206` responses with the expected `Content-Range`; if adaptive
  transfer is unavailable, the bootstrap falls back to ordinary full downloads
  in the approved order. A wrong final size or SHA-256 removes the archive, and
  every path must produce the same Railmux-pinned digest before execution.
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
  not automatically pruned in dev12. Their growth is the application-layer
  size (22.4 MiB in the measured dev11 environment), not another MSYS2 base;
  a future bounded-retention command must preserve the active and immediately
  previous versions, remain limited to exact marked app directories, and also
  bound the separate pip cache without removing files from a live install.
- No system `PATH`, shell profile, Windows package manager, user-owned MSYS2,
  or provider history is modified. All captured text and marker files use an
  explicit UTF-8 codec; CP936/GBK is never an implicit file encoding.
- The handoff uses an argv list and a fixed shell literal. A `$0` sentinel
  prevents dropping the first argument, and MSYS argument conversion is
  disabled at the Windows boundary then restored before native providers run.
- The parent waits through Ctrl-C instead of killing the child. tmux owns
  persistence after detach or outer-window closure.
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
  unchanged same-user socket after the recorded server PID is proven gone, and
  prints an info message that Codex/Claude session files were untouched. Any
  live PID, provider or unknown session denies this cleanup; a failed outer
  teardown revokes the proof immediately.
- Startup repaints the restoring surface with separate read-only indexing and
  tmux-reconnection stages. A slow startup reports its measured initialization
  time and the private-cache boundary after the first frame.
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
seconds. The dev13 candidate gives only managed-Windows default startup
discovery a five-second bound. Explicit health/watchdog probes, the POSIX
default, and the proof-gated socket unlink authority remain unchanged.

Only `0.4.0.dev4+` development releases may be cut from `windows-preview`.
This document is not a stable-support claim, and the repository README and
website remain unchanged until the manual checklist is complete.
