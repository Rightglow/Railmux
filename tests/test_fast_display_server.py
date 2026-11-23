from __future__ import annotations

import inspect
import io
import json
import select
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from railmux.config import Config
from railmux.fast_display_history import HistoryCaptureJob, HistoryCaptureWorker
from railmux.fast_display_protocol import (
    HistorySnapshot,
    InputFrameDecoder,
    PROTOCOL_VERSION,
    REMOTE_CONFIG_PROTOCOL,
    REMOTE_ATTACH_BUSY,
    REMOTE_START,
    ServerMessageDecoder,
    TerminalMode,
    encode_history_snapshot,
    encode_image_begin,
    encode_image_chunk,
    encode_image_commit,
)
from railmux.fast_display_server import parse_args as parse_server_args
from railmux.fast_display_server import render_rows
from railmux.terminal_screen import terminal_modes_for_screen
from railmux import fast_display_client, fast_display_server, tmux_ctl
from railmux.fast_display_client import (
    RemoteHello,
    parse_remote_hello,
)


def test_remote_server_has_no_bare_tmux_server_argv():
    source = inspect.getsource(fast_display_server)

    assert '["tmux",' not in source
    assert "['tmux'," not in source


def test_image_paste_revalidates_generation_and_bracket_pastes_generated_path(
    monkeypatch,
    tmp_path,
):
    pane = fast_display_server._PaneGeometry(
        "%7",
        1,
        1,
        80,
        24,
        history_generation=55,
        route_generation=55,
    )
    monkeypatch.setattr(
        fast_display_server,
        "_list_agent_panes",
        lambda _session, **_kwargs: (pane,),
    )
    stored = tmp_path / "railmux-image.png"
    pasted = []
    monkeypatch.setattr(
        fast_display_server.image_paste,
        "store_png",
        lambda data: stored if data == b"png" else None,
    )
    monkeypatch.setattr(
        fast_display_server.image_paste,
        "provider_path",
        lambda path: f"provider:{path}",
    )
    monkeypatch.setattr(
        fast_display_server,
        "_paste_provider_path",
        lambda route, path: pasted.append((route, path)) or True,
    )

    result = fast_display_server.paste_image_into_pane(
        "railmux", 9, "%7", 55, b"png"
    )

    assert result.applied is True
    assert pasted == [(pane, f"provider:{stored}")]


def test_image_upload_controller_requires_order_and_integrity(monkeypatch):
    pane = fast_display_server._PaneGeometry(
        "%7", 1, 1, 80, 24, history_generation=55, route_generation=55
    )
    monkeypatch.setattr(
        fast_display_server,
        "_list_agent_panes",
        lambda _session, **_kwargs: (pane,),
    )
    payload = b"complete-png-payload" * 5
    digest = fast_display_server.hashlib.sha256(payload).digest()
    decoder = InputFrameDecoder()
    begin = decoder.feed(encode_image_begin(4, "%7", 55, len(payload), digest))[0]
    first = decoder.feed(encode_image_chunk(4, 0, payload[:8]))[0]
    second = decoder.feed(encode_image_chunk(4, 8, payload[8:]))[0]
    commit = decoder.feed(encode_image_commit(4))[0]
    applied = fast_display_server.ImagePasteResult(4, True, "success", "done")
    paste = MagicMock(return_value=applied)
    monkeypatch.setattr(fast_display_server, "paste_image_into_pane", paste)
    controller = fast_display_server._ImageUploadController()

    assert controller.begin("railmux", begin.data) is None
    assert controller.chunk(first.data) is None
    assert controller.chunk(second.data) is None
    assert controller.commit("railmux", commit.data) == applied
    paste.assert_called_once_with("railmux", 4, "%7", 55, payload)

    controller = fast_display_server._ImageUploadController()
    assert controller.begin("railmux", begin.data) is None
    rejected = controller.chunk(second.data)
    assert rejected is not None and rejected.applied is False
    assert controller.pending is None


def test_image_paste_rejects_stale_route_before_writing(monkeypatch):
    store = MagicMock()
    monkeypatch.setattr(fast_display_server, "_list_agent_panes", lambda *_a, **_k: ())
    monkeypatch.setattr(fast_display_server.image_paste, "store_png", store)

    result = fast_display_server.paste_image_into_pane(
        "railmux", 9, "%7", 55, b"png"
    )

    assert result.applied is False
    assert result.level == "warning"
    store.assert_not_called()


def test_provider_image_path_uses_named_bracketed_tmux_buffer(monkeypatch):
    pane = fast_display_server._PaneGeometry("%7", 1, 1, 80, 24)
    calls = []
    monkeypatch.setattr(
        fast_display_server.tmux_server,
        "tmux_argv",
        lambda *argv: ["tmux", *argv],
    )

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(fast_display_server.subprocess, "run", run)

    assert fast_display_server._paste_provider_path(pane, "/tmp/image.png")
    assert calls[0][0][1] == "load-buffer"
    assert calls[0][1]["input"] == b"/tmp/image.png"
    assert "-p" in calls[1][0]
    assert calls[1][0][-2:] == ["-t", "%7"]
    assert calls[2][0][1] == "delete-buffer"


