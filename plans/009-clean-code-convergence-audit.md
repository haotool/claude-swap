# Plan 009: Clean Code / SSOT convergence audit (plans 006–008)

> **Executor instructions**: This plan is a **meta audit + convergence roadmap**, not
> a feature plan. Read it fully before starting Phase 1 work. Phase 1 items are
> **merge blockers** for the uncommitted 006–008 working tree. Run the drift check
> first; if the working tree no longer matches the audit baseline, re-run a
> focused diff review before executing. When Phase 1 is complete, update the
> status row for this plan in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat dd4536f -- src/claude_swap/{models,monitor,oauth,switcher,tui}.py tests/test_{auto_switch,oauth,switcher}.py README.md plans/`
> If 006–008 are already committed atomically and tests green, skip to Phase 2–4
> items that remain open.

## Status

- **Priority**: P1 (Phase 1 blockers gate merge)
- **Effort**: L (audit done; Phase 1 ≈ M; Phases 2–4 ≈ L cumulative)
- **Risk**: MED — beta auto-switch semantics intentionally changed; rollout needs cache warm-up
- **Depends on**: 006, 007, 008 (implementation in working tree; not fully on git HEAD)
- **Category**: audit / convergence / production-readiness
- **Planned at**: commit `dd4536f`, audit 2026-06-14 (20 departmental agents)

## Why this matters

Plans 006–008 converge the auto-switch feature toward production-grade SSOT:
one monitor engine, trusted snapshot planning, and explicit `SwitchIntent` types.
The working tree (~**+1193 / −421** lines across 10 files) implements this
functionally, but **~1200 LOC remain uncommitted** while `plans/README.md` marks
006–008 DONE. Twenty read-only departmental audits found the **core engine is
shippable for beta** (630 tests pass) while **adapter layers, cache freshness,
rollout ops, and test gaps** still block a confident merge.

This plan records those findings and sequences the remaining work so executors
do not re-audit the same gaps.

---

## Audit scope & baseline

| Item | Value |
|------|-------|
| Baseline commit | `dd4536f` (cooldown-aware picker + adaptive CLI/service monitor) |
| Branch | `feat/auto-switch-on-limit` |
| Uncommitted files | `models.py`, `monitor.py`, `oauth.py`, `switcher.py`, `tui.py`, `tests/test_auto_switch.py`, `test_oauth.py`, `test_switcher.py`, `README.md`, `plans/README.md` (+ plans 006–008 docs) |
| Test baseline cited | **630 passed, 3 skipped** |
| Audit method | 20 Composer 2.5 departmental agents, read-only, no code changes |

### Departmental coverage

Architecture/SSOT, Switch Engine, Monitor/Polling, Data Models, TUI Integration,
OAuth, QA/Tests, Dead Code, Code Style, Documentation, Anti-Overengineering,
Error Handling, Multi-Surface Parity (CLI/TUI/Service), Security,
Backward Compatibility, Plan Alignment, Naming/API, Production/SRE,
Comment Hygiene, DRY, Convergence Roadmap.

---

## Convergence scores

| Dimension | Score | Verdict |
|-----------|-------|---------|
| **Overall convergence** | **9 / 10** | Phases 2–4 landed; subprocess metadata fix (`7a8c2a9`); OAuth FileLock only major leftover |
| SSOT architecture | 9/10 | Engine + cache + adapter formatters + outcome mapping converged; usage-fetch paths remain optional |
| Production / SRE | **GO (beta)** | Upgrade runbook on HEAD; 652 tests green; not silent-upgrade safe for auto-switch |
| Security | **PASS** | 0 P0/P1; P2 OAuth refresh outside FileLock (only major open item) |
| Test readiness | **88/100** | Full suite green; TUI force-poll + `describe_usage_error` covered; menu brittleness open |
| Docs | ~95% beta-ready | README runbook + CHANGELOG + outcome mapping on HEAD |
| Plans 006–008 implementation | ~95% | On git HEAD; checkbox drift closed |

**Production gate:** **GO for beta** — manual switching and plist/config compatible;
automated switching requires explicit upgrade steps (see Rollout checklist).

---

## What converged (SSOT wins — keep)

These are **done in the working tree**; Phase 1 must preserve them, not re-fork:

1. **`monitor.monitor_step()` + `MonitorRuntimeState`** — shared engine for CLI,
   TUI foreground monitor, and launchd (via `cswap --monitor`).
2. **`monitor.should_switch()`** — single threshold rule; switcher does not re-check pct.
3. **`switcher.get/set_auto_switch_config()`** — persisted `autoSwitch` in `sequence.json`.
4. **`build_auto_switch_decision()` → `_plan_automated_switch()`** — trusted snapshots,
   fail-closed (`no_trusted_signal`), live-active identity (plan 007).
5. **`switch(SwitchIntent)`** — `ManualSwitchIntent`, `InteractiveAutoSwitchIntent`,
   `BackgroundAutoSwitchIntent` (plan 008).
6. **`oauth.build_usage_result`** — preserves `resets_at` for cooldown scoring.

---

## Cross-cutting findings

### P0 — Merge blockers

| # | Finding | Surfaces | Action |
|---|---------|----------|--------|
| P0-1 | ~1200 LOC uncommitted; `models.py` imported by `switcher.py` but not on HEAD | git, release | ✅ Atomic commit: 006→007→008 + `models.py` + tests + README + plans |
| P0-2 | TUI monitor **bypasses PID lock** — can run concurrently with CLI `--monitor` / launchd | TUI, service | ✅ Extend `_acquire_monitor_pid` to TUI (`f0ac188`) |
| P0-3 | `build_auto_switch_decision()` **outside** `monitor_step`'s `ClaudeSwitchError` handler — `LockError`/`OSError` can crash launchd loop | monitor, service | ✅ Wrap decision + perform in engine error boundary |
| P0-4 | `_next_poll_interval` ordering bug — after baseline reset at high usage, near-trigger 5s floor skipped → 60s retry while still hot | monitor | ✅ Fix interval ordering; add regression test |
| P0-5 | `plans/README.md` marks 006–008 DONE while git HEAD is stale | docs, process | ✅ Align status with commit reality after P0-1 |

### P1 — Before calling adapters "done"

| # | Finding | Action |
|---|---------|--------|
| P1-1 | **Cache freshness SSOT split** — file TTL vs per-slot `_cached_at` vs `_usage_cache_fresh` | ✅ Unify trust model (`02c78a8`); threshold pct and planning agree same poll cycle |
| P1-2 | **Repeated `switch()` while saturated** — `already_optimal` resets baselines but `should_switch` stays true; full replan every ~60s | ✅ Add saturated-hold: skip `perform_switch` until `pct < threshold` |
| P1-3 | TUI Ctrl-C misreported as `already_optimal` | Map interrupt to distinct outcome |
| P1-4 | **Automated fail-closed** vs committed round-robin on cold cache (intentional 007) | ✅ Document + upgrade runbook; cache warm-up on first active poll (`c049b5b`) |
| P1-5 | **`switch()` API break** — `quiet=`/`prefer_least_busy=` removed; return `None`→`bool` | ✅ CHANGELOG + upgrade steps (`8523386`); external callers need migration |
| P1-6 | Legacy `usage.json` without `_cached_at` — no `migrations.py` backfill | ✅ `read_cache_with_timestamp` SSOT (`50994e8`); warm-up + `--list` runbook |
| P1-7 | Duplicate switch-failure WARNING in TUI (`_auto_perform_switch` + engine) | Remove adapter duplicate; engine owns structured logs |
| P1-8 | CLI prints "switching account" before `switch_failed` detail | ✅ Fix render branch |
| P1-9 | Upgrade ops: **`cswap --list`** warm cache + **`cswap service install`** after package upgrade | README runbook |

### P2 — Quality / maintainability

- ~~Plan 008 internals: `switch()` top-level policy via `isinstance`~~ — explicit intent dispatch (`c1ac66f`).
- ~~Duplicated identity resolution~~ — `_slot_for_identity` SSOT (`87aa299`); usage-fetch paths remain (optional).
- ~~Dual outcome vocabularies~~ — `SwitchPlanOutcome` ↔ `MonitorStepKind` mapping documented (`5d24df6`).
- ~~TUI stale threshold in header~~ — `display_threshold = result.threshold` (`f0ac188`).
- ~~Auto-enable-on-start duplicated~~ — `ensure_auto_switch_enabled()` (`c1ac66f`).
- ~~Triple config status formatters~~ — `auto_switch_display()` (`c1ac66f`).
- ~~Missing README failure-mode runbook~~ — done (`e54f3cc`).
- **OAuth refresh outside FileLock** during parallel cache refresh (operational race with `force_refresh` handoff) — **only major leftover**.
- Test gaps: brittle TUI menu `KEY_DOWN` index tests; ~~duplicate fixtures in `test_auto_switch.py`~~ — conftest stubs (`e53c92b`); layer split still open.

### P3 — Nice to have

- ~~`describe_usage_error` untested~~ — covered (`5d24df6`).
- ~~TUI `s` key force-poll untested~~ — covered (`5d24df6`).
- ~~Stale Phase/Round comments in `monitor.py` and tests~~ — refreshed (`5d24df6`).
- Shared usage-fetch helpers in switcher (identity done; fetch paths optional).
- Dead code cleanup from departmental audit.
- Optional `switcher.py` module split after helpers converge.

---

## Intentional breaking changes (compatibility audit)

Safe for **manual switching** and **config/plist** surfaces. **Not** silent-upgrade safe for automated switching:

| Change | Committed (`dd4536f`) | Pending (007/008) |
|--------|-------------------------|-------------------|
| Cold/missing cache at threshold | Round-robin fallback | `no_trusted_signal` → no switch |
| Already on soonest-to-free slot | May still rotate | `already_optimal` → hold |
| TUI monitor cadence | Fixed 60s loop | Adaptive 5–60s via `monitor_step` |
| `switch()` signature | boolean kwargs, `None` return | `SwitchIntent`, `bool` return |

`migrations.py` handles credential backends only — **not** usage cache schema or intent types.

---

## Phased convergence roadmap

### Phase 1 — Blockers (before merge)

**Goal:** Safe atomic landing of 006–008 working tree.

1. Atomic commit sequence with full test suite green (`python -m pytest -q`).
2. Fix P0-2 through P0-4 (PID, error boundary, poll interval).
3. Align `plans/README.md` and plan checkboxes with git HEAD.
4. Round-4 review on full diff.

**Done when:** All P0 closed; 630+ tests pass; single commit chain on branch; no uncommitted 006–008 source.

### Phase 2 — Adapter & cache hardening ✅

**Goal:** Three surfaces behave predictably under beta load.

1. [x] Cache freshness SSOT (P1-1). — `02c78a8`, `50994e8`
2. [x] Saturated-hold state (P1-2). — `6255603`
3. [x] TUI PID exclusivity (`f0ac188`).
4. [x] TUI threshold display from `result.threshold` (`f0ac188`).
5. [x] Adapter SSOT: `auto_switch_display`, `ensure_auto_switch_enabled`, intent dispatch (`c1ac66f`).
6. [x] Usage cache warm-up / `_cached_at` backfill (P1-6). — `c049b5b`

### Phase 3 — Intent & switcher internals ✅ (core)

**Goal:** Plan 008 fully realized; reduce `switcher.py` drift.

1. [x] Thread `SwitchIntent` through `_perform_switch` (remove boolean matrix). — `64fadec`
2. [x] Shared identity helper — `_slot_for_identity` (`87aa299`); usage-fetch paths remain optional (P3).
3. [ ] Dead code cleanup from departmental audit (P3).
4. [x] Align `SwitchPlanOutcome` / `MonitorStepKind` vocabulary — mapping documented (`5d24df6`).

### Phase 4 — Docs, tests, optional split ✅ (core)

**Goal:** Production-grade beta contract.

1. [x] README failure-mode runbook + upgrade checklist. — `e54f3cc`
2. [x] CHANGELOG / semver note for API + behavior breaks. — `8523386`
3. [x] Intent contract tests (`BackgroundAutoSwitchIntent` / `InteractiveAutoSwitchIntent`). — `01b5efc`; PID lifecycle + thin CLI E2E — `62fbf24`
4. [x] Extract shared fixtures to `conftest.py` — stubs landed (`e53c92b`); layer split optional (P3).
5. [ ] Optional `switcher.py` split after helpers converge (P3).

---

## Top 15 prioritized actions

| # | Action | Phase | Owner hint |
|---|--------|-------|------------|
| 1 | Atomic commit 006→007→008 + models + tests + README | 1 | release |
| 2 | TUI PID exclusivity | 1–2 | adapter/TUI |
| 3 | `monitor_step` error boundary around `build_auto_switch_decision` | 1 | engine |
| 4 | Fix `_next_poll_interval` near-trigger ordering | 1 | engine |
| 5 | Saturated-hold after `already_optimal` | 2 | engine | ✅ `6255603` |
| 6 | Unify cache freshness SSOT | 2 | switcher | ✅ `02c78a8`, `50994e8` |
| 7 | `_cached_at` backfill or warm-up on enable/monitor start | 2 | switcher/migrations | ✅ `c049b5b` |
| 8 | Remove TUI duplicate switch-failure logs | 2 | TUI | ✅ `f0ac188` |
| 9 | Fix CLI `switch_failed` stdout | 2 | CLI adapter | ✅ |
| 10 | TUI threshold from `result.threshold` | 2 | TUI | ✅ `f0ac188` |
| 11 | README upgrade runbook (`--list`, `service install`, fail-closed) | 4 | docs | ✅ `e54f3cc` |
| 12 | Intent through `_perform_switch` | 3 | switcher | ✅ `64fadec` |
| 13 | `InteractiveAutoSwitchIntent` vs `BackgroundAutoSwitchIntent` contract tests | 4 | tests | ✅ `01b5efc` |
| 14 | PID acquire/stale/cleanup tests | 4 | tests | ✅ `62fbf24` |
| 15 | Shared fetch/identity helpers in switcher | 3 | switcher | identity ✅ `87aa299`; fetch open |

---

## Rollout checklist (beta)

**Before deploy**

1. Run `cswap --list` on every machine with auto-switch enabled (seeds `_cached_at`).
2. Confirm threshold and `autoSwitch.enabled` in `sequence.json`.

**After deploy**

1. `cswap service install` on macOS background users (re-bootstrap launchd + version stamp).
2. Verify `cswap service status` — no version mismatch warning.
3. Tail `monitor.err` / `claude-swap.log` for first threshold event.

**Staging verification**

1. Cold cache at threshold → expect fail-closed (not round-robin) unless warm-up ran.
2. Warm cache, already optimal → expect hold, not rotation.
3. TUI + service not running concurrently (after Phase 1–2 PID work).

**Monitor for**

- `no trusted usage snapshots` (expected if cache cold; failure if persistent)
- `already on optimal` (expected hold vs bug)
- Duplicate pollers (pre–Phase 2 TUI PID gap)

---

## Test backlog (from QA audit)

Priority order for new tests — **no code in this plan**; track in Phase 4:

| Pri | Test | Rationale |
|-----|------|-----------|
| P0 | Parametrize intent: `BackgroundAutoSwitchIntent` raises on single-account; `InteractiveAutoSwitchIntent` prints and returns | Product SSOT undocumented in CI |
| P0 | `get_active_usage_pct` + `UsageFetchError` through monitor path | Engine treats all failures as `None` today |
| P1 | `run_cli_monitor(once=True)` integration — mocked HTTP only, assert account change | No golden-path E2E |
| P1 | PID acquire / stale dead pid / `finally` unlink | Launchd ops gap |
| P1 | `run_cli_monitor` auto-enable when config disabled | Untested prod behavior |
| P2 | Collapse duplicate dedup tests to engine + one CLI smoke | Maintainability |
| P2 | Label-driven TUI menu selection vs `KEY_DOWN` count | Brittle tests |
| ~~P3~~ | ~~`describe_usage_error`; TUI `s` force-poll~~ | Covered (`5d24df6`) |

Target: **88/100** achieved (2026-06-14 final integration).

---

## Architecture reference (post-006)

```
CLI (--monitor) ──┐
TUI (foreground) ─┼──> monitor_step() ──> switcher.get_active_usage_pct()
launchd/service ──┘         │              build_auto_switch_decision()
                              │              switch(Intent)
                              v
                    BackgroundAutoSwitchIntent  (CLI/service)
                    InteractiveAutoSwitchIntent (TUI)
