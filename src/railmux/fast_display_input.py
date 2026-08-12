"""Terminal input decoding and pane-bounded local text selection.

This leaf module deliberately has no dependency on the SSH process lifecycle.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from urllib.parse import urlsplit

from railmux.fast_display_protocol import HistorySnapshot, MAX_CLIPBOARD_BYTES

_SGR_MOUSE_PREFIX = b"\x1b[<"
_SGR_STYLE_RE = re.compile(rb"\x1b\[[0-9;]*m")
_PAGE_UP = b"\x1b[5~"
_PAGE_DOWN = b"\x1b[6~"
_BRACKETED_PASTE_BEGIN = b"\x1b[200~"
_BRACKETED_PASTE_END = b"\x1b[201~"


@dataclass(frozen=True)
class BracketedPasteInput:
    """One opaque fragment of a terminal-owned bracketed paste.

    Fragments retain their framing bytes.  The client may therefore forward a
    paste incrementally without interpreting an embedded mouse report, Page
    key, focus event, or Railmux emergency escape as local input.
    """

    raw: bytes


@dataclass(frozen=True)
class SgrMouseEvent:
    raw: bytes
    button: int
    x: int
    y: int
    pressed: bool

    @property
    def wheel_direction(self) -> int:
        base_button = self.button & 3
        if not self.pressed or not self.button & 64 or base_button not in (0, 1):
            return 0
        return -1 if base_button == 1 else 1

    @property
    def is_hover_motion(self) -> bool:
        """Whether this is motion with no pressed mouse button."""
        return (
            self.pressed
            and bool(self.button & 32)
            and self.button & 3 == 3
            and not self.button & 64
        )

    def translated_y(self, offset: int) -> "SgrMouseEvent":
        """Translate a local projected row back into remote screen space."""
        if offset == 0:
            return self
        y = self.y + offset
        terminator = b"M" if self.pressed else b"m"
        raw = _SGR_MOUSE_PREFIX + f"{self.button};{self.x};{y}".encode() + terminator
        return replace(self, raw=raw, y=y)


def is_termux_environment(environ: Mapping[str, str] | None = None) -> bool:
    """Recognize an official Termux client without guessing from geometry."""
    values = os.environ if environ is None else environ
    if values.get("TERMUX_VERSION"):
        return True
    return values.get("PREFIX", "").rstrip("/") == "/data/data/com.termux/files/usr"


@dataclass(frozen=True)
class TouchKeyboardAction:
    """One local-only mouse-tracking transition for a Termux prompt tap."""

    handled: bool = False
    suspend_mouse: bool = False
    show_hint: bool = False


class TermuxTouchKeyboard:
    """Temporarily yield one focused agent prompt to Termux's soft keyboard."""

    def __init__(
        self,
        *,
        enabled: bool,
        timeout: float = 10.0,
        input_row_radius: int = 1,
        close_reassert_delay: float = 0.15,
    ) -> None:
        self.enabled = enabled
        self.timeout = timeout
        self.input_row_radius = input_row_radius
        self.close_reassert_delay = close_reassert_delay
        self._active = False
        self._keyboard_projected = False
        self._close_reassert_pending = False
        self._post_close_reassert_at: float | None = None
        self._deadline: float | None = None
        self._release_button: int | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def keyboard_projected(self) -> bool:
        """Whether Termux has reported the shortened keyboard viewport."""
        return self._keyboard_projected or self._close_reassert_pending

    @property
    def owns_local_focus(self) -> bool:
        """Whether a Termux keyboard handoff still owns the focus transition."""
        return self._active or self.keyboard_projected

    def consumes_focus_out(self, data: bytes) -> bool:
        """Keep Android's input-View handoff local to the Termux client."""
        return self.enabled and self.owns_local_focus and data == b"\033[O"

    @staticmethod
    def _is_plain_left_press(event: SgrMouseEvent) -> bool:
        return event.pressed and event.button == 0 and event.wheel_direction == 0

    def pointer_event(
        self,
        event: SgrMouseEvent,
        *,
        clicked_pane_id: str | None,
        cursor_pane_id: str | None,
        cursor_y: int,
        pane_frozen: bool,
        navigation_row: int | None = None,
        now: float | None = None,
    ) -> TouchKeyboardAction:
        """Consume a prompt tap and its paired release when assistance applies."""
        if self._release_button is not None:
            if not event.pressed and event.button & 3 == self._release_button:
                self._release_button = None
                return TouchKeyboardAction(handled=True)
            # Termux normally emits only the paired release after the press.
            # Leave unrelated queued reports on their ordinary routing path.
            return TouchKeyboardAction()
        if (
            not self.enabled
            or self._active
            or self._close_reassert_pending
            or pane_frozen
            or clicked_pane_id is None
            or clicked_pane_id != cursor_pane_id
            or (navigation_row is not None and event.y == navigation_row)
            or abs((event.y - 1) - cursor_y) > self.input_row_radius
            or not self._is_plain_left_press(event)
        ):
            return TouchKeyboardAction()
        # DEC cursor visibility is presentation state, not prompt authority.
        # Providers may hide the hardware cursor while retaining its exact
        # input coordinates. The verified live-agent route, matching cursor
        # route, unfrozen viewport, and bounded row distance remain the
        # fail-closed authority for yielding one Termux tap.
        requested_at = time.monotonic() if now is None else now
        # A rapid reopen supersedes the delayed ownership reassertion queued by
        # the previous close; Termux must receive this new native prompt tap.
        self._post_close_reassert_at = None
        self._active = True
        self._keyboard_projected = False
        self._deadline = requested_at + self.timeout
        self._release_button = event.button & 3
        return TouchKeyboardAction(
            handled=True,
            suspend_mouse=True,
            show_hint=True,
        )

    def observe_projection(
        self,
        projected: bool,
        *,
        now: float | None = None,
    ) -> bool:
        """Track keyboard geometry and request mouse restore once it opens."""
        if not projected and self._close_reassert_pending:
            observed_at = time.monotonic() if now is None else now
            restored = self.cancel()
            if restored:
                self._post_close_reassert_at = observed_at + self.close_reassert_delay
            return restored
        if not self._active:
            return False
        if projected:
            first_projection = not self._keyboard_projected
            self._keyboard_projected = True
            # Disabling mouse tracking after the initiating press can prevent
            # its release report from reaching us. The keyboard projection is
            # definitive evidence that gesture has ended; do not consume the
            # release of the user's first restored Railmux click instead.
            self._release_button = None
            observed_at = time.monotonic() if now is None else now
            # The keyboard is already visible, so Termux no longer needs to
            # own terminal pointer input. Restore Railmux mouse reporting now
            # instead of depending on a later, exact close-resize event.
            # Retain a bounded active state only to recognize that close.
            self._deadline = observed_at + self.timeout
            return first_projection
        if self._keyboard_projected:
            observed_at = time.monotonic() if now is None else now
            restored = self.cancel()
            if restored:
                self._post_close_reassert_at = observed_at + self.close_reassert_delay
            return restored
        return False

    def post_close_reassert_due(self, now: float | None = None) -> bool:
        """Request one delayed DEC-mode reassert after Termux finishes closing."""
        if self._post_close_reassert_at is None:
            return False
        checked_at = time.monotonic() if now is None else now
        if checked_at < self._post_close_reassert_at:
            return False
        self._post_close_reassert_at = None
        return True

    def keyboard_input(self) -> bool:
        """Restore tracking after input when no keyboard resize was observable."""
        if self._active and not self._keyboard_projected:
            return self.cancel()
        return False

    def expire(self, now: float | None = None) -> bool:
        """Clear touch assistance if open/close geometry stops reporting."""
        if not self._active or self._deadline is None:
            return False
        checked_at = time.monotonic() if now is None else now
        return (
            self.cancel(
                preserve_close_reassert=self._keyboard_projected,
            )
            if checked_at >= self._deadline
            else False
        )

    def cancel(self, *, preserve_close_reassert: bool = False) -> bool:
        """Clear local touch state and report whether tracking must resume."""
        was_active = self._active or self._close_reassert_pending
        keep_close_reassert = preserve_close_reassert and (
            self._keyboard_projected or self._close_reassert_pending
        )
        self._active = False
        self._keyboard_projected = False
        self._close_reassert_pending = keep_close_reassert
        self._post_close_reassert_at = None
        self._deadline = None
        self._release_button = None
        return was_active


