# Changelog

All notable user-facing changes to claude-swap are documented here.

## [Unreleased]

## [0.15.0b2-haotool.1] — 2026-06-28

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
