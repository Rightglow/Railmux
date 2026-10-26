"""Format a Claude Code session JSONL as human-readable ANSI text for ``less -R``.

Usage::

    python3 -m railmux.transcript <jsonl_path>
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

# ── ANSI styles ──────────────────────────────────────────────────────────
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"
CLAUDE_ACCENT = "\033[38;5;147m"
CLAUDE_MUTED = "\033[38;5;244m"
ITALIC = "\033[3m"
BOLD_OFF = "\033[22m"
ITALIC_OFF = "\033[23m"
FG_DEFAULT = "\033[39m"
LINK_END = "\033]8;;\033\\"

_CODEX_SYNTHETIC_PREFIXES = (
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<multi_agent_mode>",
    "<skills_instructions>",
    "<apps_instructions>",
    "<plugins_instructions>",
    "# AGENTS.md",
)

# ── Record-type filters ──────────────────────────────────────────────────
_SKIP_TYPES = frozenset({
    "mode", "permission-mode", "file-history-snapshot", "attachment",
    "system", "ai-title", "last-prompt", "summary",
})

# Top-level record types that only ever appear in a Codex rollout JSONL.  The
# preview reader keeps only the final 2,000 records, so for longer rollouts the
# leading ``session_meta`` header is dropped and the first record seen here is
# a ``response_item`` / ``event_msg`` / ``turn_context``.  Detect Codex by the
# *presence* of any of these types rather than by a leading ``session_meta``
# (see issue #5).
_CODEX_TYPES = frozenset({
    "session_meta", "response_item", "event_msg", "turn_context",
    "compacted", "world_state",
})
_CLAUDE_TYPES = _SKIP_TYPES | frozenset({"user", "assistant"})

_TAIL_BLOCK_SIZE = 64 * 1024


def _tail_utf8_lines(path: Path, limit: int) -> io.StringIO:
    """Return the final *limit* UTF-8 lines without scanning from the start.

    The preview caller previously launched a separate ``tail`` process before
    the Python renderer.  Reading backwards here removes that process from the
    latency-sensitive Windows path while retaining the same record boundary.
    One extra newline is collected before slicing so the first returned line
    can never be a block-boundary fragment.
    """
    chunks: list[bytes] = []
    newline_count = 0
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        while position > 0 and newline_count <= limit:
            size = min(_TAIL_BLOCK_SIZE, position)
            position -= size
            stream.seek(position)
            chunk = stream.read(size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    payload = b"".join(reversed(chunks))
    lines = payload.splitlines(keepends=True)
    selected = b"".join(lines[-limit:])
    return io.StringIO(selected.decode("utf-8", errors="replace"))


def _sanitize_text(value: object) -> str:
    """Return display text with terminal control sequences made inert.

    ``less -R`` deliberately passes some escape sequences (including OSC-8
    hyperlinks) through to the terminal. Session JSONL is untrusted display
    data, so remove ESC/C0/C1 controls before adding railmux's own ANSI styles.
    Newlines and tabs remain useful transcript formatting.
    """
    if not isinstance(value, str):
        return ""
    return "".join(
        ch for ch in value
        if ch in ("\n", "\t")
        or (ord(ch) >= 0x20 and not 0x7f <= ord(ch) <= 0x9f)
    )


def _is_codex_preview_noise(text: str) -> bool:
    """Whether a Codex user-role block is injected context, not conversation."""
    stripped = text.lstrip()
    return any(stripped.startswith(prefix)
               for prefix in _CODEX_SYNTHETIC_PREFIXES)


def _is_real_user(record: dict) -> bool:
    """True when *record* is a genuine user message (not a synthetic tool-result)."""
    if record.get("type") != "user":
        return False
    msg = record.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    content = msg.get("content", "")
    if isinstance(content, str):
        stripped = content.strip()
        if not stripped:
            return False
        # Drop Claude Code system-command / local-command echo lines.
        if stripped.startswith("<command-name") or stripped.startswith("<local-command"):
            return False
        return True
    if isinstance(content, list):
        # A user message whose content is a list of blocks — treat as real
        # only when at least one block is *not* a tool_result.
        return any(
            isinstance(block, dict) and block.get("type") != "tool_result"
            for block in content
        )
    return False


def _render_user(record: dict) -> str | None:
    msg = record["message"]
    content = msg["content"]
    header = f"\n{CYAN}{BOLD}───── User ─────{RESET}\n"
    if isinstance(content, str):
        text = _sanitize_text(content.strip())
        return header + text + "\n" if text else None
    # Mixed blocks — emit text blocks only (tool_result blocks are noise).
    parts = [header]
    for block in content:
        if (isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].strip()):
            text = _sanitize_text(block["text"].strip())
            if text:
                parts.append(text + "\n")
    return "".join(parts) if len(parts) > 1 else None


def _render_assistant_blocks(record: dict,
                             calls: dict[str, str] | None = None):
    """Yield formatted strings for each display-worthy block in an assistant message."""
    if calls is None:
        calls = {}
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return

    usage = message.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    tokens = None
    if usage:
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        if isinstance(in_tok, int) and isinstance(out_tok, int):
            tokens = in_tok + out_tok

    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type", "")
        if bt == "text":
            text = _sanitize_text(block.get("text"))
            if not text:
                continue
            token_str = f"  ({tokens:,} tokens)" if tokens else ""
            yield f"\n{GREEN}{BOLD}───── Assistant{token_str} ─────{RESET}\n"
            yield text + "\n"
            tokens = None  # only show on first text block
        elif bt == "tool_use":
            name = _sanitize_text(block.get("name")) or "?"
            tool_id = block.get("id")
            if isinstance(tool_id, str) and tool_id:
                calls[tool_id] = name
            inp = block.get("input", {})
            yield f"\n{YELLOW}{BOLD}───── Tool: {name} ─────{RESET}\n"
            if isinstance(inp, dict):
                for key, value in inp.items():
                    yield (f"  {DIM}{_sanitize_text(str(key))}:{RESET} "
                           f"{_truncate(str(value), 300)}\n")
            elif inp:
                yield f"  {DIM}args:{RESET} {_truncate(str(inp), 300)}\n"


def _render_claude_tool_results(record: dict, calls: dict[str, str]):
    """Render Claude tool_result blocks, paired to tool_use names when possible."""
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        text = _flatten_output_text(block.get("content")).strip()
        if not text:
            continue
        tool_id = block.get("tool_use_id")
        name = calls.pop(tool_id, None) if isinstance(tool_id, str) else None
        label = f"Tool output: {name}" if name else "Tool output"
        colour = RED if block.get("is_error") is True else DIM
        yield f"\n{colour}───── {_sanitize_text(label)} ─────{RESET}\n"
        yield _truncate(text, 500) + "\n"


_MD_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_INLINE_RE = re.compile(
    r"(`+)(.+?)\1"
    r"|\*\*(?=\S)(.+?)(?<=\S)\*\*"
    r"|__(?=\S)(.+?)(?<=\S)__"
    r"|(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"
    r"|\[([^\]\n]+)\]\(([^)\s]+)\)"
)
_MD_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_MD_TABLE_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
_LIST_HANG_RE = re.compile(r" *(?:(?:[-*+]|\d+[.)]) +)?")
# Zero-width escapes Railmux emits itself: SGR styles and OSC 8 links.
_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;;[^\x1b]*\x1b\\")
_WRAP_TOKEN_RE = re.compile(
    r"\x1b\[[0-9;]*m|\x1b\]8;;[^\x1b]*\x1b\\| +|[^ \x1b]+")
_TASK_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)

_char_width_function = None


def _char_width(char: str) -> int:
    """Terminal cell width of *char*, matching pyte's ``wcwidth`` when present."""
    global _char_width_function
    if _char_width_function is None:
        try:
            from wcwidth import wcwidth as _char_width_function
        except ImportError:
            import unicodedata

            def _char_width_function(value: str) -> int:
                if unicodedata.combining(value):
                    return 0
                return 2 if unicodedata.east_asian_width(value) in "WF" else 1
    return max(0, _char_width_function(char))


