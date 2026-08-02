import socket
import threading
import time

from railmux.fast_display_protocol import (
    ScreenUpdate,
    ServerMessageDecoder,
    encode_input,
    encode_keyframe_request,
    encode_resize,
)
from railmux.winlocal.backend import WinMuxBackend
from railmux.winlocal.client import NativeClient, connect_endpoint, ensure_daemon
from railmux.winlocal.daemon import AUTH_PREFIX, NATIVE_UI_FAILED, DaemonServer
from railmux.winlocal.ipc import read_endpoint, receive_message, send_message


class _FakeProcess:
    pid = 321

    def read(self, _size=65536):
        raise EOFError

    def write(self, data):
        return len(data)

    def resize(self, columns, rows):
        pass

    def is_alive(self):
        return True

    def terminate(self, force=False):
        return True


def _backend():
    return WinMuxBackend(process_factory=lambda *_args, **_kwargs: _FakeProcess())


def test_daemon_authenticates_routes_resize_and_emits_screen(tmp_path):
    backend = _backend()
    endpoint_file = tmp_path / "endpoint.json"
    server = DaemonServer(backend, endpoint_file=endpoint_file, fps=60)
    endpoint = server.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = connect_endpoint(endpoint)
    client.settimeout(2)
    try:
        send_message(client, encode_resize(90, 24))
        send_message(client, encode_input(b"a"))
        send_message(client, encode_keyframe_request())
        packet = receive_message(client)
        assert packet is not None
        messages = ServerMessageDecoder().feed(packet)
        assert any(
            isinstance(message, ScreenUpdate)
            and (message.width, message.height) == (90, 24)
            for message in messages
        )
        assert backend.create_ui_screen().get_cols_rows() == (90, 23)
    finally:
        client.close()
        server.close()
        thread.join(timeout=2)


def test_daemon_rejects_wrong_token(tmp_path):
    server = DaemonServer(_backend(), endpoint_file=tmp_path / "endpoint.json")
    endpoint = server.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(("127.0.0.1", endpoint.port))
    try:
        send_message(client, AUTH_PREFIX + b"0" * 64)
        assert receive_message(client) == b"ERROR authentication failed"
    finally:
        client.close()
        server.close()
        thread.join(timeout=2)


def test_ensure_daemon_spawns_once_and_connects(tmp_path, monkeypatch):
    endpoint_file = tmp_path / "endpoint.json"
    server = DaemonServer(_backend(), endpoint_file=endpoint_file)
    spawned = []

    def spawn():
        spawned.append(True)
        server.start()
        threading.Thread(target=server.serve_forever, daemon=True).start()

    monkeypatch.setattr(
        "railmux.winlocal.client.endpoint_path", lambda: endpoint_file
    )
    client = ensure_daemon(spawn=spawn, timeout=2)
    try:
        assert spawned == [True]
        assert read_endpoint(endpoint_file) is not None
    finally:
        client.close()
        server.close()


def test_app_exit_disconnects_frontend_but_keeps_daemon_available(tmp_path):
    app_ran = threading.Event()

    class App:
        def run(self):
            app_ran.set()

    server = DaemonServer(
        _backend(),
        endpoint_file=tmp_path / "endpoint.json",
        app_factory=App,
    )
    endpoint = server.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = connect_endpoint(endpoint)
    client.settimeout(2)
    try:
        assert app_ran.wait(2)
        deadline = time.monotonic() + 2
        while receive_message(client) is not None and time.monotonic() < deadline:
            pass
        assert server._listener is not None
    finally:
        client.close()
        server.close()
        thread.join(timeout=2)


def test_app_crash_reports_failure_and_writes_bounded_traceback(tmp_path):
    class App:
        def run(self):
            raise RuntimeError("refresh contract failed")

    error_file = tmp_path / "native-ui-error.log"
    server = DaemonServer(
        _backend(),
        endpoint_file=tmp_path / "endpoint.json",
        app_factory=App,
        ui_error_file=error_file,
    )
    endpoint = server.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = connect_endpoint(endpoint)
    client.settimeout(2)
    try:
        messages = []
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            message = receive_message(client)
            if message is None:
                break
            messages.append(message)
            if message == NATIVE_UI_FAILED:
                break
        assert NATIVE_UI_FAILED in messages
        detail = error_file.read_text(encoding="utf-8")
        assert "RuntimeError: refresh contract failed" in detail
        assert f"daemon_id={server.daemon_id}" in detail
        assert len(detail) <= 65 * 1024
    finally:
        client.close()
        server.close()
        thread.join(timeout=2)


def test_native_client_classifies_ui_failure_before_socket_close():
    daemon, frontend = socket.socketpair()
    client = NativeClient(frontend)
    thread = threading.Thread(target=client._read_socket, daemon=True)
    thread.start()
    try:
        send_message(daemon, NATIVE_UI_FAILED)
        assert client._events.get(timeout=2) == ("ui_error", None)
    finally:
        client._stop.set()
        daemon.close()
        frontend.close()
        thread.join(timeout=2)


def test_idle_daemon_without_clients_or_sessions_exits(tmp_path):
    endpoint_file = tmp_path / "endpoint.json"
    server = DaemonServer(
        _backend(),
        endpoint_file=endpoint_file,
        fps=60,
        idle_timeout=0.01,
    )
    server.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    thread.join(timeout=2)

    assert not thread.is_alive()
    assert not endpoint_file.exists()
