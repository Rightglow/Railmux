#!/usr/bin/env python3
"""Record deterministic, credential-free Railmux website demos as asciicast v2.

The recorder launches the checkout through Railmux's normal CLI in a private
tmux server, with a temporary HOME, synthetic provider histories, and local
demo-agent executables. The agents replay reviewed public responses captured
without provider session persistence; CI never reads provider configuration or
credentials. The resulting casts are suitable for the website's terminal
player and any asciicast-compatible tool.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
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
REAL_AGENT_RUNS = ROOT / "web" / "demo" / "real-agent-runs.json"
DEFAULT_DESKTOP_OUTPUT = GENERATED / "railmux-demo.cast"
DEFAULT_DUAL_OUTPUT = GENERATED / "railmux-dual-demo.cast"
DEFAULT_WORKFLOW_OUTPUT = GENERATED / "railmux-workflow-demo.cast"
DEFAULT_MOBILE_OUTPUT = GENERATED / "railmux-mobile-demo.cast"
DEFAULT_TOUR_OUTPUT = GENERATED / "railmux-tour-demo.cast"
DEFAULT_CONTROLS_OUTPUT = GENERATED / "railmux-controls-demo.cast"


@dataclass(frozen=True)
class RecordingProfile:
    name: str
    width: int
    height: int
    duration: float


DESKTOP = RecordingProfile("desktop", 180, 38, 10.0)
DUAL = RecordingProfile("dual", 210, 42, 15.5)
WORKFLOW = RecordingProfile("workflow", 160, 38, 20.0)
# A representative portrait phone geometry. Compact mode is selected by the
# narrow width; the separately documented 105x21 Termux report was landscape.
MOBILE = RecordingProfile("mobile", 46, 38, 10.0)
TOUR = RecordingProfile("tour", 160, 38, 10.0)
CONTROLS = RecordingProfile("controls", 180, 38, 27.0)
STARTUP_HOLD_SECONDS = 2.4
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
FORBIDDEN_TRANSCRIPT_FRAGMENTS = (
    b"/home/",
    b"/Users/",
    b"ANTHROPIC_API_KEY",
    b"OPENAI_API_KEY",
    b"sk-ant-",
    b"ghp_",
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


def _load_agent_runs() -> tuple[dict[str, object], str]:
    raw = REAL_AGENT_RUNS.read_bytes()
    leaked = [
        fragment for fragment in FORBIDDEN_TRANSCRIPT_FRAGMENTS if fragment in raw
    ]
    if leaked:
        raise RuntimeError(
            f"public agent capture contains private-looking data: {REAL_AGENT_RUNS}"
        )
    digest = hashlib.sha256(raw).hexdigest()
    data = json.loads(raw)
    runs = data.get("runs")
    if not isinstance(runs, list) or len(runs) < 3:
        raise RuntimeError(f"expected three public agent runs: {REAL_AGENT_RUNS}")
    for run in runs:
        if not isinstance(run, dict):
            raise RuntimeError(f"invalid public agent run: {REAL_AGENT_RUNS}")
        for field in (
            "agent",
            "captured_at",
            "source_commit",
            "capture_method",
            "title",
            "prompt",
            "files",
            "response",
        ):
            if not run.get(field):
                raise RuntimeError(
                    f"public agent run is missing {field}: {REAL_AGENT_RUNS}"
                )
    if {run["agent"] for run in runs} != {"Claude Code", "Codex"}:
        raise RuntimeError(
            f"public captures must include Claude Code and Codex: {REAL_AGENT_RUNS}"
        )
    banner = data.get("startup_banner")
    if not isinstance(banner, dict):
        raise RuntimeError(f"missing public startup banner: {REAL_AGENT_RUNS}")
    for field in (
        "agent",
        "captured_at",
        "capture_method",
        "version_command",
        "version_output",
        "lines",
    ):
        if not banner.get(field):
            raise RuntimeError(
                f"public startup banner is missing {field}: {REAL_AGENT_RUNS}"
            )
    lines = banner["lines"]
    if (
        not isinstance(lines, list)
        or len(lines) != 3
        or "{cwd}" not in lines[-1]
        or "2.1.220" not in banner["version_output"]
    ):
        raise RuntimeError(f"invalid public startup banner: {REAL_AGENT_RUNS}")
    return data, digest


def _create_fixture(root: Path) -> tuple[Path, dict[str, str]]:
    home = root / "home"
    runtime = root / "runtime"
    tmux_tmp = root / "tmux"
    claude_home = home / ".claude"
    codex_home = home / ".codex"
    config_dir = home / ".config" / "railmux"
    bin_dir = root / "bin"
    for directory in (
        home,
        runtime,
        tmux_tmp,
        claude_home / "projects",
        codex_home / "sessions",
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

    codex_project = root / "projects" / "railmux"
    codex_rollout = (
        codex_home
        / "sessions"
        / "2026"
        / "07"
        / "28"
        / "rollout-2026-07-28T10-00-00-019fa707-1ef5-70d3-bf86-f2bdb7c1457b.jsonl"
    )
    _write_jsonl(
        codex_rollout,
        [
            {
                "timestamp": "2026-07-28T10:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "019fa707-1ef5-70d3-bf86-f2bdb7c1457b",
                    "timestamp": "2026-07-28T10:00:00.000Z",
                    "cwd": str(codex_project),
                    "originator": "codex_cli_rs",
                    "cli_version": "0.145.0",
                    "source": "cli",
                    "thread_source": "user",
                    "model_provider": "openai",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": "Explain workspace layout",
                    }],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": "Compact presentation keeps detached agents alive.",
                    }],
                },
            },
        ],
    )
    os.utime(codex_rollout, (1_785_000_050, 1_785_000_050))

    agent_capture, _transcript_digest = _load_agent_runs()
    runs_json_literal = repr(json.dumps(agent_capture["runs"], ensure_ascii=False))
    banner_json_literal = repr(
        json.dumps(agent_capture["startup_banner"]["lines"], ensure_ascii=False)
    )
    resumed_sessions_literal = repr(
        json.dumps(
            {
                "11111111-1111-4111-8111-111111111111": {
                    "agent": "Claude Code",
                    "title": "Polish SSH history",
                    "prompt": "Polish SSH history",
                    "response": (
                        "The local history overlay now skips superseded frames while "
                        "keeping the latest terminal state responsive."
                    ),
                },
            },
            ensure_ascii=False,
        )
    )
    counter_literal = repr(str(root / "agent-invocations"))

    demo_agent = bin_dir / "demo-agent"
    demo_agent_source = """#!/usr/bin/env python3
