from __future__ import annotations

import io
import os
import subprocess
import time
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from railmux.fast_display_protocol import (
    DISPLAY_MAGIC,
    PROTOCOL_VERSION,
    REMOTE_ATTACH_ACCEPTED,
    REMOTE_ATTACH_BUSY,
    REMOTE_HELLO_PREFIX,
    REMOTE_START,
    RemoteExit,
    TerminalMode,
)
from railmux import fast_display_client, fast_display_server, ssh_preflight
from railmux.fast_display_client import (
    AppliedScreen,
    LOCAL_ESCAPE,
    RemoteHello,
    RemoteAttachKind,
    RemoteLaunchMode,
    RemoteStartKind,
    RemoteStartup,
    TerminalSurface,
    build_remote_command_argv,
    build_ssh_argv,
    build_ssh_install_argv,
    build_ssh_private_venv_install_argv,
    await_remote_startup,
    parse_args as parse_client_args,
    parse_remote_hello,
    prepare_remote_process,
    remote_install_help,
)
from railmux.fast_display_input import (
    SgrMouseEvent,
)


def _patch_preflight(monkeypatch, name, value):
    """Patch the extracted owner and the client's compatibility re-export."""
    monkeypatch.setattr(ssh_preflight, name, value)
    monkeypatch.setattr(fast_display_client, name, value)


def test_full_window_ssh_command_uses_railmux_remote_subcommand_and_protocol():
    argv = build_ssh_argv(
        "server",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=("-J", "jump"),
    )

    assert argv[:9] == [
        "ssh",
        "-J",
        "jump",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=3",
        "-T",
        "server",
    ]
    assert "then exec railmux remote-server" in argv[-1]
    assert f"--protocol {PROTOCOL_VERSION}" in argv[-1]
    assert "python3 -m railmux remote-server" in argv[-1]
    assert '"$HOME/.local/share/railmux/ssh-venv/bin/python"' in argv[-1]
    assert "--session 'rail mux'" in argv[-1]
    assert "--width 120 --height 40 --fps 20.0" in argv[-1]


def test_windows_remote_command_uses_shell_neutral_direct_launch():
    argv = build_ssh_argv(
        "windows-server",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
        launch_mode=RemoteLaunchMode.DIRECT,
    )

    assert argv[-1].startswith("railmux remote-server ")
    assert "if [" not in argv[-1]
    assert "--session 'rail mux'" in argv[-1]


def test_full_window_ssh_keepalive_defaults_follow_user_overrides():
    argv = build_ssh_argv(
        "server",
        session="railmux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=("-o", "ServerAliveInterval=20"),
    )

    user_interval = argv.index("ServerAliveInterval=20")
    default_interval = argv.index("ServerAliveInterval=5")
    assert user_interval < default_interval < argv.index("server")


def test_takeover_flag_is_private_remote_server_argument():
    argv = build_ssh_argv(
        "server",
        session="railmux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
        replace_existing_client=True,
    )

    assert "--replace-existing-client" in argv[-1]


def test_existing_session_only_flag_is_private_remote_server_argument():
    argv = build_ssh_argv(
        "server",
        session="railmux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
        existing_session_only=True,
    )

    assert "--existing-session-only" in argv[-1]


def test_remote_install_command_uses_user_pip_then_matching_python_module():
    argv = build_ssh_install_argv(
        "server",
        version="1.2.3",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=("-J", "jump"),
    )

    assert argv[:5] == ["ssh", "-J", "jump", "-T", "server"]
    assert "python3 -m pip --version" in argv[-1]
    assert "python3 -m pip install --user --upgrade" in argv[-1]
    assert "'railmux[ssh]==1.2.3'" in argv[-1]
    assert "pip3 install --user --upgrade" in argv[-1]
    assert "&& exec python3 -m railmux remote-server" in argv[-1]
    assert '"$HOME/.local/share/railmux/ssh-venv/bin/python" -m pip' in argv[-1]
    assert "sudo" not in argv[-1]


