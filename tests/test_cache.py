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
        cache_file.write_text(
            json.dumps(
                {
                    "timestamp": time.time(),
                    "data": {"key": "value"},
                }
            )
        )

        result = read_cache(cache_file, ttl=60)
        assert result == {"key": "value"}

    def test_returns_missing_when_expired(self, tmp_path):
        cache_file = tmp_path / "test.json"
        cache_file.write_text(
            json.dumps(
                {
                    "timestamp": time.time() - 100,
                    "data": {"key": "value"},
                }
            )
        )

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
        cache_file.write_text(
            json.dumps(
                {
                    "timestamp": time.time(),
                    "data": None,
                }
            )
        )

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


class TestUsageSlotTrusted:
    def test_legacy_row_without_cached_at_is_untrusted(self):
        from claude_swap.usage_cache import _usage_slot_trusted

        legacy = {"five_hour": {"pct": 10.0}}
        now = time.time()
        assert _usage_slot_trusted(legacy, now) is False

    def test_stamped_row_trusted_within_ttl(self):
        from claude_swap.usage_cache import _usage_slot_trusted

        now = time.time()
        entry = {"five_hour": {"pct": 10.0}, "_cached_at": now - 5}
        assert _usage_slot_trusted(entry, now) is True

    def test_unrelated_file_write_does_not_retrust_legacy_row(self):
        from claude_swap.usage_cache import _usage_error_to_cache, _usage_slot_trusted
        from claude_swap import oauth

        now = time.time()
        legacy = {"five_hour": {"pct": 10.0}}
        assert _usage_slot_trusted(legacy, now) is False

        fresh_error = _usage_error_to_cache(
            oauth.UsageFetchError(reason="network_error")
        )
        assert isinstance(fresh_error.get("_cached_at"), float)
        assert _usage_slot_trusted(fresh_error, now) is True
        assert _usage_slot_trusted(legacy, now) is False


class TestPersistRetryAfter:
    def test_rate_limit_retry_after_preserved_on_trusted_row(self):
        from claude_swap import oauth
        from claude_swap.usage_cache import _persist_usage_cache_entry

        now = time.time()
        prev_trusted = {"five_hour": {"pct": 80.0}, "_cached_at": now - 3}
        existing: dict = {}
        error = oauth.UsageFetchError(reason="rate_limited", retry_after=42)

        _persist_usage_cache_entry(existing, "1", error, prev_trusted)

        row = existing["1"]
        # Trusted usage is still shown...
        assert row["five_hour"]["pct"] == 80.0
        # ...but the server Retry-After is not dropped.
        assert row["_last_rate_limit"]["retry_after"] == 42

    def test_non_rate_limit_error_does_not_stamp_retry_after(self):
        from claude_swap import oauth
        from claude_swap.usage_cache import _persist_usage_cache_entry

        now = time.time()
        prev_trusted = {"five_hour": {"pct": 80.0}, "_cached_at": now - 3}
        existing: dict = {}
        error = oauth.UsageFetchError(reason="network_error")

        _persist_usage_cache_entry(existing, "1", error, prev_trusted)

        assert "_last_rate_limit" not in existing["1"]

    def test_stale_retry_after_cleared_on_later_non_rate_limit_error(self):
        # A 429 stamps _last_rate_limit; a subsequent non-429 failure that still
        # carries a trusted row must NOT preserve the old Retry-After, or the
        # monitor would back off on a window that no longer applies.
        from claude_swap import oauth
        from claude_swap.usage_cache import _persist_usage_cache_entry

        now = time.time()
        carried = {
            "five_hour": {"pct": 80.0},
            "_cached_at": now - 3,
            "_last_rate_limit": {"retry_after": 42, "at": now - 3},
        }
        existing: dict = {}
        error = oauth.UsageFetchError(reason="network_error")

        _persist_usage_cache_entry(existing, "1", error, carried)

        assert "_last_rate_limit" not in existing["1"]

    def test_rate_limited_without_retry_after_clears_stale_side_field(self):
        # A 429 carrying no Retry-After header must still drop a prior stamp.
        from claude_swap import oauth
        from claude_swap.usage_cache import _persist_usage_cache_entry

        now = time.time()
        carried = {
            "five_hour": {"pct": 80.0},
            "_cached_at": now - 3,
            "_last_rate_limit": {"retry_after": 42, "at": now - 3},
        }
        existing: dict = {}
        error = oauth.UsageFetchError(reason="rate_limited", retry_after=None)

        _persist_usage_cache_entry(existing, "1", error, carried)

        assert "_last_rate_limit" not in existing["1"]

    def test_persist_stamps_at_timestamp(self):
        from claude_swap import oauth
        from claude_swap.usage_cache import _persist_usage_cache_entry

        now = time.time()
        prev_trusted = {"five_hour": {"pct": 80.0}, "_cached_at": now - 3}
        existing: dict = {}
        error = oauth.UsageFetchError(reason="rate_limited", retry_after=42)

        _persist_usage_cache_entry(existing, "1", error, prev_trusted)

        stamp = existing["1"]["_last_rate_limit"]
        assert stamp["retry_after"] == 42
        assert isinstance(stamp["at"], (int, float)) and stamp["at"] > 0

    def test_extract_retry_after_decays_with_elapsed_time(self):
        from claude_swap.usage_cache import extract_retry_after

        row = {"_last_rate_limit": {"retry_after": "90", "at": 1_000.0}}
        assert extract_retry_after(row, 1_010.0) == 80
        assert extract_retry_after(row, 1_100.0) is None
        assert extract_retry_after({"five_hour": {"pct": 1}}, 1_010.0) is None

    def test_extract_retry_after_fails_closed_on_invalid_at(self):
        # Missing or non-positive 'at' means a corrupt/legacy row — fail closed
        # rather than return an undecayed (stale) backoff.
        from claude_swap.usage_cache import extract_retry_after

        assert (
            extract_retry_after({"_last_rate_limit": {"retry_after": "90"}}, 5.0)
            is None
        )
        assert (
            extract_retry_after(
                {"_last_rate_limit": {"retry_after": "90", "at": 0}},
                5.0,
            )
            is None
        )
        assert (
            extract_retry_after(
                {"_last_rate_limit": {"retry_after": "90", "at": "bad"}},
                5.0,
            )
            is None
        )