def _visible_width(text: str) -> int:
    # Claude pads an emoji-presentation selector (as in "⚠️") to two cells.
    return sum(
        1 if char == "\ufe0f" else _char_width(char)
        for char in _ESCAPE_RE.sub("", text)
    )


def _wrap_styled(line: str, width: int, hang: int) -> list[str]:
    """Word-wrap one styled line the way Claude's Ink renderer does.

    Rows break at spaces; only a word wider than a whole row is split. Rows
    after the first are indented by *hang* columns so list items and message
    bodies keep their hanging indent instead of restarting at column zero.
    """
    line = line.expandtabs(8)
    if width <= 0 or _visible_width(line) <= width:
        return [line]
    if hang >= width // 2:
        hang = 0
    rows: list[str] = []
    row: list[str] = []
    column = 0

    def newline() -> None:
        nonlocal row, column
        rows.append("".join(row).rstrip(" "))
        row = [" " * hang]
        column = hang

    for token in _WRAP_TOKEN_RE.findall(line):
        if token.startswith("\x1b"):
            row.append(token)
            continue
        token_width = _visible_width(token)
        if token.startswith(" "):
            if column + token_width <= width:
                row.append(token)
                column += token_width
            else:
                newline()
            continue
        capacity = width - hang
        if column + token_width > width and column > hang:
            # Like wrap-ansi: start a word on a fresh row when that fits it,
            # or when splitting it from there needs fewer breaks.
            breaks_here = 1 + (token_width - (width - column) - 1) // capacity
            if (
                token_width <= capacity
                or (token_width - 1) // capacity < breaks_here
            ):
                newline()
        for char in token:
            char_width = _char_width(char)
            if column + char_width > width and column > hang:
                newline()
            row.append(char)
            column += char_width
    rows.append("".join(row).rstrip(" "))
    return rows