class TerminalInputDecoder:
    """Split controls while keeping bracketed-paste payload completely opaque."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._pending_since: float | None = None
        self._in_bracketed_paste = False

    def _finish(
        self,
        parts: list[bytes | SgrMouseEvent | BracketedPasteInput],
    ) -> list[bytes | SgrMouseEvent | BracketedPasteInput]:
        if self._buffer:
            if self._pending_since is None:
                self._pending_since = time.monotonic()
        else:
            self._pending_since = None
        return parts

    @staticmethod
    def _append_bytes(
        parts: list[bytes | SgrMouseEvent | BracketedPasteInput], data: bytes
    ) -> None:
        if not data:
            return
        if parts and isinstance(parts[-1], bytes):
            parts[-1] += data
        else:
            parts.append(data)

    @staticmethod
    def _partial_suffix(data: bytearray, prefixes: tuple[bytes, ...]) -> int:
        keep = 0
        for prefix in prefixes:
            for size in range(1, min(len(data), len(prefix) - 1) + 1):
                if data[-size:] == prefix[:size]:
                    keep = max(keep, size)
        return keep

    def feed(self, data: bytes) -> list[bytes | SgrMouseEvent | BracketedPasteInput]:
        self._buffer.extend(data)
        parts: list[bytes | SgrMouseEvent | BracketedPasteInput] = []
        while self._buffer:
            if self._in_bracketed_paste:
                end = self._buffer.find(_BRACKETED_PASTE_END)
                if end >= 0:
                    end += len(_BRACKETED_PASTE_END)
                    parts.append(BracketedPasteInput(bytes(self._buffer[:end])))
                    del self._buffer[:end]
                    self._in_bracketed_paste = False
                    continue
                keep = self._partial_suffix(self._buffer, (_BRACKETED_PASTE_END,))
                emit = len(self._buffer) - keep
                if emit:
                    parts.append(BracketedPasteInput(bytes(self._buffer[:emit])))
                    del self._buffer[:emit]
                return self._finish(parts)

            paste_marker = self._buffer.find(_BRACKETED_PASTE_BEGIN)
            mouse_marker = self._buffer.find(_SGR_MOUSE_PREFIX)
            markers = tuple(
                marker for marker in (paste_marker, mouse_marker) if marker >= 0
            )
            if not markers:
                keep = self._partial_suffix(
                    self._buffer,
                    (
                        _SGR_MOUSE_PREFIX,
                        _PAGE_UP,
                        _PAGE_DOWN,
                        _BRACKETED_PASTE_BEGIN,
                    ),
                )
                emit = len(self._buffer) - keep
                self._append_bytes(parts, bytes(self._buffer[:emit]))
                del self._buffer[:emit]
                return self._finish(parts)

            marker = min(markers)
            if marker:
                self._append_bytes(parts, bytes(self._buffer[:marker]))
                del self._buffer[:marker]
                paste_marker -= marker
                mouse_marker -= marker
            if paste_marker == 0:
                self._in_bracketed_paste = True
                continue
            end = next(
                (
                    index
                    for index, value in enumerate(
                        self._buffer[len(_SGR_MOUSE_PREFIX) :], len(_SGR_MOUSE_PREFIX)
                    )
                    if value in (ord("M"), ord("m"))
                ),
                None,
            )
            if end is None:
                if len(self._buffer) <= 64:
                    return self._finish(parts)
                self._append_bytes(parts, bytes((self._buffer[0],)))
                del self._buffer[0]
                continue
            raw = bytes(self._buffer[: end + 1])
            del self._buffer[: end + 1]
            fields = raw[len(_SGR_MOUSE_PREFIX) : -1].split(b";")
            try:
                if len(fields) != 3:
                    raise ValueError
                button, x, y = (int(field) for field in fields)
                if not 0 <= button <= 255 or not 1 <= x <= 1000 or not 1 <= y <= 500:
                    raise ValueError
            except ValueError:
                self._append_bytes(parts, raw)
                continue
            parts.append(SgrMouseEvent(raw, button, x, y, raw[-1:] == b"M"))
        return self._finish(parts)

    def next_timeout(self, maximum: float = 0.1, delay: float = 0.02) -> float:
        if self._pending_since is None:
            return maximum
        remaining = delay - (time.monotonic() - self._pending_since)
        return max(0.0, min(maximum, remaining))

    def flush_pending(self, delay: float = 0.02) -> list[bytes | BracketedPasteInput]:
        if (
            not self._buffer
            or self._pending_since is None
            or time.monotonic() - self._pending_since < delay
            or self._in_bracketed_paste
        ):
            return []
        data = bytes(self._buffer)
        self._buffer.clear()
        self._pending_since = None
        return [data]


@dataclass(frozen=True)
class SelectionSource:
    """One immutable visible agent-pane surface eligible for local selection."""

    route: HistorySnapshot
    rows: tuple[bytes, ...]
    row_x_offset: int
    semantic_open: bool = True


SelectionSegment = tuple[int, int, bytes]


@dataclass(frozen=True)
class ClickTarget:
    """One explicit URL or remote-path click resolved from visible text."""

    kind: str
    value: str
    pane_id: str
    line: int | None = None
    column: int | None = None
    highlight_row: int | None = None
    highlight_column: int | None = None
    highlight_text: bytes = b""
    highlight_segments: tuple[SelectionSegment, ...] = ()


@dataclass(frozen=True)
class SelectionAction:
    """Pure routing result for one local text-selection pointer event."""

    handled: bool = False
    replay_events: tuple[SgrMouseEvent, ...] = ()
    repaint: bool = False
    copy_data: bytes | None = None
    open_target: ClickTarget | None = None


@lru_cache(maxsize=4096)
def _display_width(character: str) -> int:
    """Return one terminal-cell width without adding a base dependency."""
    try:
        import pyte
    except ImportError:
        if unicodedata.combining(character) or character == "\u200d":
            return 0
        return 2 if unicodedata.east_asian_width(character) in ("F", "W") else 1
    return max(0, pyte.screens.wcwidth(character))


def _plain_display_cells(line: bytes, width: int) -> tuple[str | None, ...]:
    """Decode one server-rendered SGR row into bounded display cells."""
    plain = _SGR_STYLE_RE.sub(b"", line).decode("utf-8", errors="replace")
    cells: list[str | None] = [" "] * width
    column = 0
    for character in plain:
        cell_width = _display_width(character)
        if cell_width == 0:
            previous = column - 1
            while previous >= 0 and cells[previous] is None:
                previous -= 1
            if previous >= 0:
                cells[previous] = (cells[previous] or "") + character
            continue
        if column >= width or column + cell_width > width:
            break
        cells[column] = character
        for continuation in range(1, cell_width):
            cells[column + continuation] = None
        column += cell_width
    return tuple(cells)


_CLICK_TOKEN_RE = re.compile(r"""[^\s<>"'`]+""")
_PATH_LINE_COLUMN_RE = re.compile(r"^(.*):([1-9][0-9]*):([1-9][0-9]*)$")
_PATH_LINE_RE = re.compile(r"^(.*):([1-9][0-9]*)$")
_BARE_PATH_RE = re.compile(r"^[A-Za-z0-9_.@+~-]+\.[A-Za-z][A-Za-z0-9]{0,11}$")
_HIDDEN_PATH_RE = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9_.@+~-]*$")
_PATH_NAMES = frozenset({"Dockerfile", "Makefile", "README", "LICENSE", "CHANGELOG"})
_TOKEN_LEADING = "([{"
_TOKEN_TRAILING = ".,;!?)]}"
_URL_ASCII_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "-._~:/?#[]@!$&()*+,;=%"
)
_EMBEDDED_ABSOLUTE_PATH_RE = re.compile(r"(?:^|(?<=[^A-Za-z0-9_.@+~/-]))/")


