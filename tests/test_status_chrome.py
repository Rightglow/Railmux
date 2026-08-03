from railmux.ui.status_chrome import (
    ACTION_COPY,
    ACTION_LAYOUT,
    ACTION_MODE,
    project_status_chrome,
)
from railmux.ui.workspace import WorkspacePage


def test_wide_status_keeps_persistent_controls_separate_from_message():
    projection = project_status_chrome(
        width=80,
        mode_label="Codex",
        layout_indicator="◨",
        status_text="session restored",
        status_level="info",
        compact=False,
        active_page=WorkspacePage.SIDEBAR,
        page_targets=("%controller", "%1", "%2"),
    )

    assert "Railmux · Codex · ◨" in projection.text
    assert projection.text.endswith("session restored")
    assert {hit.action for hit in projection.hits} == {
        ACTION_MODE, ACTION_LAYOUT, ACTION_COPY,
    }


def test_compact_status_exposes_all_available_pages_and_controls():
    projection = project_status_chrome(
        width=40,
        mode_label="Claude Code",
        layout_indicator="⬒",
        status_text="ready",
        status_level="error",
        compact=True,
        active_page=WorkspacePage.PRIMARY,
        page_targets=("%controller", "%1", "%2"),
    )

    assert projection.text.startswith("[R][1][2] CC · ⬒")
    assert {hit.action for hit in projection.hits} >= {
        "page:%controller", "page:%1", "page:%2",
        ACTION_MODE, ACTION_LAYOUT,
    }
    assert projection.error is True


def test_status_projection_is_cell_bounded_with_cjk_text():
    projection = project_status_chrome(
        width=40,
        mode_label="Codex",
        layout_indicator=None,
        status_text="中文状态" * 20,
        status_level="info",
        compact=False,
        active_page=WorkspacePage.SIDEBAR,
        page_targets=("%controller", None, None),
    )

    assert projection.text.startswith(" Railmux · Codex ")
    assert all(0 <= hit.start < hit.end <= 40 for hit in projection.hits)
