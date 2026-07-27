#!/usr/bin/env python3
"""Record deterministic, credential-free Railmux website demos as asciicast v2.

The recorder launches the checkout through Railmux's normal CLI in a private
tmux server, with a temporary HOME, synthetic Claude history, and a local demo
agent executable.  The agent replays a reviewed public response captured once
with no provider session persistence; CI never reads provider configuration or
credentials.  The resulting casts are suitable for the website's terminal
player and any asciicast-compatible tool.
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
import socket
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
REAL_AGENT_RESPONSE = ROOT / "web" / "demo" / "real-agent-response.txt"
DEFAULT_DESKTOP_OUTPUT = GENERATED / "railmux-demo.cast"
DEFAULT_WORKFLOW_OUTPUT = GENERATED / "railmux-workflow-demo.cast"
DEFAULT_MOBILE_OUTPUT = GENERATED / "railmux-mobile-demo.cast"


@dataclass(frozen=True)
class RecordingProfile:
    name: str
    width: int
    height: int
    duration: float


DESKTOP = RecordingProfile("desktop", 210, 42, 13.0)
WORKFLOW = RecordingProfile("workflow", 76, 30, 12.0)
MOBILE = RecordingProfile("mobile", 46, 26, 9.0)
TEMP_FIXTURE_PATTERN = re.compile(rb"/tmp/railmux-web-demo-[A-Za-z0-9_-]*")
PASSTHROUGH_ENV = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SHELL",
    "TERMINFO",
    "TERMINFO_DIRS",
    "TZ",
)


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

    (home / ".tmux.conf").write_text(
        "set -g status-left ''\nset -g status-right ''\n",
        encoding="utf-8",
    )

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

    real_response = REAL_AGENT_RESPONSE.read_text(encoding="utf-8").strip()
    if not real_response:
        raise RuntimeError(f"empty public agent response: {REAL_AGENT_RESPONSE}")
    response_literal = json.dumps(real_response + "\n", ensure_ascii=False)

    demo_agent = bin_dir / "demo-agent"
    demo_agent.write_text(
        f"""#!/usr/bin/env python3
import sys
import time

sys.stdout.write("\\033[2J\\033[H")
sys.stdout.write("\\033[38;5;244m• Claude · isolated public demo\\033[0m\\r\\n\\r\\n")
sys.stdout.write("\\033[48;5;52m\\033[38;5;210m- forward_wheel(event)\\033[0m\\r\\n")
sys.stdout.write("\\033[48;5;22m\\033[38;5;157m+ history.scroll(event.rows)\\033[0m\\r\\n\\r\\n")
sys.stdout.write({response_literal}.replace("\\n", "\\r\\n"))
sys.stdout.write("\\r\\n\\033[38;5;70m✓ focused test proposed\\033[0m\\r\\n\\r\\n")
sys.stdout.write("\\033[38;5;244m› Real response replayed; provider session was not persisted.\\033[0m\\r\\n")
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
    env = {key: os.environ[key] for key in PASSTHROUGH_ENV if key in os.environ}
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_RUNTIME_DIR": str(runtime),
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "TMUX_TMPDIR": str(tmux_tmp),
            "RAILMUX_TMUX_LABEL": f"railmux-web-demo-{os.getpid()}",
            "PYTHONPATH": source_root,
        }
    )
    return claude_home, env


def _event(timestamp: float, kind: str, data: bytes) -> str:
    return json.dumps(
        [round(timestamp, 6), kind, data.decode("utf-8", errors="replace")],
        ensure_ascii=False,
    )


def _sanitize_fixture_path(chunk: bytes, stable_path: bytes) -> bytes:
    """Replace full or terminal-truncated temporary fixture paths cell-for-cell."""

    return TEMP_FIXTURE_PATTERN.sub(
        lambda match: _fixed_width(stable_path, len(match.group(0))),
        chunk,
    )


def _fixed_width(value: bytes, width: int) -> bytes:
    """Return a public label occupying exactly *width* terminal cells."""

    return (value + b" " * width)[:width]


