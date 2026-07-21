#!/usr/bin/env python3
"""Bounded latest-state client prototype for a live Railmux agent pane.

The local half opens one ordinary SSH connection. A dependency-free Python
snippet on the remote host samples the latest tmux pane with ``capture-pane``
and sends only changed snapshots at a bounded frame rate. Read-only mode is the
default. Explicit interactive mode forwards bytes only to the exact Railmux
Target pane that was validated at startup; it never attaches, resizes, splits,
swaps, kills, or changes options on any tmux object.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import selectors
import struct
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass
from typing import BinaryIO, Iterator, Optional, Sequence


FRAME_MAGIC = b"RMUXF2\x00"
INPUT_MAGIC = b"RMUXI1\x00"
MAGIC = FRAME_MAGIC  # Compatibility name used by the original probe tests.
HEADER_SIZE = len(FRAME_MAGIC) + 4
FRAME_METADATA_SIZE = 8
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_INPUT_BYTES = 4096
LOCAL_ESCAPE = b"\x1d"  # Ctrl-]


class ProbeError(RuntimeError):
    """A bounded, user-facing probe failure."""


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    cursor_x: int
    cursor_y: int
    screen: bytes


class FrameDecoder:
    """Decode length-prefixed frames while tolerating SSH login noise."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer.extend(data)
        frames: list[Frame] = []
        while True:
            marker = self._buffer.find(FRAME_MAGIC)
            if marker < 0:
                keep = min(len(self._buffer), len(FRAME_MAGIC) - 1)
                if len(self._buffer) > keep:
                    del self._buffer[:-keep]
                return frames
            if marker:
                del self._buffer[:marker]
            if len(self._buffer) < HEADER_SIZE:
                return frames
            payload_size = struct.unpack(
                ">I", self._buffer[len(FRAME_MAGIC):HEADER_SIZE]
            )[0]
            if payload_size < FRAME_METADATA_SIZE or payload_size > MAX_FRAME_BYTES:
                # Treat a false marker in startup noise as noise and rescan.
                del self._buffer[0]
                continue
            packet_size = HEADER_SIZE + payload_size
            if len(self._buffer) < packet_size:
                return frames
            payload = bytes(self._buffer[HEADER_SIZE:packet_size])
            del self._buffer[:packet_size]
            width, height, cursor_x, cursor_y = struct.unpack(">HHHH", payload[:8])
            if width and height:
                frames.append(Frame(
                    width, height, cursor_x, cursor_y, payload[8:]
                ))


def encode_input(data: bytes) -> bytes:
    """Frame local terminal bytes for the remote stdin decoder."""
    if not data or len(data) > MAX_INPUT_BYTES:
        raise ValueError("input frame must contain 1 to 4096 bytes")
    return INPUT_MAGIC + struct.pack(">I", len(data)) + data


def iter_frames(stream: BinaryIO) -> Iterator[Frame]:
    decoder = FrameDecoder()
    # ``BufferedReader.read`` may wait to fill the entire requested size on a
    # live pipe. ``read1`` returns the bytes currently available, preserving
    # frame latency instead of accidentally batching many snapshots locally.
    read_available = getattr(stream, "read1", stream.read)
    while True:
        chunk = read_available(65536)
        if not chunk:
            return
        yield from decoder.feed(chunk)


