from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from railmux import __version__
from railmux.config import ConfigError, default_config_path, load_config
from railmux.pane_surface import render_startup_surface
from railmux.platform import python_support_error
from railmux.runtime_config import (
    activate_runtime_environment,
    check_executable,
    check_utf8_locale,
)
from railmux import tmux_health
from railmux import tmux_server
from railmux.system_deps import ensure_tmux_available
from railmux.ssh_args import AppendSshArgument, ExtendSshArguments


def _show_startup_message() -> None:
    """Paint immediate feedback before App performs its initial discovery."""
    if not sys.stdout.isatty():
        return
    size = shutil.get_terminal_size((80, 24))
    sys.stdout.write(render_startup_surface(size.columns, size.lines))
    sys.stdout.flush()


_LOCAL_WATCHDOG_INTERVAL = 5.0
_LOCAL_WATCHDOG_FAILURES = 3


def _native_windows() -> bool:
    return os.name == "nt"


def is_ssh_session(environment: dict[str, str] | None = None) -> bool:
    """Lazy compatibility wrapper that keeps POSIX diagnostics import-clean."""
    from railmux.diagnostics import is_ssh_session as detect

    return detect(environment)


def run_doctor(*args: object, **kwargs: object) -> int:
    """Lazy compatibility wrapper for the established CLI test/repair hook."""
    from railmux.diagnostics import run_doctor as doctor

    return doctor(*args, **kwargs)


def _restore_terminal(attributes: list | None) -> None:
    if attributes is None:
        return
    import termios

    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, attributes)
    except (OSError, termios.error):
        pass


def _stop_tmux_client(process: subprocess.Popen) -> None:
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
    attributes = None
    if sys.stdin.isatty():
        import termios

        try:
            attributes = termios.tcgetattr(sys.stdin.fileno())
        except (OSError, termios.error):
            pass
    try:
        process = subprocess.Popen(argv, env=env)
    except OSError as exc:
        print(f"error: could not start tmux client: {exc}", file=sys.stderr)
        return 2
    watchdog = tmux_health.FailureWatchdog.starting(
        time.monotonic(),
        interval=_LOCAL_WATCHDOG_INTERVAL,
        failure_limit=_LOCAL_WATCHDOG_FAILURES,
    )
    next_session_probe = watchdog.next_probe - watchdog.interval
    try:
        while process.poll() is None:
            time.sleep(0.25)
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
            try:
                current_target = tmux_server.discover_target(timeout=1.0)
            except tmux_server.TmuxServerError:
                current_target = None
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
                return 2
        returncode = process.returncode or 0
        if returncode and expected_target is not None:
            try:
                current_target = tmux_server.discover_target(timeout=1.0)
            except tmux_server.TmuxServerError:
                current_target = None
            if current_target != expected_target:
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
        return returncode
    except KeyboardInterrupt:
        _stop_tmux_client(process)
        return 130
    finally:
        _restore_terminal(attributes)


def main(argv: list[str] | None = None) -> int:
    support_error = python_support_error()
    if support_error is not None:
        print(f"error: {support_error}", file=sys.stderr)
        return 2
    raw_args = list(sys.argv[1:] if argv is None else argv)
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
        # Deprecated compatibility for the released v0.2.x CLI. Remove this
        # alias when Railmux 0.4.0 is developed; --remote is authoritative.
        remote_group.add_argument(
            "--ssh",
            dest="remote",
            metavar="HOST",
            help=argparse.SUPPRESS,
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
        doctor_args = doctor_parser.parse_args(raw_args[1:])
        if doctor_args.ssh_arg and not doctor_args.remote:
            doctor_parser.error("--ssh-args requires --remote")
        if doctor_args.remote:
            from railmux.ssh_doctor import run_remote_ssh_doctor

            return run_remote_ssh_doctor(
                doctor_args.remote,
                ssh_args=doctor_args.ssh_arg,
                json_output=doctor_args.json,
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
    if _native_windows():
        # Native Windows owns no local tmux server. The detached daemon keeps
        # ConPTY handles alive while this terminal remains a disposable client.
        activate_runtime_environment(replace(config, tmux_binary="tmux"))
        if not args.inside_tmux:
            from railmux.self_update import maybe_upgrade_before_launch
            from railmux.settings import Settings

            maybe_upgrade_before_launch(raw_args, Settings())
        from railmux.winlocal.client import main as native_main

        return native_main(raw_args, config=config)
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
        cmd = tmux_server.launcher_argv(launch_prefix, raw_args)
        dedicated_session_id = (
            tmux_server.target_session_id(dedicated_target, "railmux")
            if dedicated_target is not None else None
        )
        return _run_tmux_client_with_watchdog(
            cmd,
            tmux_server.exec_environment(),
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
    )
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
