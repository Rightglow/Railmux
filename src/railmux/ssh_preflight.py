"""Cooked-mode SSH discovery, compatibility data, and install commands.

This module deliberately has no terminal surface, history, input, or tmux
display-loop dependency. Remote config, remote doctor, and the interactive SSH
client all use this same pre-attach boundary.
"""

from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import NoReturn

from railmux.fast_display_protocol import (
    DISPLAY_MAGIC,
    PROTOCOL_VERSION,
    REMOTE_HELLO_PREFIX,
)


LOCAL_ESCAPE = b"\x1d"  # Ctrl-]
REMOTE_HELLO_TIMEOUT = 60.0
_REMOTE_HELLO_LIMIT = 16 * 1024
_DISPLAY_MAGIC_PREFIX = b"RMUXD"
_REMOTE_VENV = ".local/share/railmux/ssh-venv"


class ProbeError(RuntimeError):
    """A bounded, user-facing SSH display failure."""


class ReconnectCancelled(Exception):
    """A local Ctrl-]/Ctrl-C/EOF cancelled an in-progress reconnect."""

    def __init__(self, exit_code: int) -> None:
        super().__init__("automatic reconnect cancelled")
        self.exit_code = exit_code


@dataclass(frozen=True)
class RemoteHello:
    version: str
    protocol: int
    ready: bool
    tmux: bool = True
    config_status: str = "valid"
    tmux_configured: bool = False
    config_protocol: int = 0
    platform: str = "posix"


class RemoteStartKind(Enum):
    HELLO = "hello"
    MISSING = "missing"
    FAILED = "failed"
    TIMEOUT = "timeout"


class RemoteLaunchMode(Enum):
    POSIX = "posix"
    DIRECT = "direct"


@dataclass(frozen=True)
class RemoteStartup:
    kind: RemoteStartKind
    hello: RemoteHello | None = None
    returncode: int | None = None


@dataclass(frozen=True)
class RemoteProbe:
    """One cooked-mode launch-family probe before any attach mutation."""

    process: subprocess.Popen
    startup: RemoteStartup
    launch_mode: RemoteLaunchMode


def parse_remote_hello(line: bytes) -> RemoteHello:
    """Parse one bounded, untrusted compatibility line from the remote."""
    if not line.startswith(REMOTE_HELLO_PREFIX):
        raise ValueError("not a Railmux remote hello")
    payload = line[len(REMOTE_HELLO_PREFIX) :].rstrip(b"\r\n")
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid Railmux remote hello") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid Railmux remote hello")
    version = value.get("version")
    protocol = value.get("protocol")
    ready = value.get("ready")
    tmux = value.get("tmux")
    config_status = value.get("config_status", "valid")
    tmux_configured = value.get("tmux_configured", False)
    config_protocol = value.get("config_protocol", 0)
    platform = value.get("platform", "posix")
    if (
        not isinstance(version, str)
        or not version
        or len(version) > 128
        or not isinstance(protocol, int)
        or isinstance(protocol, bool)
        or not 1 <= protocol <= 65535
        or not isinstance(ready, bool)
        or not isinstance(tmux, bool)
        or config_status not in {"valid", "invalid"}
        or not isinstance(tmux_configured, bool)
        or not isinstance(config_protocol, int)
        or isinstance(config_protocol, bool)
        or not 0 <= config_protocol <= 65535
        or platform not in {"posix", "windows-msys2"}
    ):
        raise ValueError("invalid Railmux remote hello")
    try:
        version.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid Railmux remote version") from exc
    return RemoteHello(
        version,
        protocol,
        ready,
        tmux,
        config_status,
        tmux_configured,
        config_protocol,
        platform,
    )


def _consume_reconnect_input(fd: int) -> None:
    """Discard unavailable-connection input, stopping on local escape/Ctrl-C."""
    data = os.read(fd, 4096)
    if b"\x03" in data:
        raise ReconnectCancelled(130)
    if not data or LOCAL_ESCAPE in data:
        raise ReconnectCancelled(0)


