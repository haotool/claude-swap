# Changelog

All notable user-facing changes to claude-swap are documented here.

## [Unreleased]

## [0.15.0b2] — 2026-06-27

### Added

- **Upstream sync:** `--json` machine-readable output, `--strategy` for targeted switching, and `assume_yes` on destructive prompts.
- **TUI in-place + Watch:** menu actions render in-place; live Watch dashboard for usage velocity (fork retains Account health and Auto-switch menus).
- **Managed API-key accounts:** `cswap --add-token` can register `sk-ant-api` managed accounts (plan 021).
- **`auto_switch_planning` module:** slot scoring and automated switch planning extracted from `switcher.py` (plan 022 track 5).

### Changed

- **Credential layer:** `CredentialStore` and `CredentialRefresher` extracted from switcher; usage-cache codec moved to `usage_cache.py` (plans 018–020).
- **Switch/purge paths:** fresh-machine target resolution, switch/purge phase decomposition, and `status_payload()` JSON SSOT (plan 022 track 6).
- **Fork-only modules:** monitor, service, credential_refresh, and usage_cache aligned to upstream comment/style conventions (−147 LOC, plan 022 track 2).

### Fixed

- **OAuth refresh:** inactive-account refresh serialized under FileLock (plan 017).
- **Monitor:** per-window usage tracking so velocity polling is not masked by saturated holds.
- **Lint:** remove unused imports in `switcher.py`.

### Upgrade notes

- Fork is **96 commits ahead, 0 behind** upstream `realiti4/claude-swap` at this tag — all upstream features are included plus auto-switch Beta, `cswap service`, and `--health`.
- Auto-switch users: see [0.13.1] breaking changes and README failure-modes section before enabling monitor/service on a fresh upgrade.

## [0.13.1] — 2026-06-14

### Added

- **Auto-switch at usage limit (Beta):** TUI menu, `cswap --monitor`, and macOS launchd background service. Fail-closed target selection from trusted usage snapshots; manual `cswap --switch` still uses round-robin.
- **`cswap --health`:** account health, usage, and OAuth token status.

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