def _clicked_character_index(
    cells: tuple[str | None, ...],
    column: int,
) -> tuple[str, int] | None:
    """Return display text plus the character index owning one terminal cell."""
    if not 0 <= column < len(cells):
        return None
    owner = column
    while owner > 0 and cells[owner] is None:
        owner -= 1
    if cells[owner] is None:
        return None
    parts: list[str] = []
    character_count = 0
    clicked_index: int | None = None
    for cell_column, value in enumerate(cells):
        if value is None:
            continue
        if cell_column == owner:
            clicked_index = character_count
        parts.append(value)
        character_count += len(value)
    if clicked_index is None:
        return None
    return "".join(parts), clicked_index


def _trim_click_token(
    token: str,
    start: int,
    end: int,
) -> tuple[str, int, int]:
    while token and token[0] in _TOKEN_LEADING:
        token = token[1:]
        start += 1
    while token and token[-1] in _TOKEN_TRAILING:
        token = token[:-1]
        end -= 1
    return token, start, end


def _bounded_url_token(token: str) -> str:
    """Stop a visible URL before adjacent Unicode prose and trim punctuation."""
    end = next(
        (
            index
            for index, character in enumerate(token)
            if character not in _URL_ASCII_CHARACTERS
        ),
        len(token),
    )
    candidate = token[:end]
    while candidate and candidate[-1] in _TOKEN_TRAILING:
        candidate = candidate[:-1]
    return candidate