def test_history_worker_keeps_capture_off_caller_and_signals_result(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_capture(_session, request_id, *_args, **_kwargs):
        started.set()
        assert release.wait(2)
        return HistorySnapshot(request_id, None)

    monkeypatch.setattr(fast_display_server, "capture_history_snapshot", slow_capture)
    worker = HistoryCaptureWorker(
        object(),
        capture_snapshot=slow_capture,
        capture_batch=fast_display_server.capture_history_batch,
    )
    try:
        before = time.monotonic()
        assert worker.submit(
            HistoryCaptureJob("snapshot", "railmux", (7, 1, 1, 50), None)
        )
        assert time.monotonic() - before < 0.1
        assert started.wait(1)
        release.set()
        readable, _, _ = select.select([worker.read_fd], [], [], 2)
        assert readable == [worker.read_fd]
        assert worker.drain() == (HistorySnapshot(7, None),)
    finally:
        release.set()
        worker.close()


def test_history_worker_coalesces_unstarted_prefetches(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    captured: list[int] = []

    def capture(_pyte, _session, request_id, _lines, **_kwargs):
        captured.append(request_id)
        if request_id == 1:
            started.set()
            assert release.wait(2)
        return fast_display_server.HistoryBatch(request_id, ())

    worker = HistoryCaptureWorker(
        object(),
        capture_snapshot=fast_display_server.capture_history_snapshot,
        capture_batch=capture,
    )
    try:
        assert worker.submit(
            HistoryCaptureJob("batch", "railmux", (1, 300), None)
        )
        assert started.wait(1)
        assert worker.submit(
            HistoryCaptureJob("batch", "railmux", (2, 300), None)
        )
        assert worker.submit(
            HistoryCaptureJob("batch", "railmux", (3, 300), None)
        )
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and captured != [1, 3]:
            select.select([worker.read_fd], [], [], 0.05)
            worker.drain()
        assert captured == [1, 3]
    finally:
        release.set()
        worker.close()


def test_remote_server_hello_reports_version_protocol_and_dependency(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setattr(fast_display_server.shutil, "which", lambda _name: "/tmux")
    monkeypatch.setattr(fast_display_server.sys, "stdout", MagicMock(buffer=output))

    fast_display_server._emit_remote_hello(True)

    hello = parse_remote_hello(output.getvalue())
    assert hello == RemoteHello(
        fast_display_client.__version__,
        PROTOCOL_VERSION,
        True,
        config_protocol=REMOTE_CONFIG_PROTOCOL,
    )


def test_remote_server_hello_identifies_managed_windows_runtime(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setenv("RAILMUX_WINDOWS_RUNTIME", "msys2")
    monkeypatch.setattr(fast_display_server.shutil, "which", lambda _name: "/tmux")
    monkeypatch.setattr(fast_display_server.sys, "stdout", MagicMock(buffer=output))

    fast_display_server._emit_remote_hello(True)

    hello = parse_remote_hello(output.getvalue())
    assert hello.platform == "windows-msys2"


def test_remote_server_waits_for_exact_start_confirmation(monkeypatch):
    remote_input = MagicMock(buffer=io.BytesIO(REMOTE_START))
    monkeypatch.setattr(fast_display_server.sys, "stdin", remote_input)
    monkeypatch.setattr(
        fast_display_server.select,
        "select",
        lambda *_args: ([remote_input.buffer], [], []),
    )

    assert fast_display_server._await_client_start() is True

    remote_input.buffer = io.BytesIO(b"wrong\n")
    assert fast_display_server._await_client_start() is False


def test_remote_server_missing_dependency_never_touches_tmux(monkeypatch):
    monkeypatch.setattr(fast_display_server, "_fast_dependency_ready", lambda: False)
    emit = MagicMock()
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", emit)
    monkeypatch.setattr(fast_display_server, "_await_client_start", lambda: True)
    socket_label = MagicMock()
    monkeypatch.setattr(fast_display_server.tmux_server, "socket_label", socket_label)

    result = fast_display_server.main(
        [
            "--protocol",
            str(PROTOCOL_VERSION),
            "--width",
            "80",
            "--height",
            "24",
        ]
    )

    assert result == 2
    emit.assert_called_once_with(False)
    socket_label.assert_not_called()


def test_remote_server_reports_missing_configured_tmux_without_fallback(
    monkeypatch,
):
    monkeypatch.setattr(
        fast_display_server,
        "load_config",
        lambda: Config(tmux_binary="/missing/bin/tmux"),
    )
    monkeypatch.setattr(
        fast_display_server,
        "check_executable",
        lambda *_args, **_kwargs: MagicMock(valid=False),
    )
    monkeypatch.setattr(fast_display_server, "_fast_dependency_ready", lambda: True)
    emit = MagicMock()
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", emit)
    monkeypatch.setattr(fast_display_server, "_await_client_start", lambda: True)
    socket_label = MagicMock()
    monkeypatch.setattr(fast_display_server.tmux_server, "socket_label", socket_label)

    result = fast_display_server.main(
        ["--protocol", str(PROTOCOL_VERSION), "--width", "80", "--height", "24"]
    )

    assert result == 2
    emit.assert_called_once_with(
        True,
        config_status="valid",
        tmux_configured=True,
        tmux_available=False,
    )
    socket_label.assert_not_called()


def test_remote_protocol_probe_exits_quietly_before_local_upgrade_prompt(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(fast_display_server, "_fast_dependency_ready", lambda: True)
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", MagicMock())
    monkeypatch.setattr(fast_display_server, "_await_client_start", lambda: False)

    result = fast_display_server.main(
        [
            "--protocol",
            str(PROTOCOL_VERSION - 1),
            "--width",
            "80",
            "--height",
            "24",
        ]
    )

    assert result == 2
    assert capsys.readouterr().err == ""


def test_remote_server_attaches_only_after_start_confirmation(monkeypatch):
    monkeypatch.setattr(fast_display_server, "_fast_dependency_ready", lambda: True)
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", MagicMock())
    monkeypatch.setattr(fast_display_server, "_await_client_start", lambda: True)
    monkeypatch.setattr(
        fast_display_server.tmux_server, "socket_label", lambda: "railmux"
    )
    serve = MagicMock(return_value=17)
    monkeypatch.setattr(fast_display_server, "serve", serve)

    result = fast_display_server.main(
        [
            "--protocol",
            str(PROTOCOL_VERSION),
            "--session",
            "custom",
            "--width",
            "80",
            "--height",
            "24",
            "--fps",
            "30",
        ]
    )

    assert result == 17
    serve.assert_called_once_with(
        "custom",
        80,
        24,
        30.0,
        replace_existing_client=False,
        existing_session_only=False,
    )


def test_remote_server_existing_only_flag_reaches_attach_boundary(monkeypatch):
    monkeypatch.setattr(fast_display_server, "_fast_dependency_ready", lambda: True)
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", MagicMock())
    monkeypatch.setattr(fast_display_server, "_await_client_start", lambda: True)
    monkeypatch.setattr(
        fast_display_server.tmux_server, "socket_label", lambda: "railmux"
    )
    serve = MagicMock(return_value=0)
    monkeypatch.setattr(fast_display_server, "serve", serve)

    assert (
        fast_display_server.main(
            [
                "--protocol",
                str(PROTOCOL_VERSION),
                "--width",
                "80",
                "--height",
                "24",
                "--existing-session-only",
            ]
        )
        == 0
    )
    serve.assert_called_once_with(
        "railmux",
        80,
        24,
        20.0,
        replace_existing_client=False,
        existing_session_only=True,
    )


def test_remote_server_busy_status_is_machine_readable(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setattr(fast_display_server, "_fast_dependency_ready", lambda: True)
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", MagicMock())
    monkeypatch.setattr(fast_display_server, "_await_client_start", lambda: True)
    monkeypatch.setattr(
        fast_display_server.tmux_server, "socket_label", lambda: "railmux"
    )
    monkeypatch.setattr(
        fast_display_server,
        "serve",
        MagicMock(side_effect=fast_display_server.DisplayServerBusy("held")),
    )
    monkeypatch.setattr(fast_display_server.sys, "stdout", MagicMock(buffer=output))

    result = fast_display_server.main(
        [
            "--protocol",
            str(PROTOCOL_VERSION),
            "--width",
            "80",
            "--height",
            "24",
        ]
    )

    assert result == 2
    assert output.getvalue() == REMOTE_ATTACH_BUSY


def test_current_attach_lock_is_released_before_display_lifetime(monkeypatch):
    events = []
    monkeypatch.setattr(
        fast_display_server, "_ensure_railmux_session", lambda _session: "$4"
    )
    monkeypatch.setattr(fast_display_server, "_validate_railmux", lambda _session: "$4")
    monkeypatch.setattr(
        fast_display_server,
        "_acquire_display_lock",
        lambda _session, **_kwargs: events.append("acquire") or 9,
    )
    monkeypatch.setattr(
        fast_display_server,
        "_release_display_lock",
        lambda _fd: events.append("release"),
    )
    monkeypatch.setattr(
        fast_display_server, "_use_smallest_window_size", lambda _session: None
    )
    monkeypatch.setattr(
        fast_display_server,
        "_spawn_tmux_client",
        lambda *_args: events.append("spawn") or (123, 10),
    )
    monkeypatch.setattr(
        fast_display_server, "_wait_until_attached", lambda *_args: True
    )
    monkeypatch.setattr(
        fast_display_server, "_emit_attach_status", lambda _status: None
    )

    def display(*_args):
        events.append("display")
        assert events.index("release") < events.index("display")
        return 17

    monkeypatch.setattr(fast_display_server, "_serve_attached", display)

    assert fast_display_server.serve("railmux", 80, 24, 30.0) == 17
    assert events == ["acquire", "spawn", "release", "display"]


def test_existing_session_only_attach_never_creates_outer_session(monkeypatch):
    ensure = MagicMock(side_effect=AssertionError("must not create"))
    monkeypatch.setattr(fast_display_server, "_ensure_railmux_session", ensure)
    monkeypatch.setattr(fast_display_server, "_validate_railmux", lambda _session: "$4")
    monkeypatch.setattr(
        fast_display_server, "_acquire_display_lock", lambda *_args, **_kwargs: 9
    )
    monkeypatch.setattr(fast_display_server, "_release_display_lock", lambda _fd: None)
    monkeypatch.setattr(
        fast_display_server, "_use_smallest_window_size", lambda _session: None
    )
    monkeypatch.setattr(
        fast_display_server, "_spawn_tmux_client", lambda *_args: (123, 10)
    )
    monkeypatch.setattr(
        fast_display_server, "_wait_until_attached", lambda *_args: True
    )
    monkeypatch.setattr(
        fast_display_server, "_emit_attach_status", lambda _status: None
    )
    monkeypatch.setattr(fast_display_server, "_serve_attached", lambda *_args: 0)

    assert (
        fast_display_server.serve(
            "railmux",
            80,
            24,
            30.0,
            existing_session_only=True,
        )
        == 0
    )
    ensure.assert_not_called()


def test_replacement_reenumerates_clients_inside_attach_lock(monkeypatch):
    detached = []
    monkeypatch.setattr(fast_display_server, "_validate_railmux", lambda _session: "$4")
    monkeypatch.setattr(
        fast_display_server,
        "_detach_session_clients",
        lambda session: detached.append(session),
    )
    monkeypatch.setattr(
        fast_display_server, "_acquire_display_lock", lambda *_args, **_kwargs: 9
    )
    monkeypatch.setattr(fast_display_server, "_release_display_lock", lambda _fd: None)
    monkeypatch.setattr(
        fast_display_server, "_use_smallest_window_size", lambda _session: None
    )
    monkeypatch.setattr(
        fast_display_server, "_spawn_tmux_client", lambda *_args: (123, 10)
    )
    monkeypatch.setattr(
        fast_display_server, "_wait_until_attached", lambda *_args: True
    )
    monkeypatch.setattr(
        fast_display_server, "_emit_attach_status", lambda _status: None
    )
    monkeypatch.setattr(fast_display_server, "_serve_attached", lambda *_args: 0)

    assert (
        fast_display_server.serve("railmux", 80, 24, 30.0, replace_existing_client=True)
        == 0
    )
    assert detached == ["$4", "$4"]


def test_server_starts_default_railmux_with_current_python(monkeypatch):
    identities = iter((None, "$7"))
    monkeypatch.setattr(
        fast_display_server, "_try_session_id", lambda _session: next(identities)
    )
    monkeypatch.setattr(
        fast_display_server, "_live_controller", lambda session_id: "%9"
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert fast_display_server._ensure_railmux_session("railmux") == "$7"
    assert calls[0][0][:7] == [
        "tmux",
        "-L",
        "railmux",
        "new-session",
        "-d",
        "-s",
        "railmux",
    ]
    assert shlex.split(calls[0][0][-1]) == [
        sys.executable,
        "-m",
        "railmux",
        "--inside-tmux",
        "--no-scroll-coalescing",
    ]


def test_server_does_not_change_an_existing_railmux_scroll_policy(monkeypatch):
    monkeypatch.setattr(fast_display_server, "_try_session_id", lambda _session: "$7")
    run = MagicMock(side_effect=AssertionError("existing session was restarted"))
    monkeypatch.setattr(subprocess, "run", run)

    assert fast_display_server._ensure_railmux_session("railmux") == "$7"
    run.assert_not_called()


def test_display_lock_is_scoped_by_socket_and_session(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fast_display_server.restart_state, "runtime_state_dir", lambda: tmp_path
    )
    sockets = iter(("/tmp/server-a/railmux", "/tmp/server-b/railmux"))
    monkeypatch.setattr(
        fast_display_server, "_tmux_output", lambda *_args: next(sockets)
    )

    first = fast_display_server._acquire_display_lock("$0")
    fast_display_server._release_display_lock(first)
    second = fast_display_server._acquire_display_lock("$0")
    fast_display_server._release_display_lock(second)

    locks = sorted(path.name for path in tmp_path.glob("fast-display-*.lock"))
    assert len(locks) == 2
    assert all(name.endswith("-0.lock") for name in locks)


def test_display_lock_reports_busy_without_unlinking_live_owner(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        fast_display_server.restart_state, "runtime_state_dir", lambda: tmp_path
    )
    monkeypatch.setattr(
        fast_display_server, "_tmux_output", lambda *_args: "/tmp/tmux/railmux"
    )
    first = fast_display_server._acquire_display_lock("$0")
    try:
        with pytest.raises(fast_display_server.DisplayServerBusy):
            fast_display_server._acquire_display_lock("$0", timeout=0)
    finally:
        fast_display_server._release_display_lock(first)


def test_attach_confirmation_matches_exact_child_pid(monkeypatch):
    rows = iter(("$4 998\n", "$4 123\n"))
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "check_output",
        lambda *_args, **_kwargs: next(rows),
    )
    monkeypatch.setattr(fast_display_server.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(fast_display_server, "_child_exited", lambda _pid: False)

    assert fast_display_server._wait_until_attached("$4", 123, timeout=1.0) is True


def test_compact_resize_preparation_waits_for_exact_controller_ack(monkeypatch):
    outputs = iter(
        (
            "180 40 3 %8",
            "ready:0011223344556677:105:20",
        )
    )
    monkeypatch.setattr(
        fast_display_server,
        "_compact_tmux_output",
        lambda *_args: next(outputs),
    )
    set_option = MagicMock(return_value=True)
    monkeypatch.setattr(fast_display_server, "_set_compact_resize_option", set_option)
    sent = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(fast_display_server.subprocess, "run", sent)
    monkeypatch.setattr(
        fast_display_server.secrets,
        "token_hex",
        lambda _length: "0011223344556677",
    )
    progress = MagicMock()

    assert (
        fast_display_server._request_compact_resize_preparation(
            "$4",
            105,
            20,
            progress=progress,
        )
        == "ready:0011223344556677:105:20"
    )

    set_option.assert_called_once_with("$4", "request:0011223344556677:105:20")
    assert sent.call_args.args[0][-5:] == [
        "send-keys",
        "-l",
        "-t",
        "%8",
        "\x1b[34~",
    ]
    progress.assert_called_once_with()


def test_compact_resize_preparation_times_out_fail_open(monkeypatch):
    monkeypatch.setattr(
        fast_display_server,
        "_compact_tmux_output",
        lambda *_args: "180 40 3 %8",
    )
    set_option = MagicMock(return_value=True)
    clear = MagicMock()
    monkeypatch.setattr(fast_display_server, "_set_compact_resize_option", set_option)
    monkeypatch.setattr(fast_display_server, "_clear_compact_resize_option_if", clear)
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    monkeypatch.setattr(
        fast_display_server.secrets,
        "token_hex",
        lambda _length: "0011223344556677",
    )

    assert (
        fast_display_server._request_compact_resize_preparation(
            "$4",
            70,
            18,
            timeout=0,
        )
        is None
    )
    clear.assert_called_once_with("$4", "request:0011223344556677:70:18")


def test_noncompact_resize_never_contacts_controller(monkeypatch):
    output = MagicMock()
    monkeypatch.setattr(fast_display_server, "_compact_tmux_output", output)

    assert (
        fast_display_server._request_compact_resize_preparation("$4", 120, 30) is None
    )
    output.assert_not_called()


def test_server_window_size_policy_accepts_native_old_tmux(monkeypatch):
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1),
    )
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "tmux 2.8\n",
    )

    fast_display_server._use_smallest_window_size("$4")


def test_server_window_size_policy_fails_closed_on_modern_tmux(monkeypatch):
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1),
    )
    monkeypatch.setattr(
        fast_display_server.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "tmux 3.5a\n",
    )

    with pytest.raises(fast_display_server.DisplayServerError, match="multi-terminal"):
        fast_display_server._use_smallest_window_size("$4")


def test_server_window_size_policy_retries_a_transient_failure(monkeypatch):
    results = iter(
        (
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0),
        )
    )
    run = MagicMock(side_effect=lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(fast_display_server.subprocess, "run", run)
    monkeypatch.setattr(fast_display_server.time, "sleep", lambda _delay: None)

    fast_display_server._use_smallest_window_size("$4")

    assert run.call_count == 2


def test_server_does_not_auto_start_a_custom_missing_session(monkeypatch):
    monkeypatch.setattr(fast_display_server, "_try_session_id", lambda _session: None)

    with pytest.raises(fast_display_server.DisplayServerError, match="default"):
        fast_display_server._ensure_railmux_session("custom")


@pytest.mark.parametrize(
    ("resolved", "controller", "expected"),
    [
        (None, None, fast_display_server.RemoteExit.HARD_QUIT),
        ("$4", None, fast_display_server.RemoteExit.SOFT_QUIT),
        ("$4", "%8", fast_display_server.RemoteExit.DETACHED),
    ],
)
def test_server_classifies_remote_lifecycle(
    monkeypatch, resolved, controller, expected
):
    monkeypatch.setattr(
        fast_display_server, "_try_session_id", lambda _session: resolved
    )
    monkeypatch.setattr(
        fast_display_server, "_live_controller", lambda _session: controller
    )

    assert fast_display_server._classify_remote_exit("$4") is expected


def test_server_controller_snapshot_is_scoped_to_managed_session(monkeypatch):
    monkeypatch.setattr(
        fast_display_server,
        "_tmux_output",
        lambda *_args: (
            "$4\t%1\t0\t%1\n"
            "$4\t%8\t0\t%1"
        ),
    )

    assert fast_display_server._session_controller_pane("$4") == "%1"


@pytest.mark.parametrize(
    "snapshot",
    [
        "$5\t%1\t0\t%1",
        "$4\t%1\t1\t%1",
        "$4\t%1\t0\t%9",
        "$4\t%1\t0\t%1\n$4\t%8\t0\t%8",
        "$4\t%1\t0\tinvalid",
        "$4\t%1\t0",
    ],
)
def test_server_controller_snapshot_fails_closed(monkeypatch, snapshot):
    monkeypatch.setattr(
        fast_display_server,
        "_tmux_output",
        lambda *_args: snapshot,
    )

    assert fast_display_server._session_controller_pane("$4") is None


def test_observed_soft_quit_intent_skips_tmux_requery(monkeypatch):
    target = fast_display_server.tmux_server.TmuxServerTarget("/tmp/tmux/railmux", 123)
    intended = MagicMock(return_value=True)
    classify = MagicMock()
    consume = MagicMock()
    monkeypatch.setattr(fast_display_server.tmux_health, "soft_exit_intended", intended)
    monkeypatch.setattr(fast_display_server, "_classify_remote_exit", classify)
    monkeypatch.setattr(fast_display_server.tmux_health, "consume_clean_exit", consume)

    assert (
        fast_display_server._classify_observed_exit("$4", target)
        is fast_display_server.RemoteExit.SOFT_QUIT
    )

    intended.assert_called_once_with(server_pid=123, session_id="$4")
    classify.assert_not_called()
    consume.assert_not_called()


def test_observed_hard_quit_requires_matching_clean_exit(monkeypatch):
    target = fast_display_server.tmux_server.TmuxServerTarget("/tmp/tmux/railmux", 123)
    monkeypatch.setattr(
        fast_display_server.tmux_health, "soft_exit_intended", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        fast_display_server,
        "_classify_remote_exit",
        lambda _session: fast_display_server.RemoteExit.HARD_QUIT,
    )
    consume = MagicMock(return_value=True)
    record = MagicMock(return_value=True)
    monkeypatch.setattr(fast_display_server.tmux_health, "consume_clean_exit", consume)
    monkeypatch.setattr(fast_display_server.tmux_health, "record_incident", record)

    assert (
        fast_display_server._classify_observed_exit("$4", target)
        is fast_display_server.RemoteExit.HARD_QUIT
    )
    consume.assert_called_once_with(server_pid=123, session_id="$4")
    record.assert_not_called()


def test_observed_unexpected_tmux_loss_records_incident(monkeypatch):
    target = fast_display_server.tmux_server.TmuxServerTarget("/tmp/tmux/railmux", 123)
    monkeypatch.setattr(
        fast_display_server.tmux_health, "soft_exit_intended", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        fast_display_server,
        "_classify_remote_exit",
        lambda _session: fast_display_server.RemoteExit.HARD_QUIT,
    )
    monkeypatch.setattr(
        fast_display_server.tmux_health,
        "consume_clean_exit",
        lambda **_kwargs: False,
    )
    record = MagicMock(return_value=True)
    monkeypatch.setattr(fast_display_server.tmux_health, "record_incident", record)

    with pytest.raises(
        fast_display_server.DisplayServerError,
        match="disappeared unexpectedly",
    ):
        fast_display_server._classify_observed_exit("$4", target)

    record.assert_called_once_with(
        component="remote-display",
        reason="remote-display-server-exit",
        consecutive_failures=1,
    )


@pytest.mark.parametrize(
    "exit_kind",
    [fast_display_server.RemoteExit.SOFT_QUIT, fast_display_server.RemoteExit.DETACHED],
)
def test_observed_surviving_session_does_not_consume_clean_exit(
    monkeypatch,
    exit_kind,
):
    target = fast_display_server.tmux_server.TmuxServerTarget("/tmp/tmux/railmux", 123)
    monkeypatch.setattr(
        fast_display_server.tmux_health, "soft_exit_intended", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        fast_display_server,
        "_classify_remote_exit",
        lambda _session: exit_kind,
    )
    consume = MagicMock()
    monkeypatch.setattr(fast_display_server.tmux_health, "consume_clean_exit", consume)

    assert fast_display_server._classify_observed_exit("$4", target) is exit_kind
    consume.assert_not_called()


def test_remote_watchdog_records_only_after_consecutive_failures(monkeypatch):
    watchdog = fast_display_server.tmux_health.FailureWatchdog.starting(
        0.0, interval=5.0, failure_limit=3
    )
    monkeypatch.setattr(fast_display_server, "_tmux_output", lambda *_args: "")
    record = MagicMock(return_value=True)
    monkeypatch.setattr(fast_display_server.tmux_health, "record_incident", record)

    assert not fast_display_server._remote_watchdog_tripped(watchdog, "$4", 123, 5.0)
    assert not fast_display_server._remote_watchdog_tripped(watchdog, "$4", 123, 10.0)
    assert fast_display_server._remote_watchdog_tripped(watchdog, "$4", 123, 15.0)
    record.assert_called_once_with(
        component="remote-display",
        reason="remote-display-watchdog-timeout",
        consecutive_failures=3,
    )


def test_server_resolves_only_noncontroller_pane_under_pointer(monkeypatch):
    monkeypatch.setattr(
        fast_display_server,
        "_live_controller",
        lambda _session: pytest.fail("pane routing repeated controller probes"),
    )
    calls = []

    def list_panes(*args, **kwargs):
        calls.append((args, kwargs))
        return (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1 \n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 1     9001 %1 \n"
        )

    monkeypatch.setattr(
        subprocess,
        "check_output",
        list_panes,
    )

    assert (
        fast_display_server._pane_at_pointer("$4", 5, 5, claude_history_policy="ask")
        is None
    )
    pane = fast_display_server._pane_at_pointer(
        "$4", 40, 5, claude_history_policy="ask"
    )
    assert pane == fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        49,
        20,
        mouse_forwardable=True,
        route_generation=fast_display_server._route_generation("9001", "%8"),
    )
    assert len(calls) == 2


def test_server_rejects_incoherent_controller_identity_in_pane_snapshot(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1 \n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 1     9001 %9 \n"
        ),
    )

    assert fast_display_server._list_agent_panes(
        "$4", claude_history_policy="ask"
    ) == ()


