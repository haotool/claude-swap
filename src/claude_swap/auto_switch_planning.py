"""Cooldown-aware auto-switch target planning for claude-swap.

Pure scoring and pick/plan logic extracted from ``switcher.py`` so the
switcher keeps orchestration while monitor/TUI/CLI share one definition of
how usage snapshots map to a target slot.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime

from claude_swap.models import AutoSwitchDecisionContext, SwitchPlanResult

SLOT_SCORE_BUCKET_UNSATURATED = 0
SLOT_SCORE_BUCKET_SATURATED = 1
SLOT_SCORE_BUCKET_UNKNOWN = 2

SATURATED_SWITCH_MARGIN_S = 300


def slot_switch_score(usage: object, threshold: int) -> tuple[int, float]:
    """Score a slot for cooldown-aware switch target selection."""
    if not isinstance(usage, dict):
        return (SLOT_SCORE_BUCKET_UNKNOWN, math.inf)

    pcts: list[float] = []
    saturated_resets: list[float] = []
    for key in ("five_hour", "seven_day"):
        entry = usage.get(key)
        if not isinstance(entry, dict):
            continue
        pct = entry.get("pct")
        if not isinstance(pct, (int, float)):
            continue
        pct_f = float(pct)
        pcts.append(pct_f)
        if pct_f >= threshold:
            resets_at = entry.get("resets_at")
            if isinstance(resets_at, str):
                try:
                    ts = datetime.fromisoformat(resets_at).timestamp()
                except ValueError:
                    continue
                saturated_resets.append(ts)

    if not pcts:
        return (SLOT_SCORE_BUCKET_UNKNOWN, math.inf)

    max_pct = max(pcts)
    if max_pct < threshold:
        return (SLOT_SCORE_BUCKET_UNSATURATED, max_pct)
    if not saturated_resets:
        return (SLOT_SCORE_BUCKET_SATURATED, math.inf)
    return (SLOT_SCORE_BUCKET_SATURATED, min(saturated_resets))


def max_usage_pct(usage: dict | None) -> float | None:
    """Return the highest 5h/7d utilization percentage in a usage dict."""
    if not isinstance(usage, dict):
        return None
    pcts: list[float] = []
    for key in ("five_hour", "seven_day"):
        entry = usage.get(key)
        if isinstance(entry, dict):
            pct = entry.get("pct")
            if isinstance(pct, (int, float)):
                pcts.append(float(pct))
    return max(pcts) if pcts else None


def pick_best_from_snapshots(
    get_sequence_data: Callable[[], dict | None],
    is_switchable: Callable[[str], bool],
    threshold: int,
    snapshots: dict[str, object],
    *,
    exclude: str | None = None,
) -> str | None:
    """Score switchable slots from trusted usage snapshots only."""
    data = get_sequence_data() or {}
    sequence = data.get("sequence", [])
    if not sequence:
        return None

    scored: list[tuple[tuple[int, float], str]] = []
    for num in sequence:
        num_str = str(num)
        if exclude is not None and num_str == exclude:
            continue
        if not is_switchable(num_str):
            continue
        cached_entry = snapshots.get(num_str)
        score = slot_switch_score(cached_entry, threshold)
        scored.append((score, num_str))

    if not scored:
        return None
    if all(s[0][0] == SLOT_SCORE_BUCKET_UNKNOWN for s in scored):
        return None

    scored.sort()
    return scored[0][1]


def plan_automated_switch(
    decision: AutoSwitchDecisionContext,
    pick_best: Callable[[int, dict[str, object], str | None], str | None],
) -> SwitchPlanResult:
    """Choose an automated switch target from a trusted decision snapshot."""

    active = decision.live_active_slot or decision.sequence_active_slot
    best = pick_best(decision.threshold, decision.usage_by_slot, None)

    if best is None:
        return SwitchPlanResult(
            outcome="no_trusted_signal",
            reason=(
                "no trusted usage snapshots — run `cswap --list` or wait "
                "for the monitor to refresh cache"
            ),
        )

    if active is not None and best == active:
        return SwitchPlanResult(
            outcome="already_optimal",
            target=best,
            reason=f"already on optimal Account-{best}",
        )

    if active is not None and best is not None:
        best_score = slot_switch_score(decision.usage_by_slot.get(best), decision.threshold)
        if best_score[0] == SLOT_SCORE_BUCKET_SATURATED:
            active_score = slot_switch_score(
                decision.usage_by_slot.get(active), decision.threshold
            )
            if active_score[0] == SLOT_SCORE_BUCKET_SATURATED:
                if best_score[1] >= active_score[1] - SATURATED_SWITCH_MARGIN_S:
                    return SwitchPlanResult(
                        outcome="already_optimal",
                        target=active,
                        reason=(
                            f"both accounts saturated; staying on "
                            f"Account-{active} (target resets at most "
                            f"{SATURATED_SWITCH_MARGIN_S}s sooner)"
                        ),
                    )

    return SwitchPlanResult(
        outcome="chosen",
        target=best,
        reason=f"cooldown-aware pick Account-{best}",
    )
