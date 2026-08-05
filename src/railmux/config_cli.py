"""Cooked-mode editor for Railmux's single TOML configuration authority."""
from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator, TextIO

from railmux.config import ConfigError, default_config_path, load_config
from railmux.runtime_config import (
    check_executable,
    check_utf8_locale,
    runtime_environment,
)
from railmux.settings import MANAGED_CONFIG_KEYS, Settings
from railmux.ssh_args import AppendSshArgument, ExtendSshArguments
from railmux.terminal_status import (
    STYLE_ACCENT,
    STYLE_ERROR,
    STYLE_MUTED,
    STYLE_PROMPT,
    STYLE_SUCCESS,
    STYLE_WARNING,
    stream_is_tty,
    styled,
)


_BACK = object()
_EXIT = object()
_PAGE_CLEAR = "\033[0m\033[2J\033[H"
_CATEGORY_TITLES = ("Behavior / Options", "Program paths", "Environment")


@contextmanager
def _editor_screen(stdin: TextIO, stdout: TextIO) -> Iterator[None]:
    """Keep an interactive editor transcript out of primary scrollback."""
    active = stream_is_tty(stdin) and stream_is_tty(stdout)
    if active:
        stdout.write("\033[?1049h\033[2J\033[H")
        stdout.flush()
    try:
        yield
    finally:
        if active:
            stdout.write("\033[0m\033[?25h\033[?1049l")
            stdout.flush()


@dataclass(frozen=True)
class _PolicySetting:
    title: str
    section: str
    key: str
    config_attr: str
    choices: tuple[tuple[str, str], ...]
    setter: str


@dataclass
class _Feedback:
    message: str | None = None
    level: str = "info"

    def set(self, message: str, *, level: str = "info") -> None:
        self.message = message
        self.level = level

    def take(self) -> tuple[str, str] | None:
        if self.message is None:
            return None
        notice = (self.message, self.level)
        self.message = None
        self.level = "info"
        return notice


_BEHAVIOR_SETTINGS = (
    _PolicySetting(
        "Layout retention", "ui", "layout_retention", "layout_save_policy",
        (("always", "Always keep custom proportions"),
         ("ask", "Ask every time"), ("never", "Never keep them")),
        "set_layout_save_policy",
    ),
    _PolicySetting(
        "Codex auto-run", "codex", "auto_run", "codex_yolo_policy",
        (("always", "Always enable"), ("ask", "Ask every Railmux run"),
         ("never", "Never enable")),
        "set_codex_yolo_policy",
    ),
    _PolicySetting(
        "Railmux updates", "updates", "auto_update", "update_policy",
        (("always", "Always install updates"), ("ask", "Ask every time"),
         ("never", "Never check")),
        "set_update_policy",
    ),
    _PolicySetting(
        "Claude history in railmux ssh", "ssh", "claude_history",
        "claude_history_policy",
        (("local", "Always use smooth local history"),
         ("ask", "Ask on first upward scroll"),
         ("native", "Always use Claude native history")),
        "set_claude_history_policy",
    ),
    _PolicySetting(
        "Clicked paths in railmux ssh", "ssh", "path_open",
        "path_open_policy",
        (("internal", "Always open inside with managed Vim"),
         ("ask", "Ask every time"),
         ("external", "Always use a separate terminal")),
        "set_path_open_policy",
    ),
)

_BEHAVIOR_KEYS = {
    "ui": ("layout_retention", "layout_profile"),
    "codex": ("auto_run",),
    "updates": ("auto_update",),
    "ssh": ("history_lines", "claude_history", "path_open"),
}
_PROGRAM_KEYS = {
    "tmux": ("binary",),
    "claude": ("binary",),
    "codex": ("binary",),
}
_ENVIRONMENT_KEYS = {"environment": ("locale",)}


def _write(stream: TextIO, text: str = "") -> None:
    print(text, file=stream, flush=True)


