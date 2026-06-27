# Plan 018: Complexity-convergence roadmap (P2 structural)

> **Executor instructions**: This is a *roadmap* plan — it sequences several
> independent sub-refactors, each of which MUST be done as its own branch +
> commit with the full suite green between steps. Do not batch them. Follow
> each step, run every verification command, and confirm the expected result
> before moving on. If a "STOP condition" occurs, stop and report. When a
> sub-step lands, update the status row in `plans/README.md` unless a reviewer
> told you they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat a70bfa1..HEAD -- src/claude_swap/switcher.py src/claude_swap/cli.py src/claude_swap/monitor.py`
> If these changed since this plan was written, re-read the named functions and
> recompute the complexity baseline before proceeding; on a material mismatch,
> treat it as a STOP condition.

## Status

- **Track B (B1–B4): DONE** — 2026-06-25; see "Outcome" below.
- **Track C: partially DONE** — 2026-06-26. Both named seams now landed as
  cohesive modules with switcher keeping thin delegators / re-exports:
  - `CredentialIO` → `credentials.py` `CredentialStore` + `credential_refresh.py`
    `CredentialRefresher` (plans 020/021).
  - `UsageCache` **codec layer** → `usage_cache.py` (the 7 pure
    `_usage_*` serialization/freshness functions + `_USAGE_CACHE_TTL`). The
    *orchestration* methods (`_refresh_switchable_usage_cache` — 7-method
    coupling to creds/session/account; `_trusted_usage_snapshots`) deliberately
    stay in switcher: they are credential/session orchestration, not cache
    codec, and extracting them would create a chatty back-referencing
    collaborator (anemic-module anti-pattern). This is the evidence-based stop
    point for safe extraction; no circular coupling was introduced.
- **Track D (plan 022): DONE** — 2026-06-27. Upstream sync, TUI in-place Watch,
  auto_switch_planning + JSON payload SSOT, switch/purge phase decomposition.
  switcher.py 3712→3494 LOC; 739 passed.
- **Priority**: P2
- **Effort**: L (split across sub-steps)
- **Risk**: MED — touches the critical switch path; guard with the suite
- **Depends on**: Track A (committed: `3513731`, `a70bfa1`)
- **Category**: tech-debt / production-grade
- **Planned at**: commit `a70bfa1`, 2026-06-25

## Why this matters

After Track A removed dead code and unused imports (src 8503 → 8473 LOC,
suite green at 660), the remaining convergence debt is **structural**, not
mechanical. `switcher.py` is a 3100-LOC god-module whose
`ClaudeAccountSwitcher` carries ~70 methods, several of which exceed every
complexity budget ruff enforces:

| Function | Location | Statements | Cyclomatic |
|---|---|---|---|
| `_perform_switch` | switcher.py | 149 | 35 |
| `list_accounts` | switcher.py | 115 | 33 |
| `add_account` | switcher.py | 109 | 24 |
| `cli.main` | cli.py | 103 | 32 |
| `monitor_step` | monitor.py | 96 | 17 |
| `switch` | switcher.py | 91 | 27 |
| `add_account_from_token` | switcher.py | 89 | 22 |
| `purge` | switcher.py | — | 28 |

These do not *reduce* line count when decomposed (extraction is roughly
LOC-neutral), but they are the dominant production-grade risk: a 149-stmt
function on the credential-swap path is untestable in isolation and unsafe
to change. The goal here is **maintainability convergence with behavior
held constant**, verified by the existing suite acting as a characterization
harness.

## Sequencing (each = own commit, suite green between)

> **All four steps landed 2026-06-25, each fast-forwarded into
> `improve/p1-clean-code-convergence` with the full suite (660 passed / 3
> skipped) green between merges. See "Outcome" for the as-built result, which
> diverged from the helper names guessed below.**

### Step B1 — decompose `_perform_switch` (highest risk first, smallest blast) ✅ DONE (`59257a0`)
Extract cohesive, already-commented phases into private methods on the
switcher, preserving call order and side effects exactly:
- readback verification → `_verify_switch_readback(...)`
- multi-session race awareness → `_guard_concurrent_sessions(...)`
- target credential refresh → already `_refresh_target_credentials_before_activation`; ensure `_perform_switch` only orchestrates.
Leave the orchestration body as a linear sequence of named calls.
**STOP** if any extraction changes the number/order of credential writes —
diff `git log -p` against the `_write_*credentials` call sequence.

### Step B2 — decompose `list_accounts` ✅ DONE (`bf577ab`)
Split the row-building (per-account usage/health assembly) from the
rendering. The renderer is SSOT for the TUI (plan 004) — do **not** fork it;
extract `_build_account_rows(...)` returning data, keep one print path.

### Step B3 — `cli.main` dispatch table ✅ DONE (`34ecc42`)
Replace the if/elif command chain with a `{command: handler}` mapping.
Pure control-flow flattening; each handler keeps its current body.

### Step B4 (optional) — `add_account` / `add_account_from_token` ✅ DONE (`88057ff`)
Factor the shared "persist new slot + sequence bookkeeping" tail into one
helper both call.

## Outcome (as built, 2026-06-25)

All four hotspots cleared their statement/branch budgets; behavior held
constant (660 passed throughout; live switch round-trips for B1/B3).

| Step | Commit | Before (stmt/cyclo) | As-built helpers |
|---|---|---|---|
| B1 | `59257a0` | 149 / 35 | `_warn_switch_session_hazards`, `_activate_target_directly`, `_swap_target_transactional`, `_print_switch_result` |
| B2 | `bf577ab` | 115 / 33 | `_collect_accounts_info`, `_fetch_account_usage`, `_resolve_usages`, `_print_account_rows`, `_print_running_instances` |
| B3 | `34ecc42` | 103 / 32 | `_build_parser`, `_validate_args`, `_dispatch_action`, `_cmd_export/import/tui/monitor`, `_SUBCOMMANDS` |
| B4 | `88057ff` | 24 / 22 | `_resolve_target_slot`, `_apply_slot_displacement`, `_register_account_slot` (**net −29 LOC**, slot logic now SSOT) |

Residual over-budget functions left for a future pass: `switch` (27),
`purge` (28), `_activate_target_directly` (17, B1 by-product),
`migrations.*` (18/22), `monitor.monitor_step` (17),
`session._sync_sharing` (18). None block Track C.

Notes from execution:
- The B1 helper names above differ from the pre-work guesses
  (`_verify_switch_readback` etc.) — the real seams were the two activation
  paths plus the pre/post-lock blocks, not the readback step.
- B3 surfaced a regression: a `{cmd: function}` table binds handlers at import
  time and breaks `monkeypatch.setattr(cli, "_service_command", ...)`. Fixed by
  mapping to names and resolving via `globals()` at call time.

## Track C (deferred, separate plan when B lands)

Split `ClaudeAccountSwitcher` along seams that B1–B4 expose:
- `UsageCache` — the `_usage_*` helper cluster + `_trusted_usage_snapshots`
- `CredentialIO` — `_read/_write/_delete_*credentials`
Only attempt after B proves the seams; a premature split risks circular
coupling. Do **not** start C in the same branch as any B step.

## Findings considered and rejected

- *Collapse the `_usage_*` serialization helpers into one codec* — rejected:
  all seven have independent external callers and direct tests
  (`test_switcher.py` imports `_usage_to_cache`, `_persist_usage_cache_entry`);
  collapsing breaks the tested surface for no behavior gain.
- *Extract an `_as_dict()` guard for the `isinstance(x, dict) else {}`
  idiom* — rejected: only ~3 unifiable sites, inline ternaries are already
  readable; a helper trades clarity for a ~4-line saving (KISS/YAGNI).
- *Remove `oauth.fetch_usage` (no production caller)* — deferred, not
  rejected: it is internal (not in `__all__`) and dead, but its tests live in
  the shared `TestFetchUsage` class alongside `build_usage_result` edge-case
  tests; removal needs careful test surgery, tracked as a follow-up.
- *Remove `_pick_best_switch_target`* — rejected: self-documented test helper
  that covers the on-disk cooldown-scoring path not otherwise exercised.

## Verification baseline

Track A landing point (commit `a70bfa1`): `python -m pytest -q` →
**660 passed, 3 skipped** in ~7s; `ruff check src/ --select F` clean.
Every sub-step's done-criteria require the full suite green and no new
ruff F-class findings.
