"""CLI-strategy switch dispatch for ``ClaudeAccountSwitcher``.

Adapts ``--switch`` / ``--json`` entry points to the switcher's domain methods
without duplicating switch logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_swap import oauth
from claude_swap.json_output import account_ref
from claude_swap.models import CliSwitchIntent, SwitchPreconditionKind, SwitchPreconditions
from claude_swap.printer import accent, dimmed, warning

if TYPE_CHECKING:
    from claude_swap.protocols import SwitchCliHost


def run_switch_cli(
    switcher: SwitchCliHost,
    *,
    strategy: str | None = None,
    json_output: bool = False,
) -> dict | None:
    """Run a strategy-aware CLI switch via *switcher*."""
    return SwitchCliDispatcher(switcher).run(
        strategy=strategy, json_output=json_output,
    )


class SwitchCliDispatcher:
    """JSON/strategy CLI adapter layer for ``ClaudeAccountSwitcher.switch()``."""

    def __init__(self, switcher: SwitchCliHost) -> None:
        self._switcher = switcher

    def run(
        self, strategy: str | None = None, json_output: bool = False,
    ) -> dict | None:
        """Switch to next account in sequence.

        Args:
            strategy: Usage-aware target selection. ``"best"`` jumps to the
                  switchable account with the most remaining 5h/7d quota instead
                  of advancing the rotation; ``"next-available"`` rotates to the
                  next account, skipping any currently at its 5h/7d limit. ``None``
                  (the default) performs a plain rotation.

        ``"best"`` only switches when it can prove another account has more
        remaining quota; if usage can't be fetched or no candidate is provably
        better, it stays put (run a plain ``cswap --switch`` to rotate anyway).
        ``"next-available"`` rotates and skips accounts at their limit, falling
        back to plain rotation when usage is unavailable. Both apply only to the
        normal path (a live Claude login present); the fresh-machine path (no
        live login, e.g. right after --import) ignores them.
        """
        strategy_label = strategy if strategy in ("best", "next-available") else "rotation"
        warnings: list[str] = []
        intent = CliSwitchIntent(
            quiet=json_output,
            force_refresh=json_output,
        )

        preconditions = self._switcher._classify_switch_preconditions()
        handled, result = self._preconditions(
            preconditions,
            intent=intent,
            strategy_label=strategy_label,
            json_output=json_output,
            warnings=warnings,
        )
        if handled:
            return result

        current_email, current_org_uuid = preconditions.identity
        data = preconditions.data
        sequence = preconditions.sequence
        active_account = data.get("activeAccountNumber")
        current_num = self._switcher._find_account_slot(
            data, current_email, current_org_uuid,
        )
        if current_num is None:
            current_num = str(active_account) if active_account is not None else None

        current_ref = (
            account_ref(int(current_num), current_email) if current_num else None
        )

        rotation_kwargs = dict(
            intent=intent,
            strategy_label=strategy_label,
            sequence=sequence,
            active_account=active_account,
            current_num=current_num,
            current_ref=current_ref,
            json_output=json_output,
            warnings=warnings,
        )

        if strategy == "best":
            handled, result = self._best(
                intent=intent,
                strategy_label=strategy_label,
                current_num=current_num,
                current_ref=current_ref,
                json_output=json_output,
                warnings=warnings,
            )
            if handled:
                return result

        if strategy == "next-available":
            return self._next_available(**rotation_kwargs)

        return self._rotation(**rotation_kwargs)

    def _preconditions(
        self,
        preconditions: SwitchPreconditions,
        *,
        intent: CliSwitchIntent,
        strategy_label: str,
        json_output: bool,
        warnings: list[str],
    ) -> tuple[bool, dict | None]:
        """Handle fresh-machine / unmanaged / single-account paths.

        Returns ``(handled, result)``. When ``handled`` is true, the caller should
        return ``result`` (``None`` for interactive mode).
        """
        s = self._switcher
        if preconditions.kind == SwitchPreconditionKind.FRESH_MACHINE:
            target = s._resolve_fresh_machine_target(
                warnings=warnings if json_output else None,
            )
            op = s._perform_switch(
                target, intent=intent, emit_output=not json_output,
            )
            result = (
                s._switch_result_from_op(op, strategy_label, warnings)
                if json_output else None
            )
            return True, result

        if preconditions.kind == SwitchPreconditionKind.UNMANAGED:
            current_email, _ = preconditions.identity
            if json_output:
                ref = account_ref(None, current_email)
                return True, s._switch_noop(
                    strategy=strategy_label,
                    reason="unmanaged-account",
                    from_ref=ref,
                    to_ref=ref,
                    message="Active account is not managed; run cswap --add-account",
                )
            print(f"{accent('Notice:')} Active account '{current_email}' was not managed.")
            s.add_account()
            data = s._get_sequence_data()
            account_num = data.get("activeAccountNumber")
            print(f"It has been automatically added as Account-{account_num}.")
            print(dimmed("Please run the switch command again to switch to the next account."))
            return True, None

        if preconditions.kind == SwitchPreconditionKind.SINGLE_ACCOUNT:
            current_email, _ = preconditions.identity
            if json_output:
                num = preconditions.current_slot
                return True, s._switch_noop(
                    strategy=strategy_label,
                    reason="only-one-account",
                    to_ref=account_ref(int(num), current_email) if num else None,
                    message="Only one account is managed. Add more accounts to switch between.",
                )
            print(dimmed("Only one account is managed. Add more accounts to switch between."))
            return True, None

        return False, None

    def _best(
        self,
        *,
        intent: CliSwitchIntent,
        strategy_label: str,
        current_num: str | None,
        current_ref: dict | None,
        json_output: bool,
        warnings: list[str],
    ) -> tuple[bool, dict | None]:
        """Usage-aware jump to the account with the most remaining quota.

        Returns ``(handled, result)``. When ``handled`` is false, fall through to
        rotation (``note == "none"``).
        """
        s = self._switcher
        target, note = s._select_best_switchable(current_num)
        if target is not None:
            op = s._perform_switch(
                target, intent=intent, emit_output=not json_output,
            )
            result = (
                s._switch_result_from_op(op, strategy_label, warnings)
                if json_output else None
            )
            return True, result
        if note == "current-unavailable":
            if json_output:
                return True, s._switch_noop(
                    strategy=strategy_label, reason="usage-unavailable",
                    to_ref=current_ref,
                    message=(
                        f"Current account usage is unavailable — staying on "
                        f"Account-{current_num}."
                    ),
                )
            print(dimmed(
                f"Current account usage is unavailable — staying on "
                f"Account-{current_num}. Run cswap --switch to rotate."
            ))
            return True, None
        if note == "no-comparison":
            if json_output:
                return True, s._switch_noop(
                    strategy=strategy_label, reason="usage-unavailable",
                    to_ref=current_ref,
                    message=(
                        f"No other account has usage data to compare — staying "
                        f"on Account-{current_num}."
                    ),
                )
            print(dimmed(
                f"No other account has usage data to compare — staying on "
                f"Account-{current_num}. Run cswap --switch to rotate."
            ))
            return True, None
        if note == "incomplete-comparison":
            if json_output:
                return True, s._switch_noop(
                    strategy=strategy_label, reason="usage-unavailable",
                    to_ref=current_ref,
                    message=(
                        f"No account with known usage has more remaining quota; "
                        f"some usage is unavailable — staying on Account-{current_num}."
                    ),
                )
            print(dimmed(
                f"No account with known usage has more remaining quota; some "
                f"usage is unavailable — staying on Account-{current_num}."
            ))
            return True, None
        if note == "stay":
            if json_output:
                return True, s._switch_noop(
                    strategy=strategy_label, reason="already-best",
                    to_ref=current_ref,
                    message=(
                        f"Already on the account with the most remaining quota "
                        f"(Account-{current_num})."
                    ),
                )
            print(
                f"{accent('Already on the account with the most remaining quota')} "
                f"(Account-{current_num})."
            )
            return True, None
        if note == "exhausted":
            if json_output:
                return True, s._switch_noop(
                    strategy=strategy_label, reason="candidates-exhausted",
                    to_ref=current_ref,
                    message=(
                        f"All accounts are at their 5h/7d limit — staying on "
                        f"Account-{current_num}."
                    ),
                )
            warning(
                f"All accounts are at their 5h/7d limit — staying on "
                f"Account-{current_num}."
            )
            return True, None
        return False, None

    def _rotation_target(
        self,
        *,
        strategy: str | None,
        intent: CliSwitchIntent,
        strategy_label: str,
        sequence: list,
        active_account,
        current_num: str | None,
        current_ref: dict | None,
        json_output: bool,
        warnings: list[str],
    ) -> dict | None:
        """Find the next rotation target and perform the switch."""
        s = self._switcher
        anchor = current_num if strategy == "next-available" else active_account
        try:
            current_index = sequence.index(int(anchor))
        except (TypeError, ValueError):
            try:
                current_index = sequence.index(active_account)
            except (TypeError, ValueError):
                current_index = 0

        usage = s._usage_by_account() if strategy == "next-available" else {}

        next_account: str | None = None
        skipped_exhausted: list[str] = []
        for offset in range(1, len(sequence)):
            candidate = str(sequence[(current_index + offset) % len(sequence)])
            if not s._account_is_switchable(candidate):
                if json_output:
                    warnings.append(
                        f"Skipped Account-{candidate} (no stored credentials/config)"
                    )
                else:
                    print(
                        f"{accent('Skipping')} Account-{candidate} "
                        f"(no stored credentials/config, re-add with "
                        f"cswap --add-account --slot {candidate})"
                    )
                continue
            if strategy == "next-available":
                headroom = oauth.account_headroom(usage.get(candidate))
                if headroom is not None and headroom <= 0:
                    skipped_exhausted.append(candidate)
                    if json_output:
                        warnings.append(
                            f"Skipped Account-{candidate} (at 5h/7d limit)"
                        )
                    else:
                        print(f"{accent('Skipping')} Account-{candidate} (at 5h/7d limit)")
                    continue
            next_account = candidate
            break

        if next_account is None and skipped_exhausted:
            if json_output:
                return s._switch_noop(
                    strategy=strategy_label, reason="candidates-exhausted",
                    to_ref=current_ref, warnings=warnings,
                    message=(
                        f"All other accounts are at their 5h/7d limit — staying on "
                        f"Account-{current_num}."
                    ),
                )
            warning(
                f"All other accounts are at their 5h/7d limit — staying on "
                f"Account-{current_num}."
            )
            return None

        if next_account is None:
            if json_output:
                return s._switch_noop(
                    strategy=strategy_label, reason="no-valid-target",
                    to_ref=current_ref, warnings=warnings,
                    message="No other accounts have valid stored credentials/config.",
                )
            print(dimmed(
                "No other accounts have valid stored credentials/config.\n"
                "Re-add a skipped slot with: cswap --add-account --slot <number>"
            ))
            return None

        op = s._perform_switch(
            next_account, intent=intent, emit_output=not json_output,
        )
        return (
            s._switch_result_from_op(op, strategy_label, warnings)
            if json_output else None
        )

    def _next_available(
        self,
        *,
        intent: CliSwitchIntent,
        strategy_label: str,
        sequence: list,
        active_account,
        current_num: str | None,
        current_ref: dict | None,
        json_output: bool,
        warnings: list[str],
    ) -> dict | None:
        return self._rotation_target(
            strategy="next-available",
            intent=intent,
            strategy_label=strategy_label,
            sequence=sequence,
            active_account=active_account,
            current_num=current_num,
            current_ref=current_ref,
            json_output=json_output,
            warnings=warnings,
        )

    def _rotation(
        self,
        *,
        intent: CliSwitchIntent,
        strategy_label: str,
        sequence: list,
        active_account,
        current_num: str | None,
        current_ref: dict | None,
        json_output: bool,
        warnings: list[str],
    ) -> dict | None:
        return self._rotation_target(
            strategy=None,
            intent=intent,
            strategy_label=strategy_label,
            sequence=sequence,
            active_account=active_account,
            current_num=current_num,
            current_ref=current_ref,
            json_output=json_output,
            warnings=warnings,
        )
