import json

from railmux.winlocal.history_viewer import HistoryViewer, _bounded_tail


def test_history_viewer_renders_scrolls_and_closes(tmp_path):
    path = tmp_path / "session.jsonl"
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"line {index}"}],
            },
        }
        for index in range(40)
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    viewer = HistoryViewer(path, "codex", 80, 12)
    before = viewer.terminal.rows

    assert viewer.input(b"\x1b[5~")
    assert viewer.terminal.rows != before
    assert not viewer.input(b"q")


def test_history_viewer_does_not_close_on_pasted_q_substring(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text("", encoding="utf-8")
    viewer = HistoryViewer(path, "codex", 80, 12)

    assert viewer.input(b"request")


def test_history_tail_is_byte_and_record_bounded(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_bytes(b'{"old":true}\n' * 2100 + b'{"new":true}\n')

    lines = _bounded_tail(path).read().splitlines()

    assert len(lines) == 2000
    assert lines[-1] == '{"new":true}'