def _clear_page(stdout: TextIO) -> None:
    if stream_is_tty(stdout):
        stdout.write(_PAGE_CLEAR)
        stdout.flush()
    else:
        _write(stdout)


def _page_width(stdout: TextIO) -> int:
    try:
        width = os.get_terminal_size(stdout.fileno()).columns
    except (AttributeError, OSError, ValueError):
        width = 72
    return max(24, min(72, width or 72))


def _root_title(remote_context: bool) -> str:
    return (
        "Remote Railmux configuration"
        if remote_context
        else "Railmux configuration"
    )


def _render_root_navigation(
    stdout: TextIO,
    *,
    remote_context: bool,
    selected: int | None = None,
    actions: bool = True,
) -> None:
    _write(stdout, styled(_root_title(remote_context), STYLE_ACCENT, stream=stdout))
    _write(
        stdout,
        styled(f"  {default_config_path()}", STYLE_MUTED, stream=stdout),
    )
    _write(stdout)
    for index, title in enumerate(_CATEGORY_TITLES, 1):
        marker = ">" if selected == index else " "
        _write(stdout, f" {marker} {index}. {title}")
    if actions:
        reset_label = (
            "Reset all remote-workspace settings"
            if remote_context
            else "Reset all Railmux-managed settings"
        )
        _write(stdout, f"   r. {reset_label}")
        _write(stdout, "   q. Exit")


def _render_category_header(
    stdout: TextIO,
    *,
    remote_context: bool,
    category_index: int,
) -> None:
    _clear_page(stdout)
    _render_root_navigation(
        stdout,
        remote_context=remote_context,
        selected=category_index,
        actions=False,
    )
    _write(stdout)
    _write(
        stdout,
        styled("-" * _page_width(stdout), STYLE_MUTED, stream=stdout),
    )
    _write(stdout)
    _write(
        stdout,
        styled(_CATEGORY_TITLES[category_index - 1], STYLE_ACCENT, stream=stdout),
    )


def _render_editor_header(
    stdout: TextIO,
    *,
    remote_context: bool,
    category: str,
    setting: str,
) -> None:
    _clear_page(stdout)
    breadcrumb = f"{_root_title(remote_context)} > {category} > {setting}"
    _write(stdout, styled(breadcrumb, STYLE_ACCENT, stream=stdout))
    _write(
        stdout,
        styled(f"  {default_config_path()}", STYLE_MUTED, stream=stdout),
    )
    _write(stdout)


def _render_feedback(stdout: TextIO, feedback: _Feedback) -> None:
    notice = feedback.take()
    if notice is None:
        return
    message, level = notice
    style = {
        "success": STYLE_SUCCESS,
        "warning": STYLE_WARNING,
        "error": STYLE_ERROR,
    }.get(level, STYLE_MUTED)
    _write(stdout)
    _write(stdout, styled(message, style, stream=stdout))


def _read(stdin: TextIO, stdout: TextIO, prompt: str) -> str:
    print(
        styled(prompt, STYLE_PROMPT, stream=stdout),
        end="", file=stdout, flush=True,
    )
    value = stdin.readline()
    return "q" if value == "" else value.strip()


def _confirm(stdin: TextIO, stdout: TextIO, prompt: str) -> bool:
    return _read(stdin, stdout, f"{prompt} [y/N] ").lower() in {"y", "yes"}


def _saved(
    ok: bool,
    feedback: _Feedback,
    *,
    success: str = "Saved.",
) -> None:
    if ok:
        feedback.set(success, level="success")
    else:
        feedback.set("Could not update config.toml; unchanged.", level="error")


def _reset_one(
    settings: Settings,
    section: str,
    key: str,
    feedback: _Feedback,
) -> None:
    _saved(settings.reset_keys({section: (key,)}), feedback)


