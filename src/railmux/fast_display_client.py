"""Interactive latest-state prototype for the complete Railmux tmux window.

The remote helper attaches one real tmux client inside a private PTY and
coalesces its output before sending a compressed keyframe followed by changed
rows over ordinary SSH. All input except Ctrl-] is delivered to that tmux
client, so native tmux and Railmux bindings remain authoritative. Ctrl-] is
always consumed locally.
"""

from __future__ import annotations

import argparse
import os
import selectors
import shlex
import shutil
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass, replace
from typing import BinaryIO, Optional, Sequence

from railmux.fast_display_protocol import (
    HistoryBatch,
    HistorySnapshot,
    PROTOCOL_VERSION,
    RemoteExit,
    ScreenUpdate,
    ServerMessageDecoder,
    TerminalMode,
    UpdateKind,
    encode_history_prefetch,
    encode_history_request,
    encode_input,
    encode_keyframe_request,
    encode_resize,
)

LOCAL_ESCAPE = b"\x1d"  # Ctrl-]
_SGR_MOUSE_PREFIX = b"\x1b[<"
_HISTORY_SCROLL_LINES = 3
_HISTORY_PREFETCH_LINES = 300
_HISTORY_FULL_LINES = 2000
_HISTORY_PREFETCH_INTERVAL = 3.0
_HISTORY_PREFETCH_TIMEOUT = 6.0
_HISTORY_CONTENT_PANES = 8


@dataclass(frozen=True)
class AppliedScreen:
    width: int
    height: int
    cursor_x: int
    cursor_y: int
    cursor_visible: bool
    terminal_modes: TerminalMode
    rows: tuple[bytes, ...]
    changed_rows: tuple[int, ...]
    clear: bool


class ScreenModel:
    """Apply sequenced updates and reject patches without a valid base."""

    def __init__(self) -> None:
        self.sequence: int | None = None
        self.width = 0
        self.height = 0
        self.rows: list[bytes] = []

    def apply(
        self, update: ScreenUpdate, expected_size: os.terminal_size,
    ) -> AppliedScreen | None:
        if (update.width, update.height) != (
            expected_size.columns, expected_size.lines
        ):
            return None
        if update.kind is UpdateKind.KEYFRAME:
            rows = [b""] * update.height
            for index, row in update.rows:
                rows[index] = row
            self.rows = rows
            changed = tuple(range(update.height))
            clear = True
        else:
            expected_sequence = (
                None if self.sequence is None else (self.sequence + 1) & 0xFFFFFFFF
            )
            if (
                expected_sequence is None
                or update.sequence != expected_sequence
                or update.width != self.width
                or update.height != self.height
            ):
                return None
            for index, row in update.rows:
                self.rows[index] = row
            changed = tuple(index for index, _row in update.rows)
            clear = False
        self.sequence = update.sequence
        self.width = update.width
        self.height = update.height
        return AppliedScreen(
            width=update.width,
            height=update.height,
            cursor_x=update.cursor_x,
            cursor_y=update.cursor_y,
            cursor_visible=update.cursor_visible,
            terminal_modes=update.terminal_modes,
            rows=tuple(self.rows),
            changed_rows=changed,
            clear=clear,
        )


def full_repaint(screen: AppliedScreen) -> AppliedScreen:
    return AppliedScreen(
        width=screen.width,
        height=screen.height,
        cursor_x=screen.cursor_x,
        cursor_y=screen.cursor_y,
        cursor_visible=screen.cursor_visible,
        terminal_modes=screen.terminal_modes,
        rows=screen.rows,
        changed_rows=tuple(range(screen.height)),
        clear=True,
    )


class ProbeError(RuntimeError):
    """A bounded, user-facing prototype failure."""


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