def test_generated_remote_bootstrap_and_install_commands_are_posix_shell_syntax():
    bootstrap = build_ssh_argv(
        "server",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
    )[-1]
    installer = build_ssh_install_argv(
        "server",
        version="1.2.3",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
    )[-1]
    private_installer = build_ssh_private_venv_install_argv(
        "server",
        version="1.2.3",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
    )[-1]

    for command in (bootstrap, installer, private_installer):
        result = subprocess.run(
            ["/bin/sh", "-n", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode()


def test_private_remote_install_creates_managed_venv_without_sudo():
    argv = build_ssh_private_venv_install_argv(
        "server",
        version="1.2.3",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=("-J", "jump"),
    )

    assert argv[:5] == ["ssh", "-J", "jump", "-T", "server"]
    command = argv[-1]
    assert 'python3 -m venv "$HOME/.local/share/railmux/ssh-venv"' in command
    assert '"$HOME/.local/share/railmux/ssh-venv"/bin/python' in command
    assert "'railmux[ssh]==1.2.3'" in command
    assert "--user" not in command
    assert "sudo" not in command


@pytest.mark.parametrize(
    ("installer", "builder_name", "status_fragment"),
    (
        (
            fast_display_client._install_remote_and_start,
            "build_ssh_install_argv",
            "remote user environment",
        ),
        (
            fast_display_client._install_remote_private_venv_and_start,
            "build_ssh_private_venv_install_argv",
            "isolated remote environment",
        ),
    ),
)
def test_remote_install_waits_300_seconds_and_reports_the_bounded_stage(
    monkeypatch, capsys, installer, builder_name, status_fragment
):
    process = _PreflightProcess()
    monkeypatch.setattr(
        fast_display_client, builder_name, lambda *_args, **_kwargs: ["ssh"]
    )
    monkeypatch.setattr(fast_display_client, "_spawn_remote", lambda _argv: process)
    awaited = MagicMock(return_value=RemoteStartup(RemoteStartKind.TIMEOUT))
    monkeypatch.setattr(fast_display_client, "await_remote_startup", awaited)

    selected, startup = installer(
        parse_client_args(["server"]),
        os.terminal_size((120, 40)),
        "1.2.3",
    )

    assert selected is process
    assert startup.kind is RemoteStartKind.TIMEOUT
    awaited.assert_called_once_with(
        process, timeout=fast_display_client._REMOTE_INSTALL_TIMEOUT
    )
    stderr = capsys.readouterr().err
    assert status_fragment in stderr
    assert "up to 300 seconds" in stderr


def test_remote_install_help_is_exact_and_has_source_fallback():
    help_text = remote_install_help("server", "1.2.3")

    assert "python3 -m pip install --user --upgrade" in help_text
    assert "'railmux[ssh]==1.2.3'" in help_text
    assert "~/.local/share/railmux/ssh-venv/bin/python" in help_text
    assert "do not modify the system Python" in help_text
    assert "matching wheel or source checkout" in help_text


def test_remote_hello_is_strictly_bounded_and_typed():
    hello = parse_remote_hello(
        REMOTE_HELLO_PREFIX
        + b'{"protocol":6,"ready":true,"tmux":true,"version":"1.2.3"}\n'
    )

    assert hello == RemoteHello("1.2.3", 6, True)
    configured = parse_remote_hello(
        REMOTE_HELLO_PREFIX + b'{"config_status":"invalid","protocol":6,"ready":true,'
        b'"tmux":false,"tmux_configured":true,"version":"1.2.3"}\n'
    )
    assert configured.config_status == "invalid"
    assert configured.tmux_configured is True
    windows = parse_remote_hello(
        REMOTE_HELLO_PREFIX + b'{"platform":"windows-msys2","protocol":6,"ready":true,'
        b'"tmux":true,"version":"1.2.3"}\n'
    )
    assert windows.platform == "windows-msys2"
    with pytest.raises(ValueError):
        parse_remote_hello(
            REMOTE_HELLO_PREFIX + b'{"protocol":true,"ready":true,"tmux":true,'
            b'"version":"1.2.3"}\n'
        )
    with pytest.raises(ValueError):
        parse_remote_hello(REMOTE_HELLO_PREFIX + b"not-json\n")
    with pytest.raises(ValueError):
        parse_remote_hello(
            REMOTE_HELLO_PREFIX + b'{"platform":"windows","protocol":6,"ready":true,'
            b'"tmux":true,"version":"1.2.3"}\n'
        )


def test_remote_startup_wait_reads_hello_before_raw_mode():
    script = (
        "import sys; "
        "sys.stdout.buffer.write("
        f'b\'RAILMUX-REMOTE/1 {{"protocol":{PROTOCOL_VERSION},'
        '"ready":true,"tmux":true,'
        '"version":"1.2.3"}\\n\'); '
        "sys.stdout.buffer.flush()"
    )
    process = subprocess.Popen(
        [fast_display_client.sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    startup = await_remote_startup(process, timeout=2.0)
    process.wait(timeout=2.0)

    assert startup == RemoteStartup(
        RemoteStartKind.HELLO,
        RemoteHello("1.2.3", PROTOCOL_VERSION, True),
    )


def test_remote_startup_tolerates_a_non_newline_shell_banner():
    script = (
        "import sys; "
        "sys.stdout.buffer.write("
        f'b\'banner: RAILMUX-REMOTE/1 {{"protocol":{PROTOCOL_VERSION},'
        '"ready":true,'
        '"tmux":true,"version":"1.2.3"}\\n\'); '
        "sys.stdout.buffer.flush()"
    )
    process = subprocess.Popen(
        [fast_display_client.sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    startup = await_remote_startup(process, timeout=2.0)
    process.wait(timeout=2.0)

    assert startup == RemoteStartup(
        RemoteStartKind.HELLO,
        RemoteHello("1.2.3", PROTOCOL_VERSION, True),
    )


def test_remote_startup_rejects_an_old_wire_protocol_without_timing_out():
    process = subprocess.Popen(
        [
            fast_display_client.sys.executable,
            "-c",
            "import sys,time;sys.stdout.buffer.write(b'RMUXD5\\0');"
            "sys.stdout.buffer.flush();time.sleep(5)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        started = time.monotonic()
        startup = await_remote_startup(process, timeout=2.0)

        assert startup == RemoteStartup(RemoteStartKind.FAILED)
        assert time.monotonic() - started < 1.0
    finally:
        process.terminate()
        process.wait(timeout=2.0)


@pytest.mark.parametrize("waiter", ["hello", "attach"])
def test_reconnect_handshake_waits_are_locally_cancellable(waiter):
    process = subprocess.Popen(
        [
            fast_display_client.sys.executable,
            "-c",
            "import time; time.sleep(5)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, LOCAL_ESCAPE)
        with pytest.raises(fast_display_client.ReconnectCancelled) as exc:
            if waiter == "hello":
                await_remote_startup(process, timeout=2.0, cancel_fd=read_fd)
            else:
                fast_display_client.await_remote_attach_status(
                    process, timeout=2.0, cancel_fd=read_fd
                )
        assert exc.value.exit_code == 0
    finally:
        os.close(read_fd)
        os.close(write_fd)
        process.terminate()
        process.wait(timeout=2.0)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (REMOTE_ATTACH_ACCEPTED, RemoteAttachKind.ACCEPTED),
        (REMOTE_ATTACH_BUSY, RemoteAttachKind.BUSY),
    ],
)
def test_remote_attach_status_stops_at_line_before_display_frames(
    status,
    expected,
):
    script = (
        "import sys; "
        f"sys.stdout.buffer.write({status!r} + {DISPLAY_MAGIC!r}); "
        "sys.stdout.buffer.flush()"
    )
    process = subprocess.Popen(
        [fast_display_client.sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    assert (
        fast_display_client.await_remote_attach_status(process, timeout=2.0) is expected
    )
    assert os.read(process.stdout.fileno(), len(DISPLAY_MAGIC)) == DISPLAY_MAGIC
    process.wait(timeout=2.0)


def test_reconnect_is_default_and_can_be_disabled_or_explicit():
    default = parse_client_args(["server"])
    enabled = parse_client_args(["server", "--reconnect"])
    disabled = parse_client_args(["server", "--no-reconnect"])

    assert default.reconnect is True
    assert enabled.reconnect is True
    assert disabled.reconnect is False
    assert enabled.raw_argv == ("server", "--reconnect")
    assert disabled.raw_argv == ("server", "--no-reconnect")


def test_history_line_limit_is_optional_and_cli_bounded():
    assert parse_client_args(["server"]).history_lines is None
    assert (
        parse_client_args(
            [
                "server",
                "--history-lines",
                "2000",
            ]
        ).history_lines
        == 2000
    )
    assert (
        parse_client_args(
            [
                "server",
                "--history-lines",
                "20000",
            ]
        ).history_lines
        == 20000
    )

    with pytest.raises(SystemExit):
        parse_client_args(["server", "--history-lines", "1999"])
    with pytest.raises(SystemExit):
        parse_client_args(["server", "--history-lines", "20001"])


@pytest.mark.parametrize(
    ("enabled", "frames", "local_exit", "returncode", "expected"),
    [
        (True, 1, False, 255, True),
        (False, 1, False, 255, False),
        (True, 0, False, 255, False),
        (True, 1, True, 255, False),
        (True, 1, False, None, False),
        (True, 1, False, int(RemoteExit.DETACHED), False),
        (True, 1, False, int(RemoteExit.SOFT_QUIT), False),
        (True, 1, False, int(RemoteExit.HARD_QUIT), False),
    ],
)
def test_automatic_reconnect_classifies_only_unexpected_established_exit(
    enabled,
    frames,
    local_exit,
    returncode,
    expected,
):
    assert (
        fast_display_client.should_automatically_reconnect(
            enabled=enabled,
            painted_frames=frames,
            local_exit=local_exit,
            returncode=returncode,
        )
        is expected
    )


def test_reconnect_wait_local_ctrl_c_and_escape_are_cancellable():
    for byte, exit_code in ((b"\x03", 130), (LOCAL_ESCAPE, 0), (b"", 0)):
        read_fd, write_fd = os.pipe()
        try:
            if byte:
                os.write(write_fd, byte)
            else:
                os.close(write_fd)
                write_fd = -1
            with pytest.raises(fast_display_client.ReconnectCancelled) as exc:
                fast_display_client._consume_reconnect_input(read_fd)
            assert exc.value.exit_code == exit_code
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)


def test_automatic_reconnect_never_requests_takeover_or_interactive_auth(
    monkeypatch,
):
    process = _PreflightProcess()
    reconnect = MagicMock(return_value=(process, RemoteAttachKind.ACCEPTED))
    monkeypatch.setattr(fast_display_client, "_reconnect_remote_attach", reconnect)
    surface = MagicMock()
    args = parse_client_args(["server", "--reconnect"])

    selected = fast_display_client._automatic_reconnect(
        args,
        os.terminal_size((120, 40)),
        surface,
        9,
    )

    assert selected is process
    surface.begin_reconnect.assert_called_once_with()
    reconnect.assert_called_once()
    assert reconnect.call_args.kwargs == {
        "replace_existing_client": False,
        "cancel_fd": 9,
        "timeout": fast_display_client._RECONNECT_ATTEMPT_TIMEOUT,
        "noninteractive": True,
        "existing_session_only": True,
    }
    surface.show_local_status.assert_called_once()


def test_automatic_reconnect_waits_for_busy_helper_lease_without_takeover(
    monkeypatch,
):
    busy = _PreflightProcess()
    accepted = _PreflightProcess()
    reconnect = MagicMock(
        side_effect=(
            (busy, RemoteAttachKind.BUSY),
            (accepted, RemoteAttachKind.ACCEPTED),
        )
    )
    wait = MagicMock()
    monkeypatch.setattr(fast_display_client, "_reconnect_remote_attach", reconnect)
    monkeypatch.setattr(fast_display_client, "_wait_reconnect_delay", wait)
    args = parse_client_args(["server", "--reconnect"])

    selected = fast_display_client._automatic_reconnect(
        args,
        os.terminal_size((120, 40)),
        MagicMock(),
        9,
    )

    assert selected is accepted
    assert busy.terminated
    assert reconnect.call_count == 2
    assert all(
        call.kwargs["replace_existing_client"] is False
        for call in reconnect.call_args_list
    )
    wait.assert_called_once_with(0.5, 9)


def test_reconnect_window_outlives_the_remote_half_open_lease():
    assert (
        fast_display_client._RECONNECT_WINDOW
        > fast_display_server._CLIENT_LEASE_TIMEOUT
    )


def test_ssh_parser_accepts_ordered_exact_and_grouped_arguments():
    args = parse_client_args(
        [
            "server",
            "--ssh-arg=-F",
            "--ssh-args=config -J jump -p 2222",
            "--ssh-arg=ProxyCommand=ssh -W %h:%p gateway",
        ]
    )

    assert args.ssh_arg == [
        "-F",
        "config",
        "-J",
        "jump",
        "-p",
        "2222",
        "ProxyCommand=ssh -W %h:%p gateway",
    ]


def test_ssh_parser_selects_remote_platform_without_changing_default():
    assert parse_client_args(["server"]).remote_platform == "auto"
    assert (
        parse_client_args(["server", "--remote-platform", "windows"]).remote_platform
        == "windows"
    )


def test_reconnect_attach_forces_noninteractive_bounded_ssh(monkeypatch):
    process = _PreflightProcess()
    built = MagicMock(return_value=["ssh", "remote"])
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(fast_display_client, "build_ssh_argv", built)
    _patch_preflight(monkeypatch, "_spawn_remote", spawn)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda *_args, **_kwargs: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda *_args, **_kwargs: RemoteAttachKind.ACCEPTED,
    )
    args = parse_client_args(
        [
            "server",
            "--ssh-arg=-J",
            "--ssh-arg=jump",
        ]
    )

    selected, status = fast_display_client._reconnect_remote_attach(
        args,
        os.terminal_size((120, 40)),
        replace_existing_client=False,
        cancel_fd=9,
        timeout=5.0,
        noninteractive=True,
    )

    assert selected is process
    assert status is RemoteAttachKind.ACCEPTED
    ssh_args = built.call_args.kwargs["ssh_args"]
    assert ssh_args == [
        "-o",
        "BatchMode=yes",
        "-J",
        "jump",
        "-o",
        "ConnectTimeout=5",
    ]
    assert built.call_args.kwargs["replace_existing_client"] is False
    assert built.call_args.kwargs["launch_mode"] is RemoteLaunchMode.POSIX
    spawn.assert_called_once_with(
        ["ssh", "remote"],
        suppress_stderr=True,
    )


def test_reconnect_connect_timeout_keeps_user_first_value(monkeypatch):
    process = _PreflightProcess()
    built = MagicMock(return_value=["ssh", "remote"])
    monkeypatch.setattr(fast_display_client, "build_ssh_argv", built)
    _patch_preflight(
        monkeypatch,
        "_spawn_remote",
        MagicMock(return_value=process),
    )
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda *_args, **_kwargs: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda *_args, **_kwargs: RemoteAttachKind.ACCEPTED,
    )
    args = parse_client_args(
        [
            "server",
            "--ssh-args=-o ConnectTimeout=30",
        ]
    )

    fast_display_client._reconnect_remote_attach(
        args,
        os.terminal_size((120, 40)),
        replace_existing_client=False,
        timeout=5.0,
        noninteractive=True,
    )

    ssh_args = built.call_args.kwargs["ssh_args"]
    assert ssh_args.index("ConnectTimeout=30") < ssh_args.index("ConnectTimeout=5")


def test_reconnect_reuses_selected_windows_launch_mode(monkeypatch):
    process = _PreflightProcess()
    built = MagicMock(return_value=["ssh", "remote"])
    monkeypatch.setattr(fast_display_client, "build_ssh_argv", built)
    _patch_preflight(
        monkeypatch,
        "_spawn_remote",
        MagicMock(return_value=process),
    )
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda *_args, **_kwargs: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(
                fast_display_client.__version__,
                PROTOCOL_VERSION,
                True,
                platform="windows-msys2",
            ),
        ),
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda *_args, **_kwargs: RemoteAttachKind.ACCEPTED,
    )
    args = parse_client_args(["server", "--remote-platform", "windows"])

    fast_display_client._reconnect_remote_attach(
        args,
        os.terminal_size((120, 40)),
        replace_existing_client=False,
    )

    assert built.call_args.kwargs["launch_mode"] is RemoteLaunchMode.DIRECT


def test_remote_command_keeps_railmux_tty_mode_after_user_flags():
    binary = build_remote_command_argv(
        "server",
        remote_args=("remote-server",),
        ssh_args=("-t",),
    )
    cooked = build_remote_command_argv(
        "server",
        remote_args=("config", "--remote-context"),
        ssh_args=("-T",),
        force_tty=True,
    )

    assert binary[:4] == ["ssh", "-t", "-T", "server"]
    assert cooked[:4] == ["ssh", "-T", "-tt", "server"]


def test_local_reconnect_status_is_bounded_to_terminal_bottom_row():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((12, 4)))

    surface.show_local_status("retry\x1b-secret-is-long")

    painted = output.getvalue()
    assert b"\033[?1049h" in painted
    assert b"\033[4;1H\033[2Kretry -secre" in painted
    assert b"is-long" not in painted


def test_startup_surface_uses_alternate_screen_and_restores_primary():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    size = os.terminal_size((80, 24))

    surface.show_startup(size)
    surface.close()

    painted = output.getvalue()
    assert painted.startswith(b"\033[?1049h")
    assert b"\033[?1003h" not in painted
    assert b"\033[?1006h" not in painted
    assert b"\033[?25l" not in painted
    assert b"Restoring your workspace" in painted
    assert painted.endswith(b"\033[?1049l")


def test_startup_interaction_stays_visible_and_returns_to_restoring():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    size = os.terminal_size((80, 24))

    surface.show_startup(size)
    output.seek(0)
    output.truncate()
    surface.begin_interaction()
    output.write(b"Upgrade local Railmux? [y/N] y\r\n")
    surface.show_startup(size)

    painted = output.getvalue()
    assert b"\033[?1049l" not in painted
    assert b"\033[2J\033[H" in painted
    assert b"\033[?25h" in painted
    assert b"Upgrade local Railmux? [y/N] y" in painted
    assert painted.endswith(
        fast_display_client.render_startup_surface(size.columns, size.lines).encode(
            "utf-8"
        )
    )
    assert not surface.interaction_active


def test_startup_stage_change_repaints_without_reentering_terminal():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    size = os.terminal_size((80, 24))

    surface.show_startup(size, "Connecting to remote host…")
    output.seek(0)
    output.truncate()
    surface.show_startup(size, "Checking Railmux versions…")

    painted = output.getvalue()
    assert b"\033[?1049h" not in painted
    assert b"Checking Railmux versions" in painted
    assert b"Connecting to remote host" not in painted


def test_repeated_startup_prompt_keeps_previous_install_output_visible():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    size = os.terminal_size((80, 24))

    surface.show_startup(size)
    surface.begin_interaction()
    output.seek(0)
    output.truncate()
    output.write(b"Remote user-site install failed\r\n")
    surface.begin_interaction()

    painted = output.getvalue()
    assert painted == b"Remote user-site install failed\r\n"
    assert surface.interaction_active


def test_first_interactive_paint_activates_mouse_after_startup():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    size = os.terminal_size((20, 4))
    screen = AppliedScreen(
        width=20,
        height=4,
        cursor_x=0,
        cursor_y=0,
        cursor_visible=True,
        terminal_modes=TerminalMode.NONE,
        rows=(b"one", b"two", b"three", b"four"),
        changed_rows=(0, 1, 2, 3),
        clear=True,
    )

    surface.show_startup(size)
    output.seek(0)
    output.truncate()
    surface.paint(screen)

    painted = output.getvalue()
    assert b"\033[?1049h" not in painted
    assert b"\033[?25l" in painted
    assert b"\033[?1003h\033[?1006h" in painted


def test_local_status_preserves_painted_status_left_and_background():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((40, 4)))
    screen = AppliedScreen(
        width=40,
        height=4,
        cursor_x=2,
        cursor_y=2,
        cursor_visible=True,
        terminal_modes=TerminalMode.NONE,
        rows=(
            b"one",
            b"two",
            b"three",
            b"\033[0;30;48;2;95;175;0m Railmux [R][1][2]                    \033[0m",
        ),
        changed_rows=(0, 1, 2, 3),
        clear=True,
    )
    surface.paint(screen)
    output.seek(0)
    output.truncate()

    surface.show_local_status("Copied 12 chars.", level="success")

    painted = output.getvalue()
    assert b"\033[4;1H\033[2K" not in painted
    assert b"\033[4;21H" in painted
    assert b"\033[48;2;95;175;0m\033[1;38;5;17m\033[K" in painted
    assert b"Copied 12 chars." in painted
    assert b"\033[?25h" not in painted
    assert b"\033[?25l" not in painted


def test_reconnect_status_uses_retained_status_right_without_stale_cursor():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((40, 4)))
    screen = AppliedScreen(
        width=40,
        height=4,
        cursor_x=2,
        cursor_y=2,
        cursor_visible=True,
        terminal_modes=TerminalMode.NONE,
        rows=(
            b"one",
            b"two",
            b"three",
            b"\033[0;30;48;2;95;175;0m Railmux [R][1][2]                    \033[0m",
        ),
        changed_rows=(0, 1, 2, 3),
        clear=True,
    )
    surface.paint(screen)
    surface.begin_reconnect()
    output.seek(0)
    output.truncate()

    surface.show_local_status("Reconnecting (attempt 1)")

    painted = output.getvalue()
    assert b"\033[4;1H\033[2K" not in painted
    assert b"\033[4;21H" in painted
    assert b"\033[48;2;95;175;0m" in painted
    assert b"Reconnecting (attem" in painted
    assert painted.endswith(b"\033[0m\033[?25l")
    assert b"\033[3;3H\033[?25h" not in painted


def test_local_status_is_one_clickable_source_and_survives_remote_paint():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((40, 4)))
    screen = AppliedScreen(
        width=40,
        height=4,
        cursor_x=2,
        cursor_y=2,
        cursor_visible=True,
        terminal_modes=TerminalMode.NONE,
        rows=(b"one", b"two", b"three", b"status"),
        changed_rows=(0, 1, 2, 3),
        clear=True,
    )
    surface.paint(screen)
    surface.show_local_status("Exact warning", level="warning")

    click = SgrMouseEvent(b"down", 0, 30, 4, True)
    assert surface.local_status_at(click) == "Exact warning"
    assert surface.local_status_at(SgrMouseEvent(b"outside", 0, 10, 4, True)) is None

    output.seek(0)
    output.truncate()
    surface.paint(replace(screen, changed_rows=(3,), clear=False))
    assert b"Exact warning" in output.getvalue()

    with patch(
        "railmux.fast_display_client.local_clipboard.copy",
        return_value=True,
    ) as native:
        assert surface.copy_local_status_at(click)
    native.assert_called_once_with(b"Exact warning")
    assert surface.local_status_at(click) == "Copied status message."

    surface.clear_local_status()
    assert surface.local_status_at(SgrMouseEvent(b"down", 0, 30, 4, True)) is None


def test_interruptible_connection_status_yields_to_first_user_action():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.show_local_status(
        "Ctrl-] disconnects · reconnect on",
        interruptible=True,
    )

    assert surface.dismiss_interruptible_local_status()
    assert not surface.dismiss_interruptible_local_status()
    assert surface.local_status_at(SgrMouseEvent(b"down", 0, 1, 1, True)) is None

    surface.show_local_status("Checking remote path…")
    assert not surface.dismiss_interruptible_local_status()


def test_timed_termux_hint_expires_and_newer_status_cancels_its_deadline():
    surface = TerminalSurface(io.BytesIO())
    surface.show_local_status(
        "Tap the prompt again to open the keyboard",
        interruptible=True,
        expires_at=7.0,
    )

    assert not surface.expire_local_status(6.99)
    assert surface.expire_local_status(7.0)
    assert surface._local_status_text is None

    surface.show_local_status(
        "Tap the prompt again to open the keyboard",
        interruptible=True,
        expires_at=8.0,
    )
    surface.show_local_status("Checking remote path…")
    assert not surface.expire_local_status(9.0)
    assert surface._local_status_text == "Checking remote path…"


def test_path_open_prompt_names_the_inside_surface_as_managed_vim():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((60, 12)))

    surface.show_path_open_prompt()

    painted = output.getvalue()
    assert b"Always use Railmux managed Vim" in painted
    assert b"Use Railmux managed Vim this time" in painted