def test_history_worker_topology_cache_reuses_one_gesture_snapshot(monkeypatch):
    fast_display_server._PANE_GEOMETRY_CACHE.clear()
    calls = 0

    def list_panes(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1 \n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 1     9001 %1 \n"
        )

    monkeypatch.setattr(subprocess, "check_output", list_panes)

    first = fast_display_server._list_agent_panes(
        "$4",
        claude_history_policy="ask",
        use_cache=True,
    )
    second = fast_display_server._list_agent_panes(
        "$4",
        claude_history_policy="ask",
        use_cache=True,
    )

    assert second == first
    assert calls == 1
    fast_display_server._PANE_GEOMETRY_CACHE.clear()


def test_server_projects_bounded_codex_history_generation(monkeypatch):
    marker = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1 \n"
            f"$4 @1 0 0 %8 108 31 0 49 20 0 0 1   {marker}  9001 %1 \n"
        ),
    )

    panes = fast_display_server._list_agent_panes("$4", claude_history_policy="ask")

    assert len(panes) == 1
    assert panes[0].history_generation == fast_display_server._history_generation(
        marker, "9001"
    )
    assert panes[0].history_generation != 0
    assert not panes[0].canonical_history


def test_server_restart_changes_history_generation(monkeypatch):
    marker = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1 \n"
            f"$4 @1 0 0 %8 108 31 0 49 20 0 0 1   {marker}  9001 %1 \n"
        ),
    )

    panes = fast_display_server._list_agent_panes(
        "$4",
        claude_history_policy="ask",
    )

    assert len(panes) == 1
    assert panes[0].history_generation == fast_display_server._history_generation(
        marker,
        "9001",
    )
    assert panes[0].history_generation != fast_display_server._history_generation(
        marker,
        "9002",
    )


def test_server_accepts_canonical_history_only_for_matching_transcript(
    monkeypatch,
    tmp_path,
):
    session_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    path = tmp_path / f"rollout-2026-08-04T02-49-33-{session_id}.jsonl"
    path.write_text('{"type":"response_item"}\n')
    transcript = fast_display_server.tmux_server.encode_transcript_source(
        "codex", session_id, path
    )
    assert transcript is not None
    generation = f"{tmux_ctl.RAILMUX_CANONICAL_HISTORY_PREFIX}{session_id}"
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1 \n"
            f"$4 @1 0 0 %8 108 31 0 49 20 10 0 1  "
            f"{transcript} {generation}  9001 %1 \n"
        ),
    )

    panes = fast_display_server._list_agent_panes("$4", claude_history_policy="ask")

    assert len(panes) == 1
    assert panes[0].canonical_history
    assert panes[0].history_generation != fast_display_server._history_generation(
        session_id
    )

    generation = (
        f"{tmux_ctl.RAILMUX_CANONICAL_HISTORY_PREFIX}"
        "019fc605-5188-7212-bc48-ea023fe8b73c"
    )
    mismatched = fast_display_server._list_agent_panes(
        "$4", claude_history_policy="ask"
    )

    assert len(mismatched) == 1
    assert not mismatched[0].canonical_history


@pytest.mark.parametrize(
    "legacy_prefix", tmux_ctl.RAILMUX_LEGACY_CANONICAL_HISTORY_PREFIXES
)
def test_server_released_canonical_marker_fails_back_to_raw(
    monkeypatch,
    tmp_path,
    legacy_prefix,
):
    session_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    path = tmp_path / f"rollout-2026-08-04T02-49-33-{session_id}.jsonl"
    path.write_text('{"type":"response_item"}\n')
    transcript = fast_display_server.tmux_server.encode_transcript_source(
        "codex", session_id, path
    )
    assert transcript is not None
    generation = f"{legacy_prefix}{session_id}"
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1 \n"
            f"$4 @1 0 0 %8 108 31 0 49 20 10 0 1  "
            f"{transcript} {generation}  9001 %1 \n"
        ),
    )

    panes = fast_display_server._list_agent_panes("$4", claude_history_policy="ask")

    assert len(panes) == 1
    assert not panes[0].canonical_history
    assert panes[0].history_generation == 0


def test_server_excludes_managed_shell_and_viewer_panes(monkeypatch):
    marker = fast_display_server.json.dumps(
        {
            "version": 1,
            "outer_session_id": "$4",
            "slot": "primary",
            "kind": "shell",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1 \n"
            f"$4 @1 0 0 %8 108 31 0 49 20 0 0 1    {marker} 9001 %1 \n"
        ),
    )

    assert (
        fast_display_server._list_agent_panes(
            "$4",
            claude_history_policy="ask",
        )
        == ()
    )


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (
            "$4 @1 1 1 %1 101 0 0 80 24 0 0 0     9001 %1 \n"
            "$4 @1 1 0 %8 108 31 0 49 20 0 0 0     9001 %1 \n",
            (),
        ),
        (
            "$4 @1 1 0 %1 101 0 0 30 20 0 0 0     9001 %1 \n"
            "$4 @1 1 1 %8 108 0 0 80 24 0 0 0     9001 %1 \n",
            (
                fast_display_server._PaneGeometry(
                    "%8",
                    0,
                    0,
                    80,
                    24,
                    route_generation=fast_display_server._route_generation(
                        "9001", "%8"
                    ),
                ),
            ),
        ),
        (
            "$4 @1 1 1 %1 101 0 0 80 24 0 0 0     9001 %1 \n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 0     9001 %1 \n",
            (),
        ),
    ],
)
def test_server_exposes_only_coherent_visible_panes_when_zoomed(
    monkeypatch, rows, expected
):
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: rows)

    assert (
        fast_display_server._list_agent_panes("$4", claude_history_policy="ask")
        == expected
    )


def test_server_maps_nested_history_to_exact_real_pane(monkeypatch):
    target = fast_display_server.tmux_server.TmuxServerTarget("/tmp/default", 44)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1 \n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 1 "
            '{"source":1}    9001 %1 \n'
        ),
    )
    monkeypatch.setattr(
        fast_display_server.tmux_server,
        "resolve_history_pane",
        lambda marker, **_kwargs: (target, "%2") if marker else None,
    )

    assert fast_display_server._list_agent_panes("$4", claude_history_policy="ask") == (
        fast_display_server._PaneGeometry(
            "%8",
            31,
            0,
            49,
            20,
            target,
            "%2",
            True,
            route_generation=fast_display_server._route_generation("44", "%2"),
        ),
    )


def test_server_recovers_exact_pre_v10_claude_transcript_from_binding(
    monkeypatch,
    tmp_path,
):
    session_id = "47fca075-9cb8-44fb-a314-d57ef2256ad9"
    transcript = tmp_path / "projects" / "-workspace" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    binding = fast_display_server.json.dumps(
        {
            "session_type": "claude",
            "key": session_id,
            "tmux_name": "cc-project-123",
            "cwd": "/workspace",
        }
    )

    backed, marker = fast_display_server._binding_transcript_source(binding)

    assert backed and marker is not None
    assert str(transcript) in marker