class TerminalInputDecoder:
    """Split bounded SGR mouse reports from otherwise opaque terminal bytes."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._pending_since: float | None = None

    def _finish(
        self, parts: list[bytes | SgrMouseEvent],
    ) -> list[bytes | SgrMouseEvent]:
        if self._buffer:
            if self._pending_since is None:
                self._pending_since = time.monotonic()
        else:
            self._pending_since = None
        return parts

    @staticmethod
    def _append_bytes(parts: list[bytes | SgrMouseEvent], data: bytes) -> None:
        if not data:
            return
        if parts and isinstance(parts[-1], bytes):
            parts[-1] += data
        else:
            parts.append(data)

    def feed(self, data: bytes) -> list[bytes | SgrMouseEvent]:
        self._buffer.extend(data)
        parts: list[bytes | SgrMouseEvent] = []
        while self._buffer:
            marker = self._buffer.find(_SGR_MOUSE_PREFIX)
            if marker < 0:
                keep = 0
                for size in range(1, min(len(self._buffer), len(_SGR_MOUSE_PREFIX) - 1) + 1):
                    if self._buffer[-size:] == _SGR_MOUSE_PREFIX[:size]:
                        keep = size
                emit = len(self._buffer) - keep
                self._append_bytes(parts, bytes(self._buffer[:emit]))
                del self._buffer[:emit]
                return self._finish(parts)
            if marker:
                self._append_bytes(parts, bytes(self._buffer[:marker]))
                del self._buffer[:marker]
            end = next(
                (
                    index
                    for index, value in enumerate(self._buffer[len(_SGR_MOUSE_PREFIX):], len(_SGR_MOUSE_PREFIX))
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
            raw = bytes(self._buffer[:end + 1])
            del self._buffer[:end + 1]
            fields = raw[len(_SGR_MOUSE_PREFIX):-1].split(b";")
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

    def flush_pending(self, delay: float = 0.02) -> list[bytes]:
        if (
            not self._buffer
            or self._pending_since is None
            or time.monotonic() - self._pending_since < delay
        ):
            return []
        data = bytes(self._buffer)
        self._buffer.clear()
        self._pending_since = None
        return [data]


@dataclass(frozen=True)
class HistoryAction:
    protocol_frame: bytes = b""
    forwarded_input: bytes = b""
    render_history: bool = False
    restore_live: bool = False
    refresh_routes: bool = False


class LocalHistoryView:
    """Keep bounded history content separate from visible pointer routes."""

    def __init__(self) -> None:
        self.snapshot: HistorySnapshot | None = None
        self.offset = 0
        self.deep_pending_id: int | None = None
        self.deep_pending_epoch: int | None = None
        self.prefetch_pending_id: int | None = None
        self.prefetch_pending_epoch: int | None = None
        self.prefetch_started = 0.0
        self.visible_routes: tuple[HistorySnapshot, ...] = ()
        self.content_cache: dict[str, HistorySnapshot] = {}
        self.route_epoch = 1
        self._local_pointer_capture = False
        self._next_request_id = 1

    @property
    def active(self) -> bool:
        return self.snapshot is not None

    @property
    def pending(self) -> bool:
        return self.deep_pending_id is not None

    def _allocate_request_id(self) -> int:
        request_id = self._next_request_id
        self._next_request_id = (request_id + 1) & 0xFFFFFFFF
        if self._next_request_id == 0:
            self._next_request_id = 1
        return request_id

    @staticmethod
    def _contains(snapshot: HistorySnapshot, event: SgrMouseEvent) -> bool:
        pointer_x, pointer_y = event.x - 1, event.y - 1
        return (
            snapshot.x <= pointer_x < snapshot.x + snapshot.width
            and snapshot.y <= pointer_y < snapshot.y + snapshot.height
        )

    def begin_prefetch(self, now: float) -> bytes:
        if (
            self.prefetch_pending_id is not None
            and now - self.prefetch_started < _HISTORY_PREFETCH_TIMEOUT
        ):
            return b""
        request_id = self._allocate_request_id()
        self.prefetch_pending_id = request_id
        self.prefetch_pending_epoch = self.route_epoch
        self.prefetch_started = now
        return encode_history_prefetch(request_id, _HISTORY_PREFETCH_LINES)

    def accept_prefetch(self, batch: HistoryBatch) -> None:
        if (
            batch.request_id != self.prefetch_pending_id
            or self.prefetch_pending_epoch != self.route_epoch
        ):
            return
        self.prefetch_pending_id = None
        self.prefetch_pending_epoch = None
        self.prefetch_started = 0.0
        # Replacement is atomic: hidden/removed panes immediately stop being
        # pointer targets, while their bounded text may remain reusable.
        self.visible_routes = batch.snapshots
        for snapshot in batch.snapshots:
            if snapshot.pane_id is not None:
                self._remember_content(snapshot)

    def _remember_content(self, snapshot: HistorySnapshot) -> None:
        assert snapshot.pane_id is not None
        # Reinsert an existing pane to keep insertion order as recency order.
        self.content_cache.pop(snapshot.pane_id, None)
        self.content_cache[snapshot.pane_id] = snapshot
        while len(self.content_cache) > _HISTORY_CONTENT_PANES:
            del self.content_cache[next(iter(self.content_cache))]

    def invalidate_routes(self) -> bool:
        """Drop pointer authority without discarding bounded pane content."""
        was_active = self.cancel()
        self.route_epoch = (self.route_epoch + 1) & 0xFFFFFFFF
        if self.route_epoch == 0:
            self.route_epoch = 1
        self.visible_routes = ()
        self.prefetch_pending_id = None
        self.prefetch_pending_epoch = None
        self.prefetch_started = 0.0
        return was_active

    def clear_cache(self) -> None:
        self.invalidate_routes()
        self.content_cache.clear()

    def _route_at(self, event: SgrMouseEvent) -> HistorySnapshot | None:
        return next(
            (
                route
                for route in self.visible_routes
                if self._contains(route, event)
            ),
            None,
        )

    @staticmethod
    def _same_geometry(left: HistorySnapshot, right: HistorySnapshot) -> bool:
        return (
            left.pane_id == right.pane_id
            and left.x == right.x
            and left.y == right.y
            and left.width == right.width
            and left.height == right.height
        )

    def _start_history(
        self,
        route: HistorySnapshot,
        event: SgrMouseEvent,
        *,
        restore_live: bool = False,
    ) -> HistoryAction:
        assert route.pane_id is not None
        cached = self.content_cache.get(route.pane_id, route)
        if not self._same_geometry(cached, route):
            cached = route
        maximum = max(0, len(cached.lines) - cached.height)
        if maximum == 0:
            return HistoryAction(restore_live=restore_live)
        self.snapshot = cached
        self.offset = min(maximum, _HISTORY_SCROLL_LINES)
        request_id = self._allocate_request_id()
        self.deep_pending_id = request_id
        self.deep_pending_epoch = self.route_epoch
        return HistoryAction(
            protocol_frame=encode_history_request(
                request_id, event.x, event.y, _HISTORY_FULL_LINES
            ),
            render_history=True,
            restore_live=restore_live,
        )

    def wheel(self, event: SgrMouseEvent) -> HistoryAction:
        direction = event.wheel_direction
        if direction == 0:
            return HistoryAction(forwarded_input=event.raw)
        if self.active:
            assert self.snapshot is not None
            if self._contains(self.snapshot, event):
                maximum = max(0, len(self.snapshot.lines) - self.snapshot.height)
                self.offset = max(
                    0,
                    min(maximum, self.offset + direction * _HISTORY_SCROLL_LINES),
                )
                if self.offset == 0:
                    self.cancel()
                    return HistoryAction(restore_live=True)
                return HistoryAction(render_history=True)
            # A wheel over another region must never move the old pane.
            self.cancel()
            route = self._route_at(event)
            if route is None:
                return HistoryAction(
                    forwarded_input=event.raw,
                    restore_live=True,
                )
            if direction < 0:
                return HistoryAction(restore_live=True)
            return self._start_history(route, event, restore_live=True)
        route = self._route_at(event)
        if route is None:
            return HistoryAction(forwarded_input=event.raw)
        # Once a pointer is known to be over an agent pane, the local history
        # layer exclusively owns vertical wheel input. This avoids also
        # triggering tmux copy-mode or its pane scroll bindings.
        if direction < 0:
            return HistoryAction()
        return self._start_history(route, event)

    def pointer_event(self, event: SgrMouseEvent) -> HistoryAction:
        if event.wheel_direction:
            return self.wheel(event)
        if self._local_pointer_capture:
            if not event.pressed:
                self._local_pointer_capture = False
            return HistoryAction()
        if self.active:
            assert self.snapshot is not None
            if self._contains(self.snapshot, event):
                if event.pressed and not event.button & 32:
                    self._local_pointer_capture = True
                return HistoryAction()
            if self._route_at(event) is not None:
                self.cancel()
                return HistoryAction(
                    forwarded_input=event.raw,
                    restore_live=True,
                    refresh_routes=True,
                )
            self.invalidate_routes()
            return HistoryAction(
                forwarded_input=event.raw,
                restore_live=True,
                refresh_routes=True,
            )
        if event.pressed and not event.button & 32:
            if self._route_at(event) is not None:
                return HistoryAction(
                    forwarded_input=event.raw,
                    refresh_routes=True,
                )
            self.invalidate_routes()
            return HistoryAction(
                forwarded_input=event.raw,
                refresh_routes=True,
            )
        return HistoryAction(forwarded_input=event.raw)

    def accept(self, snapshot: HistorySnapshot) -> HistoryAction:
        if (
            snapshot.request_id != self.deep_pending_id
            or self.deep_pending_epoch != self.route_epoch
        ):
            return HistoryAction()
        self.deep_pending_id = None
        self.deep_pending_epoch = None
        if snapshot.pane_id is None:
            return HistoryAction()
        route = next(
            (
                route
                for route in self.visible_routes
                if route.pane_id == snapshot.pane_id
            ),
            None,
        )
        if route is None or not self._same_geometry(route, snapshot):
            return HistoryAction()
        self._remember_content(snapshot)
        if self.snapshot is None or self.snapshot.pane_id != snapshot.pane_id:
            return HistoryAction()
        maximum = max(0, len(snapshot.lines) - snapshot.height)
        if maximum == 0:
            self.snapshot = None
            self.offset = 0
            return HistoryAction(restore_live=True)
        self.snapshot = snapshot
        self.offset = min(maximum, self.offset)
        return HistoryAction(render_history=True)

    def visible_lines(self) -> tuple[bytes, ...]:
        if self.snapshot is None:
            return ()
        end = len(self.snapshot.lines) - self.offset
        start = max(0, end - self.snapshot.height)
        lines = self.snapshot.lines[start:end]
        if len(lines) < self.snapshot.height:
            lines = (b"",) * (self.snapshot.height - len(lines)) + lines
        return lines

    def cancel(self) -> bool:
        was_active = self.active
        self.snapshot = None
        self.offset = 0
        self.deep_pending_id = None
        self.deep_pending_epoch = None
        self._local_pointer_capture = False
        return was_active


def coalesce_forwarded_wheel(
    action: HistoryAction,
    event: SgrMouseEvent,
    forwarded_directions: set[int],
) -> HistoryAction:
    """Bound one read's remote vertical-wheel burst without a time heuristic."""
    direction = event.wheel_direction
    if not action.forwarded_input or direction == 0:
        return action
    if direction in forwarded_directions:
        return replace(action, forwarded_input=b"")
    forwarded_directions.add(direction)
    return action