def _path_parts(token: str) -> tuple[str, int | None, int | None] | None:
    path = token
    line = None
    column = None
    location = _PATH_LINE_COLUMN_RE.fullmatch(path)
    if location is not None:
        path = location.group(1)
        line = min(int(location.group(2)), 2_147_483_647)
        column = min(int(location.group(3)), 2_147_483_647)
    else:
        location = _PATH_LINE_RE.fullmatch(path)
        if location is not None:
            path = location.group(1)
            line = min(int(location.group(2)), 2_147_483_647)
    if not path or len(path.encode("utf-8")) > 4096 or "\x00" in path or "://" in path:
        return None
    name = path.rsplit("/", 1)[-1]
    path_like = (
        path == "~"
        or path.startswith(("/", "./", "../", "~/"))
        or "/" in path
        or name in _PATH_NAMES
        or any(name.startswith(f"{prefix}.") for prefix in _PATH_NAMES)
        or _BARE_PATH_RE.fullmatch(name) is not None
        or _HIDDEN_PATH_RE.fullmatch(name) is not None
    )
    return (path, line, column) if path_like else None


def click_target_at(
    cells: tuple[str | None, ...],
    column: int,
    *,
    pane_id: str,
) -> ClickTarget | None:
    """Recognize a bounded HTTP(S) URL or Unix path under one display cell."""
    clicked = _clicked_character_index(cells, column)
    if clicked is None:
        return None
    text, character_index = clicked
    for match in _CLICK_TOKEN_RE.finditer(text):
        token, start, end = _trim_click_token(
            match.group(0), match.start(), match.end()
        )
        if not start <= character_index < end:
            continue
        lower = token.lower()
        url_offsets = tuple(
            offset
            for prefix in ("http://", "https://")
            if (offset := lower.find(prefix)) >= 0
        )
        if url_offsets:
            offset = min(url_offsets)
            candidate = _bounded_url_token(token[offset:])
            candidate_start = start + offset
            candidate_end = candidate_start + len(candidate)
            if not candidate_start <= character_index < candidate_end:
                return None
            parsed = urlsplit(candidate)
            if (
                parsed.scheme.lower() in ("http", "https")
                and parsed.hostname is not None
                and len(candidate.encode("utf-8")) <= 8192
            ):
                start_cell = sum(
                    _display_width(character) for character in text[:candidate_start]
                )
                return ClickTarget(
                    "url",
                    candidate,
                    pane_id,
                    highlight_column=start_cell,
                    highlight_text=candidate.encode("utf-8"),
                )
            return None
        absolute = tuple(_EMBEDDED_ABSOLUTE_PATH_RE.finditer(token))
        if absolute:
            offset = absolute[-1].start()
            if start + offset <= character_index:
                token = token[offset:]
                start += offset
        path = _path_parts(token)
        if path is not None:
            value, line, target_column = path
            start_cell = sum(_display_width(character) for character in text[:start])
            return ClickTarget(
                "path",
                value,
                pane_id,
                line,
                target_column,
                highlight_column=start_cell,
                highlight_text=token.encode("utf-8"),
            )
        return None
    return None


