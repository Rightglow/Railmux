from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_release_waits_for_reusable_cross_platform_test_workflow():
    release = (ROOT / ".github/workflows/release.yml").read_text()
    test = (ROOT / ".github/workflows/test.yml").read_text()

    assert "uses: ./.github/workflows/test.yml" in release
    assert "needs: test" in release
    assert "workflow_call:" in test
    assert "os: [ubuntu-latest, macos-latest]" in test


def test_reusable_workflow_pins_and_runs_tmux_27_compatibility_floor():
    workflow = (ROOT / ".github/workflows/test.yml").read_text()

    assert "tmux-27-smoke:" in workflow
    assert "https://github.com/tmux/tmux/releases/download/2.7/" in workflow
    assert (
        "9ded7d100313f6bc5a87404a4048b3745d61f2332f99ec1400a7c4ed9485d452"
        in workflow
    )
    assert 'test "$(tmux -V)" = "tmux 2.7"' in workflow
    assert "RAILMUX_RUN_TMUX_INTEGRATION: \"1\"" in workflow


def test_tag_push_does_not_start_an_unrelated_duplicate_test_run():
    test = (ROOT / ".github/workflows/test.yml").read_text()

    push_section = test.split("pull_request:", 1)[0]
    assert "branches:" in push_section
    assert "- main" in push_section


def test_development_tag_creates_a_github_prerelease():
    release = (ROOT / ".github/workflows/release.yml").read_text()

    assert 'if [[ "$GITHUB_REF_NAME" == *.dev* ]]' in release
    assert "prerelease+=(--prerelease)" in release
    assert '"${prerelease[@]}"' in release


def test_release_tags_are_fenced_to_their_product_branches():
    release = (ROOT / ".github/workflows/release.yml").read_text()

    assert "fetch-depth: 0" in release
    assert "origin/main" in release
    assert "origin/windows-preview" in release
    assert "POSIX/WSL development release from main" in release
    assert "Windows-wrapper development release from windows-preview" in release
    assert "main development releases must use the 0.3.x.devN series" in release
    assert "Windows-wrapper releases must use the 0.5.0.devN series" in release
    assert "development releases must come from main or windows-preview" in release
    assert "final releases must come from main" in release


def test_active_preview_branch_is_tested_and_archive_is_rejected_explicitly():
    test = (ROOT / ".github/workflows/test.yml").read_text()
    release = (ROOT / ".github/workflows/release.yml").read_text()

    push_section = test.split("pull_request:", 1)[0]
    assert "- windows-preview" in push_section
    assert "f1b8cb128bd78831d00d4cc7dfa02453f8bd9700" in release
    assert "archived ConPTY branch moved from its frozen tip" in release
    assert "archived ConPTY history is not release-eligible" in release
    assert "windows-preview:refs/remotes/origin/windows-preview || true" in release