def input_may_change_routes(data: bytes, *, routes_visible: bool) -> bool:
    """Recognize bounded Railmux layout/modal keys without taxing agent typing."""
    if b"\x1b[19~" in data or b"\x1b[20~" in data or data == b"?":
        return True
    return not routes_visible and data in (b"\x1b", b"\r", b"\n")


def split_local_escape(data: bytes) -> tuple[bytes, bool]:
    """Return bytes before Ctrl-] and whether an emergency exit was found."""
    escape_at = data.find(LOCAL_ESCAPE)
    if escape_at < 0:
        return data, False
    return data[:escape_at], True


class RawTerminal:
    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.saved: Optional[list[object]] = None

    def __enter__(self) -> "RawTerminal":
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            self.saved = None


class TerminalSurface:
    """Paint a server-rendered screen and unconditionally restore the TTY."""

    def __init__(self, stream: BinaryIO, *, mouse: bool = True) -> None:
        self.stream = stream
        self.mouse = mouse
        self.active = False
        self.terminal_modes = TerminalMode.NONE

    def start(self) -> None:
        if self.active:
            return
        controls = [b"\033[?1049h\033[2J\033[H\033[?25l"]
        if self.mouse:
            # Button-event tracking includes wheel and drag events. SGR mode
            # preserves coordinates beyond the legacy X10 limit.
            controls.append(b"\033[?1002h\033[?1006h")
        self.stream.write(b"".join(controls))
        self.stream.flush()
        self.active = True

    def _reconcile_terminal_modes(self, requested: TerminalMode) -> None:
        """Mirror only input-affecting modes explicitly carried by protocol v5."""
        disabled = self.terminal_modes & ~requested
        enabled = requested & ~self.terminal_modes
        controls: list[bytes] = []
        for mode, disable, enable in (
            (TerminalMode.BRACKETED_PASTE, b"\033[?2004l", b"\033[?2004h"),
            (TerminalMode.FOCUS_EVENTS, b"\033[?1004l", b"\033[?1004h"),
        ):
            if disabled & mode:
                controls.append(disable)
            if enabled & mode:
                controls.append(enable)
        if controls:
            self.stream.write(b"".join(controls))
            self.stream.flush()
        self.terminal_modes = requested

    def paint(self, screen: AppliedScreen) -> None:
        self.start()
        self._reconcile_terminal_modes(screen.terminal_modes)
        rendered = [b"\033[?7l"]
        if screen.clear:
            rendered.append(b"\033[0m\033[2J")
        for row_index in screen.changed_rows:
            rendered.extend((
                f"\033[{row_index + 1};1H".encode(),
                b"\033[2K",
                screen.rows[row_index],
            ))
        rendered.extend((
            b"\033[0m\033[?7h",
            f"\033[{screen.cursor_y + 1};{screen.cursor_x + 1}H".encode(),
            b"\033[?25h" if screen.cursor_visible else b"\033[?25l",
        ))
        self.stream.write(b"".join(rendered))
        self.stream.flush()

    def paint_history(
        self, snapshot: HistorySnapshot, lines: tuple[bytes, ...],
    ) -> None:
        self.start()
        rendered: list[bytes] = [b"\033[?25l"]
        for index in range(snapshot.height):
            line = lines[index] if index < len(lines) else b""
            rendered.extend((
                f"\033[{snapshot.y + index + 1};{snapshot.x + 1}H".encode(),
                f"\033[{snapshot.width}X".encode(),
                line,
            ))
        self.stream.write(b"".join(rendered))
        self.stream.flush()

    def close(self) -> None:
        if not self.active:
            return
        controls = [b"\033[0m\033[?7h\033[?25h"]
        if self.terminal_modes & TerminalMode.BRACKETED_PASTE:
            controls.append(b"\033[?2004l")
        if self.terminal_modes & TerminalMode.FOCUS_EVENTS:
            controls.append(b"\033[?1004l")
        if self.mouse:
            controls.append(b"\033[?1002l\033[?1006l")
        controls.append(b"\033[?1049l")
        self.stream.write(b"".join(controls))
        self.stream.flush()
        self.terminal_modes = TerminalMode.NONE
        self.active = False


