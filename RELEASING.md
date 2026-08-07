# Releasing Railmux

Maintainer notes for publishing a new Railmux release to PyPI. Releases use
GitHub Actions and PyPI Trusted Publishing; no long-lived PyPI token is needed.

## Versioning

Railmux follows [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.
The single source of truth is `__version__` in `src/railmux/__init__.py`;
`pyproject.toml` reads it dynamically.

- **PATCH** — backwards-compatible bug fixes
- **MINOR** — backwards-compatible new features
- **MAJOR** — breaking changes

Before 1.0, a MINOR release may also establish a documented product-maturity
baseline for compatible capabilities delivered across earlier releases. Keep
that milestone narrative in `CHANGELOG.md`; this file remains process-only.

Field-test builds for the next release may use the PEP 440 form
`MAJOR.MINOR.PATCH.devN`, where `N` is a monotonically increasing numeric
identifier. A development tag is published to PyPI normally and marked as a
GitHub pre-release; pip excludes it from ordinary stable upgrades unless the
version is explicitly requested or pre-releases are enabled. Never append
`.devN` to an already released final version: `0.2.10.devN` sorts before
`0.2.10`, so development builds after 0.2.10 must target `0.2.11.devN`.
Release candidates use `MAJOR.MINOR.PATCHrcN`. They are also GitHub
pre-releases and opt-in on pip. Use an RC for the final artifact/upgrade path;
do not change user-visible CLI or runtime behavior between the last RC and the
final release without another RC.

All active development, RC, and final releases are cut only from `main`.
Native Windows joined that product line in 0.4 through one reviewed squash of
the managed-MSYS2 preview tree. The `windows-preview` branch freezes at
`v0.4.0.dev36`; do not publish or merge from it after promotion.
The abandoned ConPTY experiment is frozen at `v0.4.0.dev2` on
`archive/windows-conpty-deprecated`; it is not release-eligible. Shared fixes
and platform adapters now land together on focused branches from `main`.

The already-published `0.4.0.dev1` and `0.4.0.dev2` artifacts are historical
validation builds, not the active preview. Yank them on PyPI after the archive
migration so unconstrained `pip --pre` resolution does not prefer the abandoned
implementation; a yank preserves exact-pin installation for reproduction.

## One-time publishing setup

1. Create a GitHub environment named `pypi` and require maintainer approval for
   deployments if the repository plan supports it.
2. In the existing [Railmux PyPI project](https://pypi.org/project/railmux/),
   add a GitHub Trusted Publisher with:
   - Owner: `Rightglow`
   - Repository: `Railmux`
   - Workflow: `release.yml`
   - Environment: `pypi`

The workflow in `.github/workflows/release.yml` requests a short-lived OIDC
credential and publishes only after its build and test job succeeds.

## Release steps

1. Update `src/railmux/__init__.py` and move the user-visible entries in
   `CHANGELOG.md` from **Unreleased** to the new version and date.
   Preview the GitHub Release body generated from that exact section:

   ```bash
   python tools/release_notes.py X.Y.Z
   ```

   A missing or empty release section is an error, so the tagged release
   cannot silently publish an empty set of notes.
2. Run the full test suite and smoke-test the TUI on supported platforms:

   ```bash
   python -m ruff check src tests tools
   python -m pytest -q
   RAILMUX_RUN_TMUX_INTEGRATION=1 python -m pytest -q tests/test_tmux_integration.py
   ```

   When authenticated current Claude Code and Codex installations are
   available, open **Help → Ask Railmux** in both modes. Confirm ordinary
   reads/searches work without approval and requests to write files or run a
   shell fail closed. Provider-free tests continue to enforce the exact
   safety-restricted command shape.

3. Build and validate clean artifacts locally:

   ```bash
   rm -rf dist build src/*.egg-info
   python -m build
   python -m twine check dist/*
   python tools/release_notes.py X.Y.Z
   ```

4. Commit and push the release preparation. Wait for every Python 3.9–3.13,
   native-Windows wheel/bootstrap, full-archive, real-MSYS2, mirror-health,
   tmux-floor, build, and website job to complete. Mirror health is advisory;
   every product-behavior gate is required.
5. Create and push only the intended annotated tag. Pushing it starts the
   publishing workflow, so do not tag until the release commit is ready:

   ```bash
   git tag -a vX.Y.Z -m "Railmux X.Y.Z"
   git push origin vX.Y.Z
   ```

6. Watch the release workflow. It builds and tests on Python 3.9, publishes the
   checked artifacts to PyPI, and creates a GitHub Release with those artifacts
   and the matching curated `CHANGELOG.md` section.
7. Verify the published package from clean Python 3.9 and current-Python virtual
   environments:

   ```bash
   python3.9 -m venv /tmp/railmux-verify
   /tmp/railmux-verify/bin/pip install --no-cache-dir railmux==X.Y.Z
   /tmp/railmux-verify/bin/railmux --version
   rm -rf /tmp/railmux-verify
   ```

   For an RC/final that changes the managed Windows app identity, also verify
   the exact previous-app → candidate → final transition on one real Windows
   runtime. Confirm base reuse, attach, `doctor`, `runtime status --verify`, and
   `runtime prune --dry-run` before pruning anything.

Do not use `git push --follow-tags`: push the exact release tag so unrelated
local tags can never trigger a publication accidentally.