# Passed as one quoted ``python3 -c`` argument, so it needs only the remote
# Python standard library. Railmux already requires Python 3.9+ remotely.
REMOTE_SCRIPT = r'''import struct
import subprocess
import sys
import time

FRAME_MAGIC = b"RMUXF2\x00"
INPUT_MAGIC = b"RMUXI1\x00"
MAX_INPUT_BYTES = 4096
TMUX = ["tmux", "-L", "railmux"]
session, requested, raw_fps, raw_duration, raw_interactive = sys.argv[1:6]
fps = float(raw_fps)
duration = float(raw_duration)
interactive = raw_interactive == "1"

def output(*args):
    return subprocess.check_output(
        [*TMUX, *args], stderr=subprocess.DEVNULL
    ).decode(errors="replace").strip()

def valid_pane(candidate):
    if not candidate.startswith("%") or not candidate[1:].isdigit():
        return None
    try:
        return output("display-message", "-p", "-t", candidate, "#{pane_id}")
    except subprocess.CalledProcessError:
        return None

def window_option(name):
    try:
        return output("show-window-options", "-v", "-t", session, name)
    except subprocess.CalledProcessError:
        return ""

def resolve_pane():
    if requested:
        pane = valid_pane(requested)
        if pane:
            return pane
        raise SystemExit("requested tmux pane is unavailable: " + requested)
    target = window_option("@railmux_target_pane")
    pane = valid_pane(target)
    if pane:
        return pane
    controller = window_option("@railmux_controller_pane")
    rows = output(
        "list-panes", "-t", session, "-F",
        "#{pane_id}\t#{pane_active}",
    ).splitlines()
    candidates = []
    for row in rows:
        pane_id, active = row.split("\t", 1)
        if pane_id != controller:
            candidates.append((active == "1", pane_id))
    if not candidates:
        raise SystemExit("no agent pane found in tmux session: " + session)
    candidates.sort(reverse=True)
    return candidates[0][1]

pane = resolve_pane()
try:
    session_id = output("display-message", "-p", "-t", session, "#{session_id}")
except subprocess.CalledProcessError:
    raise SystemExit("could not read the Railmux session identity")
if not session_id.startswith("$") or not session_id[1:].isdigit():
    raise SystemExit("invalid Railmux session identity")
controller = valid_pane(window_option("@railmux_controller_pane")) or ""

def validate_interactive_target():
    try:
        current_session_id = output(
            "display-message", "-p", "-t", session, "#{session_id}"
        )
    except subprocess.CalledProcessError:
        raise SystemExit("Railmux session disappeared; input stopped")
    if current_session_id != session_id:
        raise SystemExit("Railmux session was replaced; input stopped")
    if not controller or window_option("@railmux_controller_pane") != controller:
        raise SystemExit("Railmux controller changed; input stopped")
    if window_option("@railmux_target_pane") != pane:
        raise SystemExit("Railmux Target pane changed; reconnect the client")
    if valid_pane(pane) != pane:
        raise SystemExit("Railmux Target pane disappeared; input stopped")
    pane_ids = output("list-panes", "-t", session, "-F", "#{pane_id}").splitlines()
    if controller not in pane_ids or pane not in pane_ids or pane == controller:
        raise SystemExit("validated pane is no longer a Railmux agent pane")

if interactive:
    if not controller:
        raise SystemExit("interactive mode requires a managed Railmux window")
    validate_interactive_target()

print(
    "Railmux fast prototype: %s %s at %.1f FPS for %s seconds"
    % (
        "interacting with" if interactive else "sampling",
        pane,
        fps,
        "unlimited" if duration == 0 else ("%.1f" % duration),
    ),
    file=sys.stderr,
    flush=True,
)
interval = 1.0 / fps
started = time.monotonic()
deadline = started
previous = None
input_buffer = bytearray()

def forward_input(data):
    validate_interactive_target()
    # -H sends the exact byte values straight to the pane. It bypasses tmux
    # client key tables, so local input cannot invoke Railmux/tmux bindings.
    # The condition and send command execute in one tmux command queue, closing
    # the validation/send race if Railmux switches Target panes concurrently.
    condition = "#{&&:#{==:#{session_id},%s},#{&&:#{==:#{@railmux_controller_pane},%s},#{==:#{@railmux_target_pane},%s}}}" % (
        session_id, controller, pane,
    )
    send = "send-keys -H -t %s %s ; display-message -p RMUX_INPUT_OK" % (
        pane, " ".join("%02x" % value for value in data),
    )
    result = subprocess.check_output(
        [
            *TMUX, "if-shell", "-F", "-t", controller,
            condition,
            send,
            "display-message -p RMUX_INPUT_REJECTED",
        ],
        stderr=subprocess.DEVNULL,
    )
    if b"RMUX_INPUT_OK" not in result.splitlines():
        raise SystemExit("Railmux Target changed during input; input stopped")

def read_input():
    try:
        data = __import__("os").read(0, 65536)
    except BlockingIOError:
        return
    if not data:
        raise SystemExit("local input stream closed")
    input_buffer.extend(data)
    while True:
        marker = input_buffer.find(INPUT_MAGIC)
        if marker < 0:
            keep = min(len(input_buffer), len(INPUT_MAGIC) - 1)
            if len(input_buffer) > keep:
                del input_buffer[:-keep]
            return
        if marker:
            del input_buffer[:marker]
        header_size = len(INPUT_MAGIC) + 4
        if len(input_buffer) < header_size:
            return
        size = struct.unpack(">I", input_buffer[len(INPUT_MAGIC):header_size])[0]
        if size < 1 or size > MAX_INPUT_BYTES:
            del input_buffer[0]
            continue
        packet_size = header_size + size
        if len(input_buffer) < packet_size:
            return
        payload = bytes(input_buffer[header_size:packet_size])
        del input_buffer[:packet_size]
        forward_input(payload)

while duration == 0 or time.monotonic() - started < duration:
    if interactive:
        import select
        readable, _, _ = select.select([sys.stdin.buffer], [], [], 0)
        if readable:
            read_input()
    try:
        raw_state = output(
            "display-message", "-p", "-t", pane,
            "#{pane_width}\t#{pane_height}\t#{cursor_x}\t#{cursor_y}",
        )
        width, height, cursor_x, cursor_y = (
            int(value) for value in raw_state.split("\t")
        )
        screen = subprocess.check_output(
            [*TMUX, "capture-pane", "-p", "-e", "-t", pane],
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, ValueError):
        raise SystemExit("target pane disappeared: " + pane)
    state = (width, height, cursor_x, cursor_y, screen)
    if state != previous:
        payload = struct.pack(">HHHH", width, height, cursor_x, cursor_y) + screen
        packet = FRAME_MAGIC + struct.pack(">I", len(payload)) + payload
        try:
            sys.stdout.buffer.write(packet)
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            break
        previous = state
    deadline += interval
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    elif remaining < -interval:
        deadline = time.monotonic()
'''


