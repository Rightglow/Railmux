#!/usr/bin/env python3
"""Record a deterministic, credential-free Railmux session as asciicast v2.

The recorder launches the checkout through Railmux's normal CLI in a private
tmux server, with a temporary HOME, synthetic Claude history, and a local demo
agent executable.  It never reads the caller's provider configuration or
credentials.  The resulting cast is suitable for the website's terminal
player and can also be replayed with any asciicast-compatible tool.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "web" / "public" / "generated"
DEFAULT_DESKTOP_OUTPUT = GENERATED / "railmux-demo.cast"
DEFAULT_MOBILE_OUTPUT = GENERATED / "railmux-mobile-demo.cast"


@dataclass(frozen=True)
class RecordingProfile:
    name: str
    width: int
    height: int
    duration: float


DESKTOP = RecordingProfile("desktop", 210, 42, 13.0)
MOBILE = RecordingProfile("mobile", 46, 26, 9.0)
TEMP_FIXTURE_PATTERN = re.compile(rb"/tmp/railmux-web-demo-[A-Za-z0-9_-]*")


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _session_records(title: str, response: str) -> list[dict[str, object]]:
    return [
        {
            "type": "user",
            "timestamp": "2026-07-27T10:00:00.000Z",
            "message": {"role": "user", "content": title},
        },
        {
            "type": "assistant",
            "timestamp": "2026-07-27T10:00:02.000Z",
            "message": {
                "role": "assistant",
                "content": response,
                "stop_reason": "end_turn",
            },
        },
    ]


def _create_fixture(root: Path) -> tuple[Path, dict[str, str]]:
    home = root / "home"
    runtime = root / "runtime"
    tmux_tmp = root / "tmux"
    claude_home = home / ".claude"
    config_dir = home / ".config" / "railmux"
    bin_dir = root / "bin"
    for directory in (
        home,
        runtime,
        tmux_tmp,
        claude_home / "projects",
        config_dir,
        bin_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)

    projects = (
        (
            "railmux",
            (
                (
                    "11111111-1111-4111-8111-111111111111",
                    "Polish SSH history",
                    "The local history overlay now skips superseded frames.",
                ),
                (
                    "22222222-2222-4222-8222-222222222222",
                    "Review layout policy",
                    "Compact mode changes visibility, not running sessions.",
                ),
                (
                    "33333333-3333-4333-8333-333333333333",
                    "Add mobile controls",
                    "The status bar exposes Railmux, Agent 1, and Agent 2.",
                ),
            ),
        ),
        (
            "compiler-lab",
            (
                (
                    "44444444-4444-4444-8444-444444444444",
                    "Review parser",
                    "The parser tests pass in the isolated workspace.",
                ),
            ),
        ),
        (
            "infra-tools",
            (
                (
                    "55555555-5555-4555-8555-555555555555",
                    "Check deployment",
                    "The deployment plan is ready for review.",
                ),
            ),
        ),
    )
    for project_index, (project_name, sessions) in enumerate(projects):
        project = root / "projects" / project_name
        project.mkdir(parents=True)
        encoded = str(project).replace("/", "-")
        project_store = claude_home / "projects" / encoded
        for session_index, (session_id, title, response) in enumerate(sessions):
            session_path = project_store / f"{session_id}.jsonl"
            _write_jsonl(
                session_path,
                _session_records(title, response),
            )
            # Stable ordering: railmux is the most recent project and its first
            # fixture is the selected session on every filesystem.
            fixed_mtime = 1_785_000_000 - project_index * 100 - session_index
            os.utime(session_path, (fixed_mtime, fixed_mtime))

    demo_agent = bin_dir / "demo-agent"
    demo_agent.write_text(
        """#!/usr/bin/env python3
import signal
import sys
import time

