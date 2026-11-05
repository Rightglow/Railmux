"""Render a Codex rollout JSONL the way the Codex TUI draws its transcript.

Railmux-managed Codex history over ``railmux ssh`` shows this projection
instead of forwarding each wheel tick to Codex's own alternate-screen
transcript. Codex 0.16x persists every finished TUI item as an
``event_msg``/``item_completed`` record (user and agent messages, commands
with their parsed intent, file changes, MCP calls, ...), so those records are
rendered with Codex's markers: ``›`` prompts, ``•`` items, ``└`` details and
the "Worked for" turn footer. Older rollouts without those records fall back
to the ``response_item`` conversation. ``thread_rolled_back`` drops the
rewound turns, as Codex does.

Rows are pre-wrapped to the pane width so the projection lines up with the
live screen that follows it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from collections.abc import Iterable, Iterator

from railmux.transcript import (
    BOLD,
    DIM,
    FG_DEFAULT,
    ITALIC,
    RED,
    RESET,
    _CJK_PUNCTUATION,
    _CODEX_SYNTHETIC_PREFIXES,
    _char_width,
    _flatten_output_text,
    _link,
    _markdown_inline,
    _markdown_lines,
    _sanitize_text,
    _wrap_rows as _wrap_rows_base,
)

def _wrap_rows(rows: list[str], width: int | None, prefix: int) -> list[str]:
    return _wrap_rows_base(rows, width, prefix, cjk_breaks=True)


CODEX_GREEN = "\033[38;5;2m"
CODEX_RED = "\033[38;5;1m"
CODEX_BLUE = "\033[38;5;75m"
CODEX_CYAN = "\033[36m"

_PROMPT = f"{BOLD}{DIM}›{RESET}"
_BULLET = f"{DIM}•{RESET}"
_OK_BULLET = f"{BOLD}{CODEX_GREEN}•{RESET}"
_FAIL_BULLET = f"{BOLD}{CODEX_RED}•{RESET}"
_OUTPUT_HEAD_LINES = 2
_OUTPUT_TAIL_LINES = 1
_UNWRAPPED_DETAIL_CHARS = 160
_EXPLORE_TYPES = frozenset({"read", "search", "list_files"})

# Only these lines can contribute; everything else (token counts, tool
# schemas, raw model items) is skipped before JSON decoding.
_ITEM_MARKERS = (
    '"item_completed"', '"task_complete"', '"turn_aborted"',
    '"thread_rolled_back"', '"session_meta"',
)
_FALLBACK_MARKERS = (
    '"response_item"', '"task_complete"', '"turn_aborted"',
    '"thread_rolled_back"', '"session_meta"',
)

_LINK_RE = re.compile(
    r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)"
    f"|(https?://[^\\s<>\"'`{_CJK_PUNCTUATION}]+)"
)
_URL_TRAILING = ".,;:!?)]}"
_STYLE_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;;[^\x1b]*\x1b\\")


def _codex_inline(line: str, *, bold: bool = False) -> str:
    """Inline markdown as Codex shows it: a link keeps its URL visible.

    Codex renders ``[label](url)`` as ``label (url)``. Both that URL and a
    bare one become OSC 8 links so they stay clickable when wrapped.
    """
    parts: list[str] = []
    last = 0
    for match in _LINK_RE.finditer(line):
        parts.append(_markdown_inline(line[last:match.start()], bold=bold))
        if match.group(1) is not None:
            url = match.group(2)
            label = _markdown_inline(match.group(1), bold=bold)
            parts.append(f"{label} ({CODEX_CYAN}{_link(url, url)}{FG_DEFAULT})")
        else:
            raw = match.group(3)
            url = raw.rstrip(_URL_TRAILING)
            parts.append(f"{CODEX_CYAN}{_link(url, url)}{FG_DEFAULT}")
            parts.append(raw[len(url):])
        last = match.end()
    parts.append(_markdown_inline(line[last:], bold=bold))
    return "".join(parts)


def _clip(text: str, width: int | None) -> str:
    """Cut one plain detail line to *width* cells with an ellipsis."""
    limit = _UNWRAPPED_DETAIL_CHARS if width is None else max(8, width)
    if _visible(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        used += _char_width(char)
        if used > limit - 1:
            break
        kept.append(char)
    return "".join(kept) + "…"


def _visible(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _duration(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds) // 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {seconds:02d}s"


def _clock(epoch: object) -> str | None:
    if not isinstance(epoch, (int, float)) or epoch <= 0:
        return None
    try:
        stamp = time.localtime(float(epoch))
    except (OverflowError, OSError, ValueError):
        return None
    hour = stamp.tm_hour % 12 or 12
    return f"{hour}:{stamp.tm_min:02d} {'AM' if stamp.tm_hour < 12 else 'PM'}"


def _command_text(command: object) -> str:
    """Show the script a shell wrapper runs, not the wrapper itself."""
    if isinstance(command, str):
        return command
    if not isinstance(command, list) or not all(
        isinstance(part, str) for part in command
    ):
        return ""
    if (
        len(command) == 3
        and os.path.basename(command[0]) in {"bash", "sh", "zsh"}
        and command[1] in {"-lc", "-c"}
    ):
        return command[2]
    return shlex.join(command)


def _diff_counts(change: dict) -> tuple[int, int]:
    kind = change.get("type")
    if kind in ("add", "delete"):
        content = change.get("content")
        count = len(content.splitlines()) if isinstance(content, str) else 0
        return (count, 0) if kind == "add" else (0, count)
    diff = change.get("unified_diff")
    if not isinstance(diff, str):
        return 0, 0
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def _counts(added: int, removed: int) -> str:
    return (
        f"({CODEX_GREEN}+{added}{FG_DEFAULT} {CODEX_RED}-{removed}{FG_DEFAULT})"
    )


class _Renderer:
    """Accumulate rendered blocks per user turn so rewinds can drop turns."""

    def __init__(self, width: int | None) -> None:
        self.width = width
        self.blocks: list[str] = []
        self.turn_starts: list[int] = []
        self.cwd: str | None = None
        self.explored: list[dict] = []
        self.pending_calls: dict[str, str] = {}

    # -- layout helpers -------------------------------------------------
    def _block(self, marker: str, lines: list[str], *, prefix: int = 2) -> None:
        if not lines:
            return
        rows = [f"{marker} {lines[0]}", *(" " * prefix + line for line in lines[1:])]
        self._emit(_wrap_rows(rows, self.width, prefix))

    def _details(self, lines: list[str], *, style: str = DIM) -> list[str]:
        """``└`` detail rows, clipped like Codex instead of wrapped."""
        width = None if self.width is None else self.width - 4
        rows: list[str] = []
        for index, line in enumerate(lines):
            lead = "  └ " if index == 0 else "    "
            rows.append(f"{DIM}{lead}{RESET}{style}{_clip(line, width)}{RESET}")
        return rows

    def _emit(self, rows: list[str]) -> None:
        self.blocks.append("\n" + "\n".join(rows) + "\n")

    def _path(self, value: object) -> str:
        path = str(value or "")
        if path.startswith("file://"):
            path = path[len("file://"):]
        if self.cwd and path.startswith(self.cwd.rstrip("/") + "/"):
            return path[len(self.cwd.rstrip("/")) + 1:]
        return path

    # -- turn structure -------------------------------------------------
    def start_turn(self) -> None:
        self.flush()
        self.turn_starts.append(len(self.blocks))

    def rollback(self, turns: object) -> None:
        self.flush()
        if not isinstance(turns, int) or turns <= 0 or not self.turn_starts:
            return
        turns = min(turns, len(self.turn_starts))
        cut = self.turn_starts[-turns]
        del self.turn_starts[-turns:]
        del self.blocks[cut:]

    def flush(self) -> None:
        if not self.explored:
            return
        explored, self.explored = self.explored, []
        lines: list[str] = []
        reads: list[str] = []

        def flush_reads() -> None:
            if reads:
                lines.append(f"{CODEX_BLUE}Read{FG_DEFAULT} {', '.join(reads)}")
                reads.clear()

        for parsed in explored:
            kind = parsed.get("type")
            if kind == "read":
                name = parsed.get("name") or self._path(parsed.get("path"))
                if name and name not in reads:
                    reads.append(_sanitize_text(str(name)))
                continue
            flush_reads()
            if kind == "search":
                query = _sanitize_text(str(parsed.get("query") or ""))
                where = self._path(parsed.get("path"))
                text = f"{CODEX_BLUE}Search{FG_DEFAULT} {query}".rstrip()
                if where:
                    text += f" in {_sanitize_text(where)}"
                lines.append(text)
            elif kind == "list_files":
                where = self._path(parsed.get("path")) or "."
                lines.append(f"{CODEX_BLUE}List{FG_DEFAULT} {_sanitize_text(where)}")
        flush_reads()
        width = None if self.width is None else self.width - 4
        rows = [f"{_BULLET} {BOLD}Explored{RESET}"]
        for index, line in enumerate(lines):
            lead = "  └ " if index == 0 else "    "
            rows.append(f"{DIM}{lead}{RESET}{_clip_styled(line, width)}")
        self._emit(rows)

    # -- items ----------------------------------------------------------
    def session_meta(self, payload: dict, *, header: bool = True) -> None:
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.cwd = cwd
        if not header or self.blocks:
            return
        version = payload.get("cli_version")
        title = f"{DIM}>_{RESET} {BOLD}OpenAI Codex{RESET}"
        if isinstance(version, str) and version:
            title += f" (v{_sanitize_text(version)})"
        rows = [title]
        if self.cwd:
            budget = None if self.width is None else self.width - 3
            rows.append(f"   {DIM}{_clip(_sanitize_text(self.cwd), budget)}{RESET}")
        self._emit(rows)

    def user(self, text: str) -> None:
        text = _sanitize_text(text).strip()
        if not text or text.lstrip().startswith(_CODEX_SYNTHETIC_PREFIXES):
            return
        self.start_turn()
        self._block(_PROMPT, text.splitlines())

    def agent(self, text: str) -> None:
        self.flush()
        lines = _sanitize_text(text).strip().splitlines()
        if not lines:
            return
        width = None if self.width is None else self.width - 2
        rows: list[str] = []
        for index, line in enumerate(
            _markdown_lines(lines, width, inline=_codex_inline)
        ):
            lead = f"{_BULLET} " if index == 0 else "  "
            plain = _STYLE_RE.sub("", line)
            if plain.startswith(">"):
                # Codex keeps the quote bar on every wrapped row.
                body = line.split(">", 1)[1].lstrip(" ")
                inner = None if width is None else width - 2
                for part in _wrap_rows([body], inner, 0):
                    rows.append(f"{lead}{CODEX_GREEN}> {part}{RESET}")
                    lead = "  "
                continue
            rows.extend(_wrap_rows([lead + line], self.width, 2))
        self._emit(rows)

    def reasoning(self, parts: Iterable[str]) -> None:
        text = "\n".join(part.strip() for part in parts if part.strip())
        if not text:
            return
        self.flush()
        lines = [
            f"{DIM}{ITALIC}{line}{RESET}"
            for line in _sanitize_text(text).splitlines()
        ]
        self._block(_BULLET, lines)

    def command(self, item: dict) -> None:
        parsed = item.get("parsed_cmd")
        if (
            isinstance(parsed, list)
            and parsed
            and all(
                isinstance(entry, dict) and entry.get("type") in _EXPLORE_TYPES
                for entry in parsed
            )
        ):
            self.explored.extend(parsed)
            return
        self.ran(
            _command_text(item.get("command")),
            item.get("aggregated_output"),
            item.get("exit_code"),
            item.get("status"),
        )

    def ran(
        self, command: str, output: object, exit_code: object, status: object,
    ) -> None:
        self.flush()
        command_lines = _sanitize_text(command).strip().splitlines() or ["?"]
        head = command_lines[0] + (" …" if len(command_lines) > 1 else "")
        failed = (
            isinstance(exit_code, int) and exit_code != 0
        ) or status in ("failed", "declined")
        bullet = _FAIL_BULLET if failed else _OK_BULLET
        rows = _wrap_rows([f"{bullet} {BOLD}Ran{RESET} {head}"], self.width, 4)
        text = _sanitize_text(
            output if isinstance(output, str) else _flatten_output_text(output)
        ).rstrip()
        out_lines = [line.rstrip() for line in text.splitlines()] if text else []
        shown = out_lines
        if len(out_lines) > _OUTPUT_HEAD_LINES + _OUTPUT_TAIL_LINES + 1:
            hidden = len(out_lines) - _OUTPUT_HEAD_LINES - _OUTPUT_TAIL_LINES
            shown = [
                *out_lines[:_OUTPUT_HEAD_LINES],
                f"… +{hidden} lines",
                *out_lines[-_OUTPUT_TAIL_LINES:],
            ]
        rows.extend(self._details(shown or ["(no output)"]))
        self._emit(rows)

    def file_change(self, changes: object, status: object = None) -> None:
        if not isinstance(changes, dict) or not changes:
            return
        self.flush()
        entries: list[tuple[str, int, int, str]] = []
        for path, change in changes.items():
            if not isinstance(change, dict):
                continue
            added, removed = _diff_counts(change)
            target = change.get("move_path")
            shown = self._path(path)
            if isinstance(target, str) and target and target != "None":
                shown += f" → {self._path(target)}"
            entries.append(
                (_sanitize_text(shown), added, removed, str(change.get("type")))
            )
        if not entries:
            return
        total_added = sum(entry[1] for entry in entries)
        total_removed = sum(entry[2] for entry in entries)
        bullet = _FAIL_BULLET if status in ("failed", "declined") else _BULLET
        if len(entries) == 1:
            path, added, removed, kind = entries[0]
            verb = {"add": "Added", "delete": "Deleted"}.get(kind, "Edited")
            rows = [f"{bullet} {BOLD}{verb}{RESET} {path} {_counts(added, removed)}"]
            self._emit(_wrap_rows(rows, self.width, 4))
            return
        rows = [
            f"{bullet} {BOLD}Edited{RESET} {len(entries)} files "
            f"{_counts(total_added, total_removed)}"
        ]
        for index, (path, added, removed, _kind) in enumerate(entries):
            lead = "  └ " if index == 0 else "    "
            rows.append(f"{DIM}{lead}{RESET}{path} {_counts(added, removed)}")
        self._emit(_wrap_rows(rows, self.width, 4))

    def mcp(self, item: dict) -> None:
        self.flush()
        server = _sanitize_text(str(item.get("server") or "mcp"))
        tool = _sanitize_text(str(item.get("tool") or "tool"))
        arguments = item.get("arguments")
        try:
            args = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            args = ""
        failed = bool(item.get("error")) or item.get("status") == "failed"
        bullet = _FAIL_BULLET if failed else _OK_BULLET
        call = f"{server}.{tool}({_sanitize_text(args) if arguments else ''})"
        budget = None if self.width is None else self.width - len("• Called ")
        rows = [f"{bullet} {BOLD}Called{RESET} {_clip(call, budget)}"]
        result = item.get("result")
        detail = ""
        if isinstance(result, dict):
            detail = _flatten_output_text(result.get("content"))
        error = item.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif isinstance(error, str):
            detail = error
        first = _sanitize_text(detail).strip().splitlines()
        if first:
            rows.extend(self._details(first[:1]))
        self._emit(rows)

    def image(self, path: object) -> None:
        self.flush()
        rows = [f"{_BULLET} {BOLD}Viewed Image{RESET}"]
        rows.extend(self._details([_sanitize_text(self._path(path))], style=""))
        self._emit(rows)

    def compacted(self) -> None:
        self.flush()
        self._emit([f"{_BULLET} {DIM}Context compacted{RESET}"])

    def worked(self, payload: dict) -> None:
        self.flush()
        duration = payload.get("duration_ms")
        if not isinstance(duration, int):
            started, completed = payload.get("started_at"), payload.get("completed_at")
            if not (
                isinstance(started, (int, float))
                and isinstance(completed, (int, float))
            ):
                return
            duration = int((completed - started) * 1000)
        text = f"Worked for {_duration(duration)}"
        clock = _clock(payload.get("completed_at"))
        if clock:
            text += f" • {clock}"
        self._emit([f"{DIM}  {text}{RESET}"])

    def interrupted(self) -> None:
        self.flush()
        self._emit([f"{RED}■{RESET} Conversation interrupted"])

    def finish(self) -> str:
        self.flush()
        return "".join(self.blocks)


def _clip_styled(line: str, width: int | None) -> str:
    """Clip a styled detail row; styles here are short, so count text only."""
    plain = _STYLE_RE.sub("", line)
    clipped = _clip(plain, width)
    if clipped == plain:
        return line + RESET
    return clipped + RESET


def _text_parts(content: object, kinds: tuple[str, ...]) -> list[str]:
    parts: list[str] = []
    if not isinstance(content, list):
        return parts
    for block in content:
        if isinstance(block, dict) and block.get("type") in kinds:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(block, dict) and block.get("type") in (
            "image", "local_image", "input_image",
        ):
            parts.append("[Image]")
    return parts


def _item(renderer: _Renderer, item: dict) -> None:
    kind = item.get("type")
    if kind == "UserMessage":
        renderer.user("\n".join(_text_parts(item.get("content"), ("text",))))
    elif kind == "AgentMessage":
        renderer.agent("\n".join(_text_parts(item.get("content"), ("Text", "text"))))
    elif kind == "Reasoning":
        summary = item.get("summary_text")
        if isinstance(summary, list):
            renderer.reasoning(part for part in summary if isinstance(part, str))
    elif kind == "CommandExecution":
        renderer.command(item)
    elif kind == "FileChange":
        renderer.file_change(item.get("changes"), item.get("status"))
    elif kind == "McpToolCall":
        renderer.mcp(item)
    elif kind == "ImageView":
        renderer.image(item.get("path"))
    elif kind == "ContextCompaction":
        renderer.compacted()


def _patch_changes(patch: str) -> dict[str, dict]:
    """File changes named by an ``apply_patch`` envelope (fallback path)."""
    changes: dict[str, dict] = {}
    current: dict | None = None
    for line in patch.splitlines():
        for prefix, kind in (
            ("*** Add File: ", "add"),
            ("*** Delete File: ", "delete"),
            ("*** Update File: ", "update"),
        ):
            if line.startswith(prefix):
                current = {"type": kind, "unified_diff": ""}
                changes[line[len(prefix):].strip()] = current
                break
        else:
            if line.startswith("*** Move to: ") and current is not None:
                current["move_path"] = line[len("*** Move to: "):].strip()
            elif current is not None and line[:1] in ("+", "-"):
                current["unified_diff"] += line + "\n"
    for change in changes.values():
        if change["type"] != "update":
            change["content"] = "\n".join(
                line[1:] for line in change["unified_diff"].splitlines())
    return changes


def _fallback_item(renderer: _Renderer, payload: dict) -> None:
    """Render a pre-``item_completed`` rollout from its model items."""
    kind = payload.get("type")
    if kind == "message":
        role = payload.get("role")
        if role == "user":
            renderer.user("\n".join(
                _text_parts(payload.get("content"), ("input_text",))))
        elif role == "assistant":
            renderer.agent("\n".join(
                _text_parts(payload.get("content"), ("output_text",))))
    elif kind == "reasoning":
        summary = payload.get("summary")
        if isinstance(summary, list):
            renderer.reasoning(
                block["text"] for block in summary
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
    elif kind in ("function_call", "local_shell_call"):
        name = payload.get("name")
        arguments = payload.get("arguments")
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else {}
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        command = args.get("command", args.get("cmd"))
        if name in ("shell", "exec_command", "shell_command", "container.exec") and command:
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                renderer.pending_calls[call_id] = _command_text(command)
    elif kind == "custom_tool_call" and payload.get("name") == "apply_patch":
        patch = payload.get("input")
        if isinstance(patch, str):
            renderer.file_change(_patch_changes(patch))
    elif kind in ("function_call_output", "custom_tool_call_output"):
        call_id = payload.get("call_id")
        pending = renderer.pending_calls.pop(call_id, None) if isinstance(
            call_id, str) else None
        if pending is not None:
            output = payload.get("output")
            if isinstance(output, str):
                try:
                    decoded = json.loads(output)
                except ValueError:
                    decoded = None
                if isinstance(decoded, dict) and "output" in decoded:
                    output = decoded.get("output")
            renderer.ran(pending, output, None, None)


def render_codex_native(
    lines: Iterable[str], width: int | None = None, *, header: bool = True,
) -> Iterator[str]:
    """Yield the Codex-TUI-like projection of rollout *lines*.

    *header* is false for a later segment of a thread whose earlier segment
    already drew the session header.
    """
    raw = lines if isinstance(lines, list) else list(lines)
    use_items = any('"item_completed"' in line for line in raw)
    markers = _ITEM_MARKERS if use_items else _FALLBACK_MARKERS
    renderer = _Renderer(width)
    for line in raw:
        if not any(marker in line for marker in markers):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        rtype = record.get("type")
        if rtype == "session_meta":
            renderer.session_meta(payload, header=header)
            continue
        if rtype == "response_item":
            if not use_items:
                _fallback_item(renderer, payload)
            continue
        if rtype != "event_msg":
            continue
        event = payload.get("type")
        if event == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict):
                _item(renderer, item)
        elif event == "task_complete":
            renderer.worked(payload)
        elif event == "turn_aborted":
            renderer.interrupted()
        elif event == "thread_rolled_back":
            renderer.rollback(payload.get("num_turns"))
    rendered = renderer.finish()
    if rendered:
        yield rendered
