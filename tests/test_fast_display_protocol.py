from __future__ import annotations

import struct
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from railmux.fast_display_protocol import (
    ClaudeHistoryPolicyResult,
    DISPLAY_MAGIC,
    HistorySnapshot,
    InputFrameDecoder,
    InputKind,
    PathKind,
    PathOpenResult,
    PathResult,
    ScreenUpdate,
    ScreenUpdateDecoder as ClientScreenUpdateDecoder,
    ServerMessageDecoder,
    TerminalMode,
    UpdateKind,
    decode_history_request,
    decode_path_request,
    decode_path_open_request,
    decode_claude_history_policy,
    decode_claude_history_choice,
    encode_history_request,
    encode_history_snapshot,
    encode_path_request,
    encode_path_open_request,
    encode_path_open_result,
    encode_path_result,
    encode_claude_history_policy,
    encode_claude_history_policy_result,
    encode_update,
)
from railmux import fast_display_client, fast_display_server
from railmux.fast_display_client import (
    UpdateKind as ClientUpdateKind,
)


def _keyframe(
    sequence: int = 1,
    width: int = 4,
    height: int = 2,
    terminal_modes: TerminalMode = TerminalMode.NONE,
) -> ScreenUpdate:
    return ScreenUpdate(
        kind=UpdateKind.KEYFRAME,
        sequence=sequence,
        width=width,
        height=height,
        cursor_x=1,
        cursor_y=0,
        cursor_visible=True,
        rows=tuple((index, f"row-{index}".encode()) for index in range(height)),
        terminal_modes=terminal_modes,
    )


def test_compressed_keyframe_crosses_standalone_client_decoder_in_parts():
    packet = encode_update(_keyframe())
    decoder = ClientScreenUpdateDecoder()

    assert decoder.feed(b"login banner\n" + packet[:8]) == []
    assert decoder.feed(packet[8:-1]) == []
    updates = decoder.feed(packet[-1:])

    assert len(updates) == 1
    update = updates[0]
    assert update.kind is ClientUpdateKind.KEYFRAME
    assert update.sequence == 1
    assert (update.width, update.height) == (4, 2)
    assert update.rows == ((0, b"row-0"), (1, b"row-1"))
    assert update.terminal_modes is TerminalMode.NONE


def test_v10_wire_round_trips_allowlisted_terminal_modes_and_rejects_unknown():
    modes = TerminalMode.BRACKETED_PASTE | TerminalMode.FOCUS_EVENTS
    update = ClientScreenUpdateDecoder().feed(
        encode_update(_keyframe(terminal_modes=modes))
    )[0]

    assert update.terminal_modes == modes
    with pytest.raises(ValueError, match="unknown terminal mode"):
        encode_update(_keyframe(terminal_modes=TerminalMode(1 << 8)))

    malformed = bytearray(encode_update(_keyframe()))
    modes_offset = len(DISPLAY_MAGIC) + 4 + 1 + 14
    malformed[modes_offset : modes_offset + 2] = struct.pack(">H", 1 << 8)
    assert ClientScreenUpdateDecoder().feed(malformed) == []


def test_v10_decoder_does_not_accept_a_v9_packet_prefix():
    old_packet = b"RMUXD9\x00" + struct.pack(">I", 32) + bytes(32)

    assert ClientScreenUpdateDecoder().feed(old_packet) == []


def test_v10_unified_decoder_round_trips_history_capabilities():
    snapshot = HistorySnapshot(
        request_id=7,
        pane_id="%42",
        x=30,
        y=1,
        width=50,
        height=2,
        lines=(b"old-1", b"old-2", b"visible-1", b"visible-2"),
        mouse_forwardable=True,
        more_available=True,
        transcript_available=True,
        history_choice_required=True,
        generation=42,
    )
    local_snapshot = replace(
        snapshot,
        request_id=8,
        transcript_backed=True,
        history_choice_required=False,
    )
    packet = b"".join(
        (
            encode_update(_keyframe()),
            encode_history_snapshot(snapshot),
            encode_history_snapshot(local_snapshot),
            encode_update(_keyframe(sequence=2)),
        )
    )
    decoder = ServerMessageDecoder()

    assert decoder.feed(packet[:11]) == []
    messages = decoder.feed(packet[11:])

    assert messages == [
        _keyframe(),
        snapshot,
        local_snapshot,
        _keyframe(sequence=2),
    ]


