from railmux.tmux_capabilities import (
    TMUX_MINIMUM_SUPPORTED,
    TMUX_WINDOWS_VISUAL_FIDELITY_RECOMMENDED,
    classify_tmux_text,
    classify_tmux_version,
    parse_tmux_version,
)


def test_support_floor_remains_aligned_with_posix():
    assert TMUX_MINIMUM_SUPPORTED == (2, 7)
    assert TMUX_WINDOWS_VISUAL_FIDELITY_RECOMMENDED == (3, 7)


def test_msys2_and_cli_version_forms_are_parsed():
    assert parse_tmux_version("3.7.b-1") == (3, 7)
    assert parse_tmux_version("tmux 3.6a") == (3, 6)
    assert parse_tmux_version("1:3.7.b-1") == (3, 7)
    assert parse_tmux_version("next-3.8") == (3, 8)
    assert parse_tmux_version("not tmux") is None


def test_capability_separates_support_from_windows_visual_fidelity():
    floor = classify_tmux_version((2, 7))
    assert floor.support == "supported"
    assert floor.windows_visual_fidelity == "degraded"

    degraded = classify_tmux_text("3.6.a-1")
    assert degraded.support == "supported"
    assert degraded.windows_visual_fidelity == "degraded"
    assert degraded.version == "3.6.a-1"

    full = classify_tmux_version((3, 7))
    assert full.support == "supported"
    assert full.windows_visual_fidelity == "full"

    unsupported = classify_tmux_version((2, 6))
    assert unsupported.support == "unsupported"
    assert unsupported.windows_visual_fidelity == "unsupported"

    unknown = classify_tmux_version((0, 0))
    assert unknown.support == "unknown"
    assert unknown.windows_visual_fidelity == "unknown"
