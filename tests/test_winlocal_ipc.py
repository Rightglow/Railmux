import socket
import threading

import pytest

from railmux.winlocal.ipc import (
    Endpoint,
    read_endpoint,
    receive_message,
    send_message,
    write_endpoint,
)


def test_endpoint_round_trip_and_message_framing(tmp_path):
    path = tmp_path / "endpoint.json"
    expected = Endpoint(23456, "a" * 64, "b" * 32, 123)
    write_endpoint(path, expected)
    left, right = socket.socketpair()
    try:
        thread = threading.Thread(target=send_message, args=(left, b"frame"))
        thread.start()
        assert receive_message(right) == b"frame"
        thread.join()
    finally:
        left.close()
        right.close()
    assert read_endpoint(path) == expected


def test_message_rejects_empty_payload():
    left, right = socket.socketpair()
    try:
        with pytest.raises(ValueError):
            send_message(left, b"")
    finally:
        left.close()
        right.close()