def _hang_for(row: str, prefix: int) -> int:
    """Hanging indent for a row whose own content starts after *prefix* cells."""
    content = _ESCAPE_RE.sub("", row)[prefix:]
    return prefix + len(_LIST_HANG_RE.match(content).group(0))


def _close_fragments(fragments: list[str]) -> list[str]:
    """Make each wrapped fragment carry and close its own SGR and link state.

    Table cells sit beside other cells on one row, so a style or link that
    continues onto a cell's next fragment must not leak across the borders.
    """
    closed: list[str] = []
    active: list[str] = []
    link = ""
    for fragment in fragments:
        prefix = "".join(active) + link
        for code in _ESCAPE_RE.findall(fragment):
            if code.startswith("\x1b]"):
                link = "" if code == LINK_END else code
            elif code == RESET:
                active.clear()
            else:
                active.append(code)
        suffix = LINK_END if link else ""
        closed.append(f"{prefix}{fragment}{suffix}{RESET}")
    return closed


def _link(text: str, url: str) -> str:
    """Wrap *text* in an OSC 8 link when *url* is a plain HTTP(S) target."""
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme.lower() in ("http", "https")
            and parsed.hostname is not None
            and len(url) <= 8192
        )
    except ValueError:
        valid = False
    if not valid:
        return text
    return f"\033]8;;{url}\033\\{text}{LINK_END}"


def _table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [
        cell.strip().replace("\\|", "|")
        for cell in _MD_TABLE_CELL_SPLIT_RE.split(stripped)
    ]


# Claude's table constants: spare columns beside the box, the narrowest
# column, the most rows a cell may wrap to, and the most body rows shown.
_TABLE_SPARE = 4
_TABLE_MIN_COLUMN = 3
_TABLE_MAX_CELL_ROWS = 4
_TABLE_MAX_ROWS = 200


def _table_alignments(separator: str, count: int) -> list[str]:
    alignments = []
    for cell in _table_cells(separator):
        if cell.startswith(":") and cell.endswith(":"):
            alignments.append("center")
        elif cell.endswith(":"):
            alignments.append("right")
        else:
            alignments.append("left")
    return (alignments + ["left"] * count)[:count]


