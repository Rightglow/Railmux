"""Rendering the status line into the outer tmux status bar (ccmgr's only status
surface). Only the pure escaping/style/guard logic of ``_render_status_to_tmux``
is exercised here (tmux itself is stubbed) — the option round-trip, the forced
redraw, and the actual colour rendering are verified manually against a
throwaway tmux session.
"""
from unittest.mock import MagicMock

from ccmgr.ui.app import App, _TMUX_THEMES


def _status_app(*, enabled=True, session="ccmgr"):
    app = App.__new__(App)
    app._tmux_status_enabled = enabled
    app._tmux_status_session = session
    return app


def _set_option_call(run):
    """The tmux set-option invocation among the captured subprocess calls."""
    for call in run.call_args_list:
        argv = call.args[0]
        if argv[:2] == ["tmux", "set-option"]:
            return argv
    raise AssertionError("no tmux set-option call captured")


def _payload(run):
    """The status-right value pushed by the set-option call."""
    return _set_option_call(run)[5]


def test_escapes_hash_and_percent_inside_style(monkeypatch):
    # tmux runs status strings through #{...} format expansion AND strftime, so
    # both '#' and '%' in the BODY must be doubled. The style prefix is added
    # after escaping, so its '#[' stays a real directive (single '#').
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app()._render_status_to_tmux("50% done #{x} #[bold] /a#b", "info")

    argv = _set_option_call(run)
    assert argv[:5] == ["tmux", "set-option", "-t", "ccmgr", "status-right"]
    assert argv[5] == "#[bg=colour236,fg=colour114]50%% done ##{x} ##[bold] /a##b#[default]"


def test_forces_status_redraw(monkeypatch):
    # tmux only auto-repaints the bar every status-interval seconds; a short
    # status message must trigger an immediate redraw or it never shows.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app()._render_status_to_tmux("hi", "info")

    argvs = [c.args[0] for c in run.call_args_list]
    assert ["tmux", "refresh-client", "-S"] in argvs
    # set-option must come before the refresh so the redraw shows the new value.
    assert argvs.index(_set_option_call(run)) < argvs.index(["tmux", "refresh-client", "-S"])


def test_level_styles_differ(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    app._render_status_to_tmux("boom", "error")
    assert _payload(run) == "#[bg=colour52,fg=colour231,bold]boom#[default]"

    run.reset_mock()
    app._render_status_to_tmux("careful", "warn")
    assert _payload(run) == "#[bg=colour236,fg=colour214,bold]careful#[default]"

    run.reset_mock()
    app._render_status_to_tmux("hint", "tip")
    assert _payload(run) == "#[bg=colour236,fg=colour245]hint#[default]"


def test_unknown_level_is_unstyled(monkeypatch):
    # A level with no mapping falls back to raw (escaped) text, no #[...] wrap.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app()._render_status_to_tmux("plain #x", "bogus")

    assert _payload(run) == "plain ##x"


def test_noop_when_disabled(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app(enabled=False)._render_status_to_tmux("anything", "error")

    run.assert_not_called()


def test_noop_without_session(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app(session=None)._render_status_to_tmux("anything", "error")

    run.assert_not_called()


def test_swallows_tmux_errors(monkeypatch):
    # A tmux failure must never propagate into the UI thread.
    def boom(*_a, **_k):
        raise OSError("tmux gone")
    monkeypatch.setattr("subprocess.run", boom)

    # Should not raise.
    _status_app()._render_status_to_tmux("hello", "info")


# ── preview themes: ` cycles the outer bar's colour scheme ───────────────

def test_render_follows_active_theme(monkeypatch):
    # The per-level style comes from the ACTIVE theme, not a fixed constant.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()
    app._tmux_theme_index = 3  # system blue

    app._render_status_to_tmux("hi", "info")

    assert _payload(run) == _TMUX_THEMES[3]["levels"]["info"] + "hi#[default]"


def _status_right(run):
    """The status-right value pushed by the last set-option among the calls."""
    found = None
    for c in run.call_args_list:
        argv = c.args[0]
        if argv[:2] == ["tmux", "set-option"] and argv[4] == "status-right":
            found = argv[5]
    assert found is not None, "no status-right set-option captured"
    return found


def test_preview_cycles_the_four_levels(monkeypatch):
    # Each ` press steps tip → info → warn → error, rendered at that level's
    # colour with sample text, all within theme 0.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    seen = []
    for _ in range(len(app._TMUX_PREVIEW_SAMPLES)):
        run.reset_mock()
        app._cycle_tmux_preview()
        seen.append((app._status_level, _status_right(run)))

    assert [lvl for lvl, _ in seen] == ["tip", "info", "warn", "error"]
    assert app._tmux_theme_index == 0  # stays on theme 0 for the first lap
    for lvl, payload in seen:
        assert payload.startswith(_TMUX_THEMES[0]["levels"][lvl])


def test_preview_rolls_to_next_theme_after_a_full_lap(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    for _ in range(len(app._TMUX_PREVIEW_SAMPLES)):  # exhaust theme 0's levels
        app._cycle_tmux_preview()
    assert app._tmux_theme_index == 0

    run.reset_mock()
    app._cycle_tmux_preview()  # next lap → tip again, theme rolls to 1

    assert app._status_level == "tip"
    assert app._tmux_theme_index == 1
    argvs = [c.args[0] for c in run.call_args_list]
    assert ["tmux", "set-option", "-t", "ccmgr", "status-style",
            _TMUX_THEMES[1]["bar"]] in argvs  # bar repainted for the new theme


def test_apply_theme_noop_when_disabled(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app(enabled=False)._apply_tmux_theme()

    run.assert_not_called()


def test_every_theme_is_well_formed():
    assert len(_TMUX_THEMES) == 5
    names = [t["name"] for t in _TMUX_THEMES]
    assert names[0] == "dark"                    # shipped default first
    assert len(set(names)) == len(names)         # names unique
    for t in _TMUX_THEMES:
        assert {"name", "bar", "brand", "levels"} <= t.keys()
        assert set(t["levels"]) == {"info", "warn", "error", "tip"}
        # error always keeps its OWN pill bg — never plain red text on the bar.
        assert "bg=colour" in t["levels"]["error"]
