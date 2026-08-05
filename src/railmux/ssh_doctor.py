"""Read-only compatibility preflight for ``railmux doctor --remote HOST``."""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import asdict, dataclass
from typing import Sequence, TextIO

from railmux import __version__
from railmux.fast_display_protocol import PROTOCOL_VERSION
from railmux.ssh_compat import CompatibilityFacts, decide as decide_compatibility
from railmux.terminal_status import (
    STYLE_ACCENT,
    STYLE_ERROR,
    STYLE_HEADING,
    STYLE_MUTED,
    STYLE_SUCCESS,
    STYLE_WARNING,
    TransientStatusLine,
    command_status,
    stream_is_tty,
    styled,
)


SSH_DOCTOR_SCHEMA_VERSION = 3
_PROBE_TIMEOUT = 15.0


@dataclass(frozen=True)
class RemoteSshDoctorSnapshot:
    """Bounded diagnostic output that deliberately omits the hostname."""

    schema_version: int
    status: str
    local_version: str
    local_protocol: int
    remote_version: str | None = None
    remote_protocol: int | None = None
    remote_dependency_ready: bool | None = None
    remote_tmux: bool | None = None
    remote_config_status: str | None = None
    remote_tmux_configured: bool | None = None
    remote_runtime: str | None = None
    launch_family: str | None = None
    compatible: bool = False
    detail: str | None = None
    read_only: bool = True
    host_omitted: bool = True


def collect_remote_ssh_snapshot(
    destination: str,
    *,
    ssh_args: Sequence[str] = (),
    remote_platform: str = "auto",
) -> RemoteSshDoctorSnapshot:
    """Probe only the pre-attach hello; never send the mutation token."""
    if shutil.which("ssh") is None:
        return RemoteSshDoctorSnapshot(
            SSH_DOCTOR_SCHEMA_VERSION,
            "ssh_missing",
            __version__,
            PROTOCOL_VERSION,
            detail="ssh is not installed or not on PATH",
        )

    # Import lazily so ordinary local diagnostics do not load the interactive
    # display client or create a dependency cycle.
    from railmux.fast_display_client import (
        ProbeError,
        RemoteStartKind,
        _stop_unstarted_remote,
        _remote_server_args,
        probe_remote_launch,
    )

    remote_args = _remote_server_args(
        session="railmux",
        width=80,
        height=24,
        fps=20.0,
        existing_session_only=True,
    )
    try:
        probe = probe_remote_launch(
            destination,
            remote_args=remote_args,
            ssh_args=ssh_args,
            remote_platform=remote_platform,
            timeout=_PROBE_TIMEOUT,
        )
    except (ProbeError, RuntimeError) as exc:
        return RemoteSshDoctorSnapshot(
            SSH_DOCTOR_SCHEMA_VERSION,
            "platform_mismatch" if isinstance(exc, ProbeError) else "ssh_unavailable",
            __version__,
            PROTOCOL_VERSION,
            detail=str(exc),
        )
    process = probe.process
    startup = probe.startup
    _stop_unstarted_remote(process)

    if startup.kind is RemoteStartKind.MISSING:
        return RemoteSshDoctorSnapshot(
            SSH_DOCTOR_SCHEMA_VERSION,
            (
                "windows_railmux_missing"
                if probe.launch_mode.value == "direct"
                else "railmux_missing"
            ),
            __version__,
            PROTOCOL_VERSION,
            launch_family=probe.launch_mode.value,
            detail=(
                "install matching Railmux from PowerShell and run 'railmux "
                "runtime install --yes' on the remote Windows account"
                if probe.launch_mode.value == "direct"
                else "remote Railmux is not installed or discoverable"
            ),
        )
    if startup.kind is RemoteStartKind.TIMEOUT:
        return RemoteSshDoctorSnapshot(
            SSH_DOCTOR_SCHEMA_VERSION,
            "timeout",
            __version__,
            PROTOCOL_VERSION,
            launch_family=probe.launch_mode.value,
            detail="timed out waiting for the remote compatibility hello",
        )
    if startup.kind is not RemoteStartKind.HELLO or startup.hello is None:
        detail = (
            "ssh could not connect to the remote host"
            if startup.returncode == 255
            else "remote Railmux did not return a valid compatibility hello"
        )
        return RemoteSshDoctorSnapshot(
            SSH_DOCTOR_SCHEMA_VERSION,
            "connection_failed",
            __version__,
            PROTOCOL_VERSION,
            launch_family=probe.launch_mode.value,
            detail=detail,
        )

    hello = startup.hello
    if hello.config_status != "valid":
        return RemoteSshDoctorSnapshot(
            SSH_DOCTOR_SCHEMA_VERSION,
            "config_invalid",
            __version__,
            PROTOCOL_VERSION,
            remote_version=hello.version,
            remote_protocol=hello.protocol,
            remote_dependency_ready=hello.ready,
            remote_tmux=hello.tmux,
            remote_config_status=hello.config_status,
            remote_tmux_configured=hello.tmux_configured,
            remote_runtime=hello.platform,
            launch_family=probe.launch_mode.value,
            detail="run 'railmux config' on the remote host to repair or reset it",
        )
    facts = CompatibilityFacts(
        local_version=__version__,
        local_protocol=PROTOCOL_VERSION,
        remote_version=hello.version,
        remote_protocol=hello.protocol,
        remote_ready=hello.ready,
        remote_tmux=hello.tmux,
    )
    decision = decide_compatibility(facts)
    offered_prompt = decision.prompt if decision.action == "prompt" else None
    if offered_prompt == "local_upgrade":
        # A doctor probe never accepts an upgrade. Re-evaluate the safe
        # decline path so a version difference with an identical protocol is
        # correctly reported as usable now.
        decision = decide_compatibility(facts, {"local_upgrade": False})
    elif offered_prompt == "remote_install":
        decision = decide_compatibility(facts, {"remote_install": False})
    if decision.action == "attach":
        status = (
            "ready_with_version_difference" if offered_prompt is not None else "ready"
        )
        compatible = True
    elif decision.action == "tmux_missing":
        status = "configured_tmux_missing" if hello.tmux_configured else "tmux_missing"
        compatible = False
    else:
        status = "incompatible"
        compatible = False
    return RemoteSshDoctorSnapshot(
        SSH_DOCTOR_SCHEMA_VERSION,
        status,
        __version__,
        PROTOCOL_VERSION,
        remote_version=hello.version,
        remote_protocol=hello.protocol,
        remote_dependency_ready=hello.ready,
        remote_tmux=hello.tmux,
        remote_config_status=hello.config_status,
        remote_tmux_configured=hello.tmux_configured,
        remote_runtime=hello.platform,
        launch_family=probe.launch_mode.value,
        compatible=compatible,
        detail=(
            "run 'railmux config' on the remote host to correct or reset the "
            "tmux executable"
            if status == "configured_tmux_missing"
            else (
                "update the remote Windows user package and managed runtime "
                "from PowerShell"
                if hello.platform == "windows-msys2" and not compatible
                else decision.reason or decision.warning
            )
        ),
    )


