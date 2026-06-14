# Plan 010: Inline `_resolve_slot_cached_at` into its single caller

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 331798e..HEAD -- src/claude_swap/switcher.py`
> If `switcher.py` changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tech-debt
- **Planned at**: commit `331798e`, 2026-06-14

## Why this matters

`_resolve_slot_cached_at` is a 9-line helper in `switcher.py` with exactly
one caller (`_usage_slot_trusted`). The indirection adds a call hop without
adding clarity — its logic is two trivial conditionals. Inlining removes
one module-level symbol from the cache-helper cluster and makes the TTL
check obvious at the only place it is used.

This is the kind of clean-code-friction tax that compounds across a file
that has already grown to 3104 LOC. Removing single-call helpers is the
cheapest path to make the rest of the file shorter without changing behavior.

## Current state

`src/claude_swap/switcher.py:331-356` — two helpers, second is the only
caller of the first:

```python
def _resolve_slot_cached_at(entry: dict, file_timestamp: float | None) -> float | None:
    """Resolve when a cache row was last known-good.

    Legacy rows without ``_cached_at`` inherit the wrapper file timestamp so
    pre-007 caches remain trusted until the file TTL expires.
    """
    if not isinstance(entry, dict):
        return None
    cached_at = entry.get("_cached_at")
    if isinstance(cached_at, (int, float)) and float(cached_at) > 0:
        return float(cached_at)
    if file_timestamp is not None and file_timestamp > 0:
        return file_timestamp
    return None


def _usage_slot_trusted(
    entry: dict,
    now: float,
    file_timestamp: float | None = None,
) -> bool:
    """True when a single usage cache row is within the per-slot TTL."""
    cached_at = _resolve_slot_cached_at(entry, file_timestamp)
    if cached_at is None:
        return False
    return now - cached_at < _USAGE_CACHE_TTL
```

Verification of single-caller status:

```bash
grep -n "_resolve_slot_cached_at" src/claude_swap/switcher.py
```

Must show exactly the definition (line 331) and one call (line 353).
If grep finds more callers, STOP — this plan's assumption is wrong.

## Commands you will need

| Purpose       | Command                                      | Expected on success     |
|---------------|----------------------------------------------|-------------------------|
| Drift check   | `git diff --stat 331798e..HEAD -- src/claude_swap/switcher.py` | clean or no changes to ll. 331-356 |
| Caller check  | `grep -n "_resolve_slot_cached_at" src/`     | exactly 2 hits (def + 1 call) |
| Tests         | `python -m pytest -q`                        | 652 passed, 3 skipped   |
| Targeted tests| `python -m pytest -q tests/test_switcher.py tests/test_auto_switch.py` | all pass |

## Scope

**In scope** (the only files you should modify):

- `src/claude_swap/switcher.py`

**Out of scope** (do NOT touch):

- Any other cache helper (`_usage_error_to_cache`, `_usage_from_cache`,
  `_usage_to_cache`, `_is_usage_dict`, `_merge_usage_with_previous`,
  `_persist_usage_cache_entry`, `_usage_slot_trusted`).
- Test files — behavior is unchanged.
- `_USAGE_CACHE_TTL` constant.

## Git workflow

- Branch: stay on `feat/auto-switch-on-limit` (no new branch needed for a
  single-symbol cleanup).
- Single commit; message style: conventional commits with scope, lowercase
  body. Example from repo history: `refactor(cache): extract read_cache_with_timestamp helper`.
  Suggested: `refactor(switcher): inline _resolve_slot_cached_at into sole caller`
- Do NOT push.

## Steps

### Step 1: Delete `_resolve_slot_cached_at` and inline its logic into `_usage_slot_trusted`

Replace lines 331-356 of `src/claude_swap/switcher.py` with:

```python
def _usage_slot_trusted(
    entry: dict,
    now: float,
    file_timestamp: float | None = None,
) -> bool:
    """True when a single usage cache row is within the per-slot TTL.

    Legacy rows without ``_cached_at`` inherit the wrapper file timestamp so
    pre-007 caches remain trusted until the file TTL expires.
    """
    if not isinstance(entry, dict):
        return False
    cached_at = entry.get("_cached_at")
    if isinstance(cached_at, (int, float)) and float(cached_at) > 0:
        resolved = float(cached_at)
    elif file_timestamp is not None and file_timestamp > 0:
        resolved = file_timestamp
    else:
        return False
    return now - resolved < _USAGE_CACHE_TTL
```

Move the legacy-row docstring sentence onto `_usage_slot_trusted` (it
describes the same behavior; do not lose it).

**Verify**:

```bash
grep -n "_resolve_slot_cached_at" src/
```

Expected: **no output** (zero hits).

### Step 2: Run the focused tests

```bash
python -m pytest -q tests/test_switcher.py tests/test_auto_switch.py
```

Expected: all pass (counts unchanged).

### Step 3: Run the full suite

```bash
python -m pytest -q
```

Expected: **652 passed, 3 skipped, 0 failed** (same as baseline at commit `331798e`).

## Test plan

No new tests. Behavior is identical — the existing `_usage_slot_trusted`
test coverage (called from `tests/test_switcher.py` and `tests/test_auto_switch.py`
via the cache-trust paths) is the regression guard.

If `python -m pytest -q tests/test_switcher.py -k "trusted" -v` does not
list at least one test name containing `trusted` or `cache_fresh`, STOP and
report — there may be a coverage gap this plan can't safely land on top of.

## Done criteria

- [ ] `grep -n "_resolve_slot_cached_at" src/` returns no matches.
- [ ] `python -m pytest -q` reports 652 passed, 3 skipped.
- [ ] `git diff --stat` shows exactly one file changed: `src/claude_swap/switcher.py`.
- [ ] Net line delta: `-15` to `-12` lines (one helper removed, one helper grows by ~4 lines).
- [ ] `plans/README.md` status row for plan 010 updated to DONE with the commit SHA.

## STOP conditions

Stop and report (do not improvise) if:

- `grep` shows more than one caller of `_resolve_slot_cached_at` — the
  single-caller premise of this plan is false; the helper is shared and
  should not be inlined without re-planning.
- Any test fails after the change — restore the original code and report
  the failing test name and traceback before retrying.
- The "Current state" excerpt doesn't match the live code at lines 331-356
  — the file has drifted since planning; re-audit before editing.

## Maintenance notes

- After landing, the cache-helper cluster has one fewer symbol but is
  otherwise unchanged. Future cache-trust adjustments edit
  `_usage_slot_trusted` directly.
- If a second caller for the legacy-timestamp resolution logic appears
  later, re-extract a helper at that point — premature re-extraction is
  the reason this one is being inlined.
