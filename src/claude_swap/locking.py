"""File locking for concurrent access protection."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import IO

# Platform-specific imports for file locking
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from claude_swap.exceptions import LockError


class FileLock:
    """Cross-process file lock using platform-specific APIs."""

    def __init__(self, lock_path: Path, timeout: float = 10.0):
        self.lock_path = lock_path
        self.timeout = timeout
        self._lock_file: IO[str] | None = None
        self._locked = False
        self._last_error: OSError | None = None

    def acquire(self, timeout: float | None = None) -> bool:
        """Acquire exclusive lock with timeout.

        Args:
            timeout: Maximum seconds to wait for lock. Defaults to the
                timeout given at construction.

        Returns:
            True if lock acquired, False if timeout.
        """
        if timeout is None:
            timeout = self.timeout
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        start = time.monotonic()
        while True:
            try:
                # Opened inside the retry loop: on Windows a transient
                # sharing violation (antivirus/indexer holding the file)
                # raises PermissionError from open() itself, which must be
                # retried like a held lock instead of escaping. "a" avoids
                # truncating a file another handle may hold a lock on.
                if self._lock_file is None:
                    self._lock_file = open(self.lock_path, "a")
                if sys.platform == "win32":
                    # Windows: use msvcrt for file locking. It locks a byte
                    # range at the current file position, and "a" opens at
                    # EOF — anchor at offset 0 so every process contends on
                    # the same byte even if the lock file is not empty.
                    self._lock_file.seek(0)
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    # POSIX: use fcntl for file locking
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked = True
                return True
            except (BlockingIOError, OSError) as exc:
                self._last_error = exc
                if time.monotonic() - start > timeout:
                    if self._lock_file is not None:
                        self._lock_file.close()
                        self._lock_file = None
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        """Release the lock."""
        if self._lock_file and self._locked:
            if sys.platform == "win32":
                # Windows: unlock using msvcrt
                try:
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass  # File may already be unlocked
            else:
                # POSIX: unlock using fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
            self._locked = False

    def __enter__(self) -> FileLock:
        if not self.acquire():
            # Carry the last OS error: a held lock and a persistent failure
            # (e.g. broken ACLs on the lock directory) time out identically,
            # and only the errno tells them apart.
            detail = (
                f" (last error: {self._last_error!r})" if self._last_error else ""
            )
            raise LockError(
                f"Failed to acquire lock - another instance may be running{detail}"
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.release()
