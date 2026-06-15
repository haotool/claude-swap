# Plan 016: Replace `SwitchIntent` `@property` indirection with `ClassVar`

> **Drift check**: `git diff --stat 8d305d1..HEAD -- src/claude_swap/models.py src/claude_swap/switcher.py`

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW (preserves plan 008/009 typed dispatch)
- **Depends on**: none
- **Category**: tech-debt
- **Planned at**: commit `8d305d1`, 2026-06-15

## Why this matters

The three `SwitchIntent` subclasses each define `quiet` and `force_refresh`
as `@property` methods returning constants:

```python
@dataclass(frozen=True)
class ManualSwitchIntent:
    @property
    def quiet(self) -> bool: return False
    @property
    def force_refresh(self) -> bool: return False
```

That's six property methods (3 × 2) defining six per-class constants —
boilerplate the language has a one-liner for. Replacing each property
with a `ClassVar[bool]` keeps the exact external interface (`intent.quiet`,
`intent.force_refresh` still work — Python's attribute lookup hits the
class var), preserves the union type and isinstance dispatch from plan
008, and removes ~18 lines of indirection.

**Why not flatten to a single class + enum (the original F7 suggestion)?**
That regresses plan 008's typed-dispatch design and the isinstance check
at `switcher.py:2424`. The `ClassVar` rewrite is the conservative win.

## Current state

`src/claude_swap/models.py:160-203`:

```python
@dataclass(frozen=True)
class ManualSwitchIntent:
    """Interactive manual rotation (round-robin)."""

    @property
    def quiet(self) -> bool:
        return False

    @property
    def force_refresh(self) -> bool:
        return False


@dataclass(frozen=True)
class InteractiveAutoSwitchIntent:
    """TUI monitor: user-visible automated switch."""

    decision: AutoSwitchDecisionContext

    @property
    def quiet(self) -> bool:
        return False

    @property
    def force_refresh(self) -> bool:
        return True


@dataclass(frozen=True)
class BackgroundAutoSwitchIntent:
    """CLI / launchd monitor: quiet automated switch."""

    decision: AutoSwitchDecisionContext

    @property
    def quiet(self) -> bool:
        return True

    @property
    def force_refresh(self) -> bool:
        return True


SwitchIntent = ManualSwitchIntent | InteractiveAutoSwitchIntent | BackgroundAutoSwitchIntent
```

Callers (verified via grep):
- `switcher.py:2425, 2429, 2662, 2663` — read `intent.quiet`, `intent.force_refresh`
- `switcher.py:2424` — `isinstance(intent, (Interactive..., Background...))`
- `tui.py:351`, `monitor.py:624` — construct `Interactive...(decision=...)` or `Background...(decision=...)`

All read paths use attribute access. **None call `intent.quiet()` as a method.** So `ClassVar` is a drop-in replacement.

## Scope

**In scope**: `src/claude_swap/models.py` only.

**Out of scope**: `switcher.py` / `tui.py` / `monitor.py` callers (their
attribute access is unchanged). Tests (behavior identical).

## Steps

### Step 1: Add `ClassVar` to imports

In `src/claude_swap/models.py`:

```python
from typing import TYPE_CHECKING, ClassVar, Literal
```

(Add `ClassVar` to the existing `from typing import` line.)

### Step 2: Replace the three subclasses

Replace lines 160-200 (the three `@dataclass(frozen=True)` blocks) with:

```python
@dataclass(frozen=True)
class ManualSwitchIntent:
    """Interactive manual rotation (round-robin)."""

    quiet: ClassVar[bool] = False
    force_refresh: ClassVar[bool] = False


@dataclass(frozen=True)
class InteractiveAutoSwitchIntent:
    """TUI monitor: user-visible automated switch."""

    decision: AutoSwitchDecisionContext

    quiet: ClassVar[bool] = False
    force_refresh: ClassVar[bool] = True


@dataclass(frozen=True)
class BackgroundAutoSwitchIntent:
    """CLI / launchd monitor: quiet automated switch."""

    decision: AutoSwitchDecisionContext

    quiet: ClassVar[bool] = True
    force_refresh: ClassVar[bool] = True
```

(`SwitchIntent = ManualSwitchIntent | ...` union type stays as-is.)

`ClassVar` is recognized by `@dataclass` and excluded from field
generation — these attributes won't be init params, won't appear in
`fields()`, but `instance.quiet` still resolves to the class value.

### Step 3: Run tests

```bash
PYENV_VERSION=3.12.10 python -m pytest -q tests/test_auto_switch.py tests/test_switcher.py
```

Expected: all pass (counts unchanged).

```bash
PYENV_VERSION=3.12.10 python -m pytest -q
```

Expected: 661 passed, 3 skipped (assumes plan 015 ran first; otherwise
658).

### Step 4: Commit

```
refactor(models): replace SwitchIntent property indirection with ClassVar

Each subclass defined .quiet and .force_refresh as @property methods
returning per-class constants. Replaced with ClassVar[bool] — same
attribute access for callers (intent.quiet still works), same union
type, same isinstance dispatch. Removes ~18 lines of boilerplate while
preserving plan 008's typed-dispatch design.

No behavior change. 661 passed, 3 skipped.

Plan: plans/016-switchintent-classvar-simplification.md
```

## Done criteria

- [ ] `models.py` has 0 `@property` decorators inside the three intent classes.
- [ ] `from typing import ... ClassVar ...` present.
- [ ] `pytest -q` matches baseline.
- [ ] `git diff --stat` shows one file: `src/claude_swap/models.py`.
- [ ] Net line delta: ~-18 lines.
- [ ] `plans/README.md` row 016 → DONE.

## STOP conditions

- Any test asserts `callable(intent.quiet)` or accesses `intent.quiet()`
  — the contract is method, not attribute; do NOT change to ClassVar.
- `@dataclass(frozen=True)` complains about ClassVar (some old Python
  versions). Python ≥3.12 is the project floor; should be fine, but
  STOP if you hit a TypeError.
- A caller does `dataclasses.fields(intent)` and expects `quiet` to
  appear — ClassVar fields are excluded by design; STOP and re-examine.
