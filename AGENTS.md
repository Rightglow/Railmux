# Railmux agent guidance

This repository's design documentation is written primarily for coding agents
starting with repository context only.

Before planning a non-trivial behavior or architecture change:

1. Read [`docs/README.md`](docs/README.md) to locate the authoritative source.
2. Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for every affected
   invariant, then read the relevant evidence document when one is listed.
3. For a provider, operating-system, terminal-emulator, or product-capability
   change, identify the affected stable feature IDs and acceptance checklist in
   [`docs/SUPPORT_MATRIX.md`](docs/SUPPORT_MATRIX.md).
4. Treat [`ROADMAP.md`](ROADMAP.md) as open questions, not approved behavior,
   and [`README.md`](README.md) as the user contract, not an internal design
   specification.

Keep the repository-front-page [`README.md`](README.md) task-oriented and
user-facing: installation, supported workflows, controls, configuration, and
troubleshooting belong there. Put implementation rationale, invariants,
recovery authority, compatibility boundaries, provider parsing rules, and
maintainer procedures in the document selected by [`docs/README.md`](docs/README.md)
or in [`CONTRIBUTING.md`](CONTRIBUTING.md). Link rather than duplicate when a
short user-facing explanation needs deeper engineering context.

Do not use completed task prompts, generated diffs, or review transcripts as a
competing source of truth. When implementation changes a durable invariant,
compatibility boundary, recovery authority, or evidence-based product decision,
update the corresponding document in the same change.
Keep provider-neutral and multi-slot constraints intact unless the task
explicitly changes the documented architecture.
Do not promote a mocked OS branch or compatible terminal protocol into a new
platform-support claim without the real-platform evidence required by the
support matrix.

Before declaring a change ready to commit or merge, perform a closure review
of the complete diff, not only a correctness pass:

- Remove experimental branches, temporary scaffolding, superseded comments,
  and tests that exist only to preserve an abandoned or intermediate
  implementation. The final tree should describe the chosen design directly.
- Keep compatibility or migration logic only for a released contract or real
  persisted/live state that Railmux must still upgrade safely. Do not add
  speculative compatibility; make the authority and retirement boundary clear.
- Keep the smallest test set that protects durable behavior, safety boundaries,
  and demonstrated regressions. Prefer one strong boundary or round-trip test
  over redundant implementation-detail permutations, and remove obsolete tests
  together with the code they protected.
- Explicitly audit the final diff for dead paths, duplicated state, stale names,
  transitional wording, and accidental scope growth before delivery.

Follow [`CONTRIBUTING.md`](CONTRIBUTING.md) for verification and delivery.

## Branch and release scope

`main` is the release-ready POSIX/WSL product line. It must not contain native
Windows development code, native Windows dependencies, native Windows CI jobs,
or support claims for running local Railmux or the local `railmux ssh` client
from PowerShell/CMD/native Windows Python. Existing WSL integrations remain in
`main` because Railmux runs inside the supported Linux runtime there. Publish
POSIX/WSL development builds from this branch when a fix needs field testing.
After the `v0.3.3` final release, the next stable-line preview series is
`0.3.4.devN`, beginning at `0.3.4.dev1`; never append another `.devN` build to
an already published final version.

`archive/windows-conpty-deprecated` is the frozen, read-only archive of the
abandoned native ConPTY compositor experiment. It ends at `v0.4.0.dev2`.
Never base new work on it, merge it, publish another release from it, or move
the branch/tag references. Consult it only for bounded lessons or
platform-neutral changes that are independently reimplemented and reviewed.

`archive/windows-wsl-delegation-deprecated` is the frozen, read-only archive
of the abandoned native-Windows-to-WSL bootstrap experiment. It ends at
`v0.4.0.dev3`. Native Windows users could not naturally share their existing
Windows Codex/Claude installation, credentials, paths, and provider history
with a Linux provider runtime, so this is not a fallback or a base for new
Windows work. Ordinary Railmux launched by a user from inside WSL remains part
of the supported POSIX product line on `main`.

`windows-preview` is the long-lived integration branch for the replacement
Windows bootstrap/wrapper experiment. It must start from current `main` and
must not inherit the archived ConPTY compositor, native provider hosting, or
parallel UI implementation. Native Windows Python owns only discovery,
consent, installation/update, path/argument translation, and handoff into a
private managed MSYS2/tmux runtime. Railmux itself runs under MSYS2 while the
providers remain the user's Windows-native Codex/Claude executables and use
their existing Windows-owned session/config directories. The POSIX UI remains
the one behavioral authority. Do not add a WSL delegation fallback: native
Windows launch and ordinary user-initiated WSL launch are separate products.
This branch may publish only PEP 440 development releases in the
`0.4.0.devN` series, continuing at `0.4.0.dev4` after the archived ConPTY and
WSL builds; do not use that series for `main` builds. Develop
Windows changes on focused branches based on
`windows-preview`, then merge them back into `windows-preview`; never merge
that branch wholesale into `main`.

Shared POSIX/provider fixes belong in `main` first and flow one way into
`windows-preview`. If a bug is discovered while testing Windows, separate the
provider-neutral fix from the Windows adapter change and land only the former
in `main`. Keep commits separable so a future Windows promotion can be reviewed
and merged deliberately instead of importing the preview branch's history.

Before changing code or publishing, verify the current branch. Final
`MAJOR.MINOR.PATCH` tags and POSIX/WSL `.devN` tags must point to commits
reachable from `main`. Windows-wrapper preview tags must contain `.devN`, use
`0.4.0.dev4` or later in the `0.4.0.devN` series, and point to commits reachable from
`windows-preview` but not `main`. No commit reachable only from the archived
ConPTY or WSL-delegation branch is release-eligible. Merge shared fixes from
`main` before cutting the corresponding Windows preview build; never copy a
Windows release commit or tag back to `main`.
