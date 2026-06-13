# Plan 001: Rebase the auto-switch feature work cleanly onto the latest upstream

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. This plan performs a **git rebase with manual
> conflict resolution on credential-storage code**; correctness matters more
> than speed. When done, update the status row for this plan in
> `plans/README.md`.
>
> **Drift check (run first)**:
> `git rev-parse --short HEAD` should be `d3b86d2` on branch
> `feat/auto-switch-on-limit`. If HEAD differs, or the branch is not
> 3-ahead/11-behind `upstream/main`, STOP and report — the divergence this
> plan resolves has changed.

## Status

- **Priority**: P1 (blocks every other plan)
- **Effort**: M
- **Risk**: MED — rewrites credential-storage call sites; a wrong resolution silently breaks macOS account reads.
- **Depends on**: none
- **Category**: migration / tech-debt
- **Planned at**: commit `d3b86d2`, 2026-06-13

## Why this matters

This fork (`origin = 7GMA/claude-swap`) carries 3 local commits on
`feat/auto-switch-on-limit` (auto-switch monitor, credential-health/usage
diagnostics) that were written against an **older** upstream. Since then,
`upstream/main` (realiti4/claude-swap) advanced 11 commits, the biggest of
which **replaced the macOS credential backend**: it moved from the third-party
`keyring` library to the system `security` CLI, extracted that into a new
`src/claude_swap/macos_keychain.py` module, renamed the Keychain service, and
added a one-time migration. Our 3 commits *independently* hand-rolled inline
`security`-CLI helpers in `switcher.py` against the **old** service name.

The result: our credential-health code reads from the pre-migration Keychain
service and duplicates (less securely) what upstream now provides as a module.
Every future feature compounds the conflict. This plan rebases our 3 commits
onto `upstream/main`, **drops our inline Keychain helpers in favor of
upstream's `macos_keychain.py`**, and keeps only our genuinely-new work
(auto-switch + usage-health). After it lands, the fork is linear and current,
and plans 002–005 build on a stable base.

## Current state

**Branch topology** (verify with `git log --oneline -5` and
`git rev-list --left-right --count upstream/main...HEAD`):

- Branch `feat/auto-switch-on-limit`, HEAD `d3b86d2`.
- Merge-base with `upstream/main`: `6752b56`.
- Our 3 commits on top of the merge-base, oldest first:
  - `4531540` — Add auto-switch at usage limit (Beta) to the TUI
  - `61689c8` — fix: harden credential health and usage diagnostics
  - `d3b86d2` — feat: add CLI health and auto-switch monitor

**`git merge-tree` predicts conflicts in exactly these files**:
- `src/claude_swap/switcher.py` — the important one (see below)
- `README.md` — both sides edited the auto-switch / usage sections
- `tests/test_macos_keychain_contract.py` — both rewrote macOS keychain tests
- `tests/test_transfer.py` — incidental overlap

**The core conflict — macOS Keychain access.** Our branch added inline helpers
in `switcher.py` (functions `_read_macos_generic_password`,
`_write_macos_generic_password`, `_delete_macos_generic_password` around
`switcher.py:99-171`, plus `_KEYCHAIN_READ_TIMEOUT_SECONDS = 3` and a
`_KeychainReadTimeout` exception near `switcher.py:70-75`). They are called at
`switcher.py:484`, `:518`, `:669` using the **old** service constant
`KEYRING_SERVICE`. They pass the secret via `-w <password>` on argv (visible in
`ps`) and resolve `security` via `PATH`.

Upstream replaced all of that with `src/claude_swap/macos_keychain.py`, whose
public API is:

```python
# src/claude_swap/macos_keychain.py (upstream/main)
def get_password(service: str, account: str) -> str | None   # None when not found (rc 44)
def item_exists(service: str, account: str) -> bool
def set_password(service: str, account: str, password: str) -> None  # secret via stdin, hex-encoded
def delete_password(service: str, account: str) -> None
class KeychainError(Exception): ...
```

