"""Transparent same-host PTY relay for cross-session MSYS2 tmux clients.

Windows OpenSSH and an interactive Windows desktop can run under different
Terminal Services sessions.  MSYS2 can keep tmux's AF_UNIX control socket
reachable across that boundary while failing to transfer the later client's
terminal handle.  This module asks the already-validated tmux server to spawn
one helper in its own Windows session.  That helper owns the real tmux PTY;
the entry process forwards bytes and resize messages without rendering or
interpreting the Railmux UI.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import hmac
import os
import re
import secrets
import select
import selectors
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from railmux import restart_state, tmux_server
from railmux.provider_paths import running_in_managed_windows_wrapper


_PROTOCOL_MAGIC = b"RMUX-WPTY-1\0"
_TOKEN_BYTES = 16
_HEADER = struct.Struct(">BI")
_MAX_FRAME_BYTES = 1024 * 1024
_TYPE_INPUT = 1
_TYPE_OUTPUT = 2
_TYPE_RESIZE = 3
_TYPE_HEARTBEAT = 4
_TYPE_EXIT = 5
_TYPE_CLOSE = 6
_CONNECT_TIMEOUT = 5.0
_SEND_TIMEOUT = 5.0
_HEARTBEAT_INTERVAL = 5.0
_HEARTBEAT_TIMEOUT = 45.0
_DRAIN_TIMEOUT = 0.25
_CHILD_EXIT_GRACE = 0.2
_PTY_INPUT_TIMEOUT = 5.0
_CURSOR_QUIET_INTERVAL = 0.1
_ANCHOR_LINE_HOLD_INTERVAL = _CURSOR_QUIET_INTERVAL
_SYNC_OUTPUT_BEGIN = b"\033[?2026h"
_SYNC_OUTPUT_END = b"\033[?2026l"
_STALE_ENDPOINT_AGE = 5 * 60
_MAX_STALE_ENDPOINTS = 64
_RELAY_NAME = re.compile(r"windows-attach-[0-9a-f]{16}\.sock\Z")


class WindowsAttachRelayError(RuntimeError):
    """A bounded relay setup or transport failure."""


class _CursorVisibilityCoalescer:
    """Keep the Windows hardware cursor stable across one tmux repaint burst.

    Codex emits cursor show/hide controls outside its synchronized-output
    frames. Windows Terminal can therefore paint the cursor at several
    frame-final coordinates even though every text frame is atomic. Coalesce
    DECTCEM presentation noise, delay a requested hide until the output becomes
    quiet, and restore the provider's final visibility then.
    While the cursor remains visible, finish synchronized frames at the last
    quiet user-facing coordinate so Windows IME pre-edit does not chase the
    provider's transient Working and footer rows. Within that same bounded
    burst, defer repainting the proven anchor row until output becomes quiet.
    Input permits the next genuinely changed authoritative row through
    immediately. Input bytes whose provider row remains semantically unchanged
    do not authorize another erase underneath terminal-owned IME pre-edit.

    A Codex repaint can finish with one extra HIDE after repeatedly alternating
    HIDE/SHOW.  Treat that three-or-more transition signature as rendering
    noise and restore the most recent visible cursor anchor.  A lone HIDE (for
    example when Railmux intentionally focuses a cursorless surface) remains
    authoritative after the quiet interval. Replaying the last exact CUP used
    by SHOW also gives Windows Terminal a stable anchor for IME pre-edit text.

    The parser retains at most one bounded prompt-row candidate, not a terminal
    frame. It tracks OSC/DCS-style string boundaries solely so byte sequences
    inside opaque payload are never altered. It remains unaware of Codex,
    panes, and provider history; synchronized-output boundaries are used only
    to place an anchor correction and quiet repaint inside atomic output.
    """

    _HIDE = b"\033[?25l"
    _SHOW = b"\033[?25h"
    _BLINK_ON = b"\033[?12h"
    _BLINK_OFF = b"\033[?12l"
    _CURSOR_STYLE_RE = re.compile(rb"\033\[[0-9;]* q\Z")
    _SGR_RE = re.compile(rb"\033\[[0-9:;]*m")
    _ANCHOR_LINE_ERASES = (b"\033[K", b"\033[0K", b"\033[2K")
    _MAX_CONTROL = 256
    _MAX_ANCHOR_LINE = 64 * 1024

    def __init__(self, quiet_interval: float = _CURSOR_QUIET_INTERVAL) -> None:
        self.quiet_interval = quiet_interval
        self._pending = bytearray()
        self._state = "ground"
        self._string_allows_bel = False
        self._string_utf8_remaining = 0
        self._desired_visible = True
        self._physical_visible = True
        self._physical_cursor_blink: bool | None = None
        self._physical_cursor_style: bytes | None = None
        self._deadline: float | None = None
        self._visibility_transitions = 0
        self._burst_saw_hide = False
        self._burst_saw_show = False
        self._exact_cursor_position: bytes | None = None
        self._frame_final_cursor_position: bytes | None = None
        self._position_debt: bytes | None = None
        # Windows Terminal anchors IME pre-edit text to its logical cursor even
        # while DECTCEM hides the hardware cursor. Codex moves that cursor
        # through Working, prompt, and footer rows inside consecutive atomic
        # paints. Keep the last quiet visible prompt anchor as the final cursor
        # of each synchronized paint; changed frame content remains authoritative.
        self._stable_cursor_position: bytes | None = None
        self._in_sync_frame = False
        # Windows Terminal draws IME pre-edit text in the terminal grid.  A
        # full-screen agent can therefore erase and recreate an unchanged
        # prompt row on every animation frame even after the cursor itself is
        # stable.  Retain at most one narrowly recognized ``EL 2 + row``
        # candidate so repeated repaint bursts can settle once at the quiet
        # boundary when a following absolute CUP proves that its immediate
        # cursor side effect is irrelevant.
        self._anchor_line_candidate: bytearray | None = None
        self._anchor_line_controls = bytearray()
        self._anchor_line_row: int | None = None
        self._anchor_line_deadline: float | None = None
        self._anchor_line_position: bytes | None = None
        self._deferred_anchor_line: tuple[bytes, bytes] | None = None
        # SGR controls after a deferred row have already reached the physical
        # terminal. Replay them after the quiet repaint so the delayed row
        # cannot leave a different active rendition for later output.
        self._deferred_following_sgr = bytearray()
        self._anchor_line_passthrough_once = False
        # Remember what Railmux last allowed onto the physical prompt row.
        # Windows IME composition can produce input bytes without changing the
        # provider's committed prompt text. In that case note_input() must not
        # turn an otherwise redundant EL 2 repaint into a visible erase.
        self._visible_anchor_signature: bytes | None = None
        self._visible_anchor_render_signature: bytes | None = None

    def note_input(self, payload: bytes) -> None:
        """Permit the next semantically changed provider row after input."""
        if not payload:
            return
        # The relay cannot distinguish committed input from composition-shaped
        # letter/DEL bytes. Drop any stale deferred row and arm one guarded
        # pass: a genuinely changed provider row may publish immediately, while
        # a semantically identical row stays coalesced through the repaint burst.
        self._frame_final_cursor_position = None
        self._invalidate_anchor_line()
        self._anchor_line_passthrough_once = True

    def note_resize(self) -> None:
        """Drop coordinates expressed in the previous terminal geometry."""
        self._stable_cursor_position = None
        self._frame_final_cursor_position = None
        self._exact_cursor_position = None
        self._position_debt = None
        self._in_sync_frame = False
        self._physical_cursor_blink = None
        self._physical_cursor_style = None
        self._anchor_line_candidate = None
        self._anchor_line_controls.clear()
        self._anchor_line_row = None
        self._anchor_line_deadline = None
        self._anchor_line_position = None
        self._deferred_anchor_line = None
        self._deferred_following_sgr.clear()
        self._anchor_line_passthrough_once = False
        self._forget_visible_anchor_line()

    @staticmethod
    def _cursor_row(position: bytes | None) -> int | None:
        if position is None or not position.startswith(b"\033["):
            return None
        try:
            parameters = position[2:-1].split(b";")
            return int(parameters[0] or b"1")
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _cursor_column(position: bytes | None) -> int | None:
        if position is None or not position.startswith(b"\033["):
            return None
        try:
            parameters = position[2:-1].split(b";")
            return int(parameters[1] or b"1") if len(parameters) > 1 else 1
        except (TypeError, ValueError):
            return None

    @classmethod
    def _anchor_line_render_signature(cls, payload: bytes) -> bytes | None:
        erase = next(
            (
                candidate
                for candidate in cls._ANCHOR_LINE_ERASES
                if payload.startswith(candidate)
            ),
            None,
        )
        if erase is None:
            return None
        return payload[len(erase):]

    @classmethod
    def _anchor_line_signature(cls, payload: bytes) -> bytes | None:
        rendered = cls._anchor_line_render_signature(payload)
        return None if rendered is None else cls._SGR_RE.sub(b"", rendered)

    def _forget_visible_anchor_line(self) -> None:
        self._visible_anchor_signature = None
        self._visible_anchor_render_signature = None

    def _accept_visible_position(self, position: bytes, *, quiet: bool) -> None:
        stable_row = self._cursor_row(self._stable_cursor_position)
        candidate_row = self._cursor_row(position)
        if quiet or (stable_row is not None and candidate_row == stable_row):
            if stable_row is not None and candidate_row != stable_row:
                self._invalidate_anchor_line()
                self._forget_visible_anchor_line()
            self._stable_cursor_position = position

    @staticmethod
    def _is_absolute_cup(sequence: bytes) -> bool:
        return sequence[-1:] in {b"H", b"f"} and all(
            value in b"0123456789;" for value in sequence[2:-1]
        )

    def _can_hold_anchor_line(self, sequence: bytes) -> bool:
        return bool(
            sequence in self._ANCHOR_LINE_ERASES
            and self._in_sync_frame
            and self._deadline is not None
            and self._burst_saw_hide
            and self._exact_cursor_position is not None
            and self._cursor_row(self._exact_cursor_position)
            == self._cursor_row(self._stable_cursor_position)
            and self._cursor_column(self._exact_cursor_position) == 1
        )

    def _start_anchor_line_candidate(self, sequence: bytes, now: float) -> None:
        self._anchor_line_candidate = bytearray(sequence)
        self._anchor_line_controls.clear()
        self._anchor_line_row = self._cursor_row(self._exact_cursor_position)
        self._anchor_line_position = self._exact_cursor_position
        self._anchor_line_deadline = now + _ANCHOR_LINE_HOLD_INTERVAL

    def _finish_anchor_line_candidate(
        self,
        rendered: bytearray,
        *,
        comparable: bool,
    ) -> None:
        candidate = self._anchor_line_candidate
        row = self._anchor_line_row
        if candidate is None:
            return
        payload = bytes(candidate)
        signature = self._anchor_line_signature(payload)
        position = self._anchor_line_position
        same_visible_text = bool(
            signature is not None
            and signature == self._visible_anchor_signature
        )
        if (
            comparable
            and row is not None
            and position is not None
            and (
                not self._anchor_line_passthrough_once
                or same_visible_text
            )
        ):
            # Preserve SGR state now, but retain only the newest authoritative
            # row for a quiet-boundary repaint. Repeated Working frames cannot
            # erase Windows Terminal's inline IME pre-edit in the meantime.
            self._deferred_anchor_line = (position, payload)
            self._deferred_following_sgr.clear()
            rendered.extend(self._anchor_line_controls)
        else:
            rendered.extend(payload)
            if row is not None and position is not None and signature is not None:
                self._visible_anchor_signature = signature
                self._visible_anchor_render_signature = (
                    self._anchor_line_render_signature(payload)
                )
            self._deferred_anchor_line = None
            self._deferred_following_sgr.clear()
            self._anchor_line_passthrough_once = False
        self._anchor_line_candidate = None
        self._anchor_line_controls.clear()
        self._anchor_line_row = None
        self._anchor_line_position = None
        self._anchor_line_deadline = None

    def _invalidate_anchor_line(self) -> None:
        self._deferred_anchor_line = None
        self._deferred_following_sgr.clear()
        self._anchor_line_passthrough_once = False

    def _repay_position_debt(self, rendered: bytearray) -> None:
        if self._position_debt is None:
            return
        rendered.extend(self._position_debt)
        self._exact_cursor_position = self._position_debt
        self._position_debt = None

    def _invalidate_cursor_position(self, value: int) -> None:
        # Printable bytes and the C0 controls that move the cursor make a prior
        # absolute CUP unsuitable as an IME anchor.  BEL and other non-moving
        # controls do not.
        if value >= 0x20 or value in {0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D}:
            self._exact_cursor_position = None
            self._frame_final_cursor_position = None

    def _observe_csi_cursor_position(self, sequence: bytes) -> None:
        final = sequence[-1:]
        parameters = sequence[2:-1]
        if final in {b"H", b"f"} and all(
            value in b"0123456789;" for value in parameters
        ):
            self._exact_cursor_position = sequence
            self._frame_final_cursor_position = None
            return
        if final not in {b"J", b"K", b"S", b"T", b"m", b"q"}:
            # Keep an anchor only across CSI operations known not to move the
            # hardware cursor. Unknown/private operations fail safely by using
            # the terminal's eventual position rather than replaying stale CUP.
            self._exact_cursor_position = None
            self._frame_final_cursor_position = None
            self._stable_cursor_position = None
            self._invalidate_anchor_line()
            self._forget_visible_anchor_line()

    def feed(self, data: bytes, now: float) -> bytes:
        if not data:
            return b""
        rendered = bytearray()
        for value in data:
            if self._state == "ground":
                if value == 0x1B:
                    self._pending.append(value)
                    self._state = "escape"
                else:
                    if self._anchor_line_candidate is not None:
                        if value >= 0x20:
                            self._anchor_line_candidate.append(value)
                            self._invalidate_cursor_position(value)
                            if (
                                len(self._anchor_line_candidate)
                                > self._MAX_ANCHOR_LINE
                            ):
                                self._finish_anchor_line_candidate(
                                    rendered,
                                    comparable=False,
                                )
                            continue
                        self._finish_anchor_line_candidate(
                            rendered,
                            comparable=False,
                        )
                    if value >= 0x20 or value in {
                        0x08,
                        0x09,
                        0x0A,
                        0x0B,
                        0x0C,
                        0x0D,
                    }:
                        self._repay_position_debt(rendered)
                    rendered.append(value)
                    self._invalidate_cursor_position(value)
                continue

            if self._state == "escape":
                if value == 0x1B:
                    self._finish_anchor_line_candidate(
                        rendered,
                        comparable=False,
                    )
                    rendered.extend(self._pending)
                    self._pending[:] = bytes((value,))
                    continue
                self._pending.append(value)
                if value == ord("["):
                    self._state = "csi"
                    continue
                if value in {ord("]"), ord("P"), ord("X"), ord("^"), ord("_")}:
                    self._finish_anchor_line_candidate(
                        rendered,
                        comparable=False,
                    )
                    if value != ord("]"):
                        self._repay_position_debt(rendered)
                    rendered.extend(self._pending)
                    self._pending.clear()
                    self._state = "string"
                    self._string_allows_bel = value == ord("]")
                    self._string_utf8_remaining = 0
                    if value != ord("]"):
                        self._exact_cursor_position = None
                        self._stable_cursor_position = None
                        self._invalidate_anchor_line()
                    continue
                self._repay_position_debt(rendered)
                self._finish_anchor_line_candidate(rendered, comparable=False)
                rendered.extend(self._pending)
                self._pending.clear()
                self._exact_cursor_position = None
                self._stable_cursor_position = None
                self._invalidate_anchor_line()
                self._state = "ground"
                continue

            if self._state == "csi":
                if value == 0x1B:
                    self._finish_anchor_line_candidate(
                        rendered,
                        comparable=False,
                    )
                    rendered.extend(self._pending)
                    self._pending[:] = bytes((value,))
                    self._state = "escape"
                    continue
                self._pending.append(value)
                if 0x40 <= value <= 0x7E:
                    sequence = bytes(self._pending)
                    self._pending.clear()
                    self._state = "ground"
                    if self._anchor_line_candidate is not None:
                        if self._is_absolute_cup(sequence):
                            self._finish_anchor_line_candidate(
                                rendered,
                                comparable=True,
                            )
                        elif sequence[-1:] == b"m":
                            self._anchor_line_candidate.extend(sequence)
                            self._anchor_line_controls.extend(sequence)
                            continue
                        else:
                            self._finish_anchor_line_candidate(
                                rendered,
                                comparable=False,
                            )
                    if sequence in (self._HIDE, self._SHOW):
                        requested_visible = sequence == self._SHOW
                        if (
                            requested_visible
                            and self._deadline is None
                            and not self._physical_visible
                        ):
                            # Repay while the hardware cursor is hidden. A
                            # later authoritative SHOW must start from the
                            # provider's true coordinate, not our old anchor.
                            self._repay_position_debt(rendered)
                        visible_position = (
                            self._frame_final_cursor_position
                            or self._exact_cursor_position
                        )
                        self._frame_final_cursor_position = None
                        if requested_visible and visible_position is not None:
                            self._accept_visible_position(
                                visible_position,
                                quiet=self._deadline is None,
                            )
                        if (
                            self._deadline is None
                            and requested_visible == self._desired_visible
                            and requested_visible == self._physical_visible
                        ):
                            continue
                        self._visibility_transitions += 1
                        self._burst_saw_show |= requested_visible
                        self._burst_saw_hide |= not requested_visible
                        self._desired_visible = requested_visible
                        self._deadline = now + self.quiet_interval
                    elif sequence in (self._BLINK_ON, self._BLINK_OFF):
                        requested_blink = sequence == self._BLINK_ON
                        if requested_blink != self._physical_cursor_blink:
                            rendered.extend(sequence)
                            self._physical_cursor_blink = requested_blink
                    elif self._CURSOR_STYLE_RE.fullmatch(sequence):
                        if sequence != self._physical_cursor_style:
                            rendered.extend(sequence)
                            self._physical_cursor_style = sequence
                    elif sequence == _SYNC_OUTPUT_BEGIN:
                        rendered.extend(sequence)
                        self._in_sync_frame = True
                        self._repay_position_debt(rendered)
                    elif sequence == _SYNC_OUTPUT_END:
                        was_in_sync_frame = self._in_sync_frame
                        if was_in_sync_frame:
                            self._in_sync_frame = False
                        if (
                            was_in_sync_frame
                            and self._deadline is not None
                            and self._burst_saw_hide
                            and self._physical_visible
                            and self._stable_cursor_position is not None
                            and self._exact_cursor_position is not None
                            and self._exact_cursor_position
                            != self._stable_cursor_position
                        ):
                            # Place the logical cursor at the stable IME
                            # anchor *inside* the atomic frame. Windows
                            # Terminal never observes the intermediate
                            # Working/footer coordinates. Remember the true
                            # provider coordinate and repay it inside the next
                            # atomic frame (or before relative output).
                            self._frame_final_cursor_position = (
                                self._exact_cursor_position
                            )
                            self._position_debt = self._exact_cursor_position
                            rendered.extend(self._stable_cursor_position)
                            self._exact_cursor_position = self._stable_cursor_position
                        rendered.extend(sequence)
                    else:
                        final = sequence[-1:]
                        absolute_position = self._is_absolute_cup(sequence)
                        if absolute_position:
                            self._position_debt = None
                        elif final not in {b"S", b"T", b"m", b"q"}:
                            self._repay_position_debt(rendered)
                        if self._can_hold_anchor_line(sequence):
                            self._start_anchor_line_candidate(sequence, now)
                        else:
                            rendered.extend(sequence)
                            if (
                                final == b"m"
                                and self._deferred_anchor_line is not None
                            ):
                                self._deferred_following_sgr.extend(sequence)
                            if final in {b"J", b"S", b"T"}:
                                self._invalidate_anchor_line()
                        self._observe_csi_cursor_position(sequence)
                    continue
                if len(self._pending) > self._MAX_CONTROL:
                    self._finish_anchor_line_candidate(
                        rendered,
                        comparable=False,
                    )
                    self._repay_position_debt(rendered)
                    rendered.extend(self._pending)
                    self._pending.clear()
                    self._exact_cursor_position = None
                    self._stable_cursor_position = None
                    self._invalidate_anchor_line()
                    self._state = "csi_passthrough"
                continue

            if self._state == "csi_passthrough":
                if value == 0x1B:
                    self._pending.append(value)
                    self._state = "escape"
                    continue
                rendered.append(value)
                if 0x40 <= value <= 0x7E:
                    self._state = "ground"
                continue

            if self._state == "string":
                rendered.append(value)
                c1_st = value == 0x9C and self._string_utf8_remaining == 0
                if self._string_utf8_remaining:
                    if 0x80 <= value <= 0xBF:
                        self._string_utf8_remaining -= 1
                    else:
                        self._string_utf8_remaining = 0
                elif 0xC2 <= value <= 0xDF:
                    self._string_utf8_remaining = 1
                elif 0xE0 <= value <= 0xEF:
                    self._string_utf8_remaining = 2
                elif 0xF0 <= value <= 0xF4:
                    self._string_utf8_remaining = 3
                if c1_st or (self._string_allows_bel and value == 0x07):
                    self._state = "ground"
                elif value == 0x1B:
                    self._state = "string_escape"
                continue

            # OSC/DCS/APC/PM/SOS strings terminate only at ST (ESC \\),
            # except OSC which also accepts BEL. Bytes inside them are opaque;
            # an apparent DECTCEM sequence there is payload, not terminal state.
            rendered.append(value)
            if value == ord("\\"):
                self._state = "ground"
            elif value != 0x1B:
                self._state = "string"
        return bytes(rendered)

    def next_timeout(self, maximum: float, now: float) -> float:
        deadlines = tuple(
            deadline
            for deadline in (self._deadline, self._anchor_line_deadline)
            if deadline is not None
        )
        if not deadlines:
            return maximum
        deadline = min(deadlines)
        if now >= deadline and self._state != "ground":
            # A partial CSI/string must wait for more PTY bytes (or child
            # exit); do not busy-spin on an already-due visibility deadline.
            return maximum
        return max(0.0, min(maximum, deadline - now))

    def flush_due(self, now: float, *, force: bool = False) -> bytes:
        line_due = bool(
            self._anchor_line_candidate is not None
            and (
                force
                or (
                    self._anchor_line_deadline is not None
                    and now >= self._anchor_line_deadline
                )
            )
        )
        visibility_due = bool(
            self._deadline is not None and (force or now >= self._deadline)
        )
        if not line_due and not visibility_due and not (
            force and self._state != "ground"
        ):
            return b""
        if self._state != "ground" and not force:
            # A held row followed by a split CSI/string must remain in source
            # order until the control is complete. Do not consume either one
            # merely because the row hold deadline elapsed first.
            return b""
        rendered = bytearray()
        if line_due:
            # The held row bytes precede any partial control sequence in the
            # original stream. Preserve that ordering during forced teardown.
            self._finish_anchor_line_candidate(rendered, comparable=False)
        if self._state != "ground":
            rendered.extend(self._pending)
            self._pending.clear()
            # A terminated child can leave a partial control/string sequence.
            # Cancel or close it before terminal recovery bytes are emitted.
            rendered.extend(
                b"\033\\" if self._state in {"string", "string_escape"} else b"\030"
            )
            self._state = "ground"
        if visibility_due and self._deferred_anchor_line is not None:
            position, payload = self._deferred_anchor_line
            render_signature = self._anchor_line_render_signature(payload)
            if render_signature != self._visible_anchor_render_signature:
                restore = self._stable_cursor_position or self._exact_cursor_position
                rendered.extend(_SYNC_OUTPUT_BEGIN)
                rendered.extend(position)
                rendered.extend(payload)
                rendered.extend(self._deferred_following_sgr)
                if restore is not None:
                    rendered.extend(restore)
                    self._exact_cursor_position = restore
                rendered.extend(_SYNC_OUTPUT_END)
                self._visible_anchor_signature = self._anchor_line_signature(payload)
                self._visible_anchor_render_signature = render_signature
            self._deferred_anchor_line = None
            self._deferred_following_sgr.clear()
        if not visibility_due:
            return bytes(rendered)
        stable_visible = self._desired_visible or (
            self._visibility_transitions >= 3
            and self._burst_saw_hide
            and self._burst_saw_show
        )
        if stable_visible:
            if (
                self._physical_visible
                and self._position_debt is None
                and self._visibility_transitions >= 3
                and self._burst_saw_hide
                and self._stable_cursor_position is not None
                and self._exact_cursor_position is not None
                and self._exact_cursor_position != self._stable_cursor_position
            ):
                # A provider that paints without synchronized-output framing
                # still gets one atomic quiet-boundary correction. Preserve
                # its true position as debt so later relative output remains
                # correct.
                self._position_debt = self._exact_cursor_position
                rendered.extend(_SYNC_OUTPUT_BEGIN)
                rendered.extend(self._stable_cursor_position)
                rendered.extend(_SYNC_OUTPUT_END)
                self._exact_cursor_position = self._stable_cursor_position
            if not self._physical_visible:
                show_position = self._position_debt or self._stable_cursor_position
                if show_position is not None:
                    rendered.extend(show_position)
                    self._exact_cursor_position = show_position
                    self._position_debt = None
                rendered.extend(self._SHOW)
                self._physical_visible = True
        elif not stable_visible and self._physical_visible:
            rendered.extend(self._HIDE)
            self._physical_visible = False
        self._desired_visible = stable_visible
        self._deadline = None
        self._visibility_transitions = 0
        self._burst_saw_hide = False
        self._burst_saw_show = False
        self._frame_final_cursor_position = None
        self._invalidate_anchor_line()
        return bytes(rendered)


@dataclass(frozen=True)
class _EndpointIdentity:
    dev: int
    ino: int


class _FrameDecoder:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buffer.extend(data)
        frames: list[tuple[int, bytes]] = []
        while len(self._buffer) >= _HEADER.size:
            kind, size = _HEADER.unpack(self._buffer[: _HEADER.size])
            if size > _MAX_FRAME_BYTES:
                raise WindowsAttachRelayError("terminal bridge frame is too large")
            end = _HEADER.size + size
            if len(self._buffer) < end:
                break
            frames.append((kind, bytes(self._buffer[_HEADER.size : end])))
            del self._buffer[:end]
        return frames


def _frame(kind: int, payload: bytes = b"") -> bytes:
    if len(payload) > _MAX_FRAME_BYTES:
        raise WindowsAttachRelayError("terminal bridge frame is too large")
    return _HEADER.pack(kind, len(payload)) + payload


def _terminal_capability(value: str | None, default: str, limit: int) -> str:
    candidate = value or default
    if not 1 <= len(candidate) <= limit or any(
        ord(char) < 0x20 or ord(char) > 0x7E for char in candidate
    ):
        return default
    return candidate


def _terminal_size(fd: int) -> tuple[int, int]:
    size = os.get_terminal_size(fd)
    return max(1, min(size.columns, 65535)), max(1, min(size.lines, 65535))


def _set_winsize(fd: int, width: int, height: int) -> None:
    fcntl.ioctl(
        fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", height, width, 0, 0),
    )


def _endpoint_identity(path: Path) -> _EndpointIdentity | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        return None
    return _EndpointIdentity(info.st_dev, info.st_ino)


def _unlink_owned_endpoint(path: Path, identity: _EndpointIdentity | None) -> None:
    if identity is None or _endpoint_identity(path) != identity:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _validate_endpoint(path: Path) -> bool:
    try:
        root = restart_state.runtime_state_dir()
        parent = path.parent.lstat()
    except OSError:
        return False
    return bool(
        path.parent == root
        and _RELAY_NAME.fullmatch(path.name)
        and stat.S_ISDIR(parent.st_mode)
        and parent.st_uid == os.getuid()
        and not parent.st_mode & 0o022
        and _endpoint_identity(path) is not None
    )


def _cleanup_stale_endpoints(root: Path) -> None:
    """Remove only old, same-owner relay sockets with no live listener."""
    try:
        entries = root.iterdir()
    except OSError:
        return
    now = time.time()
    matched = 0
    for path in entries:
        if _RELAY_NAME.fullmatch(path.name) is None:
            continue
        matched += 1
        if matched > _MAX_STALE_ENDPOINTS:
            break
        identity = _endpoint_identity(path)
        if identity is None:
            continue
        try:
            if now - path.lstat().st_mtime < _STALE_ENDPOINT_AGE:
                continue
        except OSError:
            continue
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.1)
        try:
            probe.connect(str(path))
        except OSError:
            _unlink_owned_endpoint(path, identity)
        finally:
            probe.close()


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        data = connection.recv(remaining)
        if not data:
            raise WindowsAttachRelayError("terminal bridge closed during handshake")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _peer_is_same_user(connection: socket.socket) -> bool:
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        # MSYS2 releases without SO_PEERCRED still retain a same-owner,
        # non-writable runtime directory plus an unguessable handshake token.
        return True
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, option, 12)
        _pid, uid, _gid = struct.unpack("3i", raw)
    except OSError as exc:
        if exc.errno in {
            errno.EINVAL,
            errno.ENOPROTOOPT,
            getattr(errno, "ENOTSUP", errno.EINVAL),
        }:
            return True
        return False
    except struct.error:
        return False
    return uid == os.getuid()


def _challenge_response(token: bytes, challenge: bytes) -> bytes:
    return hmac.new(token, challenge, hashlib.sha256).digest()


def _normalized_wait_status(status: int) -> int:
    result = os.waitstatus_to_exitcode(status)
    return 128 - result if result < 0 else result


def _spawn_tmux_client(
    target: tmux_server.TmuxServerTarget,
    session_id: str,
    *,
    tmux_path: str,
    width: int,
    height: int,
    term: str,
    colorterm: str | None,
    synchronized_output: bool,
) -> tuple[int, int]:
    master_fd, slave_fd = os.openpty()
    _set_winsize(slave_fd, width, height)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - exercised by real MSYS2/PTY tests
        try:
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            for target_fd in (0, 1, 2):
                os.dup2(slave_fd, target_fd)
            if slave_fd > 2:
                os.close(slave_fd)
            env = os.environ.copy()
            env.pop("TMUX", None)
            env.pop("TMUX_PANE", None)
            env["TERM"] = term
            if colorterm:
                env["COLORTERM"] = colorterm
            else:
                env.pop("COLORTERM", None)
            argv = tmux_server.target_argv(
                target,
                *tmux_server.client_feature_args(
                    ("sync",) if synchronized_output else ()
                ),
                "attach-session",
                "-t",
                session_id,
            )
            argv[0] = tmux_path
            os.execve(tmux_path, argv, env)
        except BaseException:
            os._exit(127)
    os.close(slave_fd)
    os.set_blocking(master_fd, False)
    return pid, master_fd


def _spawn_local_pty_process(
    argv: Sequence[str],
    environ: Mapping[str, str],
    *,
    width: int,
    height: int,
    suppress_stderr: bool = False,
) -> tuple[int, int]:
    """Run one ordinary tmux client behind a same-session private PTY."""
    if not argv or not argv[0]:
        raise WindowsAttachRelayError("tmux client command is unavailable")
    master_fd, slave_fd = os.openpty()
    _set_winsize(slave_fd, width, height)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - exercised by real managed Windows tests
        try:
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            for target_fd in (0, 1):
                os.dup2(slave_fd, target_fd)
            if suppress_stderr:
                null_fd = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(null_fd, 2)
                finally:
                    if null_fd > 2:
                        os.close(null_fd)
            else:
                os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            os.execvpe(argv[0], list(argv), dict(environ))
        except BaseException:
            os._exit(127)
    os.close(slave_fd)
    os.set_blocking(master_fd, False)
    return pid, master_fd


def _child_status(pid: int) -> int | None:
    try:
        waited, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return 127
    if waited == 0:
        return None
    return _normalized_wait_status(status)


def _stop_child(pid: int) -> int:
    status = _child_status(pid)
    if status is not None:
        return status
    grace_deadline = time.monotonic() + _CHILD_EXIT_GRACE
    while time.monotonic() < grace_deadline:
        status = _child_status(pid)
        if status is not None:
            return status
        time.sleep(0.01)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        status = _child_status(pid)
        if status is not None:
            return status
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        _waited, raw = os.waitpid(pid, 0)
    except ChildProcessError:
        return 127
    return _normalized_wait_status(raw)


def _write_pty_input(master_fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    deadline = time.monotonic() + _PTY_INPUT_TIMEOUT
    while view:
        try:
            written = os.write(master_fd, view)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise WindowsAttachRelayError("terminal bridge input remained blocked")
            time.sleep(0.005)
            continue
        if written <= 0:
            raise WindowsAttachRelayError("terminal bridge could not forward input")
        view = view[written:]


def _write_terminal_output(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise WindowsAttachRelayError("terminal proxy could not forward output")
        view = view[written:]


def _drain_pty_output(
    master_fd: int,
    connection: socket.socket,
) -> None:
    """Forward tmux's bounded terminal-restore tail after client exit."""
    deadline = time.monotonic() + _DRAIN_TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        readable, _writable, _exceptional = select.select(
            [master_fd], [], [], remaining
        )
        if not readable:
            return
        try:
            data = os.read(master_fd, 65536)
        except BlockingIOError:
            continue
        except OSError as exc:
            if exc.errno == errno.EIO:
                return
            raise
        if not data:
            return
        connection.sendall(_frame(_TYPE_OUTPUT, data))