def build_ssh_argv(
    destination: str,
    *,
    session: str,
    pane: Optional[str],
    fps: float,
    duration: float,
    remote_python: str,
    ssh_args: Sequence[str],
    interactive: bool = False,
) -> list[str]:
    remote_argv = [
        remote_python,
        "-c",
        REMOTE_SCRIPT,
        session,
        pane or "",
        str(fps),
        str(duration),
        "1" if interactive else "0",
    ]
    remote_command = " ".join(shlex.quote(value) for value in remote_argv)
    return ["ssh", "-T", *ssh_args, destination, remote_command]


class TerminalSurface:
    """Paint snapshots on the alternate screen and restore it on every exit."""

    def __init__(self, stream: BinaryIO, *, show_cursor: bool = False) -> None:
        self.stream = stream
        self.show_cursor = show_cursor
        self.active = False

    def start(self) -> None:
        if not self.active:
            self.stream.write(b"\033[?1049h\033[2J\033[H\033[?25l")
            self.stream.flush()
            self.active = True

    def paint(self, frame: Frame) -> None:
        self.start()
        lines = frame.screen.splitlines()[:frame.height]
        rendered = [b"\033[0m\033[?7l\033[2J"]
        for row, line in enumerate(lines, start=1):
            rendered.extend((
                b"\033[0m",
                f"\033[{row};1H".encode(),
                b"\033[2K",
                line,
            ))
        cursor_x = min(frame.cursor_x, max(0, frame.width - 1)) + 1
        cursor_y = min(frame.cursor_y, max(0, frame.height - 1)) + 1
        rendered.extend((
            b"\033[0m\033[?7h",
            f"\033[{cursor_y};{cursor_x}H".encode(),
            b"\033[?25h" if self.show_cursor else b"\033[?25l",
        ))
        self.stream.write(b"".join(rendered))
        self.stream.flush()

    def close(self) -> None:
        if self.active:
            self.stream.write(b"\033[0m\033[?25h\033[?1049l")
            self.stream.flush()
            self.active = False