def build_ssh_argv(
    destination: str,
    *,
    session: str,
    width: int,
    height: int,
    fps: float,
    remote_command: str,
    ssh_args: Sequence[str],
) -> list[str]:
    remote_argv = [
        remote_command, "remote-server",
        "--protocol", str(PROTOCOL_VERSION),
        "--session", session,
        "--width", str(width),
        "--height", str(height),
        "--fps", str(fps),
    ]
    command = " ".join(shlex.quote(part) for part in remote_argv)
    return ["ssh", "-T", *ssh_args, destination, command]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="railmux ssh",
        description="Display the complete Railmux window with bounded latest-state frames"
    )
    parser.add_argument("destination", help="SSH destination or configured host alias")
    parser.add_argument("--session", default="railmux")
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument(
        "--duration", type=float, default=0.0,
        help="run time in seconds; 0 runs until Ctrl-], detach, or Railmux exits",
    )
    parser.add_argument(
        "--remote-command", default="railmux",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-mouse", action="store_true",
        help="do not capture mouse events (allows ordinary terminal selection)",
    )
    parser.add_argument(
        "--ssh-arg", action="append", default=[],
        help="extra ssh argument; repeat and use --ssh-arg=VALUE",
    )
    args = parser.parse_args(argv)
    if not 1.0 <= args.fps <= 60.0:
        parser.error("--fps must be between 1 and 60")
    if args.duration < 0:
        parser.error("--duration must be non-negative")
    return args