def test_server_rejects_ambiguous_pre_v10_claude_transcript(
    monkeypatch,
    tmp_path,
):
    session_id = "47fca075-9cb8-44fb-a314-d57ef2256ad9"
    for project in ("one", "two"):
        transcript = tmp_path / "projects" / project / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    binding = fast_display_server.json.dumps(
        {
            "session_type": "claude",
            "key": session_id,
            "tmux_name": "cc-project-123",
            "cwd": "/workspace",
        }
    )

    assert fast_display_server._binding_transcript_source(binding) == (
        False,
        None,
    )


def test_server_history_capture_preserves_sgr_but_filters_controls(monkeypatch):
    pane = fast_display_server._PaneGeometry("%8", 31, 0, 49, 2, mouse_forwardable=True)
    monkeypatch.setattr(fast_display_server, "_pane_at_pointer", lambda *args: pane)
    calls = []

    def fake_check_output(argv, **kwargs):
        calls.append((argv, kwargs))
        return (
            b"old\n"
            + b"\x1b[31;41mred"
            + b" " * 46
            + b"\x1b[0m\n"
            + b"\x1b]52;c;evil\x07visible\n"
        )

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    snapshot = fast_display_server.capture_history_snapshot("$4", 7, 40, 5, 2000)

    assert snapshot.pane_id == "%8"
    assert snapshot.mouse_forwardable is True
    assert b"old" in snapshot.lines[0]
    assert b"red" in snapshot.lines[1]
    assert b";31;" in snapshot.lines[1]
    pyte = __import__("pyte")
    styled = pyte.Screen(49, 1)
    pyte.ByteStream(styled).feed(snapshot.lines[1])
    assert styled.buffer[0][48].bg == "red"
    assert b"]52" not in snapshot.lines[2]
    assert b"visible" in snapshot.lines[2]
    assert calls[0][0][:11] == [
        "tmux",
        "-L",
        "railmux",
        "capture-pane",
        "-p",
        "-e",
        "-N",
        "-t",
        "%8",
        "-S",
        "-2000",
    ]
    assert calls[0][0][11:16] == [
        ";",
        "display-message",
        "-p",
        "-t",
        "%8",
    ]
    assert calls[0][0][16].startswith("RAILMUX-HISTORY-")
    assert not any(
        destructive in calls[0][0]
        for destructive in ("kill-pane", "kill-session", "resize-pane", "send-keys")
    )


def test_server_history_capture_honours_limits_above_the_old_4096_cap(
    monkeypatch,
):
    pane = fast_display_server._PaneGeometry("%8", 31, 0, 49, 2)
    lines = tuple(f"line-{index}".encode() for index in range(5001))
    calls = []
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **_kwargs: calls.append(argv) or b"\n".join(lines),
    )
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    snapshot = fast_display_server._capture_pane_history(object(), pane, 7, 5000)

    assert snapshot is not None
    assert len(snapshot.lines) == 5000
    assert snapshot.lines[0] == b"line-1"
    assert snapshot.lines[-1] == b"line-5000"
    assert calls[0][9:11] == ["-S", "-5000"]


def test_server_history_capture_uses_atomic_tmux_timeline_marker(monkeypatch):
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        49,
        2,
        history_size=3,
    )
    monkeypatch.setattr(fast_display_server.secrets, "token_hex", lambda _n: "fixed")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: (
            b"old-a\nold-b\nlive-a\nlive-b\nRAILMUX-HISTORY-fixed 12 2\n"
        ),
    )
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    snapshot = fast_display_server._capture_pane_history(
        object(),
        pane,
        7,
        300,
    )

    assert snapshot is not None
    assert snapshot.lines == (b"old-a", b"old-b", b"live-a", b"live-b")
    assert (snapshot.timeline_start, snapshot.timeline_end) == (10, 14)


def test_server_raw_styled_hot_and_deep_history_keep_codex_foreground(
    monkeypatch,
):
    pyte = pytest.importorskip("pyte")
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        40,
        2,
        history_size=398,
        transcript_source="codex-marker",
        transcript_backed=True,
        transcript_provider="codex",
        history_generation=17,
    )
    wrap_head = (
        b"\033[48;5;22m 1 \033[0m\033[38;5;2m\033[48;5;22m+export VERY_LONG_ADDITION="
    )
    wrap_continuation = (
        b"\033[39m   \033[0m\033[38;5;2m\033[48;5;22mcontinued-value\033[39m"
    )
    highlighted = (
        b"\033[48;5;22m 2 \033[0m\033[38;5;2m\033[48;5;22m+"
        b"\033[38;2;205;214;244mvalue = 1\033[39m"
    )
    monochrome = (
        # tmux omits the leading background because this physical row inherits
        # it from ``highlighted``. History parsing must keep that stream state
        # while still returning an independently paintable row.
        b" 3 \033[0m\033[38;5;2m\033[48;5;22m+#!/bin/bash\033[39m"
    )
    removed = b"\033[48;5;52m 4 \033[0m\033[38;5;1m\033[48;5;52m-removed = True\033[39m"
    removed_inherited = b" 5 \033[0m\033[38;5;1m\033[48;5;52m-removed = False\033[39m"
    ordinary = b"\033[0m ordinary output"
    raw = (
        b"\n".join(
            (
                *(f"old-{index}".encode() for index in range(99)),
                wrap_head,
                wrap_continuation,
                *(f"line-{index}".encode() for index in range(294)),
                highlighted,
                monochrome,
                removed,
                removed_inherited,
                ordinary,
            )
        )
        + b"\n"
    )
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: raw)
    transcript_rows = MagicMock()
    monkeypatch.setattr(fast_display_server, "_transcript_rows", transcript_rows)

    terminal = fast_display_server._extended_pyte(pyte)
    hot = fast_display_server._capture_pane_history(terminal, pane, 1, 300)
    deep = fast_display_server._capture_pane_history(terminal, pane, 2, 400)

    assert hot is not None and deep is not None
    assert not hot.transcript_backed and not deep.transcript_backed
    assert hot.transcript_available and deep.transcript_available
    assert hot.lines == deep.lines[-300:]
    assert b"continued-value" in hot.lines[0]
    transcript_rows.assert_not_called()

    def styled_row(row):
        screen = pyte.Screen(40, 1)
        pyte.ByteStream(screen).feed(row)
        return screen

    highlighted_screen = styled_row(deep.lines[-5])
    monochrome_screen = styled_row(deep.lines[-4])
    removed_screen = styled_row(deep.lines[-3])
    removed_inherited_screen = styled_row(deep.lines[-2])
    ordinary_screen = styled_row(deep.lines[-1])
    assert b"38;5;2" in deep.lines[-4]
    assert b"48;5;22" in deep.lines[-5]
    assert b"48;5;52" in deep.lines[-3]
    assert b"38;2;0;205;0" not in deep.lines[-4]
    highlighted_start = highlighted_screen.display[0].index("value")
    monochrome_start = monochrome_screen.display[0].index("#!/bin/bash")
    assert highlighted_screen.buffer[0][highlighted_start].fg == "cdd6f4"
    assert monochrome_screen.buffer[0][monochrome_start].fg == "00cd00"
    assert highlighted_screen.buffer[0][0].bg == "005f00"
    assert monochrome_screen.buffer[0][0].bg == "005f00"
    assert removed_screen.buffer[0][0].bg == "5f0000"
    assert removed_inherited_screen.buffer[0][0].bg == "5f0000"
    assert ordinary_screen.buffer[0][0].bg == "default"
    coloured_backgrounds = {
        char.bg
        for screen in (
            highlighted_screen,
            monochrome_screen,
            removed_screen,
            removed_inherited_screen,
        )
        for char in screen.buffer[0].values()
        if char.bg != "default"
    }
    assert coloured_backgrounds == {"005f00", "5f0000"}


def test_server_claude_history_uses_stable_transcript_suffix(monkeypatch):
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        20,
        2,
        mouse_forwardable=True,
        alternate_on=True,
        transcript_source="exact-marker",
        transcript_backed=True,
        claude_history_policy="local",
    )
    transcript_rows = tuple(f"transcript-{index}".encode() for index in range(500))
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"live-a\nlive-b\n",
    )
    monkeypatch.setattr(
        fast_display_server,
        "_transcript_rows",
        lambda *_args, **_kwargs: fast_display_server._TranscriptCacheEntry(
            (1, 2, 3, 4), transcript_rows, False
        ),
    )
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    hot = fast_display_server._capture_pane_history(object(), pane, 1, 300)
    deep = fast_display_server._capture_pane_history(object(), pane, 2, 400)

    assert hot is not None and deep is not None
    assert hot.transcript_backed and hot.more_available
    assert hot.lines == deep.lines[-300:]
    assert hot.lines[-2:] == (b"live-a", b"live-b")


def test_server_claude_history_drops_transcript_tail_shown_on_screen(monkeypatch):
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        24,
        4,
        mouse_forwardable=True,
        alternate_on=True,
        transcript_source="exact-marker",
        transcript_backed=True,
        claude_history_policy="local",
    )
    # The transcript wraps the newest answer at the pane edge while the live
    # Claude screen uses a hanging indent and styling for the same text.
    transcript_rows = (
        b"\x1b[0mold question here\x1b[0m",
        b"\x1b[0m\x1b[0m",
        b"\x1b[0m\xe2\x97\x8f first answer line alph",
        b"a beta gamma delta\x1b[0m",
        b"\x1b[0m  second answer line kept\x1b[0m",
    )
    live = (
        b"\xe2\x97\x8f first answer line",
        b"  \x1b[1malpha beta gamma delta\x1b[0m",
        b"  second answer line kept",
        b"\xe2\x9d\xaf",
    )
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"\n".join(live) + b"\n",
    )
    monkeypatch.setattr(
        fast_display_server,
        "_transcript_rows",
        lambda *_args, **_kwargs: fast_display_server._TranscriptCacheEntry(
            (1, 2, 3, 4), transcript_rows, False
        ),
    )
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    snapshot = fast_display_server._capture_pane_history(object(), pane, 1, 300)

    assert snapshot is not None
    assert snapshot.lines == (transcript_rows[0],) + live
    assert snapshot.timeline_end == 1 + pane.height


@pytest.mark.parametrize("codex_wrapping", [False, True])
def test_transcript_overlap_keeps_rows_without_credible_screen_match(codex_wrapping):
    rows = tuple(
        f"repeated status line number {index % 2}".encode() for index in range(8)
    )
    unrelated = (b"nothing in common with the transcript at all", b"")
    repeated = (b"repeated status line number 0", b"repeated status line number 1")
    short = (b"tiny", b"")

    seam = fast_display_server._transcript_screen_seam
    for screen in (unrelated, repeated, short):
        assert seam(rows, screen, codex_wrapping=codex_wrapping).transcript_rows == len(rows)
    assert seam((), unrelated, codex_wrapping=codex_wrapping).transcript_rows == 0


def test_transcript_overlap_rejects_disagreeing_anchors():
    rows = (
        b"first distinctive anchor row",
        b"filler " * 20,
        b"second distinctive anchor row",
    )
    screen = (b"second distinctive anchor row", b"first distinctive anchor row")

    assert fast_display_server._transcript_screen_seam(rows, screen).transcript_rows == 3


def test_transcript_overlap_ignores_screen_regions_rendered_differently():
    rows = (
        b"older message mentioning edited readme line",
        b"filler " * 20,
        b"truncated tool output",
        b"",
        b"newest message first distinctive row",
        b"newest message second distinctive row",
    )
    # A diff view tops the screen: one of its rows also occurs much earlier
    # in the transcript, disagreeing with the anchors in the newest message.
    screen = (
        b"older message mentioning edited readme line",
        b"+ added diff row only the screen shows",
        b"",
        b"newest message first distinctive row",
        b"newest message second distinctive row",
    )

    assert fast_display_server._transcript_screen_seam(rows, screen).transcript_rows == 3


@pytest.mark.parametrize(
    ("policy", "choice_required"),
    [("ask", True), ("native", False)],
)
def test_server_waits_for_local_choice_before_rendering_claude_transcript(
    monkeypatch,
    policy,
    choice_required,
):
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        20,
        2,
        mouse_forwardable=True,
        alternate_on=True,
        transcript_source="exact-marker",
        transcript_backed=True,
        claude_history_policy=policy,
    )
    transcript_rows = MagicMock()
    monkeypatch.setattr(fast_display_server, "_transcript_rows", transcript_rows)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"live-a\nlive-b\n",
    )
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    snapshot = fast_display_server._capture_pane_history(object(), pane, 1, 300)

    assert snapshot is not None
    assert snapshot.transcript_available
    assert snapshot.history_choice_required is choice_required
    assert not snapshot.transcript_backed
    transcript_rows.assert_not_called()