def test_claude_history_prompt_is_local_bounded_and_mouse_selectable():
    output = io.BytesIO()
    surface = TerminalSurface(output)
    surface.set_physical_size(os.terminal_size((40, 12)))

    surface.show_claude_history_prompt()

    painted = output.getvalue()
    assert b"Claude Code history" in painted
    assert b"Always use smooth local history" in painted
    assert b"Claude native" in painted
    assert b"\033[1;38;5;220m[1]\033[0m" in painted
    assert surface.claude_history_prompt_choice(SgrMouseEvent(b"", 0, 20, 4, True)) == (
        "local",
        True,
    )
    assert surface.claude_history_prompt_choice(SgrMouseEvent(b"", 0, 20, 5, True)) == (
        "local",
        False,
    )
    assert surface.claude_history_prompt_choice(SgrMouseEvent(b"", 0, 20, 6, True)) == (
        "native",
        True,
    )
    assert surface.claude_history_prompt_choice(SgrMouseEvent(b"", 0, 20, 7, True)) == (
        "native",
        False,
    )
    assert (
        surface.claude_history_prompt_choice(SgrMouseEvent(b"", 0, 20, 8, True)) is None
    )


def test_claude_history_save_confirmation_has_a_bounded_wait():
    timeout = fast_display_client._CLAUDE_HISTORY_SAVE_TIMEOUT

    assert not fast_display_client.claude_history_save_timed_out(None, 100.0)
    assert not fast_display_client.claude_history_save_timed_out(
        100.0, 100.0 + timeout - 0.001
    )
    assert fast_display_client.claude_history_save_timed_out(100.0, 100.0 + timeout)


