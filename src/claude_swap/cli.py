"""Command-line interface for Claude Swap."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, cast

from claude_swap import __version__
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import error_envelope
from claude_swap.printer import bolded, dimmed, error, muted
from claude_swap.sequence_store import AutoSwitchConfig
from claude_swap.switcher import ClaudeAccountSwitcher, auto_switch_display


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
            "Manage the background auto-switch monitor (launchd on macOS, "
            "systemd --user on Linux/WSL, Task Scheduler on Windows). "
            "Runs `cswap --monitor` at login and restarts it on failure."
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


def _print_auto_switch_config(config: AutoSwitchConfig) -> None:
    _enabled, threshold, on_off, _state = auto_switch_display(config)
    print(f"{bolded('Auto-switch:')} {on_off} {muted(f'(threshold {threshold}%)')}")


def _auto_switch_command(argv: list[str]) -> None:
    """Handle `cswap auto-switch status|enable|disable|set-threshold N`."""
    parser = argparse.ArgumentParser(
        prog="cswap auto-switch",
        description="Manage persisted auto-switch configuration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap auto-switch status
  cswap auto-switch enable
  cswap auto-switch disable
  cswap auto-switch set-threshold 95
        """,
    )
    parser.add_argument(
        "action",
        choices=("status", "enable", "disable", "set-threshold"),
        help="status | enable | disable | set-threshold",
    )
    parser.add_argument(
        "threshold",
        nargs="?",
        type=int,
        metavar="NUM",
        help="Threshold percentage (required for set-threshold)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    if args.action == "set-threshold" and args.threshold is None:
        parser.error("set-threshold requires NUM")
    if args.action != "set-threshold" and args.threshold is not None:
        parser.error("threshold is only accepted with set-threshold")

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)

        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        if args.action == "status":
            config = switcher.get_auto_switch_config()
        elif args.action == "enable":
            config = switcher.set_auto_switch_config(enabled=True)
        elif args.action == "disable":
            config = switcher.set_auto_switch_config(enabled=False)
        else:
            config = switcher.set_auto_switch_config(threshold=args.threshold)

        _print_auto_switch_config(config)
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
    "service": "_service_command",
    "auto-switch": "_auto_switch_command",
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
  %(prog)s --remove-account user@example.com
  %(prog)s --status
  %(prog)s --purge
  %(prog)s --export backup.cswap
  %(prog)s --import backup.cswap
  %(prog)s --tui                              # interactive arrow-key menu
  %(prog)s --monitor                          # foreground auto-switch monitor
  %(prog)s service install                    # background auto-switch monitor
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
        "--monitor",
        action="store_true",
        help="Run the auto-switch monitor in the foreground",
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
    # Internal flag appended by the service backends to the supervised
    # ``--monitor`` argv. Hidden from --help: users start the foreground
    # monitor without it, and setting it changes only the PID-collision exit
    # code (75 so the supervisor retries, instead of 0).
    parser.add_argument(
        "--service-monitor",
        action="store_true",
        help=argparse.SUPPRESS,
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

    if args.service_monitor and not args.monitor:
        parser.error("--service-monitor can only be used with --monitor")


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


def _cmd_monitor(switcher: ClaudeAccountSwitcher, args: argparse.Namespace) -> None:
    from claude_swap.monitor import run_cli_monitor

    if args.service_monitor:
        sys.exit(run_cli_monitor(switcher, service_mode=True))
    sys.exit(run_cli_monitor(switcher))


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
    elif args.monitor:
        _cmd_monitor(switcher, args)
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
