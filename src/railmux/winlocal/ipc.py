"""Authenticated loopback IPC for the per-user Windows daemon."""
from __future__ import annotations

import json
import secrets
import socket
import struct
from dataclasses import dataclass
from pathlib import Path

from railmux.atomic_file import atomic_write_text


_LENGTH = struct.Struct(">I")
MAX_MESSAGE = 16 * 1024 * 1024


@dataclass(frozen=True)
class Endpoint:
    port: int
    token: str
    daemon_id: str
    pid: int


def write_endpoint(path: Path, endpoint: Endpoint) -> None:
    atomic_write_text(
        path,
        json.dumps(endpoint.__dict__, separators=(",", ":"), sort_keys=True),
    )


def read_endpoint(path: Path) -> Endpoint | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        endpoint = Endpoint(
            port=int(raw["port"]),
            token=str(raw["token"]),
            daemon_id=str(raw["daemon_id"]),
            pid=int(raw["pid"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not 1 <= endpoint.port <= 65535
        or len(endpoint.token) != 64
        or len(endpoint.daemon_id) != 32
        or any(character not in "0123456789abcdef" for character in endpoint.token)
        or any(character not in "0123456789abcdef" for character in endpoint.daemon_id)
        or endpoint.pid <= 0
    ):
        return None
    return endpoint


def new_token() -> str:
    return secrets.token_hex(32)


def send_message(sock: socket.socket, payload: bytes) -> None:
    if not payload or len(payload) > MAX_MESSAGE:
        raise ValueError("invalid daemon message size")
    sock.sendall(_LENGTH.pack(len(payload)) + payload)


def receive_message(sock: socket.socket) -> bytes | None:
    header = _receive_exact(sock, _LENGTH.size)
    if header is None:
        return None
    (length,) = _LENGTH.unpack(header)
    if not 0 < length <= MAX_MESSAGE:
        raise ValueError("invalid daemon message size")
    return _receive_exact(sock, length)


def _receive_exact(sock: socket.socket, size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        data = sock.recv(size - len(chunks))
        if not data:
            return None
        chunks.extend(data)
    return bytes(chunks)
