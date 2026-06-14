# Plan 012: Replace brittle TUI menu KEY_DOWN counts with `_select_from` patching

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 331798e..HEAD -- tests/test_auto_switch.py src/claude_swap/tui.py`
> If either file changed since this plan was written, re-read the
> exemplar test (`test_set_threshold_via_prompt`) and the menu-building
> code in `tui._do_auto_switch` before editing; on material drift, STOP.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `331798e`, 2026-06-14

## Why this matters

Several TUI auto-switch menu tests assert behavior by counting `KEY_DOWN`
presses to land on a menu item (e.g. "idx 2 for Install", "idx 3 for
Show status"). Any reordering of menu items, addition of a new option, or
platform-conditional disable silently breaks these tests with a misleading
failure message — the assertion fires on the *wrong* menu action.

The same file (`tests/test_auto_switch.py:246-257`) already contains the
exemplar of the correct, label-driven pattern: patch
`claude_swap.tui._select_from` with a `side_effect` that returns the
intended value(s) directly. That pattern is **label-driven**, not
**index-driven**, so menu reordering no longer breaks unrelated tests.

Plan 009 explicitly flagged this as a remaining P2 (brittle TUI menu
KEY_DOWN tests). This plan closes it.

## Current state

### The exemplar — keep, don't change

`tests/test_auto_switch.py:246-257` (lines may shift slightly if other
edits land first; confirm by reading the file):

```python
def test_set_threshold_via_prompt(self, temp_home: Path):
    switcher = ClaudeAccountSwitcher()
    screen = stub_screen()
    # Select by menu value, not KEY_DOWN index — survives label reordering.
    screen.getch.side_effect = [ord("8"), ord("0"), 10, 27]
    with patch("claude_swap.tui.curses.curs_set"), \
         patch(
             "claude_swap.tui._select_from",
             side_effect=["threshold", None],
         ):
        tui._do_auto_switch(screen, switcher)
    assert switcher.get_auto_switch_config()["threshold"] == 80
```

### The brittle tests to convert

All in `tests/test_auto_switch.py` (line numbers are guides; verify via
the in-file headers `test_service_*`):

1. `test_service_toggle_installs_on_macos` (around line 259-274):

```python
def test_service_toggle_installs_on_macos(self, temp_home: Path):
    switcher = ClaudeAccountSwitcher()
    screen = stub_screen()
    # Down to "Background service: Install" (idx 2) + Enter, then Esc.
    screen.getch.side_effect = [tui.curses.KEY_DOWN, tui.curses.KEY_DOWN, 10, 27]

    with patch("claude_swap.tui.sys.platform", "darwin"), \
         patch("claude_swap.tui._service_state", return_value="not installed"), \
         patch("claude_swap.tui._shell_out") as mock_shell:
        tui._do_auto_switch(screen, switcher)

    _stdscr_arg, fn = mock_shell.call_args.args
    assert _stdscr_arg is screen
    with patch("claude_swap.tui.service.install", return_value=0) as mock_install:
        fn()
    mock_install.assert_called_once_with(switcher)
