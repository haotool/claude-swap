# Plan 015: Boundary-case tests for `oauth.build_usage_result`

> **Drift check**: `git diff --stat 8d305d1..HEAD -- src/claude_swap/oauth.py tests/test_oauth.py`
> If material drift, re-read excerpts before adding tests.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `8d305d1`, 2026-06-15

## Why this matters

`oauth.build_usage_result` normalizes the Anthropic usage API response.
It feeds into both the user-visible CLI/TUI usage display and the
auto-switch cooldown-aware target picker (via `resets_at`). Three
boundary cases are currently uncovered:

1. **`resets_at` preservation when `utilization=None`** — auto-switch
   target ranking depends on `resets_at`. If a future change drops
   `resets_at` while the utilization is null, the picker silently picks
   the wrong account.
2. **Missing `extra_usage` key entirely** — the API may omit `extra_usage`
   for accounts that never enabled paid credits. Current code uses
   `.get("extra_usage")` which already handles `None`, but no test asserts
   the result still produces `five_hour` / `seven_day` rows.
3. **Malformed `resets_at`** — a non-ISO string from a server-side bug
   would currently raise inside `format_reset` (`datetime.fromisoformat`).
   No test confirms today's behavior — either it raises (and the caller
   sees `None`) or it should be defensively skipped.

Adding these tests doesn't change behavior; it pins it.

## Current state

`src/claude_swap/oauth.py:208-253` — `build_usage_result`:
- Reads `five_hour`/`seven_day` and includes them when present.
- `resets_at` only added when truthy (`if h5.get("resets_at"):` at 217).
- `extra_usage` requires `is_enabled` AND `used_credits`/`monthly_limit`/`utilization` all non-None.

Existing tests (`tests/test_oauth.py` `TestExtraUsage*` around line 203-274):
- `test_extra_usage_complete` — all fields populated.
- `test_extra_usage_unlimited_keeps_other_rows` — `monthly_limit=None`.
- `test_extra_usage_partial_keeps_other_rows` — `used_credits=None`.
- `test_extra_usage_disabled_keeps_other_rows` — `is_enabled=False`.

Helper at line 198-201:
```python
def _fetch_with_response(self, payload):
    mock_response = mock.MagicMock()
    mock_response.read.return_value = json.dumps(payload).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = lambda *a: None
    with patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response):
        return oauth.fetch_usage("sk-test-token")
```

(Verify the exact line numbers and helper shape before editing.)

## Scope

**In scope**: `tests/test_oauth.py` only.

**Out of scope**: `src/claude_swap/oauth.py` (this plan adds coverage, not
behavior changes).

## Steps

### Step 1: Add three tests inside the existing `TestExtraUsage` (or sibling) class

After `test_extra_usage_disabled_keeps_other_rows`, append:

```python
def test_resets_at_preserved_when_utilization_null(self):
    """Cooldown picker needs resets_at even when utilization is null."""
    result = self._fetch_with_response({
        "five_hour": {"utilization": None, "resets_at": "2026-06-15T12:00:00+00:00"},
        "seven_day": {"utilization": 50.0, "resets_at": "2026-06-22T00:00:00+00:00"},
    })
    assert result is not None
    # When utilization is None, the entry still gets a pct=None; resets_at
    # must round-trip so the target picker can score this slot.
    assert result["five_hour"]["pct"] is None
    assert result["five_hour"]["resets_at"] == "2026-06-15T12:00:00+00:00"
    assert "countdown" in result["five_hour"]
    assert "clock" in result["five_hour"]

def test_missing_extra_usage_key_keeps_other_rows(self):
    """API omits extra_usage entirely → result has five_hour/seven_day, no spend."""
    result = self._fetch_with_response({
        "five_hour": {"utilization": 22.0, "resets_at": None},
        "seven_day": {"utilization": 61.0, "resets_at": None},
        # no extra_usage key at all
    })
    assert result is not None
    assert result["five_hour"]["pct"] == 22.0
    assert result["seven_day"]["pct"] == 61.0
    assert "spend" not in result

def test_malformed_resets_at_propagates_as_none(self):
    """A bad resets_at currently raises ValueError inside format_reset,
    which fetch_usage's outer except converts to None — pin that contract."""
    result = self._fetch_with_response({
        "five_hour": {"utilization": 22.0, "resets_at": "not-an-iso-string"},
    })
    # fetch_usage swallows the exception and returns None.
    assert result is None
```

**Note**: The first test ASSUMES `build_usage_result` accepts `pct=None`.
Check the current code: at line 216, `h5_entry = {"pct": h5["utilization"]}`
— accepts whatever `h5["utilization"]` is, including `None`. So the assertion
should hold. If the test fails because of an `if h5.get("utilization") is not None`
guard, STOP and re-read the code; the contract may be different.

### Step 2: Run tests

```bash
PYENV_VERSION=3.12.10 python -m pytest -q tests/test_oauth.py -v
```

Expected: existing tests pass + 3 new pass = +3 tests.

```bash
PYENV_VERSION=3.12.10 python -m pytest -q
```

Expected: 661 passed, 3 skipped (was 658, +3).

### Step 3: Commit

```
test(oauth): pin build_usage_result boundary contracts

Add three regression tests for under-covered build_usage_result paths:
  - resets_at preserved when utilization is null (cooldown picker contract)
  - missing extra_usage key keeps five_hour/seven_day rows
  - malformed resets_at currently propagates as None (pin behavior)

No production change. 661 passed, 3 skipped.

Plan: plans/015-oauth-build_usage_result-boundary-tests.md
```

## Done criteria

- [ ] 3 new tests added to `tests/test_oauth.py`.
- [ ] `pytest -q` reports 661 passed, 3 skipped.
- [ ] No production source modified.
- [ ] `plans/README.md` row 015 → DONE.

## STOP conditions

- A new test fails (the assumption about today's behavior is wrong) —
  do NOT change production code; STOP and report.
- The helper `_fetch_with_response` is gone or renamed — adapt or report.
