from __future__ import annotations

import io
import struct

import pytest

from tools.fast_client_probe import (
    INPUT_MAGIC,
    MAGIC,
    REMOTE_SCRIPT,
    Frame,
    FrameDecoder,
    TerminalSurface,
    build_ssh_argv,
    encode_input,
    iter_frames,
    parse_args,
)


def _packet(width: int, height: int, screen: bytes) -> bytes:
    payload = struct.pack(">HHHH", width, height, 3, 2) + screen
    return MAGIC + struct.pack(">I", len(payload)) + payload


def test_decoder_accepts_partial_frames_and_ignores_login_noise():
    packet = _packet(80, 24, b"hello\n")
    decoder = FrameDecoder()

    assert decoder.feed(b"remote banner\n" + packet[:5]) == []
    assert decoder.feed(packet[5:-2]) == []
    assert decoder.feed(packet[-2:]) == [Frame(80, 24, 3, 2, b"hello\n")]


def test_decoder_recovers_from_false_marker_and_reads_multiple_frames():
    false = MAGIC + struct.pack(">I", 1) + b"x"
    data = false + _packet(10, 5, b"one") + _packet(12, 6, b"two")

    assert list(iter_frames(io.BytesIO(data))) == [
        Frame(10, 5, 3, 2, b"one"),
        Frame(12, 6, 3, 2, b"two"),
    ]


def test_ssh_command_quotes_remote_arguments_without_local_shell():
    argv = build_ssh_argv(
        "example",
        session="rail mux",
        pane="%42",
        fps=20.0,
        duration=30.0,
        remote_python="python3",
        ssh_args=("-J", "jump-host"),
        interactive=True,
    )

    assert argv[:5] == ["ssh", "-T", "-J", "jump-host", "example"]
    assert "'rail mux'" in argv[-1]
    assert "%42" in argv[-1]
    assert argv[-1].endswith(" 1")


def test_input_frames_are_bounded_and_length_prefixed():
    framed = encode_input(b"hello")

    assert framed.startswith(INPUT_MAGIC)
    assert struct.unpack(">I", framed[len(INPUT_MAGIC):len(INPUT_MAGIC) + 4])[0] == 5
    assert framed.endswith(b"hello")
    with pytest.raises(ValueError):
        encode_input(b"")


def test_terminal_surface_restores_alternate_screen():
    stream = io.BytesIO()
    surface = TerminalSurface(stream)

    surface.paint(Frame(5, 2, 1, 1, b"one\ntwo\n"))
    surface.close()

    rendered = stream.getvalue()
    assert rendered.startswith(b"\033[?1049h")
    assert b"\033[?7l\033[2J" in rendered
    assert b"\033[1;1H\033[2Kone" in rendered
    assert b"\033[2;1H\033[2Ktwo" in rendered
    assert b"one\r\ntwo" not in rendered
    assert b"\033[?7h" in rendered
    assert b"\033[2;2H\033[?25l" in rendered
    assert rendered.endswith(b"\033[0m\033[?25h\033[?1049l")


def test_interactive_surface_shows_remote_cursor():
    stream = io.BytesIO()
    surface = TerminalSurface(stream, show_cursor=True)

    surface.paint(Frame(10, 5, 4, 2, b"prompt"))

    assert stream.getvalue().endswith(b"\033[3;5H\033[?25h")


def test_remote_capture_drops_unneeded_trailing_spaces():
    compile(REMOTE_SCRIPT, "<remote fast client>", "exec")
    assert '[*TMUX, "capture-pane", "-p", "-e", "-t", pane]' in REMOTE_SCRIPT
    assert 'TMUX = ["tmux", "-L", "railmux"]' in REMOTE_SCRIPT
    assert '"capture-pane", "-p", "-e", "-N"' not in REMOTE_SCRIPT


def test_remote_interactive_path_has_no_tmux_topology_mutations():
    assert '"tmux", "send-keys", "-H"' not in REMOTE_SCRIPT
    assert '"send-keys", "-H"' not in REMOTE_SCRIPT
    assert "send-keys -H -t %s" in REMOTE_SCRIPT
    for destructive in (
        "attach-session",
        "new-session",
        "kill-session",
        "kill-pane",
        "split-window",
        "swap-pane",
        "resize-pane",
        "set-option",
    ):
        assert destructive not in REMOTE_SCRIPT


@pytest.mark.parametrize(
    "argv",
    [
        ["host", "--fps", "0"],
        ["host", "--fps", "61"],
        ["host", "--duration", "-1"],
    ],
)
def test_cli_rejects_unbounded_invalid_workloads(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)
