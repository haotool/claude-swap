"""Tests for the shared cache helper."""

from __future__ import annotations

import json
import sys
import time

import pytest

from claude_swap.cache import MISSING, read_cache, write_cache


class TestReadCache:
    def test_returns_data_within_ttl(self, tmp_path):
        cache_file = tmp_path / "test.json"
        cache_file.write_text(json.dumps({
            "timestamp": time.time(),
            "data": {"key": "value"},
        }))

        result = read_cache(cache_file, ttl=60)
        assert result == {"key": "value"}

    def test_returns_missing_when_expired(self, tmp_path):
        cache_file = tmp_path / "test.json"
        cache_file.write_text(json.dumps({
            "timestamp": time.time() - 100,
            "data": {"key": "value"},
        }))

        result = read_cache(cache_file, ttl=60)
        assert result is MISSING

    def test_returns_missing_for_missing_file(self, tmp_path):
        result = read_cache(tmp_path / "nonexistent.json", ttl=60)
        assert result is MISSING

    def test_returns_missing_for_corrupt_json(self, tmp_path):
        cache_file = tmp_path / "test.json"
        cache_file.write_text("not valid json{{{")

        result = read_cache(cache_file, ttl=60)
        assert result is MISSING

    def test_cached_none_is_distinguishable_from_miss(self, tmp_path):
        cache_file = tmp_path / "test.json"
        cache_file.write_text(json.dumps({
            "timestamp": time.time(),
            "data": None,
        }))

        result = read_cache(cache_file, ttl=60)
        assert result is None
        assert result is not MISSING


class TestWriteCache:
    def test_creates_file_and_parent_dirs(self, tmp_path):
        cache_file = tmp_path / "sub" / "dir" / "test.json"
        write_cache(cache_file, {"key": "value"})

        assert cache_file.exists()
        raw = json.loads(cache_file.read_text())
        assert raw["data"] == {"key": "value"}
        assert "timestamp" in raw

    def test_roundtrip(self, tmp_path):
        cache_file = tmp_path / "test.json"
        data = {"accounts": [1, 2, 3], "nested": {"a": True}}

        write_cache(cache_file, data)
        result = read_cache(cache_file, ttl=60)

        assert result == data

    def test_atomic_replace_swaps_inode_on_posix(self, tmp_path):
        cache_file = tmp_path / "test.json"
        write_cache(cache_file, {"v": 1})
        if sys.platform == "win32":
            # os.replace is atomic on Windows too but inode is not exposed.
            write_cache(cache_file, {"v": 2})
            assert read_cache(cache_file, ttl=60) == {"v": 2}
            return
        first_inode = cache_file.stat().st_ino
        write_cache(cache_file, {"v": 2})
        assert read_cache(cache_file, ttl=60) == {"v": 2}
        assert cache_file.stat().st_ino != first_inode

    def test_no_tmp_files_left_on_success(self, tmp_path):
        cache_file = tmp_path / "test.json"
        write_cache(cache_file, {"v": 1})
        assert list(tmp_path.glob("*.tmp")) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode only")
    def test_sets_mode_0600(self, tmp_path):
        cache_file = tmp_path / "test.json"
        write_cache(cache_file, {"v": 1})

        assert (cache_file.stat().st_mode & 0o777) == 0o600

    def test_cleans_tmp_on_error(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "test.json"

        def boom(*_args, **_kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr("claude_swap.cache.os.replace", boom)
        with pytest.raises(OSError, match="simulated replace failure"):
            write_cache(cache_file, {"v": 1})
        assert list(tmp_path.glob("*.tmp")) == []
