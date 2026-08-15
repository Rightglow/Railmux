"""Safe local launchers for explicit ``railmux ssh`` semantic clicks."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from ipaddress import ip_address
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Sequence
from urllib.parse import urlsplit


_TEXT_EXTENSIONS = frozenset({
    ".bash",
    ".c",
    ".cc",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".cu",
    ".cuh",
    ".go",
    ".h",
    ".hpp",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsonl",
    ".jsx",
    ".log",
    ".lua",
    ".md",
    ".php",
    ".pl",
    ".proto",
    ".py",
    ".rb",
    ".rs",
    ".rst",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vim",
    ".xml",
    ".yaml",
    ".yml",
    ".zsh",
})
_TEXT_NAMES = frozenset({
    "Dockerfile",
    "Makefile",
    "README",
    "LICENSE",
    "CHANGELOG",
})
_TERMINAL_CANDIDATES = (
    ("xdg-terminal-exec", ("--",)),
    ("gnome-terminal", ("--",)),
    ("konsole", ("-e",)),
    ("kitty", ("--detach",)),
    ("wezterm", ("start", "--")),
    ("alacritty", ("-e",)),
    ("foot", ("-e",)),
    ("xterm", ("-e",)),
)


@dataclass(frozen=True)
class OpenResult:
    """One immediate local launch result and optional clipboard fallback."""

    opened: bool
    message: str
    level: str
    copy_data: bytes | None = None


def _detached_popen(argv: Sequence[str]) -> None:
    environ = None
    if os.environ.get("RAILMUX_WINDOWS_RUNTIME") == "msys2":
        # Native Windows launchers must receive URLs and drive paths verbatim;
        # MSYS2's ordinary POSIX-to-Windows argv conversion is neither needed
        # nor safe for these already validated arguments.
        environ = dict(os.environ)
        environ["MSYS2_ARG_CONV_EXCL"] = "*"
    subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=environ,
    )


def _url_opener() -> tuple[str, ...] | None:
    if os.environ.get("TERMUX_VERSION"):
        if executable := shutil.which("termux-open-url"):
            return (executable,)
    if sys.platform == "darwin":
        if executable := shutil.which("open"):
            return (executable, "--")
        return None
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        if executable := shutil.which("wslview"):
            return (executable,)
        if executable := shutil.which("explorer.exe"):
            return (executable,)
    if os.environ.get("RAILMUX_WINDOWS_RUNTIME") == "msys2":
        # ``explorer.exe URL`` is not the Windows URL-association API. In
        # particular, Explorer interprets some otherwise valid query strings
        # (for example a path followed by ``??key=value``) as filesystem
        # input and opens a folder window. ``url.dll`` delegates the already
        # validated HTTP(S) token through the user's registered protocol
        # handler without involving a command shell.
        if executable := shutil.which("rundll32.exe"):
            return (executable, "url.dll,FileProtocolHandler")
        return None
    if executable := shutil.which("xdg-open"):
        return (executable,)
    return None


def open_url(url: str) -> OpenResult:
    """Open one already validated HTTP(S) URL without invoking a shell."""
    opener = _url_opener()
    if opener is None:
        return OpenResult(
            False,
            "No local browser opener found · URL copied",
            "warning",
            url.encode("utf-8"),
        )
    try:
        _detached_popen((*opener, url))
    except OSError:
        return OpenResult(
            False,
            "Could not open local browser · URL copied",
            "warning",
            url.encode("utf-8"),
        )
    hostname = urlsplit(url).hostname
    loopback = hostname == "localhost"
    if hostname is not None and not loopback:
        try:
            loopback = ip_address(hostname).is_loopback
        except ValueError:
            pass
    if loopback:
        return OpenResult(
            True,
            "Opened local loopback URL · remote ports are not tunneled",
            "warning",
        )
    return OpenResult(True, "Opened URL in local browser", "success")


def open_windows_path(path: str, *, directory: bool) -> OpenResult:
    """Open one already validated managed-Windows path without a shell."""
    explorer = shutil.which("explorer.exe")
    cygpath = shutil.which("cygpath")
    if explorer is None or cygpath is None:
        return OpenResult(
            False,
            "Windows path opener unavailable · path copied",
            "warning",
            path.encode("utf-8"),
        )
    try:
        native = subprocess.check_output(
            (cygpath, "-w", "--", path),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
            encoding="utf-8",
            errors="strict",
        ).rstrip("\r\n")
        if not native or any(character in native for character in "\x00\r\n"):
            raise ValueError
        _detached_popen((explorer, native))
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        return OpenResult(
            False,
            "Could not open Windows path · path copied",
            "warning",
            path.encode("utf-8"),
        )
    return OpenResult(
        True,
        "Opened directory outside Railmux"
        if directory
        else "Opened file outside Railmux",
        "success",
    )


def is_vim_text_path(path: str) -> bool:
    """Whether a regular remote file belongs to the conservative text set."""
    name = PurePosixPath(path).name
    return (
        name in _TEXT_NAMES
        or any(name.startswith(f"{prefix}.") for prefix in _TEXT_NAMES)
        or PurePosixPath(name).suffix.lower() in _TEXT_EXTENSIONS
    )


def _remote_login_command(
    path: str,
    *,
    directory: bool,
    regular_file: bool,
    line: int | None,
    column: int | None,
) -> str:
    """Build a quoted command for the remote account's login shell."""
    destination = path if directory else str(PurePosixPath(path).parent)
    enter_directory = (
        f"cd -- {shlex.quote(destination)} "
        '&& exec "${SHELL:-/bin/sh}" -l'
    )
    if directory or not regular_file or not is_vim_text_path(path):
        script = enter_directory
    else:
        vim_argv = ["vim"]
        if line is not None:
            if column is not None:
                vim_argv.append(f"+call cursor({line}, {column})")
            else:
                vim_argv.append(f"+{line}")
        vim_argv.extend(("--", path))
        notice = shlex.quote(
            "Railmux: vim is unavailable; opened the file's directory."
        )
        script = (
            "if command -v vim >/dev/null 2>&1; "
            f"then exec {shlex.join(vim_argv)}; "
            f"else printf '%s\\n' {notice}; {enter_directory}; fi"
        )
    return 'exec "${SHELL:-/bin/sh}" -lc ' + shlex.quote(script)


