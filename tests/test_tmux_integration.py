"""Opt-in smoke coverage against a real, isolated tmux server."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from railmux import tmux_ctl


pytestmark = pytest.mark.skipif(
    os.environ.get("RAILMUX_RUN_TMUX_INTEGRATION") != "1",
    reason="set RAILMUX_RUN_TMUX_INTEGRATION=1 to run real tmux smoke tests",
)


@pytest.fixture
def isolated_tmux(monkeypatch):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")

    # Unix-domain socket paths are commonly capped at 104–108 bytes. pytest's
    # nested tmp_path can exceed that on macOS/Linux, so use a short private
    # directory directly under the platform temp root.
    socket_root = Path(tempfile.mkdtemp(prefix="rx-tmux-"))
    socket_root.chmod(0o700)
    socket_label = f"railmux-smoke-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    session_name = "railmux-smoke-display"
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_root))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)

    socket_path: str | None = None
    try:
        subprocess.run(
            [
                "tmux", "-L", socket_label, "-f", "/dev/null",
                "new-session", "-d", "-s", session_name, "sleep 60",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        socket_path = subprocess.check_output(
            [
                "tmux", "-L", socket_label, "display-message", "-p",
                "-t", session_name, "#{socket_path}",
            ],
            text=True,
        ).strip()
        server_pid = subprocess.check_output(
            [
                "tmux", "-L", socket_label, "display-message", "-p",
                "-t", session_name, "#{pid}",
            ],
            text=True,
        ).strip()
        pane_id = subprocess.check_output(
            [
                "tmux", "-L", socket_label, "display-message", "-p",
                "-t", session_name, "#{pane_id}",
            ],
            text=True,
        ).strip()

        # Bare tmux commands in tmux_ctl now resolve only to this private socket.
        monkeypatch.setenv("TMUX", f"{socket_path},{server_pid},0")
        monkeypatch.setenv("TMUX_PANE", pane_id)
        tmux_ctl.tmux_version.cache_clear()
        yield session_name, pane_id, socket_path
    finally:
        kill_command = (
            ["tmux", "-S", socket_path, "kill-server"]
            if socket_path
            else ["tmux", "-L", socket_label, "kill-server"]
        )
        subprocess.run(
            kill_command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        tmux_ctl.tmux_version.cache_clear()
        shutil.rmtree(socket_root, ignore_errors=True)


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_real_tmux_session_split_attach_persistence_and_styles(isolated_tmux):
    display_session, primary_pane, socket_path = isolated_tmux
    agent_session = "railmux-smoke-agent"

    assert tmux_ctl.session_exists(display_session)
    assert tmux_ctl.pane_size(primary_pane) is not None
    assert tmux_ctl.window_size(primary_pane) is not None

    original_border = subprocess.check_output(
        [
            "tmux", "show-window-options", "-v", "-t", display_session,
            "pane-border-style",
        ],
        text=True,
    ).strip()
    assert tmux_ctl.set_window_border_styles("fg=#5faf00", "fg=#5faf00")
    assert subprocess.check_output(
        [
            "tmux", "show-window-options", "-v", "-t", display_session,
            "pane-border-style",
        ],
        text=True,
    ).strip() == "fg=#5faf00"

    created, reason = tmux_ctl.new_detached_session(
        agent_session, "sleep 60")
    assert created, reason
    assert tmux_ctl.session_exists(agent_session)

    attach_command = (
        f"TMUX= exec tmux -S {shlex.quote(socket_path)} "
        f"attach-session -t {agent_session}"
    )
    display_pane = tmux_ctl.split_window_h(
        attach_command,
        target=primary_pane,
        size_percent=60,
        detached=True,
    )
    assert display_pane is not None
    assert tmux_ctl.pane_alive(display_pane)
    assert _wait_until(
        lambda: tmux_ctl.session_attached_count(agent_session) == 1)

    # Removing only the display pane must detach, never kill, the background
    # agent session that owns the actual process.
    assert tmux_ctl.kill_pane(display_pane)
    assert _wait_until(
        lambda: tmux_ctl.session_attached_count(agent_session) == 0)
    assert tmux_ctl.session_exists(agent_session)

    assert tmux_ctl.set_window_border_styles(original_border, original_border)
    assert subprocess.check_output(
        [
            "tmux", "show-window-options", "-v", "-t", display_session,
            "pane-border-style",
        ],
        text=True,
    ).strip() == original_border
    assert tmux_ctl.kill_session(agent_session)