def render_remote_ssh_text(snapshot: RemoteSshDoctorSnapshot) -> str:
    """Render a compact preflight without retaining or printing the host."""
    remote_identity = "unavailable"
    if snapshot.remote_version is not None:
        remote_identity = (
            f"{snapshot.remote_version}; protocol v{snapshot.remote_protocol}"
        )
    lines = [
        "Railmux SSH preflight (host omitted)",
        f"Status: {snapshot.status}",
        f"Local: {snapshot.local_version}; protocol v{snapshot.local_protocol}",
        f"Remote: {remote_identity}",
    ]
    if snapshot.remote_dependency_ready is not None:
        lines.append(
            "Remote SSH display dependency: "
            + ("ready" if snapshot.remote_dependency_ready else "missing")
        )
    if snapshot.remote_tmux is not None:
        lines.append(
            "Remote tmux: " + ("available" if snapshot.remote_tmux else "missing")
        )
    if snapshot.remote_config_status is not None:
        lines.append(f"Remote config: {snapshot.remote_config_status}")
    if snapshot.remote_tmux_configured:
        lines.append("Remote tmux source: configured executable")
    if snapshot.remote_runtime is not None:
        lines.append(f"Remote runtime: {snapshot.remote_runtime}")
    if snapshot.launch_family is not None:
        lines.append(f"Remote launch family: {snapshot.launch_family}")
    lines.append("Compatible now: " + ("yes" if snapshot.compatible else "no"))
    if snapshot.detail:
        lines.append(f"Detail: {snapshot.detail}")
    lines.extend(
        (
            "Read-only: yes; no session was attached, created, resized, or replaced.",
            "Privacy: hostname, credentials, session IDs, and content are omitted.",
        )
    )
    return "\n".join(lines)


def render_remote_ssh_terminal_text(
    snapshot: RemoteSshDoctorSnapshot,
    stream: TextIO,
) -> str:
    """Add restrained terminal styling without changing the plain contract."""
    lines = render_remote_ssh_text(snapshot).splitlines()
    rendered: list[str] = []
    failure_statuses = {
        "connection_failed",
        "incompatible",
        "ssh_missing",
        "ssh_unavailable",
        "timeout",
    }
    for index, line in enumerate(lines):
        if index == 0:
            title, marker, suffix = line.partition(" (")
            rendered.append(
                styled(title, STYLE_ACCENT, stream=stream)
                + (
                    styled(marker + suffix, STYLE_MUTED, stream=stream)
                    if marker
                    else ""
                )
            )
            continue
        if line.startswith(("Read-only:", "Privacy:")):
            rendered.append(styled(line, STYLE_MUTED, stream=stream))
            continue
        label, separator, value = line.partition(": ")
        if not separator:
            rendered.append(line)
            continue
        value_style = ""
        if label == "Status":
            if snapshot.status in failure_statuses:
                value_style = STYLE_ERROR
            elif snapshot.status == "ready":
                value_style = STYLE_SUCCESS
            else:
                value_style = STYLE_WARNING
        elif label == "Compatible now":
            value_style = STYLE_SUCCESS if snapshot.compatible else STYLE_ERROR
        elif label == "Detail":
            value_style = STYLE_WARNING
        rendered_value = (
            styled(value, value_style, stream=stream) if value_style else value
        )
        rendered.append(
            f"{styled(label + ':', STYLE_HEADING, stream=stream)} {rendered_value}"
        )
    return "\n".join(rendered)


def run_remote_ssh_doctor(
    destination: str,
    *,
    ssh_args: Sequence[str] = (),
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    json_output: bool = False,
    remote_platform: str = "auto",
) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    status = TransientStatusLine(
        stderr,
        enabled=not json_output and stream_is_tty(stdout),
    )
    status.show(
        command_status(
            "railmux doctor",
            "Checking remote SSH compatibility…",
            stream=stderr,
        )
    )
    try:
        snapshot = collect_remote_ssh_snapshot(
            destination,
            ssh_args=ssh_args,
            remote_platform=remote_platform,
        )
    finally:
        status.clear()
    if json_output:
        json.dump(asdict(snapshot), stdout, indent=2, sort_keys=True)
        print(file=stdout)
    else:
        print(render_remote_ssh_terminal_text(snapshot, stdout), file=stdout)
    return 0 if snapshot.compatible else 2