def test_v10_history_snapshot_round_trips_the_maximum_line_count():
    snapshot = HistorySnapshot(
        request_id=8,
        pane_id="%42",
        x=30,
        y=1,
        width=50,
        height=2,
        lines=(b"x",) * 20000,
    )

    assert ServerMessageDecoder().feed(encode_history_snapshot(snapshot)) == [snapshot]


def test_rejected_history_response_is_bounded_and_screen_decoder_ignores_it():
    rejected = HistorySnapshot(9, None)
    packet = encode_history_snapshot(rejected) + encode_update(_keyframe())

    assert ServerMessageDecoder().feed(packet)[0] == rejected
    assert ClientScreenUpdateDecoder().feed(packet) == [_keyframe()]


def test_history_request_round_trip_validates_pointer_and_line_limit():
    decoder = InputFrameDecoder()
    message = decoder.feed(encode_history_request(12, 80, 24, 1500))[0]

    assert message.kind is InputKind.REQUEST_HISTORY
    assert decode_history_request(message.data) == (12, 80, 24, 1500)
    with pytest.raises(ValueError):
        encode_history_request(1, 0, 24)
    assert decode_history_request(
        decoder.feed(encode_history_request(1, 80, 24, 20000))[0].data
    ) == (1, 80, 24, 20000)
    with pytest.raises(ValueError):
        encode_history_request(1, 80, 24, 20001)


def test_path_request_and_result_round_trip_are_bounded():
    request = InputFrameDecoder().feed(encode_path_request(17, "%42", "src/main.py"))[0]

    assert request.kind is InputKind.RESOLVE_PATH
    assert decode_path_request(request.data) == (17, "%42", "src/main.py")
    result = PathResult(17, PathKind.FILE, "/workspace/src/main.py")
    assert ServerMessageDecoder().feed(encode_path_result(result)) == [result]
    rejected = PathResult(18, PathKind.UNAVAILABLE)
    assert ServerMessageDecoder().feed(encode_path_result(rejected)) == [rejected]

    with pytest.raises(ValueError):
        encode_path_request(1, "42", "main.py")
    with pytest.raises(ValueError):
        encode_path_request(1, "%42", "x" * 4097)
    with pytest.raises(ValueError):
        encode_path_result(PathResult(1, PathKind.UNAVAILABLE, "/leak"))


def test_path_open_request_and_result_round_trip_are_bounded():
    decoder = InputFrameDecoder()
    message = decoder.feed(
        encode_path_open_request(
            19,
            "%42",
            "src/main.py",
            policy="internal",
            persistent=True,
            line=123,
            column=7,
        )
    )[0]

    assert message.kind is InputKind.OPEN_PATH
    assert decode_path_open_request(message.data) == (
        19,
        "%42",
        "src/main.py",
        "internal",
        True,
        123,
        7,
    )
    result = PathOpenResult(19, True, "success", "Opened inside Railmux")
    assert ServerMessageDecoder().feed(encode_path_open_result(result)) == [result]

    with pytest.raises(ValueError):
        encode_path_open_request(1, "%42", "main.py", policy="ask", persistent=False)
    with pytest.raises(ValueError):
        encode_path_open_request(
            1,
            "%42",
            "main.py",
            policy="external",
            persistent=False,
            column=3,
        )
    with pytest.raises(ValueError):
        encode_path_open_result(PathOpenResult(1, False, "debug", "no"))


