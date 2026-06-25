# Plan 020: Extract CredentialStore out of switcher.py (align interface to upstream)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If a "STOP condition" occurs, stop and report — do not improvise.
> When done, update the status row in `plans/README.md` unless a reviewer told
> you they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 612433b..HEAD -- src/claude_swap/switcher.py`
> If `switcher.py` changed since this plan was written, re-read the credential
> methods listed under "Current state" and re-confirm their line ranges and
> signatures before proceeding; on a material mismatch (renamed/removed
> methods, changed signatures), STOP.

## Status

- **Priority**: P1 (clean-code convergence; unblocks API-key feature)
- **Effort**: M
- **Risk**: MED — touches the active/backup credential read/write paths; guard
  with the full suite at every phase
- **Depends on**: none
- **Enables**: Plan 021 (managed API-key accounts) — to be implemented inside
  the extracted module, not back in switcher.py
- **Category**: refactor / production-grade
- **Planned at**: commit `612433b`, 2026-06-25

## Problem (evidence)

`src/claude_swap/switcher.py` is a 3238-line God Object: one class
(`ClaudeAccountSwitcher`) with 77 methods, of which the credential/keychain
storage layer (~45 occurrences of `keychain` / `set_password` / `get_password`
/ `.credentials.json`) is inlined. This violates the repo's own coding rules
(`200–400 typical, 800 max`; "MANY SMALL FILES > FEW LARGE FILES"; "Extract
utilities from large components"; SOLID single-responsibility).

Upstream already solved this exact problem after our fork point:
commit `d962866` "Extract the credential storage layer out of switcher.py into
a CredentialStore module" produced `credentials.py` (668 lines, a `_StoreHost`
Protocol + `CredentialStore` class, 26 methods). Our branch diverged before
that refactor and instead grew `switcher.py` 1943 → 3238. Aligning our module
boundary and method names to upstream's `CredentialStore` both fixes the God
Object and makes future selective convergence (and the API-key feature) a
drop-in rather than a fight.

## Current state (excerpts)

Host attributes already exposed by `ClaudeAccountSwitcher.__init__`
(`switcher.py`) — these satisfy upstream's `_StoreHost` Protocol directly:

- `self.platform = Platform.detect()` (343)
- `self.credentials_dir = self.backup_dir / "credentials"` (360)
- `self._logger = setup_logging(...)` (362)

Credential methods to extract (line numbers at `612433b`):

| Method | Line | Internal call sites | Upstream parity |
|--------|------|--------------------|-----------------|
| `_read_credentials` | 457 | 10 | same name |
| `_write_credentials(creds, *, verify=False)` | 488 | 3 | **diverges** — extra `verify` kwarg (ours) |
| `_uses_file_backup_backend` | 554 | 5 | same name |
| `_read_account_credentials` | 565 | 9 | same name |
| `_write_account_credentials` | 591 | 9 | same name |
| `_write_verified_live_account_credentials` | 655 | — | **ours only** |
| `_delete_account_credentials` | 878 | 1 | same name |
| inlined keychain helpers (set/get/delete_password blocks) | 457–900 | — | upstream factors into `_kc_*` methods |

Blast radius: every call site for these methods is **inside switcher.py**. No
other module (`cli.py`, `session.py`, `transfer.py`, `tui.py`, `monitor.py`)
calls them directly — confirmed via
`git grep -nE '\._(read_credentials|write_credentials|read_account_credentials|write_account_credentials)\b' -- src/claude_swap ':!src/claude_swap/switcher.py'`
(no hits). So a thin in-switcher delegation layer keeps all callers working.

## Goal

Move the credential/keychain storage layer into a new
`src/claude_swap/credentials.py` as a `CredentialStore` class fed by a
`_StoreHost` Protocol, leaving thin delegators in `switcher.py` so no call site
changes. Method names and the Protocol shape mirror upstream's `credentials.py`
to enable later convergence. Behavior is unchanged (pure refactor).

## Approach (Strangler Fig — one phase per commit, suite green each time)

### Phase 1 — Scaffold
- Create `credentials.py` with the `_StoreHost` Protocol
  (`platform: Platform`, `credentials_dir: Path`, `_logger: logging.Logger`)
  and `CredentialStore.__init__(self, host)` holding `_keychain_usable_cache`
  and `_last_active_credentials_backend`.
- In `switcher.__init__`, after the host attributes exist, add
  `self._store = CredentialStore(self)`.
- Move `_keychain_usable_cache` / `_last_active_credentials_backend` onto the
  store; expose them on the switcher as `@property` delegators.
- No credential logic moved yet. **Suite green.**

### Phase 2 — Move the active-credential axis
- Move `_read_credentials`, `_write_credentials`, `_use_keychain`, `_kc_call`,
  and the inlined active-credential keychain block into the store.
- **Pre-step**: where keychain access is inlined inside a larger method, first
  extract it into a named method *in place* (still in switcher), confirm green,
  then move it. Do not move-and-rename in one edit.
- Preserve our `verify` kwarg:
  `_write_credentials(self, credentials, *, verify=False)`; document the
  divergence from upstream in the docstring.
- Replace the switcher methods with thin delegators. **Suite green.**

### Phase 3 — Move the backup-credential axis
- Move `_read_account_credentials`, `_write_account_credentials`,
  `_delete_account_credentials`, `_uses_file_backup_backend`,
  `_write_verified_live_account_credentials`, and the `_backup_enc_*` /
  `_kc_*_backup` helpers into the store.
- Leave thin delegators in switcher. **Suite green.**

### Phase 4 — Finalize & measure
- Confirm switcher.py imports `macos_keychain` for nothing it still owns; no
  residual `set_password` / `get_password` / `delete_password` calls remain in
  switcher.
- Record metrics in the PR description (see DoD).

## STOP conditions

- If any credential method turns out to be called from **outside** switcher.py
  (re-run the `git grep` above against the current tree) — STOP and widen the
  delegation/import plan before moving it.
- If a method reads switcher *methods* (not just the three data attributes the
  Protocol exposes) — STOP. The Protocol is data-only; resolve the coupling
  (pass the value in, or promote the dependency) before moving.
- If moving `_write_credentials` would drop the `verify` path — STOP; the kwarg
  and its code path must survive the move with test coverage.

## Test plan

- After **each** phase: `python -m pytest -q` must stay at the baseline (667
  passed, 3 skipped) and `ruff check src/ --select F` clean.
- Add a focused test constructing `CredentialStore` against a fake host (object
  exposing `platform` / `credentials_dir` / `_logger`) to prove the Protocol
  boundary is data-only and the store is independently testable.
- No behavioral assertions change — this is a pure refactor; any diff in test
  *outcomes* (not just import paths) is a STOP.

## Out of scope (separate decisions)

- **Managed API-key accounts** (upstream PR #72) — implement in Plan 021 on top
  of the extracted `CredentialStore`; not in this plan.
- Splitting `json_output` / usage-status sentinels out of switcher — a separate
  convergence step; track independently.
- Any rename of our `verify` kwarg or `_write_verified_live_account_credentials`
  to match upstream exactly — keep our semantics; only the module boundary and
  shared method names align here.

## Definition of done (measurable)

- `credentials.py` exists, < 800 lines, single responsibility.
- `switcher.py` drops ~700 lines (3238 → ~2500).
- `keychain` / `set_password` / `get_password` occurrences in switcher.py → ~0.
- All call sites unchanged; suite at baseline; `ruff --select F` clean.
- `_StoreHost` Protocol and `CredentialStore` method names match upstream's
  `credentials.py` where our semantics allow.

## Verification baseline

Pre-work (commit `612433b`): `python -m pytest -q` → **667 passed, 3
skipped**; `ruff check src/ --select F` → clean.