def run(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ProbeError("stdin and stdout must both be interactive terminals")
    if shutil.which("ssh") is None:
        raise ProbeError("ssh is not installed or not on PATH")

    current_size = os.get_terminal_size(sys.stdout.fileno())
    if current_size.columns < 40 or current_size.lines < 12:
        raise ProbeError("local terminal must be at least 40x12")
    if current_size.columns > 1000 or current_size.lines > 500:
        raise ProbeError("local terminal exceeds prototype limits of 1000x500")
    argv = build_ssh_argv(
        args.destination,
        session=args.session,
        width=current_size.columns,
        height=current_size.lines,
        fps=args.fps,
        remote_command=args.remote_command,
        ssh_args=args.ssh_arg,
    )
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    surface = TerminalSurface(sys.stdout.buffer, mouse=not args.no_mouse)
    decoder = ServerMessageDecoder()
    model = ScreenModel()
    terminal_input = TerminalInputDecoder()
    history = LocalHistoryView()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout.fileno(), selectors.EVENT_READ, "remote")
    selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "local")
    started = time.monotonic()
    next_history_prefetch = started
    frames = 0
    painted_rows = 0
    wire_bytes = 0
    local_exit = False
    remote_closed = False
    awaiting_keyframe = False
    latest_screen: AppliedScreen | None = None
    route_refresh_needed = False

    def send_protocol_frame(frame: bytes) -> None:
        process.stdin.write(frame)
        process.stdin.flush()

    def apply_history_action(action: HistoryAction) -> None:
        nonlocal route_refresh_needed
        if action.restore_live and latest_screen is not None:
            surface.paint(full_repaint(latest_screen))
        if (
            action.render_history
            and history.snapshot is not None
            and latest_screen is not None
        ):
            surface.paint_history(history.snapshot, history.visible_lines())
        if action.protocol_frame:
            send_protocol_frame(action.protocol_frame)
        if action.forwarded_input:
            send_protocol_frame(encode_input(action.forwarded_input))
        if action.refresh_routes:
            route_refresh_needed = True

    def handle_terminal_part(
        part: bytes | SgrMouseEvent,
        forwarded_wheels: set[int],
    ) -> None:
        nonlocal route_refresh_needed
        if isinstance(part, SgrMouseEvent):
            # Keep a frozen viewport stable across reported clicks and drags.
            # Terminal-native selection overrides never arrive here.
            action = history.pointer_event(part)
            apply_history_action(
                coalesce_forwarded_wheel(action, part, forwarded_wheels)
            )
            return
        if not part:
            return
        may_change_routes = input_may_change_routes(
            part, routes_visible=bool(history.visible_routes)
        )
        if history.active or history.pending:
            if part == b"\x1b":
                restore = history.cancel()
                apply_history_action(HistoryAction(restore_live=restore))
                return
            if part not in (b"\x1b[I", b"\x1b[O"):
                restore = history.cancel()
                if may_change_routes:
                    history.invalidate_routes()
                    route_refresh_needed = True
                apply_history_action(HistoryAction(
                    forwarded_input=part,
                    restore_live=restore,
                ))
                return
        if may_change_routes:
            history.invalidate_routes()
            route_refresh_needed = True
        send_protocol_frame(encode_input(part))

    print(
        "railmux ssh: Ctrl-] disconnects locally; Ctrl-B d detaches; "
        f"mouse forwarding is {'off' if args.no_mouse else 'on'}",
        file=sys.stderr,
    )
    try:
        with RawTerminal(sys.stdin.fileno()):
            while True:
                if args.duration and time.monotonic() - started >= args.duration:
                    local_exit = True
                    break
                observed_size = os.get_terminal_size(sys.stdout.fileno())
                if observed_size != current_size:
                    if observed_size.columns < 40 or observed_size.lines < 12:
                        raise ProbeError("resized terminal is smaller than 40x12")
                    if observed_size.columns > 1000 or observed_size.lines > 500:
                        raise ProbeError("resized terminal exceeds prototype limits")
                    if history.active and latest_screen is not None:
                        surface.paint(full_repaint(latest_screen))
                    history.clear_cache()
                    route_refresh_needed = True
                    process.stdin.write(encode_resize(
                        observed_size.columns, observed_size.lines
                    ))
                    process.stdin.flush()
                    current_size = observed_size
                    awaiting_keyframe = True
                events = selector.select(timeout=terminal_input.next_timeout())
                for key, _mask in events:
                    if key.data == "remote":
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            remote_closed = True
                            break
                        wire_bytes += len(chunk)
                        saw_screen_update = False
                        for message in decoder.feed(chunk):
                            if isinstance(message, HistoryBatch):
                                history.accept_prefetch(message)
                                continue
                            if isinstance(message, HistorySnapshot):
                                apply_history_action(history.accept(message))
                                continue
                            update = message
                            applied = model.apply(update, current_size)
                            if applied is None:
                                if not awaiting_keyframe:
                                    process.stdin.write(encode_keyframe_request())
                                    process.stdin.flush()
                                    awaiting_keyframe = True
                                continue
                            saw_screen_update = True
                            if update.kind is UpdateKind.KEYFRAME:
                                awaiting_keyframe = False
                            latest_screen = applied
                            if not history.active:
                                surface.paint(applied)
                            frames += 1
                            painted_rows += len(applied.changed_rows)
                        if saw_screen_update and route_refresh_needed:
                            prefetch = history.begin_prefetch(time.monotonic())
                            if prefetch:
                                send_protocol_frame(prefetch)
                            if history.prefetch_pending_id is not None:
                                route_refresh_needed = False
                                next_history_prefetch = (
                                    time.monotonic() + _HISTORY_PREFETCH_INTERVAL
                                )
                    else:
                        data = os.read(sys.stdin.fileno(), 4096)
                        if not data:
                            local_exit = True
                            break
                        data, emergency_exit = split_local_escape(data)
                        if emergency_exit:
                            local_exit = True
                        forwarded_wheels: set[int] = set()
                        for part in terminal_input.feed(data):
                            handle_terminal_part(part, forwarded_wheels)
                        if local_exit:
                            break
                if not local_exit:
                    for part in terminal_input.flush_pending():
                        handle_terminal_part(part, set())
                if local_exit:
                    break
                if remote_closed:
                    break
                if process.poll() is not None and not events:
                    break
                now = time.monotonic()
                if (
                    not args.no_mouse
                    and latest_screen is not None
                    and now >= next_history_prefetch
                ):
                    prefetch = history.begin_prefetch(now)
                    if prefetch:
                        send_protocol_frame(prefetch)
                        route_refresh_needed = False
                    next_history_prefetch = now + _HISTORY_PREFETCH_INTERVAL
    except KeyboardInterrupt:
        # Raw mode normally forwards Ctrl-C. This only handles an external
        # signal and follows the conventional shell exit status.
        return 130
    except BrokenPipeError:
        remote_closed = True
    finally:
        selector.close()
        surface.close()
        if local_exit and process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    elapsed = max(0.001, time.monotonic() - started)
    print(
        f"railmux ssh: painted {frames} coalesced updates / "
        f"{painted_rows} rows in {elapsed:.1f}s; "
        f"received {wire_bytes / 1024:.1f} KiB",
        file=sys.stderr,
    )
    known_exit = {
        int(RemoteExit.DETACHED): "detached; the Railmux session is still running",
        int(RemoteExit.SOFT_QUIT): "soft-quit; agent sessions were left running",
        int(RemoteExit.HARD_QUIT): "hard-quit; the managed Railmux session ended",
    }
    if process.returncode in known_exit:
        print(f"railmux ssh: {known_exit[process.returncode]}", file=sys.stderr)
        return 0
    if frames == 0 and not local_exit:
        raise ProbeError("remote display helper exited before its first frame")
    if not local_exit and process.returncode:
        print(
            "railmux ssh: remote display failed; run 'railmux doctor' on "
            "the remote host for tmux health and the last recorded incident",
            file=sys.stderr,
        )
        return process.returncode
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
