"""Single source of truth for usage window parsing and slot ranking."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from claude_swap.models import AutoSwitchDecisionContext, SwitchPlanResult

SLOT_SCORE_BUCKET_UNSATURATED = 0
SLOT_SCORE_BUCKET_SATURATED = 1
SLOT_SCORE_BUCKET_UNKNOWN = 2

SATURATED_SWITCH_MARGIN_S = 300

_RATE_LIMIT_KEYS = ("five_hour", "seven_day")


def usage_pcts(usage: object) -> list[float]:
    """Extract valid 5h/7d utilization percentages from a usage entry."""
    if not isinstance(usage, dict):
        return []
    pcts: list[float] = []
    for key in _RATE_LIMIT_KEYS:
        entry = usage.get(key)
        if isinstance(entry, dict):
            pct = entry.get("pct")
            if isinstance(pct, (int, float)):
                pcts.append(float(pct))
    return pcts


def binding_pct(usage: object) -> float | None:
    """Highest 5h/7d utilization percentage, or None when unavailable."""
    pcts = usage_pcts(usage)
    return max(pcts) if pcts else None


def headroom(usage: object) -> float | None:
    """Remaining percentage before the binding rate-limit window hits 100%."""
    bp = binding_pct(usage)
    if bp is None:
        return None
    return 100.0 - bp


def cooldown_score(usage: object, threshold: int) -> tuple[int, float]:
    """Score a slot for cooldown-aware switch target selection."""
    if not isinstance(usage, dict):
        return (SLOT_SCORE_BUCKET_UNKNOWN, math.inf)

    pcts: list[float] = []
    saturated_resets: list[float] = []
    for key in _RATE_LIMIT_KEYS:
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


@dataclass(frozen=True)
class RankSlotsResult:
    """A ranked switch target with an optional human-readable decision note."""

    target: str | None
    note: str | None = None


def rank_slots(
    usages: dict[str, object],
    *,
    mode: Literal["headroom_best", "cooldown_aware"],
    threshold: int = 95,
    exclude: str | None = None,
    current: str | None = None,
    candidates: list[str] | None = None,
    is_switchable: Callable[[str], bool] | None = None,
) -> RankSlotsResult:
    """Pick a switch target from per-slot usage, by the given ranking mode."""
    if mode == "headroom_best":
        return _rank_headroom_best(
            usages,
            current=current,
            others=candidates or [],
        )
    return _rank_cooldown_aware(
        usages,
        threshold=threshold,
        exclude=exclude,
        candidates=candidates or [],
        is_switchable=is_switchable or (lambda _n: True),
    )


def _rank_headroom_best(
    usages: dict[str, object],
    *,
    current: str | None,
    others: list[str],
) -> RankSlotsResult:
    if not others:
        return RankSlotsResult(None, "none")

    current_headroom = headroom(usages.get(str(current)))
    if current_headroom is None:
        return RankSlotsResult(None, "current-unavailable")

    scored = [(headroom(usages.get(num)), num) for num in others]
    known = [(h, num) for h, num in scored if h is not None]
    if not known:
        return RankSlotsResult(None, "no-comparison")

    best_headroom, best_num = max(known, key=lambda t: t[0])
    if best_headroom > current_headroom:
        return RankSlotsResult(best_num, "")

    if any(h is None for h, _ in scored):
        return RankSlotsResult(None, "incomplete-comparison")
    if current_headroom <= 0:
        return RankSlotsResult(None, "exhausted")
    return RankSlotsResult(None, "stay")


def _rank_cooldown_aware(
    usages: dict[str, object],
    *,
    threshold: int,
    exclude: str | None,
    candidates: list[str],
    is_switchable: Callable[[str], bool],
) -> RankSlotsResult:
    if not candidates:
        return RankSlotsResult(None)

    scored: list[tuple[tuple[int, float], str]] = []
    for num in candidates:
        num_str = str(num)
        if exclude is not None and num_str == exclude:
            continue
        if not is_switchable(num_str):
            continue
        score = cooldown_score(usages.get(num_str), threshold)
        scored.append((score, num_str))

    if not scored:
        return RankSlotsResult(None)
    if all(s[0][0] == SLOT_SCORE_BUCKET_UNKNOWN for s in scored):
        return RankSlotsResult(None)

    scored.sort()
    return RankSlotsResult(scored[0][1])


def pick_best_from_snapshots(
    get_sequence_data: Callable[[], dict[str, Any] | None],
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

    result = rank_slots(
        snapshots,
        mode="cooldown_aware",
        threshold=threshold,
        exclude=exclude,
        candidates=[str(n) for n in sequence],
        is_switchable=is_switchable,
    )
    return result.target


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
        best_score = cooldown_score(decision.usage_by_slot.get(best), decision.threshold)
        if best_score[0] == SLOT_SCORE_BUCKET_SATURATED:
            active_score = cooldown_score(
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