def test_server_codex_history_uses_canonical_transcript_after_rewind(
    monkeypatch,
):
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        40,
        2,
        mouse_forwardable=True,
        history_size=500,
        alternate_on=False,
        transcript_source="codex-marker",
        transcript_backed=True,
        transcript_provider="codex",
        history_generation=19,
        canonical_history=True,
    )
    canonical = (b"retained prompt", b"replacement answer")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"abandoned red interruption\nlive-a\nlive-b\n",
    )
    monkeypatch.setattr(
        fast_display_server,
        "_transcript_rows",
        lambda *_args, **_kwargs: fast_display_server._TranscriptCacheEntry(
            (1, 2, 3, 4), canonical, False
        ),
    )
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    snapshot = fast_display_server._capture_pane_history(object(), pane, 7, 100)

    assert snapshot is not None
    assert snapshot.transcript_backed
    assert not snapshot.history_choice_required
    assert snapshot.generation == 19
    assert b"retained prompt" in snapshot.lines
    assert snapshot.lines[-2:] == (b"live-a", b"live-b")
    assert all(b"abandoned red interruption" not in line for line in snapshot.lines)


def test_server_unreadable_claude_transcript_preserves_native_wheel_fallback(
    monkeypatch,
):
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        20,
        2,
        mouse_forwardable=True,
        alternate_on=True,
        transcript_source="unreadable-marker",
        transcript_backed=True,
        claude_history_policy="local",
    )
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"live-a\nlive-b\n",
    )
    monkeypatch.setattr(
        fast_display_server, "_transcript_rows", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    snapshot = fast_display_server._capture_pane_history(object(), pane, 1, 300)

    assert snapshot is not None
    assert snapshot.mouse_forwardable
    assert not snapshot.transcript_backed


def test_transcript_wrapper_preserves_combined_sgr_after_line_wrap():
    pyte = fast_display_server._extended_pyte(__import__("pyte"))

    rows, dropped, total_rows = fast_display_server._wrap_transcript_rows(
        pyte,
        "\033[0;31mabcd",
        2,
    )

    assert not dropped
    assert len(rows) == 2
    assert total_rows == 2
    assert b"\033[31m" in rows[1]


def test_transcript_cache_evicts_least_recent_file_width(monkeypatch, tmp_path):
    pyte = fast_display_server._extended_pyte(__import__("pyte"))
    monkeypatch.setattr(fast_display_server, "_TRANSCRIPT_CACHE_LIMIT", 2)
    fast_display_server._TRANSCRIPT_CACHE.clear()
    keys = []
    try:
        for suffix in ("1", "2", "3"):
            session_id = f"47fca075-9cb8-44fb-a314-d57ef2256ad{suffix}"
            path = tmp_path / f"{session_id}.jsonl"
            path.write_text(
                '{"type":"user","message":{"role":"user","content":"hello"}}\n'
            )
            marker = fast_display_server.tmux_server.encode_transcript_source(
                "claude", session_id, path
            )
            assert marker is not None
            assert (
                fast_display_server._transcript_rows(
                    pyte, marker, 40, allow_stale=False
                )
                is not None
            )
            keys.append((str(path), 40))

        assert tuple(fast_display_server._TRANSCRIPT_CACHE) == tuple(keys[-2:])
    finally:
        fast_display_server._TRANSCRIPT_CACHE.clear()


def test_prefetch_reuses_a_grown_transcript_only_briefly(
    monkeypatch, tmp_path,
):
    pyte = fast_display_server._extended_pyte(__import__("pyte"))
    session_id = "47fca075-9cb8-44fb-a314-d57ef2256ada"
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text('{"type":"user"}\n')
    marker = fast_display_server.tmux_server.encode_transcript_source(
        "claude", session_id, path
    )
    assert marker is not None
    now = [100.0]
    monkeypatch.setattr(fast_display_server.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        fast_display_server.transcript_renderer,
        "format_transcript",
        lambda stream, *_args, **_kwargs: iter((stream.getvalue(),)),
    )
    fast_display_server._TRANSCRIPT_CACHE.clear()
    fast_display_server._TRANSCRIPT_FORMAT_CACHE.clear()
    try:
        first = fast_display_server._transcript_rows(
            pyte, marker, 40, allow_stale=True
        )
        assert first is not None
        path.write_text('{"type":"user","reply":"new"}\n')
        now[0] += 1.0
        assert fast_display_server._transcript_rows(
            pyte, marker, 40, allow_stale=True
        ) is first
        # A new reply must reach the routing prefetch, which the first
        # wheel tick paints, once the short reuse window has passed.
        now[0] += 5.0
        fresh = fast_display_server._transcript_rows(
            pyte, marker, 40, allow_stale=True
        )
        assert fresh is not None and fresh is not first
        assert b"new" in b"".join(fresh.rows)
    finally:
        fast_display_server._TRANSCRIPT_CACHE.clear()
        fast_display_server._TRANSCRIPT_FORMAT_CACHE.clear()


def test_transcript_format_cache_reuses_exact_identity_per_width(
    monkeypatch, tmp_path,
):
    pyte = fast_display_server._extended_pyte(__import__("pyte"))
    session_id = "47fca075-9cb8-44fb-a314-d57ef2256ad9"
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text('{"type":"user"}\n')
    marker = fast_display_server.tmux_server.encode_transcript_source(
        "claude", session_id, path
    )
    assert marker is not None
    calls = []

    def format_transcript(*_args, **_kwargs):
        calls.append(True)
        return iter(("formatted transcript",))

    monkeypatch.setattr(
        fast_display_server.transcript_renderer,
        "format_transcript",
        format_transcript,
    )
    fast_display_server._TRANSCRIPT_CACHE.clear()
    fast_display_server._TRANSCRIPT_FORMAT_CACHE.clear()
    try:
        assert fast_display_server._transcript_rows(
            pyte, marker, 40, allow_stale=False
        ) is not None
        fast_display_server._TRANSCRIPT_CACHE.clear()
        assert fast_display_server._transcript_rows(
            pyte, marker, 40, allow_stale=False
        ) is not None
        assert len(calls) == 1
        # Claude history is wrapped for the pane, so a new width re-formats.
        assert fast_display_server._transcript_rows(
            pyte, marker, 10, allow_stale=False
        ) is not None
        assert len(calls) == 2

        path.write_text('{"type":"user","changed":true}\n')
        assert fast_display_server._transcript_rows(
            pyte, marker, 10, allow_stale=False
        ) is not None
        assert len(calls) == 3
    finally:
        fast_display_server._TRANSCRIPT_CACHE.clear()
        fast_display_server._TRANSCRIPT_FORMAT_CACHE.clear()


def test_transcript_format_cache_does_not_retain_oversized_projection(
    monkeypatch, tmp_path,
):
    pyte = fast_display_server._extended_pyte(__import__("pyte"))
    session_id = "47fca075-9cb8-44fb-a314-d57ef2256ad9"
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text('{"type":"user"}\n')
    marker = fast_display_server.tmux_server.encode_transcript_source(
        "claude", session_id, path
    )
    assert marker is not None
    calls = []

    def format_transcript(*_args, **_kwargs):
        calls.append(True)
        return iter(("too large",))

    monkeypatch.setattr(
        fast_display_server.transcript_renderer,
        "format_transcript",
        format_transcript,
    )
    monkeypatch.setattr(
        fast_display_server, "_TRANSCRIPT_FORMAT_CACHE_MAX_CHARS", 3,
    )
    fast_display_server._TRANSCRIPT_CACHE.clear()
    fast_display_server._TRANSCRIPT_FORMAT_CACHE.clear()
    try:
        assert fast_display_server._transcript_rows(
            pyte, marker, 40, allow_stale=False
        ) is not None
        assert fast_display_server._transcript_rows(
            pyte, marker, 10, allow_stale=False
        ) is not None
        assert len(calls) == 2
        assert not fast_display_server._TRANSCRIPT_FORMAT_CACHE
    finally:
        fast_display_server._TRANSCRIPT_CACHE.clear()
        fast_display_server._TRANSCRIPT_FORMAT_CACHE.clear()


def test_transcript_rows_render_codex_locator_with_codex_formatter(tmp_path):
    pyte = fast_display_server._extended_pyte(__import__("pyte"))
    session_id = "019fcaad-27a1-70c0-8029-8a9c7803fa6b"
    path = tmp_path / f"rollout-2026-08-04T02-49-33-{session_id}.jsonl"
    path.write_text(
        '{"type":"response_item","payload":{"type":"message",'
        '"role":"user","content":[{"type":"input_text",'
        '"text":"retained question"}]}}\n'
    )
    marker = fast_display_server.tmux_server.encode_transcript_source(
        "codex", session_id, path
    )
    assert marker is not None
    fast_display_server._TRANSCRIPT_CACHE.clear()
    try:
        entry = fast_display_server._transcript_rows(
            pyte, marker, 60, allow_stale=False
        )
        assert entry is not None
        assert any(b"retained question" in row for row in entry.rows)
    finally:
        fast_display_server._TRANSCRIPT_CACHE.clear()


def test_server_history_capture_truncates_to_newest_styled_byte_budget(
    monkeypatch,
):
    pane = fast_display_server._PaneGeometry("%8", 31, 0, 49, 2)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"oldest\nmiddle\nnewest\n",
    )
    line_bytes = fast_display_server._HISTORY_SNAPSHOT_RAW_BUDGET // 3 + 100
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(line + b"x" * line_bytes for line in lines),
    )

    snapshot = fast_display_server._capture_pane_history(object(), pane, 7, 20000)

    assert snapshot is not None
    assert len(snapshot.lines) == 2
    assert snapshot.lines[0].startswith(b"middle")
    assert snapshot.lines[1].startswith(b"newest")
    assert ServerMessageDecoder().feed(encode_history_snapshot(snapshot)) == [snapshot]


def test_server_captures_nested_history_from_real_pane_without_resizing(
    monkeypatch,
):
    target = fast_display_server.tmux_server.TmuxServerTarget("/tmp/default", 44)
    pane = fast_display_server._PaneGeometry("%8", 31, 0, 49, 2, target, "%2")
    monkeypatch.setattr(fast_display_server, "_pane_at_pointer", lambda *_args: pane)
    monkeypatch.setattr(
        fast_display_server.tmux_server,
        "target_is_live",
        lambda candidate, **_kwargs: candidate == target,
    )
    calls = []
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **_kwargs: calls.append(argv) or b"old\nnew\n",
    )

    snapshot = fast_display_server.capture_history_snapshot("$4", 7, 40, 5, 300)

    assert snapshot.pane_id == "%8"
    assert calls[0][:11] == [
        "tmux",
        "-S",
        "/tmp/default",
        "capture-pane",
        "-p",
        "-e",
        "-N",
        "-t",
        "%2",
        "-S",
        "-300",
    ]
    assert calls[0][11:16] == [
        ";",
        "display-message",
        "-p",
        "-t",
        "%2",
    ]
    assert calls[0][16].startswith("RAILMUX-HISTORY-")
    assert not any(
        item in calls[0]
        for item in ("resize-pane", "swap-pane", "send-keys", "kill-pane")
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--protocol", str(PROTOCOL_VERSION), "--width", "39", "--height", "24"],
        ["--protocol", str(PROTOCOL_VERSION), "--width", "80", "--height", "11"],
        [
            "--protocol",
            str(PROTOCOL_VERSION),
            "--width",
            "80",
            "--height",
            "24",
            "--fps",
            "61",
        ],
    ],
)
def test_server_rejects_unbounded_geometry_and_frame_rates(argv):
    with pytest.raises(SystemExit):
        parse_server_args(argv)


@dataclass(frozen=True)
class _Char:
    data: str = " "
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False


class _FakeScreen:
    lines = 1
    columns = 4
    _character_width = staticmethod(lambda value: 2 if value == "你" else 1)
    buffer = {
        0: {
            0: _Char("A", fg="red", bold=True),
            1: _Char("你"),
            # Defensive fixture: a non-conforming backend may repeat wide
            # glyph data in its physical continuation cell.
            2: _Char("你"),
            3: _Char("\x1b\x9b"),
        }
    }
    mode = {2004 << 5, 1004 << 5}


