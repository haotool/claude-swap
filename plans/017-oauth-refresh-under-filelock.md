# Plan 017: Serialize inactive-account OAuth refresh under FileLock

> **Drift check**: `git diff --stat 8d305d1..HEAD -- src/claude_swap/switcher.py src/claude_swap/oauth.py`

## Status

- **Priority**: P2
- **Effort**: S–M
- **Risk**: MED (touches the credential-write critical path)
- **Depends on**: none
- **Category**: bug (race condition)
- **Planned at**: commit `8d305d1`, 2026-06-15

## Why this matters

Plan 009 flagged this as the **only major leftover** P2 from the audit:
`_refresh_inactive_credentials_if_needed` (switcher.py:840) calls
`oauth.refresh_oauth_credentials` and `self._write_account_credentials`
**outside any FileLock**. Meanwhile:

- `_refresh_target_credentials_before_activation` (line 779) runs inside
  the switch-time FileLock (held at line 2710).
- The `persist` callback inside `list_accounts.fetch` (line 2010) wraps
  its write in FileLock.

So inactive-account refresh from `list_accounts` / monitor cache-warm
paths is the lone unlocked write-path. Two real races result:

1. **Last-writer-wins corruption** when a concurrent `switch()` writes
   the same account's credentials with the lock held — the unlocked
   refresh can overwrite the just-switched-to fresh token with its own
   slightly-older refresh result.