def _edit_policy(
    item: _PolicySetting,
    stdin: TextIO,
    stdout: TextIO,
    feedback: _Feedback,
    *,
    remote_context: bool,
) -> object | None:
    settings = Settings()
    current = getattr(settings, item.config_attr)
    while True:
        _render_editor_header(
            stdout,
            remote_context=remote_context,
            category="Behavior / Options",
            setting=item.title,
        )
        _write(stdout, f"Current: {current}")
        _write(stdout)
        for index, (value, label) in enumerate(item.choices, 1):
            marker = "*" if value == current else " "
            _write(stdout, f"  {index}. [{marker}] {label}")
        _write(stdout, "  r. Reset to default")
        _write(stdout, "  b. Back")
        _write(stdout, "  q. Exit")
        _render_feedback(stdout, feedback)
        choice = _read(stdin, stdout, "Choose: ").lower()
        if choice == "b":
            return _BACK
        if choice == "q":
            return _EXIT
        if choice == "r":
            reset_keys = (
                ("layout_retention", "layout_profile")
                if item.section == "ui" and item.key == "layout_retention"
                else (item.key,)
            )
            _saved(settings.reset_keys({item.section: reset_keys}), feedback)
            return None
        if not choice.isdigit() or not 1 <= int(choice) <= len(item.choices):
            feedback.set("Choose one of the listed entries.", level="warning")
            continue
        selected = item.choices[int(choice) - 1][0]
        setter = getattr(settings, item.setter)
        _saved(setter(selected), feedback)
        return None


def _edit_history_lines(
    stdin: TextIO,
    stdout: TextIO,
    feedback: _Feedback,
    *,
    remote_context: bool,
) -> object | None:
    config = load_config()
    _render_editor_header(
        stdout,
        remote_context=remote_context,
        category="Behavior / Options",
        setting="railmux ssh history lines",
    )
    _write(stdout, f"Current: {config.ssh_history_lines}")
    _write(stdout)
    _write(stdout, "Enter 2000-20000, r to reset, b to go back, or q to exit.")
    _render_feedback(stdout, feedback)
    value = _read(stdin, stdout, "Value: ").lower()
    if value == "b":
        return _BACK
    if value == "q":
        return _EXIT
    settings = Settings()
    if value == "r":
        _reset_one(settings, "ssh", "history_lines", feedback)
        return None
    try:
        parsed = int(value)
    except ValueError:
        feedback.set(
            "History lines must be an integer between 2000 and 20000.",
            level="error",
        )
        return None
    _saved(settings.set_ssh_history_lines(parsed), feedback)
    return None


def _behavior_menu(
    stdin: TextIO,
    stdout: TextIO,
    feedback: _Feedback,
    *,
    remote_context: bool = False,
) -> object | None:
    while True:
        config = load_config()
        settings = Settings()
        current = [
            settings.layout_save_policy,
            settings.codex_yolo_policy,
            settings.update_policy,
            settings.claude_history_policy,
            settings.path_open_policy,
        ]
        labels = [item.title for item in _BEHAVIOR_SETTINGS]
        if not remote_context:
            current.append(str(config.ssh_history_lines))
            labels.append("railmux ssh history lines")
        _render_category_header(
            stdout,
            remote_context=remote_context,
            category_index=1,
        )
        for index, (label, value) in enumerate(zip(
            labels,
            current,
        ), 1):
            _write(stdout, f"  {index}. {label} [{value}]")
        _write(stdout, "  r. Reset all behavior options")
        _write(stdout, "  b. Back")
        _write(stdout, "  q. Exit")
        _render_feedback(stdout, feedback)
        choice = _read(stdin, stdout, "Choose: ").lower()
        if choice == "b":
            return _BACK
        if choice == "q":
            return _EXIT
        if choice == "r":
            if _confirm(stdin, stdout, "Reset every behavior option?"):
                behavior_keys = dict(_BEHAVIOR_KEYS)
                if remote_context:
                    behavior_keys["ssh"] = ("claude_history", "path_open")
                _saved(settings.reset_keys(behavior_keys), feedback)
            continue
        if not choice.isdigit() or not 1 <= int(choice) <= len(current):
            feedback.set("Choose one of the listed entries.", level="warning")
            continue
        index = int(choice) - 1
        result = (
            _edit_policy(
                _BEHAVIOR_SETTINGS[index],
                stdin,
                stdout,
                feedback,
                remote_context=remote_context,
            )
            if index < len(_BEHAVIOR_SETTINGS)
            else _edit_history_lines(
                stdin,
                stdout,
                feedback,
                remote_context=remote_context,
            )
        )
        if result is _EXIT:
            return _EXIT