def _pane_cells(
    source: SelectionSource,
    *,
    start: int = 0,
    end: int | None = None,
) -> tuple[tuple[str | None, ...], ...]:
    """Decode pane-local cells from either full-screen or history rows."""
    route = source.route
    if end is None:
        end = route.height
    decode_width = source.row_x_offset + route.width
    decoded: list[tuple[str | None, ...]] = []
    for index in range(start, min(end, route.height)):
        line = source.rows[index] if index < len(source.rows) else b""
        cells = _plain_display_cells(line, decode_width)
        decoded.append(cells[source.row_x_offset : source.row_x_offset + route.width])
    return tuple(decoded)


def _rows_are_wrapped(
    rows: tuple[tuple[str | None, ...], ...],
    upper: int,
) -> bool:
    """Conservatively recognize a visual soft-wrap between adjacent rows."""
    if not 0 <= upper < len(rows) - 1 or not rows[upper]:
        return False
    return rows[upper][-1] != " " and rows[upper + 1][0] not in (" ", None)


def _row_text(cells: tuple[str | None, ...]) -> str:
    """Return plain row text while preserving ordinary cell spacing."""
    return "".join(cell or "" for cell in cells)


def _display_column(text: str, character_index: int) -> int:
    return sum(_display_width(character) for character in text[:character_index])


def _hard_wrapped_target(
    rows: tuple[tuple[str | None, ...], ...],
    row: int,
    column: int,
    *,
    route: HistorySnapshot,
) -> ClickTarget | None:
    """Join an indented path that an agent UI rendered across hard newlines.

    Codex and Claude sometimes wrap a long path as two real terminal rows,
    indenting the continuation. The first row can itself be an existing
    directory, so accepting it early opens the wrong target. Only join a
    bounded sequence when the first target is the final token on its row and
    every continuation is the sole indented token on the next row.
    """
    if not 0 <= row < len(rows) or route.width <= 0:
        return None
    best: ClickTarget | None = None
    first_candidate = max(0, row - 7)
    for first in range(first_candidate, row + 1):
        first_text = _row_text(rows[first])
        tail = re.search(r"""[^\s<>"'`]+\s*$""", first_text)
        if tail is None:
            continue
        tail_text = tail.group(0).rstrip()
        if not tail_text:
            continue
        tail_end = tail.start() + len(tail_text)
        tail_column = _display_column(first_text, tail_end - 1)
        target = click_target_at(
            rows[first],
            tail_column,
            pane_id=route.pane_id or "",
        )
        if (
            target is None
            or target.highlight_column is None
            or not target.highlight_text
        ):
            continue
        first_fragment = target.highlight_text.decode("utf-8", errors="replace")
        fragments: list[tuple[int, int, str]] = [
            (first, target.highlight_column, first_fragment)
        ]
        joined = first_fragment
        previous_end = target.highlight_column + sum(
            _display_width(character) for character in first_fragment
        )
        for following in range(first + 1, min(len(rows), first + 8)):
            following_text = _row_text(rows[following])
            continuation = re.fullmatch(
                r"""(\s{0,8})([^\s<>"'`]+)\s*""",
                following_text,
            )
            if continuation is None:
                break
            indent, fragment = continuation.groups()
            fragment, _start, _end = _trim_click_token(
                fragment,
                len(indent),
                len(indent) + len(fragment),
            )
            if (
                not fragment
                or (fragment[0] not in "._~" and not fragment[0].isalnum())
                or ("/" not in fragment and _path_parts(fragment) is None)
            ):
                break
            # A trailing slash explicitly promises a continuation. Otherwise
            # require the prior fragment to end close to the pane edge, which
            # distinguishes visual wrapping from unrelated adjacent lines.
            explicit_continuation = joined.endswith(("/", "-"))
            if not explicit_continuation and route.width - previous_end > 8:
                break
            fragment_column = _display_column(following_text, len(indent))
            fragments.append((following, fragment_column, fragment))
            joined += fragment
            previous_end = fragment_column + sum(
                _display_width(character) for character in fragment
            )
            if not first <= row <= following:
                continue
            clicked_fragment = next(
                (
                    (fragment_row, fragment_start, fragment_text)
                    for fragment_row, fragment_start, fragment_text in fragments
                    if fragment_row == row
                ),
                None,
            )
            if clicked_fragment is None:
                continue
            _fragment_row, fragment_start, fragment_text = clicked_fragment
            fragment_width = sum(
                _display_width(character) for character in fragment_text
            )
            if not fragment_start <= column < fragment_start + fragment_width:
                continue
            if target.kind == "url":
                parsed = urlsplit(joined)
                if (
                    parsed.scheme.lower() not in ("http", "https")
                    or parsed.hostname is None
                    or len(joined.encode("utf-8")) > 8192
                ):
                    continue
                value = joined
                line = None
                target_column = None
            else:
                path = _path_parts(joined)
                if path is None:
                    continue
                value, line, target_column = path
            segments = tuple(
                (
                    route.y + fragment_row,
                    route.x + fragment_start,
                    fragment_text.encode("utf-8"),
                )
                for fragment_row, fragment_start, fragment_text in fragments
            )
            candidate = ClickTarget(
                target.kind,
                value,
                route.pane_id or "",
                line,
                target_column,
                highlight_row=segments[0][0],
                highlight_column=segments[0][1],
                highlight_text=segments[0][2],
                highlight_segments=segments,
            )
            if best is None or len(candidate.value) > len(best.value):
                best = candidate
    return best


