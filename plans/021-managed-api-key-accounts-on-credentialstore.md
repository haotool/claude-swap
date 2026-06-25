# Plan 021: Managed API-key (sk-ant-api) accounts on the extracted CredentialStore

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If a "STOP condition" occurs, stop and report — do not improvise.
> When done, update the status row in `plans/README.md` unless a reviewer told
> you they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 612433b..HEAD -- src/claude_swap/switcher.py src/claude_swap/monitor.py src/claude_swap/session.py src/claude_swap/transfer.py`
> Plan 020 must have landed first: confirm `src/claude_swap/credentials.py`
> exists with a `CredentialStore` class. If it does not, STOP — this plan
> implements the feature *inside* that module. Re-confirm the excerpts below
> against the current tree before editing.

## Status

- **Priority**: P2 (feature parity with upstream PR #72; not a correctness gate)
- **Effort**: M
- **Risk**: MED — adds a second auth axis to credential write/read and a new
  account `kind`; the monitor interaction (below) is novel to this fork
- **Depends on**: **Plan 020** (CredentialStore extraction) — hard dependency
- **Upstream reference**: realiti4/claude-swap PR #72 (`b3b559a`), tag `v0.15.0b1`
- **Category**: feature / selective-convergence
- **Planned at**: commit `612433b`, 2026-06-25

## Problem (evidence)

Upstream `v0.15.0b1` (PR #72) lets `cswap --add-token` register a managed
API key (`sk-ant-api…`, the kind Claude Code uses after `/login` with a key)
in addition to OAuth setup-tokens, switching it like any other account with
mutual auth-axis exclusion. Our fork lacks it. We cannot cherry-pick PR #72:
it is built on upstream's `credentials.py` / `json_output.py` modules and an
`USAGE_*` sentinel system our branch does not have (see Plan 020 problem
statement and the conflict analysis: cherry-picking `b3b559a` yields
`modify/delete` conflicts because those files don't exist here). The feature
must be re-implemented against our structure — which, after Plan 020, has a
clean `CredentialStore` to host it.

Additionally, this fork has something upstream did not when PR #72 shipped: an
auto-switch **monitor** (`monitor.py` / `service.py`, per-window tracking from
Plan 019). An API-key account has no subscription quota, so it interacts with
the monitor in ways PR #72 never had to solve (see "Fork-specific design").

## Current state (excerpts, commit `612433b`)

- `switcher.py` `add_account_from_token` — OAuth-only today: wraps the token in
  `{"claudeAiOauth": {...}}`, writes account creds/config, appends to sequence.
  No `kind` field is stored on account records.
- `switcher.py` `_resolve_active_usage` (≈2341) — SSOT for
  `get_active_usage_pct` / `get_active_usage_breakdown`. Guards with
  `if not creds or not oauth.extract_access_token(creds): return None`. A raw
  `sk-ant-api…` credential has no extractable OAuth token, so it currently
  collapses to `None` == "usage unavailable".
- `switcher.py` per-account usage (≈2001) returns the inline string
  `"no credentials"` when no token extracts — an API-key account would be
  mislabeled "no credentials" rather than "no quota".
- `monitor.py` `should_switch(pct, threshold)` → `pct is not None and pct >= threshold`,
  so `None` (API-key active) never triggers a switch *away* — good. But the
  step classifies `None` usage as `usage_unavailable`, which drives the
  failure-backoff sequence and the "observably-silent" WARNING (`monitor.py`
  ≈89–92). That path is wrong for an API-key account: it is not a fetch
  failure.
- `monitor.py` target picker (`_slot_switch_score` ≈130, `_trusted_usage_snapshots`
  ≈987, `_pick_best_switch_target` ≈1212) selects a switch *target* from usage
  snapshots. API-key accounts have no snapshot.
- `session.py` `run()` / `setup_session()` — OAuth-shaped session bootstrap;
  `_is_session_valid` requires `authMethod == "claude.ai"`.
- `transfer.py` `export_accounts` / `import_accounts` — assume credentials are a
  JSON object; a raw `sk-ant-api…` string would fail the JSON parse.
- There is **no** `USAGE_*` sentinel module in this branch; status is conveyed
  as `float | None` plus inline strings.

## Goal

Register and switch managed API-key accounts with mutual auth-axis exclusion,
implemented inside `CredentialStore`, surfaced correctly in usage/status output,
and **safe under the auto-switch monitor** — without introducing upstream's
sentinel module. Behavior for existing OAuth accounts is unchanged.

## Approach

### A. Storage & activation (in `credentials.py` / `CredentialStore`)
Port PR #72's credential layer onto our store:
- `looks_like_api_key(s)` — `s.startswith("sk-ant-api") and not s.startswith("{")`.
- `approved_form(key)` — `key.strip()[-20:]` (mirrors Claude Code
  `normalizeApiKeyForConfig`; required or Claude Code re-prompts to approve).
- `_read_credentials` — after the OAuth keychain+file reads return empty, fall
  through to `_read_managed_key` (macOS Keychain service **`"Claude Code"`**,
  then `~/.claude.json` `primaryApiKey`). Try OAuth fully first so a file-only
  OAuth login is never misread as a key.
- `_write_credentials` — detect kind from payload; OAuth path also calls
  `_clear_managed_key`; API-key path calls `_write_managed_credentials` then
  `_clear_oauth_credential` (delete keychain item **and** `.credentials.json`).
- `_write_managed_credentials` / `_clear_managed_key` / `_read_managed_key` /
  `_read_global_config` / `_update_global_config` (atomic mkstemp+os.replace,
  mode 0o600, key-scoped to `primaryApiKey` / `customApiKeyResponses`).
- **Reconcile with our `verify` kwarg**: `_write_credentials(creds, *, verify=False)`
  is ours (not upstream). Define how `verify` behaves on the API-key path —
  preferred: `verify` is a no-op for API keys (there is no usage round-trip to
  verify) and documented as such. STOP if `verify` currently performs a network
  check that an API key cannot satisfy.

### B. Account model (`switcher.py`)
- Store `kind: "api_key"` on the account record in `add_account_from_token`
  (OAuth records stay kindless for back-compat).
- Add `_account_kind(account_num) -> "api_key" | "oauth"` (kindless ⇒ "oauth").
- `_reject_live_api_key_capture(creds)` guard in `add_account` paths
  (`add_account` snapshots the *live* credential; never back it up as kindless).
- `_reject_cross_kind_collision(email, is_api_key)` — refuse registering a token
  whose `(email, "")` already exists as the other kind; point at a distinct
  `--email`. Default `…@token.local` labels never collide.
- `add_account_from_token`: auto-detect via `looks_like_api_key`; store raw key
  as the credential (not OAuth JSON); default label `api-key-{slot}@token.local`;
  log/print "API key" vs "token".

### C. Usage & status (fit OUR model, NOT a sentinel module)
- In `_resolve_active_usage` and the per-account usage path, branch on
  `looks_like_api_key(creds)` **before** the `extract_access_token` guard and
  represent "no quota" distinctly from "no credentials". Use the existing
  inline-string convention (e.g. `"API key (no quota)"`), not a new
  `USAGE_API_KEY` constant — match how this branch already conveys
  `"no credentials"` / `"not managed"`.
- `list_accounts` / `status` render that string for API-key accounts.

### D. Fork-specific design — monitor interaction (the novel part)
PR #72 had no monitor; this fork does. Decide and implement:
1. **Active account is API-key →** the monitor must treat it as **idle /
   infinite headroom**, NOT `usage_unavailable`. Add an explicit branch
   (detect via `_account_kind` of the active account, or `looks_like_api_key`
   on the active creds) so the step is classified `idle` and does **not** enter
   failure-backoff or emit the "observably-silent" WARNING. `should_switch`
   already returns False for `None`, so no switch-away fires — but the
   *classification* and logging must say "API key, no quota to monitor".
2. **API-key as a switch target →** an API-key account is the safest possible
   target (no quota to exhaust). At minimum it must not crash the snapshot
   logic and must remain a *selectable* target. Preferring it over a
   near-threshold OAuth account is desirable but optional; if implemented, keep
   it explicit in `_slot_switch_score`. STOP and report if making API-key
   selectable requires reworking `_trusted_usage_snapshots` beyond a small
   guard — defer the preference to a follow-up and only guarantee
   "selectable + no crash" here.

### E. Session & transfer
- `session.py`: `_ensure_not_api_key(account_num, email)` guard in `run()`
  (before the same-account direct-launch fast path) and `setup_session()`;
  raise `SessionError` with guidance to use `--switch-to`. (Session mode is
  OAuth-shaped; API-key session support is explicitly out of scope.)
- `transfer.py`: export carries the raw `sk-ant-api…` string verbatim plus
  `kind: "api_key"`; import accepts a string credential when `kind == "api_key"`
  (validate it `looks_like_api_key`) instead of requiring a JSON object.

## STOP conditions

- Plan 020 not landed (`credentials.py` / `CredentialStore` absent) — STOP.
- Our `verify` kwarg performs a network/usage check incompatible with API keys
  — STOP and resolve its semantics before wiring the API-key write path.
- Making an API-key account a selectable monitor target requires more than a
  small guard in the snapshot/picker path — STOP, ship "selectable + no crash"
  only, and file the preference ordering as a follow-up.
- Any change alters an existing OAuth account's switch/usage/monitor outcome
  (not just added API-key branches) — STOP; this must be additive.

## Test plan

- Storage: `CredentialStore` write/read round-trip for an API key on a fake
  host — macOS keychain path and `~/.claude.json` `primaryApiKey` fallback;
  assert mutual exclusion clears the OAuth credential, and the OAuth write path
  clears the managed key.
- `approved_form` records `key[-20:]` in `customApiKeyResponses.approved`.
- Registration: `add_account_from_token("sk-ant-api03-…")` stores `kind:
  "api_key"`, default label `api-key-{slot}@token.local`; cross-kind collision
  raises; `--add-account` on a live API-key login is rejected.
- Usage: an active API-key account reports the "no quota" string (not "no
  credentials") in `list_accounts` / `status`.
- **Monitor (fork-specific):** with an API-key active account, `monitor_step`
  classifies `idle` (not `usage_unavailable`), does **not** enter
  failure-backoff, and emits no "observably-silent" WARNING; `should_switch`
  stays False. Pin this — it is the regression most likely to be missed.
- Monitor target: API-key account is a valid switch target and does not crash
  `_trusted_usage_snapshots` / `_pick_best_switch_target`.
- Transfer: export→import of an API-key account round-trips the raw string and
  `kind`.
- Session: `cswap run <api-key-account>` raises `SessionError` with the
  `--switch-to` guidance.
- Full suite green; `ruff check src/ --select F` clean.

## Out of scope (separate decisions)

- API-key **session** support (`cswap run`) — explicitly rejected here.
- Preference ordering that *favors* API-key targets over OAuth — optional;
  defer to a follow-up if it exceeds a small guard (see STOP).
- Introducing upstream's `json_output.py` / `USAGE_*` sentinel module — not
  adopted; a separate convergence decision if ever wanted.

## Verification baseline

Pre-work (commit `612433b`): `python -m pytest -q` → **667 passed, 3
skipped**; `ruff check src/ --select F` → clean. (Plan 020 will raise the
count; re-baseline against the post-020 HEAD before starting.)
