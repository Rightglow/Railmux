from __future__ import annotations

import json
import re

from railmux.codex_transcript import render_codex_native

_STYLE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;;[^\x1b]*\x1b\\")


def _item(item: dict) -> dict:
    return {"type": "event_msg", "payload": {"type": "item_completed", "item": item}}


def _user(text: str) -> dict:
    return _item({"type": "UserMessage", "content": [{"type": "text", "text": text}]})


def _agent(text: str) -> dict:
    return _item({"type": "AgentMessage", "content": [{"type": "Text", "text": text}]})


def _render(records: list[dict], width: int | None = 60) -> list[str]:
    lines = [json.dumps(record, ensure_ascii=False) + "\n" for record in records]
    text = "".join(render_codex_native(lines, width))
    return [_STYLE.sub("", row) for row in text.splitlines()]


def test_codex_items_render_like_the_codex_tui() -> None:
    rows = _render([
        {"type": "session_meta", "payload": {"cwd": "/w", "cli_version": "0.166.0"}},
        _user("<environment_context>noise</environment_context>"),
        _user("fix the bug"),
        _item({"type": "CommandExecution", "command": ["/bin/bash", "-lc", "cat a.py"],
               "parsed_cmd": [{"type": "read", "name": "a.py", "path": "/w/a.py"}]}),
        _item({"type": "CommandExecution", "command": ["/bin/bash", "-lc", "rg foo"],
               "parsed_cmd": [{"type": "search", "query": "foo", "path": "/w/src"}]}),
        _item({"type": "CommandExecution",
               "command": ["/bin/bash", "-lc", "pytest -q\necho done"],
               "parsed_cmd": [{"type": "unknown", "cmd": "pytest -q"}],
               "aggregated_output": "1\n2\n3\n4\n5\n6\n", "exit_code": 1}),
        _item({"type": "FileChange", "status": "completed", "changes": {
            "/w/a.py": {"type": "update", "unified_diff": "@@\n-x\n+y\n+z\n"},
            "/w/b.py": {"type": "add", "content": "one\ntwo\n"}}}),
        _agent("Done.\n\n> quoted"),
        {"type": "event_msg", "payload": {"type": "task_complete", "duration_ms": 71000}},
    ])

    assert rows[1:3] == [">_ OpenAI Codex (v0.166.0)", "   /w"]
    assert "noise" not in "\n".join(rows)
    assert "› fix the bug" in rows
    explored = rows.index("• Explored")
    assert rows[explored + 1:explored + 3] == [
        "  └ Read a.py", "    Search foo in src"]
    ran = rows.index("• Ran pytest -q …")
    assert rows[ran + 1:ran + 5] == ["  └ 1", "    2", "    … +3 lines", "    6"]
    assert "• Edited 2 files (+4 -1)" in rows
    assert "  └ a.py (+2 -1)" in rows
    assert "• Done." in rows
    assert "  > quoted" in rows
    assert rows[-1].startswith("  Worked for 1m 11s")


def test_codex_rollback_drops_rewound_turns() -> None:
    rows = _render([
        _user("first"), _agent("one"),
        _user("second"), _agent("two"),
        {"type": "event_msg",
         "payload": {"type": "thread_rolled_back", "num_turns": 1}},
        _user("again"), _agent("three"),
    ])

    text = "\n".join(rows)
    assert "› first" in rows and "• one" in rows
    assert "second" not in text and "two" not in text
    assert "› again" in rows and "• three" in rows


def test_codex_links_stay_visible_and_clickable_when_wrapped() -> None:
    url = "https://example.com/" + "x" * 70
    lines = [json.dumps(_agent(f"打开网页 ([页面]({url}))。")) + "\n"]
    styled = "".join(render_codex_native(lines, 40))

    assert f"\x1b]8;;{url}\x1b\\" in styled
    visible = [_STYLE.sub("", row) for row in styled.splitlines()]
    assert all(len(row) <= 40 for row in visible)
    assert "".join(row.strip() for row in visible).count(url) == 1


def test_codex_quote_keeps_its_bar_on_wrapped_rows() -> None:
    rows = _render([_agent("> " + "word " * 20)], width=30)

    quoted = [row for row in rows if "word" in row]
    assert len(quoted) > 1
    assert all(row.startswith(("• > ", "  > ")) for row in quoted)


def test_rollout_without_items_falls_back_to_model_messages() -> None:
    rows = _render([
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "old question"}]}},
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "shell", "call_id": "c1",
            "arguments": json.dumps({"command": ["bash", "-lc", "ls"]})}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1", "output": "a\nb"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "old answer"}]}},
    ])

    assert "› old question" in rows
    assert rows[rows.index("• Ran ls") + 1] == "  └ a"
    assert "• old answer" in rows


def test_item_rollouts_ignore_duplicate_model_items() -> None:
    rows = _render([
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "question"}]}},
        _user("question"),
    ])

    assert rows.count("› question") == 1


def test_codex_cjk_text_breaks_between_characters() -> None:
    text = "后，NVFP4 Conv 仍更快，而 WinoVAE 的整网优势很大程度来自周边算子融合。" * 3
    rows = [row for row in _render([_agent(text)], width=40) if row.strip()]

    assert len(rows) > 2
    for row in rows[:-1]:
        width = sum(2 if ord(char) > 0x2e80 else 1 for char in row)
        assert width >= 37, row
    assert not any(row.lstrip(" •").startswith(("，", "。")) for row in rows)


def test_codex_detail_rows_never_exceed_the_pane_width() -> None:
    rows = _render([
        {"type": "session_meta", "payload": {"cwd": "/very/long/" + "d" * 80}},
        _item({"type": "McpToolCall", "server": "slack", "tool": "read_thread",
               "arguments": {"channel_id": "C" * 80},
               "result": {"content": [{"type": "text", "text": "x" * 90}]}}),
        _item({"type": "CommandExecution", "command": "echo " + "y" * 90,
               "parsed_cmd": [{"type": "unknown"}], "aggregated_output": "z" * 90}),
    ], width=40)

    assert rows and all(len(row) <= 40 for row in rows)
    assert all(not row or row.startswith((" ", "•", ">")) for row in rows)
