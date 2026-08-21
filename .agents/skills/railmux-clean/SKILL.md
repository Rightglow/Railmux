---
name: railmux-clean
description: Audit or safely simplify the Railmux repository after previews, release candidates, migrations, or major feature work. Use when a user invokes railmux_clean or $railmux-clean, asks to trim README or website internals, align public surfaces and the support matrix with a stable release, retire transitional code/docs/tests after a release, reduce redundant tests, or perform a behavior-preserving cleanup for code redundancy, agent navigability, Markdown quality, and directory organization.
---

# Railmux Clean

Keep the checked-in tree focused on the current supported product without
discarding recovery, compatibility, evidence, or behavior that users still
depend on. Treat cleanup as an evidence-backed product-maintenance task, not a
line-count exercise.

## Select the operating mode

Infer the narrowest mode authorized by the request:

- **Audit**: inspect and report only. Use this when the user asks what could be
  cleaned, invokes the skill without a change verb, or asks for a proposal.
- **Apply one lane**: change only the named cleanup lane.
- **Release cleanup**: combine relevant lanes around a named stable release.
- **Full cleanup**: use all lanes only when the user explicitly asks for it.

Do not publish, tag, push, delete branches, or rewrite history unless the user
explicitly requests that separate action. Do not mix discovered feature fixes
into a non-functional cleanup; report them separately.

Read [references/cleanup-lanes.md](references/cleanup-lanes.md) completely
before planning. Use only the lane sections relevant to the selected mode.

## Establish authority and scope

1. Read `AGENTS.md` completely.
2. Follow `docs/CODE_MAP.md` to the smallest relevant authority, beginning
   with `docs/README.md`. For release cleanup also read `RELEASING.md`,
   `CONTRIBUTING.md`, `CHANGELOG.md`, and `docs/SUPPORT_MATRIX.md`. Read
   `RELEASING.md` whenever the transition-retirement lane is selected, even
   when it runs alone.
3. Inspect the branch, worktree, recent tags, package version, and relevant
   release metadata. Preserve unrelated user changes.
4. State the cleanup target: current stable release, an explicitly named
   release candidate being prepared as final, or repository-only
   non-functional maintenance.
5. Build an inventory before editing. Include user surfaces, authoritative
   engineering docs, implementation paths, tests, CI, tools, and generated
   outputs that are actually tracked.

For claims about the latest published release, do not infer publication from
`__version__` or a local tag alone. Verify the authoritative GitHub/PyPI release
state when it affects the requested work. Ordinary public surfaces target the
latest final release by default; align them to an upcoming version only during
explicit release preparation. Follow the publication checks in `RELEASING.md`;
if authoritative GitHub/PyPI state cannot be verified, classify release-bound
retirement candidates as **Defer**, never **Retire**.

## Classify before changing

Assign every candidate one disposition and record its evidence:

- **Keep**: current contract, safety boundary, supported recovery path, or
  unique regression coverage.
- **Move/link**: useful detail exists at the wrong audience level.
- **Merge**: duplicate authority or coverage can become one stronger source.
- **Retire**: no supported caller, persisted state, release contract, or unique
  evidence still requires it.
- **Defer**: authority or retirement evidence is incomplete.

Do not call an item transitional merely because its name, age, platform, or
implementation is inconvenient. Record its introduction authority, supported
state, retirement condition, references, and replacement before retiring it.

## Prepare an evidence-backed plan

For audit mode, produce a table with candidate, lane, evidence, disposition,
risk, dependency/coverage replacement, and validation. Stop after the report.

For an authorized implementation:

1. Partition work by lane so a documentation-surface cleanup cannot silently
   change runtime behavior.
2. Map every removed implementation path to callers and every removed test to
   the durable behavior still covered elsewhere.
3. Prefer moving advanced or maintainer material to its authoritative home and
   linking once over deleting information that remains operationally useful.
4. Sequence structural moves before wording cleanup when the new locations are
   needed as link targets.
5. Identify real-platform or manual evidence that cannot be replaced by unit
   tests.

Ask for direction only when two plausible choices change the supported product
or recovery contract. Otherwise choose the smallest reversible cleanup.

## Apply changes safely

- Keep README and website task-oriented. An ordinary user should see install,
  first launch, supported workflows, core controls/configuration, and concise
  troubleshooting—not runtime internals or release archaeology.
- Do not remove an advanced command solely because most users do not need it.
  First determine whether it is a required recovery/uninstall path; if so,
  retain discoverability in focused troubleshooting or maintainer docs.
- Preserve historical release notes. Stop stale history from acting as current
  guidance, but do not rewrite old changelog entries as if they never happened.
- Retire preview/RC bridges only after the stable boundary and real persisted or
  live-state obligations are proven. Git history is normally the archive; do
  not keep source-tree museums.
- Reduce tests by behavior and risk, not by count. Keep contract, safety,
  platform-boundary, migration, and demonstrated-regression coverage. Remove
  implementation-detail duplication only after stronger coverage exists.
- In the non-functional lane, preserve public APIs, CLI output, config/schema,
  persisted state, protocol behavior, terminal behavior, timings with semantic
  meaning, and support claims.
- Keep generated artifacts out of hand edits. Update their source or run the
  documented generator.

## Validate in widening rings

1. Check references, imports, entry points, CLI help, version strings, and links
   affected by the cleanup.
2. Run focused tests for moved or retired ownership and for each preserved
   behavior boundary.
3. Run the repository checks required by `CONTRIBUTING.md` in proportion to the
   change. Use real tmux/platform gates when their contract is touched.
4. Compare README, website, support matrix, changelog, package version, and
   release metadata against the chosen release target.
5. Inspect the complete final diff for dead paths, duplicated authority, stale
   prerelease wording, accidental behavior changes, and unrelated edits.

Validation of mocked Windows or SSH branches is not publication evidence for a
platform support claim. Preserve manual acceptance items when automation cannot
faithfully cover terminal, IME, pointer, clipboard, provider, or cross-host
behavior.

## Report the result

Lead with what became simpler. Include:

- selected mode and lanes;
- files moved, merged, or retired and the authority that replaced them;
- tests removed, consolidated, or added, with the durable coverage retained;
- release/version sources used for public-surface alignment;
- automated and manual validation completed or still required;
- deferred candidates and the missing retirement evidence.

Never describe reduced line count or test count alone as success.
