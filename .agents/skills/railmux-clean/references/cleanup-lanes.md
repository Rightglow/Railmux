# Railmux cleanup lanes

Use this reference to keep different cleanup intents independent. Select only
the lanes authorized by the user; a finding in one lane does not authorize work
in another.

## Contents

1. Public product surface
2. Stable-release alignment
3. Transition retirement
4. Test rationalization
5. Non-functional repository structure
6. Documentation lifecycle
7. Candidate and closure ledgers

## 1. Public product surface

### Goal

Let a new or ordinary user install Railmux, understand support, start work, and
recover from common failures without reading implementation details.

### Audit

- Inventory root `README.md`, website source, CLI `--help`, packaging metadata,
  install guidance, screenshots, and links.
- Separate core tasks from advanced diagnostics, manual recovery, maintainer
  operations, implementation rationale, and release archaeology.
- Identify duplicated claims and determine their authoritative source.
- Search for commands exposed as ordinary workflow even though normal startup
  performs them automatically.

### Placement rules

- Keep install, first launch, core workflow, interaction basics, supported
  platforms/providers, basic configuration, and short troubleshooting in the
  README and website.
- Keep exhaustive capability/evidence status in `docs/SUPPORT_MATRIX.md`.
- Keep managed-runtime ownership, invariants, recovery authority, and package
  details in `docs/WINDOWS_RUNTIME.md` or its current successor.
- Keep contributor and release procedures in `CONTRIBUTING.md` and
  `RELEASING.md`.
- Link once from a user surface when advanced recovery is genuinely useful.

### Runtime-command example

Treat `railmux runtime ...` as a classification exercise, not a predetermined
deletion. Ask:

1. Does first launch/update already perform this safely?
2. Is the command required for uninstall, verification, support, or repair?
3. Is it stable public API or a maintainer/debug interface?
4. Can ordinary troubleshooting link to focused details instead of listing the
   entire command family?

Usually keep only the necessary user action or focused troubleshooting link on
the landing surface. Preserve required recovery/uninstall commands in the
appropriate focused document and CLI help.

## 2. Stable-release alignment

### Choose the target

- Default public target: latest published final version, excluding `.devN` and
  `rcN`.
- Explicit release preparation: the named candidate intended to become final,
  after required evidence passes.
- Engineering docs may describe `main`, but must label unreleased capability
  and must not make the public site imply it is already stable.

### Cross-surface contract

Compare at least:

- `src/railmux/__init__.py` and packaging metadata;
- `CHANGELOG.md`, `RELEASING.md`, tags, GitHub Release, and PyPI publication;
- README install/version/support wording;
- website install/version/support wording and generated outputs;
- `docs/SUPPORT_MATRIX.md` status labels and manual evidence;
- platform/runtime evidence ledgers named by the support matrix.

Align claims, not necessarily detail. README and website should agree on the
supported product while the support matrix may carry more precise evidence.
Never promote mocked coverage to supported or field-validated status.

## 3. Transition retirement

### Entry gate

Read `RELEASING.md` for version grammar, publication checks, and frozen branch
or tag boundaries before applying this lane, including when it runs by itself.
Use `docs/ARCHITECTURE.md` as the authority for session/history state. Read the
evidence document named by `docs/README.md` only for the affected subsystem:
for example, `docs/WINDOWS_RUNTIME.md` for managed-runtime generations and
`docs/BACKGROUND_SESSION_INDEX.md` for background-index generations. An
evidence document does not replace the architecture contract.

Before removing a preview, RC, migration, or compatibility path, prove:

- the relevant stable release is actually published, or the user explicitly
  scoped cleanup to unreleased states that never became a contract;
- no supported upgrade begins from the state being removed;
- no real persisted or live runtime/session state still requires the path;
- current install, update, attach, recovery, doctor, config, and uninstall
  flows have replacement coverage where applicable;
- references, flags, environment variables, schemas, markers, CI, tests, and
  docs can retire together.

Verify stable publication using the GitHub/PyPI checks described by
`RELEASING.md`. If those authoritative sources are unavailable or disagree,
record the missing evidence and classify the candidate as **Defer**.

### Usually retain

- migrations from a released stable version;
- runtime-generation transitions required by installations in the field;
- session/history safety and ownership checks;
- failure recovery that protects user data or live agents;
- protocol compatibility promised by architecture;
- concise historical changelog entries and unique reproducible evidence.

### Candidates to retire after proof

