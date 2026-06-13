# Plan 004: Surface multi-account health + usage in the TUI (reusing the CLI renderer)

> **Executor instructions**: Follow each step, verify before proceeding, stop
> and report on any "STOP condition". Update this plan's row in
> `plans/README.md` when done.
>
> **Drift check (run first)**:
> `git diff --stat <SHA-after-plan-001>..HEAD -- src/claude_swap/tui.py src/claude_swap/switcher.py`
> Plan 001 must be DONE (the rebase changes how credentials are read). Re-read
> the excerpts below against the live `tui.py`; mismatch ⇒ STOP.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW — additive TUI menu entry that reuses an existing renderer.
- **Depends on**: `plans/001-sync-fork-onto-upstream.md` (recommended after
  002/003 but not blocked by them)
- **Category**: feature
- **Planned at**: commit `d3b86d2`, 2026-06-13

## Why this matters

The CLI already has a rich multi-account health view: `cswap --health` prints
every managed account with its 5h/7d usage, an `ok`/notes health line, and
OAuth token status. The **TUI cannot show it**. The TUI's "List accounts" entry
shells out to `switcher.list_accounts()` *without* the health flags, and its
account pickers (`_account_items`) deliberately omit usage to stay snappy. So a
TUI-only user can't see, in one place, which of their accounts are healthy,
near their limit, or holding an expired token.

This plan adds a dedicated **"Account health & usage"** TUI entry that reuses
the **existing** `list_accounts(show_token_status=True, show_health=True)`
renderer — no formatting is re-implemented in curses, honoring the TUI's stated
design rule ("never re-implements account logic — every action shells out").
The single CLI renderer stays the SSOT for how usage/health is displayed.

## Current state

**The renderer to reuse already exists** — `switcher.list_accounts` at
`switcher.py:1324`:

```python
def list_accounts(self, show_token_status: bool = False,
                  show_health: bool = False) -> None:
    ...
    # prints per account: "N: email [tag] (active)",
    #   usage lines (5h/7d), and when show_health: "• health: ok|<notes>",
    #   and when show_token_status: "• <token status>"
```

`cli.py:226` already calls it for `--health`:
```python
elif args.health:
    switcher.list_accounts(show_token_status=True, show_health=True)
```

**The TUI today** (`tui.py:66-100`, the main menu) offers:
```python
items = [
    ("Switch account", "switch"),
    ("Add account", "add"),
    ("Remove account", "remove"),
    ("Refresh credentials (current login, in-place)", "refresh"),
    ("List accounts (with usage)", "list"),     # -> switcher.list_accounts()
    ("Status", "status"),
    ("Auto-switch at limit (Beta)", "auto"),
    ("Quit", "quit"),
]
...
elif choice == "list":
    _shell_out(stdscr, lambda: switcher.list_accounts())
```

`_shell_out` (`tui.py:512`) suspends curses, runs the callable with normal
stdout, pauses for a keypress, then restores curses — exactly the right vehicle
for a full-color multi-line report.

**The compact status header** `_status_line` (`tui.py:355`) is intentionally
pure-local (no network) — keep it that way; this plan adds a separate on-demand
health view rather than slowing the menu with a network call on every redraw.

**Convention**: menu items are `(label, value)` tuples dispatched in
`_main_loop`; health/usage display logic stays in `switcher`, the TUI only
chooses what flags to pass.

## Commands you will need

| Purpose | Command | Expected |
|---------|---------|----------|
| Run tests | `python -m pytest -q` | all pass |
| TUI tests | `python -m pytest tests/test_tui.py -q` | all pass |
| Parse check | `python -c "import claude_swap.tui"` | exit 0 |

## Scope

**In scope**:
- `src/claude_swap/tui.py` — add the "Account health & usage" menu entry and its
  dispatch; clarify the existing "List accounts" label.
- `tests/test_tui.py` — assert the new entry shells out with the health flags.

**Out of scope**:
- `src/claude_swap/switcher.py` — reuse `list_accounts` as-is; do NOT add a new
  rendering method or change its output.
- `_status_line` / `_account_items` — leave pure-local (no network in the menu
  or pickers). Do not add usage% to the switch/remove pickers.
- Any new network/caching code — `list_accounts` already caches usage
  (`_USAGE_CACHE_TTL`); rely on it.

## Steps

### Step 1: Add the menu entry

In `tui.py` `_main_loop`, insert a new item after the existing "List accounts"
line and relabel the old one for clarity:

```python
("List accounts (quick)", "list"),
("Account health & usage", "health"),
```

(The "quick" list stays `switcher.list_accounts()` with no flags; the new entry
is the detailed health view.)

**Verify**: `grep -n "Account health & usage" src/claude_swap/tui.py` matches.

### Step 2: Dispatch it through the existing renderer

In the `try/elif` dispatch block of `_main_loop` (next to the `choice == "list"`
branch), add:

```python
elif choice == "health":
    _shell_out(
        stdscr,
        lambda: switcher.list_accounts(
            show_token_status=True,
            show_health=True,
        ),
    )
```

**Verify**: `python -c "import claude_swap.tui"` exits 0.

### Step 3: Test the wiring

In `tests/test_tui.py`, follow the existing pattern for testing a menu choice
(the file already drives `_main_loop` with a fake `stdscr` and a mock switcher).
Add a test that selecting "health" calls
`switcher.list_accounts(show_token_status=True, show_health=True)` exactly once.
If the existing tests assert against `_shell_out`, mock it and assert the
lambda, when invoked, calls `list_accounts` with both flags `True`.

**Verify**: `python -m pytest tests/test_tui.py -q` → all pass including the new
test.

## Test plan

- New: one test in `tests/test_tui.py` asserting the "Account health & usage"
  entry invokes `switcher.list_accounts(show_token_status=True,
  show_health=True)`. Pattern: the existing menu-dispatch tests in that file.
- Regression: the "List accounts (quick)" entry still calls
  `list_accounts()` with no flags.
- Verification: `python -m pytest -q` → all pass.

## Done criteria

ALL must hold:

- [ ] The TUI main menu contains an "Account health & usage" entry that shells
      out to `list_accounts(show_token_status=True, show_health=True)`.
- [ ] The quick "List accounts" entry is unchanged in behavior
      (`list_accounts()` no flags).
- [ ] No new rendering/formatting code added to `switcher.py`
      (`git diff` touches only `tui.py` + `tests/test_tui.py`).
- [ ] `python -m pytest -q` exits 0; new TUI test passes.
- [ ] `_status_line` and `_account_items` still make no network calls
      (`git diff` shows them unchanged).
- [ ] `plans/README.md` status row updated.

## STOP conditions

Stop and report if:

- After plan 001's rebase, `list_accounts`'s signature no longer accepts
  `show_health` / `show_token_status` — report the new signature.
- `tests/test_tui.py` has no existing pattern for asserting a menu dispatch
  (so you'd have to invent test infrastructure) — report; a thin harness may be
  acceptable but the reviewer should approve the approach.

## Maintenance notes

- Because the TUI reuses `list_accounts`, any future change to the usage/health
  display (columns, color, new fields) automatically appears in both `cswap
  --health` and the TUI — that's the intended SSOT. Keep it that way; resist
  adding a curses-native table that would fork the formatting.
- Deferred (possible follow-up): an in-curses, auto-refreshing dashboard of all
  accounts' usage. That *would* re-implement rendering and needs its own plan +
  a decision on the network-refresh cadence; out of scope here.
- A reviewer should confirm the new view is responsive enough — it makes the
  same network calls as `cswap --health`, served from the 15s usage cache.
