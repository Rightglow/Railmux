"""Interactive configuration of one SSH destination without touching tmux."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence

from packaging.version import InvalidVersion, Version

from railmux import __version__
from railmux.fast_display_client import (
    ProbeError,
    RemoteHello,
    RemoteLaunchMode,
    RemoteStartKind,
    RemoteStartup,
    _remote_server_args,
    _spawn_remote,
    _stop_unstarted_remote,
    _upgrade_local_and_restart,
    await_remote_startup,
    build_remote_command_argv,
    build_remote_install_argv,
    build_remote_private_venv_install_argv,
    probe_remote_launch,
    remote_install_help,
    remote_windows_install_help,
)
from railmux.fast_display_protocol import REMOTE_CONFIG_PROTOCOL
from railmux.terminal_status import (
    STYLE_PROMPT,
    TransientStatusLine,
    command_status,
    styled,
)


_PROBE_WIDTH = 80
_PROBE_HEIGHT = 24
_PROBE_FPS = 20.0


def _confirm(question: str) -> bool:
    try:
        answer = input(styled(f"{question} [y/N] ", STYLE_PROMPT, stream=sys.stdout))
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False
    return answer.strip().lower() in {"y", "yes"}


def _probe_args() -> list[str]:
    return _remote_server_args(
        session="railmux",
        width=_PROBE_WIDTH,
        height=_PROBE_HEIGHT,
        fps=_PROBE_FPS,
    )


def _start_probe(
    destination: str,
    ssh_args: Sequence[str],
    *,
    install: str | None = None,
    remote_platform: str = "auto",
) -> tuple[subprocess.Popen, RemoteStartup, RemoteLaunchMode]:
    remote_args = _probe_args()
    if install == "user":
        argv = build_remote_install_argv(
            destination,
            version=__version__,
            remote_args=remote_args,
            ssh_args=ssh_args,
        )
    elif install == "venv":
        argv = build_remote_private_venv_install_argv(
            destination,
            version=__version__,
            remote_args=remote_args,
            ssh_args=ssh_args,
        )
    else:
        probe = probe_remote_launch(
            destination,
            remote_args=remote_args,
            ssh_args=ssh_args,
            remote_platform=remote_platform,
        )
        return probe.process, probe.startup, probe.launch_mode
    process = _spawn_remote(argv)
    return process, await_remote_startup(process), RemoteLaunchMode.POSIX


def _version_order(remote_version: str) -> int | None:
    try:
        local = Version(__version__)
        remote = Version(remote_version)
    except InvalidVersion:
        return None
    return (remote > local) - (remote < local)


def _validate_config_protocol(
    hello: RemoteHello,
    *,
    raw_argv: Sequence[str],
    status: TransientStatusLine | None = None,
) -> bool:
    if hello.config_protocol == REMOTE_CONFIG_PROTOCOL:
        return True
    order = _version_order(hello.version)
    if hello.config_protocol > REMOTE_CONFIG_PROTOCOL and order == 1:
        if status is not None:
            status.clear()
        if _confirm(
            f"Remote Railmux {hello.version} uses newer remote-config "
            f"protocol v{hello.config_protocol}. Upgrade local Railmux?"
        ):
            _upgrade_local_and_restart(
                hello.version,
                raw_argv,
                subcommand="config",
            )
        raise ProbeError(
            "the remote configuration protocol is newer; upgrade local "
            "Railmux before retrying"
        )
    if hello.config_protocol > REMOTE_CONFIG_PROTOCOL:
        raise ProbeError(
            "the remote configuration protocol is newer, but its package "
            "version is not newer; install matching Railmux versions manually"
        )
    if order == 1:
        if status is not None:
            status.clear()
        if _confirm(
            f"Remote Railmux {hello.version} does not advertise a compatible "
            "remote-config protocol. Upgrade local Railmux to that version?"
        ):
            _upgrade_local_and_restart(
                hello.version,
                raw_argv,
                subcommand="config",
            )
        raise ProbeError(
            "the newer remote package does not advertise a compatible "
            "configuration protocol; upgrade local Railmux or configure it "
            "after logging in"
        )
    if order is None and hello.version != __version__:
        raise ProbeError(
            "the remote package does not advertise remote configuration and "
            "its version cannot be ordered safely; configure it after logging in"
        )
    return False


def _installed_probe_or_error(
    process: subprocess.Popen,
    startup: RemoteStartup,
) -> RemoteHello | None:
    if startup.kind is RemoteStartKind.HELLO:
        assert startup.hello is not None
        return startup.hello
    _stop_unstarted_remote(process)
    return None


def _ensure_remote_config_cli(
    destination: str,
    ssh_args: Sequence[str],
    raw_argv: Sequence[str],
    *,
    remote_platform: str = "auto",
    status: TransientStatusLine | None = None,
) -> RemoteLaunchMode:
    process, startup, launch_mode = _start_probe(
        destination, ssh_args, remote_platform=remote_platform)
    if startup.kind is RemoteStartKind.TIMEOUT:
        _stop_unstarted_remote(process)
        raise ProbeError(
            "timed out waiting for the remote Railmux compatibility handshake"
        )
    if startup.kind is RemoteStartKind.FAILED and startup.returncode == 255:
        _stop_unstarted_remote(process)
        raise ProbeError("ssh could not connect to the remote host")

    hello = _installed_probe_or_error(process, startup)
    if hello is not None:
        _stop_unstarted_remote(process)
        supported = _validate_config_protocol(
            hello,
            raw_argv=raw_argv,
            status=status,
        )
        if supported:
            return launch_mode
        reason = (
            f"Remote Railmux {hello.version} does not support safe remote "
            "configuration."
        )
    else:
        reason = "Railmux is not installed or discoverable remotely."

    windows_remote = (
        hello.platform == "windows-msys2"
        if hello is not None
        else launch_mode is RemoteLaunchMode.DIRECT
    )
    if windows_remote:
        raise ProbeError(
            f"{reason}\n{remote_windows_install_help(destination, __version__)}"
        )

    if status is not None:
        status.clear()
    if not _confirm(
        f"{reason} Install Railmux {__version__} with SSH support into the "
        f"remote user environment on {destination}?"
    ):
        raise ProbeError(remote_install_help(destination, __version__))

    process, startup, _launch_mode = _start_probe(
        destination, ssh_args, install="user", remote_platform="posix")
    hello = _installed_probe_or_error(process, startup)
    if hello is not None:
        _stop_unstarted_remote(process)
        supported = _validate_config_protocol(
            hello,
            raw_argv=raw_argv,
            status=status,
        )
        if supported:
            return RemoteLaunchMode.POSIX
        raise ProbeError(
            "automatic installation completed but did not provide a compatible "
            "remote configuration command"
        )

    if status is not None:
        status.clear()
    if not _confirm(
        "Remote user-site installation failed or timed out. Create the isolated "
        "~/.local/share/railmux/ssh-venv environment and install Railmux "
        f"{__version__} there? This does not use sudo or modify system Python."
    ):
        raise ProbeError(
            "remote user-site installation failed or timed out.\n"
            f"{remote_install_help(destination, __version__)}"
        )

    process, startup, _launch_mode = _start_probe(
        destination, ssh_args, install="venv", remote_platform="posix")
    hello = _installed_probe_or_error(process, startup)
    if hello is None:
        raise ProbeError(
            "automatic private-environment installation did not produce a "
            f"compatible Railmux.\n{remote_install_help(destination, __version__)}"
        )
    _stop_unstarted_remote(process)
    supported = _validate_config_protocol(
        hello,
        raw_argv=raw_argv,
        status=status,
    )
    if not supported:
        raise ProbeError(
            "automatic private-environment installation completed but did not "
            "provide a compatible remote configuration command"
        )
    return RemoteLaunchMode.POSIX


def run_remote_config(
    destination: str,
    *,
    ssh_args: Sequence[str],
    raw_argv: Sequence[str],
    remote_platform: str = "auto",
) -> int:
    """Negotiate safely, then run the target user's cooked config editor."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "error: stdin and stdout must both be interactive terminals",
            file=sys.stderr,
        )
        return 2
    if shutil.which("ssh") is None:
        print("error: ssh is not installed or not on PATH", file=sys.stderr)
        return 2
    status = TransientStatusLine(sys.stderr)
    try:
        status.show(
            command_status(
                "railmux config",
                "Connecting and checking remote Railmux…",
                stream=sys.stderr,
            )
        )
        launch_mode = _ensure_remote_config_cli(
            destination,
            ssh_args,
            raw_argv,
            remote_platform=remote_platform,
            status=status,
        )
        status.show(
            command_status(
                "railmux config",
                "Opening remote settings…",
                stream=sys.stderr,
            )
        )
        argv = build_remote_command_argv(
            destination,
            remote_args=("config", "--remote-context"),
            ssh_args=ssh_args,
            force_tty=True,
            launch_mode=launch_mode,
        )
        result = subprocess.run(argv, check=False)
    except KeyboardInterrupt:
        status.clear()
        print(file=sys.stderr)
        return 130
    except ProbeError as exc:
        status.clear()
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        status.clear()
        print(f"error: could not start ssh: {exc}", file=sys.stderr)
        return 2
    finally:
        status.clear()
    if result.returncode == 255:
        print(
            "error: ssh connection failed while editing remote config", file=sys.stderr
        )
        return 2
    if result.returncode < 0:
        return 128 + abs(result.returncode)
    return result.returncode
