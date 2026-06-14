# Plan 011: Remove `# ----` banner divider comments from branch-added code

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 331798e..HEAD -- src/claude_swap/migrations.py src/claude_swap/monitor.py src/claude_swap/tui.py`
> If any of these three files changed since this plan was written, list the
> banner-comment lines in the live code with the grep in Step 1 and confirm
> the count before editing; if the file has drifted materially, STOP.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tech-debt (style consistency)
- **Planned at**: commit `331798e`, 2026-06-14

## Why this matters

The upstream author (`realiti4`) writes **zero** `# ----...` banner-divider
comments anywhere in `src/claude_swap/` at the merge-base (`6752b56`).
Three branch-added/modified files now contain **18** such dividers
introduced by feature work on this branch. They add noise without adding
information that section names in code or natural file structure don't
already convey, and they make the file's actual line count harder to scan.

Removing them is a one-shot, zero-risk style alignment that brings the
branch closer to the author's own conventions before merge upstream.

## Current state

Run this to confirm the current count and locations:

```bash
grep -n "^# -\{4,\}" src/claude_swap/*.py
```

Expected output **before** this plan runs (18 hits across 3 files):

```
src/claude_swap/migrations.py:46:# ---------------------------------------------------------------------------
src/claude_swap/migrations.py:48:# ---------------------------------------------------------------------------
src/claude_swap/migrations.py:103:# ---------------------------------------------------------------------------
src/claude_swap/migrations.py:105:# ---------------------------------------------------------------------------
src/claude_swap/migrations.py:455:# ---------------------------------------------------------------------------
src/claude_swap/migrations.py:457:# ---------------------------------------------------------------------------
src/claude_swap/monitor.py:56:# ----------------------------------------------------------------------------
src/claude_swap/monitor.py:58:# ----------------------------------------------------------------------------
src/claude_swap/tui.py:55:# ---------------------------------------------------------------------------
src/claude_swap/tui.py:57:# ---------------------------------------------------------------------------
src/claude_swap/tui.py:127:# ---------------------------------------------------------------------------
src/claude_swap/tui.py:129:# ---------------------------------------------------------------------------
src/claude_swap/tui.py:202:# ---------------------------------------------------------------------------
src/claude_swap/tui.py:204:# ---------------------------------------------------------------------------
src/claude_swap/tui.py:413:# ---------------------------------------------------------------------------
src/claude_swap/tui.py:415:# ---------------------------------------------------------------------------
src/claude_swap/tui.py:485:# ---------------------------------------------------------------------------
src/claude_swap/tui.py:487:# ---------------------------------------------------------------------------
```

Upstream baseline reference: `git show 6752b56:src/claude_swap/switcher.py | grep -c "^# -\{4,\}"` → **0**.

### Banner shape

Each banner is a pair of identical 76-char `# ---...` lines surrounding a
single text line describing the section. Example from
`src/claude_swap/monitor.py:56-58`:

```python
# ----------------------------------------------------------------------------
# Polling cadence — all tunables live here so the trade-offs are auditable.
# ----------------------------------------------------------------------------
```

The middle line is real content (it describes what the constants below do)
and **must be preserved**. Only the two divider lines are removed.

## Commands you will need

| Purpose            | Command                                         | Expected on success |
|--------------------|-------------------------------------------------|---------------------|
| Find dividers      | `grep -n "^# -\{4,\}" src/claude_swap/*.py`     | 18 hits before, 0 after |
| Drift check        | `git diff --stat 331798e..HEAD -- src/claude_swap/migrations.py src/claude_swap/monitor.py src/claude_swap/tui.py` | inspect for unexpected churn |
| Tests              | `python -m pytest -q`                           | 652 passed, 3 skipped |
| Lint (smoke)       | `python -c "import claude_swap.migrations, claude_swap.monitor, claude_swap.tui"` | exit 0 |

## Scope

**In scope** (the only files you should modify):