class _PreflightProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode or 0

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def _accept_attach(monkeypatch):
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda _process: RemoteAttachKind.ACCEPTED,
    )


def test_compatible_remote_is_confirmed_before_attach(monkeypatch):
    _accept_attach(monkeypatch)
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )

    stages = []
    selected = prepare_remote_process(
        args,
        os.terminal_size((120, 40)),
        on_stage=stages.append,
    )

    assert selected is process
    assert process.stdin.getvalue() == REMOTE_START
    assert stages == [
        "Connecting to remote host…",
        "Checking Railmux versions…",
        "Attaching to workspace…",
    ]


def test_auto_remote_launch_falls_back_to_windows_and_pins_reconnect_mode(
    monkeypatch,
):
    _accept_attach(monkeypatch)
    posix = _PreflightProcess(1)
    windows = _PreflightProcess()
    processes = iter((posix, windows))
    commands = []

    def spawn(argv):
        commands.append(argv)
        return next(processes)

    startups = iter(
        (
            RemoteStartup(RemoteStartKind.FAILED, returncode=1),
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello(
                    fast_display_client.__version__,
                    PROTOCOL_VERSION,
                    True,
                    platform="windows-msys2",
                ),
            ),
        )
    )
    _patch_preflight(monkeypatch, "_spawn_remote", spawn)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: next(startups),
    )
    args = parse_client_args(["server"])

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is windows
    assert posix.terminated is False
    assert "if [" in commands[0][-1]
    assert commands[1][-1].startswith("railmux remote-server ")
    assert args._selected_remote_launch_mode is RemoteLaunchMode.DIRECT
    assert windows.stdin.getvalue() == REMOTE_START