```

2. `test_service_toggle_uninstalls_on_macos` (around 276-290) — same shape, 2 `KEY_DOWN`s.
3. `test_service_status_shells_out` (around 292-311) — 3 `KEY_DOWN`s.
4. `test_service_status_shows_error_off_macos` (around 313-331) — 3 `KEY_DOWN`s.
5. `test_service_toggle_shows_error_off_macos` (around 333-343) — 2 `KEY_DOWN`s.

### How `_select_from` is called from `_do_auto_switch`

Read `src/claude_swap/tui.py` around `_do_auto_switch` and find the
`_select_from(...)` call(s). Identify the **label string** the menu uses
to identify each action (e.g. the label or sentinel value returned by
`_select_from` for the service-install branch). Confirm the actual label
names by grepping for the strings in `tui.py`:

```bash
grep -n "service\|Install\|Uninstall\|Show status\|_select_from" src/claude_swap/tui.py
```

Use the label values you find there as the `side_effect` returns in the
converted tests. **If the labels do not exist** (the dispatch uses a
different sentinel like `"service_install"`), use those sentinels instead
— the exemplar at line 254 uses `"threshold"`.

## Commands you will need

| Purpose         | Command                                              | Expected on success |
|-----------------|------------------------------------------------------|---------------------|
| Drift check     | `git diff --stat 331798e..HEAD -- tests/test_auto_switch.py src/claude_swap/tui.py` | inspect; no unexpected churn |
| KEY_DOWN audit  | `grep -c "KEY_DOWN" tests/test_auto_switch.py`       | 10 before, ≤3 after |
| Discover labels | `grep -n "_select_from\|service\|Install" src/claude_swap/tui.py` | shows menu strings |
| Tests           | `python -m pytest -q tests/test_auto_switch.py`      | unchanged pass count |
| Full suite      | `python -m pytest -q`                                | 652 passed, 3 skipped |

## Scope

**In scope** (the only files you should modify):

- `tests/test_auto_switch.py` — the 5 brittle tests listed above.

**Out of scope** (do NOT touch):

- `src/claude_swap/tui.py` — the production code is correct; this plan
  changes test mechanics only.
- Any other test file.
- `tests/test_tui.py` — its `KEY_DOWN` uses (wrap-around behavior tests)
  are testing the navigation itself, which is a different concern.
- The exemplar `test_set_threshold_via_prompt` — already correct.
- Other tests in `test_auto_switch.py` that legitimately use `KEY_DOWN`
  for navigation behavior (if any).

## Git workflow

- Branch: stay on `feat/auto-switch-on-limit`.
- Single commit. Suggested message:
  `test(tui): replace brittle KEY_DOWN counts with label-driven _select_from patches`
- Do NOT push.

## Steps

### Step 1: Discover the menu sentinel values

```bash
grep -n "_select_from" src/claude_swap/tui.py
```

Read each `_select_from` call inside `_do_auto_switch` (or any helper it
delegates to) to see what the menu's `options` argument actually looks
like. Note the sentinel values for: install / uninstall / status (macOS
present and absent platform variants).

Likely shape (verify in code — names may differ): `_select_from(...)`
returns a string label or a stable sentinel from a `(label, sentinel)`
tuple list. If labels and sentinels diverge, use the sentinel (it's the
stable contract).

If the menu uses an Enum or constants for sentinels, import them in the
test file as already done for `tui` (`from claude_swap import tui`).

### Step 2: Convert each test

For each of the 5 tests, rewrite the body following the exemplar shape.

Template (adapt sentinel names to what you discover in Step 1):

```python
def test_service_toggle_installs_on_macos(self, temp_home: Path):
    switcher = ClaudeAccountSwitcher()
    screen = stub_screen()
    # Esc closes the menu after the selected action's callback returns.
    screen.getch.side_effect = [27]

    with patch("claude_swap.tui.sys.platform", "darwin"), \
         patch("claude_swap.tui._service_state", return_value="not installed"), \
         patch(
             "claude_swap.tui._select_from",
             side_effect=[<install_sentinel>, None],
         ), \
         patch("claude_swap.tui._shell_out") as mock_shell:
        tui._do_auto_switch(screen, switcher)

    _stdscr_arg, fn = mock_shell.call_args.args
    assert _stdscr_arg is screen
    with patch("claude_swap.tui.service.install", return_value=0) as mock_install:
        fn()
    mock_install.assert_called_once_with(switcher)
```

Replace `<install_sentinel>` with the actual value found in Step 1
(e.g. `"service_install"` or `tui.MENU_SERVICE_INSTALL` — whatever the
production code uses).

Apply the same pattern to the other 4 tests, swapping in the right
sentinel and any per-test platform/service-state patches.

For `test_service_toggle_shows_error_off_macos` and
`test_service_status_shows_error_off_macos`: the sentinel should still
be the install/status one — the test now asserts that *with that
selection*, the off-macOS branch routes to `_show_message` instead of
`_shell_out`.

### Step 3: Verify KEY_DOWN audit drops

```bash
grep -c "KEY_DOWN" tests/test_auto_switch.py
```

Expected: drop from **10** to **≤ 3** (or 0 if the remaining hits were
all in the converted tests). Any remaining `KEY_DOWN` must belong to a
test that genuinely covers navigation behavior — record which test in the
commit message.

### Step 4: Run targeted then full tests

```bash
python -m pytest -q tests/test_auto_switch.py -k "service"
python -m pytest -q
```

Expected: same pass counts as before this plan (the 5 converted tests
still pass, plus everything else).

## Test plan

No new tests are added by this plan — only existing tests are restructured.
After the change, **each converted test should still fail if the
production behavior breaks** but should **not** fail if menu order changes.

Sanity check (manual, optional): temporarily reorder one menu option in
`tui.py`, run the converted tests, confirm they still pass. Revert the
reorder. (Do not commit the reorder.)

## Done criteria

- [ ] `grep -c "KEY_DOWN" tests/test_auto_switch.py` returns ≤ 3.
- [ ] All 5 service tests in `test_auto_switch.py` patched via
      `_select_from`, not `KEY_DOWN` counting.
- [ ] `python -m pytest -q` reports 652 passed, 3 skipped.
- [ ] `git diff --stat` shows exactly one file changed:
      `tests/test_auto_switch.py`.
- [ ] `plans/README.md` status row for plan 012 updated to DONE.

## STOP conditions

Stop and report if:

- `_select_from` is not the function dispatched from `_do_auto_switch`
  (the production code uses a different selector) — re-read `tui.py`
  before converting tests against a wrong patch path.
- After conversion, the converted tests pass but a previously-passing
  test elsewhere starts failing — revert and investigate; the menu may
  share state in an unexpected way.
- The "Discover labels" step finds menu items that don't have a stable
  sentinel (i.e. the dispatch is *entirely* by index, no labels) — this
  is a production-code finding, not a test fix; STOP and report.

## Maintenance notes

- For any future TUI menu test: copy the exemplar at
  `tests/test_auto_switch.py:test_set_threshold_via_prompt`. Reach for
  `KEY_DOWN` only when testing navigation behavior itself.
- If `_select_from`'s API ever changes (e.g. label vs. sentinel split,
  multi-select), this set of tests is the central place to update.
