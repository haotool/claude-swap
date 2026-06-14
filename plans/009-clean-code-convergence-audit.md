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
| **Overall convergence** | **6.5–7 / 10** | WIP committed → ~7.5; Phases 2–4 → ~9 |
| SSOT architecture | 7/10 | Engine converged; adapters/cache drift |
| Production / SRE | Conditional **GO (beta)** | Not silent-upgrade safe for auto-switch |
| Security | **PASS** | 0 P0/P1; P2 OAuth refresh outside FileLock |
| Test readiness | **74/100** | Engine strong; TUI/intent/E2E gaps |
| Docs (working tree) | ~75% beta-ready | HEAD README stale until commit |
| Plans 006–008 implementation | ~80–95% | Functional; git/checkbox drift |

**Production gate:** Conditional GO for **beta only** — manual switching and
plist/config compatible; automated switching requires explicit upgrade steps
(see Rollout checklist).

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
| P0-1 | ~1200 LOC uncommitted; `models.py` imported by `switcher.py` but not on HEAD | git, release | Atomic commit: 006→007→008 + `models.py` + tests + README + plans |
| P0-2 | TUI monitor **bypasses PID lock** — can run concurrently with CLI `--monitor` / launchd | TUI, service | Extend `_acquire_monitor_pid` to TUI or explicit mutual-exclusion contract |
| P0-3 | `build_auto_switch_decision()` **outside** `monitor_step`'s `ClaudeSwitchError` handler — `LockError`/`OSError` can crash launchd loop | monitor, service | ✅ Wrap decision + perform in engine error boundary |
| P0-4 | `_next_poll_interval` ordering bug — after baseline reset at high usage, near-trigger 5s floor skipped → 60s retry while still hot | monitor | ✅ Fix interval ordering; add regression test |
| P0-5 | `plans/README.md` marks 006–008 DONE while git HEAD is stale | docs, process | Align status with commit reality after P0-1 |

### P1 — Before calling adapters "done"

| # | Finding | Action |
|---|---------|--------|
| P1-1 | **Cache freshness SSOT split** — file TTL vs per-slot `_cached_at` vs `_usage_cache_fresh` | Unify trust model; threshold pct and planning must agree same poll cycle |
| P1-2 | **Repeated `switch()` while saturated** — `already_optimal` resets baselines but `should_switch` stays true; full replan every ~60s | ✅ Add saturated-hold: skip `perform_switch` until `pct < threshold` |
| P1-3 | TUI Ctrl-C misreported as `already_optimal` | Map interrupt to distinct outcome |
| P1-4 | **Automated fail-closed** vs committed round-robin on cold cache (intentional 007) | Document + upgrade runbook; optional cache warm-up on monitor start |
| P1-5 | **`switch()` API break** — `quiet=`/`prefer_least_busy=` removed; return `None`→`bool` | CHANGELOG / semver note; external callers need migration |
| P1-6 | Legacy `usage.json` without `_cached_at` — no `migrations.py` backfill | One-time stamp from file `timestamp` or force refresh post-upgrade |
| P1-7 | Duplicate switch-failure WARNING in TUI (`_auto_perform_switch` + engine) | Remove adapter duplicate; engine owns structured logs |
| P1-8 | CLI prints "switching account" before `switch_failed` detail | Fix render branch |
| P1-9 | Upgrade ops: **`cswap --list`** warm cache + **`cswap service install`** after package upgrade | README runbook |

### P2 — Quality / maintainability

- Plan 008 incomplete internally: `switch()` still derives `quiet`/`force_refresh` via `isinstance`; `_perform_switch` still boolean-driven.
- `switcher.py` ~3051 lines — duplicated identity resolution and usage fetch paths.
- Dual outcome vocabularies: `SwitchPlanOutcome` vs `MonitorStepKind` (both use `"already_optimal"` by convention).
- TUI stale threshold in header (startup param, not `result.threshold`).
- Auto-enable-on-start duplicated in `run_cli_monitor` + `_do_auto_switch`.
- Triple config status formatters (CLI/TUI/service).
- Missing README failure-mode runbook (`no_trusted_signal`, idle heartbeat, PATH snapshot).
- OAuth refresh outside FileLock during parallel cache refresh (operational race with `force_refresh` handoff).
- Test gaps: no `InteractiveAutoSwitchIntent` contract test; brittle TUI menu `KEY_DOWN` index tests; duplicate fixtures in `test_auto_switch.py`.