def _relay_server_loop(
    connection: socket.socket,
    *,
    target: tmux_server.TmuxServerTarget,
    session_id: str,
    tmux_path: str,
    width: int,
    height: int,
    term: str,
    colorterm: str | None,
    synchronized_output: bool,
) -> int:
    pid, master_fd = _spawn_tmux_client(
        target,
        session_id,
        tmux_path=tmux_path,
        width=width,
        height=height,
        term=term,
        colorterm=colorterm,
        synchronized_output=synchronized_output,
    )
    decoder = _FrameDecoder()
    selector = selectors.DefaultSelector()
    selector.register(connection, selectors.EVENT_READ, "client")
    selector.register(master_fd, selectors.EVENT_READ, "tmux")
    last_heartbeat = time.monotonic()
    status: int | None = None
    try:
        while status is None:
            now = time.monotonic()
            if now - last_heartbeat > _HEARTBEAT_TIMEOUT:
                break
            for key, _events in selector.select(timeout=0.25):
                if key.data == "tmux":
                    try:
                        data = os.read(master_fd, 65536)
                    except BlockingIOError:
                        continue
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        data = b""
                    if not data:
                        status = _child_status(pid)
                        break
                    connection.sendall(_frame(_TYPE_OUTPUT, data))
                    continue

                data = connection.recv(65536)
                if not data:
                    break
                for kind, payload in decoder.feed(data):
                    if kind == _TYPE_INPUT:
                        _write_pty_input(master_fd, payload)
                    elif kind == _TYPE_RESIZE and len(payload) == 4:
                        new_width, new_height = struct.unpack(">HH", payload)
                        if new_width and new_height:
                            _set_winsize(master_fd, new_width, new_height)
                            try:
                                os.killpg(pid, signal.SIGWINCH)
                            except ProcessLookupError:
                                pass
                    elif kind == _TYPE_HEARTBEAT:
                        last_heartbeat = time.monotonic()
                    elif kind == _TYPE_CLOSE:
                        status = _stop_child(pid)
                        break
                    else:
                        raise WindowsAttachRelayError(
                            "terminal bridge received an invalid client frame"
                        )
            else:
                if status is None:
                    status = _child_status(pid)
                continue
            # A socket EOF uses the loop-breaking path above.
            break
    finally:
        selector.close()
        if status is None:
            status = _stop_child(pid)
        try:
            _drain_pty_output(master_fd, connection)
        except OSError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
    try:
        connection.sendall(_frame(_TYPE_EXIT, struct.pack(">i", status)))
    except OSError:
        pass
    return status


