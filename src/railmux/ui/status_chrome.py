"""Semantic bottom-chrome projection shared by non-tmux frontends."""
from __future__ import annotations

from dataclasses import dataclass

from urwid.str_util import calc_width

from railmux.ui.workspace import WorkspacePage


ACTION_MODE = "mode"
ACTION_LAYOUT = "layout"
ACTION_COPY = "copy"


@dataclass(frozen=True)
class StatusHit:
    """One clickable half-open cell range in the bottom status row."""

    start: int
    end: int
    action: str


@dataclass(frozen=True)
class StatusProjection:
    """Plain terminal text plus semantic hit targets for one status row."""

    text: str
    hits: tuple[StatusHit, ...]
    error: bool


def project_status_chrome(
    *,
    width: int,
    mode_label: str,
    layout_indicator: str | None,
    status_text: str,
    status_level: str,
    compact: bool,
    active_page: WorkspacePage,
    page_targets: tuple[str | None, str | None, str | None],
) -> StatusProjection:
    """Build the platform-neutral visible controls for a full-width bar.

    Renderers remain free to choose colours, but labels and click authority
    live here so a native frontend cannot accidentally expose only status-right.
    """
    width = max(1, width)
    parts: list[str] = []
    hits: list[StatusHit] = []
    cells = 0

    def append(value: str, action: str | None = None) -> None:
        nonlocal cells
        value = _safe_text(value)
        bounded = _fit_cells(value, max(0, width - cells))
        if not bounded:
            return
        start = cells
        parts.append(bounded)
        cells += _cells(bounded)
        if action is not None and cells > start:
            hits.append(StatusHit(start, cells, action))

    if compact:
        if width < 52:
            labels = ("R", "1", "2")
            visible_mode = (
                "Cx" if mode_label == "Codex" else
                "CC" if mode_label == "Claude Code" else mode_label[:2]
            )
        elif width < 80:
            labels = ("Railmux", "A1", "A2")
            visible_mode = (
                "Codex" if mode_label == "Codex" else
                "CC" if mode_label == "Claude Code" else mode_label[:8]
            )
        else:
            labels = ("Railmux", "Agent 1", "Agent 2")
            visible_mode = mode_label
        pages = (
            WorkspacePage.SIDEBAR,
            WorkspacePage.PRIMARY,
            WorkspacePage.SECONDARY,
        )
        for page, target, label in zip(pages, page_targets, labels):
            # Native terminals do not all expose tmux's per-range style
            # channel.  Keep the selected page visible in plain text too.
            marker = f"<{label}>" if page is active_page else f"[{label}]"
            append(marker, f"page:{target}" if target is not None else None)
        append(" ")
        append(visible_mode, ACTION_MODE)
    else:
        append(" Railmux · ")
        append(mode_label, ACTION_MODE)

    if layout_indicator:
        append(" · ")
        append(layout_indicator, ACTION_LAYOUT)
    append(" ")

    safe_status = _safe_text(status_text)
    available = max(0, width - cells)
    if safe_status and available:
        visible_status = _fit_cells(safe_status, available)
        status_cells = _cells(visible_status)
        padding = max(0, available - status_cells)
        if padding:
            parts.append(" " * padding)
            cells += padding
        start = cells
        parts.append(visible_status)
        cells += status_cells
        if cells > start:
            hits.append(StatusHit(start, cells, ACTION_COPY))

    return StatusProjection(
        text="".join(parts),
        hits=tuple(hits),
        error=status_level == "error",
    )


def _safe_text(value: str) -> str:
    return "".join(
        " " if ord(character) < 32 or 0x7F <= ord(character) <= 0x9F
        else character
        for character in value
    )


def _cells(value: str) -> int:
    return calc_width(value, 0, len(value))


def _fit_cells(value: str, available: int) -> str:
    if available <= 0:
        return ""
    result: list[str] = []
    used = 0
    for character in value:
        width = _cells(character)
        if used + width > available:
            break
        result.append(character)
        used += width
    return "".join(result)
