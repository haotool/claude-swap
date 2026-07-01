"""Curses-based interactive TUI for claude-swap.

Activated via ``cswap --tui``. Provides a single-level arrow-key menu over
the existing CLI commands, so users don't have to memorize flags.

The TUI never re-implements account logic — every action shells out to the
existing ``ClaudeAccountSwitcher`` methods. It exists purely as a navigation
layer.

Design constraints:
    * Zero new runtime dependencies (uses stdlib ``curses``).
    * Falls back gracefully when terminal is too small or curses is missing.
    * After each action, returns to the main menu (does not auto-exit).
"""

from __future__ import annotations

import contextlib
import curses
import io
import os
import re
import sys
import time
from datetime import datetime
from typing import Callable

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.models import InteractiveAutoSwitchIntent
from claude_swap import printer, service
from claude_swap.monitor import (
    MONITOR_POLL_SECONDS,
    MONITOR_POLL_SECONDS_MIN,
    MonitorRuntimeState,
    SwitchCancelled,
    active_usage_display,
    acquire_pid,
    get_logger,
    monitor_step,
    release_pid,
)
from claude_swap.switcher import ClaudeAccountSwitcher, auto_switch_display

_acquire_monitor_pid = acquire_pid
_release_monitor_pid = release_pid


# Minimum terminal size we render in. Below this, we bail to plain CLI advice.
_MIN_ROWS = 12
_MIN_COLS = 60

_STYLE_BOLD = 1
_STYLE_DIM = 2
_STYLE_RED = 4
_STYLE_YELLOW = 8
_STYLE_ACCENT = 16
_STYLE_MUTED = 32

# SGR parameter string (between ESC[ and m) → style flag. Each printer token is
# reset-terminated, so flags accumulate within a token and clear on reset.
_SGR_PARAM_TO_STYLE: dict[str, int] = {
    "1": _STYLE_BOLD,
    "2": _STYLE_DIM,
    "31": _STYLE_RED,
    "33": _STYLE_YELLOW,
    "38;5;173": _STYLE_ACCENT,  # printer accent (warm salmon)
    "38;5;250": _STYLE_MUTED,   # printer muted (soft gray)
}

_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def _ansi_segments(text: str) -> list[tuple[str, int]]:
    """Split a line into ``(visible_text, style_flags)`` runs."""
    segments: list[tuple[str, int]] = []
    flags = 0
    pos = 0
    for m in _SGR_RE.finditer(text):
        if m.start() > pos:
            segments.append((text[pos:m.start()], flags))
        param = m.group(1)
        if param in ("", "0"):
            flags = 0
        else:
            flags |= _SGR_PARAM_TO_STYLE.get(param, 0)
        pos = m.end()
    if pos < len(text):
        segments.append((text[pos:], flags))
    return [(t, f) for (t, f) in segments if t != ""] or [("", 0)]


def _clamp_interval(n: int, lo: int = 1, hi: int = 60) -> int:
    """Clamp a watch refresh interval (seconds) into ``[lo, hi]``."""
    return max(lo, min(hi, n))


_PAIR_RED = 1
_PAIR_YELLOW = 2
_PAIR_ACCENT = 3
_PAIR_MUTED = 4

_colors_initialized = False


def _init_colors() -> None:
    """Initialize curses color pairs once. Safe on no-color terminals."""
    global _colors_initialized
    if _colors_initialized:
        return
    _colors_initialized = True
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(_PAIR_RED, curses.COLOR_RED, bg)
        curses.init_pair(_PAIR_YELLOW, curses.COLOR_YELLOW, bg)
        if getattr(curses, "COLORS", 0) >= 256:
            curses.init_pair(_PAIR_ACCENT, 173, bg)
            curses.init_pair(_PAIR_MUTED, 250, bg)
        else:
            curses.init_pair(_PAIR_ACCENT, curses.COLOR_YELLOW, bg)
            curses.init_pair(_PAIR_MUTED, curses.COLOR_WHITE, bg)
    except curses.error:
        pass


