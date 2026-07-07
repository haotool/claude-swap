# Changelog

All notable user-facing changes to claude-swap are documented here.

Release version is defined in `pyproject.toml` (currently `0.17.1+haotool.1`).

## [Unreleased]

> **Upgrading with an installed background service?** Run
> `cswap service install` once after upgrading. The old service command line
> (`--monitor --service-monitor`) is retired; until you reinstall, the
> installed service starts, prints a migration note, and exits cleanly —
> no auto-switching happens and no supervisor restart loop is triggered.

### Added

- **Upstream v0.17.1 merged** (usage-polling tuning and macOS hot-reload
  fix — upstream #85/#86):
  - **Deliberate staleness stays decision-grade.** Usage kept stale by a
    failure backoff or the scheduler's own cadence remains trusted for
    switch decisions past the 5-minute freshness window, capped at 1 hour
    (`TRUST_MAX_AGE_S`) — a sustained outage eventually reads as unknown
    so the verified-failover machinery takes back over.
  - **Candidate polls never outlive a window reset.** A poll is never
    scheduled past the candidate's next quota-window reset: stored usage
    is obsolete the moment a window rolls over.
  - **Paste-safe usage-fetch warnings.** Failure logs drop the account
    email and carry the server's `Retry-After` instead.
  - **macOS Keychain switches hot-reload running sessions.** A successful
    Keychain write now rewrites an already-present `.credentials.json`
    (never creating or deleting one), bumping its mtime so a running
    Claude Code session drops its memoized token and picks up the new
    account without a restart.
- **Upstream v0.17.0 merged** (per-account usage store, adaptive
  auto-switch, `cswap config` — upstream #73/#84/#88 and the 07-04 `main`
  series):
  - **Per-account usage store.** Usage snapshots persist per account in
    `usage_store.json` with stale-on-error semantics (a failed fetch keeps
    the last good reading, marked stale, instead of blanking the row),
    staggered fetches, and a per-account `backoffUntil` that honors the
    server's `Retry-After` on 429s. `--list`/`--status`/TUI read through
    the store; the fork's `usage_cache` is retired in its favor (one cache,
    upstream's).
  - **Adaptive auto-switch polling.** Each tick polls the active account
    plus one rotating candidate instead of the whole roster; polling
    escalates near the threshold, skips ahead to the earliest quota reset
    when everything is exhausted, and slows down when usage is not moving.
  - **Idle-hold.** An expired active token with Claude Code idle holds
    instead of switching — protection resumes on the next activity.
  - **`cswap config` subcommand.** `config list|get|set|unset|path` edits
    `settings.json` with strict validation; settings are defined in one
    table (`SettingSpec`) shared by loading, validation, and help text.
  - **Subcommand aliases** (`cswap ls`, `rm`, `update`, …) and usage-fetch
    failure classification with WARNING-level logs.
  - **Watch view rides the usage store.** The TUI dashboard drops its
    refresh-interval knob (`+`/`-`): network traffic now follows the
    store's discipline — active account plus at most one due alternate
    per serve-TTL window — and just-refreshed rows flash briefly. `r`
    still forces a full pass.
- **Upstream v0.16.0 merged** (`cswap auto` engine and per-model usage,
  upstream #81/#83 plus the author's alignment fix): `cswap auto` runs a
  UI-agnostic threshold auto-switcher — poll usage, switch proactively to
  the account with the most headroom, quarantine dead refresh-token
  lineages, sleep until the earliest quota reset when everything is
  exhausted — with `--once` cron ticks (outcome in the exit code),
  `--json` event streams, `--dry-run`, and `settings.json` defaults.
  `--list`/`--status` now render per-model weekly windows (e.g. `Fable:`)
  with an `(!)` at-limit marker, integrated into the fork's `ListReporter`
  pipeline and label-aligned across rows. Manual and automatic switches
  (and active-token persists) now hold Claude Code's own advisory locks
  (`~/.claude.lock`, `~/.claude.json.lock`) while writing, so a swap can
  never interleave with a running token refresh. OAuth refresh failures
  are classified (`RefreshOutcome`): dead grants quarantine, network blips
  retry. Upstream's rewritten `--list` rendering and `persist_active`
  hardening were ported into `list_reporter.py`, where the fork keeps
  those paths; the new engine modules were annotated to keep the repo-wide
  `mypy --strict` gate green.
### Changed

- **One decision core.** The fork's monitor loop is retired; the upstream
  `cswap auto` engine is the only auto-switcher. `cswap service install`
  supervises it on all three platforms (same service slot and log
  surfaces), the TUI's **Auto-switch at limit** drives the same engine in
  the foreground, and `cswap --monitor` / `service install --runner` /
  the `auto-switch` subcommand are gone. Engine events are mirrored into
  the structured `claude-swap.log`, so background runs stay observable on
  Windows where `pythonw` has no visible stdout.
- **One settings file.** Auto-switch configuration lives in
  `settings.json` (`autoswitch.threshold` etc.), managed with
  `cswap config`. A one-time migration moves a previously tuned threshold
  out of `sequence.json` and drops the legacy `autoSwitch` section; an
  out-of-range legacy threshold is clamped into the engine's valid range
  with a warning instead of silently discarded.
- **Breaking: `enabled=false` is gone.** The retired monitor honored
  `autoSwitch.enabled=false` as "watch but never switch"; the engine has
  no disabled mode — an installed service switches proactively. The
  migration warns on stderr and in the log when it drops a legacy
  `enabled=false` while a service is installed; run
  `cswap service uninstall` if you don't want proactive switching.
- **Fork Retry-After patch superseded.** The fork's engine-level
  `Retry-After` handling (previously listed here as "capped at 15
  minutes") is retired: upstream's usage store now persists a per-account
  `backoffUntil` honoring `Retry-After`, which replaces it wholesale.
- **Schema-drift warning moved to the shared parser.** An answered usage
  payload with no recognized rate-limit windows logs one structured
  warning from `build_usage_result`, covering the engine, `--list`, and
  the TUI alike.

- **Upstream v0.16.0b1 merged** (`--share-history`, upstream #80 plus the
  author's hardening pass): `cswap run --share-history` links `projects/` and
  `history.jsonl` from `~/.claude` into the session profile so every account
  sees one unified conversation history. History a profile already accumulated
  is merged into `~/.claude` first (skipped while a live session holds the
  profile); stale share manifests can only ever unlink symlinks, never real
  history; seeded history files/dirs match Claude Code's own 0600/0700 modes.
  POSIX-only — the flag is rejected on Windows, where copy-mode sharing would
  fork history instead of sharing it. Merged clean: no fork-side code needed
  changes, and upstream's new session code passes the fork's global
  `mypy --strict` untouched.

### Fixed

- **Redirected Windows output is UTF-8, not the ANSI code page:** piped or
  redirected `cswap` output previously encoded with the locale ANSI code page
  (e.g. cp950), so glyphs like the em dash came out as ANSI multi-byte
  sequences that UTF-8 consumers (editors, CI logs, terminal harnesses)
  render as `�`, and glyphs the page couldn't encode degraded to `?`.
  Redirected streams now emit UTF-8; interactive consoles were and remain
  unaffected (wide-char API).
- **`CLAUDE_CONFIG_DIR` is used verbatim, mirroring Claude Code:** cswap no
  longer tilde-expands the value. Claude Code performs no expansion, so the
  old behavior made cswap manage `$HOME/x` while a self-started Claude Code
  used the literal `./~/x` — swapping credentials Claude Code never read.
  A value starting with a literal `~` now logs a one-time warning instead.
- **Rotated OAuth tokens survive a wedged lock holder:** when persisting a
  just-refreshed single-use token cannot take the file lock within its 30s
  budget, the rotation is parked in a slot-tagged, owner-only pending file
  next to the backup and applied automatically by the next list/status/switch
  pass over the slot. A re-login or import that lands after the park wins over
  the parked rotation. Previously the token was dropped with only a log
  record, degrading the slot to manual re-login.
- **Windows service hardening:**
  - The Task Scheduler watchdog re-fire is anchored to a time trigger, so it
    also covers the logon session the service was installed in — the logon
    trigger alone only armed at the next sign-in.
  - `service install` warns that Task Scheduler cannot forward
    `CLAUDE_CONFIG_DIR` into the service process.
  - Redirected/piped output on Windows degrades to `errors="replace"`, so
    `cswap --list > file` cannot crash on tree glyphs under a cp1252 locale.
  - The WSL keepalive suggestion is now `sleep infinity`, which ships with
    coreutils on the default Ubuntu image — `dbus-launch` (dbus-x11) does
    not.
  - The shared decision log keeps appending when a size-cap rollover is
    refused by a concurrent holder (Windows sharing violation) instead of
    silencing every record after it.
  - The task XML scopes its logon trigger to the installing user
    (`DOMAIN\user`), system binaries resolve under `%SystemRoot%` rather
    than PATH, and a failed task query is no longer misreported as a
    loaded task (it reads as not installed until a query succeeds).
- **Platform hardening (Windows/Linux/WSL review follow-up):**
  - Service-manager output on Windows (PowerShell, schtasks) is decoded
    with the OEM codepage and `errors="replace"`, so localized console
    output (e.g. cp850 `ausgeführt`) can no longer crash a service call
    with `UnicodeDecodeError`; POSIX service calls tolerate stray bytes
    the same way.
  - `FileLock.acquire` retries when opening the lock file itself fails —
    a transient Windows sharing violation (antivirus/indexer) now waits
    like a held lock instead of raising — and opens in append mode so an
    existing lock file is never truncated under another holder's handle.
  - Windows PID liveness treats `OpenProcess` failing with
    `ERROR_ACCESS_DENIED` as alive only after the PID is confirmed
    against the system PID list (psapi `EnumProcesses`) — an elevated
    Claude Code session exists but is unopenable — matching the POSIX
    `PermissionError` rule; previously such sessions read as dead and
    the engine idled.
  - The Task Scheduler XML quotes a `<Command>` path containing spaces
    and no longer `.resolve()`s the interpreter path, so a
    `C:\Program Files` Python and a `--symlinks` venv both launch.
  - systemd units escape `%` as `%%` in ExecStart and Environment
    values, so paths containing `%` survive specifier expansion.
  - `service install` under WSL warns when `CLAUDE_CONFIG_DIR` points at
    `/mnt/<drive>`: Windows-side Claude Code sessions hold Windows PIDs
    a WSL service cannot see.
  - `service install` under WSL also warns when the backup dir sits on
    a `/mnt/<drive>` mount: 9p does not implement file locking, so
    every lock acquire there fails — keep `XDG_DATA_HOME` on the Linux
    filesystem.
  - Windows color support honors a refused `SetConsoleMode`, so legacy
    consoles get plain text instead of bare escape codes.
  - CI: the `windows-task-scheduler` job is blocking and gains a real
    install/start liveness smoke, a new `linux-systemd` job round-trips
    the unit through the runner's real `systemctl --user`, the Windows
    job probes an OEM-codepage decode under `chcp 850`, and
    `mypy --strict --platform win32` covers the win32-only branches.
- **macOS purge sweeps both credential backends:** account removal and
  `--purge` delete Keychain items *and* fallback `.enc` files
  unconditionally, instead of trusting the per-process capability cache to
  know which backend past runs wrote to.
- **`sync_live_to_backup` keeps its never-raises promise:** a busy lock
  (`LockError`) during the live→backup sync is logged and swallowed like
  every other environmental failure, instead of aborting the surrounding
  list/status/switch pass.
- **launchd reinstall rides out the teardown race:** `service install` over a
  loaded agent retries a `bootstrap` that fails with rc=5 — launchd tears the
  previous instance down asynchronously after `bootout`, and an immediate
  bootstrap can land in that window — up to three times, 0.5s apart, instead
  of leaving the agent installed but not loaded. Any other failure still
  surfaces immediately.
- **A consumed rotation survives failed backup verification:** when the
  backup write cannot be verified after a network refresh has already
  consumed the single-use refresh token, the rotation is parked in the
  slot's pending file (the same recovery path as a wedged lock) before the
  error surfaces, instead of being lost with the error.
- **A lost pending-rotation recovery race is quiet:** when two concurrent
  passes race to recover the same parked rotation, the loser's read of the
  already-consumed file logs at debug instead of warning "Discarding
  unreadable pending credential rotation" over a rotation that was in fact
  applied.
- **systemd `service status` shows real state lines:** the status detail
  now comes from `systemctl show -p ActiveState -p MainPID`; the previous
  filter only recognized launchctl vocabulary and dropped every line of
  `systemctl status` output.
- **Engine crash tracebacks reach the log:** the tick/loop safety nets log
  the full traceback into `claude-swap.log` before emitting the error
  event, so a service crash under `pythonw` (no stderr) stays diagnosable.
- **A corrupt quarantine state entry no longer wedges every tick:** a
  non-dict entry in `autoswitch_state.json` is dropped (with an
  unquarantine event) instead of raising before each poll.
- **`cswap auto --json | head` exits cleanly:** a closed stdout pipe stops
  the engine instead of cascading `BrokenPipeError` through the tick guard.

### Changed

- **mypy config collapsed to global strict:** the per-module override list is
  gone; the config-driven run (pre-commit) now enforces exactly what CI does.
  Internal only — no behavior change.
- **Docs drift fixed:** README/CHANGELOG version strings track `pyproject.toml`
  again, CONTRIBUTING's mypy notes match the strict config, and the
  upstream-sync doc points at the `converge/*` branch flow.
- **Test hardening:** the `--import` rollback failure matrix and the service
  backends' status/logs/error surfaces are now covered (transfer 82% → 95%;
  launchd/systemd/task_scheduler 98–100% line coverage).
- **Author-style convergence:** fork-only modules carry contract docstrings in
  upstream's voice, spellings follow upstream's American convention, and
  `switcher.py`'s indentation, docstrings, and import prologue were re-aligned
  with upstream — shrinking the shared-file diff against `upstream/main` by
  ~240 lines. Comments/docs only, no behavior change.
- **Test suite reorganized:** the two largest test files were split by
  feature (switch path, broken-slot resilience, add-account, org migration,
  purge) and the obvious copy-paste tables converted to
  `pytest.mark.parametrize`. No assertion was removed; coverage is
  unchanged.
- **CI registers a real scheduled task on Windows:** a new
  `windows-task-scheduler` job round-trips the production task XML through
  `Register-ScheduledTask` under a run-unique name (no `Start-ScheduledTask`)
  and asserts it queries back as loaded before unregistering. Marked
  `continue-on-error` until its first green run.

## [0.15.1+haotool.1] — 2026-07-03

### Changed

- **Upstream v0.15.1 merged** (plus upstream `main` as of 2026-07-03):
  - OS-native TLS trust via `truststore` (upstream #78), fixing
    inactive-account token refresh behind corporate/AV-intercepted TLS,
    which the fork's background monitor is especially exposed to.
  - `--switch-to` onto the current account is a no-op; `--force` restores the
    stored credentials over the live login (upstream #79 design, superseding
    the fork's interim same-slot guard). `--import` over the live login now
    hints at `--switch-to <slot> --force`.
  - Upstream Windows test fixes adopted; the Windows CI job is now blocking.
- **`SequenceStore` extracted:** `sequence.json` state is now a typed, immutable
  model (`SequenceData` / `AccountRecord` / `AutoSwitchConfig`) behind a
  lock-agnostic store, replacing raw-dict access throughout the account
  add/remove/switch/auto-switch paths in `switcher.py`. On-disk shape is
  unchanged (unknown/future keys and key presence preserved). Internal refactor
  only — no user-facing behavior change. The new module is mypy-strict; the
  broader `switcher` typing debt is now also resolved (see below).
- **`switcher` is now mypy-strict:** the `ignore_errors` carve-out for
  `claude_swap.switcher` was removed after typing its `sequence.json`,
  usage-cache, and config-dict flows and fixing the `ListHost` /
  `SwitchCliHost` protocol signatures. Internal typing only — no behavior change.
- **Whole package is now mypy-strict:** `switch_cli` and `tui` were typed too
  (a `_RotationParams` TypedDict, precondition None-narrowing, and `curses.window`
  annotations), removing the last `ignore_errors` entry — all 35 modules now pass
  `mypy --strict`. Typing only — no behavior change.
- **Switch paths converged:** the CLI strategy dispatch and the switcher now
  share one switch-decision implementation, and four test-only shims were
  removed from production code. Internal refactor — no behavior change.

### Fixed

- **Cross-account backup poisoning on macOS:** an active-credential read that
  fell back to a leftover plaintext file while the Keychain was locked is
  classified as degraded and is never synced into the active slot's backup —
  the file may hold another account's tokens.
- **`--import --force` onto the active slot** is no longer silently undone by
  the next `--list`/`--status` live→backup sync: the sync skips when the
  backup is at least as new as the live credential (import-wins, #79
  semantics).
- **Rotated single-use tokens under lock contention:** the persist waits out
  legitimate lock holders (30s budget) and re-checks the slot's refresh-token
  lineage under the lock, so contention no longer drops the only working
  credential and a mid-refresh re-login is never clobbered.
- **Monitor PID-file acquisition race:** the stale-file path re-verifies the
  PID file still holds the dead PID before unlinking it, so two monitors can
  no longer both win the singleton; the PID holder is identified by an argv
  fingerprint instead of loose substrings, eliminating recycled-PID false
  positives.
- **Windows Task Scheduler service:** the task XML drops the schema-invalid
  `EnvironmentVariables` block (service mode travels on argv), overrides the
  hostile schema defaults (72h `ExecutionTimeLimit`, battery kill switches),
  and repeats the logon trigger every five minutes with `IgnoreNew` so a dead
  monitor is restarted while a healthy one is never disturbed.
- **WSL keepalive guidance** now suggests `dbus-launch true` — a command that
  actually leaves a resident process holding the WSL instance open.
- **Usage cache after account removal:** removing a slot reclaims its
  `usage.json` row, and cache freshness compares the managed subset instead of
  requiring exact key-set equality — the 15s TTL cache works again after any
  removal, and the auto-switch refresh gate no longer refetches
  already-answered slots every cycle.
- **Keychain-unavailable classification** is preserved across the list/status
  facades, so a locked Keychain shows as unavailable instead of
  "no credentials".
- **Credential write verification** checks the store the write actually landed
  on, and `add_account` re-resolves the slot under the lock; the OAuth
  User-Agent is bound to the package version.

## [0.15.0b2+haotool.1] — 2026-06-28

### Added

- **Auto-switch at usage limit (Beta):** TUI menu, `cswap --monitor`, and macOS launchd background service. Fail-closed target selection from trusted usage snapshots.
- **`cswap --health`:** account health, usage, and OAuth token status.
- **TUI Watch:** live status and usage dashboard (in-place rendering aligned with upstream).
- **Upstream sync:** `--json`, `--strategy`, `assume_yes`, managed API-key accounts.

### Changed

- **Credential layer:** `CredentialStore`, `CredentialRefresher`, and usage-cache codec extracted from switcher; aligned with upstream Keychain routing.
- **Switch paths:** cooldown-aware auto-switch planning, SwitchIntent types, OAuth verify-on-activation.

### Fixed

- OAuth inactive-account refresh serialized under FileLock.
- Per-window usage tracking so monitor velocity is not masked by saturated holds.
- macOS Keychain account name and `.enc`-wins backup reconciliation aligned with upstream.

### Upgrade notes

Auto-switch users: see [0.13.1] breaking changes and README failure-modes section before enabling monitor/service.

## [0.13.1] — 2026-06-14

Historical note: auto-switch, `--health`, and related fork features ship in **[0.15.0b2+haotool.1]** above.

### Fixed

- **Session mode on Windows:** session validation now resolves `claude` via `shutil.which` so `.cmd` shims are found (upstream PR #54).

### Breaking — `switch()` API (auto-switch beta)

Automated switching now uses explicit **SwitchIntent** types instead of boolean kwargs.

| Before | After |
|--------|-------|
| `switch(quiet=True)` | `switch(BackgroundAutoSwitchIntent(decision=...))` |
| `switch(prefer_least_busy=True)` | `switch(InteractiveAutoSwitchIntent(decision=...))` or `BackgroundAutoSwitchIntent(...)` with a decision from `build_auto_switch_decision()` |
| `switch()` returned `None` | `switch()` returns `bool` — `True` when credentials changed, `False` when no switch was needed |

**Automated switching is fail-closed.** When usage snapshots are cold or expired, the monitor will not round-robin blindly; it logs `no trusted usage snapshots` and holds until cache is warm. Manual `cswap --switch` still uses round-robin.

### Upgrade steps (auto-switch users)

1. **Before upgrading:** run `cswap --list` on every machine with auto-switch enabled (seeds per-slot `_cached_at` snapshots).
2. **After upgrading:** run `cswap service install` (macOS background users), then `cswap service status`.
3. **External callers** of `ClaudeAccountSwitcher.switch()`: pass a `SwitchIntent` and handle the `bool` return value. Import intents from `claude_swap.models`.

See also the [Failure modes and upgrade](README.md#failure-modes-and-upgrade) section in README.
