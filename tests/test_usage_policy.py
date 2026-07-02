"""Golden and contract tests for usage_policy SSOT.

Characterization tests pin observable decisions of both ranking paths before
and after unification.  The parametrized contract table asserts both modes
agree on shared fixtures where their semantics overlap.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.usage_policy import (
    cooldown_score as slot_switch_score,
    pick_best_from_snapshots,
    plan_automated_switch,
)
from claude_swap.models import AutoSwitchDecisionContext
from tests.conftest import bootstrap_switchable_accounts


def _usage(pct: float, *, resets_at: str | None = None) -> dict:
    entry: dict = {"pct": pct}
    if resets_at is not None:
        entry["resets_at"] = resets_at
    return {"five_hour": entry, "seven_day": {"pct": 0.0}}


def _headroom_select(
    temp_home: Path,
    current: str,
    usage_map: dict[str, object],
    *,
    num_accounts: int = 3,
) -> tuple[str | None, str]:
    s = bootstrap_switchable_accounts(temp_home, num_accounts=num_accounts)
    with (
        patch.object(s, "_usage_by_account", return_value=usage_map),
        patch.object(s, "_account_is_switchable", return_value=True),
    ):
        return s._select_best_switchable(current)


class TestHeadroomGolden:
    """Pin _select_best_switchable / --strategy best decisions."""

    def test_switches_to_strictly_better_headroom(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": _usage(50), "2": _usage(90), "3": _usage(20)},
        )
        assert target == "3"
        assert note == ""

    def test_stays_when_current_is_best(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": _usage(89), "2": _usage(100)},
            num_accounts=2,
        )
        assert target is None
        assert note == "stay"

    def test_all_exhausted(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": _usage(100), "2": _usage(100), "3": _usage(100)},
        )
        assert target is None
        assert note == "exhausted"

    def test_current_unavailable(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": None, "2": _usage(10)},
        )
        assert target is None
        assert note == "current-unavailable"

    def test_no_comparison(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": _usage(50), "2": None},
        )
        assert target is None
        assert note == "no-comparison"

    def test_incomplete_comparison(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": _usage(50), "2": _usage(90), "3": None},
        )
        assert target is None
        assert note == "incomplete-comparison"

    def test_tie_resolves_to_stay(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": _usage(50), "2": _usage(50)},
            num_accounts=2,
        )
        assert target is None
        assert note == "stay"


class TestCooldownPlannerGolden:
    """Pin auto-switch planner scoring and plan decisions."""

    def test_unsaturated_beats_saturated(self):
        unsat = slot_switch_score({"five_hour": {"pct": 30}}, 95)
        sat = slot_switch_score(
            {"five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}},
            95,
        )
        assert unsat < sat

    def test_pick_best_unsaturated_first(self):
        snapshots = {
            "1": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}},
            "2": {"five_hour": {"pct": 30}},
            "3": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T13:01:00+00:00"}},
        }
        best = pick_best_from_snapshots(
            lambda: {"sequence": [1, 2, 3]},
            lambda _n: True,
            95,
            snapshots,
            exclude="1",
        )
        assert best == "2"

    def test_pick_best_soonest_reset_when_all_saturated(self):
        snapshots = {
            "1": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}},
            "2": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T13:30:00+00:00"}},
            "3": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}},
        }
        assert (
            pick_best_from_snapshots(
                lambda: {"sequence": [1, 2, 3]},
                lambda _n: True,
                95,
                snapshots,
            )
            == "2"
        )

    def test_plan_stays_on_both_saturated_within_margin(self):
        decision = AutoSwitchDecisionContext(
            threshold=95,
            active_usage_pct=100.0,
            live_active_slot="1",
            sequence_active_slot="1",
            usage_by_slot={
                "1": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T16:01:00+00:00"}
                },
                "2": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}
                },
            },
        )
        plan = plan_automated_switch(
            decision,
            lambda _t, snaps, ex: pick_best_from_snapshots(
                lambda: {"sequence": [1, 2]},
                lambda _n: True,
                95,
                snaps,
                exclude=ex,
            ),
        )
        assert plan.outcome == "already_optimal"
        assert plan.target == "1"

    def test_plan_switches_when_reset_meaningfully_sooner(self):
        decision = AutoSwitchDecisionContext(
            threshold=95,
            active_usage_pct=100.0,
            live_active_slot="1",
            sequence_active_slot="1",
            usage_by_slot={
                "1": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T16:10:00+00:00"}
                },
                "2": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}
                },
            },
        )
        plan = plan_automated_switch(
            decision,
            lambda _t, snaps, ex: pick_best_from_snapshots(
                lambda: {"sequence": [1, 2]},
                lambda _n: True,
                95,
                snaps,
                exclude=ex,
            ),
        )
        assert plan.outcome == "chosen"
        assert plan.target == "2"


# Shared fixtures where both modes' decisions are independently checkable.
_CONTRACT_FIXTURES = [
    pytest.param(
        {
            "1": _usage(50),
            "2": _usage(90),
            "3": _usage(20),
        },
        "1",
        ["2", "3"],
        ("3", ""),
        "3",
        id="headroom-and-cooldown-pick-lowest-utilization",
    ),
    pytest.param(
        {
            "1": _usage(100),
            "2": _usage(100),
            "3": _usage(100),
        },
        "1",
        ["2", "3"],
        (None, "exhausted"),
        "2",
        id="all-saturated-headroom-exhausted-cooldown-picks-first-tie",
    ),
    pytest.param(
        {
            "1": _usage(100, resets_at="2026-06-14T16:00:00+00:00"),
            "2": _usage(100, resets_at="2026-06-14T13:30:00+00:00"),
            "3": _usage(30),
        },
        "1",
        ["2", "3"],
        ("3", ""),
        "3",
        id="cooldown-and-headroom-prefer-unsaturated",
    ),
]


class TestScoringContract:
    """Both ranking modes on a shared fixture set."""

    @pytest.mark.parametrize(
        "usages,current,others,headroom_expected,cooldown_expected",
        _CONTRACT_FIXTURES,
    )
    def test_both_modes(
        self,
        temp_home: Path,
        usages: dict[str, object],
        current: str,
        others: list[str],
        headroom_expected: tuple[str | None, str],
        cooldown_expected: str | None,
    ):
        from claude_swap.usage_policy import rank_slots

        hr = rank_slots(
            usages,
            mode="headroom_best",
            current=current,
            candidates=others,
        )
        assert (hr.target, hr.note or "") == headroom_expected

        cd = rank_slots(
            usages,
            mode="cooldown_aware",
            threshold=95,
            candidates=["1", "2", "3"],
            exclude=current,
            is_switchable=lambda _n: True,
        )
        assert cd.target == cooldown_expected

    @pytest.mark.parametrize(
        "usage,expected_headroom,expected_binding",
        [
            ({"five_hour": {"pct": 80.0}, "seven_day": {"pct": 20.0}}, 20.0, 80.0),
            ({"five_hour": {"pct": 40.0}}, 60.0, 40.0),
            ({"spend": {"pct": 99.0}, "five_hour": {"pct": 10.0}}, 90.0, 10.0),
            ({}, None, None),
            (None, None, None),
        ],
    )
    def test_parser_ssot(
        self,
        usage: object,
        expected_headroom: float | None,
        expected_binding: float | None,
    ):
        from claude_swap.usage_policy import binding_pct, headroom

        assert headroom(usage) == expected_headroom
        assert binding_pct(usage) == expected_binding
        assert oauth.account_headroom(usage) == expected_headroom