class _SilentArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _relay_server_main(argv: Sequence[str]) -> int:
    parser = _SilentArgumentParser(add_help=False)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--socket-path", required=True)
    parser.add_argument("--tmux-path", required=True)
    parser.add_argument("--server-pid", required=True, type=int)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--width", required=True, type=int)
    parser.add_argument("--height", required=True, type=int)
    parser.add_argument("--term", required=True)
    parser.add_argument("--colorterm", default="")
    parser.add_argument("--synchronized-output", action="store_true")
    try:
        args = parser.parse_args(list(argv))
    except (SystemExit, ValueError):
        return 2
    # A detached server can predate the current preview app layer. The helper
    # executable is absolute and belongs to the current layer; replace only
    # these two marker hints, then independently verify both on-disk markers.
    os.environ["RAILMUX_MSYS2_RUNTIME_ID"] = args.runtime_id
    os.environ["RAILMUX_MSYS2_APP_ID"] = args.app_id
    if not running_in_managed_windows_wrapper():
        return 2
    try:
        token = bytes.fromhex(args.token)
    except ValueError:
        return 2
    endpoint = Path(args.endpoint)
    if (
        len(token) != _TOKEN_BYTES
        or _validated_label(args.label) is None
        or args.server_pid <= 0
        or not os.path.isabs(args.socket_path)
        or not os.path.isabs(args.tmux_path)
        or not os.access(args.tmux_path, os.X_OK)
        or not args.session_id.startswith("$")
        or not args.session_id[1:].isdigit()
        or not 1 <= args.width <= 65535
        or not 1 <= args.height <= 65535
        or not 1 <= len(args.term) <= 128
        or any(ord(char) < 0x20 or ord(char) > 0x7E for char in args.term)
        or len(args.colorterm) > 64
        or not _validate_endpoint(endpoint)
    ):
        return 2
    os.environ[tmux_server.SOCKET_LABEL_ENV] = args.label
    target = tmux_server.TmuxServerTarget(args.socket_path, args.server_pid)
    if not tmux_server.target_is_live(
        target, timeout=1.0
    ) or not tmux_server.target_has_session(target, args.session_id, timeout=1.0):
        return 2
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(_CONNECT_TIMEOUT)
    try:
        connection.connect(str(endpoint))
        connection.sendall(_PROTOCOL_MAGIC)
        challenge = _recv_exact(connection, hashlib.sha256().digest_size)
        connection.sendall(_challenge_response(token, challenge))
        connection.settimeout(_SEND_TIMEOUT)
        return _relay_server_loop(
            connection,
            target=target,
            session_id=args.session_id,
            tmux_path=args.tmux_path,
            width=args.width,
            height=args.height,
            term=args.term,
            colorterm=args.colorterm or None,
            synchronized_output=args.synchronized_output,
        )
    except (OSError, WindowsAttachRelayError):
        return 2
    finally:
        connection.close()