def test_auto_direct_fallback_accepts_authoritative_posix_hello(monkeypatch):
    posix_shell = _PreflightProcess(1)
    direct = _PreflightProcess()
    processes = iter((posix_shell, direct))
    startups = iter(
        (
            RemoteStartup(RemoteStartKind.FAILED, returncode=1),
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello(
                    fast_display_client.__version__,
                    PROTOCOL_VERSION,
                    True,
                    platform="posix",
                ),
            ),
        )
    )
    timeouts = []
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: next(processes))
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: (timeouts.append(timeout), next(startups))[1],
    )

    probe = fast_display_client.probe_remote_launch(
        "server",
        remote_args=("remote-server",),
        ssh_args=(),
        timeout=4.0,
    )

    assert probe.process is direct
    assert probe.launch_mode is RemoteLaunchMode.DIRECT
    assert probe.startup.hello is not None
    assert probe.startup.hello.platform == "posix"
    assert timeouts == [4.0, 4.0]


def test_explicit_windows_rejects_direct_posix_hello(monkeypatch):
    process = _PreflightProcess()
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda *_args, **_kwargs: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(
                fast_display_client.__version__,
                PROTOCOL_VERSION,
                True,
                platform="posix",
            ),
        ),
    )

    with pytest.raises(fast_display_client.ProbeError, match="non-Windows"):
        fast_display_client.probe_remote_launch(
            "server",
            remote_args=("remote-server",),
            ssh_args=(),
            remote_platform="windows",
        )


