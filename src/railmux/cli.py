from __future__ import annotations

import argparse
import os
import signal
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import replace
from pathlib import Path

from railmux import __version__
from railmux.config import ConfigError, default_config_path, load_config
from railmux.diagnostics import is_ssh_session, run_doctor
from railmux.pane_surface import render_startup_surface
from railmux.runtime_config import (
    activate_runtime_environment,
    check_executable,
    check_utf8_locale,
)
from railmux import tmux_health
from railmux import tmux_server
from railmux import windows_tmux_lifecycle
from railmux.system_deps import ensure_tmux_available
from railmux.ssh_args import AppendSshArgument, ExtendSshArguments
from railmux.terminal_status import command_status


def _show_startup_message(
    detail: str = "Reconnecting sessions and panes…",
) -> None:
    """Paint immediate feedback before App performs its initial discovery."""
    if not sys.stdout.isatty():
        return
    size = shutil.get_terminal_size((80, 24))
    sys.stdout.write(render_startup_surface(
        size.columns, size.lines, detail=detail))
    sys.stdout.flush()


_LOCAL_WATCHDOG_INTERVAL = 5.0
_LOCAL_WATCHDOG_FAILURES = 3


def _interactive_terminal_size() -> tuple[int, int] | None:
    """Return an exact entry TTY size without manufacturing a fallback."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    for stream in (sys.stdout, sys.stdin):
        try:
            size = os.get_terminal_size(stream.fileno())
        except (AttributeError, OSError):
            continue
        if 0 < size.columns <= 65535 and 0 < size.lines <= 65535:
            return size.columns, size.lines
    return None


def _restore_terminal(attributes: list | None) -> None:
    if attributes is None:
        return
    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, attributes)
    except (OSError, termios.error):
        pass


def _reset_terminal_modes(fd: int) -> None:
    """Best-effort recovery when a relay ends before tmux's restore tail."""
    try:
        os.write(
            fd,
            b"\x1b[?1049l\x1b[?1l\x1b>\x1b[?1000l\x1b[?1002l"
            b"\x1b[?1003l\x1b[?1004l\x1b[?1006l\x1b[?2004l"
            b"\x1b[?7h\x1b[?25h\x1b[0m\r",
        )
    except OSError:
        pass


