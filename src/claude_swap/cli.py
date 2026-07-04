"""Command-line interface for Claude Swap."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, cast

from claude_swap import __version__
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import error_envelope
from claude_swap.printer import dimmed, error, muted
from claude_swap.switcher import ClaudeAccountSwitcher


def _run_command(argv: list[str]) -> None:
    """Handle `cswap run NUM|EMAIL [--no-share] [-- <claude args>]`.

    Pre-dispatched before the main parser is built: a positional subcommand
    can't coexist with main()'s required mutually-exclusive flag group, and
    this keeps the existing parser untouched. Limitation: `run` must be the
    first argument (`cswap --debug run 2` is not supported; use
    `cswap run 2 --debug`).

    On POSIX this execs claude and never returns; on Windows it exits with
    claude's return code. Either way the post-dispatch update check in
    main() is unreachable, which is intended.
    """
    # Everything after the first `--` is forwarded to claude verbatim.
    if "--" in argv:
        split = argv.index("--")
        head, tail = argv[:split], argv[split + 1 :]
    else:
        head, tail = argv, []

    parser = argparse.ArgumentParser(
        prog="cswap run",
        description=(
            "[EXPERIMENTAL] Launch Claude Code as a stored account in this "
            "terminal only (the default login and other terminals are "
            "unaffected)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap run 2
  cswap run user@example.com
  cswap run 2 --no-share
  cswap run 2 --share-history
  cswap run 2 -- --resume
        """,
    )
    parser.add_argument(
        "account",
        metavar="NUM|EMAIL",
        help="Account to run (number or email)",
    )
    parser.add_argument(
        "--no-share",
        action="store_true",
        help=(
            "Don't share settings/keybindings/CLAUDE.md/skills/commands/agents "
            "from ~/.claude into the session profile (and remove previously "
            "shared items)"
        ),
    )
    parser.add_argument(
        "--share-history",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Share conversation history (projects/ and history.jsonl) from "
            "~/.claude into the session profile, so every account sees one "
            "unified history. History the profile already accumulated is "
            "merged into ~/.claude first. --no-share-history restores "
            "per-account history (the default). Not supported on Windows."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(head)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)

        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        from claude_swap.session import SessionManager

        SessionManager(switcher).run(
            args.account,
            tail,
            share=not args.no_share,
            share_history=args.share_history,
        )
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _auto_command(argv: list[str]) -> None:
    """Handle `cswap auto [--once] [--json] [...]`.

    Pre-dispatched before the main parser is built, like `run` (and with the
    same limitation: `auto` must be the first argument). Runs the auto-switch
    engine — a foreground loop by default, or a single evaluate-and-maybe-
    switch tick with --once whose exit code reports the outcome (for cron/
    systemd timers): 0 switched, 1 error, 2 no action needed, 3 blocked
    (no viable target / all accounts exhausted).
    """
    import signal
    import time as _time

    parser = argparse.ArgumentParser(
        prog="cswap auto",
        description=(
            "Automatically switch accounts when the active one nears its "
            "5h/7d rate limit. Runs a foreground polling loop; use --once "
            "for a single tick (cron-friendly)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes with --once:
  0  switched to another account
  1  error (network trouble, lock contention, ...)
  2  no action needed
  3  blocked: wanted to switch but no viable target / all exhausted

Examples:
  cswap auto                       # foreground loop, switch at 90%% used
  cswap auto --threshold 80        # switch earlier
  cswap auto --json                # one JSON event per line (for scripts)
  cswap auto --once; echo $?       # single tick, outcome in exit code
  cswap auto --dry-run             # log decisions, never actually switch

Defaults live in settings.json in the backup root; flags override them.
        """,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Evaluate once, maybe switch, and exit (exit code = outcome)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable JSON event per line on stdout",
    )
    parser.add_argument(
        "--interval",
        type=float,
        metavar="SECONDS",
        help="Poll interval in loop mode (min 15; default 60)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        metavar="PCT",
        help=(
            "Switch when the active account's binding 5h/7d window reaches "
            "this utilization (50-99.9; default 90)"
        ),
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        metavar="SECONDS",
        help="Minimum time between proactive switches (default 300)",
    )
    parser.add_argument(
        "--include-api-key-accounts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow switching onto managed API-key accounts as a last resort "
            "(they bill per token; default: excluded)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and report, but never switch or write state",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    from claude_swap.autoswitch import AutoSwitchEngine, AutoSwitchEvent
    from claude_swap.printer import accent, yellowed
    from claude_swap.settings import load_settings, merged_with_cli

    def jsonl_emit(event: AutoSwitchEvent) -> None:
        print(json.dumps(event.to_json()), flush=True)

    def human_emit(event: AutoSwitchEvent) -> None:
        stamp = _time.strftime("%H:%M:%S")
        line = event.human()
        if event.kind == "switch":
            line = accent(line)
        elif event.kind in ("error", "account-quarantined"):
            line = yellowed(line)
        elif event.kind in ("poll", "no-switch", "sleep"):
            line = dimmed(line)
        print(f"{stamp}  {line}", flush=True)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        stdout_emit = jsonl_emit if args.json else human_emit
        log = switcher._logger

        def emit(event: AutoSwitchEvent) -> None:
            # Mirror every event into the structured decision log
            # (claude-swap.log): under a service supervisor stdout may go
            # nowhere — on Windows pythonw it doesn't exist at all.
            level = (
                logging.WARNING
                if event.kind in ("error", "account-quarantined")
                else logging.INFO
            )
            log.log(level, "auto: %s", event.human())
            stdout_emit(event)

        settings = merged_with_cli(load_settings(switcher.backup_dir), args)
        engine = AutoSwitchEngine(
            switcher,
            settings,
            emit,
            dry_run=args.dry_run,
        )

        if args.once:
            sys.exit(engine.tick().value)

        # Loop mode: SIGTERM (systemd stop) exits the loop cleanly.
        signal.signal(signal.SIGTERM, lambda *_: engine.stop())
        if not args.json:
            print(
                dimmed(
                    f"Auto-switch running: threshold {settings.threshold:.0f}%, "
                    f"every {settings.interval_seconds:.0f}s"
                    f"{' (dry-run)' if args.dry_run else ''} — Ctrl-C to stop"
                )
            )
        sys.exit(engine.run_loop())
    except ClaudeSwitchError as e:
        if args.json:
            print(json.dumps(error_envelope(e)))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Auto-switch stopped')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)


def _use_native_tls() -> None:
    """Route TLS trust decisions through the OS-native verifier.

    Claude's token endpoint (``platform.claude.com``) serves a Let's Encrypt
    chain. Python's stdlib ``ssl`` uses OpenSSL, which on Windows loads the
    system cert store as a flat set and matches CA certs by *subject name*, so a
    stale, expired duplicate of an intermediate (e.g. an old ``ISRG Root X2``
    left in the user's store) can shadow the valid path and fail verification
    with "certificate has expired" even though the served chain is valid — which
    silently breaks inactive-account token refresh. The OS-native verifiers
    (SChannel on Windows, SecureTransport on macOS) build the chain correctly
    and don't trip on the expired duplicate — the same reason Claude Code (Node,
    with its own bundled roots) is unaffected. ``truststore`` delegates to them.

    Best-effort: on any failure fall back to stdlib ``ssl`` rather than block
    the CLI over a TLS-trust nicety.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass


def _relax_redirected_stream_encoding() -> None:
    """Keep redirected Windows output from crashing on non-ANSI glyphs.

    Redirected/piped stdout on Windows encodes with the locale ANSI code page
    (e.g. cp1252) under ``errors=strict``, so the tree connectors and bullets
    in ``--list`` made ``cswap --list > file`` raise ``UnicodeEncodeError``.
    Interactive consoles go through the wide-char API and are unaffected —
    only non-tty streams are degraded. ``hasattr`` guards a replaced
    ``sys.stdout`` that is not a ``TextIOWrapper``.
    """
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if (
                stream is not None
                and hasattr(stream, "reconfigure")
                and not stream.isatty()
            ):
                stream.reconfigure(errors="replace")


def _service_command(argv: list[str]) -> None:
    """Handle `cswap service install|uninstall|status|logs`.

    Pre-dispatched before the main parser is built, mirroring `_run_command`:
    a positional subcommand can't coexist with main()'s required mutually-
    exclusive flag group, and this keeps the existing parser untouched.
    Limitation: `service` must be the first argument (`cswap --debug service
    status` is not supported; use `cswap service status --debug`).
    """
    parser = argparse.ArgumentParser(
        prog="cswap service",
        description=(
            "Manage the background auto-switch engine (launchd on macOS, "
            "systemd --user on Linux/WSL, Task Scheduler on Windows). "
            "Runs `cswap auto` at login and restarts it on failure."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap service install
  cswap service status
  cswap service logs
  cswap service uninstall
        """,
    )
    parser.add_argument(
        "action",
        choices=("install", "uninstall", "status", "logs"),
        help="install | uninstall | status | logs",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        from claude_swap import service

        action = {
            "install": service.install,
            "uninstall": service.uninstall,
            "status": service.status,
            "logs": service.logs,
        }[args.action]
        sys.exit(action(switcher))
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


# Map subcommand -> handler name; resolved via globals() at call time so tests
# can monkeypatch the module-level handler (e.g. cli._service_command).
_SUBCOMMANDS = {
    "run": "_run_command",
    "auto": "_auto_command",
    "service": "_service_command",
}


def _build_parser() -> argparse.ArgumentParser:
    """Construct the top-level flag parser (the bare-flag, non-subcommand UI)."""
    parser = argparse.ArgumentParser(
        description="Multi-Account Switcher for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --add-account
  %(prog)s --add-token sk-ant-oat01-...           # OAuth setup-token
  %(prog)s --add-token sk-ant-api03-...           # managed API key
  %(prog)s --add-token sk-ant-oat01-... --slot 3
  %(prog)s --add-token sk-ant-oat01-... --email me@example.com
  %(prog)s --add-token - --slot 3
  %(prog)s --list
  %(prog)s --health
  %(prog)s --switch
  %(prog)s --switch --strategy best             # switch to the account with most quota left
  %(prog)s --switch --strategy next-available   # rotate, skipping rate-limited accounts
  %(prog)s --switch-to 2
  %(prog)s --switch-to user@example.com
  %(prog)s run 2                            # run account 2 in this terminal only
  %(prog)s run 2 -- --resume                # forward args after '--' to claude
  %(prog)s auto                             # auto-switch when nearing rate limits
  %(prog)s auto --once                      # single auto-switch tick (cron-friendly)
  %(prog)s --remove-account user@example.com
  %(prog)s --status
  %(prog)s --purge
  %(prog)s --export backup.cswap
  %(prog)s --import backup.cswap
  %(prog)s --tui                              # interactive arrow-key menu
  %(prog)s service install                    # background auto-switch engine
  %(prog)s --upgrade                          # self-upgrade to latest version
        """,
    )

    # Version and debug flags (outside mutually exclusive group)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--token-status",
        action="store_true",
        help="Show OAuth token expiry state (use with --list)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit machine-readable JSON to stdout (use with --list, --status, "
            "--switch, or --switch-to). See README 'JSON output for scripting'."
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=["best", "next-available"],
        metavar="{best,next-available}",
        help=(
            "With --switch: pick the target by remaining 5h/7d quota. "
            "'best' jumps to the account with the most headroom; "
            "'next-available' rotates to the next account, skipping any at their limit"
        ),
    )
    parser.add_argument(
        "--slot",
        type=int,
        metavar="NUM",
        help="Specify slot number when adding account (use with --add-account or --add-token)",
    )
    parser.add_argument(
        "--email",
        metavar="EMAIL",
        help=(
            "Email address for the account. Optional with --add-token; "
            "defaults to setup-token-{slot}@token.local (or "
            "api-key-{slot}@token.local for API keys) since these tokens "
            "carry no real email metadata."
        ),
    )
    parser.add_argument(
        "--account",
        metavar="NUM|EMAIL",
        help="Limit export to one account (use with --export)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing accounts during import; with --switch-to, "
            "activate the stored credentials without backing up the current "
            "login first"
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include full ~/.claude.json in export (default: oauthAccount only)",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--add-account",
        action="store_true",
        help="Add current account to managed accounts",
    )
    group.add_argument(
        "--remove-account",
        metavar="NUM|EMAIL",
        help="Remove account by number or email",
    )
    group.add_argument(
        "--list",
        action="store_true",
        help="List all managed accounts",
    )
    group.add_argument(
        "--health",
        action="store_true",
        help="Show account health, usage, and OAuth token status",
    )
    group.add_argument(
        "--switch",
        action="store_true",
        help="Rotate to next account in sequence",
    )
    group.add_argument(
        "--switch-to",
        metavar="NUM|EMAIL",
        help="Switch to specific account number or email",
    )
    group.add_argument(
        "--status",
        action="store_true",
        help="Show current account status",
    )
    group.add_argument(
        "--purge",
        action="store_true",
        help="Remove all claude-swap data from the system",
    )
    group.add_argument(
        "--export",
        metavar="PATH",
        help="Export accounts to file (use '-' for stdout)",
    )
    group.add_argument(
        "--import",
        dest="import_",
        metavar="PATH",
        help="Import accounts from file (use '-' for stdin)",
    )
    group.add_argument(
        "--tui",
        action="store_true",
        help="Launch interactive arrow-key menu (single-level)",
    )
    group.add_argument(
        "--upgrade",
        action="store_true",
        help="Upgrade claude-swap to the latest version on PyPI",
    )
    group.add_argument(
        "--add-token",
        metavar="TOKEN|-",
        nargs="?",
        const="",
        help=(
            "Register a raw OAuth setup-token or managed API key (sk-ant-api...) "
            "as a new account; the type is auto-detected. Pass '-' to read from "
            "stdin or omit the value to be prompted securely."
        ),
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Enforce cross-flag constraints argparse cannot express directly."""
    if args.token_status and not (args.list or args.health):
        parser.error("--token-status can only be used with --list or --health")

    if args.json and not (args.list or args.status or args.switch or args.switch_to):
        parser.error(
            "--json can only be used with --list, --status, --switch, or --switch-to"
        )

    if args.json and args.token_status:
        # Token status is not part of the JSON v1 schema; reject rather than
        # silently ignore it (a future additive field can add it).
        parser.error("--token-status cannot be combined with --json")

    if args.strategy is not None and not args.switch:
        parser.error("--strategy can only be used with --switch")

    if args.slot is not None and not (args.add_account or args.add_token is not None):
        parser.error("--slot can only be used with --add-account or --add-token")

    if args.email is not None and args.add_token is None:
        parser.error("--email can only be used with --add-token")

    if args.account is not None and not args.export:
        parser.error("--account can only be used with --export")

    if args.force and not (args.import_ or args.switch_to):
        parser.error("--force can only be used with --import or --switch-to")

    if args.full and not args.export:
        parser.error("--full can only be used with --export")


def _cmd_export(switcher: ClaudeAccountSwitcher, args: argparse.Namespace) -> None:
    from claude_swap.transfer import export_accounts

    export_accounts(switcher, args.export, account=args.account, full=args.full)


def _cmd_import(switcher: ClaudeAccountSwitcher, args: argparse.Namespace) -> None:
    from claude_swap.transfer import import_accounts

    import_accounts(switcher, args.import_, force=args.force)


def _cmd_tui(switcher: ClaudeAccountSwitcher, args: argparse.Namespace) -> None:
    try:
        from claude_swap.tui import run as tui_run
    except ImportError:
        error(
            "TUI mode requires the 'curses' module. "
            "On Windows, install with: pip install windows-curses"
        )
        sys.exit(1)
    sys.exit(tui_run(switcher))


def _dispatch_action(
    switcher: ClaudeAccountSwitcher, args: argparse.Namespace
) -> dict[str, Any] | None:
    """Run the single selected mutually-exclusive action. Returns JSON payload when applicable."""
    if args.add_account:
        switcher.add_account(slot=args.slot)
    elif args.add_token is not None:
        switcher.add_account_from_token(
            token=args.add_token,
            email=args.email,
            slot=args.slot,
        )
    elif args.remove_account:
        switcher.remove_account(args.remove_account)
    elif args.list:
        if args.json:
            return switcher.list_accounts(
                show_token_status=args.token_status, json_output=True
            )
        switcher.list_accounts(show_token_status=args.token_status)
    elif args.health:
        switcher.list_accounts(show_token_status=True, show_health=True)
    elif args.switch:
        if args.json:
            return cast(
                dict[str, Any],
                switcher.switch(strategy=args.strategy, json_output=True),
            )
        switcher.switch(strategy=args.strategy)
    elif args.switch_to:
        return switcher.switch_to(
            args.switch_to, json_output=args.json, force=args.force
        )
    elif args.status:
        if args.json:
            return switcher.status(json_output=True)
        switcher.status()
    elif args.purge:
        switcher.purge()
    elif args.export:
        _cmd_export(switcher, args)
    elif args.import_:
        _cmd_import(switcher, args)
    elif args.tui:
        _cmd_tui(switcher, args)
    return None


def main() -> None:
    """Main entry point for the CLI."""
    _use_native_tls()
    _relax_redirected_stream_encoding()
    if len(sys.argv) > 1 and sys.argv[1] in _SUBCOMMANDS:
        # Subcommands return only in tests where exec/sys.exit is mocked.
        globals()[_SUBCOMMANDS[sys.argv[1]]](sys.argv[2:])
        return

    parser = _build_parser()
    args = parser.parse_args()
    _validate_args(parser, args)

    # Self-upgrade runs before switcher init so we don't touch config/keychain
    # just to upgrade the tool itself.
    if args.upgrade:
        from claude_swap.update_check import run_self_upgrade

        try:
            sys.exit(run_self_upgrade())
        except KeyboardInterrupt:
            print(f"\n{dimmed('Upgrade cancelled')}")
            sys.exit(130)

    # Initialize switcher and dispatch under a single error handler so
    # init-time failures (e.g. MigrationError on a backup-dir collision)
    # are presented like every other ClaudeSwitchError: clean stderr line,
    # exit 1, no traceback.
    # JSON-capable commands return a payload; the CLI is the single point that
    # serializes it (so no command writes JSON to stdout itself).
    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)

        # Check for root (unless in container) - POSIX only
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        payload = _dispatch_action(switcher, args)
    except ClaudeSwitchError as e:
        # In JSON mode keep stdout pure JSON: emit the structured error envelope
        # there (exit 1) instead of a red stderr line.
        if args.json:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        # Route the cancellation note to stderr in JSON mode so stdout stays
        # parseable (the guarantee covers completion / handled errors, not Ctrl-C).
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)

    if args.json and payload is not None:
        print(json.dumps(payload, indent=2))

    # Passive update notification (never fails). Skipped after --purge so we
    # don't immediately recreate <backup_root>/cache/update_check.json inside
    # the directory we just deleted. Skipped after --upgrade as a safety guard
    # in case the dispatch is later refactored to fall through.
    if not args.purge and not args.upgrade and not args.json:
        from claude_swap.update_check import check_for_update

        msg = check_for_update(__version__)
        if msg:
            print(f"\n{muted(msg)}", file=sys.stderr)


if __name__ == "__main__":
    main()