import fcntl
import json
import os
import re
import shutil
import signal
import sys
import textwrap

RUNS = json.loads(__RUNS_JSON__)
CLAUDE_BANNER = json.loads(__BANNER_JSON__)
RESUMED_SESSIONS = json.loads(__RESUMED_SESSIONS__)
COUNTER = __COUNTER__
PROFILE = os.environ.get("RAILMUX_DEMO_PROFILE", "")


def clipped(text, width):
    return text[:max(0, width)]


def write_row(parts, row, text):
    parts.append(f"\\033[{row};1H\\033[2K" + text)


BODY_TOKEN = re.compile(
    r"`[^`]+`"
    r"|(?:src|tests)/[A-Za-z0-9_./-]+(?:::[A-Za-z0-9_]+)?"
    r"|[A-Za-z_][A-Za-z0-9_]*(?:\\(\\)|\\.[A-Za-z_][A-Za-z0-9_]*)+"
    r"|\\b(?:True|False|None|COMPACT|WIDE|SINGLE|SIDE_BY_SIDE|STACKED)\\b"
    r'|b""|=='
)


def styled_body(text):
    # Restrained semantic colour matching the agent TUIs.
    pieces = ["\\033[22;38;5;252m"]
    offset = 0
    for match in BODY_TOKEN.finditer(text):
        pieces.append(text[offset:match.start()])
        token = match.group(0)
        if token.startswith(("src/", "tests/")):
            colour = 114
        elif token in {
            "True", "False", "None", "COMPACT", "WIDE", "SINGLE",
            "SIDE_BY_SIDE", "STACKED", 'b""', "==",
        }:
            colour = 179
        else:
            colour = 110
        if token.startswith("`") and token.endswith("`"):
            token = token[1:-1]
        pieces.append(f"\\033[38;5;{colour}m" + token)
        pieces.append("\\033[38;5;252m")
        offset = match.end()
    pieces.append(text[offset:])
    return "".join(pieces)