class RawTerminal:
    """Enter raw input mode without ever changing the remote pane geometry."""

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


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Display bounded latest-state snapshots of a live Railmux agent "
            "pane over ordinary SSH"
        ),
    )
    parser.add_argument("destination", help="SSH destination or configured host alias")
    parser.add_argument("--session", default="railmux", help="outer tmux session")
    parser.add_argument("--pane", help="exact pane ID, for example %%42")
    parser.add_argument("--fps", type=float, default=20.0, help="maximum sample FPS")
    parser.add_argument(
        "--duration", type=float, default=30.0,
        help=(
            "bounded run time in seconds; 0 runs until Ctrl-C in read-only "
            "mode or Ctrl-] in interactive mode"
        ),
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help=(
            "forward keyboard bytes only to the validated Railmux Target pane; "
            "Ctrl-] exits locally"
        ),
    )
    parser.add_argument("--remote-python", default="python3")
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
    if not sys.stdout.isatty():
        raise ProbeError("stdout must be an interactive terminal")
    if args.interactive and not sys.stdin.isatty():
        raise ProbeError("interactive mode requires an interactive stdin")
    if shutil.which("ssh") is None:
        raise ProbeError("ssh is not installed or not on PATH")

    argv = build_ssh_argv(
        args.destination,
        session=args.session,
        pane=args.pane,
        fps=args.fps,
        duration=args.duration,
        remote_python=args.remote_python,
        ssh_args=args.ssh_arg,
        interactive=args.interactive,
    )
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if args.interactive else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    surface = TerminalSurface(sys.stdout.buffer, show_cursor=args.interactive)
    started = time.monotonic()
    frames = 0
    screen_bytes = 0
    interrupted = False
    local_exit = False

    def paint(frame: Frame) -> None:
        nonlocal frames, screen_bytes
        local = os.get_terminal_size(sys.stdout.fileno())
        if local.columns < frame.width or local.lines < frame.height:
            raise ProbeError(
                f"local terminal is {local.columns}x{local.lines}, smaller "
                f"than pane {frame.width}x{frame.height}; enlarge the "
                "client window before retrying"
            )
        surface.paint(frame)
        frames += 1
        screen_bytes += len(frame.screen)

    def run_read_only() -> None:
        for frame in iter_frames(process.stdout):
            paint(frame)

    def run_interactive() -> None:
        nonlocal local_exit
        assert process.stdin is not None
        decoder = FrameDecoder()
        selector = selectors.DefaultSelector()
        stdout_fd = process.stdout.fileno()
        stdin_fd = sys.stdin.fileno()
        selector.register(stdout_fd, selectors.EVENT_READ, "remote")
        selector.register(stdin_fd, selectors.EVENT_READ, "local")
        print(
            "interactive snapshot mode: Ctrl-] exits locally; tmux/Railmux "
            "bindings and mouse input are not forwarded",
            file=sys.stderr,
        )
        try:
            with RawTerminal(stdin_fd):
                while True:
                    events = selector.select(timeout=0.25)
                    for key, _mask in events:
                        if key.data == "remote":
                            chunk = os.read(stdout_fd, 65536)
                            if not chunk:
                                selector.unregister(stdout_fd)
                                return
                            for frame in decoder.feed(chunk):
                                paint(frame)
                        else:
                            data = os.read(stdin_fd, 512)
                            if not data:
                                local_exit = True
                                return
                            escape = data.find(LOCAL_ESCAPE)
                            if escape >= 0:
                                data = data[:escape]
                                if data:
                                    process.stdin.write(encode_input(data))
                                    process.stdin.flush()
                                local_exit = True
                                return
                            process.stdin.write(encode_input(data))
                            process.stdin.flush()
                    if process.poll() is not None and not events:
                        return
        finally:
            selector.close()

    try:
        if args.interactive:
            run_interactive()
        else:
            run_read_only()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        surface.close()
        if process.poll() is None:
            process.terminate()
        try:
            returncode = process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait()

    elapsed = max(0.001, time.monotonic() - started)
    print(
        f"snapshot client: painted {frames} changed frames in {elapsed:.1f}s "
        f"({frames / elapsed:.1f} FPS, {screen_bytes / 1024:.1f} KiB screen data)",
        file=sys.stderr,
    )
    if interrupted:
        return 130
    if local_exit:
        return 0
    if frames == 0:
        raise ProbeError(
            "no frame received; verify the SSH destination, remote Python, "
            "tmux session, and pane target"
        )
    return returncode


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