def test_server_renderer_preserves_wide_cells_and_filters_terminal_controls():
    rows = render_rows(_FakeScreen())

    assert len(rows) == 1
    rendered = rows[0]
    assert b"A" in rendered
    assert "你".encode() in rendered
    assert rendered.count("你".encode()) == 1
    assert b"\x1b\x1b" not in rendered
    assert "\x9b".encode() not in rendered
    assert "�".encode() in rendered
    assert rendered.endswith(b"\033[0m")


def test_server_renderer_keeps_legitimate_adjacent_cjk_once_per_glyph():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    screen = terminal.Screen(8, 1)
    terminal.ByteStream(screen).feed("基本通了".encode())

    rendered = render_rows(screen)[0].decode("utf-8", errors="replace")

    assert "基本通了" in rendered
    assert all(rendered.count(character) == 1 for character in "基本通了")


def test_server_renderer_omits_default_trailing_cells_after_row_clear():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    screen = terminal.Screen(240, 2)
    terminal.ByteStream(screen).feed(b"short")

    first, blank = render_rows(screen)

    assert b"short" in first
    assert len(first) < 80
    assert blank == b"\033[0m\033[0m"


def test_server_renderer_keeps_visible_styled_trailing_blanks():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    screen = terminal.Screen(40, 1)
    terminal.ByteStream(screen).feed(b"\033[41m   \033[0m")

    rendered = render_rows(screen)[0]

    assert b";41m   " in rendered


def test_terminal_model_preserves_fragmented_http_osc8_targets_in_rows():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    screen = terminal.Screen(24, 2)
    stream = fast_display_server.HyperlinkTrackingStream(
        screen,
        terminal.ByteStream(screen),
    )
    target = b"https://jirasw.nvidia.com/browse/TRT-28431"

    for chunk in (
        b"\033]8;i",
        b"d=issue;" + target[:20],
        target[20:] + b"\033",
        b"\\TRT-28431\033]8;;\033\\",
    ):
        stream.feed(chunk)

    rendered = render_rows(screen)[0]
    assert b"TRT-28431" in rendered
    assert b"\033]8;;" + target + b"\033\\" in rendered
    assert screen.buffer[0][0].hyperlink == target.decode()
    assert screen.buffer[0][8].hyperlink == target.decode()
    assert screen.buffer[0][9].hyperlink is None


def test_terminal_model_drops_unsafe_or_stale_osc8_targets():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    screen = terminal.Screen(8, 1)
    stream = fast_display_server.HyperlinkTrackingStream(
        screen,
        terminal.ByteStream(screen),
    )

    stream.feed(b"\033]8;;https://safe.test\007ok\033]8;;\007")
    stream.feed(b"\033[1Gplain")
    stream.feed(b"\033]8;;file:///tmp/nope\007x\033]8;;\007")

    rendered = render_rows(screen)[0]
    assert b"https://safe.test" not in rendered
    assert b"file:///tmp/nope" not in rendered
    assert all(screen.buffer[0][column].hyperlink is None for column in range(6))


def test_terminal_model_bounds_distinct_live_osc8_targets():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    screen = terminal.Screen(300, 1)
    stream = fast_display_server.HyperlinkTrackingStream(
        screen,
        terminal.ByteStream(screen),
    )

    for index in range(257):
        stream.feed(
            f"\033]8;;https://example.test/{index}\007x\033]8;;\007".encode()
        )

    assert len(screen._known_hyperlinks) == 256
    assert screen.buffer[0][255].hyperlink == "https://example.test/255"
    assert screen.buffer[0][256].hyperlink is None


def test_server_renderer_collapses_reported_repeated_cjk_physical_cells():
    text = "基本通了，但发现一个真 bug："
    cells = [
        _Char(character)
        for character in text
        for _physical_cell in range(2 if ord(character) > 127 else 1)
    ]

    class RepeatedCjkScreen:
        lines = 1
        columns = len(cells)
        _character_width = staticmethod(lambda value: 2 if ord(value) > 127 else 1)
        buffer = {0: {column: cell for column, cell in enumerate(cells)}}

    rendered = render_rows(RepeatedCjkScreen())[0].decode("utf-8", errors="replace")

    assert text in rendered
    assert all(
        rendered.count(character) == text.count(character) for character in set(text)
    )


def test_server_renderer_preserves_real_content_over_a_continuation_cell():
    class RepaintedScreen:
        lines = 1
        columns = 2
        _character_width = staticmethod(lambda value: 2 if value == "你" else 1)
        buffer = {0: {0: _Char("你"), 1: _Char("x")}}

    rendered = render_rows(RepaintedScreen())[0].decode("utf-8", errors="replace")

    assert "你x" in rendered


def test_transcript_wrapper_does_not_duplicate_cjk_full_width_cells():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    text = "基本通了，但发现一个真 bug："

    rows, dropped, total_rows = fast_display_server._wrap_transcript_rows(
        terminal, text, 80
    )

    assert not dropped
    assert total_rows == len(rows)
    rendered = b"".join(rows).decode("utf-8", errors="replace")
    assert text in rendered
    assert all(rendered.count(character) == text.count(character) for character in text)


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (b"\033[2S", ["11111", "44444", "     ", "     ", "55555"]),
        (b"\033[2T", ["11111", "     ", "     ", "22222", "55555"]),
    ],
)
def test_server_terminal_model_applies_parameterized_scroll_inside_margins(
    operation,
    expected,
):
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    screen = terminal.Screen(5, 5)
    stream = terminal.ByteStream(screen)
    for row, value in enumerate(b"12345", 1):
        stream.feed(f"\033[{row};1H".encode() + bytes((value,)) * 5)

    # Restrict scrolling to rows 2-4 and keep the cursor outside that region.
    # SU/SD operate on DECSTBM regardless of cursor position and must not move
    # the cursor; pyte 0.8.2 silently ignored both sequences.
    stream.feed(b"\033[2;4r\033[5;3H")
    screen.dirty.clear()
    stream.feed(operation)

    assert screen.display == expected
    assert (screen.cursor.x, screen.cursor.y) == (2, 4)
    assert screen.dirty == {1, 2, 3}


def test_server_terminal_model_repeats_character_with_current_style():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    with pytest.warns(DeprecationWarning):
        screen = terminal.DiffScreen(8, 1)
    stream = terminal.ByteStream(screen)

    stream.feed(b"\033[31m#\033[4b")

    assert screen.display == ["#####   "]
    assert [screen.buffer[0][column].fg for column in range(5)] == ["red"] * 5


def test_server_terminal_model_round_trips_indexed_colours_for_local_palette():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    screen = terminal.Screen(3, 1)
    stream = terminal.ByteStream(screen)

    stream.feed(b"\033[38;5;2;48;5;22mA\033[39;48;2;1;2;3mB\033[0;38;2;4;5;6mC")
    rendered = render_rows(screen)[0]

    assert b"38;5;2;48;5;22mA" in rendered
    assert b"39;48;2;1;2;3mB" in rendered
    assert b"38;2;4;5;6;49mC" in rendered
    assert b"38;2;0;205;0" not in rendered


@pytest.mark.parametrize("reset", [b"\033[22m", b"\033[0m", b"\033[m"])
def test_server_terminal_model_preserves_dim_and_resets_intensity(reset):
    terminal = fast_display_server._extended_pyte(pytest.importorskip("pyte"))
    screen = terminal.Screen(80, 1)
    stream = fast_display_server.HyperlinkTrackingStream(
        screen, terminal.ByteStream(screen)
    )
    # Line numbers and queued messages use faint default foreground. Keep
    # bold independent, OSC 8 intact, and colour operands out of SGR state.
    stream.feed(b"\033[1;2m42 \033[22mnormal \033[2m")
    stream.feed(b"\033]8;;https://example.com\033\\queued message\033]8;;\033\\")
    stream.feed(reset + b" ordinary \033[38;5;2mG\033[38;2;2;22;0mR")
    rendered = render_rows(screen)[0]
    copy = terminal.Screen(80, 1)
    fast_display_server.HyperlinkTrackingStream(
        copy, terminal.ByteStream(copy)
    ).feed(rendered)

    assert copy.buffer[0][0].dim and copy.buffer[0][0].bold
    assert not copy.buffer[0][3].dim and not copy.buffer[0][3].bold
    assert copy.buffer[0][10].dim
    assert copy.buffer[0][10].hyperlink == "https://example.com"
    assert not copy.buffer[0][25].dim
    assert not copy.buffer[0][34].dim
    assert not copy.buffer[0][35].dim
    assert copy.buffer[0] == screen.buffer[0]


def test_server_terminal_model_dim_only_repaint_changes_serialized_row():
    terminal = fast_display_server._extended_pyte(pytest.importorskip("pyte"))
    screen = terminal.Screen(20, 1)
    stream = terminal.ByteStream(screen)
    stream.feed(b"queued message")
    ordinary = render_rows(screen)
    stream.feed(b"\r\033[2mqueued message")
    dimmed = render_rows(screen)
    assert dimmed != ordinary
    stream.feed(b"\r\033[22mqueued message")
    assert render_rows(screen) == ordinary


def test_server_terminal_model_ignores_private_device_status_queries():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    screen = terminal.Screen(8, 2)

    terminal.ByteStream(screen).feed(b"before\033[?6nafter")

    assert "".join(screen.display).replace(" ", "").startswith("beforeafter")


def test_server_history_renderer_uses_extended_terminal_sequences():
    pyte = pytest.importorskip("pyte")
    rendered = fast_display_server._render_history_line(
        fast_display_server._extended_pyte(pyte),
        b"\033[31m#\033[4b\033[0m",
        8,
    )

    assert rendered.count(b"#") == 5
    assert b";31;" in rendered


def test_server_projects_only_allowlisted_private_terminal_modes():
    assert terminal_modes_for_screen(_FakeScreen()) == (
        TerminalMode.BRACKETED_PASTE | TerminalMode.FOCUS_EVENTS
    )

    class OtherModes:
        mode = {1000 << 5, 1006 << 5, 9999 << 5}

    assert terminal_modes_for_screen(OtherModes()) is TerminalMode.NONE


def test_transcript_rows_reopen_links_across_wraps_and_drop_unsafe_targets():
    pyte = fast_display_server._extended_pyte(__import__("pyte"))
    link = "\x1b]8;;https://example.com/a\x1b\\"
    close = "\x1b]8;;\x1b\\"
    text = f"{link}abcdef{close} \x1b]8;;file:///etc/passwd\x1b\\x{close}\n"

    rows, _dropped, _total = fast_display_server._wrap_transcript_rows(
        pyte, text, 4)

    encoded_link, encoded_close = link.encode(), close.encode()
    assert rows[0] == b"\x1b[0m" + encoded_link + b"abcd" + encoded_close + b"\x1b[0m"
    assert rows[1].startswith(b"\x1b[0m" + encoded_link + b"ef" + encoded_close)
    assert b"file:" not in b"".join(rows)
    assert fast_display_server._overlap_text(rows[0]) == "abcd"


@pytest.mark.parametrize(
    ("policy", "alternate_on", "history_size", "managed"),
    [
        ("local", True, 0, True),
        # Default: an unrewound live Codex pane keeps Codex's own wheel.
        ("native", True, 0, False),
        # --no-alt-screen Codex already has real tmux scrollback.
        ("local", False, 120, False),
    ],
)
def test_server_live_codex_history_follows_codex_history_policy(
    monkeypatch, policy, alternate_on, history_size, managed,
):
    pane = fast_display_server._PaneGeometry(
        "%8",
        31,
        0,
        40,
        2,
        mouse_forwardable=True,
        # The earlier geometry can describe a different screen and depth.
        history_size=0 if history_size else 10,
        alternate_on=not alternate_on,
        transcript_source="codex-marker",
        transcript_backed=True,
        transcript_provider="codex",
        codex_history_policy=policy,
        history_generation=21,
    )
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **_kwargs: (
            f"live-a\nlive-b\n{argv[-1].split()[0]} {history_size} 2 {int(alternate_on)}\n"
        ).encode(),
    )
    transcript_rows = MagicMock(
        return_value=fast_display_server._TranscriptCacheEntry(
            (1, 2, 3, 4), (b"\xe2\x80\xba earlier prompt", b"earlier answer"), False
        )
    )
    monkeypatch.setattr(fast_display_server, "_transcript_rows", transcript_rows)
    monkeypatch.setattr(
        fast_display_server,
        "_render_history_lines",
        lambda _pyte, lines, _width: tuple(lines),
    )

    snapshot = fast_display_server._capture_pane_history(object(), pane, 7, 100)

    assert snapshot is not None
    assert snapshot.transcript_available
    assert not snapshot.history_choice_required
    assert snapshot.transcript_backed is managed
    assert snapshot.lines[-2:] == (b"live-a", b"live-b")
    assert (b"earlier answer" in snapshot.lines) is managed
    assert transcript_rows.called is managed