def test_explicit_windows_remote_failure_never_runs_posix_installer(monkeypatch):
    process = _PreflightProcess(1)
    args = parse_client_args(["server", "--remote-platform", "windows"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.FAILED, returncode=1
        ),
    )
    install = MagicMock()
    monkeypatch.setattr(fast_display_client, "_install_remote_and_start", install)

    with pytest.raises(fast_display_client.ProbeError) as exc:
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "py -m pip install --upgrade" in str(exc.value)
    assert "railmux runtime install --yes" in str(exc.value)
    install.assert_not_called()


def test_reconnect_flag_does_not_change_initial_ssh_command():
    plain = parse_client_args(["server"])
    reconnecting = parse_client_args(["server", "--reconnect"])

    def command(args):
        return build_ssh_argv(
            args.destination,
            session=args.session,
            width=120,
            height=40,
            fps=args.fps,
            ssh_args=args.ssh_arg,
        )

    assert command(reconnecting) == command(plain)


def test_first_frame_timeout_applies_only_while_waiting():
    assert not fast_display_client.first_frame_timed_out(None, 100.0)
    assert not fast_display_client.first_frame_timed_out(100.0, 99.999)
    assert fast_display_client.first_frame_timed_out(100.0, 100.0)


def test_busy_legacy_attach_can_be_replaced_once_with_consent(monkeypatch):
    original = _PreflightProcess()
    retry = _PreflightProcess()
    replacement = _PreflightProcess()
    args = parse_client_args(["server"])
    statuses = iter(
        (
            RemoteAttachKind.BUSY,
            RemoteAttachKind.BUSY,
            RemoteAttachKind.ACCEPTED,
        )
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda _process: next(statuses),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: True)
    built = MagicMock(return_value=["ssh", "reconnect"])
    monkeypatch.setattr(fast_display_client, "build_ssh_argv", built)
    reconnects = iter((retry, replacement))
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: next(reconnects)
    )
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )

    selected = fast_display_client._finish_remote_attach(
        args, os.terminal_size((120, 40)), original
    )

    assert selected is replacement
    assert original.terminated
    assert retry.terminated
    assert replacement.stdin.getvalue() == REMOTE_START
    assert [
        call.kwargs["replace_existing_client"] for call in built.call_args_list
    ] == [False, True]


def test_transient_current_attach_contention_retries_without_takeover(monkeypatch):
    original = _PreflightProcess()
    retry = _PreflightProcess()
    args = parse_client_args(["server"])
    statuses = iter((RemoteAttachKind.BUSY, RemoteAttachKind.ACCEPTED))
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda _process: next(statuses),
    )
    confirm = MagicMock()
    monkeypatch.setattr(fast_display_client, "_confirm", confirm)
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: retry)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )

    selected = fast_display_client._finish_remote_attach(
        args, os.terminal_size((120, 40)), original
    )

    assert selected is retry
    assert original.terminated
    confirm.assert_not_called()


def test_busy_attach_decline_leaves_remote_session_untouched(monkeypatch):
    process = _PreflightProcess()
    retry = _PreflightProcess()
    args = parse_client_args(["server"])
    statuses = iter((RemoteAttachKind.BUSY, RemoteAttachKind.BUSY))
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_attach_status",
        lambda _process: next(statuses),
    )
    events = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda _question: events.append("confirm") or False,
    )
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: retry)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )

    with pytest.raises(fast_display_client.ProbeError, match="still owned"):
        fast_display_client._finish_remote_attach(
            args,
            os.terminal_size((120, 40)),
            process,
            before_interaction=lambda: events.append("reveal"),
        )

    assert process.terminated
    assert retry.terminated
    assert events == ["reveal", "confirm"]