def relay_server_main(argv: Sequence[str]) -> int:
    try:
        return _relay_server_main(argv)
    except BaseException:
        # The invoking run-shell job redirects output as a second boundary;
        # never let an internal traceback disturb the live Railmux pane.
        return 2


class RelayClient:
    """Process-like terminal bridge used by the existing launcher watchdog."""

    def __init__(
        self,
        connection: socket.socket,
        listener: socket.socket,
        endpoint: Path,
        identity: _EndpointIdentity,
        *,
        stdin_fd: int,
        stdout_fd: int,
        stabilize_cursor: bool = False,
    ) -> None:
        self.connection = connection
        self.listener = listener
        self.endpoint = endpoint
        self.identity = identity
        self.stdin_fd = stdin_fd
        self.stdout_fd = stdout_fd
        self.returncode: int | None = None
        self._decoder = _FrameDecoder()
        self._selector = selectors.DefaultSelector()
        self._selector.register(connection, selectors.EVENT_READ, "relay")
        self._selector.register(stdin_fd, selectors.EVENT_READ, "terminal")
        self._size = _terminal_size(stdin_fd)
        self._next_heartbeat = time.monotonic() + _HEARTBEAT_INTERVAL
        self._cursor = _CursorVisibilityCoalescer() if stabilize_cursor else None

    def poll(self) -> int | None:
        return self.returncode

    def pump(self, timeout: float) -> None:
        if self.returncode is not None:
            return
        now = time.monotonic()
        size = _terminal_size(self.stdin_fd)
        if size != self._size:
            self.connection.sendall(_frame(_TYPE_RESIZE, struct.pack(">HH", *size)))
            self._size = size
            if self._cursor is not None:
                self._cursor.note_resize()
        if now >= self._next_heartbeat:
            self.connection.sendall(_frame(_TYPE_HEARTBEAT))
            self._next_heartbeat = now + _HEARTBEAT_INTERVAL
        wait = max(0.0, timeout)
        if self._cursor is not None:
            wait = self._cursor.next_timeout(wait, now)
        for key, _events in self._selector.select(timeout=wait):
            if key.data == "terminal":
                data = os.read(self.stdin_fd, 65536)
                if not data:
                    self._selector.unregister(self.stdin_fd)
                    self.connection.sendall(_frame(_TYPE_CLOSE))
                    continue
                if self._cursor is not None:
                    self._cursor.note_input(data)
                self.connection.sendall(_frame(_TYPE_INPUT, data))
                continue
            data = self.connection.recv(65536)
            if not data:
                raise WindowsAttachRelayError(
                    "terminal bridge connection ended unexpectedly"
                )
            for kind, payload in self._decoder.feed(data):
                if kind == _TYPE_OUTPUT:
                    if self._cursor is not None:
                        payload = self._cursor.feed(payload, time.monotonic())
                    view = memoryview(payload)
                    while view:
                        written = os.write(self.stdout_fd, view)
                        view = view[written:]
                elif kind == _TYPE_EXIT and len(payload) == 4:
                    self.returncode = struct.unpack(">i", payload)[0]
                else:
                    raise WindowsAttachRelayError(
                        "terminal bridge received an invalid relay frame"
                    )
        if self._cursor is not None:
            rendered = self._cursor.flush_due(time.monotonic())
            if rendered:
                _write_terminal_output(self.stdout_fd, rendered)

    def terminate(self) -> None:
        if self.returncode is not None:
            return
        try:
            self.connection.sendall(_frame(_TYPE_CLOSE))
        except OSError:
            pass
        self.returncode = 143

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.returncode is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("windows terminal bridge", timeout)
            self.pump(0.05)
        return self.returncode

    def close(self) -> None:
        if self._cursor is not None:
            rendered = self._cursor.flush_due(time.monotonic(), force=True)
            if not self._cursor._physical_visible:
                rendered += self._cursor._SHOW
                self._cursor._physical_visible = True
            if rendered:
                try:
                    _write_terminal_output(self.stdout_fd, rendered)
                except OSError:
                    pass
        self._selector.close()
        self.connection.close()
        self.listener.close()
        _unlink_owned_endpoint(self.endpoint, self.identity)