def _stop_tmux_client(process: object) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_tmux_client_with_watchdog(
    argv: list[str],
    env: dict[str, str],
    *,
    expected_target: tmux_server.TmuxServerTarget | None = None,
    expected_session_id: str | None = None,
) -> int:
    """Keep one monitor outside tmux so a frozen server cannot trap the TTY."""
    from railmux.provider_paths import running_in_managed_windows_wrapper

    managed_windows = running_in_managed_windows_wrapper()
    attributes = None
    if sys.stdin.isatty():
        try:
            attributes = termios.tcgetattr(sys.stdin.fileno())
        except (OSError, termios.error):
            pass
    direct_terminal_size = (
        _interactive_terminal_size() if managed_windows else None
    )
    started_at = time.monotonic()
    try:
        popen_kwargs: dict[str, object] = {"env": env}
        if managed_windows and expected_target is not None:
            # A fast direct-client rejection is an implementation detail when
            # the server-origin bridge succeeds. Suppress tmux's raw
            # ``open terminal failed`` line and emit one Railmux-owned status
            # below; bridge failures still receive an actionable error.
            popen_kwargs["stderr"] = subprocess.DEVNULL
        process: object = subprocess.Popen(argv, **popen_kwargs)
    except OSError as exc:
        print(f"error: could not start tmux client: {exc}", file=sys.stderr)
        return 2
    def monitor_client(
        monitor_started_at: float | None = None,
        *,
        asynchronous_probe: bool = False,
    ) -> tuple[int, bool]:
        nonlocal direct_terminal_size, expected_target, expected_session_id
        watchdog = tmux_health.FailureWatchdog.starting(
            (
                time.monotonic()
                if monitor_started_at is None
                else monitor_started_at
            ),
            interval=_LOCAL_WATCHDOG_INTERVAL,
            failure_limit=_LOCAL_WATCHDOG_FAILURES,
        )
        next_session_probe = watchdog.next_probe - watchdog.interval
        probe_thread: threading.Thread | None = None
        probe_result: list[tmux_server.TmuxServerTarget | None] = []

        def discover() -> tmux_server.TmuxServerTarget | None:
            try:
                return tmux_server.discover_target(timeout=1.0)
            except tmux_server.TmuxServerError:
                return None

        while process.poll() is None:
            pump = getattr(process, "pump", None)
            if pump is None:
                time.sleep(0.25)
                if managed_windows:
                    current_size = _interactive_terminal_size()
                    if (
                        current_size is not None
                        and current_size != direct_terminal_size
                    ):
                        direct_terminal_size = current_size
                        pid = getattr(process, "pid", None)
                        if (
                            isinstance(pid, int)
                            and not isinstance(pid, bool)
                            and pid > 0
                        ):
                            try:
                                # MSYS2 occasionally misses the native
                                # console's resize notification. Signal only
                                # this launcher's direct tmux client so it
                                # re-reads both dimensions; tmux remains the
                                # authority for shared-window sizing.
                                os.kill(pid, signal.SIGWINCH)
                            except (OSError, ValueError):
                                pass
            else:
                pump(0.25)
            now = time.monotonic()
            if (
                expected_target is not None
                and expected_session_id is None
                and now >= next_session_probe
            ):
                expected_session_id = tmux_server.target_session_id(
                    expected_target, "railmux", timeout=0.25)
                next_session_probe = now + 1.0
            if not watchdog.due(now):
                continue
            if asynchronous_probe:
                if probe_thread is None:
                    probe_result.clear()

                    def run_probe() -> None:
                        probe_result.append(discover())

                    probe_thread = threading.Thread(
                        target=run_probe,
                        name="railmux-tmux-health",
                        daemon=True,
                    )
                    probe_thread.start()
                    continue
                if probe_thread.is_alive():
                    continue
                current_target = probe_result[0] if probe_result else None
                probe_thread = None
            else:
                current_target = discover()
            if expected_target is None and current_target is not None:
                expected_target = current_target
                expected_session_id = tmux_server.target_session_id(
                    expected_target, "railmux", timeout=0.25)
                next_session_probe = now + 1.0
            healthy = (
                expected_target is not None
                and current_target == expected_target
            )
            if watchdog.observe(healthy, now):
                tmux_health.record_incident(
                    component="launcher",
                    reason="launcher-watchdog-timeout",
                    consecutive_failures=watchdog.consecutive_failures,
                )
                _stop_tmux_client(process)
                _restore_terminal(attributes)
                print(
                    "error: the dedicated Railmux tmux server stopped "
                    "responding; run 'railmux doctor' for diagnostics",
                    file=sys.stderr,
                )
                return 2, True
        return process.returncode or 0, False

    relay = None
    relay_attempted = False
    try:
        returncode, watchdog_failed = monitor_client(started_at)
        if watchdog_failed:
            return returncode
        failed_at = time.monotonic()
        # tmux may leave the client TTY in raw mode even after its process
        # exits. Restore it before a bounded recovery probe or user message.
        _restore_terminal(attributes)
        current_target = None
        if returncode and expected_target is not None:
            try:
                current_target = tmux_server.discover_target(timeout=1.0)
            except tmux_server.TmuxServerError:
                current_target = None
            if current_target == expected_target and expected_session_id is None:
                expected_session_id = tmux_server.target_session_id(
                    expected_target, "railmux", timeout=0.5)

            can_relay = bool(
                current_target == expected_target
                and expected_session_id is not None
                and failed_at - started_at < 5.0
                and attributes is not None
                and sys.stdout.isatty()
                and managed_windows
            )
            if can_relay:
                relay_attempted = True
                from railmux.windows_attach_relay import (
                    WindowsAttachRelayError,
                    start_relay_client,
                )

                try:
                    relay = start_relay_client(
                        target=expected_target,
                        session_id=expected_session_id,
                        environ=env,
                        stdin_fd=sys.stdin.fileno(),
                        stdout_fd=sys.stdout.fileno(),
                    )
                    print(
                        command_status(
                            "info",
                            "connected through the Windows terminal bridge",
                            stream=sys.stderr,
                        ),
                        file=sys.stderr,
                    )
                    tty.setraw(sys.stdin.fileno())
                    process = relay
                    returncode, watchdog_failed = monitor_client(
                        asynchronous_probe=True)
                    if returncode:
                        _reset_terminal_modes(sys.stdout.fileno())
                    if watchdog_failed:
                        return returncode
                except (OSError, termios.error, WindowsAttachRelayError):
                    _reset_terminal_modes(sys.stdout.fileno())
                    _restore_terminal(attributes)
                    outcome = (
                        "could not attach"
                        if relay is None
                        else "connection ended unexpectedly"
                    )
                    print(
                        f"error: the Windows terminal bridge {outcome}; the "
                        "existing workspace was left running; run "
                        "'railmux doctor' for diagnostics",
                        file=sys.stderr,
                    )
                    returncode = 2
                finally:
                    _restore_terminal(attributes)

                if returncode:
                    try:
                        current_target = tmux_server.discover_target(timeout=1.0)
                    except tmux_server.TmuxServerError:
                        current_target = None

            if returncode and current_target != expected_target:
                clean_exit = bool(
                    expected_session_id is not None
                    and tmux_health.consume_clean_exit(
                        server_pid=expected_target.server_pid,
                        session_id=expected_session_id,
                    )
                )
                if not clean_exit:
                    tmux_health.record_incident(
                        component="launcher",
                        reason="launcher-server-exit",
                        consecutive_failures=1,
                    )
            elif (
                returncode
                and current_target == expected_target
                and managed_windows
            ):
                tmux_health.record_incident(
                    component="launcher",
                    reason=(
                        "launcher-relay-failed"
                        if relay_attempted
                        else "launcher-attach-rejected"
                    ),
                    consecutive_failures=1,
                )
                if not relay_attempted:
                    print(
                        "error: direct tmux attach was rejected and the "
                        "Windows terminal bridge was unavailable; the "
                        "existing workspace was left running; run "
                        "'railmux doctor' for diagnostics",
                        file=sys.stderr,
                    )
        # Routine post-exit removal of a proof-authorized last-session socket
        # is housekeeping. Only pre-launch recovery is user-visible.
        windows_tmux_lifecycle.recover_abandoned_socket()
        return returncode
    except KeyboardInterrupt:
        _stop_tmux_client(process)
        if relay is not None:
            _reset_terminal_modes(sys.stdout.fileno())
        return 130
    finally:
        if relay is not None:
            relay.close()
        _restore_terminal(attributes)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "_windows-attach-relay":
        from railmux.windows_attach_relay import relay_server_main

        return relay_server_main(raw_args[1:])
    if raw_args and raw_args[0] == "config":
        from railmux.config_cli import main as config_main

        return config_main(raw_args[1:])
    if raw_args and raw_args[0] == "ssh":
        from railmux.fast_display_client import main as ssh_main

        # Help is configuration- and side-effect-free even when the user's
        # config, locale, tmux, or update state currently needs repair.
        if any(value in {"-h", "--help"} for value in raw_args[1:]):
            return ssh_main(raw_args[1:])
        try:
            config = load_config()
        except ConfigError as exc:
            print(
                f"error: Railmux configuration is invalid: {exc}; "
                "run 'railmux config' to repair or reset it",
                file=sys.stderr,
            )
            return 2
        locale_valid, locale_detail = check_utf8_locale(config.locale)
        if not locale_valid:
            print(
                f"error: configured locale is unusable: {locale_detail}; run "
                "'railmux config' to correct or reset it",
                file=sys.stderr,
            )
            return 2
        # The local latest-state client does not use local tmux. Only local
        # locale applies here; the remote helper reads its own tmux setting.
        activate_runtime_environment(replace(config, tmux_binary="tmux"))
        # The SSH client is a user-facing launcher too. Check the local
        # installation before connecting so an accepted upgrade can restart
        # the exact ``railmux ssh ...`` command on the new version.
        from railmux.self_update import maybe_upgrade_before_launch
        from railmux.settings import Settings
        maybe_upgrade_before_launch(raw_args, Settings())

        return ssh_main(raw_args[1:])
    if raw_args and raw_args[0] == "remote-server":
        from railmux.fast_display_server import main as remote_server_main
        return remote_server_main(raw_args[1:])
    if raw_args and raw_args[0] == "doctor":
        doctor_parser = argparse.ArgumentParser(
            prog="railmux doctor",
            description=(
                "Print privacy-safe local diagnostics, or use --remote for a "
                "read-only SSH compatibility preflight"
            ),
        )
        doctor_parser.add_argument(
            "--claude-home",
            default=str(Path.home() / ".claude"),
            help=argparse.SUPPRESS,
        )
        doctor_parser.add_argument(
            "--json",
            action="store_true",
            help="print the versioned privacy-safe diagnostic snapshot as JSON",
        )
        remote_group = doctor_parser.add_mutually_exclusive_group()
        remote_group.add_argument(
            "--remote",
            dest="remote",
            metavar="HOST",
            help=(
                "run a read-only remote SSH compatibility preflight; "
                "the host is omitted from output"
            ),
        )
        doctor_parser.add_argument(
            "--ssh-arg",
            action=AppendSshArgument,
            dest="ssh_arg",
            default=[],
            metavar="VALUE",
            help=argparse.SUPPRESS,
        )
        doctor_parser.add_argument(
            "--ssh-args",
            action=ExtendSshArguments,
            dest="ssh_arg",
            metavar="ARGS",
            help="a quoted group of ssh arguments for --remote",
        )
        doctor_parser.add_argument(
            "--remote-platform",
            choices=("auto", "posix", "windows"),
            default="auto",
            help=(
                "remote shell family for --remote: auto, posix, or windows "
                "(default: auto)"
            ),
        )
        doctor_args = doctor_parser.parse_args(raw_args[1:])
        if doctor_args.ssh_arg and not doctor_args.remote:
            doctor_parser.error("--ssh-args requires --remote")
        if doctor_args.remote_platform != "auto" and not doctor_args.remote:
            doctor_parser.error("--remote-platform requires --remote")
        if doctor_args.remote:
            from railmux.ssh_doctor import run_remote_ssh_doctor

            return run_remote_ssh_doctor(
                doctor_args.remote,
                ssh_args=doctor_args.ssh_arg,
                json_output=doctor_args.json,
                remote_platform=doctor_args.remote_platform,
            )
        return run_doctor(
            claude_home=Path(doctor_args.claude_home),
            json_output=doctor_args.json,
        )

    parser = argparse.ArgumentParser(
        prog="railmux",
        usage=(
            "railmux [OPTIONS]\n"
            "       railmux ssh HOST [OPTIONS]\n"
            "       railmux config [--remote HOST] [OPTIONS]\n"
            "       railmux doctor [--remote HOST] [OPTIONS]"
        ),
        description="Terminal workspace for Claude Code and Codex sessions",
        epilog=(
            "Commands:\n"
            "  railmux ssh HOST       responsive remote workspace\n"
            "  railmux config         edit local settings\n"
            "  railmux config --remote HOST\n"
            "                         edit settings on an SSH destination\n"
            "  railmux doctor         inspect the local installation\n"
            "  railmux doctor --remote HOST\n"
            "                         inspect remote compatibility\n\n"
            "Run 'railmux COMMAND --help' for command-specific options."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"railmux {__version__}")
    parser.add_argument(
        "--project",
        metavar="PATH",
        help="open with this project path selected",
    )
    parser.add_argument(
        "--claude-home",
        default=str(Path.home() / ".claude"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--inside-tmux",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    scroll_group = parser.add_mutually_exclusive_group()
    scroll_group.add_argument(
        "--scroll-coalescing",
        dest="scroll_coalescing",
        action="store_true",
        default=None,
        help="Force-enable tmux copy-mode wheel event coalescing",
    )
    scroll_group.add_argument(
        "--no-scroll-coalescing",
        dest="scroll_coalescing",
        action="store_false",
        help="Force-disable tmux copy-mode wheel event coalescing",
    )
    args = parser.parse_args(raw_args)

    try:
        config = load_config()
    except ConfigError as exc:
        path = default_config_path()
        try:
            display_path = f"~/{path.relative_to(Path.home()).as_posix()}"
        except ValueError:
            display_path = "the Railmux configuration file"
        print(
            f"error: {display_path}: {exc}; run 'railmux config' to repair "
            "or reset it",
            file=sys.stderr,
        )
        return 2
    locale_valid, locale_detail = check_utf8_locale(config.locale)
    if not locale_valid:
        print(
            f"error: configured locale is unusable: {locale_detail}; run "
            "'railmux config' to correct or reset it",
            file=sys.stderr,
        )
        return 2
    if config.tmux_binary != "tmux":
        tmux_check = check_executable("tmux", config.tmux_binary)
        if not tmux_check.valid:
            print(
                f"error: configured tmux is unusable: {tmux_check.error}; run "
                "'railmux config' to correct or reset it",
                file=sys.stderr,
            )
            return 2
    activate_runtime_environment(config)

    # tmux is required even when TMUX is already set: an inherited TMUX value
    # with no tmux binary on PATH otherwise enters a TUI whose controls cannot
    # work. Keep this preflight ahead of every TUI startup path.
    tmux_available = (
        ensure_tmux_available(configured=True)
        if config.tmux_binary != "tmux"
        else ensure_tmux_available()
    )
    if not tmux_available:
        return 2

    try:
        # Validate before any command can address a tmux server. ``tmux -V``
        # above is server-independent.
        tmux_server.socket_label()
        if windows_tmux_lifecycle.recover_abandoned_socket():
            print(
                "info: recovered an unresponsive Windows tmux socket left by "
                "a previous session; Codex and Claude session files were not "
                "changed",
                file=sys.stderr,
            )
        dedicated_target = tmux_server.discover_target()
        on_dedicated_server = tmux_server.is_current_server(dedicated_target)
    except tmux_server.TmuxServerError as exc:
        if isinstance(exc, tmux_server.TmuxServerUnresponsive):
            tmux_health.record_incident(
                component="launcher",
                reason="startup-probe-timeout",
                consecutive_failures=1,
            )
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if (
        dedicated_target is not None
        and not tmux_server.sync_server_environment(dedicated_target)
    ):
        print(
            "warning: the existing Railmux tmux server did not accept the "
            "configured runtime environment; run 'railmux doctor' or "
            "'railmux config' before starting new agents",
            file=sys.stderr,
        )

    if args.inside_tmux and not on_dedicated_server:
        print(
            "error: --inside-tmux is reserved for Railmux's dedicated tmux "
            "server",
            file=sys.stderr,
        )
        return 2

    if not args.inside_tmux and not on_dedicated_server:
        # Check exactly once in the user-facing outer launcher. The
        # ``--inside-tmux`` child must never repeat the network check or prompt.
        from railmux.self_update import maybe_upgrade_before_launch
        from railmux.settings import Settings
        maybe_upgrade_before_launch(raw_args, Settings())

        # A swap transaction survives a killed Railmux Python process in tmux
        # metadata. Repair it before ``new-session -A``: otherwise that command
        # may attach to a stranded display window and never start App. Route the
        # legacy bare helpers only to the already-proven dedicated server.
        if dedicated_target is not None:
            from railmux.display_transport import recover_interrupted_swaps
            with tmux_server.scoped_target_environment(dedicated_target):
                recovery = recover_interrupted_swaps()
            if recovery.unresolved:
                print(
                    "warning: an interrupted agent display could not be "
                    "repaired; the marked pane was left untouched",
                    file=sys.stderr,
                )

        launch_prefix = (
            [sys.executable, "-m", "railmux"]
            if Path(sys.argv[0]).name == "__main__.py"
            else [sys.argv[0]]
        )
        client_env = tmux_server.exec_environment()
        from railmux.provider_paths import (
            running_in_managed_windows_wrapper,
        )
        managed_windows = running_in_managed_windows_wrapper()
        dedicated_session_id = (
            tmux_server.target_session_id(dedicated_target, "railmux")
            if dedicated_target is not None else None
        )
        created_detached_session = False
        if dedicated_target is not None and dedicated_session_id is None:
            if managed_windows:
                dedicated_session_id = (
                    tmux_server.ensure_detached_launcher_session(
                        dedicated_target,
                        launch_prefix,
                        raw_args,
                        env=client_env,
                        initial_size=_interactive_terminal_size(),
                    )
                )
                created_detached_session = dedicated_session_id is not None
        if (
            dedicated_target is not None
            and dedicated_session_id is not None
            and not created_detached_session
        ):
            if managed_windows:
                from railmux.windows_ui_transition import ensure_current_ui

                runtime_id = os.environ.get("RAILMUX_MSYS2_RUNTIME_ID", "")
                app_id = os.environ.get("RAILMUX_MSYS2_APP_ID", "")
                transition = ensure_current_ui(
                    dedicated_target,
                    dedicated_session_id,
                    runtime=runtime_id,
                    target_app=app_id,
                    target_version=__version__,
                )
                if transition.status in {"legacy", "pending", "blocked"}:
                    detail = transition.detail or "the running UI was unchanged"
                    print(
                        "info: the Windows app-layer update is pending; "
                        f"{detail}. Soft Quit the existing UI, then run "
                        "'railmux' again to finish it.",
                        file=sys.stderr,
                    )
        # Windows Terminal 1.23+ implements DEC synchronized output, but TERM
        # remains the generic xterm-256color and tmux cannot infer that fact.
        # Without ``sync``, Codex's active-turn redraw exposes intermediate
        # hardware-cursor coordinates. Unknown private modes are ignored by
        # older Windows Terminal builds, while non-WT/conhost entry remains
        # unchanged.
        client_features = (
            ("sync",)
            if managed_windows and client_env.get("WT_SESSION")
            else ()
        )
        cmd = tmux_server.launcher_argv(
            launch_prefix,
            raw_args,
            client_features=client_features,
        )
        return _run_tmux_client_with_watchdog(
            cmd,
            client_env,
            expected_target=dedicated_target,
            expected_session_id=dedicated_session_id,
        )

    # Inside tmux now. App construction performs bounded initial provider/tmux
    # discovery before
    # Urwid can paint its first frame. A tiny terminal-native surface prevents
    # that interval from looking like a hung empty tmux pane.
    _show_startup_message()
    # Lazy import so non-TUI invocations (--version etc) don't pull urwid.
    from railmux.ui.app import App
    from railmux.provider_paths import running_in_windows_wrapper
    app = App(
        claude_home=Path(args.claude_home),
        config=config,
        # A direct invocation from an existing dedicated pane is intentionally
        # non-owning; quitting it must not kill the surrounding workspace.
        auto_launched=args.inside_tmux,
        scroll_coalescing=(
            is_ssh_session() if args.scroll_coalescing is None
            else args.scroll_coalescing
        ),
        startup_progress=(
            _show_startup_message
            if running_in_windows_wrapper() else None
        ),
    )
    app.run()
    request = getattr(app, "_ui_upgrade_request", None)
    if request is not None:
        from railmux.windows_ui_transition import UpgradeRequest, upgrade_exec_argv

        # Mocked or partially constructed App objects must not manufacture an
        # upgrade merely by dynamically answering arbitrary attributes.
        if not isinstance(request, UpgradeRequest):
            return 0

        upgrade_argv = upgrade_exec_argv(request, [sys.argv[0], *raw_args])
        if upgrade_argv is None:
            print(
                "error: the requested Windows app layer no longer validates; "
                "run 'railmux runtime status --verify'",
                file=sys.stderr,
            )
            return 2
        upgrade_env = os.environ.copy()
        upgrade_env["RAILMUX_MSYS2_RUNTIME_ID"] = request.runtime
        upgrade_env["RAILMUX_MSYS2_APP_ID"] = request.app
        os.execve(upgrade_argv[0], upgrade_argv, upgrade_env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
