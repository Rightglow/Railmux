from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_release_waits_for_reusable_cross_platform_test_workflow():
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    test = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "uses: ./.github/workflows/test.yml" in release
    assert "needs: test" in release
    assert "workflow_call:" in test
    assert "os: [ubuntu-latest, macos-latest]" in test
    assert "website:" in test
    assert "npm run build" in test


def test_reusable_workflow_pins_and_runs_tmux_27_compatibility_floor():
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "tmux-27-smoke:" in workflow
    assert "https://github.com/tmux/tmux/releases/download/2.7/" in workflow
    assert (
        "9ded7d100313f6bc5a87404a4048b3745d61f2332f99ec1400a7c4ed9485d452"
        in workflow
    )
    assert 'test "$(tmux -V)" = "tmux 2.7"' in workflow
    assert "RAILMUX_RUN_TMUX_INTEGRATION: \"1\"" in workflow


def test_tag_push_does_not_start_an_unrelated_duplicate_test_run():
    test = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")

    push_section = test.split("pull_request:", 1)[0]
    assert "branches:" in push_section
    assert "- main" in push_section


def test_development_and_rc_tags_create_github_prereleases():
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert '[[ "$GITHUB_REF_NAME" =~ (\\.dev|rc)[0-9]+$ ]]' in release
    assert "prerelease+=(--prerelease)" in release
    assert '"${prerelease[@]}"' in release


def test_release_tags_are_canonical_and_fenced_to_main():
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "fetch-depth: 0" in release
    assert "origin/main" in release
    assert "release tags must be canonical final, rcN, or .devN versions" in release
    assert "all active releases must come from main" in release
    assert "development release from main" in release
    assert "release candidate from main" in release
    assert "final release from main" in release


def test_archived_windows_branches_remain_rejected_explicitly():
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "f1b8cb128bd78831d00d4cc7dfa02453f8bd9700" in release
    assert "912cae6bd62d31a6ad69ce7da2f85be4750ed84b" in release
    assert "archived ConPTY branch moved from its frozen tip" in release
    assert "archived ConPTY history is not release-eligible" in release
    assert "archived WSL-delegation branch moved from its frozen tip" in release
    assert "archived WSL-delegation history is not release-eligible" in release
    assert "windows-preview:refs/remotes/origin/windows-preview" not in release


def test_windows_wheel_and_promoted_mirror_gates_are_present():
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "Build and install the native Windows wheel without an index" in workflow
    assert (
        'pip install --no-index --no-deps --find-links dist "railmux==$version"'
        in workflow
    )
    assert "$statusJson = .\\.wheel-venv\\Scripts\\railmux runtime status --json" in workflow
    assert '$status.status -ne "not_installed"' in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.base_ref == 'main'" in workflow