def _pad_cell(text: str, width: int, alignment: str) -> str:
    spare = max(0, width - _visible_width(text))
    if alignment == "center":
        return " " * (spare // 2) + text + " " * (spare - spare // 2)
    if alignment == "right":
        return " " * spare + text
    return text + " " * spare


def _markdown_table(lines: list[str], width: int) -> list[str]:
    """Draw a markdown table as Claude does.

    *lines* holds the header, the separator and the body rows; *width* is the
    message body width, two cells less than Claude's terminal width. Cells
    wrap inside a box, unless a cell needs more than four rows or the box
    is too wide: then each row is listed as ``header: value`` lines.
    """
    header = [_markdown_inline(cell) for cell in _table_cells(lines[0])]
    count = len(header)
    alignments = _table_alignments(lines[1], count)
    body = []
    for line in lines[2:]:
        cells = [_markdown_inline(cell) for cell in _table_cells(line)]
        body.append((cells + [""] * count)[:count])
    hidden = max(0, len(body) - _TABLE_MAX_ROWS)
    body = body[:_TABLE_MAX_ROWS]
    columns = width + 2
    rows = [header, *body]
    natural = [
        max(_TABLE_MIN_COLUMN, *(_visible_width(row[i]) for row in rows))
        for i in range(count)
    ]
    minimum = [
        max(
            _TABLE_MIN_COLUMN,
            *(
                _visible_width(word)
                for row in rows
                for word in _ESCAPE_RE.sub("", row[i]).split()
            ),
        )
        for i in range(count)
    ]
    available = max(
        columns - 1 - 3 * count - _TABLE_SPARE, count * _TABLE_MIN_COLUMN)
    hard = False
    if sum(natural) <= available:
        widths = natural
    elif sum(minimum) <= available:
        # Every column fits its longest word, and the spare cells go to each
        # column in proportion to how much more it wants.
        spare = available - sum(minimum)
        wants = [n - m for n, m in zip(natural, minimum)]
        wanted = sum(wants)
        widths = [
            m + (int(want / wanted * spare) if wanted else 0)
            for m, want in zip(minimum, wants)
        ]
    else:
        hard = True
        widths = [
            max(int(m * available / sum(minimum)), _TABLE_MIN_COLUMN)
            for m in minimum
        ]

    def wrap(cell: str, index: int) -> list[str]:
        if not hard and _visible_width(cell) <= widths[index]:
            return [cell]
        return _close_fragments(_wrap_styled(cell, widths[index], 0))

    header_cells = [
        wrap(f"{BOLD}{cell}{BOLD_OFF}" if cell else cell, index)
        for index, cell in enumerate(header)
    ]
    body_cells = [
        [wrap(cell, index) for index, cell in enumerate(row)] for row in body
    ]
    tallest = max(
        len(cell) for row in [header_cells, *body_cells] for cell in row)
    if tallest > _TABLE_MAX_CELL_ROWS:
        return _vertical_table(header, body, columns, hidden)

    def border(left: str, middle: str, right: str) -> str:
        return left + middle.join("─" * (cell + 2) for cell in widths) + right

    def box_rows(cells: list[list[str]], heading: bool) -> list[str]:
        height = max(len(cell) for cell in cells)
        out = []
        for line_index in range(height):
            parts = []
            for index, cell in enumerate(cells):
                # Short cells sit in the vertical middle of the row.
                at = line_index - (height - len(cell)) // 2
                text = cell[at] if 0 <= at < len(cell) else ""
                alignment = "center" if heading else alignments[index]
                parts.append(f" {_pad_cell(text, widths[index], alignment)} ")
            out.append("│" + "│".join(parts) + "│")
        return out

    rendered = [border("┌", "┬", "┐"), *box_rows(header_cells, True)]
    for cells in body_cells:
        rendered.append(border("├", "┼", "┤"))
        rendered.extend(box_rows(cells, False))
    rendered.append(border("└", "┴", "┘"))
    if max(_visible_width(row) for row in rendered) > columns - _TABLE_SPARE:
        return _vertical_table(header, body, columns, hidden)
    if hidden:
        rendered.append(_hidden_rows(hidden))
    return rendered


def _hidden_rows(count: int) -> str:
    return f"… {count:,} more {'row' if count == 1 else 'rows'} not shown"


def _vertical_table(
    header: list[str], body: list[list[str]], columns: int, hidden: int,
) -> list[str]:
    """Claude's fallback layout: one ``header: value`` line block per row."""
    labels = [_ESCAPE_RE.sub("", cell).strip() for cell in header]
    if not body:
        labels, body = [""] * len(header), [header]
    rule = "─" * min(columns - 1, 40)
    rendered: list[str] = []
    for row in body:
        block = []
        for label, cell in zip(labels, row):
            value = " ".join(cell.split())
            if not label and not value:
                continue
            first = (
                columns - _visible_width(label) - 3 if label else columns - 1)
            lines = _close_fragments(
                _wrap_styled(value, max(first, 10), 0))
            if len(lines) > 1:
                rest = " ".join(line.strip() for line in lines[1:])
                lines = [
                    lines[0],
                    *_close_fragments(_wrap_styled(rest, columns - 3, 0)),
                ]
            block.append(
                f"{BOLD}{label}:{BOLD_OFF} {lines[0]}" if label else lines[0])
            block.extend(line for line in lines[1:] if line.strip())
        if not block:
            continue
        if rendered:
            rendered.append(rule)
        rendered.extend(block)
    if hidden:
        if rendered:
            rendered.append(rule)
        rendered.append(_hidden_rows(hidden))
    return rendered


def _markdown_inline(line: str, *, bold: bool = False) -> str:
    """Style inline code, bold, italic and link spans like Claude's view.

    Only targeted "off" codes close each span, so an enclosing style (for
    example a bold heading) survives an inner code or italic span. *bold*
    marks an already-bold context whose nested strong spans must not end it.
    A link shows only its text and stays clickable through OSC 8.
    """

    def replace(match: re.Match[str]) -> str:
        code, strong, strong_alt, emphasis = (
            match.group(2), match.group(3), match.group(4), match.group(5))
        if code is not None:
            return f"{CLAUDE_ACCENT}{code}{FG_DEFAULT}"
        if match.group(6) is not None:
            return _link(_markdown_inline(match.group(6), bold=bold),
                         match.group(7))
        if strong is not None or strong_alt is not None:
            inner = _markdown_inline(
                strong if strong is not None else strong_alt, bold=True)
            return inner if bold else f"{BOLD}{inner}{BOLD_OFF}"
        return f"{ITALIC}{_markdown_inline(emphasis, bold=bold)}{ITALIC_OFF}"

    return _MD_INLINE_RE.sub(replace, line)


def _markdown_lines(lines: list[str], width: int | None = None) -> list[str]:
    """Approximate Claude Code's markdown rendering for sanitized text lines.

    Styled lines end with a full reset: every span is already closed, and the
    reset keeps row wrappers from carrying an ever-growing SGR prefix. Tables
    are drawn as boxes only when the target *width* is known.
    """
    rendered: list[str] = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if _MD_FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            rendered.append(line)
            continue
        if (
            width is not None
            and line.lstrip().startswith("|")
            and index < len(lines)
            and _MD_TABLE_SEPARATOR_RE.match(lines[index])
        ):
            end = index + 1
            while end < len(lines) and lines[end].lstrip().startswith("|"):
                end += 1
            rendered.extend(_markdown_table(lines[index - 1:end], width))
            index = end
            continue
        heading = _MD_HEADING_RE.match(line)
        if heading:
            inner = _markdown_inline(heading.group(2), bold=True)
            rendered.append(f"{BOLD}{inner}{RESET}")
            # Claude always leaves a blank line below a heading.
            if index < len(lines) and lines[index].strip():
                rendered.append("")
            continue
        styled = _markdown_inline(line)
        rendered.append(styled + RESET if styled != line else line)
    return rendered


def _wrap_rows(rows: list[str], width: int | None, prefix: int) -> list[str]:
    """Pre-wrap rendered rows when the pane *width* is known."""
    if width is None:
        return rows
    return [
        wrapped
        for row in rows
        for wrapped in _wrap_styled(row, width, _hang_for(row, prefix))
    ]


def _claude_native_block(
    marker: str, text: str, *, colour: str, markdown: bool = False,
    width: int | None = None,
) -> str:
    """Render a static, non-interactive approximation of one Claude TUI block."""
    lines = _sanitize_text(text).strip().splitlines()
    if markdown:
        lines = _markdown_lines(
            lines, None if width is None else width - 2)
    if not lines:
        return ""
    rows = [
        f"{colour}{marker}{RESET} {lines[0]}",
        *(f"  {line}" for line in lines[1:]),
    ]
    return "\n" + "\n".join(_wrap_rows(rows, width, 2)) + "\n"


def _render_claude_native_user(
    record: dict, width: int | None = None,
) -> str | None:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        text = "\n".join(
            str(block.get("text", "")).strip()
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
    else:
        text = ""
    if text.startswith("<task-notification>"):
        # Claude shows a finished background task as one status line, not as
        # a prompt carrying the notification's XML.
        summary = _TASK_SUMMARY_RE.search(text)
        if summary is None:
            return None
        return _claude_native_block(
            "●", summary.group(1).strip(), colour=CLAUDE_MUTED, width=width,
        ) or None
    return _claude_native_block(
        "❯", text, colour=CLAUDE_ACCENT, width=width) or None


def _render_claude_native_assistant(
    record: dict, calls: dict[str, str], width: int | None = None,
):
    """Yield Claude-like read-only text/tool rows from semantic JSONL records."""
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            rendered = _claude_native_block(
                "●", str(block.get("text", "")), colour=CLAUDE_ACCENT,
                markdown=True, width=width,
            )
            if rendered:
                yield rendered
            continue
        if block_type != "tool_use":
            continue
        name = _sanitize_text(block.get("name")) or "Tool"
        tool_id = block.get("id")
        if isinstance(tool_id, str) and tool_id:
            calls[tool_id] = name
        tool_input = block.get("input")
        summary = ""
        if isinstance(tool_input, dict):
            for key in (
                "description", "command", "query", "pattern",
                "file_path", "path", "url", "skill",
            ):
                value = tool_input.get(key)
                if isinstance(value, str) and value.strip():
                    summary = _sanitize_text(value.strip()).replace("\n", " ")
                    break
        suffix = f"({_truncate(summary, 180)})" if summary else ""
        row = f"{CLAUDE_ACCENT}●{RESET} {BOLD}{name}{RESET}{suffix}"
        yield "\n" + "\n".join(_wrap_rows([row], width, 2)) + "\n"


def _render_claude_native_tool_results(
    record: dict, calls: dict[str, str], width: int | None = None,
):
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        text = _flatten_output_text(block.get("content")).strip()
        if not text:
            continue
        tool_id = block.get("tool_use_id")
        if isinstance(tool_id, str):
            calls.pop(tool_id, None)
        # _truncate sanitizes before appending its own styled ellipsis.
        lines = _truncate(text, 500).splitlines()
        if not lines:
            continue
        colour = RED if block.get("is_error") is True else CLAUDE_MUTED
        rows = [f"  ⎿  {lines[0]}", *(f"     {line}" for line in lines[1:])]
        yield colour
        for row in rows:
            for wrapped in _wrap_rows([row], width, 5):
                yield f"{wrapped}\n"
        yield RESET


def _claude_current_branch(records: list[dict]) -> list[dict]:
    """Project an append-only Claude JSONL onto its current main branch.

    Claude Code links semantic records with ``uuid`` / ``parentUuid``. Editing
    or rewinding a prompt appends a second child instead of removing the old
    suffix, so file order is not conversation order. The newest non-sidechain
    UUID is the active tip; walking its parents retains the shared prefix and
    the replacement suffix while excluding abandoned and sub-agent branches.

    UUID-less metadata is retained for the normal renderer filters. If the
    input is a bounded tail, walking stops safely at the first missing parent.
    """
    by_uuid: dict[str, dict] = {}
    tip: str | None = None
    for record in records:
        uuid = record.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            continue
        by_uuid[uuid] = record
        if record.get("isSidechain") is not True:
            tip = uuid
    if tip is None:
        return records

    current: set[str] = set()
    cursor: str | None = tip
    while cursor and cursor not in current:
        record = by_uuid.get(cursor)
        if record is None:
            break
        current.add(cursor)
        parent = record.get("parentUuid")
        cursor = parent if isinstance(parent, str) and parent else None

    return [
        record for record in records
        if not isinstance(record.get("uuid"), str)
        or not record.get("uuid")
        or record["uuid"] in current
    ]


def _render_claude_records(
    records: list[dict], *, claude_native: bool, width: int | None = None,
):
    """Render only the active Claude branch from buffered JSONL records.

    *width* pre-wraps native rows to the pane like Claude's own renderer.
    """
    calls: dict[str, str] = {}
    for record in _claude_current_branch(records):
        rtype = record.get("type", "")
        if rtype in _SKIP_TYPES:
            continue
        if not claude_native:
            if _is_real_user(record):
                rendered = _render_user(record)
                if rendered:
                    yield rendered
            if rtype == "user":
                yield from _render_claude_tool_results(record, calls)
            elif rtype == "assistant":
                yield from _render_assistant_blocks(record, calls)
            continue
        # Claude hides meta prompts such as a loaded skill's instructions.
        if _is_real_user(record) and record.get("isMeta") is not True:
            rendered = _render_claude_native_user(record, width)
            if rendered:
                yield rendered
        if rtype == "user":
            yield from _render_claude_native_tool_results(record, calls, width)
        elif rtype == "assistant":
            yield from _render_claude_native_assistant(record, calls, width)


def format_transcript(
    source: Path | object,
    fmt: str | None = None,
    *,
    claude_native: bool = False,
    width: int | None = None,
):
    """Read a session JSONL and yield ANSI-formatted strings.

    *source* may be a ``Path`` or any file-like object (e.g. ``sys.stdin``).

    *fmt* is an optional explicit format hint (``"codex"`` or ``"claude"``).
    When omitted (the default), the format is auto-detected from the record
    types actually present in the stream, so a bounded Codex rollout whose
    leading ``session_meta`` header was omitted still renders as Codex.

    *width* (Claude native history only) wraps rows and draws tables for a
    pane of that many cells.

    Callers should write each chunk to stdout, e.g.::

        for chunk in format_transcript(path_or_file):
            sys.stdout.write(chunk)
    """
    own = False
    fh = None
    # When True we're rendering a Codex rollout JSONL; when False, Claude.
    _codex: bool | None = None
    if fmt == "codex":
        _codex = True
    elif fmt == "claude":
        _codex = False
    # call_id -> tool name, so Codex tool outputs can be labelled with the
    # call that produced them (issue #3).
    codex_calls: dict[str, str] = {}
    # Claude rewind/edit branches live in one append-only JSONL, so those
    # records must be buffered until the active tip is known. Production
    # callers bound the input to the latest 2,000 records.
    claude_records: list[dict] = []
    try:
        if isinstance(source, Path):
            fh = open(source, "r", encoding="utf-8")
            own = True
        else:
            fh = source
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            rtype = record.get("type", "")

            # Unknown metadata should not lock detection to Claude: a tailed
            # Codex rollout can begin with a future record type before the next
            # response_item/event_msg establishes the format.
            if _codex is None:
                if rtype in _CODEX_TYPES:
                    _codex = True
                elif rtype in _CLAUDE_TYPES:
                    _codex = False
                else:
                    continue

            if _codex:
                yield from _render_codex(record, codex_calls)
                continue
            claude_records.append(record)
        if _codex is False:
            yield from _render_claude_records(
                claude_records, claude_native=claude_native, width=width)
    except OSError:
        yield f"{YELLOW}Could not read: {source}{RESET}\n"
    finally:
        if own and fh is not None:
            fh.close()


# ── Codex format rendering ──────────────────────────────────────────────


def _render_codex(record: dict, calls: dict[str, str] | None = None):
    """Dispatch a single Codex JSONL record to the right renderer.

    *calls* accumulates ``call_id -> tool name`` so that tool-output records
    can be attributed back to the call that produced them.
    """
    if calls is None:
        calls = {}
    rtype = record.get("type", "")
    if rtype == "session_meta":
        yield from _render_codex_session_meta(record)
    elif rtype == "response_item":
        yield from _render_codex_response_item(record, calls)
    # event_msg, turn_context — skip (token counts, lifecycle noise)


def _render_codex_session_meta(record: dict):
    """Show session metadata as a header."""
    payload = record.get("payload", {}) or {}
    if not isinstance(payload, dict):
        return
    sid = payload.get("id", "?")
    cwd = payload.get("cwd", "?")
    version = payload.get("cli_version", "")
    provider = payload.get("model_provider", "")
    yield f"\n{CYAN}{BOLD}───── Codex Session ─────{RESET}\n"
    yield f"  {DIM}id:{RESET}    {_sanitize_text(str(sid))}\n"
    yield f"  {DIM}cwd:{RESET}   {_sanitize_text(str(cwd))}\n"
    if provider:
        yield f"  {DIM}model:{RESET}  {_sanitize_text(str(provider))}"
        if version:
            yield f"  (CLI {_sanitize_text(str(version))})"
        yield "\n"


def _render_codex_response_item(record: dict, calls: dict[str, str] | None = None):
    """Render a response_item.

    Handles ``message`` plus both tool-call families:
    ``function_call`` / ``function_call_output`` and the (far more common)
    ``custom_tool_call`` / ``custom_tool_call_output`` — see issue #3.
    """
    if calls is None:
        calls = {}
    payload = record.get("payload", {}) or {}
    if not isinstance(payload, dict):
        return
    pt = payload.get("type", "")
    if pt == "message":
        yield from _render_codex_message(payload)
    elif pt == "reasoning":
        yield from _render_codex_reasoning(payload)
    elif pt in ("function_call", "custom_tool_call"):
        yield from _render_codex_tool_call(payload, calls)
    elif pt in ("function_call_output", "custom_tool_call_output"):
        yield from _render_codex_tool_output(payload, calls)


def _render_codex_message(payload: dict):
    """Render a user or assistant message."""
    role = payload.get("role", "")
    content = payload.get("content", [])
    if not isinstance(content, list):
        return

    if role == "user":
        # Extract text from input_text blocks.
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "input_text":
                text = block.get("text", "")
                if (isinstance(text, str) and text.strip()
                        and not _is_codex_preview_noise(text)):
                    parts.append(text.strip())
        if parts:
            yield f"\n{CYAN}{BOLD}───── User ─────{RESET}\n"
            yield _sanitize_text("\n".join(parts)) + "\n"
    elif role == "assistant":
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    yield f"\n{GREEN}{BOLD}───── Assistant ─────{RESET}\n"
                    yield _sanitize_text(text) + "\n"
    # role == "developer" — skip (system instructions, permissions)
    # role == "tool" — skip (tool results)


def _render_codex_reasoning(payload: dict):
    """Render only provider-supplied plaintext summaries, never hidden reasoning."""
    summary = payload.get("summary")
    if not isinstance(summary, list):
        return
    parts: list[str] = []
    for block in summary:
        if (isinstance(block, dict) and block.get("type") == "summary_text"
                and isinstance(block.get("text"), str)
                and block["text"].strip()):
            parts.append(block["text"].strip())
    if parts:
        yield f"\n{DIM}───── Reasoning summary ─────{RESET}\n"
        yield _sanitize_text("\n".join(parts)) + "\n"


def _truncate(text: str, limit: int) -> str:
    """Sanitize and truncate untrusted display text before adding our ANSI."""
    clipped = text[:limit]
    safe = _sanitize_text(clipped)
    return safe if len(text) <= limit else safe + f"{DIM}…{RESET}"


def _render_codex_tool_call(payload: dict, calls: dict[str, str]):
    """Render a ``function_call`` or ``custom_tool_call``.

    ``function_call`` carries its parameters in ``arguments`` (a JSON string);
    ``custom_tool_call`` carries them in ``input``.  The ``call_id`` is recorded
    so the matching output can be attributed back to this call.
    """
    name = _sanitize_text(payload.get("name")) or "?"
    call_id = payload.get("call_id")
    if isinstance(call_id, str) and call_id:
        calls[call_id] = name
    yield f"\n{YELLOW}{BOLD}───── Tool: {name} ─────{RESET}\n"
    arguments = payload.get("arguments")
    if arguments is None:
        arguments = payload.get("input")
    if arguments:
        yield f"  {DIM}args:{RESET} {_truncate(str(arguments), 300)}\n"


def _flatten_output_text(output) -> str:
    """Flatten a provider tool-output value into plain text.

    ``output`` is either a plain string or a list of ``{type, text}`` blocks.
    """
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: list[str] = []
        for block in output:
            if isinstance(block, dict):
                text = block.get("text")
                if not isinstance(text, str):
                    text = block.get("content")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(block, str) and block:
                parts.append(block)
        return "".join(parts)
    return ""


def _render_codex_tool_output(payload: dict, calls: dict[str, str]):
    """Render a ``function_call_output`` / ``custom_tool_call_output``.

    Paired to the originating call via ``call_id`` where available.
    """
    text = _flatten_output_text(payload.get("output")).strip()
    if not text:
        return
    call_id = payload.get("call_id")
    name = calls.pop(call_id, None) if isinstance(call_id, str) else None
    label = f"Tool output: {name}" if name else "Tool output"
    yield f"\n{DIM}───── {label} ─────{RESET}\n"
    yield _truncate(text, 500) + "\n"


# ── CLI entry point ──────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv
    # Optional explicit format hint: ``--format codex|claude`` (or ``--format=…``).
    # Kept back-compatible: when absent the format is auto-detected.
    fmt: str | None = None
    preview_limit: int | None = None
    args: list[str] = []
    it = iter(argv[1:])
    for tok in it:
        if tok == "--format":
            fmt = next(it, None)
        elif tok.startswith("--format="):
            fmt = tok.split("=", 1)[1]
        elif tok == "--preview-limit":
            raw_limit = next(it, None)
            try:
                preview_limit = int(raw_limit) if raw_limit is not None else None
            except ValueError:
                preview_limit = None
            if preview_limit is None or preview_limit <= 0:
                print("--preview-limit requires a positive integer", file=sys.stderr)
                sys.exit(2)
        elif tok.startswith("--preview-limit="):
            try:
                preview_limit = int(tok.split("=", 1)[1])
            except ValueError:
                preview_limit = None
            if preview_limit is None or preview_limit <= 0:
                print("--preview-limit requires a positive integer", file=sys.stderr)
                sys.exit(2)
        else:
            args.append(tok)
    if not args:
        print(
            "Usage: python3 -m railmux.transcript [--format codex|claude] "
            "[--preview-limit N] "
            "<jsonl_path|- for stdin>",
            file=sys.stderr,
        )
        sys.exit(1)
    source = args[0]
    if source == "-":
        stream = sys.stdin
    else:
        jsonl_path = Path(source)
        if not jsonl_path.exists():
            print(f"File not found: {jsonl_path}", file=sys.stderr)
            sys.exit(1)
        try:
            stream = (
                _tail_utf8_lines(jsonl_path, preview_limit)
                if preview_limit is not None
                else jsonl_path
            )
        except OSError:
            print(f"Could not read: {jsonl_path}", file=sys.stderr)
            sys.exit(1)

    try:
        if preview_limit is not None:
            sys.stdout.write(
                f"{DIM}Read-only history preview · showing at most the latest "
                f"{preview_limit:,} records; older activity may be omitted."
                f"{RESET}\n"
            )
        for chunk in format_transcript(stream, fmt):
            sys.stdout.write(chunk)
        if preview_limit is not None:
            sys.stdout.write(
                f"\n{DIM}Read-only · latest ≤{preview_limit:,} records · "
                f"/ search · n/N next/previous · q close"
                f"{RESET}\n"
            )
        # Force a closed pager to fail inside this try block rather than at
        # the interpreter's implicit final flush.
        sys.stdout.flush()
    except BrokenPipeError:
        # The user can quit less while the producer is still writing a large
        # transcript. Redirect the fd so Python's shutdown flush cannot print
        # an "Exception ignored" diagnostic after this normal preview exit.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, sys.stdout.fileno())
            finally:
                os.close(devnull)
        except (AttributeError, OSError, ValueError):
            # Test doubles and already-closed streams may not expose a live fd.
            pass
        return


if __name__ == "__main__":
    main()