Upstream's `switcher.py` calls it through a **new** service constant
`SECURITY_SERVICE` (not `KEYRING_SERVICE`), e.g.
`macos_keychain.get_password(SECURITY_SERVICE, username)` at upstream
`switcher.py:347`, `set_password(...)` at `:375`, `delete_password(...)` at
`:419`/`:1897`, and keeps `keyring` only for legacy reads + a migration that
relocates old items into the new service. Upstream's version is **strictly more
secure** than ours (pinned `/usr/bin/security`, secret via stdin, hex-encoded),
so we adopt it and discard ours.

**Resolution principle for `switcher.py`**: for any conflict hunk that is about
*how macOS credentials are stored/read/deleted*, **take upstream's side**
(`macos_keychain.*` + `SECURITY_SERVICE`). For any conflict hunk that is about
*usage fetching, the usage cache, account health notes, or auto-switch config*
(our additions — symbols like `get_active_usage_pct`, `get_auto_switch_config`,
`set_auto_switch_config`, `_USAGE_CACHE_TTL`, `DEFAULT_AUTO_SWITCH_THRESHOLD`,
`_max_usage_pct`, `_merge_usage_with_previous`), **keep our side** and re-wire
any credential read inside it to go through `macos_keychain.get_password(
SECURITY_SERVICE, username)`.