- `src/claude_swap/migrations.py`
- `src/claude_swap/monitor.py`
- `src/claude_swap/tui.py`

**Out of scope** (do NOT touch):

- Other source files (even if they have other comment patterns).
- Test files (`tests/`).
- The plain-text content lines between divider pairs — they're real
  section descriptions, not noise.
- Any other comment cleanup (`# noqa`, `# TODO`, etc.) — out of scope.
- Reformatting / reordering of code between the removed dividers.

## Git workflow

- Branch: stay on `feat/auto-switch-on-limit`.
- Single commit. Message: `style: remove banner divider comments to match upstream`
  (one-line body; this is a pure formatting change).
- Do NOT push.

## Steps

### Step 1: Confirm the baseline count

```bash
grep -c "^# -\{4,\}" src/claude_swap/migrations.py src/claude_swap/monitor.py src/claude_swap/tui.py
```

Expected:

```
src/claude_swap/migrations.py:6
src/claude_swap/monitor.py:2
src/claude_swap/tui.py:10
```

If any count differs, STOP — the file has drifted since planning.

### Step 2: Remove divider lines from each file

For each of the three files, delete every line that matches the regex
`^# -{4,}\s*$`. **Do not** delete the section-description line that sits
between each pair of dividers.

The safest approach is targeted Edit calls per pair (one Edit per banner
pair) rather than a single regex pass — each Edit removes the two divider
lines and leaves the middle description line in place.

Example transformation (from `monitor.py:56-58`):

Before:

```python
# ----------------------------------------------------------------------------
# Polling cadence — all tunables live here so the trade-offs are auditable.
# ----------------------------------------------------------------------------
```

After:

```python
# Polling cadence — all tunables live here so the trade-offs are auditable.
```

Apply to all 9 banner pairs (3 in `migrations.py`, 1 in `monitor.py`, 5 in `tui.py`).

**Verify** after each file:

```bash
grep -n "^# -\{4,\}" src/claude_swap/<file>.py
```

Expected: no output (zero hits) for the file you just edited.

### Step 3: Confirm zero dividers project-wide

```bash
grep -n "^# -\{4,\}" src/claude_swap/*.py
```

Expected: no output.

### Step 4: Smoke import + full tests

```bash
python -c "import claude_swap.migrations, claude_swap.monitor, claude_swap.tui"
python -m pytest -q
```

Expected: import exits 0; pytest reports **652 passed, 3 skipped, 0 failed**.

## Test plan

No new tests. Pure comment removal — existing suite is the regression
guard. If any test that depends on file line numbers (very unlikely) fails,
investigate before retrying.

## Done criteria

- [ ] `grep -n "^# -\{4,\}" src/claude_swap/*.py` returns no matches.
- [ ] `python -m pytest -q` reports 652 passed, 3 skipped.
- [ ] `git diff --stat` shows exactly 3 files changed:
      `migrations.py`, `monitor.py`, `tui.py`.
- [ ] Net line delta: `-18` lines (no additions; deletions only).
- [ ] `plans/README.md` status row for plan 011 updated to DONE.

## STOP conditions

Stop and report if:

- The line count in Step 1 doesn't match (file drifted; re-plan).
- A test fails after divider removal — investigate the test (it should not
  depend on line numbers in source files; if it does, that's a separate
  test-fragility finding).
- A divider sits next to a docstring delimiter or in a way that removing
  it breaks indentation — pause and report the file:line so a human can
  decide.
- You discover any banner pair that's actually a `"""..."""` boundary or
  a magic marker the parser depends on (none expected, but verify).

## Maintenance notes

- Future contributors who reach for banner dividers should be redirected
  to use Python's `class`/`def`/blank-line structure, which the author
  relies on for visual grouping. Consider adding a one-line rule to
  `CONTRIBUTING.md` if one exists, but do not add it as part of this plan.
- A reviewer should spot-check that no description-only middle line was
  accidentally removed (each `# ---` pair surrounded a real comment).
