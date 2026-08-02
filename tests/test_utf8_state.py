"""UTF-8 persistence must not inherit the process locale."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_utf8_user_state_survives_a_non_utf8_process_locale(tmp_path):
    config_root = tmp_path / ".config" / "railmux"
    config_root.mkdir(parents=True)
    (config_root / "config.toml").write_bytes(
        '# 中文注释\n[updates]\nauto_update = "never"\n'.encode("utf-8")
    )
    (config_root / "favorites.json").write_bytes(
        json.dumps(["会话"], ensure_ascii=False).encode("utf-8")
    )
    (config_root / "renames.json").write_bytes(
        json.dumps({"会话": "中文标题"}, ensure_ascii=False).encode("utf-8")
    )
    (config_root / "path-cache.json").write_bytes(
        json.dumps(
            {"编码目录": str(tmp_path / "项目")}, ensure_ascii=False
        ).encode("utf-8")
    )
    project_dir = tmp_path / ".claude" / "projects" / "encoded"
    project_dir.mkdir(parents=True)
    session_id = "11111111-1111-1111-1111-111111111111"
    records = (
        {"type": "ai-title", "aiTitle": "会话标题"},
        {"type": "user", "message": {"content": "中文问题"}},
        {
            "type": "assistant",
            "message": {
                "content": "中文回答",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    )
    (project_dir / f"{session_id}.jsonl").write_bytes(
        (
            "\n".join(
                json.dumps(row, ensure_ascii=False) for row in records
            )
            + "\n"
        ).encode("utf-8")
    )
    history = tmp_path / ".claude" / "history.jsonl"
    history.write_bytes(
        (
            json.dumps(
                {"sessionId": "remove", "display": "删除我"},
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {"sessionId": "keep", "display": "保留我"},
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    )
    script = r"""
import json
import locale
import sys
from pathlib import Path
from railmux.discovery import _load_path_cache
from railmux.favorites import Favorites
from railmux.models import Project
from railmux.renames import Renames
from railmux.session_index import _scan_session
from railmux.settings import Settings
from railmux.ui.app import App

home = Path(sys.argv[1])
assert locale.getpreferredencoding(False).upper() not in {'UTF-8', 'UTF8'}
assert Settings().update_policy == 'never'
assert Favorites().is_favorite('\u4f1a\u8bdd')
assert Renames().get('\u4f1a\u8bdd') == '\u4e2d\u6587\u6807\u9898'
assert _load_path_cache()['\u7f16\u7801\u76ee\u5f55'].endswith('\u9879\u76ee')
project = Project(home / '\u9879\u76ee', 'encoded', home / '.claude' / 'projects' / 'encoded', 1, 0)
session = _scan_session(project, project.claude_dir / '11111111-1111-1111-1111-111111111111.jsonl')
assert session is not None and session.title == '\u4f1a\u8bdd\u6807\u9898'
assert App._remove_from_history('remove', claude_home=home / '.claude')
rows = [json.loads(line) for line in (home / '.claude' / 'history.jsonl').read_bytes().decode('utf-8').splitlines()]
assert rows == [{'sessionId': 'keep', 'display': '\u4fdd\u7559\u6211'}]
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "LC_ALL": "C",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONUTF8": "0",
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