sys.stdout.write("\\033[2J\\033[H")
sys.stdout.write("\\033[38;5;244m• Read src/railmux/fast_display_client.py\\033[0m\\r\\n\\r\\n")
sys.stdout.write("I found the scroll owner boundary. The remote client keeps\\r\\n")
sys.stdout.write("rendering live state while history remains a local overlay.\\r\\n\\r\\n")
sys.stdout.write("\\033[48;5;52m\\033[38;5;210m- forward_wheel(event)\\033[0m\\r\\n")
sys.stdout.write("\\033[48;5;22m\\033[38;5;157m+ history.scroll(event.rows)\\033[0m\\r\\n\\r\\n")
sys.stdout.write("\\033[38;5;70m✓ 18 focused tests passed\\033[0m\\r\\n\\r\\n")
sys.stdout.write("\\033[38;5;244m› Demo agent is isolated; no provider credentials used.\\033[0m\\r\\n")
sys.stdout.flush()
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )
    demo_agent.chmod(0o700)

    (config_dir / "config.toml").write_text(
        "[claude]\n"
        f'binary = "{demo_agent}"\n\n'
        "[live]\n"
        "poll_interval_ms = 250\n\n"
        "[updates]\n"
        'auto_update = "never"\n',
        encoding="utf-8",
    )

    source_root = str(ROOT / "src")
    existing_path = os.environ.get("PYTHONPATH")
    pythonpath = (
        source_root
        if not existing_path
        else os.pathsep.join((source_root, existing_path))
    )
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_RUNTIME_DIR": str(runtime),
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "TMUX_TMPDIR": str(tmux_tmp),
            "RAILMUX_TMUX_LABEL": f"railmux-web-demo-{os.getpid()}",
            "PYTHONPATH": pythonpath,
            "NO_COLOR": "",
        }
    )
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return claude_home, env


def _event(timestamp: float, kind: str, data: bytes) -> str:
    return json.dumps(
        [round(timestamp, 6), kind, data.decode("utf-8", errors="replace")],
        ensure_ascii=False,
    )


def _sanitize_fixture_path(chunk: bytes, stable_path: bytes) -> bytes:
    """Replace full or terminal-truncated temporary fixture paths cell-for-cell."""

    return TEMP_FIXTURE_PATTERN.sub(
        lambda match: stable_path[: len(match.group(0))],
        chunk,
    )