def test_missing_remote_prompts_then_installs_and_starts(monkeypatch):
    _accept_attach(monkeypatch)
    missing = _PreflightProcess(127)
    installed = _PreflightProcess()
    args = parse_client_args(["server"])
    spawn = MagicMock(return_value=missing)
    _patch_preflight(monkeypatch, "_spawn_remote", spawn)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.MISSING, returncode=127
        ),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: True)
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_and_start",
        lambda _args, _size, _version: (
            installed,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
            ),
        ),
    )

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is installed
    assert installed.stdin.getvalue() == REMOTE_START
    spawn.assert_called_once()


def test_missing_remote_decline_returns_copyable_install_help(
    monkeypatch,
):
    process = _PreflightProcess(127)
    args = parse_client_args(["server"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.MISSING, returncode=127
        ),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: False)

    with pytest.raises(fast_display_client.ProbeError) as exc:
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "python3 -m pip install --user" in str(exc.value)
    assert fast_display_client.__version__ in str(exc.value)


def test_remote_without_tmux_gives_system_package_guidance(monkeypatch):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True, False),
        ),
    )

    with pytest.raises(fast_display_client.ProbeError, match="tmux is not"):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert process.terminated


def test_newer_compatible_remote_prompts_for_local_upgrade_but_can_continue(
    monkeypatch,
    capsys,
):
    _accept_attach(monkeypatch)
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or False,
    )

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is process
    assert "Upgrade local Railmux to 999.0?" in questions[0]
    assert "protocol" not in questions[0].lower()
    assert process.stdin.getvalue() == REMOTE_START
    assert "continuing with local Railmux" in capsys.readouterr().err


def test_newer_remote_protocol_can_upgrade_and_restart_local_client(monkeypatch):
    process = _PreflightProcess()
    args = parse_client_args(["server", "--fps", "30"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION + 1, True),
        ),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: True)

    class Restarted(Exception):
        pass

    def restart(version, raw_args):
        assert version == "999.0"
        assert raw_args == ("server", "--fps", "30")
        raise Restarted

    _patch_preflight(monkeypatch, "_upgrade_local_and_restart", restart)

    with pytest.raises(Restarted):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert process.terminated


def test_local_upgrade_reveals_prompt_then_restores_primary_before_exec(
    monkeypatch,
):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    events = []
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION + 1, True),
        ),
    )
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda _question: events.append("confirm") or True,
    )

    class Restarted(Exception):
        pass

    def restart(_version, _raw_args):
        events.append("restart")
        raise Restarted

    _patch_preflight(monkeypatch, "_upgrade_local_and_restart", restart)

    with pytest.raises(Restarted):
        prepare_remote_process(
            args,
            os.terminal_size((120, 40)),
            before_interaction=lambda: events.append("reveal"),
            before_local_restart=lambda: events.append("restore"),
        )

    assert events == ["reveal", "confirm", "restore", "restart"]
    assert process.terminated


def test_released_021_client_can_offer_upgrade_to_remote_022(monkeypatch):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(fast_display_client, "__version__", "0.2.1")
    monkeypatch.setattr(fast_display_client, "PROTOCOL_VERSION", 6)
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("0.2.2", 7, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or True,
    )

    class Restarted(Exception):
        pass

    def restart(version, raw_args):
        assert version == "0.2.2"
        assert raw_args == ("server",)
        raise Restarted

    _patch_preflight(monkeypatch, "_upgrade_local_and_restart", restart)

    with pytest.raises(Restarted):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert process.terminated
    assert "Remote Railmux 0.2.2 is newer than local 0.2.1" in questions[0]
    assert "Upgrade local Railmux to 0.2.2?" in questions[0]
    assert "requires SSH protocol v7" in questions[0]


def test_newer_protocol_with_non_newer_package_cannot_downgrade_local(
    monkeypatch,
):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION + 1, True),
        ),
    )
    confirm = MagicMock()
    monkeypatch.setattr(fast_display_client, "_confirm", confirm)

    with pytest.raises(
        fast_display_client.ProbeError, match="unsafe automatic local downgrade"
    ):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    confirm.assert_not_called()
    assert process.terminated


def test_local_upgrade_uses_current_python_user_site_and_restarts(monkeypatch):
    monkeypatch.setattr(fast_display_client.sys, "prefix", "/usr")
    monkeypatch.setattr(fast_display_client.sys, "base_prefix", "/usr")
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(fast_display_client.subprocess, "run", run)
    monkeypatch.setattr(
        "railmux.self_update.installed_version_matches", lambda _version: True
    )

    class Restarted(Exception):
        pass

    observed = {}

    def execv(executable, argv):
        observed["executable"] = executable
        observed["argv"] = argv
        raise Restarted

    monkeypatch.setattr(fast_display_client.os, "execv", execv)

    with pytest.raises(Restarted):
        fast_display_client._upgrade_local_and_restart(
            "1.2.3", ("server", "--fps", "30")
        )

    install = run.call_args.args[0]
    assert install == [
        fast_display_client.sys.executable,
        "-m",
        "pip",
        "install",
        "--user",
        "--upgrade",
        "railmux==1.2.3",
    ]
    assert observed["argv"] == [
        fast_display_client.sys.executable,
        "-m",
        "railmux",
        "ssh",
        "server",
        "--fps",
        "30",
    ]


def test_older_remote_protocol_prompts_for_matching_remote_upgrade(monkeypatch):
    _accept_attach(monkeypatch)
    old = _PreflightProcess()
    upgraded = _PreflightProcess()
    args = parse_client_args(["server"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: old)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("0.1.0", PROTOCOL_VERSION - 1, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or True,
    )
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_and_start",
        lambda _args, _size, _version: (
            upgraded,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
            ),
        ),
    )

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is upgraded
    assert "uses older SSH protocol" in questions[0]
    assert old.terminated
    assert upgraded.stdin.getvalue() == REMOTE_START