def build_remote_open_argv(
    destination: str,
    *,
    ssh_args: Sequence[str],
    path: str,
    directory: bool,
    regular_file: bool = True,
    line: int | None = None,
    column: int | None = None,
) -> tuple[str, ...]:
    """Return an interactive SSH argv for one validated remote path."""
    filtered = tuple(value for value in ssh_args if value != "-T")
    command = _remote_login_command(
        path,
        directory=directory,
        regular_file=regular_file,
        line=line,
        column=column,
    )
    # Put -t after caller-supplied SSH options so this explicitly interactive
    # child wins over a RequestTTY setting inherited from the original display.
    return ("ssh", *filtered, "-t", "--", destination, command)


def _mac_terminal_argv(command: str) -> tuple[str, ...] | None:
    executable = shutil.which("osascript")
    if executable is None:
        return None
    script = (
        "on run argv\n"
        "tell application \"Terminal\"\n"
        "activate\n"
        "do script (item 1 of argv)\n"
        "end tell\n"
        "end run"
    )
    return (executable, "-e", script, command)


def _linux_terminal_argv(ssh_argv: Sequence[str]) -> tuple[str, ...] | None:
    for name, prefix in _TERMINAL_CANDIDATES:
        executable = shutil.which(name)
        if executable is not None:
            return (executable, *prefix, *ssh_argv)
    return None


def _wsl_terminal_argv(ssh_argv: Sequence[str]) -> tuple[str, ...] | None:
    """Launch the same Linux SSH argv in a fresh Windows Terminal tab."""
    distribution = os.environ.get("WSL_DISTRO_NAME")
    terminal = shutil.which("wt.exe")
    wsl = shutil.which("wsl.exe")
    if not distribution or terminal is None or wsl is None:
        return None
    return (
        terminal,
        "new-tab",
        wsl,
        "--distribution",
        distribution,
        "--exec",
        *ssh_argv,
    )


def open_remote_path(
    destination: str,
    *,
    ssh_args: Sequence[str],
    path: str,
    directory: bool,
    regular_file: bool = True,
    line: int | None = None,
    column: int | None = None,
) -> OpenResult:
    """Open one server-validated path in a new local terminal when possible."""
    ssh_argv = build_remote_open_argv(
        destination,
        ssh_args=ssh_args,
        path=path,
        directory=directory,
        regular_file=regular_file,
        line=line,
        column=column,
    )
    command = shlex.join(ssh_argv)
    if sys.platform == "darwin":
        terminal_argv = _mac_terminal_argv(command)
    elif os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        terminal_argv = _wsl_terminal_argv(ssh_argv)
    else:
        terminal_argv = _linux_terminal_argv(ssh_argv)
    if terminal_argv is None:
        return OpenResult(
            False,
            "No supported local terminal · SSH command copied",
            "warning",
            command.encode("utf-8"),
        )
    try:
        _detached_popen(terminal_argv)
    except OSError:
        return OpenResult(
            False,
            "Could not open a local terminal · SSH command copied",
            "warning",
            command.encode("utf-8"),
        )
    if directory:
        message = "Opening remote directory · new terminal"
    elif regular_file and is_vim_text_path(path):
        message = "Opening remote file in Vim · new terminal"
    else:
        message = "Opening remote file's directory · new terminal"
    return OpenResult(True, message, "success")