def _highlight_runs(
    target: ClickTarget,
    *,
    width: int,
    first_row: int,
    route_x: int,
    route_y: int,
) -> tuple[SelectionSegment, ...]:
    """Split one logical wrapped highlight back into physical pane rows."""
    if target.highlight_column is None or not target.highlight_text or width <= 0:
        return ()
    text = target.highlight_text.decode("utf-8", errors="replace")
    row = first_row + target.highlight_column // width
    column = target.highlight_column % width
    chunks: list[SelectionSegment] = []
    chunk_row = row
    chunk_column = column
    chunk: list[str] = []
    for character in text:
        cell_width = _display_width(character)
        if cell_width > 0 and column + cell_width > width:
            if chunk:
                chunks.append(
                    (
                        route_y + chunk_row,
                        route_x + chunk_column,
                        "".join(chunk).encode("utf-8"),
                    )
                )
            row += 1
            column = 0
            chunk_row = row
            chunk_column = 0
            chunk = []
        chunk.append(character)
        column += cell_width
        if column == width:
            chunks.append(
                (
                    route_y + chunk_row,
                    route_x + chunk_column,
                    "".join(chunk).encode("utf-8"),
                )
            )
            row += 1
            column = 0
            chunk_row = row
            chunk_column = 0
            chunk = []
    if chunk:
        chunks.append(
            (
                route_y + chunk_row,
                route_x + chunk_column,
                "".join(chunk).encode("utf-8"),
            )
        )
    return tuple(chunks)


def _target_in_rows(
    rows: tuple[tuple[str | None, ...], ...],
    row: int,
    column: int,
    *,
    route: HistorySnapshot,
) -> ClickTarget | None:
    """Resolve a semantic target across one bounded visual-wrap chain."""
    if not 0 <= row < len(rows) or route.width <= 0:
        return None
    # A full terminal row followed by a non-indented row is an authoritative
    # visual soft-wrap chain.  Do not let the more permissive agent hard-wrap
    # heuristic accept a shorter prefix first: on a three-row URL that made
    # rows one and two resolve only the first two fragments, while hovering
    # the final row happened to resolve the complete URL.
    soft_wrapped = (row > 0 and _rows_are_wrapped(rows, row - 1)) or (
        row + 1 < len(rows) and _rows_are_wrapped(rows, row)
    )
    if not soft_wrapped:
        hard_wrapped = _hard_wrapped_target(
            rows,
            row,
            column,
            route=route,
        )
        if hard_wrapped is not None:
            return hard_wrapped
    first = row
    while first > 0 and row - first < 7 and _rows_are_wrapped(rows, first - 1):
        first -= 1
    last = row
    while last + 1 < len(rows) and last - first < 7 and _rows_are_wrapped(rows, last):
        last += 1
    cells = tuple(cell for pane_row in rows[first : last + 1] for cell in pane_row)
    target = click_target_at(
        cells,
        (row - first) * route.width + column,
        pane_id=route.pane_id or "",
    )
    if target is None and (first != row or last != row):
        first = row
        target = click_target_at(
            rows[row],
            column,
            pane_id=route.pane_id or "",
        )
    if target is None:
        return None
    segments = _highlight_runs(
        target,
        width=route.width,
        first_row=first,
        route_x=route.x,
        route_y=route.y,
    )
    if not segments:
        return None
    first_segment = segments[0]
    return replace(
        target,
        highlight_row=first_segment[0],
        highlight_column=first_segment[1],
        highlight_text=first_segment[2],
        highlight_segments=segments,
    )