def test_claude_history_policy_input_round_trip_is_bounded():
    decoder = InputFrameDecoder()
    encoded = encode_claude_history_policy("local")
    message = decoder.feed(encoded)[0]

    assert message.kind is InputKind.SET_CLAUDE_HISTORY
    assert decode_claude_history_choice(message.data) == ("local", True)
    temporary = decoder.feed(encode_claude_history_policy("native", persistent=False))[
        0
    ]
    assert decode_claude_history_choice(temporary.data) == ("native", False)
    assert decoder.feed(encoded[:-1] + b"\x05") == []
    with pytest.raises(ValueError):
        encode_claude_history_policy("ask")
    with pytest.raises(ValueError):
        decode_claude_history_policy(b"\x03")
    with pytest.raises(ValueError):
        decode_claude_history_choice(b"\x05")


def test_claude_history_policy_result_round_trips_scope_and_outcome():
    decoder = ServerMessageDecoder()

    assert decoder.feed(
        encode_claude_history_policy_result("native", persistent=True, applied=True)
    ) == [ClaudeHistoryPolicyResult("native", True, True)]
    assert decoder.feed(
        encode_claude_history_policy_result("local", persistent=False, applied=True)
    ) == [ClaudeHistoryPolicyResult("local", False, True)]
    with pytest.raises(ValueError):
        encode_claude_history_policy_result("ask", persistent=True, applied=True)
    with pytest.raises(ValueError):
        encode_claude_history_policy_result("local", persistent=True, applied=1)


@pytest.mark.parametrize(
    ("policy", "persistent", "runtime", "prefetch", "forwarded"),
    [
        ("local", True, None, True, b""),
        ("local", False, "local", True, b""),
        ("native", True, None, False, b"wheel"),
        ("native", False, "native", False, b"wheel"),
    ],
)
def test_claude_history_policy_ack_applies_scope_and_original_wheel(
    policy,
    persistent,
    runtime,
    prefetch,
    forwarded,
):
    action = fast_display_client.apply_claude_history_policy_result(
        (policy, persistent, b"wheel"),
        ClaudeHistoryPolicyResult(policy, persistent, True),
    )

    assert action is not None
    assert action.update_runtime
    assert action.runtime_choice == runtime
    assert action.prefetch is prefetch
    assert action.forwarded_input == forwarded


def test_claude_history_policy_ack_rejects_mismatch_and_preserves_failed_state():
    assert (
        fast_display_client.apply_claude_history_policy_result(
            ("local", False, b"wheel"),
            ClaudeHistoryPolicyResult("native", False, True),
        )
        is None
    )

    failed = fast_display_client.apply_claude_history_policy_result(
        ("local", True, b"wheel"),
        ClaudeHistoryPolicyResult("local", True, False),
    )
    assert failed is not None
    assert not failed.update_runtime
    assert not failed.prefetch
    assert failed.forwarded_input == b""


def test_claude_history_reconnect_resends_only_this_time_choice():
    assert fast_display_client.claude_history_reconnect_frame(None) == b""
    message = InputFrameDecoder().feed(
        fast_display_client.claude_history_reconnect_frame("native")
    )[0]
    assert decode_claude_history_choice(message.data) == ("native", False)


def test_server_claude_history_choice_persists_only_when_requested():
    settings = MagicMock()
    settings.set_claude_history_policy.return_value = True

    applied, override = fast_display_server.apply_claude_history_choice(
        "local",
        persistent=True,
        current_override="native",
        settings=settings,
    )
    assert (applied, override) == (True, None)
    settings.set_claude_history_policy.assert_called_once_with("local")

    settings.reset_mock()
    applied, override = fast_display_server.apply_claude_history_choice(
        "native",
        persistent=False,
        current_override="local",
        settings=settings,
    )
    assert (applied, override) == (True, "native")
    settings.set_claude_history_policy.assert_not_called()


def test_server_failed_persistent_history_choice_keeps_previous_override():
    settings = MagicMock()
    settings.set_claude_history_policy.return_value = False

    assert fast_display_server.apply_claude_history_choice(
        "local",
        persistent=True,
        current_override="native",
        settings=settings,
    ) == (False, "native")