def await_remote_startup(
    process: subprocess.Popen,
    timeout: float = REMOTE_HELLO_TIMEOUT,
    *,
    cancel_fd: int | None = None,
) -> RemoteStartup:
    """Wait before raw mode until the remote proves its compatibility state."""
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    received = bytearray()
    line_start = 0
    while len(received) < _REMOTE_HELLO_LIMIT:
        magic_start = received.find(_DISPLAY_MAGIC_PREFIX)
        if magic_start >= 0:
            magic_end = magic_start + len(DISPLAY_MAGIC)
            legacy_end = received.find(b"\0", magic_start)
            if len(received) >= magic_end or legacy_end >= 0:
                return RemoteStartup(RemoteStartKind.FAILED)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return RemoteStartup(RemoteStartKind.TIMEOUT)
        readers = [process.stdout.fileno()]
        if cancel_fd is not None:
            readers.append(cancel_fd)
        readable, _writable, _exceptional = select.select(readers, [], [], remaining)
        if not readable:
            return RemoteStartup(RemoteStartKind.TIMEOUT)
        if cancel_fd is not None and cancel_fd in readable:
            _consume_reconnect_input(cancel_fd)
            if process.stdout.fileno() not in readable:
                continue
        chunk = os.read(process.stdout.fileno(), 1)
        if not chunk:
            returncode = process.wait()
            kind = (
                RemoteStartKind.MISSING
                if returncode == 127
                else RemoteStartKind.FAILED
            )
            return RemoteStartup(kind, returncode=returncode)
        received.extend(chunk)
        if chunk != b"\n":
            continue
        line = bytes(received[line_start:])
        line_start = len(received)
        marker = line.find(REMOTE_HELLO_PREFIX)
        if marker < 0:
            continue
        try:
            hello = parse_remote_hello(line[marker:])
        except ValueError:
            return RemoteStartup(RemoteStartKind.FAILED)
        return RemoteStartup(RemoteStartKind.HELLO, hello=hello)
    return RemoteStartup(RemoteStartKind.FAILED)


def _remote_server_args(
    *,
    session: str,
    width: int,
    height: int,
    fps: float,
    replace_existing_client: bool = False,
    existing_session_only: bool = False,
) -> list[str]:
    args = [
        "remote-server",
        "--protocol",
        str(PROTOCOL_VERSION),
        "--session",
        session,
        "--width",
        str(width),
        "--height",
        str(height),
        "--fps",
        str(fps),
    ]
    if replace_existing_client:
        args.append("--replace-existing-client")
    if existing_session_only:
        args.append("--existing-session-only")
    return args


def _remote_launch_command(server_args: Sequence[str]) -> str:
    direct = shlex.join(["railmux", *server_args])
    managed_python = f'"$HOME/{_REMOTE_VENV}/bin/python"'
    managed_args = shlex.join(["-m", "railmux", *server_args])
    branches = [
        f"if [ -x {managed_python} ] "
        f"&& {managed_python} -c 'import railmux' >/dev/null 2>&1; "
        f"then exec {managed_python} {managed_args}",
        f"elif command -v railmux >/dev/null 2>&1; then exec {direct}",
    ]
    for python in ("python3", "python"):
        probe = shlex.join([python, "-c", "import railmux"])
        launch = shlex.join([python, "-m", "railmux", *server_args])
        branches.append(
            f"elif command -v {python} >/dev/null 2>&1 "
            f"&& {probe} >/dev/null 2>&1; then exec {launch}"
        )
    branches.append("else exit 127; fi")
    return "; ".join(branches)


def _remote_direct_launch_command(server_args: Sequence[str]) -> str:
    """Build a shell-neutral command accepted by Windows OpenSSH PowerShell."""
    return shlex.join(["railmux", *server_args])


def build_remote_command_argv(
    destination: str,
    *,
    remote_args: Sequence[str],
    ssh_args: Sequence[str],
    force_tty: bool = False,
    launch_mode: RemoteLaunchMode = RemoteLaunchMode.POSIX,
) -> list[str]:
    """Build SSH argv through the shared remote Railmux discovery ladder."""
    return [
        "ssh",
        *ssh_args,
        "-tt" if force_tty else "-T",
        destination,
        (
            _remote_direct_launch_command(remote_args)
            if launch_mode is RemoteLaunchMode.DIRECT
            else _remote_launch_command(remote_args)
        ),
    ]


def _spawn_remote(
    argv: Sequence[str],
    *,
    suppress_stderr: bool = False,
) -> subprocess.Popen:
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if suppress_stderr else None,
        )
    except OSError as exc:
        raise ProbeError(f"could not start ssh: {exc}") from exc
    assert process.stdin is not None
    assert process.stdout is not None
    return process