2. **Double-refresh `invalid_grant`** when two processes (e.g. CLI
   `--list` + launchd monitor) hit threshold at once and both try to
   refresh the same expired account. Anthropic's refresh tokens are
   single-use (claude-code#24317); the second loser bricks the slot.

This plan acquires the FileLock around the refresh+write and re-checks
expiry under the lock so a redundant second refresh becomes a no-op.

## Current state

`src/claude_swap/switcher.py:840-867`:

```python
def _refresh_inactive_credentials_if_needed(
    self,
    account_num: str,
    email: str,
    credentials: str,
) -> tuple[str, str | None]:
    """Refresh an inactive backup token before it reaches expiry."""
    oauth_data = oauth.extract_oauth_data(credentials)
    if (
        not oauth_data
        or not oauth_data.get("accessToken")
        or not oauth_data.get("refreshToken")
        or not oauth.is_oauth_token_expired(oauth_data.get("expiresAt"))
    ):
        return credentials, None

    refreshed = oauth.refresh_oauth_credentials(credentials)
    if not refreshed:
        self._logger.info(
            "OAuth refresh unavailable: account=%s email=%s",
            account_num,
            email,
        )
        return credentials, "token refresh failed"

    self._write_account_credentials(account_num, email, refreshed)
    self._logger.info("Refreshed inactive credentials for account %s", account_num)
    return refreshed, "token refreshed"
```

Caller (line 1990-1999 in `list_accounts`):

```python
else:
    creds = self._read_account_credentials(str(num), email)
    if creds:
        creds, refresh_note = self._refresh_inactive_credentials_if_needed(
            str(num),
            email,
            creds,
        )
```

The caller has no lock held. The function does HTTP + write without one.

`FileLock` is **not re-entrant in the same process** (see `locking.py`).
Acquiring the lock here is safe because:
- The call sequence is `_refresh_inactive_credentials_if_needed(...)` →
  returns → (lock released) → `fetch_usage_for_account(...)` which may
  acquire the lock via the `persist` callback. The two lock acquisitions
  are sequential, never nested.

## Scope

**In scope**:
- `src/claude_swap/switcher.py` — `_refresh_inactive_credentials_if_needed`
- `tests/test_oauth.py` or `tests/test_switcher.py` — one new test
  covering the under-lock re-check (skip refresh when disk already has
  a fresh token).

**Out of scope**:
- `_refresh_target_credentials_before_activation` — already lock-held by
  its caller at line 2710.
- `oauth.refresh_oauth_credentials` itself (network function — stays
  pure).
- The `persist` callback at line 2010 — already correct.

## Steps

### Step 1: Rewrite `_refresh_inactive_credentials_if_needed` to lock + re-check

Replace the function body (lines 847-867 — the body, not the def
signature):

```python
def _refresh_inactive_credentials_if_needed(
    self,
    account_num: str,
    email: str,
    credentials: str,
) -> tuple[str, str | None]:
    """Refresh an inactive backup token before it reaches expiry.

    Acquires the file lock for the refresh + persist step and re-reads
    the on-disk credentials under the lock. If another process already
    refreshed this slot since the caller's read, the on-disk fresh token
    is returned without a redundant network call (Anthropic's single-use
    refresh tokens make double-refresh a hard failure — claude-code#24317).
    """
    oauth_data = oauth.extract_oauth_data(credentials)
    if (
        not oauth_data
        or not oauth_data.get("accessToken")
        or not oauth_data.get("refreshToken")
        or not oauth.is_oauth_token_expired(oauth_data.get("expiresAt"))
    ):
        return credentials, None

    with FileLock(self.lock_file):
        # Re-read under the lock — another process may have refreshed
        # while we were waiting.
        latest = self._read_account_credentials(account_num, email) or credentials
        latest_oauth = oauth.extract_oauth_data(latest)
        if (
            latest_oauth
            and latest_oauth.get("accessToken")
            and not oauth.is_oauth_token_expired(latest_oauth.get("expiresAt"))
        ):
            self._logger.info(
                "OAuth refresh skipped (already fresh on disk): account=%s",
                account_num,
            )
            return latest, "token already fresh on disk"

        refreshed = oauth.refresh_oauth_credentials(latest)
        if not refreshed:
            self._logger.info(
                "OAuth refresh unavailable: account=%s email=%s",
                account_num,
                email,
            )
            return latest, "token refresh failed"

        self._write_account_credentials(account_num, email, refreshed)
        self._logger.info("Refreshed inactive credentials for account %s", account_num)
        return refreshed, "token refreshed"
```

Two behavior changes that matter:
1. Network refresh + write are atomic with respect to other claude-swap
   processes on the same machine.
2. On a TOCTOU win (another process refreshed first), we return the
   freshly-loaded credentials with a distinct note instead of attempting
   a guaranteed-to-fail double refresh.

### Step 2: Add a regression test

In `tests/test_switcher.py` (or `tests/test_oauth.py`, whichever has
similar fixtures — model after an existing test that exercises
`_refresh_inactive_credentials_if_needed` or `list_accounts`).

```python
def test_refresh_inactive_skips_when_disk_already_fresh(self, temp_home: Path):
    """Lock-acquired re-check skips redundant refresh when disk is fresh."""
    switcher = ClaudeAccountSwitcher()
    # ... bootstrap an account whose in-memory creds say "expired"
    #     but whose on-disk creds (written by a simulated concurrent
    #     process) say "fresh".
    stale_creds = json.dumps({"claudeAiOauth": {
        "accessToken": "old", "refreshToken": "rt-old",
        "expiresAt": 0,  # expired
    }})
    fresh_creds = json.dumps({"claudeAiOauth": {
        "accessToken": "new", "refreshToken": "rt-new",
        "expiresAt": (int(time.time()) + 3600) * 1000,
    }})
    switcher._write_account_credentials("1", "x@y.z", fresh_creds)

    with patch("claude_swap.oauth.refresh_oauth_credentials") as mock_refresh:
        result, note = switcher._refresh_inactive_credentials_if_needed(
            "1", "x@y.z", stale_creds,
        )

    assert "fresh" in note.lower() or "skip" in note.lower()
    assert result == fresh_creds
    mock_refresh.assert_not_called()
```

(Adapt to the actual fixture style used elsewhere in the file — the
above is the contract, not necessarily the exact syntax.)

### Step 3: Run tests

```bash
PYENV_VERSION=3.12.10 python -m pytest -q tests/test_switcher.py tests/test_oauth.py tests/test_auto_switch.py
```

Expected: all pass + 1 new.

```bash
PYENV_VERSION=3.12.10 python -m pytest -q
```

Expected: 662 passed, 3 skipped (assumes 015 + 016 ran first; otherwise
adjust).

### Step 4: Commit

```
fix(oauth): serialize inactive-account refresh under FileLock

_refresh_inactive_credentials_if_needed performed HTTP refresh + write
outside any lock. Concurrent claude-swap processes (CLI --list,
launchd monitor) could race two refreshes against the same single-use
refresh token (claude-code#24317), bricking the slot.

Now acquires the existing FileLock for the refresh + write, and
re-reads under the lock: if another process already refreshed the
slot, we return its result without a redundant network call.

Plan: plans/017-oauth-refresh-under-filelock.md
```

## Done criteria

- [ ] `_refresh_inactive_credentials_if_needed` wraps refresh + write in
      `with FileLock(self.lock_file):`.
- [ ] Under-lock re-check returns disk-fresh creds without calling
      `refresh_oauth_credentials`.
- [ ] 1 new test passes that proves the skip-when-fresh behavior.
- [ ] `pytest -q` is green.
- [ ] No other refresh site or caller touched.
- [ ] `plans/README.md` row 017 → DONE.

## STOP conditions

- A test currently calls `_refresh_inactive_credentials_if_needed`
  inside an already-held `FileLock` — that would now deadlock. Verify
  via grep:
  `grep -B5 "_refresh_inactive_credentials_if_needed" tests/ src/`
  Any line showing a `with FileLock` opening before the call → STOP.
- `FileLock` in `locking.py` is re-entrant after all (read it to
  confirm) — the second-acquisition concern disappears, but the test
  above still applies.
- The under-lock re-read returns DIFFERENT creds than expected (e.g.
  the file was just overwritten with garbage) — the function falls back
  to refreshing, but verify no test fixture depends on the old "no
  re-read" contract.
- More than one caller exists for this function (`grep -n
  "_refresh_inactive_credentials_if_needed" src/`). Currently only one
  (line 1993). If a new caller appeared, audit its locking context.

## Maintenance notes

- A future test for "two processes both refresh the same slot" would
  need real subprocess fixtures; the unit test in Step 2 covers the
  intra-process branch (the cheap and high-leverage half). Cross-process
  flock contention is implicitly covered by `tests/test_locking.py` if
  that exists; otherwise it's a future test backlog item.
- If a third caller for inactive-refresh appears, factor the lock into
  a helper so the dedup logic stays in one place.