def _sanitize_public_output(chunk: bytes, stable_path: bytes, label: bytes) -> bytes:
    """Remove machine-specific paths, socket labels, and host names."""

    chunk = _sanitize_fixture_path(chunk, stable_path)
    private_values = (
        (label, b"demo-socket"),
        (socket.gethostname().encode(), b"demo-host"),
        (os.uname().nodename.encode(), b"demo-host"),
        (str(ROOT).encode(), b"/demo/railmux-source"),
    )
    for value, replacement in private_values:
        if value:
            chunk = chunk.replace(value, _fixed_width(replacement, len(value)))
    return chunk


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
        stable_fixture_path = b"/demo/railmux-web-workspace-v1"
        started = time.monotonic()
        ready_at: float | None = None
        startup_output = bytearray()
        sent: set[str] = set()

        def elapsed() -> float:
            return time.monotonic() - ready_at if ready_at is not None else -1.0

        def record_input(payload: str) -> None:
            if ready_at is None:
                return
            events.append(
                _event(
                    elapsed(),
                    "i",
                    payload.encode(),
                )
            )

        def send_once(
            name: str,
            at: float,
            payload: bytes,
            cue: str | None,
        ) -> None:
            if name not in sent and elapsed() >= at:
                if cue is not None:
                    record_input(cue)
                os.write(master, payload)
                sent.add(name)

        def cue_once(name: str, at: float, cue: str) -> None:
            if name not in sent and elapsed() >= at:
                record_input(cue)
                sent.add(name)

        def send_client_keys(
            name: str,
            at: float,
            keys: tuple[str, ...],
            cue: str,
        ) -> None:
            if name in sent or elapsed() < at:
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
            record_input(cue)
            sent.add(name)

        required_actions = {
            DESKTOP.name: {
                "launch-primary",
                "split",
                "target-secondary",
                "return-sidebar",
                "launch-secondary",
            },
            WORKFLOW.name: {
                "launch-primary",
                "return-sidebar",
                "reopen-agent",
            },
            MOBILE.name: {
                "launch-primary",
                "mobile-sidebar",
                "mobile-agent",
            },
        }[profile.name]

        try:
            while True:
                if time.monotonic() - started > profile.duration + 15:
                    break
                if ready_at is not None and elapsed() >= profile.duration:
                    break

                real_response_visible = b"Real response replayed" in raw_output
                running_sidebar_visible = b"railmux/(new)" in raw_output
                if profile is DESKTOP:
                    # Build a real two-agent workspace. Function/navigation keys
                    # go through the attached tmux client's key tables so the
                    # recording exercises the same global bindings as a user.
                    send_once(
                        "launch-primary",
                        0.8,
                        b"n",
                        "key|N|Open a real agent",
                    )
                    if real_response_visible:
                        send_client_keys(
                            "split",
                            3.0,
                            ("F8",),
                            "key|F8|Add a second agent pane",
                        )
                    if "split" in sent:
                        send_client_keys(
                            "target-secondary",
                            3.9,
                            ("C-b", "Right"),
                            "key|C-b \u2192|Target agent two",
                        )
                    if "target-secondary" in sent:
                        send_client_keys(
                            "return-sidebar",
                            4.7,
                            ("C-b", "Tab"),
                            "key|C-b Tab|Return to Railmux",
                        )
                    if "return-sidebar" in sent and running_sidebar_visible:
                        send_once(
                            "launch-secondary",
                            5.5,
                            b"n",
                            "key|N|Open the second agent",
                        )
                elif profile is WORKFLOW:
                    # The guided cast makes interaction explicit while the hero
                    # remains a quiet result-focused proof.
                    cue_once(
                        "launch-primary-cue",
                        0.8,
                        "mouse|8|8|New session",
                    )
                    send_once(
                        "launch-primary",
                        1.4,
                        b"\x1b[<0;8;8M\x1b[<0;8;8m",
                        None,
                    )
                    if real_response_visible:
                        send_client_keys(
                            "return-sidebar",
                            5.0,
                            ("C-b", "Tab"),
                            "key|C-b Tab|Back to the sidebar",
                        )
                    if "return-sidebar" in sent and running_sidebar_visible:
                        cue_once(
                            "reopen-agent-cue",
                            6.5,
                            "mouse|10|24|Running session",
                        )
                        send_once(
                            "reopen-agent",
                            7.1,
                            b"\x1b[<0;10;24M\x1b[<0;10;24m",
                            None,
                        )
                else:
                    # Real compact projection: open Agent 1, return to Railmux,
                    # then jump back to the live agent through compact routing.
                    send_once(
                        "launch-primary",
                        0.8,
                        b"n",
                        "key|N|Open agent",
                    )
                    if real_response_visible:
                        send_client_keys(
                            "mobile-sidebar",
                            4.0,
                            ("C-b", "Tab"),
                            "key|C-b Tab|Sidebar",
                        )
                    if "mobile-sidebar" in sent and running_sidebar_visible:
                        send_client_keys(
                            "mobile-agent",
                            5.8,
                            ("C-b", "Tab"),
                            "key|C-b Tab|Agent",
                        )

                readable, _, _ = select.select([master], [], [], 0.05)
                if readable:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    chunk = _sanitize_public_output(
                        chunk,
                        stable_fixture_path,
                        label.encode(),
                    )
                    if ready_at is None:
                        startup_output.extend(chunk)
                        ready = startup_output.upper()
                        if b"PROJECTS" not in ready or b"NEW SESSION" not in ready:
                            continue
                        ready_at = time.monotonic()
                        # The readiness chunk is a full Railmux repaint in
                        # normal startup. Keep it as frame zero and discard
                        # tmux's machine-specific startup chrome.
                        raw_output.extend(chunk)
                        events.append(_event(0, "o", chunk))
                        continue
                    raw_output.extend(chunk)
                    events.append(_event(elapsed(), "o", chunk))
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

        missing_actions = sorted(required_actions - sent)
        if ready_at is None or b"PROJECTS" not in raw_output.upper() or missing_actions:
            tail = raw_output.decode("utf-8", errors="replace")[-800:].strip()
            raise RuntimeError(
                f"Railmux {profile.name} demo missed required milestones"
                + (f" ({', '.join(missing_actions)})" if missing_actions else "")
                + (f": {tail}" if tail else "")
            )
        private_fragments = (
            b"/tmp/railmux-web-demo-",
            label.encode(),
            socket.gethostname().encode(),
            os.uname().nodename.encode(),
            str(ROOT).encode(),
        )
        leaked = [value for value in private_fragments if value and value in raw_output]
        if leaked:
            raise RuntimeError(
                f"Railmux {profile.name} demo retained private recorder metadata"
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
        "--workflow-output",
        type=Path,
        default=DEFAULT_WORKFLOW_OUTPUT,
        help=f"workflow asciicast path (default: {DEFAULT_WORKFLOW_OUTPUT})",
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
        _record(args.workflow_output.resolve(), WORKFLOW)
        _record(args.mobile_output.resolve(), MOBILE)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"record_web_demo.py: error: {exc}", file=sys.stderr)
        return 1
    print(f"recorded desktop Railmux demo: {args.desktop_output.resolve()}")
    print(f"recorded workflow Railmux demo: {args.workflow_output.resolve()}")
    print(f"recorded mobile Railmux demo: {args.mobile_output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
