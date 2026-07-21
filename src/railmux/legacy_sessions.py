"""Deprecated upgrade bridge for tmux's historical default server.

New sessions must never enter this path. Remove this module after Railmux has
kept the compatibility promise for a documented upgrade window and field
reports/``railmux doctor`` no longer find legacy candidates in supported
installations. Until then it stays read-only except for an explicit, separately
revalidated user Kill.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from railmux import tmux_server


@dataclass(frozen=True)
class LegacySession:
    name: str
    cwd: Path
    created_at: int
    session_id: str
    pane_id: str
    orphan_marker: str
    binding: str
    historical_shape: bool


def discover(
    *, timeout: float = 1.0,
) -> tuple[
    tmux_server.TmuxServerTarget | None,
    tuple[LegacySession, ...],
    bool,
]:
    """Return single-pane candidates without writing to the legacy server.

    A target disappearing or changing identity is reported as an empty result;
    action-time checks still pin both its server PID and immutable session ID.
    """
    try:
        target = tmux_server.discover_legacy_target(timeout=timeout)
    except tmux_server.TmuxServerError:
        return None, (), False
    if target is None:
        return None, (), True
    fmt = (
        "#{session_name}\t#{pane_current_path}\t#{session_created}"
        "\t#{session_id}\t#{pane_id}\t#{session_windows}\t#{window_panes}"
        "\t#{@railmux_orphan_v2}\t#{@railmux_binding_v1}"
    )
    try:
        output = subprocess.check_output(
            tmux_server.target_argv(target, "list-sessions", "-F", fmt),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None, (), False
    if not tmux_server.target_is_live(target, timeout=timeout):
        return None, (), False

    records: list[LegacySession] = []
    for line in output.splitlines():
        fields = line.split("\t", 8)
        if len(fields) != 9:
            continue
        name, cwd, created, session_id, pane_id, windows, panes, marker, binding = fields
        if (not name or not cwd or not session_id.startswith("$")
                or not pane_id.startswith("%")):
            continue
        try:
            created_at = int(created)
        except ValueError:
            continue
        records.append(
            LegacySession(
                name=name,
                cwd=Path(cwd),
                created_at=max(0, created_at),
                session_id=session_id,
                pane_id=pane_id,
                orphan_marker=marker,
                binding=binding,
                historical_shape=(windows == "1" and panes == "1"),
            )
        )
    return target, tuple(records), True
