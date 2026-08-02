from railmux.fast_display_protocol import UpdateKind
from railmux.winlocal.compositor import Compositor, TerminalPane


def test_compositor_emits_keyframe_then_bounded_patch():
    sidebar = TerminalPane(24, 8)
    primary = TerminalPane(50, 8)
    sidebar.feed(b"Projects\r\nSessions")
    primary.feed(b"agent ready")
    compositor = Compositor(80, 12)

    first = compositor.compose(sidebar, primary, status=b"railmux | ready")
    primary.feed(b"!")
    second = compositor.compose(sidebar, primary, status=b"railmux | ready")

    assert first.kind is UpdateKind.KEYFRAME
    assert len(first.rows) == 12
    assert second.kind is UpdateKind.PATCH
    assert 0 < len(second.rows) < 12
    assert all(0 <= index < 12 for index, _row in second.rows)


def test_compositor_supports_side_by_side_and_stacked_agents():
    panes = [TerminalPane(20, 6) for _ in range(3)]
    for index, pane in enumerate(panes):
        pane.feed(f"pane-{index}".encode())
    compositor = Compositor(100, 30)

    side = compositor.compose(*panes, focus="secondary")
    compositor.resize(70, 20)
    stacked = compositor.compose(*panes, stacked=True, focus="primary")

    assert side.width == 100 and side.height == 30
    assert stacked.width == 70 and stacked.height == 20


def test_sidebar_is_full_width_without_agent_and_status_cannot_scroll_frame():
    sidebar = TerminalPane(20, 6)
    sidebar.feed(b"Projects")
    compositor = Compositor(40, 12)

    update = compositor.compose(sidebar, None, status=b"x" * 1000)
    rows = dict(update.rows)

    assert compositor.regions(has_primary=False, has_secondary=False)[
        "sidebar"
    ].width == 40
    assert b"Projects" in rows[0]
