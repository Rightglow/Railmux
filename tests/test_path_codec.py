from pathlib import Path

import pytest

from ccmgr.path_codec import encode, decode


def test_encode_simple():
    assert encode(Path("/home/user/project")) == "-home-user-project"


def test_encode_preserves_dashes_in_segments():
    assert encode(Path("/home/user/claude-chat")) == "-home-user-claude-chat"


def test_encode_trailing_slash_stripped():
    assert encode(Path("/home/user/project/")) == "-home-user-project"


def test_decode_unambiguous_with_filesystem(tmp_path):
    real = tmp_path / "foo" / "bar"
    real.mkdir(parents=True)
    encoded = encode(real)
    assert decode(encoded) == real


def test_decode_with_dashes_in_segment(tmp_path):
    real = tmp_path / "claude-chat"
    real.mkdir(parents=True)
    encoded = encode(real)
    assert decode(encoded) == real


def test_decode_nonexistent_splits_every_dash(tmp_path, monkeypatch):
    # Pick a token unlikely to exist as a real top-level dir.
    encoded = "-zzz-foo-bar"
    result = decode(encoded)
    assert result == Path("/zzz/foo/bar"), result


def test_decode_with_dashes_in_intermediate_segment(tmp_path):
    real = tmp_path / "claude-chat" / "src"
    real.mkdir(parents=True)
    encoded = encode(real)
    assert decode(encoded) == real


def test_decode_two_dashed_segments(tmp_path):
    real = tmp_path / "foo-bar" / "baz-qux"
    real.mkdir(parents=True)
    encoded = encode(real)
    assert decode(encoded) == real


def test_decode_non_ascii_recovery(tmp_path):
    """Chinese characters replaced by dashes during Claude encoding are recovered."""
    real = tmp_path / "项目" / "src"
    real.mkdir(parents=True)
    # Claude's encoding replaces non-ASCII chars with dashes.
    # Simulate what Claude would write to ~/.claude/projects/.
    from ccmgr.path_codec import _claude_encode_path
    encoded = _claude_encode_path(str(real))
    # encoded should contain more dashes than the ASCII-only fallback.
    assert decode(encoded) == real


def test_decode_non_ascii_nested(tmp_path):
    """Recovery works through multiple levels of non-ASCII directories."""
    real = tmp_path / "数据" / "无尽夏" / "CatWork"
    real.mkdir(parents=True)
    from ccmgr.path_codec import _claude_encode_path
    encoded = _claude_encode_path(str(real))
    assert decode(encoded) == real


def test_decode_non_ascii_fallback_when_no_match(tmp_path):
    """When filesystem scan finds nothing, return the best-guess path without crashing."""
    from ccmgr.path_codec import _claude_encode_path
    encoded = _claude_encode_path("/zzz/你好/世界")
    result = decode(encoded)
    # Falls back to best-guess from backtracking (all dashes as separators).
    assert result is not None
    assert not result.exists()  # path doesn't exist, but decode didn't crash