- branches for abandoned algorithms that cannot be selected;
- RC-only flags or environment variables never included in a stable contract;
- migrations between prerelease states no supported install can still occupy;
- duplicated app/runtime markers superseded by one authoritative schema;
- temporary diagnostics, scaffolding, review handoffs, generated diffs, and
  implementation diaries whose durable conclusions live elsewhere;
- tests that only preserve a removed intermediate implementation.

### Closure check

Search by retired symbol, marker, version grammar, environment variable, file
name, CLI spelling, and user-facing phrase. Ensure errors and doctor output no
longer recommend retired paths. Prefer deletion over an in-tree archive unless
the artifact remains reproducible evidence for a current decision.

## 4. Test rationalization

### Build a coverage ledger first

For every candidate test or test group, record:

- durable behavior or failure boundary;
- contract authority;
- unique platform/process/terminal condition;
- whether it is positive, negative-preservation, integration, or
  implementation-detail coverage;
- replacement test if removed.

### Consolidate safely

- Prefer parameterization when only data differs and failure diagnosis remains
  clear.
- Prefer one representative boundary or round-trip test over many mocks of the
  same helper.
- Keep both the broken case and ordinary unaffected case for regressions.
- Keep real tmux, SSH, Windows, terminal, IME, clipboard, pointer, and provider
  checks when mocks cannot establish fidelity.
- Before consolidating PTY, display-transport, or terminal-renderer tests, run
  and preserve the backpressure verification contract in
  `docs/ARCHITECTURE.md`, including the ordinary POSIX boundary it requires.
- Remove fixtures and helpers with their last caller.
- Do not merge tests whose independent setup catches ordering, ownership,
  timeout, encoding, or cleanup bugs.

Test count, duration, and file size are signals, not removal criteria. A slow
test should be optimized or moved to the correct gate when it protects a real
boundary.

## 5. Non-functional repository structure

Run this lane independently when the request is about redundancy or agent
navigability only.

### Invariants

Do not change observable CLI/UI behavior, exit status, output text relied on by
tests or tools, configuration/schema, persisted state, protocol/wire format,
recovery authority, security boundary, support status, or semantically relevant
timing. If a cleanup requires such a change, move it to a separately authorized
functional task.

### Review targets

- repeated helpers, parsing, normalization, constants, and state projections;
- modules with mixed ownership or circular routing;
- duplicated or misleading names and dead abstraction layers;
- package exports and import direction;
- file/directory placement relative to `docs/CODE_MAP.md` ownership;
- large files whose cohesive units have independently testable boundaries;
- stale comments that narrate implementation history rather than current
  invariants;
- agent navigation: obvious entry points, searchable symbols, small authority
  set, and accurate code-map links.

### Refactor rules

- Prefer one source of truth and shallow call paths.
- Extract by stable responsibility, not arbitrary line count.
- Avoid generic `utils` dumping grounds.
- Preserve names that form CLI, config, protocol, persisted, or documented API.
- Update `docs/CODE_MAP.md` when ownership moves; keep invariants in their
  authoritative document rather than the map.
- For PTY, display-transport, terminal-renderer, or semantically relevant timing
  refactors, satisfy the backpressure verification contract in
  `docs/ARCHITECTURE.md`; structural intent does not weaken that contract.
- Prove behavior preservation with existing public/boundary tests before and
  after the refactor.

## 6. Documentation lifecycle

- Keep architecture focused on the current invariant, not the sequence of
  failed attempts that produced it.
- Keep evidence only while it protects an open decision, a supported
  experimental path, or a reproducible future choice.
- Move genuine future work to `ROADMAP.md`; do not present it as commitment.
- Remove completed prompts, review transcripts, local diagnostic reports, and
  patch handoffs after durable conclusions are captured.
- Do not duplicate exact defaults, timeout values, version floors, or support
  claims across navigation docs.
- Validate internal and public links after moves.
- Do not hand-edit built website output when the source and documented build
  pipeline own it.

## 7. Candidate and closure ledgers

Use this compact candidate format:

| Candidate | Lane | Current authority/caller | Disposition | Replacement | Risk | Validation |
| --- | --- | --- | --- | --- | --- | --- |

For test changes, add behavior authority and surviving test. For transition
changes, add introduced state, released/persisted status, and earliest safe
removal boundary.

Before delivery, record:

- all tracked files added, moved, merged, and deleted;
- all externally visible text or behavior touched;
- every removed test and its surviving coverage;
- every compatibility path removed and its retirement proof;
- release sources used to align public claims;
- focused/full checks run and manual evidence still pending;
- deferred items with a concrete reason rather than a vague future cleanup.