```

**Adapter drift to close (Phase 2):**

| Dimension | CLI / launchd | TUI |
|-----------|---------------|-----|
| Switch intent | Background (quiet) | Interactive (prints) |
| PID exclusivity | Yes | No (P0-2) |
| Failure logging | Engine only | Engine + duplicate (P1-7) |
| Threshold display | From `result.threshold` | Frozen at start (P2) |

---

## STOP conditions

Stop Phase 1 execution and re-audit if:

1. Working tree diverges materially from audit baseline (unexpected files in diff stat).
2. Full pytest suite is not green before merge commit.
3. A executor proposes re-introducing cold-cache round-robin as silent fallback (rejected in plan 007).
4. TUI monitor loop is re-forked instead of calling `monitor_step` (rejects plan 006).
5. `models.py` would be committed without its switcher consumers (partial commit risk).

---

## Done criteria

**Plan 009 Phase 1 complete when:**

- [x] All P0 findings closed or explicitly deferred with user sign-off in this file.
- [x] 006–008 source + tests + README + plans on git HEAD (not just working tree).
- [x] `python -m pytest -q` green on CI/local (652 passed, 3 skipped; subprocess metadata fix `7a8c2a9`).
- [x] `plans/README.md` row for 009 updated to DONE (Phase 1) or IN PROGRESS (Phase 2+).

**Plan 009 fully complete when:**

- [x] Phases 2–4 core items closed. (Phase 2 ✅; Phase 3 core ✅; Phase 4 core ✅; P3 optional items remain.)
- [x] Rollout checklist in README matches shipped behavior. — `e54f3cc`
- [x] Test readiness ≥ 85/100 or documented acceptance of remaining gaps. (88/100 — TUI menu brittleness P2; OAuth FileLock P2.)

### Final production integration (2026-06-14)

| # | Item | Commit |
|---|------|--------|
| 1 | Subprocess CLI tests: `pyproject.toml` fallback when package metadata missing | `7a8c2a9` |
| 2 | Outcome mapping doc + stale comment cleanup + TUI force-poll / `describe_usage_error` tests | `5d24df6` |

**Verification:** `python3 -m pytest -q` → **652 passed, 3 skipped, 0 failed**.

**Remaining optional (non-blocking):**

- **P2:** OAuth refresh outside FileLock (only major leftover); brittle TUI menu `KEY_DOWN` tests.
- **P3:** Shared usage-fetch helpers; dead code cleanup; conftest layer split; optional `switcher.py` module split.

### Post-review Top 5 (2026-06-14)

| # | Item | Commit |
|---|------|--------|
| 1 | Cache read SSOT (`read_cache_with_timestamp`) | `50994e8` |
| 2 | Monitor warm-up on first active poll | `c049b5b` |
| 3 | PID lifecycle + usage-error + CLI monitor E2E tests | `62fbf24` |
| 4 | CHANGELOG + upgrade steps for `switch()` break | `8523386` |
| 5 | Saturated-hold regression coverage (with #3 suite) | `6255603` + `62fbf24` |

**Verification:** `python3 -m pytest -q` → **637 passed, 3 skipped, 12 failed** (12 subprocess CLI tests — `PackageNotFoundError` when package not installed editable; fixed in `7a8c2a9`).

### P2 convergence integration (2026-06-14)

| # | Item | Commit |
|---|------|--------|
| 1 | Shared `_slot_for_identity` helper | `87aa299` |
| 2 | `auto_switch_display` + `ensure_auto_switch_enabled` adapter SSOT | `c1ac66f` |
| 3 | conftest stubs + cache warm-up test coverage | `e53c92b` |

**Verification:** `python3 -m pytest -q` → **638 passed, 3 skipped, 12 failed** (subprocess metadata failures; fixed in `7a8c2a9`).

---

## Relationship to plans 006–008

| Plan | Role | Audit verdict |
|------|------|---------------|
| 006 | Monitor engine SSOT | **Implemented** in working tree; adapter drift remains |
| 007 | Trusted snapshot planning | **Implemented**; intentional fail-closed vs round-robin |
| 008 | SwitchIntent API | **Done**; `_perform_switch` intent-driven (`64fadec`) |
| **009** | Audit + convergence roadmap | **This document** — does not replace 006–008 |

```
006 ──> 007 ──> 008 ──> 009 (audit) ──> Phase 1 merge ──> Phases 2–4
```

---

## Verification commands

```bash
# Drift vs audit baseline
git diff --stat dd4536f -- src/claude_swap/ tests/ README.md plans/

# Full suite (merge gate)
python -m pytest -q

# Targeted auto-switch surface
python -m pytest -q tests/test_auto_switch.py tests/test_switcher.py tests/test_oauth.py tests/test_service.py
```

Expected: **652 passed, 3 skipped** (final production integration, 2026-06-14).