def _style_to_attr(flags: int) -> int:
    """Resolve our style flags to a curses attribute int. Never raises."""
    attr = curses.A_NORMAL
    if flags & _STYLE_BOLD:
        attr |= curses.A_BOLD
    if flags & _STYLE_DIM:
        attr |= curses.A_DIM
    try:
        if not curses.has_colors():
            return attr
        if flags & _STYLE_RED:
            attr |= curses.color_pair(_PAIR_RED)
        elif flags & _STYLE_YELLOW:
            attr |= curses.color_pair(_PAIR_YELLOW)
        elif flags & _STYLE_ACCENT:
            attr |= curses.color_pair(_PAIR_ACCENT)
        elif flags & _STYLE_MUTED:
            attr |= curses.color_pair(_PAIR_MUTED)
    except curses.error:
        pass
    return attr


def _addstr_ansi(stdscr, y: int, x: int, text: str, max_width: int) -> None:
    """Draw an ANSI-styled line clipped to ``max_width`` visible characters."""
    remaining = max_width
    cx = x
    for seg_text, flags in _ansi_segments(text):
        if remaining <= 0:
            break
        chunk = seg_text[:remaining]
        try:
            stdscr.addstr(y, cx, chunk, _style_to_attr(flags))
        except curses.error:
            pass
        cx += len(chunk)
        remaining -= len(chunk)


def run(switcher: ClaudeAccountSwitcher) -> int:
    """Entry point for ``cswap --tui``. Returns process exit code."""
    try:
        return curses.wrapper(_main_loop, switcher)
    except _ExitRequested:
        return 0


# Main menu loop
class _ExitRequested(Exception):
    """Control-flow signal to exit the curses wrapper cleanly (not a user error)."""


def _main_loop(stdscr: "curses._CursesWindow", switcher: ClaudeAccountSwitcher) -> int:
    rows, cols = stdscr.getmaxyx()
    if rows < _MIN_ROWS or cols < _MIN_COLS:
        curses.endwin()
        sys.stderr.write(
            f"Terminal too small for TUI ({rows}x{cols}, need at least "
            f"{_MIN_ROWS}x{_MIN_COLS}). Use the regular CLI flags instead.\n"
        )
        return 2

    curses.curs_set(0)  # hide cursor
    _init_colors()
    has_token_flow = hasattr(switcher, "add_account_from_token")

    while True:
        items: list[tuple[str, str]] = [
            ("Switch account", "switch"),
            ("Add account", "add"),
            ("Remove account", "remove"),
            ("Refresh credentials (current login, in-place)", "refresh"),
            ("List accounts (with usage)", "list"),
            ("Account health & usage", "health"),
            ("Status", "status"),
            ("Watch (live status + usage)", "watch"),
            ("Auto-switch at limit (Beta)", "auto"),
            ("Quit", "quit"),
        ]
        choice = _select_from(
            stdscr,
            title="claude-swap",
            subtitle=_status_line(switcher),
            items=items,
        )
        if choice in (None, "quit"):
            return 0

        try:
            if choice == "switch":
                _do_switch(stdscr, switcher)
            elif choice == "add":
                _do_add(stdscr, switcher, has_token_flow)
            elif choice == "remove":
                _do_remove(stdscr, switcher)
            elif choice == "refresh":
                _do_refresh(stdscr, switcher)
            elif choice == "list":
                _run_inline(stdscr, "Accounts", lambda: switcher.list_accounts())
            elif choice == "health":
                _run_inline(
                    stdscr,
                    "Account health & usage",
                    lambda: switcher.list_accounts(
                        show_token_status=True,
                        show_health=True,
                    ),
                )
            elif choice == "status":
                _run_inline(stdscr, "Status", switcher.status)
            elif choice == "watch":
                _do_watch(stdscr, switcher)
            elif choice == "auto":
                _do_auto_switch(stdscr, switcher)
        except ClaudeSwitchError as e:
            _show_message(stdscr, f"Error: {e}", is_error=True)
        except KeyboardInterrupt:
            _show_message(stdscr, "Operation cancelled.")


