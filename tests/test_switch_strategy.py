"""Usage-aware switch strategy, scoring contract, and auto-switch guards."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


class TestClassifySwitchPreconditions:
    """Focused coverage for _classify_switch_preconditions()."""

    def _setup(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _seed(
        self,
        s: ClaudeAccountSwitcher,
        num: int,
        email: str,
        *,
        creds: bool = True,
        config: bool = True,
    ) -> None:
        if creds:
            s._write_account_credentials(
                str(num),
                email,
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": f"sk-{num}",
                            "refreshToken": f"rt-{num}",
                        },
                    }
                ),
            )
        if config:
            s._write_account_config(
                str(num),
                email,
                json.dumps(
                    {
                        "oauthAccount": {
                            "emailAddress": email,
                            "accountUuid": f"uuid-{num}",
                        },
                    }
                ),
            )

        data = s._get_sequence_data() or {
            "activeAccountNumber": None,
            "lastUpdated": "",
            "sequence": [],
            "accounts": {},
        }
        data["accounts"][str(num)] = {
            "email": email,
            "uuid": f"uuid-{num}",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        s._write_json(s.sequence_file, data)

    def _set_live_identity(
        self, temp_home: Path, email: str, uuid: str = "uuid-1"
    ) -> None:
        (temp_home / ".claude").mkdir(parents=True, exist_ok=True)
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": email,
                        "accountUuid": uuid,
                    },
                }
            )
        )

    def test_fresh_machine(self, temp_home: Path):
        from claude_swap.models import SwitchPreconditionKind

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")

        result = s._classify_switch_preconditions()

        assert result.kind == SwitchPreconditionKind.FRESH_MACHINE
        assert result.identity is None
        assert result.data is None
        assert result.sequence is None
        assert result.current_slot is None

    def test_unmanaged(self, temp_home: Path):
        from claude_swap.models import SwitchPreconditionKind

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._set_live_identity(temp_home, "unknown@example.com", "uuid-x")

        result = s._classify_switch_preconditions()

        assert result.kind == SwitchPreconditionKind.UNMANAGED
        assert result.identity == ("unknown@example.com", "")
        assert result.data is None
        assert result.sequence is None
        assert result.current_slot is None

    def test_single_account(self, temp_home: Path):
        from claude_swap.models import SwitchPreconditionKind

        s = self._setup(temp_home)
        self._seed(s, 1, "solo@example.com")
        self._set_live_identity(temp_home, "solo@example.com", "uuid-1")

        result = s._classify_switch_preconditions()

        assert result.kind == SwitchPreconditionKind.SINGLE_ACCOUNT
        assert result.identity == ("solo@example.com", "")
        assert result.data is not None
        assert result.sequence == [1]
        assert result.current_slot == "1"

    def test_ready(self, temp_home: Path):
        from claude_swap.models import SwitchPreconditionKind

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._set_live_identity(temp_home, "a@example.com", "uuid-1")

        result = s._classify_switch_preconditions()

        assert result.kind == SwitchPreconditionKind.READY
        assert result.identity == ("a@example.com", "")
        assert result.data is not None
        assert result.sequence == [1, 2]
        assert result.current_slot is None

    def test_missing_sequence_data_degrades_cleanly(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A missing/corrupt sequence.json makes _get_sequence_data return None;
        # the classifier must treat it as empty (no accounts), not raise
        # AttributeError on None.get(...).
        from claude_swap.models import SwitchPreconditionKind

        s = self._setup(temp_home)
        self._set_live_identity(temp_home, "a@example.com", "uuid-1")
        monkeypatch.setattr(
            s, "_switch_bootstrap_identity", lambda: ("a@example.com", "")
        )
        monkeypatch.setattr(s, "_account_exists", lambda email, org: True)
        # Corrupt/missing sequence.json -> _get_sequence_data returns None; the
        # `or {}` guard must let the classifier reach a clean verdict instead of
        # raising AttributeError on None.get("sequence").
        monkeypatch.setattr(s, "_get_sequence_data", lambda: None)

        result = s._classify_switch_preconditions()

        assert result.kind == SwitchPreconditionKind.SINGLE_ACCOUNT


class TestUsageAwareSwitch:
    """--switch --strategy best / next-available pick targets by remaining 5h/7d
    quota. `best` only switches when another account is provably better and
    otherwise stays put; `next-available` rotates, skipping accounts at their
    limit (and anchors on the live account)."""

    def _setup(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _seed(self, s: ClaudeAccountSwitcher, num: int, email: str) -> None:
        s._write_account_credentials(
            str(num),
            email,
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": f"sk-{num}",
                        "refreshToken": f"rt-{num}",
                    },
                }
            ),
        )
        s._write_account_config(
            str(num),
            email,
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": email,
                        "accountUuid": f"uuid-{num}",
                    },
                }
            ),
        )
        data = s._get_sequence_data()
        data["accounts"][str(num)] = {
            "email": email,
            "uuid": f"uuid-{num}",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        s._write_json(s.sequence_file, data)

    def _make_live(self, temp_home: Path, email: str, num: int) -> None:
        (temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-live",
                        "refreshToken": "rt-live",
                    },
                }
            )
        )
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": email,
                        "accountUuid": f"uuid-{num}",
                    },
                }
            )
        )

    @staticmethod
    def _usage(pct: float) -> dict:
        return {"five_hour": {"pct": pct}, "seven_day": {"pct": 0.0}}

    def test_best_switches_to_more_headroom(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(50), "2": self._usage(90), "3": self._usage(20)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts"),
        ):
            s.switch(strategy="best")

        assert s._get_sequence_data()["activeAccountNumber"] == 3

    def test_best_stays_when_current_is_already_best(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(89), "2": self._usage(100)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts") as mock_list,
        ):
            s.switch(strategy="best")

        assert (
            "Already on the account with the most remaining quota"
            in capsys.readouterr().out
        )
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_best_all_exhausted_stays_put(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(100), "2": self._usage(100), "3": self._usage(100)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts"),
        ):
            s.switch(strategy="best")

        out = capsys.readouterr().out
        assert "All accounts are at their 5h/7d limit" in out
        assert "staying on Account-1" in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1

    def test_best_current_usage_unavailable_stays(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": None, "2": self._usage(10)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts") as mock_list,
        ):
            s.switch(strategy="best")

        assert "Current account usage is unavailable" in capsys.readouterr().out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_best_no_candidate_usage_stays(self, temp_home: Path, capsys):
        """Current known but no other account has usage data → no comparison is
        possible → stay (not rotation, not 'all exhausted')."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(50), "2": None}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts") as mock_list,
        ):
            s.switch(strategy="best")

        out = capsys.readouterr().out
        assert "No other account has usage data to compare" in out
        assert "All accounts are at their 5h/7d limit" not in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_best_incomplete_comparison_stays(self, temp_home: Path, capsys):
        """Current known + a known *worse* candidate + an unknown candidate →
        stay, without claiming 'most remaining quota' or 'all exhausted' (the
        unknown one can't be ruled better)."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(50), "2": self._usage(90), "3": None}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts") as mock_list,
        ):
            s.switch(strategy="best")

        out = capsys.readouterr().out
        assert "some usage is unavailable" in out
        assert "most remaining quota" not in out
        assert "All accounts are at their 5h/7d limit" not in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_best_current_exhausted_with_unknown_candidate_stays(
        self, temp_home: Path, capsys
    ):
        """Current known & exhausted + a known (also-exhausted) candidate + an
        unknown candidate → stay, but must NOT claim 'all exhausted' since the
        unknown account might have room."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(100), "2": self._usage(100), "3": None}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts") as mock_list,
        ):
            s.switch(strategy="best")

        out = capsys.readouterr().out
        assert "some usage is unavailable" in out
        assert "All accounts are at their 5h/7d limit" not in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_skip_exhausted_skips_limited_account(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(0), "2": self._usage(100), "3": self._usage(20)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts"),
        ):
            s.switch(strategy="next-available")

        out = capsys.readouterr().out
        assert "Skipping Account-2 (at 5h/7d limit)" in out
        assert s._get_sequence_data()["activeAccountNumber"] == 3

    def test_skip_exhausted_all_limited_stays_put(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(0), "2": self._usage(100), "3": self._usage(100)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts") as mock_list,
        ):
            s.switch(strategy="next-available")

        out = capsys.readouterr().out
        assert "staying on Account-1" in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_skip_exhausted_unknown_usage_is_not_skipped(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        with (
            patch.object(s, "_usage_by_account", return_value={"1": None, "2": None}),
            patch.object(s, "list_accounts"),
        ):
            s.switch(strategy="next-available")

        assert s._get_sequence_data()["activeAccountNumber"] == 2

    def test_next_available_anchors_on_live_account_under_drift(self, temp_home: Path):
        """When the live login has drifted from activeAccountNumber,
        next-available rotates relative to the LIVE account (current_num), not
        the stale record — so it never no-ops onto the account you're already
        on."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)
        self._make_live(temp_home, "b@example.com", 2)

        usage = {"1": self._usage(0), "2": self._usage(0), "3": self._usage(0)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts"),
        ):
            s.switch(strategy="next-available")

        assert s._get_sequence_data()["activeAccountNumber"] == 3


class TestSwitchQuietGuardsRaise:
    """Background auto-switch must raise ``SwitchError`` for "nothing to switch
    to" cases, not silently return — otherwise the monitor logs a false
    "switched account" on every threshold crossing.

    Interactive callers (``ManualSwitchIntent``) still see friendly print + return.
    """

    def _bootstrap(self, temp_home: Path, num_accounts: int) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        accounts: dict = {}
        sequence: list[int] = []
        for i in range(1, num_accounts + 1):
            accounts[str(i)] = {"email": f"a{i}@example.com"}
            sequence.append(i)
        data = {
            "accounts": accounts,
            "sequence": sequence,
            "activeAccountNumber": 1 if sequence else None,
        }
        s._write_json(s.sequence_file, data)
        # Pretend there's a current login for account 1 so we get past
        # the fresh-machine path and into the len(sequence)<2 guard.
        (temp_home / ".claude").mkdir(parents=True, exist_ok=True)
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "a1@example.com",
                        "accountUuid": "uuid-1",
                    }
                }
            )
        )
        return s

    def test_quiet_single_account_raises(self, temp_home: Path):
        from claude_swap.exceptions import SwitchError
        from claude_swap.models import BackgroundAutoSwitchIntent

        s = self._bootstrap(temp_home, num_accounts=1)
        decision = s.build_auto_switch_decision(95, 99.0)
        with pytest.raises(SwitchError, match="Only one account"):
            s.switch(BackgroundAutoSwitchIntent(decision=decision))

    def test_interactive_single_account_still_silent(self, temp_home: Path, capsys):
        s = self._bootstrap(temp_home, num_accounts=1)
        s.switch()
        out = capsys.readouterr().out
        assert "Only one account" in out

    def test_quiet_automated_no_trusted_signal_returns_false_without_stdout_leak(
        self,
        temp_home: Path,
        capsys,
    ):
        """Unattended path stays put on planning miss without polluting launchd stdout."""
        from claude_swap.models import BackgroundAutoSwitchIntent

        s = self._bootstrap(temp_home, num_accounts=3)
        decision = s.build_auto_switch_decision(95, 99.0)
        assert s.switch(BackgroundAutoSwitchIntent(decision=decision)) is False

        out = capsys.readouterr().out
        assert "Skipping" not in out


class TestSlotSwitchScore:
    """Cooldown-aware target picker — pure scoring function.

    Lock the score's total order: unsaturated (bucket 0) < saturated with
    known reset (bucket 1) < saturated unknown reset (bucket 1, inf) <
    unknown usage (bucket 2).  Within bucket 0, lower pct wins.  Within
    bucket 1 with known resets, sooner timestamp wins.
    """

    def test_unknown_usage_is_worst(self):
        from claude_swap.usage_policy import cooldown_score as slot_switch_score

        # Non-dict / empty / no recognised keys all collapse to bucket 2.
        for value in (None, {}, "not a dict", {"unexpected_field": 42}):
            bucket, _ = slot_switch_score(value, 95)
            assert bucket == 2, f"{value!r} should be bucket 2, got {bucket}"

    def test_unsaturated_prefers_lower_pct(self):
        from claude_swap.usage_policy import cooldown_score as slot_switch_score

        low = slot_switch_score({"five_hour": {"pct": 30}}, 95)
        mid = slot_switch_score({"five_hour": {"pct": 60}}, 95)
        high = slot_switch_score({"five_hour": {"pct": 80}}, 95)
        assert low < mid < high

    def test_unsaturated_takes_max_of_5h_and_7d(self):
        from claude_swap.usage_policy import cooldown_score as slot_switch_score

        # The blocking limit is the higher of the two; score must reflect it.
        out = slot_switch_score(
            {"five_hour": {"pct": 20}, "seven_day": {"pct": 70}},
            95,
        )
        assert out == (0, 70.0)

    def test_saturated_with_resets_prefers_soonest(self):
        from claude_swap.usage_policy import cooldown_score as slot_switch_score

        soon = slot_switch_score(
            {"five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}},
            95,
        )
        later = slot_switch_score(
            {"five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}},
            95,
        )
        assert soon < later
        assert soon[0] == 1 and later[0] == 1  # both saturated bucket

    def test_saturated_without_resets_ranks_last_in_bucket(self):
        from claude_swap.usage_policy import cooldown_score as slot_switch_score

        with_reset = slot_switch_score(
            {"five_hour": {"pct": 100, "resets_at": "2099-12-31T00:00:00+00:00"}},
            95,
        )
        no_reset = slot_switch_score({"five_hour": {"pct": 100}}, 95)
        assert with_reset < no_reset
        assert no_reset == (1, math.inf)

    def test_total_order_across_all_buckets(self):
        """The whole point: tuple-sort gives the right global ranking."""
        from claude_swap.usage_policy import cooldown_score as slot_switch_score

        candidates = [
            ("A-unsat-low", {"five_hour": {"pct": 30}}),
            ("B-unsat-mid", {"five_hour": {"pct": 80}}),
            (
                "C-sat-soon",
                {"five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}},
            ),
            (
                "D-sat-late",
                {"five_hour": {"pct": 100, "resets_at": "2026-06-14T18:00:00+00:00"}},
            ),
            ("E-sat-no-reset", {"five_hour": {"pct": 100}}),
            ("F-unknown", {}),
        ]
        scored = sorted((slot_switch_score(u, 95), name) for name, u in candidates)
        names_in_order = [name for _, name in scored]
        # Loose contract: unsat comes before sat; sat-with-reset before
        # sat-without; unknown is last.  Exact pct ordering matters within bucket.
        assert names_in_order[:2] == ["A-unsat-low", "B-unsat-mid"]
        assert names_in_order[2:4] == ["C-sat-soon", "D-sat-late"]
        assert names_in_order[-2] == "E-sat-no-reset"
        assert names_in_order[-1] == "F-unknown"

    def test_invalid_resets_at_falls_to_no_reset_bucket(self):
        """Malformed timestamps must not raise — they degrade the slot to
        'saturated without reset' so the picker still ranks it sensibly."""
        from claude_swap.usage_policy import cooldown_score as slot_switch_score

        out = slot_switch_score(
            {"five_hour": {"pct": 100, "resets_at": "not-a-timestamp"}},
            95,
        )
        assert out == (1, math.inf)


class TestPickBestSwitchTarget:
    """Cache-first picker — integration with the on-disk usage cache."""

    def _bootstrap(
        self, temp_home: Path, num_accounts: int = 3
    ) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        accounts: dict = {}
        for i in range(1, num_accounts + 1):
            accounts[str(i)] = {"email": f"a{i}@example.com"}
        data = {
            "accounts": accounts,
            "sequence": list(range(1, num_accounts + 1)),
            "activeAccountNumber": 1,
        }
        s._write_json(s.sequence_file, data)
        return s

    def _seed_cache(self, switcher: ClaudeAccountSwitcher, payload: dict):
        from claude_swap.cache import write_cache
        from claude_swap.usage_cache import _usage_to_cache

        write_cache(
            switcher.usage_cache_path,
            {k: _usage_to_cache(v) for k, v in payload.items()},
        )

    def _pick_best(
        self,
        s: ClaudeAccountSwitcher,
        threshold: int,
        exclude: str | None = None,
    ) -> str | None:
        from claude_swap.cache import read_cache_data
        from claude_swap.usage_cache import _usage_from_cache

        cached = read_cache_data(s.usage_cache_path, default={}) or {}
        if not isinstance(cached, dict):
            cached = {}
        snapshots = {str(k): _usage_from_cache(v) for k, v in cached.items()}
        return s._pick_best_from_snapshots(threshold, snapshots, exclude=exclude)

    def test_cold_cache_returns_none(self, temp_home: Path):
        """No usage cache → return None so caller falls back to round-robin.
        This is the load-bearing 'first run' contract."""
        s = self._bootstrap(temp_home)
        # All switchable but no cache → all bucket-2 → return None
        with patch.object(s, "_account_is_switchable", return_value=True):
            assert self._pick_best(s, 95) is None

    def test_picks_unsaturated_over_saturated(self, temp_home: Path):
        """When at least one slot is unsaturated, it wins regardless of
        how soon a saturated slot would free up."""
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(
            s,
            {
                "1": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}
                },
                "2": {"five_hour": {"pct": 30}},  # the winner
                "3": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T13:01:00+00:00"}
                },
            },
        )
        with patch.object(s, "_account_is_switchable", return_value=True):
            assert self._pick_best(s, 95, exclude="1") == "2"

    def test_picks_soonest_reset_when_all_saturated(self, temp_home: Path):
        """The headline use case: all accounts at limit, pick the one that
        will free up first.  This is what the user explicitly asked for."""
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(
            s,
            {
                "1": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}
                },
                "2": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T13:30:00+00:00"}
                },  # soonest
                "3": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}
                },
            },
        )
        with patch.object(s, "_account_is_switchable", return_value=True):
            assert self._pick_best(s, 95) == "2"
            assert self._pick_best(s, 95, exclude="1") == "2"

    def test_global_pick_orders_saturated_by_cooldown(self, temp_home: Path):
        """From any non-optimal saturated slot, the picker targets the global
        soonest ``resets_at`` (Account-2), not round-robin sequence order."""
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(
            s,
            {
                "1": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}
                },
                "2": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T13:30:00+00:00"}
                },
                "3": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}
                },
            },
        )
        with patch.object(s, "_account_is_switchable", return_value=True):
            assert self._pick_best(s, 95) == "2"
            # Active on soonest → global optimum is self.
            assert self._pick_best(s, 95) == "2"

    def test_switch_stays_when_already_on_soonest_saturated(self, temp_home: Path):
        """Automated path must not rotate away from the soonest-to-free slot."""
        from claude_swap.models import BackgroundAutoSwitchIntent

        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(
            s,
            {
                "1": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}
                },
                "2": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T13:30:00+00:00"}
                },
                "3": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}
                },
            },
        )
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 2
        s._write_json(s.sequence_file, data)
        with (
            patch.object(s, "_account_is_switchable", return_value=True),
            patch.object(
                s, "_get_current_account", return_value=("a2@example.com", "uuid-2")
            ),
            patch.object(s, "_account_exists", return_value=True),
            patch.object(s, "_perform_switch") as mock_perform,
        ):
            decision = s.build_auto_switch_decision(95, 100.0)
            switched = s.switch(BackgroundAutoSwitchIntent(decision=decision))
        assert switched is False
        mock_perform.assert_not_called()

    def test_automated_plan_rejects_stale_cache(self, temp_home: Path):
        """Expired cache entries must not drive unattended target planning."""
        import json

        s = self._bootstrap(temp_home, num_accounts=2)
        cache_path = s.backup_dir / "cache" / "usage.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "timestamp": time.time(),
                    "data": {
                        "1": {
                            "five_hour": {"pct": 30},
                            "_cached_at": time.time() - 9999,
                        },
                        "2": {
                            "five_hour": {"pct": 40},
                            "_cached_at": time.time() - 9999,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        decision = s.build_auto_switch_decision(95, 99.0)
        plan = s.plan_automated_switch(decision)
        assert plan.outcome == "no_trusted_signal"

    def test_automated_plan_uses_live_active_slot(self, temp_home: Path):
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(
            s,
            {
                "1": {
                    "five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}
                },
                "2": {"five_hour": {"pct": 30}},
                "3": {"five_hour": {"pct": 80}},
            },
        )
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)
        with (
            patch.object(s, "_account_is_switchable", return_value=True),
            patch.object(
                s, "_get_current_account", return_value=("a3@example.com", "")
            ),
        ):
            decision = s.build_auto_switch_decision(95, 96.0)
            assert decision.live_active_slot == "3"
            plan = s.plan_automated_switch(decision)
        assert plan.outcome == "chosen"
        assert plan.target == "2"

    def test_plan_stays_put_when_both_accounts_saturated_similar_resets(
        self,
        temp_home: Path,
    ):
        """When BOTH accounts are saturated and the target resets at most
        _SATURATED_SWITCH_MARGIN_S=300s sooner, the plan must return
        already_optimal to prevent indefinite oscillation.

        Root cause: continued use on the active account advances its resets_at
        forward on every poll, making the idle account always appear marginally
        'better'.  Without the margin guard the monitor switches back and forth
        every 60s triggering the multi-session race warning on every swap."""
        s = self._bootstrap(temp_home, num_accounts=2)
        # Both saturated; Account-2 resets only 60s sooner — within 300s margin.
        self._seed_cache(
            s,
            {
                "1": {
                    "five_hour": {
                        "pct": 100,
                        "resets_at": "2026-06-14T16:01:00+00:00",
                    }
                },
                "2": {
                    "five_hour": {
                        "pct": 100,
                        "resets_at": "2026-06-14T16:00:00+00:00",  # 60s sooner
                    }
                },
            },
        )
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)
        with (
            patch.object(s, "_account_is_switchable", return_value=True),
            patch.object(
                s,
                "_get_current_account",
                return_value=("a1@example.com", ""),
            ),
        ):
            decision = s.build_auto_switch_decision(95, 100.0)
            plan = s.plan_automated_switch(decision)
        assert plan.outcome == "already_optimal", (
            "must not oscillate when target resets < 300s sooner"
        )

    def test_plan_switches_when_target_resets_meaningfully_sooner(
        self,
        temp_home: Path,
    ):
        """When the best target is saturated but resets >300s sooner than the
        active account, the switch IS worth making — the user will get capacity
        back meaningfully earlier on the target account."""
        s = self._bootstrap(temp_home, num_accounts=2)
        # Account-2 resets 10 minutes (600s) sooner — outside the 300s margin.
        self._seed_cache(
            s,
            {
                "1": {
                    "five_hour": {
                        "pct": 100,
                        "resets_at": "2026-06-14T16:10:00+00:00",
                    }
                },
                "2": {
                    "five_hour": {
                        "pct": 100,
                        "resets_at": "2026-06-14T16:00:00+00:00",  # 600s sooner
                    }
                },
            },
        )
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)
        with (
            patch.object(s, "_account_is_switchable", return_value=True),
            patch.object(
                s,
                "_get_current_account",
                return_value=("a1@example.com", ""),
            ),
        ):
            decision = s.build_auto_switch_decision(95, 100.0)
            plan = s.plan_automated_switch(decision)
        assert plan.outcome == "chosen"
        assert plan.target == "2"

    def test_trusted_snapshots_return_present_trusted_subset(self, temp_home: Path):
        # A slot missing from the cache is excluded, not fleet-poisoning: the
        # trusted slots that ARE present remain available for planning.
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(s, {"2": {"five_hour": {"pct": 40}}})
        with patch.object(s, "_account_is_switchable", return_value=True):
            assert s._trusted_usage_snapshots() == {"2": {"five_hour": {"pct": 40}}}

    def test_excludes_specified_slot(self, temp_home: Path):
        """The active account is excluded by the caller; without exclusion
        a soonest-reset active account would otherwise re-pick itself."""
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(
            s,
            {
                "1": {"five_hour": {"pct": 30}},  # active, best score
                "2": {"five_hour": {"pct": 60}},
                "3": {"five_hour": {"pct": 80}},
            },
        )
        with patch.object(s, "_account_is_switchable", return_value=True):
            # active=1 excluded → best of {2,3} is 2
            assert self._pick_best(s, 95, exclude="1") == "2"

    def test_skips_non_switchable_slots(self, temp_home: Path):
        """A slot with great usage but no stored credentials must never
        be returned — we'd raise SwitchError trying to activate it."""
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(
            s,
            {
                "1": {"five_hour": {"pct": 80}},
                "2": {"five_hour": {"pct": 10}},  # best by score, but unswitchable
                "3": {"five_hour": {"pct": 50}},
            },
        )

        def switchable(num):
            return num != "2"

        with patch.object(s, "_account_is_switchable", side_effect=switchable):
            assert self._pick_best(s, 95, exclude="1") == "3"

    def test_cold_cache_partial_falls_back_to_signal(self, temp_home: Path):
        """When some slots have cache data and others don't, we still pick
        the best of what we know — only ALL-cold returns None."""
        s = self._bootstrap(temp_home, num_accounts=3)
        # Only slot 2 has cached usage
        self._seed_cache(s, {"2": {"five_hour": {"pct": 40}}})
        with patch.object(s, "_account_is_switchable", return_value=True):
            # Slot 2 has signal (bucket 0); slots 1 & 3 are bucket 2.
            # exclude=1 (active), so candidates are {2, 3} → 2 wins.
            assert self._pick_best(s, 95, exclude="1") == "2"
