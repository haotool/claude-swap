# Changelog

All notable user-facing changes to claude-swap are documented here.

Release version is defined in `pyproject.toml` (currently `0.15.1+haotool.1`).

## [Unreleased]

### Changed

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