def _edit_program(
    section: str,
    title: str,
    stdin: TextIO,
    stdout: TextIO,
    feedback: _Feedback,
    *,
    remote_context: bool,
) -> object | None:
    config = load_config()
    current = getattr(config, f"{section}_binary")
    _render_editor_header(
        stdout,
        remote_context=remote_context,
        category="Program paths",
        setting=f"{title} executable",
    )
    _write(stdout, f"Current: {current}")
    _write(stdout)
    _write(stdout, "Enter a command name or executable path.")
    _write(stdout, "Use r to restore PATH lookup, b to go back, or q to exit.")
    _render_feedback(stdout, feedback)
    value = _read(stdin, stdout, "Executable: ")
    lowered = value.lower()
    if lowered == "b":
        return _BACK
    if lowered == "q":
        return _EXIT
    settings = Settings()
    if lowered == "r":
        _reset_one(settings, section, "binary", feedback)
        return None
    if not value:
        feedback.set("No change made.")
        return None
    candidate_config = (
        replace(config, tmux_binary=value)
        if section == "tmux"
        else config
    )
    environment = runtime_environment(candidate_config)
    check = check_executable(section, value, environ=environment)
    if not check.valid:
        feedback.set(
            f"Not saved: {check.error}. Correct the path or press r to use PATH.",
            level="error",
        )
        return None
    assert check.resolved is not None
    details = [f"Validated {check.resolved}."]
    if check.version:
        details.append(check.version)
    if section == "tmux":
        details.append(
            "The selected tmux applies to the next Railmux invocation; "
            "existing sessions were not changed."
        )
    _saved(
        settings.set_program_binary(section, check.value),
        feedback,
        success=" ".join((*details, "Saved.")),
    )
    return None


def _program_menu(
    stdin: TextIO,
    stdout: TextIO,
    feedback: _Feedback,
    *,
    remote_context: bool,
) -> object | None:
    programs = (("tmux", "tmux"), ("claude", "Claude Code"), ("codex", "Codex"))
    while True:
        config = load_config()
        _render_category_header(
            stdout,
            remote_context=remote_context,
            category_index=2,
        )
        for index, (section, title) in enumerate(programs, 1):
            _write(stdout, f"  {index}. {title} [{getattr(config, f'{section}_binary')}]")
        _write(stdout, "  r. Reset all program paths")
        _write(stdout, "  b. Back")
        _write(stdout, "  q. Exit")
        _render_feedback(stdout, feedback)
        choice = _read(stdin, stdout, "Choose: ").lower()
        if choice == "b":
            return _BACK
        if choice == "q":
            return _EXIT
        if choice == "r":
            if _confirm(stdin, stdout, "Reset every program path to PATH lookup?"):
                _saved(Settings().reset_keys(_PROGRAM_KEYS), feedback)
            continue
        if not choice.isdigit() or not 1 <= int(choice) <= len(programs):
            feedback.set("Choose one of the listed entries.", level="warning")
            continue
        section, title = programs[int(choice) - 1]
        result = _edit_program(
            section,
            title,
            stdin,
            stdout,
            feedback,
            remote_context=remote_context,
        )
        if result is _EXIT:
            return _EXIT