# Sub-flows
def _do_switch(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    items = _account_items(switcher)
    if not items:
        _show_message(stdscr, "No managed accounts. Add one first.")
        return
    items.append(("-- Cancel --", None))
    choice = _select_from(stdscr, "switch to", items=items)
    if choice is None:
        return
    _run_inline(stdscr, "Switch account", lambda: switcher.switch_to(choice))


def _do_add(stdscr, switcher: ClaudeAccountSwitcher, has_token_flow: bool) -> None:
    items: list[tuple[str, str]] = [
        ("From current Claude Code login   (cswap --add-account)", "login"),
    ]
    if has_token_flow:
        items.append(
            ("From a setup-token              (cswap --add-token)", "token")
        )
    items.append(("-- Cancel --", None))

    choice = _select_from(stdscr, "add account", items=items)
    if choice is None:
        return

    if choice == "login":
        _shell_out(stdscr, switcher.add_account)
        return

    # choice == "token"
    email = _prompt_text(stdscr, "Email for this token: ")
    if not email:
        return
    token = _prompt_text(stdscr, "Setup token: ", password=True)
    if not token:
        return
    _shell_out(
        stdscr,
        lambda: switcher.add_account_from_token(token=token, email=email, slot=None),
    )


def _do_remove(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    items = _account_items(switcher)
    if not items:
        _show_message(stdscr, "No managed accounts.")
        return
    items.append(("-- Cancel --", None))
    choice = _select_from(stdscr, "remove which account?", items=items)
    if choice is None:
        return
    if not _confirm(stdscr, f"Remove account {choice}? Type 'y' to confirm: "):
        return
    _run_inline(
        stdscr,
        "Remove account",
        lambda: switcher.remove_account(choice, assume_yes=True),
    )


def _do_refresh(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    identity = switcher._get_current_account()
    if identity is None:
        _show_message(
            stdscr,
            "No active Claude Code login detected. Log in first, then retry.",
            is_error=True,
        )
        return
    email, _org = identity
    _shell_out(stdscr, lambda: switcher.add_account(slot=None))


def _do_watch(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    """Live, auto-refreshing dashboard of status + usage (read-only)."""
    seq = switcher._get_sequence_data() or {}
    if not seq.get("accounts"):
        _show_message(stdscr, "No managed accounts to watch. Add one first.")
        return
    _watch_loop(stdscr, switcher)


def _watch_loop(stdscr, switcher: ClaudeAccountSwitcher, interval: int = 5) -> None:
    """Re-capture ``list_accounts()`` every ``interval`` seconds and redraw."""
    stdscr.timeout(250)
    try:
        last_refresh = 0.0
        body: list[str] = []
        while True:
            now = time.monotonic()
            if now - last_refresh >= interval:
                body = _capture(lambda: switcher.list_accounts()).splitlines()
                now = time.monotonic()
                last_refresh = now

            stdscr.erase()
            rows, cols = stdscr.getmaxyx()
            _draw_header(stdscr, "Watch", _status_line(switcher), cols)
            age = int(now - last_refresh)
            meta = (
                f"every {interval}s · updated {age}s ago · "
                f"[+/-] interval  [r] refresh  [q] back"
            )
            try:
                stdscr.addstr(3, 2, meta[: cols - 4], curses.A_DIM)
            except curses.error:
                pass
            body_top = 5
            for i, line in enumerate(body):
                y = body_top + i
                if y >= rows - 1:
                    break
                _addstr_ansi(stdscr, y, 2, line, cols - 4)
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), 27):
                return
            if key in (ord("+"), ord("=")):
                interval = _clamp_interval(interval + 1)
            elif key in (ord("-"), ord("_")):
                interval = _clamp_interval(interval - 1)
            elif key == ord("r"):
                last_refresh = 0.0
    finally:
        stdscr.timeout(-1)


# Auto-switch flow
def _auto_toggle(stdscr, switcher: ClaudeAccountSwitcher, cfg: dict) -> None:
    switcher.set_auto_switch_config(enabled=not cfg["enabled"])


def _auto_threshold(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    raw = _prompt_text(stdscr, "Switch when usage reaches (%): ")
    if not raw:
        return
    try:
        switcher.set_auto_switch_config(threshold=int(raw))
    except ValueError:
        _show_message(stdscr, "Threshold must be a whole number.", is_error=True)
    except ClaudeSwitchError as e:
        _show_message(stdscr, f"Invalid threshold: {e}", is_error=True)


def _auto_service_toggle(
    stdscr, switcher: ClaudeAccountSwitcher, current_service_state: str
) -> None:
    if current_service_state == "unsupported":
        _show_service_unsupported(stdscr)
    elif current_service_state in ("installed but not loaded", "loaded"):
        _shell_out(stdscr, lambda: service.uninstall(switcher))
    else:
        _shell_out(stdscr, lambda: service.install(switcher))


def _auto_service_status(
    stdscr, switcher: ClaudeAccountSwitcher, current_service_state: str
) -> None:
    if current_service_state == "unsupported":
        _show_service_unsupported(stdscr)
    else:
        _shell_out(stdscr, lambda: service.status(switcher))


def _auto_start_monitor(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    cfg = switcher.ensure_auto_switch_enabled()
    _run_auto_monitor(stdscr, switcher, cfg["threshold"])


def _do_auto_switch(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    """Settings + launcher for auto-switch (Beta).

    Lets the user enable cooldown-aware auto-switch when the active account's
    5h/7d usage reaches a threshold, tune that threshold, and start the
    foreground monitor. Settings persist in ``sequence.json``.

    No Claude Code restart is needed for the switch to take effect — on
    Linux/Windows the new credentials are picked up on the next message; on
    macOS within Claude Code's ~30s Keychain cache TTL. For automated paths
    (TUI monitor + launchd background service) the target's OAuth token is
    force-refreshed before activation so the first API call against the new
    account gets a freshly-issued token.
    """
    while True:
        cfg = switcher.get_auto_switch_config()
        _enabled, threshold, on_off, _state = auto_switch_display(cfg)
        # Local name deliberately differs from ``service.service_state`` (the
        # function used inside ``_service_state()``) so future readers don't
        # accidentally shadow the import.
        current_service_state = _service_state()
        subtitle = (
            f"Rule {on_off} @ {threshold}%  ·  "
            f"Background service {current_service_state}"
        )
        items: list[tuple[str, str | None]] = [
            (f"Auto-switch: {'Disable' if cfg['enabled'] else 'Enable'}", "toggle"),
            (f"Threshold: set to {cfg['threshold']}%", "threshold"),
            (_service_menu_label(current_service_state), "service-toggle"),
            ("Background service: Show status", "service-status"),
            ("Start monitor now", "start"),
            ("-- Back --", None),
        ]
        choice = _select_from(
            stdscr,
            "Auto-switch at limit (Beta)",
            items=items,
            subtitle=subtitle,
        )
        if choice is None:
            return

        if choice == "toggle":
            _auto_toggle(stdscr, switcher, cfg)
        elif choice == "threshold":
            _auto_threshold(stdscr, switcher)
        elif choice == "service-toggle":
            _auto_service_toggle(stdscr, switcher, current_service_state)
        elif choice == "service-status":
            _auto_service_status(stdscr, switcher, current_service_state)
        elif choice == "start":
            _auto_start_monitor(stdscr, switcher)


def _run_auto_monitor(
    stdscr, switcher: ClaudeAccountSwitcher, threshold: int
) -> None:
    """Foreground watcher: TUI adapter over the shared monitor engine."""
    running_pid = _acquire_monitor_pid(switcher)
    if running_pid is not None:
        _show_message(
            stdscr,
            f"Auto-switch monitor already running (pid {running_pid}). "
            "Stop the CLI monitor or background service first.",
            is_error=True,
        )
        return

    curses.curs_set(0)
    stdscr.timeout(1000)
    log = get_logger(switcher)
    log.info(
        "monitor start: threshold=%s adaptive=%s-%ss pid=%s",
        threshold,
        MONITOR_POLL_SECONDS_MIN,
        MONITOR_POLL_SECONDS,
        os.getpid(),
    )
    state = MonitorRuntimeState()
    poll_ceiling = MONITOR_POLL_SECONDS
    display_threshold = threshold
    last_checked = "never"
    message = "Monitoring started."
    seconds_to_next = 0
    usage_display = "unavailable"

    def perform_switch(decision) -> bool:
        return _auto_perform_switch(stdscr, switcher, decision)

    try:
        while True:
            if seconds_to_next <= 0:
                result = monitor_step(
                    switcher,
                    state,
                    poll_seconds=poll_ceiling,
                    perform_switch=perform_switch,
                )
                display_threshold = result.threshold
                usage_display = active_usage_display(result, state)
                last_checked = datetime.now().strftime("%H:%M:%S")
                seconds_to_next = result.next_interval
                message = result.user_message

            _draw_monitor(
                stdscr,
                display_threshold,
                usage_display,
                last_checked,
                seconds_to_next,
                message,
            )
            key = stdscr.getch()
            if key in (27, ord("q"), ord("Q")):
                return
            if key in (ord("s"), ord("S")):
                seconds_to_next = 0
                continue
            seconds_to_next -= 1
    finally:
        log.info("monitor stopped")
        stdscr.timeout(-1)
        _release_monitor_pid(switcher)


def _auto_perform_switch(
    stdscr,
    switcher: ClaudeAccountSwitcher,
    decision,
) -> bool:
    """Suspend curses, run automated ``switch()``, then resume."""
    curses.def_prog_mode()
    curses.endwin()
    switched = False
    try:
        try:
            switched = switcher.switch(
                InteractiveAutoSwitchIntent(decision=decision),
            )
        except ClaudeSwitchError as e:
            print(f"Auto-switch error: {e}")
            raise
        except KeyboardInterrupt:
            print("\nOperation cancelled.")
            raise SwitchCancelled from None
        print()
        time.sleep(2.5)  # brief pause so the output is readable
    finally:
        curses.reset_prog_mode()
        stdscr.refresh()
    return switched


def _draw_monitor(
    stdscr,
    threshold: int,
    usage_display: str,
    last_checked: str,
    seconds_to_next: int,
    message: str,
) -> None:
    """Render the auto-switch monitor screen."""
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    _draw_header(
        stdscr,
        "Auto-switch monitor (Beta)",
        f"threshold {threshold}%  ·  adaptive {MONITOR_POLL_SECONDS_MIN}–{MONITOR_POLL_SECONDS}s",
        cols,
    )
    lines = [
        f"Active account usage : {usage_display}",
        f"Last checked         : {last_checked}",
        f"Next check in        : {max(0, seconds_to_next)}s",
        "",
        message,
    ]
    for i, line in enumerate(lines):
        if 4 + i >= rows - 2:
            break
        stdscr.addstr(4 + i, 2, line[: cols - 4])
    footer = "[s] check now  [q/Esc] stop monitor"
    stdscr.addstr(rows - 1, 2, footer[: cols - 4], curses.A_DIM)
    stdscr.refresh()


# Helpers
def _status_line(switcher: ClaudeAccountSwitcher) -> str:
    """Compact one-liner: 'Active: email [org] · N managed'. Pure-local, no network."""
    seq = switcher._get_sequence_data() or {}
    n = len(seq.get("accounts", {}))
    identity = switcher._get_current_account()
    if identity is None:
        active = "(no active login)"
    else:
        email, org = identity
        tag = "personal" if not org else org[:8]
        active = f"{email} [{tag}]"
    line = f"Active: {active}  ·  {n} managed"
    enabled, threshold, on_off, _state = auto_switch_display(
        switcher.get_auto_switch_config()
    )
    if enabled:
        line += f"  ·  auto-switch {on_off} ({threshold}%)"
    return line


def _service_state() -> str:
    """Return a compact background-service state for the TUI.

    Works on every platform with a service backend (launchd / systemd / Task
    Scheduler); only genuinely unsupported platforms fall through to
    ``"unsupported"``.
    """
    try:
        return service.service_state()
    except ClaudeSwitchError:
        return "unsupported"


def _service_menu_label(service_state: str) -> str:
    """TUI label for the background-service toggle action."""
    if service_state == "unsupported":
        return "Background service: unavailable on this platform"
    if service_state in ("installed but not loaded", "loaded"):
        return "Background service: Uninstall"
    return "Background service: Install"


def _show_service_unsupported(stdscr) -> None:
    """Render a consistent TUI error when no service backend fits this platform."""
    _show_message(
        stdscr,
        "Background service is not available on this platform. "
        "Use the foreground monitor (`cswap --monitor`) instead.",
        is_error=True,
    )


def _account_items(switcher: ClaudeAccountSwitcher) -> list[tuple[str, str]]:
    """Build (label, account_num) list for switch/remove sub-pages.

    No network — usage % is intentionally omitted to keep the picker snappy.
    """
    seq = switcher._get_sequence_data_migrated() or {}
    accounts = seq.get("accounts", {})
    if not accounts:
        return []
    active = str(seq.get("activeAccountNumber", ""))
    items: list[tuple[str, str]] = []
    for num in sorted(seq.get("sequence", []), key=int):
        acc = accounts.get(str(num), {})
        email = acc.get("email", "?")
        org = acc.get("organizationName", "") or "personal"
        marker = "  ★ active" if str(num) == active else ""
        label = f"{num}  {email:<32}  [{org}]{marker}"
        items.append((label, str(num)))
    return items


# Curses primitives — kept thin so we can mock them in tests
def _select_from(
    stdscr,
    title: str,
    items: list[tuple[str, str | None]],
    subtitle: str = "",
) -> str | None:
    """Vertical menu picker. Returns the selected value, or ``None`` on cancel.

    ``items`` is a list of ``(label, value)`` pairs. Items whose value is
    ``None`` are treated as cancel sentinels (selecting them returns ``None``).
    """
    idx = 0
    while True:
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()
        _draw_header(stdscr, title, subtitle, cols)

        for i, (label, _val) in enumerate(items):
            y = 4 + i
            if y >= rows - 2:
                break
            line = label[: cols - 6]
            if i == idx:
                stdscr.addstr(y, 2, "> ", curses.A_BOLD)
                stdscr.addstr(y, 4, line, curses.A_REVERSE)
            else:
                stdscr.addstr(y, 4, line)

        footer = "[↑/↓] move  [Enter] select  [Esc/q] cancel"
        stdscr.addstr(rows - 1, 2, footer[: cols - 4], curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(items)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(items)
        elif key in (curses.KEY_ENTER, 10, 13):
            return items[idx][1]
        elif key in (27, ord("q")):  # Esc / q
            return None


def _prompt_text(stdscr, label: str, password: bool = False) -> str | None:
    """Single-line text prompt. Returns string or ``None`` on Esc.

    When ``password`` is True, keystrokes are not echoed.
    """
    curses.curs_set(1)
    try:
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()
        _draw_header(stdscr, "claude-swap", "", cols)
        stdscr.addstr(4, 2, label)
        footer = "[Enter] confirm  [Esc] cancel"
        stdscr.addstr(rows - 1, 2, footer[: cols - 4], curses.A_DIM)

        buf: list[str] = []
        cursor_x = 2 + len(label)
        while True:
            stdscr.move(4, cursor_x + len(buf))
            stdscr.refresh()
            key = stdscr.getch()
            if key == 27:  # Esc
                return None
            if key in (curses.KEY_ENTER, 10, 13):
                return "".join(buf).strip()
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
                    if password:
                        # nothing to erase visually (we never echoed)
                        pass
                    else:
                        x = cursor_x + len(buf)
                        stdscr.addstr(4, x, " ")
                        stdscr.move(4, x)
                continue
            if 32 <= key < 127:  # printable ASCII
                buf.append(chr(key))
                if not password:
                    stdscr.addstr(4, cursor_x + len(buf) - 1, chr(key))
    finally:
        curses.curs_set(0)


def _confirm(stdscr, prompt: str) -> bool:
    """Y/N prompt. Returns True only on 'y' / 'Y'."""
    answer = _prompt_text(stdscr, prompt)
    return bool(answer) and answer.lower() in ("y", "yes")


def _show_message(stdscr, msg: str, is_error: bool = False) -> None:
    """Display a single-line message and wait for any key."""
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    _draw_header(stdscr, "claude-swap", "", cols)
    attr = curses.A_BOLD if is_error else curses.A_NORMAL
    for i, line in enumerate(msg.split("\n")):
        if 4 + i >= rows - 2:
            break
        stdscr.addstr(4 + i, 2, line[: cols - 4], attr)
    stdscr.addstr(rows - 1, 2, "[Press any key to continue]", curses.A_DIM)
    stdscr.refresh()
    stdscr.getch()


def _draw_header(stdscr, title: str, subtitle: str, cols: int) -> None:
    stdscr.addstr(1, 2, title[: cols - 4], curses.A_BOLD)
    if subtitle:
        stdscr.addstr(2, 2, subtitle[: cols - 4], curses.A_DIM)


def _pager(stdscr, title: str, lines: list[str], subtitle: str = "") -> None:
    """Scrollable read-only view of ``lines`` (which may contain ANSI codes)."""
    top = 0
    while True:
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()
        _draw_header(stdscr, title, subtitle, cols)
        body_top = 4
        body_height = max(1, rows - body_top - 1)
        max_top = max(0, len(lines) - body_height)
        top = min(top, max_top)
        for i in range(body_height):
            li = top + i
            if li >= len(lines):
                break
            _addstr_ansi(stdscr, body_top + i, 2, lines[li], cols - 4)
        more = "  (more ↓)" if top < max_top else ""
        footer = f"[↑/↓ PgUp/PgDn] scroll  [q] back{more}"
        try:
            stdscr.addstr(rows - 1, 2, footer[: cols - 4], curses.A_DIM)
        except curses.error:
            pass
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            top = max(0, top - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            top = min(max_top, top + 1)
        elif key in (curses.KEY_NPAGE, ord("d")):
            top = min(max_top, top + body_height)
        elif key in (curses.KEY_PPAGE, ord("u")):
            top = max(0, top - body_height)
        elif key in (curses.KEY_HOME, ord("g")):
            top = 0
        elif key in (curses.KEY_END, ord("G")):
            top = max_top
        elif key in (curses.KEY_ENTER, 10, 13, 27, ord("q")):
            return


def _capture(fn: Callable[[], None]) -> str:
    """Run ``fn`` capturing stdout+stderr (with color forced on) into a string."""
    buf = io.StringIO()
    saved_stdin = sys.stdin
    sys.stdin = io.StringIO()
    try:
        with printer.force_color(), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(buf):
            try:
                fn()
            except ClaudeSwitchError as e:
                print(f"Error: {e}")
            except EOFError:
                print("Error: interactive input is not available here.")
            except KeyboardInterrupt:
                print("Operation cancelled.")
    finally:
        sys.stdin = saved_stdin
    return buf.getvalue()


def _run_inline(stdscr, title: str, fn: Callable[[], None]) -> None:
    """Run ``fn``, capturing its output and showing it in the in-TUI pager."""
    _pager(stdscr, title, _capture(fn).splitlines())


def _shell_out(stdscr, fn: Callable[[], None]) -> None:
    """Temporarily exit curses to run ``fn`` with normal stdout/stdin.

    Pauses afterwards so the user can read output, then restores the curses
    screen.
    """
    curses.def_prog_mode()  # save curses state
    curses.endwin()
    try:
        try:
            fn()
        except ClaudeSwitchError as e:
            print(f"Error: {e}")
        except KeyboardInterrupt:
            print("\nOperation cancelled.")
        print()
        try:
            input("[Press Enter to return to TUI]")
        except (EOFError, KeyboardInterrupt):
            pass
    finally:
        curses.reset_prog_mode()  # restore curses state
        stdscr.refresh()