def render_claude(run, columns, rows):
    width = max(18, columns - 1)
    footer_row = max(8, rows - 3)
    content_rows = []
    cwd = os.getcwd()
    for line in CLAUDE_BANNER:
        line = line.replace("{cwd}", cwd)
        # Claude Code's mark is warm coral; the product/version text remains
        # neutral so the banner reads like the real TUI rather than a logo
        # pasted over the terminal recording.
        content_rows.append(
            "\\033[38;5;173m" + line[:11]
            + "\\033[38;5;252m" + line[11:]
        )
    content_rows.append("")
    for index, line in enumerate(textwrap.wrap(run["prompt"], max(16, width - 4))):
        prefix = "\\033[38;5;147m❯\\033[0m " if index == 0 else "  "
        content_rows.append(prefix + "\\033[38;5;252m" + line)
    content_rows.extend((
        "",
        "\\033[38;5;147m●\\033[0m \\033[1;38;5;252m" + run["title"],
    ))
    for paragraph in run["response"].splitlines():
        for line in textwrap.wrap(paragraph, max(16, width - 2)):
            content_rows.append("  " + styled_body(line))

    parts = ["\\033[2J\\033[H"]
    for row, line in enumerate(content_rows[:footer_row - 1], start=1):
        write_row(parts, row, line)
    rule = "─" * width
    write_row(parts, footer_row, "\\033[38;5;239m" + rule)
    write_row(parts, footer_row + 1, "\\033[38;5;147m❯\\033[0m ")
    write_row(parts, footer_row + 2, "\\033[38;5;239m" + rule)
    write_row(
        parts,
        footer_row + 3,
        "\\033[38;5;244m  ⏵⏵ normal mode · shift+tab to cycle · ← for agents",
    )
    return "".join(parts) + "\\033[0m"


def render_empty_claude(columns, rows):
    # A newly opened Claude session before its first prompt.
    width = max(18, columns - 1)
    footer_row = max(8, rows - 3)
    cwd = os.getcwd()
    content_rows = []
    for line in CLAUDE_BANNER:
        line = line.replace("{cwd}", cwd)
        content_rows.append(
            "\\033[38;5;173m" + line[:11]
            + "\\033[38;5;252m" + line[11:]
        )

    parts = ["\\033[2J\\033[H"]
    for row, line in enumerate(content_rows, start=1):
        write_row(parts, row, line)
    rule = "─" * width
    write_row(parts, footer_row, "\\033[38;5;239m" + rule)
    write_row(parts, footer_row + 1, "\\033[38;5;147m❯\\033[0m ")
    write_row(parts, footer_row + 2, "\\033[38;5;239m" + rule)
    write_row(
        parts,
        footer_row + 3,
        "\\033[38;5;244m  ⏵⏵ normal mode · shift+tab to cycle · ← for agents",
    )
    return "".join(parts) + "\\033[0m"