def _edit_locale(
    stdin: TextIO,
    stdout: TextIO,
    feedback: _Feedback,
    *,
    remote_context: bool,
) -> object | None:
    config = load_config()
    _render_editor_header(
        stdout,
        remote_context=remote_context,
        category="Environment",
        setting="UTF-8 locale",
    )
    _write(stdout, f"Current: {config.locale}")
    _write(stdout)
    _write(stdout, "Enter an installed UTF-8 locale such as C.UTF-8 or en_US.UTF-8.")
    _write(stdout, "Use r to inherit the shell environment, b to go back, or q to exit.")
    _render_feedback(stdout, feedback)
    value = _read(stdin, stdout, "Locale: ")
    lowered = value.lower()
    if lowered == "q":
        return _EXIT
    if lowered == "b":
        return _BACK
    if lowered == "r":
        _reset_one(Settings(), "environment", "locale", feedback)
        return None
    valid, detail = check_utf8_locale(value)
    if not valid:
        feedback.set(
            f"Not saved: {detail}. Run 'locale -a' to list installed locales.",
            level="error",
        )
        return None
    _saved(
        Settings().set_locale(value),
        feedback,
        success=(
            f"Validated {detail}. Saved. The locale applies to new "
            "Railmux-managed processes; running agents were not restarted."
        ),
    )
    return None


def _environment_menu(
    stdin: TextIO,
    stdout: TextIO,
    feedback: _Feedback,
    *,
    remote_context: bool,
) -> object | None:
    while True:
        config = load_config()
        _render_category_header(
            stdout,
            remote_context=remote_context,
            category_index=3,
        )
        _write(stdout, f"  1. UTF-8 locale [{config.locale}]")
        _write(stdout, "  r. Reset environment settings")
        _write(stdout, "  b. Back")
        _write(stdout, "  q. Exit")
        _render_feedback(stdout, feedback)
        choice = _read(stdin, stdout, "Choose: ").lower()
        if choice == "b":
            return _BACK
        if choice == "q":
            return _EXIT
        if choice == "r":
            if _confirm(stdin, stdout, "Reset environment settings?"):
                _saved(Settings().reset_keys(_ENVIRONMENT_KEYS), feedback)
            continue
        if choice != "1":
            feedback.set("Choose one of the listed entries.", level="warning")
            continue
        result = _edit_locale(
            stdin,
            stdout,
            feedback,
            remote_context=remote_context,
        )
        if result is _EXIT:
            return _EXIT


def _backup_invalid_config(path: Path) -> Path:
    candidate = path.with_name(f"{path.name}.invalid.bak")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.invalid-{suffix}.bak")
        suffix += 1
    path.replace(candidate)
    try:
        candidate.chmod(0o600)
    except OSError:
        # The backup is not complete until it has private permissions. Restore
        # the original name when possible so a failed repair never silently
        # removes the user's only configuration authority.
        try:
            candidate.replace(path)
        except OSError:
            pass
        raise
    return candidate


def _recover_invalid_config(
    error: ConfigError,
    stdin: TextIO,
    stdout: TextIO,
    feedback: _Feedback,
) -> bool:
    path = default_config_path()
    _clear_page(stdout)
    _write(
        stdout,
        styled("Railmux configuration is invalid", STYLE_ERROR, stream=stdout),
    )
    _write(stdout, str(error))
    _write(stdout, "Run this command after correcting the TOML, or reset it now.")
    if not _confirm(stdin, stdout, "Back up the invalid file and reset Railmux settings?"):
        return False
    try:
        backup = _backup_invalid_config(path)
    except OSError as exc:
        _write(stdout, f"Could not back up the invalid file: {exc}")
        return False
    feedback.set(
        f"Backed up the invalid file as {backup.name}.",
        level="success",
    )
    return True


