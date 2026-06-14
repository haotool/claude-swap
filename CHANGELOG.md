# Changelog

All notable user-facing changes to claude-swap are documented here.

## [Unreleased]

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
