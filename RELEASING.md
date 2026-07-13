# Releasing railmux

Maintainer notes for cutting a new release to PyPI.

## Versioning

railmux follows [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`. The single source of truth is `__version__` in `src/railmux/__init__.py`; `pyproject.toml` reads it dynamically via `tool.setuptools.dynamic`.

- **PATCH** — backwards-compatible bug fixes
- **MINOR** — backwards-compatible new features
- **MAJOR** — breaking changes (CLI flags, key bindings, on-disk config format, etc.)

## Prerequisites

- Push access to the [PyPI project](https://pypi.org/project/railmux/) with 2FA enabled.
- A PyPI API token scoped to the `railmux` project, stored in `~/.pypirc` or exported as `TWINE_PASSWORD` (with `TWINE_USERNAME=__token__`).
- Dev extras installed: `pip install -e ".[dev]"` — this provides `build` and `twine`.

## Release steps

1. **Bump the version** in `src/railmux/__init__.py` (e.g. `0.1.3` → `0.1.4`).
2. **Update `CHANGELOG.md`** with the user-visible changes for this version. If the file does not exist yet, create one using [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.
3. **Run the test suite**:
   ```bash
   pytest
   ```
4. **Smoke-test the TUI** locally (`railmux`) — at minimum: list projects, resume a session, switch sessions, quit cleanly.
5. **Commit** the version bump and changelog:
   ```bash
   git commit -am "release: vX.Y.Z"
   ```
6. **Build the distributions** from a clean `dist/`:
   ```bash
   rm -rf dist/
   python -m build
   ```
   This produces both an sdist (`.tar.gz`) and a wheel (`.whl`) in `dist/`.
7. **Check the artifacts** — verifies metadata and that the README renders on PyPI:
   ```bash
   python -m twine check dist/*
   ```
8. **(Optional) Dry-run on TestPyPI** to catch metadata or auth issues before touching the real index:
   ```bash
   python -m twine upload --repository testpypi dist/*
   pip install --index-url https://test.pypi.org/simple/ \
               --extra-index-url https://pypi.org/simple/ \
               railmux==X.Y.Z
   ```
9. **Upload to PyPI**:
   ```bash
   python -m twine upload dist/*
   ```
10. **Verify** the install works from a clean virtualenv:
    ```bash
    python -m venv /tmp/railmux-verify && source /tmp/railmux-verify/bin/activate
    pip install --upgrade railmux
    railmux --version   # should print X.Y.Z
    deactivate && rm -rf /tmp/railmux-verify
    ```
11. **Tag and push** — do this *after* the upload succeeds so a failed publish does not leave a dangling tag:
    ```bash
    git tag -a vX.Y.Z -m "railmux vX.Y.Z"
    git push origin main --follow-tags
    ```
12. **Create a GitHub Release** from the tag at https://github.com/regmi-saugat/railmux/releases/new, pasting the changelog entry into the release notes.

## Future: Trusted Publishing

Once releases get more frequent, this flow should move to PyPI [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) via GitHub Actions. That removes the need for long-lived API tokens entirely — PyPI mints a short-lived OIDC credential per workflow run. The migration is roughly:

1. Add a publisher on PyPI pointing at this repo and a workflow filename (e.g. `release.yml`).
2. Add `.github/workflows/release.yml` triggered on `push: tags: ['v*']` that runs `python -m build` and `pypa/gh-action-pypi-publish`.
3. Tag-first becomes safe again: pushing the tag triggers the publish.