class LocalPtyClient:
    """Process-like same-session tmux proxy with cursor burst coalescing."""

    def __init__(
        self,
        pid: int,
        master_fd: int,
        *,
        stdin_fd: int,
        stdout_fd: int,
    ) -> None:
        self.pid = pid
        self.master_fd = master_fd
        self.stdin_fd = stdin_fd
        self.stdout_fd = stdout_fd
        self.returncode: int | None = None
        self._selector = selectors.DefaultSelector()
        self._selector.register(master_fd, selectors.EVENT_READ, "tmux")
        self._selector.register(stdin_fd, selectors.EVENT_READ, "terminal")
        self._size = _terminal_size(stdin_fd)
        self._cursor = _CursorVisibilityCoalescer()
        self._closed = False

    def poll(self) -> int | None:
        return self.returncode

    def _resize_if_needed(self) -> None:
        size = _terminal_size(self.stdin_fd)
        if size == self._size:
            return
        _set_winsize(self.master_fd, *size)
        self._size = size
        self._cursor.note_resize()
        try:
            os.killpg(self.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass

    def _forward_tmux_output(self, data: bytes, now: float) -> None:
        rendered = self._cursor.feed(data, now)
        if rendered:
            _write_terminal_output(self.stdout_fd, rendered)

    def _flush_cursor(self, now: float, *, force: bool = False) -> None:
        rendered = self._cursor.flush_due(now, force=force)
        if rendered:
            _write_terminal_output(self.stdout_fd, rendered)

    def _drain_after_exit(self) -> None:
        deadline = time.monotonic() + _DRAIN_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            readable, _writable, _exceptional = select.select(
                [self.master_fd], [], [], remaining
            )
            if not readable:
                return
            try:
                data = os.read(self.master_fd, 65536)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno == errno.EIO:
                    return
                raise
            if not data:
                return
            self._forward_tmux_output(data, time.monotonic())

    def pump(self, timeout: float) -> None:
        if self.returncode is not None:
            return
        self._resize_if_needed()
        now = time.monotonic()
        wait = self._cursor.next_timeout(max(0.0, timeout), now)
        for key, _events in self._selector.select(timeout=wait):
            if key.data == "terminal":
                data = os.read(self.stdin_fd, 65536)
                if not data:
                    self.terminate()
                    return
                self._cursor.note_input(data)
                _write_pty_input(self.master_fd, data)
                continue
            try:
                data = os.read(self.master_fd, 65536)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                data = b""
            if data:
                self._forward_tmux_output(data, time.monotonic())
        self._flush_cursor(time.monotonic())
        self.returncode = _child_status(self.pid)
        if self.returncode is not None:
            # tmux normally writes its restore tail before exiting. Drain every
            # byte already queued on the PTY, then release any partial cursor
            # prefix and the final requested visibility.
            self._drain_after_exit()
            self._flush_cursor(time.monotonic(), force=True)

    def terminate(self) -> None:
        if self.returncode is None:
            self.returncode = _stop_child(self.pid)
        try:
            self._drain_after_exit()
            self._flush_cursor(time.monotonic(), force=True)
        except (OSError, RuntimeError):
            # Termination and reaping remain authoritative even when the
            # presentation channel that triggered cleanup is already broken.
            pass

    def kill(self) -> None:
        if self.returncode is not None:
            return
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            _waited, status = os.waitpid(self.pid, 0)
        except ChildProcessError:
            self.returncode = 127
        else:
            self.returncode = _normalized_wait_status(status)
        try:
            self._drain_after_exit()
            self._flush_cursor(time.monotonic(), force=True)
        except (OSError, RuntimeError):
            pass

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.returncode is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("windows terminal proxy", timeout)
            self.pump(0.05)
        return self.returncode

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.returncode is None:
            self.terminate()
        try:
            self._flush_cursor(time.monotonic(), force=True)
            if not self._cursor._physical_visible:
                _write_terminal_output(self.stdout_fd, self._cursor._SHOW)
                self._cursor._physical_visible = True
        finally:
            self._selector.close()
            try:
                os.close(self.master_fd)
            except OSError:
                pass


def start_local_pty_client(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    stdin_fd: int,
    stdout_fd: int,
    suppress_stderr: bool = False,
) -> LocalPtyClient:
    """Start the managed-Windows visual proxy without addressing a server."""
    if not running_in_managed_windows_wrapper(environ):
        raise WindowsAttachRelayError("terminal proxy is unavailable")
    width, height = _terminal_size(stdin_fd)
    pid, master_fd = _spawn_local_pty_process(
        argv,
        environ,
        width=width,
        height=height,
        suppress_stderr=suppress_stderr,
    )
    try:
        return LocalPtyClient(
            pid,
            master_fd,
            stdin_fd=stdin_fd,
            stdout_fd=stdout_fd,
        )
    except BaseException:
        _stop_child(pid)
        try:
            os.close(master_fd)
        except OSError:
            pass
        raise


def start_relay_client(
    *,
    target: tmux_server.TmuxServerTarget,
    session_id: str,
    environ: Mapping[str, str],
    stdin_fd: int,
    stdout_fd: int,
) -> RelayClient:
    if not running_in_managed_windows_wrapper(environ):
        raise WindowsAttachRelayError("terminal bridge is unavailable")
    if not session_id.startswith("$") or not session_id[1:].isdigit():
        raise WindowsAttachRelayError("managed Railmux session is unavailable")
    token = secrets.token_bytes(_TOKEN_BYTES)
    token_hex = token.hex()
    root = restart_state.runtime_state_dir()
    _cleanup_stale_endpoints(root)
    endpoint = root / f"windows-attach-{secrets.token_hex(8)}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity: _EndpointIdentity | None = None
    connection: socket.socket | None = None
    try:
        listener.bind(str(endpoint))
        os.chmod(endpoint, 0o600)
        identity = _endpoint_identity(endpoint)
        if identity is None:
            raise WindowsAttachRelayError("terminal bridge endpoint is not private")
        listener.listen(2)
        listener.settimeout(0.5)
        width, height = _terminal_size(stdin_fd)
        label = tmux_server.socket_label(environ)
        tmux_path = shutil.which("tmux", path=environ.get("PATH"))
        if tmux_path is None or not os.path.isabs(tmux_path):
            raise WindowsAttachRelayError("managed tmux executable is unavailable")
        term = _terminal_capability(environ.get("TERM"), "xterm-256color", 128)
        colorterm = _terminal_capability(environ.get("COLORTERM"), "", 64)
        helper = [
            sys.executable,
            "-I",
            "-m",
            "railmux",
            "_windows-attach-relay",
            "--endpoint",
            str(endpoint),
            "--token",
            token_hex,
            "--label",
            label,
            "--runtime-id",
            environ.get("RAILMUX_MSYS2_RUNTIME_ID", ""),
            "--app-id",
            environ.get("RAILMUX_MSYS2_APP_ID", ""),
            "--socket-path",
            target.socket_path,
            "--tmux-path",
            tmux_path,
            "--server-pid",
            str(target.server_pid),
            "--session-id",
            session_id,
            "--width",
            str(width),
            "--height",
            str(height),
            "--term",
            term,
            "--colorterm",
            colorterm,
        ]
        if environ.get("WT_SESSION"):
            # The helper runs in the tmux server's Terminal Services session,
            # so carry only this capability bit from the actual entry client;
            # never persist or transmit the opaque WT_SESSION identifier.
            helper.append("--synchronized-output")
        command = (
            "exec env -u PYTHONPATH "
            + " ".join(shlex.quote(argument) for argument in helper)
            + " >/dev/null 2>&1"
        )
        result = subprocess.run(
            tmux_server.target_argv(target, "run-shell", "-b", command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
            env=dict(environ),
        )
        if result.returncode != 0:
            raise WindowsAttachRelayError("tmux did not start the terminal bridge")
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                candidate, _address = listener.accept()
            except socket.timeout:
                continue
            candidate.settimeout(_CONNECT_TIMEOUT)
            try:
                hello = _recv_exact(candidate, len(_PROTOCOL_MAGIC))
                if hello != _PROTOCOL_MAGIC or not _peer_is_same_user(candidate):
                    candidate.close()
                    continue
                challenge = secrets.token_bytes(hashlib.sha256().digest_size)
                candidate.sendall(challenge)
                response = _recv_exact(candidate, hashlib.sha256().digest_size)
                if not hmac.compare_digest(
                    response, _challenge_response(token, challenge)
                ):
                    candidate.close()
                    continue
            except (OSError, WindowsAttachRelayError):
                candidate.close()
                continue
            connection = candidate
            break
        if connection is None:
            raise WindowsAttachRelayError("terminal bridge did not become ready")
        connection.settimeout(_SEND_TIMEOUT)
        connection.sendall(_frame(_TYPE_RESIZE, struct.pack(">HH", width, height)))
        return RelayClient(
            connection,
            listener,
            endpoint,
            identity,
            stdin_fd=stdin_fd,
            stdout_fd=stdout_fd,
            stabilize_cursor=bool(environ.get("WT_SESSION")),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if connection is not None:
            connection.close()
        listener.close()
        _unlink_owned_endpoint(endpoint, identity)
        raise WindowsAttachRelayError("terminal bridge setup failed") from exc
    except BaseException:
        if connection is not None:
            connection.close()
        listener.close()
        _unlink_owned_endpoint(endpoint, identity)
        raise


def _validated_label(label: str) -> str | None:
    try:
        return tmux_server.socket_label({tmux_server.SOCKET_LABEL_ENV: label})
    except tmux_server.TmuxServerError:
        return None