def test_terminal_history_policies_reread_changed_settings(monkeypatch, tmp_path):
    from railmux import settings

    path = tmp_path / "config.toml"
    monkeypatch.setattr(fast_display_server, "default_config_path", lambda: path)
    monkeypatch.setattr(settings, "default_config_path", lambda: path)
    monkeypatch.setattr(fast_display_server, "_TERMINAL_HISTORY_SIGNATURE", None)
    monkeypatch.setattr(
        fast_display_server, "_TERMINAL_HISTORY_POLICIES", ("native", "local"))

    assert fast_display_server._terminal_history_policies() == ("native", "local")
    path.write_text('[interaction]\ncodex_history = "local"\ndevin_history = "native"\n')
    assert fast_display_server._terminal_history_policies() == ("local", "native")
    path.unlink()
    assert fast_display_server._terminal_history_policies() == ("native", "local")


def test_transcript_rows_render_live_codex_items_as_codex_tui(tmp_path):
    pyte = fast_display_server._extended_pyte(__import__("pyte"))
    session_id = "01a1ad19-810b-7222-b651-c68e2231556d"
    path = tmp_path / f"rollout-2026-11-05T19-04-52-{session_id}.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": "/w"}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "UserMessage",
            "content": [{"type": "text", "text": "retained question"}]}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "AgentMessage",
            "content": [{"type": "Text", "text": "See [docs](https://e.x/a)."}]}}},
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    marker = fast_display_server.tmux_server.encode_transcript_source(
        "codex", session_id, path
    )
    assert marker is not None
    fast_display_server._TRANSCRIPT_CACHE.clear()
    fast_display_server._TRANSCRIPT_FORMAT_CACHE.clear()
    try:
        entry = fast_display_server._transcript_rows(
            pyte, marker, 60, allow_stale=False
        )
        assert entry is not None
        visible = [
            fast_display_server._OVERLAP_STYLE_RE.sub(b"", row).decode()
            for row in entry.rows
        ]
        assert "› retained question" in visible
        answer = next(row for row in entry.rows if b"docs (" in row)
        assert b"\x1b]8;;https://e.x/a\x1b\\" in answer
    finally:
        fast_display_server._TRANSCRIPT_CACHE.clear()
        fast_display_server._TRANSCRIPT_FORMAT_CACHE.clear()


def test_transcript_rows_join_earlier_codex_rollout_segments(tmp_path):
    pyte = fast_display_server._extended_pyte(__import__("pyte"))
    thread = "01a19ab1-1a91-7651-9329-f25ee87872ab"
    segment = "01a1a037-5962-7af1-8dd6-9bdca787060e"
    other = "01a19ab1-1a91-7651-9329-f25ee87872ac"
    sessions = tmp_path / "sessions"

    def rollout(day: str, name: str, prompt: str) -> Path:
        path = sessions / "2026" / "11" / day / name
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"type": "session_meta", "payload": {"id": thread, "cwd": "/w"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "UserMessage",
                "content": [{"type": "text", "text": prompt}]}}},
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        return path

    rollout("02", f"rollout-2026-11-02T05-17-40-{thread}.jsonl", "first segment")
    rollout("02", f"rollout-2026-11-02T06-00-00-{other}.jsonl", "unrelated")
    newest = rollout(
        "03", f"rollout-2026-11-03T07-02-24-{thread}_{segment}.jsonl", "newest")
    marker = fast_display_server.tmux_server.encode_transcript_source(
        "codex", thread, newest)
    assert marker is not None
    assert fast_display_server.tmux_server.decode_transcript_source(marker) is not None
    for cache in (
        fast_display_server._TRANSCRIPT_CACHE,
        fast_display_server._TRANSCRIPT_FORMAT_CACHE,
        fast_display_server._CODEX_SEGMENT_CACHE,
    ):
        cache.clear()
    try:
        entry = fast_display_server._transcript_rows(
            pyte, marker, 60, allow_stale=False)
    finally:
        fast_display_server._TRANSCRIPT_CACHE.clear()
        fast_display_server._TRANSCRIPT_FORMAT_CACHE.clear()
        fast_display_server._CODEX_SEGMENT_CACHE.clear()

    assert entry is not None
    visible = [
        fast_display_server._OVERLAP_STYLE_RE.sub(b"", row).decode()
        for row in entry.rows
    ]
    assert visible.index("› first segment") < visible.index("› newest")
    assert "› unrelated" not in visible
    assert sum("OpenAI Codex" in row for row in visible) == 1


def test_managed_codex_and_devin_keep_styled_deep_history_and_native_input(
    monkeypatch, tmp_path,
):
    from railmux.fast_display_history import LocalHistoryView
    from railmux.fast_display_input import SgrMouseEvent
    from railmux.fast_display_protocol import (
        decode_history_prefetch,
        decode_history_request,
        encode_history_batch,
    )

    session_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    path = tmp_path / f"rollout-2026-08-04T02-49-33-{session_id}.jsonl"
    path.write_text('{"type":"response_item"}\n')
    server = fast_display_server
    locator = server.tmux_server.encode_transcript_source("codex", session_id, path)
    assert locator is not None
    captures = []

    def tmux_output(argv, **_kwargs):
        if "list-panes" in argv:
            return (
                "$4 @1 0 1 %1 101 0 0 30 18 0 0 0     9001 %1 \n"
                f"$4 @1 0 0 %8 108 31 0 40 18 850 0 1  {locator}   9001 %1 \n"
                "$4 @1 0 0 %9 109 72 0 40 18 850 0 0     9001 %1 devin\n"
            )
        pane_id = argv[argv.index("-t") + 1]
        captures.append(pane_id)
        depth = int(argv[argv.index("-S") + 1][1:])
        rows = [
            f"\x1b[2;38;2;31;89;211;48;5;221m{pane_id} row {i:04d}".encode()
            for i in range(868 - min(868, depth + 18), 868)
        ]
        nonce = argv[-1].split()[0]
        return b"\n".join((*rows, f"{nonce} 850 18 0".encode(), b""))

    monkeypatch.setattr(subprocess, "check_output", tmux_output)
    monkeypatch.setattr(server, "_transcript_rows", MagicMock(side_effect=AssertionError(
        "a locator must not displace real styled pane rows")))
    monkeypatch.setattr(server, "_terminal_history_policies", lambda: ("local", "local"))
    pyte = server._extended_pyte(__import__("pyte"))
    view = LocalHistoryView()

    def prefetch():
        frame = InputFrameDecoder().feed(view.begin_prefetch(time.monotonic(), force=True))[0]
        request_id, depth = decode_history_prefetch(frame.data)
        batch = server.capture_history_batch(pyte, "$4", request_id, depth)
        decoded, = ServerMessageDecoder().feed(encode_history_batch(batch))
        return view.accept_prefetch(decoded)

    prefetch()
    for pane_id, column in (("%8", 34), ("%9", 75)):
        wheel = SgrMouseEvent(f"\x1b[<64;{column};3M".encode(), 64, column, 3, True)
        action = view.wheel(wheel)
        assert action.render_history
        for _ in range(20):
            action = view.page(b"\x1b[5~", column - 1, 2)
            if action.protocol_frame:
                break
        message, = InputFrameDecoder().feed(action.protocol_frame)
        request_id, x, y, depth = decode_history_request(message.data)
        deep = server.capture_history_snapshot("$4", request_id, x, y, depth, pyte)
        assert deep.timeline_start == 0
        before = view.overlays()
        assert view.accept(deep).render_history
        assert view.overlays()[0][1] == before[0][1]
        assert deep.pane_id == pane_id and not deep.transcript_backed
        assert b"row 0000" in deep.lines[0]
        visible_row = view.overlays()[0][1][0]
        screen = pyte.Screen(40, 1)
        pyte.ByteStream(screen).feed(visible_row)
        assert screen.buffer[0][0].fg == "1f59d3"
        assert screen.buffer[0][0].bg != "default"
        assert screen.buffer[0][0].dim
        if pane_id == "%8":
            assert view.cancel_for_input(column - 1, 2)

    captures.clear()
    monkeypatch.setattr(server, "_terminal_history_policies", lambda: ("local", "native"))
    assert prefetch().restore_live
    assert captures == ["%8"]
    native = SgrMouseEvent(b"\x1b[<64;75;3M", 64, 75, 3, True)
    assert view.wheel(native).forwarded_input == native.raw
    assert view.page(b"\x1b[5~", 74, 2).forwarded_input == b"\x1b[5~"
    assert view.wheel(SgrMouseEvent(b"\x1b[<64;34;3M", 64, 34, 3, True)).render_history


def test_codex_history_keeps_the_preceding_prompt_when_hot_capture_starts_mid_answer(
    monkeypatch, tmp_path,
):
    from railmux.fast_display_protocol import ServerMessageDecoder

    server = fast_display_server
    session_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    path = tmp_path / f"rollout-2026-08-04T02-49-33-{session_id}.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": session_id}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "content": [{"type": "text", "text": "Question before the long answer"}]}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "AgentMessage", "content": [{
                "type": "Text", "text": "\n".join(f"Answer row {i:04d}" for i in range(400)),
            }]}}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    locator = server.tmux_server.encode_transcript_source("codex", session_id, path)
    assert locator is not None
    pane = server._PaneGeometry(
        "%8", 31, 0, 40, 6, mouse_forwardable=True, alternate_on=True,
        transcript_source=locator, transcript_backed=True,
        transcript_provider="codex", codex_history_policy="local",
    )
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_kw: b"\n" * 6)
    pyte = server._extended_pyte(__import__("pyte"))
    hot = server._capture_pane_history(pyte, pane, 1, 300)
    assert hot is not None and hot.more_available
    assert len(hot.lines) == 300 and hot.transcript_backed
    assert all(b"Question before" not in row for row in hot.lines)
    decoded, = ServerMessageDecoder().feed(encode_history_snapshot(hot))
    assert decoded.codex_prompt_context == "Question before the long answer"

    from dataclasses import replace

    native = server._capture_pane_history(
        pyte, replace(pane, codex_history_policy="native"), 2, 300)
    assert native is not None
    assert native.codex_prompt_context is None and not native.transcript_backed


@pytest.mark.parametrize("prompt_kind", ["pinned", "actual", "reply", "clipped_reply"])
def test_codex_local_history_stitches_body_without_a_second_prompt(
    monkeypatch, tmp_path, prompt_kind,
):
    from railmux.fast_display_history import LocalHistoryView
    from railmux.fast_display_input import SgrMouseEvent
    from railmux.fast_display_protocol import HistoryBatch, decode_history_prefetch

    server = fast_display_server
    session_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    path = tmp_path / f"rollout-2026-08-04T02-49-33-{session_id}.jsonl"
    prompt = "› Explain the differences in the two measurement runs."
    user_message = prompt[2:]
    width = 64
    if prompt_kind in ("reply", "clipped_reply"):
        question = "Which measurements?"
        response = "Use the same input."
        prompt = f"› > {question} {response}"
        if prompt_kind == "clipped_reply":
            prefix = "请核对这两次运行的结果并检查每一项测量是否一致"
            width = 52
            question = prefix + "并把结果记录下来。"
            prompt = f"› > {prefix}…"
        user_message = (
            "<send_user_message_question_reply>\n"
            + json.dumps([{"questionItemId": "item", "question": question, "answer": response}])
            + "\n</send_user_message_question_reply>"
        )
    answer = [f"Distinct comparison measurement from run {i:03d}." for i in range(320)]
    newest = ["The final comparison uses the same input.", "Both runs report a stable result."]
    messages = [("UserMessage", user_message), ("AgentMessage", "\n".join(answer))]
    if prompt_kind == "actual":
        # Codex can show a real user row at the top. Even an identical prompt
        # belongs in the timeline when it starts a separate turn.
        messages += [("UserMessage", prompt[2:]), ("AgentMessage", "\n".join(newest))]
        body = ["", f"• {newest[0]}", f"  {newest[1]}"]
    else:
        body = [f"  {row}" for row in answer[-5:]]
    path.write_text("".join(
        json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": kind, "content": [{"type": "text", "text": message}],
        }}}) + "\n"
        for kind, message in messages
    ))
    locator = server.tmux_server.encode_transcript_source("codex", session_id, path)
    assert locator is not None
    screen = "\n".join([prompt, *body, "", "› ", *([""] * (5 - len(body))), ""])
    monkeypatch.setattr(
        subprocess, "check_output", lambda *_a, **_kw: screen.encode(),
    )
    pane = server._PaneGeometry(
        "%8", 31, 0, width, 8, mouse_forwardable=True, alternate_on=True,
        transcript_source=locator, transcript_backed=True,
        transcript_provider="codex", codex_history_policy="local",
    )
    pyte = server._extended_pyte(__import__("pyte"))
    view = LocalHistoryView()
    request, = InputFrameDecoder().feed(view.begin_prefetch(time.monotonic()))
    request_id, limit = decode_history_prefetch(request.data)
    hot = server._capture_pane_history(pyte, pane, request_id, limit)
    deep = server._capture_pane_history(pyte, pane, 2, 400)
    assert hot is not None and deep is not None

    def visible(rows):
        return [server._OVERLAP_STYLE_RE.sub(b"", row).decode().strip() for row in rows]

    assert hot.lines == deep.lines[-limit:]
    assert hot.timeline_end == deep.timeline_end
    rows = visible(deep.lines)
    assert sum(row.startswith("› ") for row in rows) == (2 if prompt_kind == "actual" else 1)
    if prompt_kind == "actual":
        assert rows[rows.index(answer[-1]) + 1] == prompt
        return
    start = rows.index(answer[-6])
    assert rows[start:start + 6] == answer[-6:]

    view.accept_prefetch(HistoryBatch(request_id, (hot,)))
    wheel_up = SgrMouseEvent(b"\x1b[<64;34;3M", 64, 34, 3, True)
    assert view.wheel(wheel_up).render_history
    assert view.wheel(wheel_up).render_history
    overlay = visible(view.overlays()[0][1])
    assert overlay[0] == prompt and overlay.count(prompt) == 1
    assert overlay[1:] == answer[-8:-1]
    selection = view.selection_source(wheel_up, (), focused_pane_id=pane.pane_id)
    assert selection is not None and selection.rows[0] == b""