**The read-timeout guard is deferred, not kept.** Our
`_KEYCHAIN_READ_TIMEOUT_SECONDS` / `_KeychainReadTimeout` do not exist in
upstream's module. Dropping them keeps this rebase mechanical and KISS.
Re-introducing a timeout on top of `macos_keychain.get_password` is recorded as
a follow-up in `plans/README.md`; do **not** port it in this plan.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Fetch upstream | `git fetch upstream` | exit 0 |
| Branch state | `git rev-list --left-right --count upstream/main...HEAD` | `11<TAB>3` before rebase |
| Run tests | `python -m pytest -q` | all pass (was 427 passed / 2 skipped pre-rebase; expect MORE after, from upstream's new session/keychain tests) |
| Single test file | `python -m pytest tests/test_switcher.py -q` | all pass |
| See a conflict | `git diff` (during rebase) | shows `<<<<<<<` markers |
| Inspect upstream file | `git show upstream/main:src/claude_swap/macos_keychain.py` | prints the module |

Activate the venv first if needed: `source .venv/bin/activate`.

## Scope

**In scope** (modify only as the rebase requires):
- The git history of branch `feat/auto-switch-on-limit` (rebased).
- Conflict resolution in: `src/claude_swap/switcher.py`, `README.md`,
  `tests/test_macos_keychain_contract.py`, `tests/test_transfer.py`.

**Out of scope** (do NOT touch):
- `src/claude_swap/macos_keychain.py` — adopt upstream's as-is; no edits.
- `src/claude_swap/session.py` — upstream's new file; leave intact.
- Any feature work from plans 002–005 — this plan only re-applies what already
  exists in the 3 commits, nothing new.
- `pyproject.toml` version bump — leave at whatever upstream sets.

## Git workflow

This is a fork-maintenance plan; git operations ARE the work.

1. **Safety backup first** (so the old state is always recoverable):
   ```
   git branch backup/pre-upstream-rebase feat/auto-switch-on-limit
   ```
   Verify: `git branch --list 'backup/*'` shows the branch.

2. **Sync local `main` to upstream** (fast-forward only; it must not diverge):
   ```
   git fetch upstream
   git checkout main
   git merge --ff-only upstream/main
   git checkout feat/auto-switch-on-limit
   ```
   Verify: `git log --oneline -1 main` equals
   `git log --oneline -1 upstream/main`. If `--ff-only` fails, STOP and
   report (local `main` has its own commits — a human must decide).

3. **Rebase the 3 feature commits onto upstream**:
   ```
   git rebase --onto upstream/main 6752b56 feat/auto-switch-on-limit
   ```
   This replays `4531540`, `61689c8`, `d3b86d2` on top of `upstream/main`.

## Steps

### Step 1: Create the safety backup branch and sync main

Run the "Git workflow" items 1 and 2 above.

**Verify**: `git branch --list 'backup/*'` lists
`backup/pre-upstream-rebase`, and `git rev-parse main` ==
`git rev-parse upstream/main`.

### Step 2: Start the rebase and read the upstream Keychain module

Run the rebase command (Git workflow item 3). It will stop at the first commit
that conflicts (likely `61689c8` or `4531540`).

Before resolving anything, read upstream's module so you know the API you are
resolving toward:
`git show HEAD:src/claude_swap/macos_keychain.py` (during a rebase, `HEAD` is
the upstream base) — confirm it defines `get_password`, `set_password`,
`delete_password`, `item_exists`, `KeychainError`.

**Verify**: `git status` shows "interactive rebase in progress" and lists
unmerged paths.

### Step 3: Resolve `switcher.py` conflicts using the resolution principle

For each `<<<<<<<` / `=======` / `>>>>>>>` block in `src/claude_swap/switcher.py`:

- **Keychain storage hunk** (mentions `security`, `find-generic-password`,
  `add-generic-password`, `KEYRING_SERVICE`, `_read_macos_generic_password` and
  friends) → keep **upstream's** side: `macos_keychain.*` with
  `SECURITY_SERVICE`. Delete our inline `_read/_write/_delete_macos_generic_password`
  definitions and the `_KEYCHAIN_READ_TIMEOUT_SECONDS` / `_KeychainReadTimeout`
  symbols entirely.
- **Usage / health / auto-switch hunk** (mentions `usage`, `get_active_usage_pct`,
  `get_auto_switch_config`, `set_auto_switch_config`, `_USAGE_CACHE_TTL`,
  `DEFAULT_AUTO_SWITCH_THRESHOLD`, `_merge_usage_with_previous`,
  `_max_usage_pct`, `health_notes`) → keep **our** side. If our code inside such
  a hunk calls one of the deleted inline helpers, replace that call with
  `macos_keychain.get_password(SECURITY_SERVICE, username)` (for reads) — match
  how upstream's other call sites read credentials.
- Ensure `from claude_swap import macos_keychain` is imported (upstream adds it
  near `switcher.py:15`) and that `SECURITY_SERVICE` is defined (it comes from
  upstream's side — keep it).

After editing, search for leftovers:
`grep -n "_read_macos_generic_password\|_write_macos_generic_password\|_delete_macos_generic_password\|_KEYCHAIN_READ_TIMEOUT\|_KeychainReadTimeout\|KEYRING_SERVICE" src/claude_swap/switcher.py`
— the inline-helper names and `_KEYCHAIN_*` must return **nothing**.
`KEYRING_SERVICE` may still appear **only** in upstream's legacy-migration code
(that's intended); if it appears anywhere our usage/health code reads live
credentials, that call is wrong — fix it to `SECURITY_SERVICE`.

Then mark resolved and continue:
```
git add src/claude_swap/switcher.py
```

**Verify**: `git diff --check` reports no conflict markers in `switcher.py`;
`python -c "import ast; ast.parse(open('src/claude_swap/switcher.py').read())"`
exits 0 (file parses).

### Step 4: Resolve the test and README conflicts

- `tests/test_macos_keychain_contract.py`: keep **upstream's** version of any
  hunk that tests the `security` CLI / `macos_keychain` module. If our side
  added a test that is specifically about usage/health timeout behavior tied to
  the deleted `_KeychainReadTimeout`, drop that test (the behavior was
  deferred). When unsure, prefer upstream's contract test.
- `tests/test_transfer.py`: take whichever side is a superset; these are
  incidental. If both added distinct test functions, keep both.
- `README.md`: keep **our** auto-switch / `--monitor` / `--health` documentation
  (it is more complete), but preserve any **new** upstream sections (e.g. the
  `cswap run` session-mode docs) — merge, don't clobber.

`git add` each resolved file, then `git rebase --continue`. Repeat Steps 3–4
for each subsequent conflicting commit until the rebase reports it is finished.

**Verify**: `git status` shows a clean working tree on
`feat/auto-switch-on-limit` (no rebase in progress);
`git rev-list --left-right --count upstream/main...HEAD` prints `0<TAB>3`
(0 behind, our 3 commits ahead).

### Step 5: Run the full suite and fix fallout

```
python -m pytest -q
```

If failures reference macOS keychain, confirm every live-credential read in
our health/usage code uses `macos_keychain.get_password(SECURITY_SERVICE, ...)`.
If a test references the deleted `_KeychainReadTimeout`/`_KEYCHAIN_*`, that test
should have been dropped in Step 4 — remove it.

**Verify**: `python -m pytest -q` → all pass, 0 failed. Record the new
pass/skip counts in the `plans/README.md` status note for this plan.

## Test plan

No new product tests are written here — this is a rebase. The verification is
that the **union** of our tests and upstream's tests passes:

- `python -m pytest tests/test_switcher.py -q` — switcher behavior intact.
- `python -m pytest tests/test_auto_switch.py -q` — our auto-switch tests still
  pass (they must survive the rebase unchanged).
- `python -m pytest tests/test_macos_keychain_contract.py -q` — keychain
  contract (now upstream's) passes.
- `python -m pytest -q` — whole suite green.

## Done criteria

ALL must hold:

- [ ] `git rev-list --left-right --count upstream/main...HEAD` prints `0\t3`.
- [ ] `git diff --check` reports no conflict markers anywhere.
- [ ] `grep -rn "_read_macos_generic_password\|_write_macos_generic_password\|_delete_macos_generic_password\|_KeychainReadTimeout\|_KEYCHAIN_READ_TIMEOUT" src/` returns nothing.
- [ ] `src/claude_swap/macos_keychain.py` exists and is byte-identical to `git show upstream/main:src/claude_swap/macos_keychain.py` (`git diff upstream/main -- src/claude_swap/macos_keychain.py` is empty).
- [ ] `python -m pytest -q` exits 0, 0 failed.
- [ ] `git branch --list 'backup/*'` still lists the safety branch (do not delete it).
- [ ] `plans/README.md` status row updated with the new test counts.

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check fails (HEAD ≠ `d3b86d2` or topology ≠ 3-ahead/11-behind).
- `git merge --ff-only upstream/main` fails on `main` (local main diverged).
- A `switcher.py` conflict hunk mixes Keychain-storage code AND usage/health
  code so tightly you cannot cleanly apply the resolution principle — report the
  hunk verbatim instead of guessing.
- After resolving, more than ~5 tests fail and the failures are not obviously
  the deleted-timeout tests — the resolution likely mis-took a side.
- You find our usage/health code depended on `_KeychainReadTimeout` for
  correctness (not just a test) — report it so the timeout follow-up can be
  prioritized instead of deferred.

## Maintenance notes

- After this lands, push the branch to `origin` (the fork) only when the user
  asks; do not force-push without telling them (rebase rewrote history, so the
  remote feature branch will require `--force-with-lease`).
- Keep `main` as a pure mirror of `upstream/main` going forward; never commit
  to it directly. Future syncs become `git fetch upstream && git checkout main
  && git merge --ff-only upstream/main && git rebase main feat/...`.
- The deferred read-timeout: if macOS users report `cswap --list`/`--monitor`
  hanging on a locked Keychain, wrap `macos_keychain.get_password` with a
  `subprocess` timeout there (one place), not back in `switcher.py`.
- A reviewer should scrutinize every remaining `KEYRING_SERVICE` reference in
  `switcher.py` — each must be legacy-migration only, never a live read.