def _managed_reset_keys(*, remote_context: bool) -> dict[str, tuple[str, ...]]:
    keys = dict(MANAGED_CONFIG_KEYS)
    if remote_context:
        keys["ssh"] = ("claude_history", "path_open")
    return keys


def _run_editor(
    *,
    remote_context: bool,
    stdin: TextIO,
    stdout: TextIO,
) -> int:
    feedback = _Feedback()
    try:
        load_config()
    except ConfigError as exc:
        if not _recover_invalid_config(exc, stdin, stdout, feedback):
            return 2

    def behavior_menu() -> object | None:
        return _behavior_menu(
            stdin,
            stdout,
            feedback,
            remote_context=remote_context,
        )

    def program_menu() -> object | None:
        return _program_menu(
            stdin,
            stdout,
            feedback,
            remote_context=remote_context,
        )

    def environment_menu() -> object | None:
        return _environment_menu(
            stdin,
            stdout,
            feedback,
            remote_context=remote_context,
        )

    category_menus = (
        behavior_menu,
        program_menu,
        environment_menu,
    )
    while True:
        _clear_page(stdout)
        _render_root_navigation(
            stdout,
            remote_context=remote_context,
        )
        _render_feedback(stdout, feedback)
        choice = _read(stdin, stdout, "Choose: ").lower()
        if choice == "q":
            return 0
        if choice == "r":
            reset_scope = (
                "all remote-workspace settings"
                if remote_context
                else "all Railmux-managed settings"
            )
            if _confirm(
                stdin,
                stdout,
                f"Reset {reset_scope} and preserve unknown keys?",
            ):
                _saved(
                    Settings().reset_keys(
                        _managed_reset_keys(remote_context=remote_context)
                    ),
                    feedback,
                )
            continue
        if not choice.isdigit() or not 1 <= int(choice) <= len(category_menus):
            feedback.set("Choose one of the listed entries.", level="warning")
            continue
        result = category_menus[int(choice) - 1]()
        if result is _EXIT:
            return 0


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run the standalone hierarchical settings editor without requiring tmux."""
    parser = argparse.ArgumentParser(
        prog="railmux config",
        description=(
            "Edit this machine's behavior, program paths, and runtime locale "
            "without starting tmux; use --remote for an SSH destination."
        ),
    )
    parser.add_argument(
        "--remote",
        metavar="HOST",
        help="edit the configuration owned by an SSH destination",
    )
    parser.add_argument(
        "--remote-platform",
        choices=("auto", "posix", "windows"),
        default="auto",
        help=(
            "remote shell family for --remote: auto, posix, or windows "
            "(default: auto)"
        ),
    )
    parser.add_argument(
        "--ssh-arg",
        action=AppendSshArgument,
        dest="ssh_arg",
        default=[],
        metavar="VALUE",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--ssh-args",
        action=ExtendSshArguments,
        dest="ssh_arg",
        metavar="ARGS",
        help=(
            "a quoted group of ssh arguments for --remote, split without a shell"
        ),
    )
    parser.add_argument(
        "--remote-context",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    try:
        args = parser.parse_args([] if argv is None else argv)
        if args.remote_context and args.remote:
            parser.error("--remote and --remote-context cannot be combined")
        if args.ssh_arg and not args.remote:
            parser.error("--ssh-args requires --remote")
        if args.remote_platform != "auto" and not args.remote:
            parser.error("--remote-platform requires --remote")
    except SystemExit as exc:
        return int(exc.code)
    if args.remote:
        from railmux.remote_config import run_remote_config

        return run_remote_config(
            args.remote,
            ssh_args=args.ssh_arg,
            raw_argv=tuple([] if argv is None else argv),
            remote_platform=args.remote_platform,
        )
    remote_context = bool(args.remote_context)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    with _editor_screen(stdin, stdout):
        return _run_editor(
            remote_context=remote_context,
            stdin=stdin,
            stdout=stdout,
        )