### P3 — Nice to have

- `describe_usage_error` untested.
- TUI `s` key force-poll untested.
- Stale Phase/Round comments in `monitor.py` and tests.
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

### Phase 2 — Adapter & cache hardening

**Goal:** Three surfaces behave predictably under beta load.

1. Cache freshness SSOT (P1-1).
2. Saturated-hold state (P1-2).
3. TUI PID exclusivity or documented contract (P0-2 completion).
4. TUI threshold display from `result.threshold`.
5. Remove duplicate failure logging; fix CLI `switch_failed` copy.
6. Usage cache warm-up / `_cached_at` backfill (P1-6).

### Phase 3 — Intent & switcher internals

**Goal:** Plan 008 fully realized; reduce `switcher.py` drift.

1. Thread `SwitchIntent` through `_perform_switch` (remove boolean matrix).
2. Shared identity + usage-fetch helpers (`list_accounts` vs `_refresh_switchable_usage_cache`).
3. Dead code cleanup from departmental audit.
4. Align `SwitchPlanOutcome` / `MonitorStepKind` vocabulary or document mapping.

### Phase 4 — Docs, tests, optional split

**Goal:** Production-grade beta contract.

1. README failure-mode runbook + upgrade checklist.
2. CHANGELOG / semver note for API + behavior breaks.
3. Test backlog (see below) — intent contract, PID lifecycle, thin CLI E2E.
4. Extract shared fixtures to `conftest.py`; split `test_auto_switch.py` by layer.
5. Optional `switcher.py` split after helpers converge.

---

## Top 15 prioritized actions

| # | Action | Phase | Owner hint |
|---|--------|-------|------------|
| 1 | Atomic commit 006→007→008 + models + tests + README | 1 | release |
| 2 | TUI PID exclusivity | 1–2 | adapter/TUI |
| 3 | `monitor_step` error boundary around `build_auto_switch_decision` | 1 | engine |
| 4 | Fix `_next_poll_interval` near-trigger ordering | 1 | engine |
| 5 | Saturated-hold after `already_optimal` | 2 | engine |
| 6 | Unify cache freshness SSOT | 2 | switcher |
| 7 | `_cached_at` backfill or warm-up on enable/monitor start | 2 | switcher/migrations |
| 8 | Remove TUI duplicate switch-failure logs | 2 | TUI |
| 9 | Fix CLI `switch_failed` stdout | 2 | CLI adapter |
| 10 | TUI threshold from `result.threshold` | 2 | TUI |
| 11 | README upgrade runbook (`--list`, `service install`, fail-closed) | 4 | docs |
| 12 | Intent through `_perform_switch` | 3 | switcher |
| 13 | `InteractiveAutoSwitchIntent` vs `BackgroundAutoSwitchIntent` contract tests | 4 | tests |
| 14 | PID acquire/stale/cleanup tests | 4 | tests |
| 15 | Shared fetch/identity helpers in switcher | 3 | switcher |

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
| P3 | `describe_usage_error`; TUI `s` force-poll | Coverage gaps |

Target: raise test readiness from **74/100** to **85+** after Phase 4.

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

- [ ] All P0 findings closed or explicitly deferred with user sign-off in this file.
- [ ] 006–008 source + tests + README + plans on git HEAD (not just working tree).
- [ ] `python -m pytest -q` green on CI/local.
- [ ] `plans/README.md` row for 009 updated to DONE (Phase 1) or IN PROGRESS (Phase 2+).

**Plan 009 fully complete when:**

- [ ] Phases 2–4 actionable items tracked as separate plans or checkboxes above are closed.
- [ ] Rollout checklist in README matches shipped behavior.
- [ ] Test readiness ≥ 85/100 or documented acceptance of remaining gaps.

---

## Relationship to plans 006–008

| Plan | Role | Audit verdict |
|------|------|---------------|
| 006 | Monitor engine SSOT | **Implemented** in working tree; adapter drift remains |
| 007 | Trusted snapshot planning | **Implemented**; intentional fail-closed vs round-robin |
| 008 | SwitchIntent API | **~90%**; `_perform_switch` still boolean-driven |
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

Expected: **630+ passed** before Phase 1 merge; count may rise as test backlog lands in Phase 4.