class LocalTextSelection:
    """Own one pane-bounded, visible-screen selection for ``railmux ssh``."""

    def __init__(self) -> None:
        self._press: SgrMouseEvent | None = None
        self._route: HistorySnapshot | None = None
        self._rows: tuple[tuple[str | None, ...], ...] = ()
        self._anchor: tuple[int, int] | None = None
        self._head: tuple[int, int] | None = None
        self._active = False
        self._semantic_open = False
        self._flash: tuple[SelectionSegment, ...] = ()
        self._flash_until: float | None = None
        self._hover: tuple[SelectionSegment, ...] = ()

    @property
    def active(self) -> bool:
        return self._active

    @property
    def capturing(self) -> bool:
        return self._press is not None

    def cancel(self) -> bool:
        was_active = self._active
        self._press = None
        self._route = None
        self._rows = ()
        self._anchor = None
        self._head = None
        self._active = False
        self._semantic_open = False
        return was_active

    @staticmethod
    def _target_at(
        event: SgrMouseEvent,
        source: SelectionSource | None,
    ) -> ClickTarget | None:
        if source is None or source.route.pane_id is None:
            return None
        row = event.y - 1 - source.route.y
        column = event.x - 1 - source.route.x
        if not 0 <= row < len(source.rows):
            return None
        start = max(0, row - 7)
        end = min(source.route.height, row + 8)
        return _target_in_rows(
            _pane_cells(source, start=start, end=end),
            row - start,
            column,
            route=replace(
                source.route,
                y=source.route.y + start,
                height=end - start,
            ),
        )

    def hover(
        self,
        event: SgrMouseEvent,
        source: SelectionSource | None,
    ) -> bool:
        """Update a local-only semantic hover without forwarding motion."""
        if not event.is_hover_motion or self.capturing or self.active:
            changed = bool(self._hover)
            self._hover = ()
            return changed
        target = self._target_at(event, source)
        hover = () if target is None else target.highlight_segments
        changed = hover != self._hover
        self._hover = hover
        return changed

    def flash(self, target: ClickTarget, *, now: float, duration: float) -> bool:
        if (
            target.highlight_row is None
            or target.highlight_column is None
            or not target.highlight_text
            or duration <= 0
        ):
            return False
        self._flash = target.highlight_segments or (
            (
                target.highlight_row,
                target.highlight_column,
                target.highlight_text,
            ),
        )
        if any(
            row is None or column is None or not text
            for row, column, text in self._flash
        ):
            self._flash = ()
            return False
        self._flash = tuple(
            (int(row), int(column), text) for row, column, text in self._flash
        )
        self._flash_until = now + duration
        return True

    def clear_expired_flash(self, now: float) -> bool:
        if not self._flash or self._flash_until is None or now < self._flash_until:
            return False
        self._flash = ()
        self._flash_until = None
        return True

    def validate_routes(
        self,
        routes: tuple[HistorySnapshot, ...],
    ) -> bool:
        """Cancel when a refreshed route set no longer matches the capture."""
        if self._route is None:
            return False
        route = self._route
        if any(
            candidate.pane_id == route.pane_id
            and candidate.x == route.x
            and candidate.y == route.y
            and candidate.width == route.width
            and candidate.height == route.height
            for candidate in routes
        ):
            return False
        return self.cancel()

    @staticmethod
    def _is_plain_left_press(event: SgrMouseEvent) -> bool:
        return (
            event.pressed
            and event.wheel_direction == 0
            and not event.button & 32
            and event.button & 3 == 0
        )

    @staticmethod
    def _point(
        event: SgrMouseEvent,
        route: HistorySnapshot,
    ) -> tuple[int, int]:
        x = min(
            route.width - 1,
            max(0, event.x - 1 - route.x),
        )
        y = min(
            route.height - 1,
            max(0, event.y - 1 - route.y),
        )
        return x, y

    def _begin(
        self,
        event: SgrMouseEvent,
        source: SelectionSource,
    ) -> None:
        route = source.route
        self._press = event
        self._route = route
        self._rows = _pane_cells(source)
        self._anchor = self._point(event, route)
        self._head = self._anchor
        self._active = False
        self._semantic_open = source.semantic_open

    def pointer_event(
        self,
        event: SgrMouseEvent,
        source: SelectionSource | None,
    ) -> SelectionAction:
        """Capture a drag, or replay an unchanged click through normal routing."""
        if self._press is not None:
            assert self._route is not None
            if event.pressed and event.button & 32:
                head = self._point(event, self._route)
                changed = head != self._head
                self._head = head
                if head != self._anchor:
                    self._active = True
                return SelectionAction(
                    handled=True,
                    repaint=changed and self._active,
                )
            if not event.pressed:
                press = self._press
                if not self._active:
                    target = (
                        _target_in_rows(
                            self._rows,
                            self._anchor[1],
                            self._anchor[0],
                            route=self._route,
                        )
                        if (
                            self._semantic_open
                            and self._anchor is not None
                            and self._route.pane_id is not None
                        )
                        else None
                    )
                    self.cancel()
                    return SelectionAction(
                        handled=True,
                        replay_events=() if target is not None else (press, event),
                        open_target=target,
                    )
                head = self._point(event, self._route)
                changed = head != self._head
                self._head = head
                self._press = None
                return SelectionAction(
                    handled=True,
                    repaint=changed,
                    copy_data=self.selected_text(),
                )
            press = self._press
            repaint = self.cancel()
            return SelectionAction(
                replay_events=(press,),
                repaint=repaint,
            )

        if self._is_plain_left_press(event) and source is not None:
            repaint = self.cancel() or bool(self._hover)
            self._hover = ()
            self._begin(event, source)
            return SelectionAction(handled=True, repaint=repaint)

        return SelectionAction(repaint=self.cancel())

    def _ordered_points(self) -> tuple[tuple[int, int], tuple[int, int]]:
        assert self._anchor is not None and self._head is not None
        if (self._anchor[1], self._anchor[0]) <= (self._head[1], self._head[0]):
            return self._anchor, self._head
        return self._head, self._anchor

    @staticmethod
    def _row_span(
        row: int,
        start: tuple[int, int],
        end: tuple[int, int],
        width: int,
    ) -> tuple[int, int]:
        start_x = start[0] if row == start[1] else 0
        end_x = end[0] if row == end[1] else width - 1
        return start_x, end_x

    @staticmethod
    def _adjust_wide_start(
        cells: tuple[str | None, ...],
        start: int,
    ) -> int:
        while start > 0 and cells[start] is None:
            start -= 1
        return start

    def selected_text(self) -> bytes | None:
        if not self._active or self._route is None:
            return None
        start, end = self._ordered_points()
        lines: list[str] = []
        for row in range(start[1], end[1] + 1):
            cells = self._rows[row]
            start_x, end_x = self._row_span(row, start, end, self._route.width)
            start_x = self._adjust_wide_start(cells, start_x)
            text = "".join(cell or "" for cell in cells[start_x : end_x + 1]).rstrip(
                " "
            )
            lines.append(text)
        data = "\n".join(lines).encode("utf-8")
        if not data:
            return None
        if len(data) > MAX_CLIPBOARD_BYTES:
            data = (
                data[:MAX_CLIPBOARD_BYTES]
                .decode("utf-8", errors="ignore")
                .encode("utf-8")
            )
        return data or None

    def segments(self) -> tuple[SelectionSegment, ...]:
        """Return reverse-video text runs in logical screen coordinates."""
        flash = self._flash
        hover = self._hover
        if not self._active or self._route is None:
            return (*hover, *flash)
        start, end = self._ordered_points()
        segments: list[SelectionSegment] = []
        for row in range(start[1], end[1] + 1):
            cells = self._rows[row]
            start_x, end_x = self._row_span(row, start, end, self._route.width)
            start_x = self._adjust_wide_start(cells, start_x)
            text = "".join(cell or "" for cell in cells[start_x : end_x + 1])
            segments.append(
                (
                    self._route.y + row,
                    self._route.x + start_x,
                    text.encode("utf-8"),
                )
            )
        return (*segments, *hover, *flash)


def page_key_direction(data: bytes) -> int:
    """Return local-history direction for an unmodified terminal page key."""
    if data == _PAGE_UP:
        return 1
    if data == _PAGE_DOWN:
        return -1
    return 0


def split_page_key_input(data: bytes) -> tuple[bytes, ...]:
    """Split complete page keys from adjacent opaque terminal input."""
    parts: list[bytes] = []
    start = 0
    while start < len(data):
        matches: list[tuple[int, bytes]] = []
        for key in (_PAGE_UP, _PAGE_DOWN):
            position = data.find(key, start)
            while position > 0 and data[position - 1] == 0x1B:
                position = data.find(key, position + len(key))
            if position >= 0:
                matches.append((position, key))
        if not matches:
            parts.append(data[start:])
            break
        position, key = min(matches, key=lambda item: item[0])
        if position > start:
            parts.append(data[start:position])
        parts.append(key)
        start = position + len(key)
    return tuple(part for part in parts if part)
