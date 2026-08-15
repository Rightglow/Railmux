# Railmux agent guidance

This repository's design documentation is written primarily for coding agents
starting with repository context only.

Before planning a non-trivial behavior or architecture change:

1. Use [`docs/CODE_MAP.md`](docs/CODE_MAP.md) to identify the owning symbols,
   focused tests, and authority. It is a navigation index, not a specification.
2. Read [`docs/README.md`](docs/README.md), then only the affected sections of
   [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and any evidence document it
   names. Do not load unrelated platform or transport history by default.
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

## Preserve established interaction semantics

Treat an existing mouse, keyboard, click, focus, preview, and scrolling
behavior as a product contract even when a nearby bug can be solved more
easily by repurposing it. Do not infer authorization to remap that behavior
from a discussion about an adjacent workflow. State the exact before/after
interaction and obtain explicit product agreement before implementing such a
change.

Keep live terminal interaction, explicit provider-history Preview, and
transport-managed history as separate concepts. In particular, direct live
scrolling remains tmux/provider-native, while `railmux ssh` retains its own
bounded per-pane scrolling manager and explicit Preview remains a deliberate
Space/menu action. A transcript locator advertises availability; its presence
alone is never authority to change ordinary live-history format. Any fallback
that changes the source or representation of history must be gated by the
smallest exact validated state that requires it, such as a confirmed branch
generation matching the same rollout.

Every regression fix must protect both sides of its boundary: add a positive
test for the broken case and a negative behavior-preservation test for the
ordinary unaffected case. When fidelity is the user-visible contract, cover a
representative composition of that path (for example, an unrewound live Codex
pane with a transcript locator, styled output, and deep history), rather than
relying only on isolated helper tests. A new test that merely codifies a
changed implementation is not evidence that established behavior survived.

For PTY, display-transport, or terminal-renderer changes, tiny in-memory frames
are not sufficient backpressure evidence. Include a producer burst larger than
one production read, a slow or blocked downstream writer, and input arriving
while output is busy. Assert bounded queue/state growth, latest-screen rather
than intermediate-frame replay, continued input/health-loop progress, and the
ordinary low-volume path. Keep native POSIX local attach outside a Windows/SSH
renderer change unless the product request explicitly expands that boundary.

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

From `v0.4.0` onward, `main` is the single release-ready product line for
macOS, Linux, WSL, and the native Windows managed-runtime adapter. Shared UI,
provider, session, transport, and lifecycle behavior has one implementation;
do not create a Windows-only copy. Native Windows Python may own only runtime
discovery, consent, installation/update, path and argument translation, and
handoff into the private managed MSYS2/tmux runtime. Providers remain the
user's Windows-native Codex/Claude executables and keep their Windows-owned
session/config directories.

All active development, RC, and final tags must point to commits reachable
from `main`. Canonical version grammar, the frozen preview/archive branch
boundaries, publication checks, and transition evidence live in
[`RELEASING.md`](RELEASING.md); do not duplicate that history in task context.

Do not add a WSL delegation fallback or revive a ConPTY compositor. Native
Windows launch and a user independently launching Railmux inside WSL remain
separate supported entry surfaces. Automatic installation of a Windows runtime
over SSH and adoption of arbitrary user-owned MSYS2 trees remain out of scope.

Before changing code or publishing, verify the current branch, inspect the
complete diff, and keep platform-neutral behavior platform-neutral. A Windows
bug may require an adapter fix, a shared fix, or both; test both the affected
surface and the ordinary POSIX path. No commit reachable only from an archived
Windows branch is release-eligible.