def _stop_unstarted_remote(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def probe_remote_launch(
    destination: str,
    *,
    remote_args: Sequence[str],
    ssh_args: Sequence[str],
    remote_platform: str = "auto",
    force_tty: bool = False,
    timeout: float = REMOTE_HELLO_TIMEOUT,
) -> RemoteProbe:
    """Probe and pin one POSIX or shell-neutral remote launch family."""
    if remote_platform not in {"auto", "posix", "windows"}:
        raise ValueError("invalid remote platform")
    launch_mode = (
        RemoteLaunchMode.DIRECT
        if remote_platform == "windows"
        else RemoteLaunchMode.POSIX
    )
    effective_ssh_args = (
        *ssh_args,
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=3",
    )

    def start(mode: RemoteLaunchMode) -> tuple[subprocess.Popen, RemoteStartup]:
        argv = build_remote_command_argv(
            destination,
            remote_args=remote_args,
            ssh_args=effective_ssh_args,
            force_tty=force_tty,
            launch_mode=mode,
        )
        process = _spawn_remote(argv)
        startup = await_remote_startup(process, timeout=timeout)
        return process, startup

    process, startup = start(launch_mode)
    if (
        remote_platform == "auto"
        and launch_mode is RemoteLaunchMode.POSIX
        and startup.kind is RemoteStartKind.FAILED
        and startup.returncode != 255
    ):
        _stop_unstarted_remote(process)
        launch_mode = RemoteLaunchMode.DIRECT
        process, startup = start(launch_mode)

    if (
        startup.kind is RemoteStartKind.HELLO
        and startup.hello is not None
        and remote_platform == "windows"
        and startup.hello.platform != "windows-msys2"
    ):
        _stop_unstarted_remote(process)
        raise ProbeError(
            "the direct remote launcher answered from a non-Windows Railmux "
            "runtime; use --remote-platform posix for that host"
        )
    return RemoteProbe(process, startup, launch_mode)


def build_remote_install_argv(
    destination: str,
    *,
    version: str,
    remote_args: Sequence[str],
    ssh_args: Sequence[str],
) -> list[str]:
    """Install into the remote user environment, then exec Railmux args."""
    requirement = f"railmux[ssh]=={version}"
    managed_python = f'"$HOME/{_REMOTE_VENV}/bin/python"'
    managed_install = shlex.join(
        ["-m", "pip", "install", "--upgrade", requirement]
    )
    managed_launch = shlex.join(["-m", "railmux", *remote_args])
    branches = [
        f"if [ -x {managed_python} ] "
        f"&& {managed_python} -m pip --version >/dev/null 2>&1; "
        f"then {managed_python} {managed_install} 1>&2 "
        f"&& exec {managed_python} {managed_launch}; exit $?"
    ]
    candidates = (
        (("python3", "-m", "pip"), ("python3", "-m", "railmux")),
        (("python", "-m", "pip"), ("python", "-m", "railmux")),
        (("pip3",), ("python3", "-m", "railmux")),
        (("pip",), ("python", "-m", "railmux")),
    )
    for installer, runner in candidates:
        executable = installer[0]
        runner_executable = runner[0]
        pip_probe = shlex.join([*installer, "--version"])
        condition = (
            f"command -v {executable} >/dev/null 2>&1 "
            f"&& {pip_probe} >/dev/null 2>&1"
        )
        if runner_executable != executable:
            condition += f" && command -v {runner_executable} >/dev/null 2>&1"
        install = shlex.join(
            [*installer, "install", "--user", "--upgrade", requirement]
        )
        launch = shlex.join([*runner, *remote_args])
        branches.append(
            f"elif {condition}; then {install} 1>&2 && exec {launch}; exit $?"
        )
    branches.append(
        "else echo 'error: no usable python/pip, python3/pip3, or pip was found' "
        ">&2; exit 127; fi"
    )
    return ["ssh", *ssh_args, "-T", destination, "; ".join(branches)]


def build_remote_private_venv_install_argv(
    destination: str,
    *,
    version: str,
    remote_args: Sequence[str],
    ssh_args: Sequence[str],
) -> list[str]:
    """Install into Railmux's private remote venv, then exec Railmux args."""
    requirement = f"railmux[ssh]=={version}"
    managed_dir = f'"$HOME/{_REMOTE_VENV}"'
    managed_python = f"{managed_dir}/bin/python"
    install = shlex.join(["-m", "pip", "install", "--upgrade", requirement])
    launch = shlex.join(["-m", "railmux", *remote_args])
    branches = [
        f"if [ -x {managed_python} ] "
        f"&& {managed_python} -m pip --version >/dev/null 2>&1; "
        f"then {managed_python} {install} 1>&2 "
        f"&& exec {managed_python} {launch}; exit $?"
    ]
    for python in ("python3", "python"):
        branches.append(
            f"elif command -v {python} >/dev/null 2>&1 "
            f"&& {python} -m venv {managed_dir} 1>&2; then "
            f"{managed_python} {install} 1>&2 "
            f"&& exec {managed_python} {launch}; exit $?"
        )
    branches.append(
        "else echo 'error: no usable python3 or python was found to create "
        "the private Railmux environment' >&2; exit 127; fi"
    )
    return ["ssh", *ssh_args, "-T", destination, "; ".join(branches)]


def remote_install_help(destination: str, version: str) -> str:
    requirement = shlex.quote(f"railmux[ssh]=={version}")
    return (
        f"Install it manually on {destination}, then retry:\n"
        f"  python3 -m pip install --user --upgrade {requirement}\n"
        f"or:\n  pip3 install --user --upgrade {requirement}\n"
        "These commands use per-user site packages and do not modify the "
        "system Python. If site policy still rejects them, use a private "
        "Railmux environment:\n"
        f"  python3 -m venv ~/{_REMOTE_VENV}\n"
        f"  ~/{_REMOTE_VENV}/bin/python -m pip install --upgrade {requirement}\n"
        "If that version is not published, copy the matching wheel or source "
        "checkout to the remote host and install it with the same Python."
    )


def remote_windows_install_help(destination: str, version: str) -> str:
    return (
        f"Update the user-level Railmux for Windows on {destination} from "
        "PowerShell, then retry:\n"
        f"  py -m pip install --upgrade railmux=={version}\n"
        "  railmux runtime install --yes\n"
        "The runtime command installs only the matching Railmux app layer and "
        "reuses a verified managed MSYS2 base when its pinned version is "
        "unchanged. It does not modify Codex or Claude session files. If 'py' "
        "is unavailable, use the same Windows Python executable that installed "
        "the Railmux command."
    )


def local_windows_update_help(version: str) -> str:
    """Return the native-owner update path for a managed Windows client."""
    return (
        "Update this Windows Railmux installation from PowerShell, then retry:\n"
        f"  py -m pip install --upgrade railmux=={version}\n"
        "  railmux runtime install --yes\n"
        "Do not update the versioned MSYS2 app environment with its private "
        "pip; the native bootstrap owns application-layer installation."
    )


def _local_upgrade_argv(version: str) -> list[str]:
    from railmux.self_update import upgrade_argv

    return upgrade_argv(version)


def _upgrade_local_and_restart(
    version: str,
    raw_args: Sequence[str],
    *,
    subcommand: str = "ssh",
) -> NoReturn:
    from railmux.provider_paths import running_in_windows_wrapper
    from railmux.self_update import installed_version_matches

    if running_in_windows_wrapper():
        raise ProbeError(local_windows_update_help(version))

    argv = _local_upgrade_argv(version)
    command_label = f"railmux {subcommand}"
    print(f"{command_label}: upgrading local Railmux to {version}...", file=sys.stderr)
    try:
        result = subprocess.run(argv, check=False)
    except OSError as exc:
        raise ProbeError(
            f"could not start local pip: {exc}\nRun manually:\n  {shlex.join(argv)}"
        ) from exc
    if result.returncode:
        raise ProbeError(
            "local Railmux upgrade failed. Run manually, then retry:\n  "
            f"{shlex.join(argv)}"
        )
    if not installed_version_matches(version):
        raise ProbeError(
            "pip reported success, but a fresh Railmux process did not import "
            f"version {version}. Run manually, then retry:\n  {shlex.join(argv)}"
        )
    restart = [sys.executable, "-m", "railmux", subcommand, *raw_args]
    print(f"{command_label}: local upgrade succeeded; restarting...", file=sys.stderr)
    try:
        os.execv(sys.executable, restart)
    except OSError as exc:
        raise ProbeError(
            "local upgrade succeeded but Railmux could not restart; run:\n  "
            f"{shlex.join(restart)}"
        ) from exc
    raise AssertionError("os.execv returned unexpectedly")
