# Plan 014: Make `cache.write_cache` atomic with mode 0o600

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 331798e..HEAD -- src/claude_swap/cache.py`
> If `cache.py` changed since this plan was written, re-read it and
> compare to the "Current state" excerpt before editing; on material
> drift, STOP.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug (silent cache loss on crash) + security (file mode)
- **Planned at**: commit `331798e`, 2026-06-14

## Why this matters

`cache.write_cache` writes JSON with `path.write_text(...)`. Two issues:

1. **Non-atomic write.** A crash, SIGKILL, OOM, or power loss between
   `open(path, "w")` and `close()` leaves a half-written file. The
   readers (`read_cache`, `read_cache_data`, `read_cache_with_timestamp`)
   all swallow `json.JSONDecodeError` and return the "no cache"
   sentinel — so the corruption is **silent**. For the usage cache,
   that means the next monitor poll silently sees a cold cache and
   falls into the fail-closed path (`no_trusted_signal`) until a manual
   `cswap --list` warm-up.
2. **No explicit mode.** The file lands with the process umask,
   typically `0o644` on Linux — world-readable. The cache stores usage
   percentages, reset timestamps, and occasionally `usage_fetch_error`
   metadata. Not credentials, but more than the codebase's own
   convention for everything else: the credential write at
   `switcher.py:543-552` uses `tempfile.mkstemp` + `os.replace` + an
   explicit `os.chmod(..., 0o600)`.

This plan aligns `write_cache` with the credential-write pattern: atomic
swap via `os.replace`, mode `0o600` on POSIX, no mode change on Windows.
That guarantees readers see either the previous good file or the new
good file — never half a file — and removes the world-readability of the
new file.

## Current state

`src/claude_swap/cache.py:73-79` (the function to fix):

```python
def write_cache(path: Path, data) -> None:
    """Write data to a cache file with a timestamp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"timestamp": time.time(), "data": data}),
        encoding="utf-8",
    )
```

Exemplar atomic write (mode 0o600 + replace), from
`src/claude_swap/switcher.py:540-559` (`_write_credentials`):

```python
cred_dir = get_claude_config_home()
cred_dir.mkdir(parents=True, exist_ok=True)
cred_file = cred_dir / ".credentials.json"
try:
    fd, tmp_path = tempfile.mkstemp(dir=str(cred_dir), suffix=".tmp")
    try:
        os.write(fd, credentials.encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(cred_file))
        if sys.platform != "win32":
            os.chmod(str(cred_file), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
```

Match this shape exactly — the tmp-file in the same directory, the
`os.replace`, the `chmod` only on non-Windows, the `BaseException`
cleanup. Do not invent a new style.

All current callers of `write_cache`:

```bash
grep -n "write_cache" src/claude_swap/*.py
```

Expected hits (5 call sites, all already trusted to handle exceptions):

- `src/claude_swap/cache.py:73` — definition
- `src/claude_swap/switcher.py:31` — import
- `src/claude_swap/switcher.py:1074, 2069, 2223, 2361` — usage cache writes
- `src/claude_swap/update_check.py:12` — import
- `src/claude_swap/update_check.py:66` — version cache write

All callers treat write failures as best-effort (the cache is
non-authoritative); raising from `write_cache` on a real I/O error is
preserved behavior.

## Commands you will need

| Purpose         | Command                                              | Expected on success |
|-----------------|------------------------------------------------------|---------------------|
| Drift check     | `git diff --stat 331798e..HEAD -- src/claude_swap/cache.py` | inspect for unexpected changes |
| Caller audit    | `grep -n "write_cache" src/claude_swap/*.py`         | all 7 sites match above |
| New test        | `python -m pytest -q tests/test_cache.py`            | new file passes |
| Targeted tests  | `python -m pytest -q tests/test_switcher.py tests/test_auto_switch.py` | all pass |
| Full suite      | `python -m pytest -q`                                | 654 passed, 3 skipped (was 652) |

## Scope

**In scope**:

- `src/claude_swap/cache.py` — rewrite `write_cache`.
- `tests/test_cache.py` — **create** this file with the regression tests
  (see Test plan). If it already exists, append to it.

**Out of scope** (do NOT touch):

- Any other function in `cache.py` (readers stay as-is — they already
  tolerate corruption; this plan removes the *source* of corruption, not
  the defensive reads).
- Any caller of `write_cache` — the function's contract (raises on real
  failure, otherwise returns None) is unchanged.
- `switcher.py:_write_credentials` — already correct; do not refactor
  it to share code with `write_cache`. Inline the pattern instead.
  (Sharing a helper between two trust domains is a larger refactor that
  belongs in its own plan if it's worth doing at all.)

## Git workflow

- Branch: stay on `feat/auto-switch-on-limit`.
- One commit recommended. Suggested message:
  `fix(cache): atomic write with mode 0o600 to avoid silent corruption`
- Do NOT push.

## Steps

### Step 1: Rewrite `write_cache` in `src/claude_swap/cache.py`

Add the necessary imports at the top of the file if missing:

```python
import os
import sys
import tempfile
```

Replace `write_cache` (current lines 73-79) with:

```python
def write_cache(path: Path, data) -> None:
    """Write data to a cache file with a timestamp.

    Atomic: writes to a same-directory temp file, fsync-free ``os.replace``
    swaps it into place, then chmods to 0o600 on POSIX so the cache is not
    world-readable. Readers tolerate a missing/corrupt file by returning
    the default — this function eliminates the *source* of corruption so
    that path is exercised only for genuinely-absent caches.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"timestamp": time.time(), "data": data})
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, payload.encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(path))
        if sys.platform != "win32":
            os.chmod(str(path), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
```

**Verify**:

```bash
python -c "from claude_swap.cache import write_cache; print('ok')"
```

Expected: `ok` (no ImportError).

### Step 2: Confirm the readers still tolerate the new payload shape

The payload format is unchanged (`{"timestamp": ..., "data": ...}`), so
no reader change is needed. Confirm by a smoke read/write round-trip in a
Python one-liner using `tmp` (do not modify any production file):

```bash
python -c "
import json, time
from pathlib import Path
from claude_swap.cache import write_cache, read_cache_data, read_cache_with_timestamp
p = Path('/tmp/cswap_plan014_smoke.json')
write_cache(p, {'x': 1})
assert read_cache_data(p) == {'x': 1}
data, ts = read_cache_with_timestamp(p)
assert data == {'x': 1} and isinstance(ts, float)
print('ok')
p.unlink()
"
```

Expected: `ok`. Clean up if the script aborts before `unlink`.

### Step 3: Add the regression tests in `tests/test_cache.py`

Check whether the file exists:

```bash
ls tests/test_cache.py 2>/dev/null && echo exists || echo new
```

If new, create it with the imports the rest of the test suite uses
(`from claude_swap.cache import ...`, `from pathlib import Path`,
`import pytest`). If it already exists, append.

Add these tests (model after the existing `tests/test_oauth.py` or
`tests/test_switcher.py` for fixture style — plain `def test_*()`
functions, `tmp_path` fixture for filesystem isolation):

```python
import json
import os
import sys
from pathlib import Path

import pytest

from claude_swap.cache import (
    read_cache_data,
    read_cache_with_timestamp,
    write_cache,
)


def test_write_cache_round_trip(tmp_path: Path):
    p = tmp_path / "cache.json"
    write_cache(p, {"x": 1, "y": "z"})
    assert read_cache_data(p) == {"x": 1, "y": "z"}
    data, ts = read_cache_with_timestamp(p)
    assert data == {"x": 1, "y": "z"}
    assert isinstance(ts, float) and ts > 0


def test_write_cache_is_atomic_replace(tmp_path: Path):
    p = tmp_path / "cache.json"
    # Seed an existing file with a known-good payload.
    write_cache(p, {"v": 1})
    first_inode_or_id = p.stat().st_ino if sys.platform != "win32" else None

    write_cache(p, {"v": 2})
    assert read_cache_data(p) == {"v": 2}
    # On POSIX, os.replace gives the swapped file a new inode (the temp
    # file's). On Windows we cannot assert inode change, but the content
    # check above is sufficient to prove a non-destructive swap.
    if first_inode_or_id is not None:
        assert p.stat().st_ino != first_inode_or_id


def test_write_cache_no_tmp_files_left_on_success(tmp_path: Path):
    p = tmp_path / "cache.json"
    write_cache(p, {"v": 1})
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode only")
def test_write_cache_sets_mode_0600(tmp_path: Path):
    p = tmp_path / "cache.json"
    write_cache(p, {"v": 1})
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_cache_cleans_tmp_on_error(tmp_path: Path, monkeypatch):
    """A failure during os.replace must not leave a stray .tmp file."""
    p = tmp_path / "cache.json"

    def boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("claude_swap.cache.os.replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        write_cache(p, {"v": 1})
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
```

Five tests cover round-trip, atomic swap (POSIX), success-path tmp
cleanup, mode 0o600 on POSIX, and error-path tmp cleanup.

### Step 4: Run the new tests, then targeted, then full suite

```bash
python -m pytest -q tests/test_cache.py
python -m pytest -q tests/test_switcher.py tests/test_auto_switch.py tests/test_oauth.py
python -m pytest -q
```

Expected:

- `tests/test_cache.py`: 5 passed (or 4 if Windows — `mode_0600` skips).
- targeted: pass counts unchanged.
- full: **654 passed, 3 skipped** (2 over the 652 baseline on macOS/Linux;
  653 on Windows since mode test skips).

## Test plan

Per Step 3: 5 new tests in `tests/test_cache.py` covering the atomic
swap, mode 0o600, success-path cleanup, error-path cleanup, and the
unchanged read/write contract. Model is `tmp_path`-based plain pytest
functions, matching the rest of the suite.

## Done criteria

- [ ] `tests/test_cache.py` exists with at least the 5 tests above.
- [ ] `python -m pytest -q` reports **654 passed, 3 skipped** on
      macOS/Linux (one fewer on Windows where the mode test skips).
- [ ] `cache.write_cache` uses `tempfile.mkstemp` + `os.replace` + (POSIX)
      `os.chmod 0o600`. No remaining `path.write_text` in that function.
- [ ] `git diff --stat` shows exactly two files changed:
      `src/claude_swap/cache.py`, `tests/test_cache.py`.
- [ ] No production caller of `write_cache` modified.
- [ ] `plans/README.md` status row for plan 014 updated to DONE.

## STOP conditions

Stop and report if:

- A caller of `write_cache` already implements its own atomic wrapper
  (search: `grep -B2 -A5 "write_cache(" src/claude_swap/*.py`) — the
  caller's wrapper might shadow the new behavior; consult before
  editing.
- `tests/test_cache.py` already exists with conflicting fixtures or
  names — STOP and report so the new tests are placed correctly.
- The smoke round-trip in Step 2 fails — the readers may have stricter
  payload expectations than the existing format; do not change reader
  code from this plan.
- On Windows-only platforms, `os.replace` raises `PermissionError` for
  open destinations — verify on the host before raising the alarm; the
  test suite is the canonical signal.

## Maintenance notes

- If a future feature needs to share the atomic-write pattern with
  another module (e.g. session-profile manifests), extract a helper to
  `src/claude_swap/locking.py` or a new `atomic.py` and have both call
  sites use it. Do not extract from a single caller.
- The cache files now have mode `0o600` from this commit forward. Old
  files written by previous versions retain the umask-derived mode
  (typically `0o644`) until the next write. This is acceptable — the
  cache is non-authoritative, and the next monitor poll will rewrite it
  within the TTL.
- A reviewer should check that `tempfile.mkstemp(dir=str(path.parent), ...)`
  keeps the tmp file on the same filesystem as the final path (required
  for `os.replace` to be atomic on POSIX). The code as written satisfies
  this.
