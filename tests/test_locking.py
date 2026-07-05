"""Tests for file locking mechanism."""

from __future__ import annotations

import builtins
import multiprocessing
import os
import sys
import time
from pathlib import Path

import pytest

from claude_swap import locking as locking_mod
from claude_swap.exceptions import LockError
from claude_swap.locking import FileLock


class TestFileLock:
    """Test FileLock class."""

    def test_acquire_and_release(self, tmp_path: Path):
        """Test basic lock acquire and release."""
        lock_path = tmp_path / ".lock"
        lock = FileLock(lock_path)

        assert lock.acquire(timeout=1.0) is True
        assert lock._locked is True
        lock.release()
        assert lock._locked is False

    def test_context_manager(self, tmp_path: Path):
        """Test using lock as context manager."""
        lock_path = tmp_path / ".lock"

        with FileLock(lock_path) as lock:
            assert lock._locked is True

        assert lock._locked is False

    def test_context_manager_creates_parent_dirs(self, tmp_path: Path):
        """Test that lock creates parent directories."""
        lock_path = tmp_path / "nested" / "dir" / ".lock"

        with FileLock(lock_path):
            assert lock_path.parent.exists()

    def test_lock_timeout(self, tmp_path: Path):
        """Test that lock times out when already held."""
        lock_path = tmp_path / ".lock"

        # Acquire first lock
        lock1 = FileLock(lock_path)
        assert lock1.acquire(timeout=1.0) is True

        # Try to acquire second lock - should timeout
        lock2 = FileLock(lock_path)
        assert lock2.acquire(timeout=0.5) is False

        lock1.release()

    def test_lock_acquired_after_release(self, tmp_path: Path):
        """Test that lock can be acquired after previous holder releases."""
        lock_path = tmp_path / ".lock"

        lock1 = FileLock(lock_path)
        lock1.acquire(timeout=1.0)
        lock1.release()

        lock2 = FileLock(lock_path)
        assert lock2.acquire(timeout=1.0) is True
        lock2.release()

    def test_context_manager_raises_on_timeout(self, tmp_path: Path):
        """Test that context manager raises LockError on timeout."""
        lock_path = tmp_path / ".lock"

        # Hold the lock
        holder = FileLock(lock_path)
        holder.acquire(timeout=1.0)

        # Try to acquire with context manager
        with pytest.raises(LockError):
            # Create a lock with very short timeout
            lock = FileLock(lock_path)
            lock.acquire = lambda timeout=10.0: False  # Force failure
            with lock:
                pass

        holder.release()

    def test_double_release_safe(self, tmp_path: Path):
        """Test that releasing twice doesn't raise."""
        lock_path = tmp_path / ".lock"
        lock = FileLock(lock_path)

        lock.acquire(timeout=1.0)
        lock.release()
        lock.release()  # Should not raise

    def test_transient_open_failure_is_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A sharing violation from open() itself is retried, not raised.

        On Windows an antivirus/indexer can hold the lock file briefly and
        open() raises PermissionError; that must behave like a held lock
        (retry until timeout) instead of escaping acquire().
        """
        lock_path = tmp_path / ".lock"
        real_open = builtins.open
        failures = {"left": 2}

        def flaky_open(file, *args, **kwargs):
            if str(file) == str(lock_path) and failures["left"] > 0:
                failures["left"] -= 1
                raise PermissionError("sharing violation")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", flaky_open)

        lock = FileLock(lock_path)
        assert lock.acquire(timeout=5.0) is True
        assert failures["left"] == 0
        lock.release()

    def test_open_failure_until_timeout_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A persistent open() failure degrades to the normal timeout path."""
        lock_path = tmp_path / ".lock"
        real_open = builtins.open

        def denied_open(file, *args, **kwargs):
            if str(file) == str(lock_path):
                raise PermissionError("sharing violation")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", denied_open)

        lock = FileLock(lock_path)
        assert lock.acquire(timeout=0.3) is False
        assert lock._lock_file is None

    def test_acquire_does_not_truncate_existing_lock_file(self, tmp_path: Path):
        """Append mode keeps bytes another holder's handle may rely on."""
        lock_path = tmp_path / ".lock"
        lock_path.write_text("existing content")

        with FileLock(lock_path):
            if sys.platform == "win32":
                # msvcrt.locking denies even reads through a second handle
                # while held, so probe for truncation via metadata instead.
                assert lock_path.stat().st_size == len("existing content")
            else:
                assert lock_path.read_text() == "existing content"
        assert lock_path.read_text() == "existing content"

    def test_lock_error_carries_last_os_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A persistent failure must surface in the LockError message.

        A held lock and broken ACLs on the lock directory time out
        identically; without the last OS error the message misdiagnoses
        both as "another instance may be running".
        """
        lock_path = tmp_path / ".lock"
        real_open = builtins.open

        def denied_open(file, *args, **kwargs):
            if str(file) == str(lock_path):
                raise PermissionError("lock dir ACL broken")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", denied_open)

        with pytest.raises(LockError, match="lock dir ACL broken") as excinfo:
            with FileLock(lock_path, timeout=0.2):
                pass
        assert str(lock_path) in str(excinfo.value)

    def test_windows_lock_anchors_byte_at_file_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """msvcrt locks at the current position and "a" opens at EOF.

        The locked byte must be anchored at offset 0, or two processes
        opening a non-empty lock file at different sizes would lock
        different bytes and mutual exclusion would silently fail.
        """
        lock_path = tmp_path / ".lock"
        lock_path.write_text("stale debris")
        offsets: dict[int, int] = {}

        class FakeMsvcrt:
            LK_NBLCK = 2
            LK_UNLCK = 0

            @staticmethod
            def locking(fd: int, mode: int, nbytes: int) -> None:
                offsets[mode] = os.lseek(fd, 0, os.SEEK_CUR)

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(locking_mod, "msvcrt", FakeMsvcrt, raising=False)

        lock = FileLock(lock_path)
        assert lock.acquire(timeout=1.0) is True
        lock.release()
        assert offsets[FakeMsvcrt.LK_NBLCK] == 0


def _hold_lock_process(lock_path: str, duration: float, ready_event, done_event):
    """Helper function to hold a lock in a subprocess."""
    lock = FileLock(Path(lock_path))
    if lock.acquire(timeout=5.0):
        ready_event.set()  # Signal that lock is held
        time.sleep(duration)
        lock.release()
    done_event.set()


class TestFileLockConcurrency:
    """Test concurrent access to file locks."""

    def test_concurrent_access_blocked(self, tmp_path: Path):
        """Test that concurrent processes are blocked."""
        lock_path = tmp_path / ".lock"

        ready_event = multiprocessing.Event()
        done_event = multiprocessing.Event()

        # Start process that holds the lock
        p = multiprocessing.Process(
            target=_hold_lock_process,
            args=(str(lock_path), 2.0, ready_event, done_event),
        )
        p.start()

        # Wait for the subprocess to acquire the lock
        ready_event.wait(timeout=5.0)

        # Now try to acquire - should fail fast
        lock = FileLock(lock_path)
        result = lock.acquire(timeout=0.5)

        assert result is False

        # Clean up
        p.join(timeout=5.0)
        if p.is_alive():
            p.terminate()

    def test_lock_acquired_after_process_exits(self, tmp_path: Path):
        """Test that lock can be acquired after holding process exits."""
        lock_path = tmp_path / ".lock"

        ready_event = multiprocessing.Event()
        done_event = multiprocessing.Event()

        # Start process that holds the lock briefly
        p = multiprocessing.Process(
            target=_hold_lock_process,
            args=(str(lock_path), 0.5, ready_event, done_event),
        )
        p.start()

        # Wait for subprocess to finish
        done_event.wait(timeout=5.0)
        p.join(timeout=5.0)

        # Now we should be able to acquire
        lock = FileLock(lock_path)
        result = lock.acquire(timeout=1.0)

        assert result is True
        lock.release()
