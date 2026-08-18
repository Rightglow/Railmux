from __future__ import annotations

import inspect
import io
import select
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from railmux.config import Config
from railmux.fast_display_history import HistoryCaptureJob, HistoryCaptureWorker
from railmux.fast_display_protocol import (
    HistorySnapshot,
    PROTOCOL_VERSION,
    REMOTE_CONFIG_PROTOCOL,
    REMOTE_ATTACH_BUSY,
    REMOTE_START,
    ServerMessageDecoder,
    TerminalMode,
    encode_history_snapshot,
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
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1\n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 1     9001 %1\n"
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
        "%8", 31, 0, 49, 20, mouse_forwardable=True
    )
    assert len(calls) == 2


def test_server_rejects_incoherent_controller_identity_in_pane_snapshot(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: (
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1\n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 1     9001 %9\n"
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
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1\n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 1     9001 %1\n"
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
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1\n"
            f"$4 @1 0 0 %8 108 31 0 49 20 0 0 1   {marker}  9001 %1\n"
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
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1\n"
            f"$4 @1 0 0 %8 108 31 0 49 20 0 0 1   {marker}  9001 %1\n"
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
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1\n"
            f"$4 @1 0 0 %8 108 31 0 49 20 10 0 1  "
            f"{transcript} {generation}  9001 %1\n"
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
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1\n"
            f"$4 @1 0 0 %8 108 31 0 49 20 10 0 1  "
            f"{transcript} {generation}  9001 %1\n"
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
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1\n"
            f"$4 @1 0 0 %8 108 31 0 49 20 0 0 1    {marker} 9001 %1\n"
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
            "$4 @1 1 1 %1 101 0 0 80 24 0 0 0     9001 %1\n"
            "$4 @1 1 0 %8 108 31 0 49 20 0 0 0     9001 %1\n",
            (),
        ),
        (
            "$4 @1 1 0 %1 101 0 0 30 20 0 0 0     9001 %1\n"
            "$4 @1 1 1 %8 108 0 0 80 24 0 0 0     9001 %1\n",
            (fast_display_server._PaneGeometry("%8", 0, 0, 80, 24),),
        ),
        (
            "$4 @1 1 1 %1 101 0 0 80 24 0 0 0     9001 %1\n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 0     9001 %1\n",
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
            "$4 @1 0 1 %1 101 0 0 30 20 0 0 0     9001 %1\n"
            "$4 @1 0 0 %8 108 31 0 49 20 0 0 1 "
            '{"source":1}    9001 %1\n'
        ),
    )
    monkeypatch.setattr(
        fast_display_server.tmux_server,
        "resolve_history_pane",
        lambda marker, **_kwargs: (target, "%2") if marker else None,
    )

    assert fast_display_server._list_agent_panes("$4", claude_history_policy="ask") == (
        fast_display_server._PaneGeometry("%8", 31, 0, 49, 20, target, "%2", True),
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


def test_transcript_format_cache_reuses_exact_identity_across_widths(
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
        assert fast_display_server._transcript_rows(
            pyte, marker, 10, allow_stale=False
        ) is not None
        assert len(calls) == 1

        path.write_text('{"type":"user","changed":true}\n')
        assert fast_display_server._transcript_rows(
            pyte, marker, 20, allow_stale=False
        ) is not None
        assert len(calls) == 2
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
