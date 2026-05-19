# Contributing to ccmgr

Thanks for your interest in ccmgr. Issues and pull requests are welcome.

## Dev setup

ccmgr targets Python 3.12+ and requires `tmux` on `PATH`.

```bash
git clone https://github.com/regmi-saugat/ccmgr
cd ccmgr
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

```bash
pytest
```

Tests live in `tests/` and run against the package installed in editable mode. Please add a test alongside any bugfix or new behavior.

## Running locally

```bash
ccmgr
```

The entry point is `ccmgr.cli:main`. Source lives under `src/ccmgr/`.

## Pull requests

- Open an issue first for non-trivial changes so we can agree on the approach before you write code.
- Keep PRs focused — one logical change per PR.
- Make sure `pytest` passes and the TUI still launches cleanly before pushing.
- Commit messages: short imperative subject (e.g. `discovery: handle empty projects dir`); reference the issue in the body when relevant.

## Reporting bugs

File an issue at https://github.com/regmi-saugat/ccmgr/issues with:

- ccmgr version (`ccmgr --version` or check `src/ccmgr/__init__.py`)
- Python version, OS, and tmux version (`tmux -V`)
- Steps to reproduce and what you expected vs. what happened
