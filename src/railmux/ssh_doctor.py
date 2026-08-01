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


SSH_DOCTOR_SCHEMA_VERSION = 2
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
    compatible: bool = False
    detail: str | None = None
    read_only: bool = True
    host_omitted: bool = True


def collect_remote_ssh_snapshot(
    destination: str,
    *,
    ssh_args: Sequence[str] = (),
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
        RemoteStartKind,
        _spawn_remote,
        _stop_unstarted_remote,
        await_remote_startup,
        build_ssh_argv,
    )

    argv = build_ssh_argv(
        destination,
        session="railmux",
        width=80,
        height=24,
        fps=20.0,
        ssh_args=ssh_args,
        existing_session_only=True,
    )
    try:
        process = _spawn_remote(argv)
    except RuntimeError as exc:
        return RemoteSshDoctorSnapshot(
            SSH_DOCTOR_SCHEMA_VERSION,
            "ssh_unavailable",
            __version__,
            PROTOCOL_VERSION,
            detail=str(exc),
        )
    try:
        startup = await_remote_startup(process, timeout=_PROBE_TIMEOUT)
    finally:
        _stop_unstarted_remote(process)

    if startup.kind is RemoteStartKind.MISSING:
        return RemoteSshDoctorSnapshot(
            SSH_DOCTOR_SCHEMA_VERSION,
            "railmux_missing",
            __version__,
            PROTOCOL_VERSION,
            detail="remote Railmux is not installed or discoverable",
        )
    if startup.kind is RemoteStartKind.TIMEOUT:
        return RemoteSshDoctorSnapshot(
            SSH_DOCTOR_SCHEMA_VERSION,
            "timeout",
            __version__,
            PROTOCOL_VERSION,
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
            "ready_with_version_difference"
            if offered_prompt is not None
            else "ready"
        )
        compatible = True
    elif decision.action == "tmux_missing":
        status = (
            "configured_tmux_missing"
            if hello.tmux_configured
            else "tmux_missing"
        )
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
        compatible=compatible,
        detail=(
            "run 'railmux config' on the remote host to correct or reset the "
            "tmux executable"
            if status == "configured_tmux_missing"
            else decision.reason or decision.warning
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


def run_remote_ssh_doctor(
    destination: str,
    *,
    ssh_args: Sequence[str] = (),
    stdout: TextIO | None = None,
    json_output: bool = False,
) -> int:
    stdout = sys.stdout if stdout is None else stdout
    snapshot = collect_remote_ssh_snapshot(destination, ssh_args=ssh_args)
    if json_output:
        json.dump(asdict(snapshot), stdout, indent=2, sort_keys=True)
        print(file=stdout)
    else:
        print(render_remote_ssh_text(snapshot), file=stdout)
    return 0 if snapshot.compatible else 2