def render_codex(run, columns, rows):
    width = max(18, columns - 1)
    footer_row = max(9, rows - 2)
    inner = max(12, width - 4)
    cwd = os.getcwd()
    box_rule = "─" * (width - 2)
    content_rows = [
        "\\033[38;5;244m╭" + box_rule + "╮",
        "\\033[38;5;244m│ \\033[1;38;5;75m>_ OpenAI Codex (v0.145.0)"
        + "\\033[22;38;5;244m" + " " * max(1, inner - 27) + "│",
        "\\033[38;5;244m│ model: \\033[38;5;110mgpt-5.3-codex"
        + "\\033[38;5;244m" + " " * max(1, inner - 22) + "│",
        "\\033[38;5;244m│ directory: \\033[38;5;114m"
        + clipped(cwd, max(8, inner - 11))
        + "\\033[38;5;244m"
        + " " * max(1, inner - 11 - len(clipped(cwd, max(8, inner - 11)))) + "│",
        "\\033[38;5;244m╰" + box_rule + "╯",
        "",
        "\\033[38;5;75m•\\033[0m \\033[1;38;5;252m" + run["title"],
    ]
    for paragraph in run["response"].splitlines():
        for line in textwrap.wrap(paragraph, max(16, width - 2)):
            content_rows.append("  " + styled_body(line))

    parts = ["\\033[2J\\033[H"]
    for row, line in enumerate(content_rows[:footer_row - 1], start=1):
        write_row(parts, row, line)
    write_row(parts, footer_row, "\\033[38;5;252m› Ask Codex to do anything")
    write_row(parts, footer_row + 1, "\\033[38;5;244m  ? for shortcuts")
    return "".join(parts) + "\\033[0m"


def repaint(_signum=None, _frame=None):
    size = shutil.get_terminal_size((88, 24))
    if PROFILE == "workflow" and agent_kind == "claude" and session_id is None:
        rendered = render_empty_claude(size.columns, size.lines)
    else:
        renderer = render_codex if agent_kind == "codex" else render_claude
        rendered = renderer(run, size.columns, size.lines)
    sys.stdout.write(rendered)
    sys.stdout.flush()


with open(COUNTER, "a+", encoding="utf-8") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    handle.seek(0)
    value = handle.read().strip()
    invocation = int(value) if value else 0
    handle.seek(0)
    handle.truncate()
    handle.write(str(invocation + 1))
    handle.flush()

session_id = None
if "--resume" in sys.argv:
    position = sys.argv.index("--resume")
    if position + 1 < len(sys.argv):
        session_id = sys.argv[position + 1]
agent_kind = "codex" if "codex" in os.path.basename(sys.argv[0]) else "claude"
candidates = [
    item for item in RUNS
    if item["agent"] == ("Codex" if agent_kind == "codex" else "Claude Code")
]
profile_offsets = {
    "desktop": 0,
    "dual": 1,
    "mobile": 1,
    "controls": 0,
}
candidate_index = invocation + profile_offsets.get(PROFILE, 0)
run = RESUMED_SESSIONS.get(
    session_id, candidates[candidate_index % len(candidates)]
)
signal.signal(signal.SIGWINCH, repaint)
repaint()
while True:
    signal.pause()