def test_windows_remote_upgrade_fails_closed_with_windows_guidance(monkeypatch):
    process = _PreflightProcess()
    args = parse_client_args(["server", "--remote-platform", "windows"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(
                "0.1.0",
                PROTOCOL_VERSION - 1,
                True,
                platform="windows-msys2",
            ),
        ),
    )
    confirm = MagicMock()
    install = MagicMock()
    monkeypatch.setattr(fast_display_client, "_confirm", confirm)
    monkeypatch.setattr(fast_display_client, "_install_remote_and_start", install)

    with pytest.raises(fast_display_client.ProbeError) as exc:
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "py -m pip install --upgrade" in str(exc.value)
    assert "railmux runtime install --yes" in str(exc.value)
    confirm.assert_not_called()
    install.assert_not_called()


def test_older_compatible_remote_can_be_upgraded_to_local_version(monkeypatch):
    _accept_attach(monkeypatch)
    monkeypatch.setattr(fast_display_client, "__version__", "0.2.5")
    old = _PreflightProcess()
    upgraded = _PreflightProcess()
    args = parse_client_args(["server"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: old)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("0.2.4", PROTOCOL_VERSION, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or True,
    )
    installed_versions = []

    def install(_args, _size, version):
        installed_versions.append(version)
        return (
            upgraded,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello("0.2.5", PROTOCOL_VERSION, True),
            ),
        )

    monkeypatch.setattr(fast_display_client, "_install_remote_and_start", install)

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is upgraded
    assert installed_versions == ["0.2.5"]
    assert "Remote Railmux 0.2.4 is older than local 0.2.5" in questions[0]
    assert old.terminated
    assert upgraded.stdin.getvalue() == REMOTE_START


def test_older_compatible_remote_can_continue_when_upgrade_declined(
    monkeypatch,
    capsys,
):
    _accept_attach(monkeypatch)
    monkeypatch.setattr(fast_display_client, "__version__", "0.2.5")
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("0.2.4", PROTOCOL_VERSION, True),
        ),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: False)

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is process
    assert not process.terminated
    assert process.stdin.getvalue() == REMOTE_START
    assert "continuing with compatible remote Railmux 0.2.4" in (
        capsys.readouterr().err
    )


def test_higher_remote_version_is_offered_to_local_before_protocol_direction(
    monkeypatch,
):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION - 1, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or False,
    )

    with pytest.raises(fast_display_client.ProbeError, match="newer remote"):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "Remote Railmux 999.0 is newer than local" in questions[0]
    assert "Upgrade local Railmux to 999.0?" in questions[0]
    assert process.terminated


def test_declining_local_upgrade_does_not_downgrade_remote_dependency_repair(
    monkeypatch,
):
    _accept_attach(monkeypatch)
    process = _PreflightProcess()
    repaired = _PreflightProcess()
    args = parse_client_args(["server"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: process)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION, False),
        ),
    )
    answers = iter((False, True))
    monkeypatch.setattr(
        fast_display_client, "_confirm", lambda _question: next(answers)
    )
    installed_versions = []

    def install(_args, _size, version):
        installed_versions.append(version)
        return (
            repaired,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello("999.0", PROTOCOL_VERSION, True),
            ),
        )

    monkeypatch.setattr(fast_display_client, "_install_remote_and_start", install)

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is repaired
    assert installed_versions == ["999.0"]
    assert process.terminated
    assert repaired.stdin.getvalue() == REMOTE_START


def test_failed_remote_auto_install_returns_manual_recovery(monkeypatch):
    missing = _PreflightProcess(127)
    failed = _PreflightProcess(1)
    args = parse_client_args(["server"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: missing)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.MISSING, returncode=127
        ),
    )
    answers = iter((True, False))
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or next(answers),
    )
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_and_start",
        lambda _args, _size, _version: (
            failed,
            RemoteStartup(RemoteStartKind.FAILED, returncode=1),
        ),
    )

    with pytest.raises(fast_display_client.ProbeError) as exc:
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "user-site installation failed" in str(exc.value)
    assert "matching wheel or source checkout" in str(exc.value)
    assert "Remote user-site installation failed" in questions[1]


@pytest.mark.parametrize(
    "install_kind", [RemoteStartKind.FAILED, RemoteStartKind.TIMEOUT]
)
def test_failed_user_site_install_can_fall_back_to_private_venv(
    monkeypatch,
    install_kind,
):
    _accept_attach(monkeypatch)
    missing = _PreflightProcess(127)
    failed = _PreflightProcess(1)
    installed = _PreflightProcess()
    args = parse_client_args(["server"])
    _patch_preflight(monkeypatch, "_spawn_remote", lambda _argv: missing)
    _patch_preflight(
        monkeypatch,
        "await_remote_startup",
        lambda _process, timeout=None: RemoteStartup(
            RemoteStartKind.MISSING, returncode=127
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or True,
    )
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_and_start",
        lambda _args, _size, _version: (
            failed,
            RemoteStartup(install_kind, returncode=1),
        ),
    )
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_private_venv_and_start",
        lambda _args, _size, _version: (
            installed,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
            ),
        ),
    )

    selected = prepare_remote_process(args, os.terminal_size((120, 40)))

    assert selected is installed
    assert failed.poll() == 1
    expected = (
        "Remote user-site installation timed out after 300 seconds"
        if install_kind is RemoteStartKind.TIMEOUT
        else "Remote user-site installation failed with exit code 1"
    )
    assert expected in questions[1]
    assert installed.stdin.getvalue() == REMOTE_START


@pytest.mark.parametrize(
    ("startup", "expected"),
    (
        (
            RemoteStartup(RemoteStartKind.TIMEOUT),
            "timed out after 300 seconds before the Railmux compatibility handshake",
        ),
        (
            RemoteStartup(RemoteStartKind.FAILED, returncode=23),
            "failed with exit code 23",
        ),
        (
            RemoteStartup(RemoteStartKind.MISSING, returncode=127),
            "completed, but Railmux was not discoverable afterward",
        ),
        (
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello("0.0.0", 1, False),
            ),
            "completed, but did not provide a compatible Railmux",
        ),
    ),
)
def test_remote_install_failure_keeps_outcomes_distinct(startup, expected):
    assert expected in fast_display_client._remote_install_failure(
        startup, environment="user-site"
    )