def _railmux_python(env: dict[str, str]) -> str:
    """Choose an interpreter that can import the checkout and its dependencies."""
    candidates = [Path(sys.executable), ROOT / ".venv" / "bin" / "python"]
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        probe = subprocess.run(
            [
                str(candidate),
                "-c",
                "import railmux, tomlkit, urwid",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            return str(candidate)
    raise RuntimeError(
        "Railmux dependencies are unavailable; install the checkout first "
        "(python -m pip install -e .)"
    )


def _client_name(label: str, env: dict[str, str]) -> str | None:
    try:
        names = subprocess.check_output(
            ["tmux", "-L", label, "list-clients", "-F", "#{client_name}"],
            env=env,
            stderr=subprocess.DEVNULL,
            text=True,
        ).splitlines()
    except (OSError, subprocess.CalledProcessError):
        return None
    return names[0] if names else None


def _record(output: Path, profile: RecordingProfile) -> None:
    if shutil.which("tmux") is None:
        raise RuntimeError("tmux is required to record the Railmux demo")

    with tempfile.TemporaryDirectory(prefix="railmux-web-demo-", dir="/tmp") as raw:
        fixture_root = Path(raw)
        claude_home, env = _create_fixture(fixture_root)
        env["RAILMUX_TMUX_LABEL"] += f"-{profile.name}"
        python = _railmux_python(env)
        label = env["RAILMUX_TMUX_LABEL"]
        master, slave = pty.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", profile.height, profile.width, 0, 0),
        )
        process = subprocess.Popen(
            [
                python,
                "-m",
                "railmux",
                "--claude-home",
                str(claude_home),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
        os.close(slave)

        header = {
            "version": 2,
            "width": profile.width,
            "height": profile.height,
            "timestamp": 1785146400,
            "duration": profile.duration,
            "command": f"railmux (isolated {profile.name} website demo)",
            "title": (f"Railmux — real {profile.name} tmux UI, credential-free demo"),
            "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
        }
        events: list[str] = []
        raw_output = bytearray()
        fixture_path = str(fixture_root).encode()
        stable_fixture_path = b"/demo/railmux-web-workspace-v1"
        if len(fixture_path) != len(stable_fixture_path):
            raise RuntimeError("temporary demo path has an unexpected width")
        started = time.monotonic()
        sent: set[str] = set()

        def record_input(payload: bytes) -> None:
            events.append(
                _event(
                    time.monotonic() - started,
                    "i",
                    payload,
                )
            )

        def send_once(name: str, at: float, payload: bytes) -> None:
            if name not in sent and time.monotonic() - started >= at:
                record_input(payload)
                os.write(master, payload)
                sent.add(name)

        def send_client_keys(
            name: str,
            at: float,
            keys: tuple[str, ...],
            display_bytes: bytes,
        ) -> None:
            if name in sent or time.monotonic() - started < at:
                return
            client = _client_name(label, env)
            if client is None:
                return
            for key in keys:
                result = subprocess.run(
                    [
                        "tmux",
                        "-L",
                        label,
                        "send-keys",
                        "-K",
                        "-c",
                        client,
                        key,
                    ],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if result.returncode:
                    return
            record_input(display_bytes)
            sent.add(name)

        try:
            while time.monotonic() - started < profile.duration:
                elapsed = time.monotonic() - started
                if profile is DESKTOP:
                    # Build a real two-agent workspace. Function/navigation keys
                    # go through the attached tmux client's key tables so the
                    # recording exercises the same global bindings as a user.
                    send_once("launch-primary", 2.2, b"n")
                    send_client_keys("split", 4.6, ("F8",), b"\x1b[19~")
                    send_client_keys(
                        "target-secondary",
                        5.4,
                        ("C-b", "Right"),
                        b"\x02\x1b[C",
                    )
                    send_client_keys(
                        "return-sidebar",
                        6.1,
                        ("C-b", "Tab"),
                        b"\x02\t",
                    )
                    send_once("launch-secondary", 6.8, b"n")
                else:
                    # Real compact projection: open Agent 1, return to Railmux,
                    # then jump back to the live agent through compact routing.
                    send_once("launch-primary", 2.2, b"n")
                    send_client_keys(
                        "mobile-sidebar",
                        4.8,
                        ("C-b", "Tab"),
                        b"\x02\t",
                    )
                    send_client_keys(
                        "mobile-agent",
                        6.5,
                        ("C-b", "Tab"),
                        b"\x02\t",
                    )

                readable, _, _ = select.select([master], [], [], 0.05)
                if readable:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    # The UI legitimately shows an absolute project path in a
                    # transient status message. A narrow terminal may truncate
                    # that path before the temporary suffix ends, so sanitize
                    # every visible prefix while preserving its cell width.
                    chunk = _sanitize_fixture_path(chunk, stable_fixture_path)
                    raw_output.extend(chunk)
                    events.append(_event(elapsed, "o", chunk))
                if process.poll() is not None:
                    break
        finally:
            subprocess.run(
                ["tmux", "-L", label, "kill-server"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            os.close(master)

        if b"PROJECTS" not in raw_output.upper():
            tail = raw_output.decode("utf-8", errors="replace")[-800:].strip()
            raise RuntimeError(
                "Railmux demo exited before drawing the workspace"
                + (f": {tail}" if tail else "")
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(header, ensure_ascii=False) + "\n" + "\n".join(events) + "\n",
            encoding="utf-8",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--desktop-output",
        type=Path,
        default=DEFAULT_DESKTOP_OUTPUT,
        help=f"desktop asciicast path (default: {DEFAULT_DESKTOP_OUTPUT})",
    )
    parser.add_argument(
        "--mobile-output",
        type=Path,
        default=DEFAULT_MOBILE_OUTPUT,
        help=f"mobile asciicast path (default: {DEFAULT_MOBILE_OUTPUT})",
    )
    args = parser.parse_args(argv)
    try:
        _record(args.desktop_output.resolve(), DESKTOP)
        _record(args.mobile_output.resolve(), MOBILE)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"record_web_demo.py: error: {exc}", file=sys.stderr)
        return 1
    print(f"recorded desktop Railmux demo: {args.desktop_output.resolve()}")
    print(f"recorded mobile Railmux demo: {args.mobile_output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