"""
    demo_agent_source = (
        demo_agent_source.replace("__RUNS_JSON__", runs_json_literal)
        .replace("__BANNER_JSON__", banner_json_literal)
        .replace("__RESUMED_SESSIONS__", resumed_sessions_literal)
        .replace("__COUNTER__", counter_literal)
    )
    demo_agent.write_text(
        demo_agent_source,
        encoding="utf-8",
    )
    demo_agent.chmod(0o700)
    demo_codex = bin_dir / "demo-codex"
    demo_codex.symlink_to(demo_agent)

    (config_dir / "config.toml").write_text(
        "[claude]\n"
        f'binary = "{demo_agent}"\n\n'
        "[codex]\n"
        f'binary = "{demo_codex}"\n'
        f'home = "{codex_home}"\n'
        'auto_run = "never"\n\n'
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


def _utf8_event_boundary(data: bytes, preferred: int) -> int:
    """Move a cast-event boundary left if it bisects one UTF-8 character."""
    cut = max(0, min(len(data), preferred))
    for candidate in range(cut, max(-1, cut - 4), -1):
        try:
            data[:candidate].decode("utf-8")
        except UnicodeDecodeError:
            continue
        return candidate
    return 0


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


def _startup_surface(
    python: str,
    env: dict[str, str],
    width: int,
    height: int,
) -> bytes:
    """Render the exact installed Railmux startup surface."""
    output = subprocess.check_output(
        [
            python,
            "-c",
            (
                "import sys; "
                "from railmux.pane_surface import render_startup_surface; "
                "sys.stdout.write(render_startup_surface("
                f"{width}, {height}))"
            ),
        ],
        env=env,
        stderr=subprocess.DEVNULL,
    )
    # The renderer writes ordinary ``\n`` because this subprocess uses a
    # pipe. A real TTY's ONLCR would turn those into CRLF; asciicast replays
    # bytes literally, so normalize here or each centered row starts at the
    # previous row's cursor column and wraps across the player.
    return output.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


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


def _running_row_follows_empty_state(output: bytes | bytearray) -> bool:
    """Whether the latest Codex repaint replaced its empty Running state."""
    return (
        output.rfind(b"railmux/(new)")
        > output.rfind(b"(no running Codex sessions)")
    )


def _record(output: Path, profile: RecordingProfile) -> None:
    if shutil.which("tmux") is None:
        raise RuntimeError("tmux is required to record the Railmux demo")

    with tempfile.TemporaryDirectory(prefix="railmux-web-demo-", dir="/tmp") as raw:
        fixture_root = Path(raw)
        claude_home, env = _create_fixture(fixture_root)
        env["RAILMUX_DEMO_PROFILE"] = profile.name
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

        startup_offset = STARTUP_HOLD_SECONDS if profile is DESKTOP else 0.0
        total_duration = profile.duration + startup_offset
        header = {
            "version": 2,
            "width": profile.width,
            "height": profile.height,
            "timestamp": 1785146400,
            "duration": total_duration,
            "command": f"railmux (isolated {profile.name} website demo)",
            "title": (f"Railmux — real {profile.name} tmux UI, credential-free demo"),
            "transcript_sha256": _load_agent_runs()[1],
            "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
        }
        events: list[str] = []
        raw_output = bytearray()
        stable_fixture_path = b"/demo/railmux-web-workspace-v1"
        started = time.monotonic()
        ready_at: float | None = None
        startup_output = bytearray()
        post_resize_output = bytearray()
        sanitizer_tail = bytearray()
        sanitizer_hold = 512
        sent: set[str] = set()
        controls_exit_frozen = False

        def elapsed() -> float:
            return time.monotonic() - ready_at if ready_at is not None else -1.0

        def cast_time() -> float:
            return max(0.0, startup_offset + elapsed())

        def record_input(payload: str) -> None:
            if ready_at is None:
                return
            events.append(
                _event(
                    cast_time(),
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
                "startup-surface",
                "launch-primary",
            },
            DUAL.name: {
                "launch-primary",
                "split",
                "target-secondary",
                "return-sidebar",
                "switch-codex",
                "launch-secondary",
                "secondary-running",
            },
            WORKFLOW.name: {
                "preview-session",
                "resume-session",
                "return-sidebar",
                "launch-second",
                "return-sidebar-again",
                "switch-running-agent",
            },
            MOBILE.name: {
                "launch-primary",
                "mobile-sidebar",
                "mobile-agent",
            },
            TOUR.name: {
                "new-project",
                "close-new-project",
                "open-help",
            },
            CONTROLS.name: {
                "launch-primary",
                "return-sidebar",
                "expand-more",
                "switch-codex",
                "cycle-layout",
                "open-quit",
                "soft-quit",
                "finish-soft-quit",
            },
        }[profile.name]

        try:
            while True:
                if time.monotonic() - started > profile.duration + 15:
                    break
                if ready_at is not None and elapsed() >= profile.duration:
                    break

                real_response_visible = "← for agents".encode() in raw_output
                preview_visible = b"Read-only history preview" in raw_output
                running_sidebar_visible = b"railmux/(new)" in raw_output
                if profile is DESKTOP:
                    # The opening website view stays deliberately simple: one
                    # sidebar and one native-looking agent pane.
                    send_once(
                        "launch-primary",
                        0.8,
                        b"n",
                        "key|N|Open a real agent",
                    )
                elif profile is DUAL:
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
                            "switch-codex",
                            5.3,
                            b"m",
                            "key|M|Switch to Codex mode",
                        )
                    if (
                        "switch-codex" in sent
                        and b"Explain workspace layout" in raw_output
                    ):
                        send_once(
                            "launch-secondary",
                            6.2,
                            b"n",
                            "key|N|Open Codex in agent two",
                        )
                elif profile is WORKFLOW:
                    # Preview a stopped transcript without launching it, resume
                    # that exact conversation, start a genuinely empty second
                    # conversation, then use Running to switch back.
                    cue_once(
                        "preview-session-cue",
                        0.8,
                        "mouse|10|13|Preview stopped session",
                    )
                    send_once(
                        "preview-session",
                        1.4,
                        b"\x1b[<0;10;13M\x1b[<0;10;13m",
                        None,
                    )
                    if preview_visible:
                        send_once(
                            "resume-session",
                            4.2,
                            b"\r",
                            "key|Enter|Resume this conversation",
                        )
                    if "resume-session" in sent and real_response_visible:
                        send_once(
                            "return-sidebar",
                            7.4,
                            b"\x02\t",
                            "key|C-b Tab|Back to the sidebar",
                        )
                    if "return-sidebar" in sent and b"RUNNING" in raw_output.upper():
                        send_once(
                            "launch-second",
                            8.5,
                            b"n",
                            "key|N|Start an empty session",
                        )
                    if "launch-second" in sent:
                        send_once(
                            "return-sidebar-again",
                            12.2,
                            b"\x02\t",
                            "key|C-b Tab|See both running sessions",
                        )
                    if (
                        "return-sidebar-again" in sent
                        and b"RUNNING" in raw_output.upper()
                    ):
                        cue_once(
                            "switch-running-agent-cue",
                            13.7,
                            "mouse|10|27|Switch to Polish SSH history",
                        )
                        send_once(
                            "switch-running-agent",
                            14.3,
                            b"\x1b[<0;10;27M\x1b[<0;10;27m",
                            None,
                        )
                elif profile is MOBILE:
                    # Real compact projection: open Agent 1, then exercise the
                    # actual clickable [R] and [1] bottom-row page controls.
                    send_once(
                        "launch-primary",
                        0.8,
                        b"n",
                        "key|N|Open agent",
                    )
                    if real_response_visible:
                        send_once(
                            "mobile-sidebar",
                            4.0,
                            b"\x1b[<0;2;38M\x1b[<0;2;38m",
                            "mouse|2|38|Open [R] sidebar",
                        )
                    if "mobile-sidebar" in sent and running_sidebar_visible:
                        send_once(
                            "mobile-agent",
                            5.8,
                            b"\x1b[<0;5;38M\x1b[<0;5;38m",
                            "mouse|5|38|Open [1] agent",
                        )
                elif profile is TOUR:
                    # Show two discoverable entry points without creating a
                    # project or starting a provider session.
                    cue_once(
                        "new-project-cue",
                        0.6,
                        "mouse|10|2|Open New project",
                    )
                    send_once(
                        "new-project",
                        1.2,
                        b"\x1b[<0;10;2M\x1b[<0;10;2m",
                        None,
                    )
                    if b"Choose directory" in raw_output:
                        send_once(
                            "close-new-project",
                            4.0,
                            b"\x1b",
                            "key|Esc|Close directory browser",
                        )
                    if (
                        "close-new-project" in sent
                        and b"PROJECTS" in raw_output.upper()
                    ):
                        send_once(
                            "open-help",
                            5.2,
                            b"?",
                            "key|?|Open Help",
                        )
                else:
                    # One bounded control story: keep a Claude session alive,
                    # expose More, switch the sidebar to Codex, cycle layout,
                    # then show the real quit confirmation and complete a soft
                    # quit, preserving the isolated agent session.
                    send_once(
                        "launch-primary",
                        0.8,
                        b"n",
                        "key|N|Start a Claude Code session",
                    )
                    if real_response_visible:
                        send_client_keys(
                            "return-sidebar",
                            4.0,
                            ("C-b", "Tab"),
                            "key|C-b Tab|Return to Railmux",
                        )
                    if "return-sidebar" in sent and running_sidebar_visible:
                        send_once(
                            "expand-more",
                            6.2,
                            b"+",
                            "key|+|Show Mode, Layout, and Options",
                        )
                    if "expand-more" in sent:
                        send_once(
                            "switch-codex",
                            8.5,
                            b"m",
                            "key|M|Switch sidebar to Codex",
                        )
                    if (
                        "switch-codex" in sent
                        and b"Explain workspace layout" in raw_output
                    ):
                        send_client_keys(
                            "cycle-layout",
                            11.0,
                            ("F8",),
                            "key|F8|Cycle workspace layout",
                        )
                    if "cycle-layout" in sent:
                        send_once(
                            "open-quit",
                            13.8,
                            b"q",
                            "key|Q|Compare Quit and Soft Quit",
                        )
                    if "open-quit" in sent and b"Quit railmux?" in raw_output:
                        send_once(
                            "soft-quit",
                            18.0,
                            b"s",
                            "key|S|Soft quit — keep agents running",
                        )
                    if "soft-quit" in sent and b"Keep this layout?" in raw_output:
                        send_once(
                            "finish-soft-quit",
                            22.0,
                            b"n",
                            "key|N|Skip layout save and finish soft quit",
                        )

                readable, _, _ = select.select([master], [], [], 0.05)
                if readable:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    sanitizer_tail.extend(chunk)
                    sanitized = _sanitize_public_output(
                        bytes(sanitizer_tail),
                        stable_fixture_path,
                        label.encode(),
                    )
                    if len(sanitized) <= sanitizer_hold:
                        continue
                    cut = _utf8_event_boundary(
                        sanitized, len(sanitized) - sanitizer_hold
                    )
                    if cut == 0:
                        continue
                    chunk = sanitized[:cut]
                    sanitizer_tail = bytearray(sanitized[cut:])
                    if ready_at is None:
                        startup_output.extend(chunk)
                        ready = startup_output.upper()
                        if b"PROJECTS" not in ready or b"NEW SESSION" not in ready:
                            continue
                        ready_at = time.monotonic()
                        if profile is DESKTOP:
                            if b"Restoring your workspace" not in startup_output:
                                raise RuntimeError(
                                    "desktop launch did not paint its startup surface"
                                )
                            sent.add("startup-surface")
                            startup = _startup_surface(
                                python, env, profile.width, profile.height
                            )
                            raw_output.extend(startup)
                            events.append(_event(0, "o", startup))
                        # The readiness chunk is a full Railmux repaint in
                        # normal startup. Keep it after the held product-native
                        # startup surface and discard machine-specific chrome.
                        raw_output.extend(chunk)
                        events.append(_event(startup_offset, "o", chunk))
                        continue
                    raw_output.extend(chunk)
                    resized = (
                        profile is DUAL
                        and "split" in sent
                        or profile is CONTROLS
                        and "cycle-layout" in sent
                    )
                    if resized:
                        post_resize_output.extend(chunk)
                    events.append(_event(cast_time(), "o", chunk))
                    if (
                        profile is DUAL
                        and "launch-secondary" in sent
                        and _running_row_follows_empty_state(raw_output)
                    ):
                        sent.add("secondary-running")
                    if (
                        profile is CONTROLS
                        and b"Keeping 1 agent session running." in chunk
                    ):
                        # Teardown can repaint the outer tmux client a few
                        # milliseconds later. End on the complete, product-
                        # native progress surface so viewers can actually read
                        # the successful soft-quit result.
                        controls_exit_frozen = True
                        sanitizer_tail.clear()
                        break
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

        if sanitizer_tail and not controls_exit_frozen:
            chunk = _sanitize_public_output(
                bytes(sanitizer_tail),
                stable_fixture_path,
                label.encode(),
            )
            raw_output.extend(chunk)
            events.append(_event(cast_time(), "o", chunk))
        if (
            profile is DUAL
            and "launch-secondary" in sent
            and _running_row_follows_empty_state(raw_output)
        ):
            # The final repaint can fit entirely in the sanitizer's bounded
            # UTF-8/secret holdback. Validate the completed public stream too,
            # after that tail has been sanitized and committed to the cast.
            sent.add("secondary-running")

        missing_actions = sorted(required_actions - sent)
        if ready_at is None or b"PROJECTS" not in raw_output.upper() or missing_actions:
            tail = raw_output.decode("utf-8", errors="replace")[-800:].strip()
            raise RuntimeError(
                f"Railmux {profile.name} demo missed required milestones"
                + (f" ({', '.join(missing_actions)})" if missing_actions else "")
                + (f": {tail}" if tail else "")
            )
        if (
            profile is CONTROLS
            and b"Keeping 1 agent session running." not in raw_output
        ):
            raise RuntimeError(
                "Railmux controls demo did not reach the soft-quit exit state"
            )
        if (
            profile in (DUAL, CONTROLS)
            and re.search("─{101,}".encode(), post_resize_output)
        ):
            raise RuntimeError(
                f"Railmux {profile.name} demo retained an over-wide rule "
                "after pane resize"
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
        # Extend the final visible state so control-free looping does not snap
        # immediately back to frame zero.
        events.append(_event(total_duration - 0.1, "o", b"\x1b7\x1b8"))
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
        "--dual-output",
        type=Path,
        default=DEFAULT_DUAL_OUTPUT,
        help=f"dual-agent asciicast path (default: {DEFAULT_DUAL_OUTPUT})",
    )
    parser.add_argument(
        "--workflow-output",
        type=Path,
        default=DEFAULT_WORKFLOW_OUTPUT,
        help=f"workflow asciicast path (default: {DEFAULT_WORKFLOW_OUTPUT})",
    )
    parser.add_argument(
        "--tour-output",
        type=Path,
        default=DEFAULT_TOUR_OUTPUT,
        help=f"New Project/Help asciicast path (default: {DEFAULT_TOUR_OUTPUT})",
    )
    parser.add_argument(
        "--mobile-output",
        type=Path,
        default=DEFAULT_MOBILE_OUTPUT,
        help=f"mobile asciicast path (default: {DEFAULT_MOBILE_OUTPUT})",
    )
    parser.add_argument(
        "--controls-output",
        type=Path,
        default=DEFAULT_CONTROLS_OUTPUT,
        help=f"controls asciicast path (default: {DEFAULT_CONTROLS_OUTPUT})",
    )
    args = parser.parse_args(argv)
    try:
        _record(args.desktop_output.resolve(), DESKTOP)
        _record(args.dual_output.resolve(), DUAL)
        _record(args.workflow_output.resolve(), WORKFLOW)
        _record(args.mobile_output.resolve(), MOBILE)
        _record(args.tour_output.resolve(), TOUR)
        _record(args.controls_output.resolve(), CONTROLS)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"record_web_demo.py: error: {exc}", file=sys.stderr)
        return 1
    print(f"recorded desktop Railmux demo: {args.desktop_output.resolve()}")
    print(f"recorded dual-agent Railmux demo: {args.dual_output.resolve()}")
    print(f"recorded workflow Railmux demo: {args.workflow_output.resolve()}")
    print(f"recorded mobile Railmux demo: {args.mobile_output.resolve()}")
    print(f"recorded product tour demo: {args.tour_output.resolve()}")
    print(f"recorded controls demo: {args.controls_output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