@pytest.mark.parametrize("layout", ["shared_boundary", "shifted_boundary", "native"])
def test_codex_local_history_stitches_rewrapped_table_links(monkeypatch, tmp_path, layout):
    server = fast_display_server
    session_id = "019fc7c1-a27c-7ae0-9937-7570552a112a"
    path = tmp_path / f"rollout-2026-08-04T02-49-33-{session_id}.jsonl"
    base = "https://example.test/validation/round-one/reviews/"
    summary = "请复核边界输入并查看当前测试记录，然后对比结果是否完全一致。这里还有一条要在发布前核查。"
    answer = (
        "Please check the following.\n"
        "| Item | Summary |\n"
        "| --- | --- |\n"
        f"| **Pending review in the original environment** | {summary} "
        f"[#123]({base}123) · [#456]({base}456) |\n"
        "| Follow-up | Separate validation uses a different configuration and still reproduces the issue. |"
    )
    path.write_text(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "AgentMessage", "content": [{"type": "text", "text": answer}],
    }}}) + "\n")
    locator = server.tmux_server.encode_transcript_source("codex", session_id, path)
    assert locator is not None
    # The visible native table starts mid-link and uses a different indent,
    # separator and column wrapping than the transcript. There may be no
    # common row boundary in the matching portion of the viewport.
    indent = " " * 39
    matching_end = ")" if layout != "shifted_boundary" else ""
    live_end = "  ─────────────────────  ──────────────────────────────────"
    if not matching_end:
        live_end = ")" + live_end
    live = tuple(row.encode() for row in (
        f"{indent}{base.removeprefix('https://')}123) · #456 (https://",
        f"{indent}{base.removeprefix('https://')}456{matching_end}",
        live_end,
        "  Follow-up    Separate validation uses a different configuration",
        "               and still reproduces the issue.",
        "› ",
    ))
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_kw: b"\n".join(live) + b"\n")
    pane = server._PaneGeometry(
        "%8", 31, 0, 96, len(live), mouse_forwardable=True, alternate_on=True,
        transcript_source=locator, transcript_backed=True, transcript_provider="codex",
        codex_history_policy="native" if layout == "native" else "local",
    )
    pyte = server._extended_pyte(__import__("pyte"))
    snapshot = server._capture_pane_history(pyte, pane, 1, 300)
    assert snapshot is not None
    rendered_live = server._render_history_lines(pyte, live, pane.width)
    if layout == "native":
        assert not snapshot.transcript_backed
        assert snapshot.lines == rendered_live
        return

    assert snapshot.transcript_backed
    visible = "".join(map(server._overlap_text, snapshot.lines))
    assert summary in visible
    assert visible.count(f"#123({base}123)·#456({base}456)") == 1
    assert visible.count("example.test/validation/round-one/reviews/") == 2
    for target in (123, 456):
        assert f"\x1b]8;;{base}{target}\x1b\\".encode() in b"".join(snapshot.lines)
    assert snapshot.lines[-4:] == rendered_live[-4:]

    hot = server._capture_pane_history(pyte, pane, 2, len(live) + 1)
    assert hot is not None
    assert hot.timeline_end == snapshot.timeline_end
    assert hot.lines == snapshot.lines[-len(hot.lines):]


def test_devin_local_history_strips_leaked_input_boxes_and_preserves_committed_content(
    monkeypatch,
):
    server = fast_display_server
    width, height = 80, 12
    rule = "─" * width
    titled_rule = "─" * 58 + " Session Title ─"
    md_divider = " " + "─" * (width - 1)
    raw_rows = [
        # Duplicate startup splash + Starting session input box (should be stripped).
        "  Devin CLI",
        "  v3000.14.30821",
        rule,
        "❭ first question",
        rule,
        "Gemini 4 Neon High                                    Starting session…",
        "",
        # Real committed splash + first committed prompt (must be kept).
        "  Devin CLI",
        "  v3000.14.30821",
        "",
        "❭ first question",
        "",
        " Here is section 1.",
        md_divider,
        # Leaked truncated streaming preview + mid-turn guidance + leaked footer.
        "[... 11 lines truncated (ctrl+o to expand) ...]",
        " ✓ Read ./notes/README.md · 39 lines",
        "❭ mid-turn guidance",
        "",
        "⠐⠒ Thinking · 1m 20s (esc twice to interrupt)",
        titled_rule,
        "❭ Guide Devin while it works",
        rule,
        "Gemini 4 Neon High · bypass               Context: 120k / 1.0M tokens",
        # Committed block repeating the tool call and mid-turn prompt (must be kept).
        " ✓ Read ./notes/README.md · 39 lines",
        "",
        "❭ mid-turn guidance",
        "",
        " Here is the real answer.",
        # Standalone leaked footer without truncation marker (should be stripped).
        "⠦⠀ Typing · 2m 01s (esc twice to interrupt)",
        titled_rule,
        "❭ draft text",
        rule,
        "Gemini 4 Neon High · bypass               ctrl+o details · alt+t thinking",
        " Final committed paragraph.",
        # Live viewport (last 12 rows): MUST remain untouched including live footer.
        " ✓ Running verification",
        "",
        " Live line 1",
        " Live line 2",
        " Live line 3",
        " Live line 4",
        " Live line 5",
        "⠀⠸ Running tools · 10s (esc twice to interrupt)",
        titled_rule,
        "❭ Guide Devin while it works",
        rule,
        "Gemini 4 Neon High · bypass               Context: 140k / 1.0M tokens",
    ]
    history_size = len(raw_rows) - height

    def fake_capture(argv, **_kwargs):
        nonce = argv[-1].split()[0]
        encoded = [row.encode("utf-8") for row in raw_rows]
        return b"\n".join((*encoded, f"{nonce} {history_size} {height} 0".encode(), b""))

    monkeypatch.setattr(subprocess, "check_output", fake_capture)
    pyte = server._extended_pyte(__import__("pyte"))

    # Positive case: Devin pane strips leaked scrollback frames while keeping live viewport.
    devin_pane = server._PaneGeometry(
        "%9", 31, 0, width, height, history_size=history_size, devin_pane=True,
    )
    snap = server._capture_pane_history(pyte, devin_pane, 1, 2000)
    assert snap is not None
    plain_all = [server._devin_plain_row(row) for row in snap.lines]
    plain_scrollback = plain_all[:-height]
    plain_live = plain_all[-height:]
    assert plain_scrollback.count("  Devin CLI") == 1
    assert plain_scrollback.count("❭ first question") == 1
    assert plain_scrollback.count("❭ mid-turn guidance") == 1
    assert md_divider in plain_scrollback
    assert " Final committed paragraph." in plain_scrollback
    assert not any("Starting session" in r for r in plain_scrollback)
    assert not any("Guide Devin while it works" in r for r in plain_scrollback)
    assert not any("truncated (ctrl+o to expand)" in r for r in plain_scrollback)
    assert not any("draft text" in r for r in plain_scrollback)
    assert not any("Context:" in r for r in plain_scrollback)
    # Live viewport still contains the active input box untouched.
    assert any("Guide Devin while it works" in r for r in plain_live)
    assert snap.timeline_start == 0 and snap.timeline_end == 0
    assert snap.more_available is False

    # Negative preservation: non-Devin pane keeps raw scrollback unchanged.
    plain_pane = server._PaneGeometry(
        "%8", 31, 0, width, height, history_size=history_size, devin_pane=False,
    )
    raw_snap = server._capture_pane_history(pyte, plain_pane, 2, 2000)
    assert raw_snap is not None
    assert len(raw_snap.lines) == len(raw_rows)
    assert raw_snap.timeline_start == 0 and raw_snap.timeline_end == len(raw_rows)


def test_devin_sanitizer_never_drops_unique_user_prompt():
    server = fast_display_server
    width = 80
    rule = "─" * width
    rows = [
        row.encode("utf-8")
        for row in (
            "[... 5 lines truncated (ctrl+o to expand) ...]",
            " partial line",
            "❭ unique user prompt that never repeats below",
            "⠐⠒ Thinking · 10s (esc twice to interrupt)",
            rule,
            "❭ Guide Devin while it works",
            rule,
            "Gemini 4 Neon High · bypass               Context: 50k / 1.0M tokens",
            " some unrelated output after",
        )
    ]
    cleaned = [
        server._devin_plain_row(r)
        for r in server._sanitize_devin_scrollback(rows, 20)
    ]
    # The unique user prompt MUST survive; only the leaked spinner/footer box is stripped.
    assert "❭ unique user prompt that never repeats below" in cleaned
    assert not any("Guide Devin while it works" in r for r in cleaned)


def test_devin_sanitizer_strips_repeated_btw_side_panels_while_keeping_main_chat():
    server = fast_display_server
    width = 96
    btw_top = "─" * 82 + " /btw 3/3 ──"
    bottom_rule = "─" * width
    rows = [
        row.encode("utf-8")
        for row in (
            "❭ main question before btw",
            " Main agent paragraph 1.",
            "[... 5 lines truncated (ctrl+o to expand) ...]",
            " ● cat tracker.json",
            "",
            "⠀⠸ Running tools · 70m 30s",
            btw_top,
            "❭ side question inside btw panel",
            "",
            *(f" btw answer line {i}" for i in range(1, 22)),
            "❭ Ask a /btw…",
            bottom_rule,
            "↵ ask · ↑↓ scroll/history · esc back to chat",
            " Main agent paragraph 2 committed after btw.",
        )
    ]
    cleaned = [
        server._devin_plain_row(r)
        for r in server._sanitize_devin_scrollback(rows, 38)
    ]
    assert "❭ main question before btw" in cleaned
    assert " Main agent paragraph 1." in cleaned
    assert " Main agent paragraph 2 committed after btw." in cleaned
    assert not any("/btw 3/3" in r for r in cleaned)
    assert not any("Ask a /btw" in r for r in cleaned)
    assert not any("esc back to chat" in r for r in cleaned)
    assert not any("truncated (ctrl+o to expand)" in r for r in cleaned)

    # Shallow capture boundary cutting mid-/btw panel drops the orphaned tail too.
    truncated_prefix = [
        row.encode("utf-8")
        for row in (
            " partial btw answer line cut at capture top",
            "❭ Ask a /btw…",
            bottom_rule,
            "↵ ask · ↑↓ scroll/history · esc back to chat",
            " Real main conversation resumes cleanly.",
        )
    ]
    cleaned_prefix = [
        server._devin_plain_row(r)
        for r in server._sanitize_devin_scrollback(truncated_prefix, 38)
    ]
    assert cleaned_prefix == [" Real main conversation resumes cleanly."]
