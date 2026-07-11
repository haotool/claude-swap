"""Switch-time provenance guard (upstream issue #117) and its diagnostics.

Upstream's TestProvenanceGuard suite, adapted to the fork's layout: display
diagnostics live on ``ListReporter`` and the active fetch is
``ListReporter.fetch_active_usage``.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.exceptions import SwitchError
from claude_swap.list_reporter import ListReporter
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry

from tests.test_switcher_switch import TestPerformSwitchPostDisplay


def _read_safety_copy(switcher, entry_id: str) -> str:
    """Decode a preserved credential entry straight from its file (the store
    is write-only by design — no read helper exists in production code)."""
    path = switcher._store._stash_entry_path(entry_id)
    return base64.b64decode(path.read_text().strip(), validate=True).decode()


def _no_target_refresh(switcher):
    """Skip the fork's pre-activation target refresh (network) in these tests."""
    return patch.object(
        switcher,
        "_refresh_target_credentials_before_activation",
        side_effect=lambda num, email, creds, force=False: creds,
    )


class TestProvenanceGuard:
    """Issue #117, fail-open hybrid: the identity oracle is advisory.

    Positively-foreign bytes are preserved and never written into a slot;
    an *unverifiable* divergence falls back to the exact pre-fix backup, so
    endpoint state never changes whether or how a switch completes.

    Two timelines, kept explicitly separate:

    - *normal operation* — Claude Code legitimately rotated the active
      account's token (same lineage, or resolved to the outgoing slot);
    - *fault injection* — the live store holds a foreign or unattributable
      credential (the poisoning precondition).
    """

    _setup_two_accounts = TestPerformSwitchPostDisplay._setup_two_accounts
    _install_store_patches = staticmethod(
        TestPerformSwitchPostDisplay._install_store_patches
    )

    _A1_BACKUP = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-stored-1", "refreshToken": "rt-1",
    }})

    def _run_switch(self, switcher, resolver=None, quiet=True):
        with patch.object(switcher, "list_accounts"), _no_target_refresh(
            switcher
        ), patch(
            "claude_swap.oauth.fetch_oauth_profile",
            side_effect=(lambda token: resolver) if resolver is not None
            else (lambda token: None),
        ):
            return switcher._perform_switch("2", emit_output=not quiet)

    # -- normal-operation timeline ---------------------------------------

    def test_byte_identical_live_skips_credential_backup(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Nothing rotated since cswap's own write → nothing to capture."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        live_state = {"creds": self._A1_BACKUP}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        writes: list = []
        try:
            switcher._write_account_credentials = (
                lambda n, e, c: writes.append((n, e))
            )
            op = self._run_switch(switcher)
        finally:
            for p in patches:
                p.stop()
        assert writes == []  # credential backup skipped entirely
        assert op["warnings"] == []
        # Config backup still refreshed.
        assert configs_store.get(("1", "test@example.com"))

    def test_access_token_rotation_same_lineage_backs_up(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Same refresh token, new access token → provenance is local, no
        network needed, backup captures the rotation."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-fresh-1", "refreshToken": "rt-1", "expiresAt": 9,
        }})
        live_state = {"creds": rotated}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch(
                "claude_swap.oauth.fetch_oauth_profile",
            ) as profile:
                with patch.object(switcher, "list_accounts"), \
                     _no_target_refresh(switcher):
                    op = switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()
        profile.assert_not_called()
        assert creds_store[("1", "test@example.com")] == rotated
        assert op["warnings"] == []

    def test_full_rotation_resolved_to_outgoing_slot_backs_up(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Refresh-token rotation of the *same* account (the routine case a
        long-lived Claude Code session produces) re-syncs into the slot."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-fresh-1", "refreshToken": "rt-1-rotated",
        }})
        live_state = {"creds": rotated}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-1", "email": "test@example.com",
                "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == rotated
        assert op["warnings"] == []
        assert switcher.list_unclaimed_credentials() == {}

    def test_resolution_backfills_empty_slot_uuid(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        sample_sequence_data["accounts"]["1"]["uuid"] = ""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        live_state = {"creds": json.dumps({"claudeAiOauth": {
            "accessToken": "sk-f", "refreshToken": "rt-1-rotated",
        }})}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            self._run_switch(switcher, resolver={
                "uuid": "uuid-resolved", "email": "test@example.com",
                "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["uuid"] == "uuid-resolved"

    # -- fault-injection timeline -----------------------------------------

    def test_foreign_credential_preserved_never_backed_into_any_slot(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """The poisoning precondition: live bytes belong to another managed
        slot (uuid-positive). They must be preserved as a safety copy — not
        written into the outgoing slot, and not routed into the resolved slot
        either (identity proves ownership, not generation freshness). Here the
        foreign slot is also the switch target: the switch still activates the
        target's *stored* backup, never the displaced live bytes."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        a2_backup = creds_store[("2", "account2@example.com")]
        foreign = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2-rotated", "refreshToken": "rt-2-rotated",
        }})
        live_state = {"creds": foreign}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-2", "email": "account2@example.com",
                "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        # Outgoing slot untouched; resolved slot untouched.
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP
        assert creds_store[("2", "account2@example.com")] == a2_backup
        # Foreign bytes preserved byte-exactly.
        entries = switcher.list_unclaimed_credentials()
        assert len(entries) == 1
        (entry_id,) = entries
        assert _read_safety_copy(switcher, entry_id) == foreign
        assert entries[entry_id]["resolvedIdentity"]["uuid"] == "uuid-2"
        assert any(
            "ownership mismatch" in w and "Account-2" in w
            for w in op["warnings"]
        )
        # The switch itself proceeded, onto the stored backup.
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == "sk-stale-2"

    def test_foreign_synced_lineage_warns_without_any_write(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Foreign bytes whose lineage already sits in that slot's backup:
        nothing needs preserving, nothing may be written — warn only."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        a2_backup = creds_store[("2", "account2@example.com")]
        live_state = {"creds": a2_backup}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-2", "email": "account2@example.com",
                "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP
        assert creds_store[("2", "account2@example.com")] == a2_backup
        assert switcher.list_unclaimed_credentials() == {}
        assert any("already matches Account-2" in w for w in op["warnings"])

    def test_alien_credential_preserved_and_skipped(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Resolved to no managed slot: preserve, warn, proceed — the message
        can't name a slot, so it recommends a plain `cswap add`."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        alien = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-x",
        }})
        live_state = {"creds": alien}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-unmanaged", "email": "elsewhere@example.com",
                "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP
        entries = switcher.list_unclaimed_credentials()
        assert len(entries) == 1
        (entry_id,) = entries
        assert _read_safety_copy(switcher, entry_id) == alien
        assert any(
            "does not match a managed account" in w for w in op["warnings"]
        )

    def test_blank_stored_uuid_email_match_is_alien_not_foreign(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A cross-slot attribution must be uuid-positive: an email+org match
        against a slot with no recorded uuid is preserved as alien, and that
        slot is never named or touched."""
        sample_sequence_data["accounts"]["2"]["uuid"] = ""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        a2_backup = creds_store[("2", "account2@example.com")]
        drifted = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-d", "refreshToken": "rt-d",
        }})
        live_state = {"creds": drifted}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-2-real", "email": "account2@example.com",
                "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP
        assert creds_store[("2", "account2@example.com")] == a2_backup
        assert len(switcher.list_unclaimed_credentials()) == 1
        assert any(
            "does not match a managed account" in w for w in op["warnings"]
        )

    def test_partial_identity_uuid_only_matching_outgoing_slot_backs_up(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A response that dropped email/organization but whose uuid equals
        the outgoing slot's must classify own-rotated: partial data must
        never turn a legitimate rotation into preserve-and-skip (that would
        recreate the fail-closed stale-slot behavior on schema drift)."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-f", "refreshToken": "rt-1-rotated",
        }})
        live_state = {"creds": rotated}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-1", "email": None, "organizationUuid": None,
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == rotated
        assert switcher.list_unclaimed_credentials() == {}
        assert op["warnings"] == []

    def test_partial_identity_uuid_match_with_slot_org_recorded(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Same, with the outgoing slot recording an organization: org must
        agree only when *both* sides carry one, so a uuid-only response
        still resolves to the outgoing slot."""
        sample_sequence_data["accounts"]["1"]["organizationUuid"] = "org-1"
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        data = switcher._get_sequence_data()
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-f", "refreshToken": "rt-1-rotated",
        }})
        with patch.object(
            switcher, "_read_account_credentials",
            return_value=self._A1_BACKUP,
        ):
            kind, foreign_slot = switcher._classify_outgoing_credential(
                "1", "test@example.com", rotated,
                {"live": rotated,
                 "resolved": {"uuid": "uuid-1", "email": None,
                              "organizationUuid": None}},
                data,
            )
        assert (kind, foreign_slot) == ("own-rotated", None)

    def test_partial_identity_matching_nothing_falls_open(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A response missing email/organization that matches no slot is
        indistinguishable from schema drift → unresolved (pre-fix backup),
        never alien (preserve-and-skip)."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        mystery = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-x",
        }})
        live_state = {"creds": mystery}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-nobody", "email": None,
                "organizationUuid": None,
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == mystery
        assert switcher.list_unclaimed_credentials() == {}
        assert op["warnings"] == []

    def test_foreign_attribution_survives_missing_email(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """uuid+org is a positive cross-slot match even without an email —
        preserve-and-skip stays available where the evidence is complete
        enough to be positive."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        a2_backup = creds_store[("2", "account2@example.com")]
        foreign = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2-rotated", "refreshToken": "rt-2-rotated",
        }})
        live_state = {"creds": foreign}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-2", "email": None, "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP
        assert creds_store[("2", "account2@example.com")] == a2_backup
        assert len(switcher.list_unclaimed_credentials()) == 1
        assert any("Account-2" in w for w in op["warnings"])

    def test_unresolvable_mismatch_backs_up_pre_fix(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """The fail-open core: offline / endpoint failure means the identity
        oracle is silent, and the switch behaves exactly pre-fix — the
        divergent bytes are backed into the outgoing slot (most such
        divergences are the account's own rotation; skipping would leave the
        slot holding a consumed token), with no safety copy and no
        user-facing warning."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        mystery = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-x",
        }})
        live_state = {"creds": mystery}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver=None)
        finally:
            for p in patches:
                p.stop()
        # Pre-fix backup happened; the switch completed quietly.
        assert creds_store[("1", "test@example.com")] == mystery
        assert switcher.list_unclaimed_credentials() == {}
        assert op["warnings"] == []
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == "sk-stale-2"

    def test_profile_exception_falls_back_to_pre_fix_backup(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A raising profile call must be indistinguishable from None: the
        switch completes with the pre-fix backup."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        mystery = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-x",
        }})
        live_state = {"creds": mystery}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch.object(switcher, "list_accounts"), \
                 _no_target_refresh(switcher), patch(
                "claude_swap.oauth.fetch_oauth_profile",
                side_effect=OSError("network down"),
            ):
                op = switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == mystery
        assert switcher.list_unclaimed_credentials() == {}
        assert op["warnings"] == []

    def test_safety_copy_failure_aborts_before_live_overwrite(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Preservation is the safety boundary for positively-foreign bytes:
        no safety copy, no switch. (Never reachable from endpoint failure —
        the unresolved path writes no safety copy.)"""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        mystery = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-x",
        }})
        live_state = {"creds": mystery}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch.object(
                switcher._store, "_write_unclaimed_credential",
                side_effect=OSError("disk full"),
            ):
                with pytest.raises(Exception):
                    self._run_switch(switcher, resolver={
                        "uuid": "uuid-unmanaged",
                        "email": "elsewhere@example.com",
                        "organizationUuid": "",
                    })
        finally:
            for p in patches:
                p.stop()
        # Nothing moved: live store, outgoing backup both untouched.
        assert live_state["creds"] == mystery
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP

    def test_moved_bytes_between_prefetch_and_lock_fall_to_unresolved(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A pre-lock resolution only binds to the bytes it resolved: when
        the live store moved in between, the stale answer is discarded and
        the switch falls back to the pre-fix backup of the current bytes."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        provenance = {
            "live": "something-else-entirely",
            "resolved": {"uuid": "uuid-2", "email": "account2@example.com",
                         "organizationUuid": ""},
        }
        moved = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-m", "refreshToken": "rt-m",
        }})
        live_state = {"creds": moved}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch.object(switcher, "list_accounts"), \
                 _no_target_refresh(switcher):
                op = switcher._perform_switch(
                    "2", emit_output=False, provenance=provenance
                )
        finally:
            for p in patches:
                p.stop()
        # Stale resolution rejected → unresolved → pre-fix backup, no copy.
        assert creds_store[("1", "test@example.com")] == moved
        assert switcher.list_unclaimed_credentials() == {}
        assert op["warnings"] == []


class TestSelfSwitchProvenance:
    """The already-active short-circuits must not hide a diverged live login."""

    _setup_two_accounts = TestPerformSwitchPostDisplay._setup_two_accounts
    _install_store_patches = staticmethod(
        TestPerformSwitchPostDisplay._install_store_patches
    )

    def test_matching_self_switch_is_noop(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1", "refreshToken": "rt-1",
        }})
        creds_store[("1", "test@example.com")] = backup
        live_state = {"creds": backup}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            result = switcher.switch_to("1", json_output=True)
        finally:
            for p in patches:
                p.stop()
        assert result["switched"] is False
        assert result["reason"] == "already-active"

    def test_diverged_unresolvable_self_switch_noops_silently(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Endpoint trouble must be invisible on the self-switch path too:
        an unclassifiable divergence is an ordinary already-active no-op
        (exact pre-fix behavior — no mutation, no user-facing warning; the
        diagnostic goes to the log). Leaving everything untouched is also the
        safe write: activating the stored backup over an unverified live
        credential could replace a fresh rotated token with its consumed
        ancestor."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1", "refreshToken": "rt-1",
        }})
        diverged = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1-new", "refreshToken": "rt-1-rotated",
        }})
        creds_store[("1", "test@example.com")] = backup
        live_state = {"creds": diverged}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch("claude_swap.oauth.fetch_oauth_profile", return_value=None):
                result = switcher.switch_to("1", json_output=True)
        finally:
            for p in patches:
                p.stop()
        assert result["switched"] is False
        assert result["reason"] == "already-active"
        assert result.get("warnings", []) == []
        assert live_state["creds"] == diverged  # left untouched
        assert creds_store[("1", "test@example.com")] == backup

    def test_diverged_resolved_self_switch_reconciles(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A rotation proven to be the slot's own gets re-synced to backup."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1", "refreshToken": "rt-1",
        }})
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1-new", "refreshToken": "rt-1-rotated",
        }})
        creds_store[("1", "test@example.com")] = backup
        configs_store[("1", "test@example.com")] = json.dumps({
            "oauthAccount": {"emailAddress": "test@example.com", "accountUuid": "uuid-1"},
        })
        live_state = {"creds": rotated}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch(
                "claude_swap.oauth.fetch_oauth_profile",
                return_value={"uuid": "uuid-1", "email": "test@example.com",
                              "organizationUuid": ""},
            ), patch.object(switcher, "list_accounts"):
                result = switcher.switch_to("1", json_output=True)
        finally:
            for p in patches:
                p.stop()
        # The rotation was captured into the slot's backup.
        assert creds_store[("1", "test@example.com")] == rotated
        assert result["switched"] is False or result["to"]["number"] == 1


class TestDuplicateAccountDetection:
    def _switcher(self, temp_home, sample_sequence_data):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        return switcher

    def test_same_fingerprint_across_slots_flagged(
        self, temp_home, sample_sequence_data,
    ):
        switcher = self._switcher(temp_home, sample_sequence_data)
        same = json.dumps({"claudeAiOauth": {
            "accessToken": "sk", "refreshToken": "rt-shared",
        }})
        info = [
            (1, "account1@example.com", "", "", True, same),
            (2, "account2@example.com", "", "", False, same),
        ]
        warnings = ListReporter(switcher).duplicate_account_warnings(info)
        assert len(warnings) == 1
        assert "Account-1 and Account-2" in warnings[0]

    def test_same_uuid_across_slots_flagged(
        self, temp_home, sample_sequence_data,
    ):
        sample_sequence_data["accounts"]["2"]["uuid"] = "uuid-1"
        switcher = self._switcher(temp_home, sample_sequence_data)
        info = [
            (1, "account1@example.com", "", "", True, "creds-a"),
            (2, "account2@example.com", "", "", False, "creds-b"),
        ]
        warnings = ListReporter(switcher).duplicate_account_warnings(info)
        assert len(warnings) == 1
        assert "both authenticate" in warnings[0]

    def test_empty_uuids_never_match_each_other(
        self, temp_home, sample_sequence_data,
    ):
        """add-token placeholders (uuid "") must not false-positive."""
        sample_sequence_data["accounts"]["1"]["uuid"] = ""
        sample_sequence_data["accounts"]["2"]["uuid"] = ""
        switcher = self._switcher(temp_home, sample_sequence_data)
        info = [
            (1, "setup-token-1@token.local", "", "", True, "creds-a"),
            (2, "setup-token-2@token.local", "", "", False, "creds-b"),
        ]
        assert ListReporter(switcher).duplicate_account_warnings(info) == []

    def test_clean_accounts_produce_no_warnings(
        self, temp_home, sample_sequence_data,
    ):
        switcher = self._switcher(temp_home, sample_sequence_data)
        info = [
            (1, "account1@example.com", "", "", True,
             json.dumps({"claudeAiOauth": {"refreshToken": "rt-1"}})),
            (2, "account2@example.com", "", "", False,
             json.dumps({"claudeAiOauth": {"refreshToken": "rt-2"}})),
        ]
        assert ListReporter(switcher).duplicate_account_warnings(info) == []


class TestLockstepUsageDetection:
    """Heuristic detector for the different-generation collapse (issue #117):
    fingerprints and sequence identities look distinct, but both slots report
    the same account's usage — identical percentages and reset instants."""

    def _switcher(self, temp_home, sample_sequence_data):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        return switcher

    @staticmethod
    def _info(n=2):
        return [
            (i, f"account{i}@example.com", "", "", i == 1, f"creds-{i}")
            for i in range(1, n + 1)
        ]

    @staticmethod
    def _entry(h5_pct, h5_reset, d7_pct, d7_reset):
        usage = {}
        if h5_pct is not None:
            usage["five_hour"] = {"pct": h5_pct}
            if h5_reset is not None:
                usage["five_hour"]["resets_at"] = h5_reset
        if d7_pct is not None:
            usage["seven_day"] = {"pct": d7_pct}
            if d7_reset is not None:
                usage["seven_day"]["resets_at"] = d7_reset
        return UsageEntry(last_good=usage, fetched_at=time.time(), age_s=0.0)

    def test_identical_usage_and_resets_flagged(
        self, temp_home, sample_sequence_data,
    ):
        switcher = self._switcher(temp_home, sample_sequence_data)
        entries = {
            "1": self._entry(25.0, "2026-07-10T12:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
            "2": self._entry(25.0, "2026-07-10T12:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
        }
        warnings = ListReporter(switcher).lockstep_usage_warnings(self._info(), entries)
        assert len(warnings) == 1
        assert "Account-1 and Account-2" in warnings[0]
        assert "may be the same account" in warnings[0]

    def test_differing_resets_not_flagged(self, temp_home, sample_sequence_data):
        switcher = self._switcher(temp_home, sample_sequence_data)
        entries = {
            "1": self._entry(25.0, "2026-07-10T12:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
            "2": self._entry(25.0, "2026-07-10T13:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
        }
        assert ListReporter(switcher).lockstep_usage_warnings(self._info(), entries) == []

    def test_idle_accounts_without_resets_not_flagged(
        self, temp_home, sample_sequence_data,
    ):
        """Two fresh accounts at 0% with no reset scheduled are
        indistinguishable — never flag them."""
        switcher = self._switcher(temp_home, sample_sequence_data)
        entries = {
            "1": self._entry(0.0, None, 0.0, None),
            "2": self._entry(0.0, None, 0.0, None),
        }
        assert ListReporter(switcher).lockstep_usage_warnings(self._info(), entries) == []

    def test_sentinel_usage_never_compared(self, temp_home, sample_sequence_data):
        """API-key slots (and other sentinel states) carry no comparable
        usage."""
        switcher = self._switcher(temp_home, sample_sequence_data)
        entries = {
            "1": UsageEntry(sentinel="api-key"),
            "2": UsageEntry(sentinel="api-key"),
        }
        assert ListReporter(switcher).lockstep_usage_warnings(self._info(), entries) == []

    def test_payload_carries_lockstep_warnings_additively(
        self, temp_home, sample_sequence_data,
    ):
        switcher = self._switcher(temp_home, sample_sequence_data)
        lockstep = {
            "1": self._entry(25.0, "2026-07-10T12:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
            "2": self._entry(25.0, "2026-07-10T12:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
        }
        payload = ListReporter(switcher).build_list_payload(self._info(), lockstep)
        assert len(payload["lockstepUsageWarnings"]) == 1
        clean = {
            "1": self._entry(25.0, "2026-07-10T12:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
            "2": self._entry(30.0, "2026-07-10T13:00:00Z", 10.0, "2026-07-15T00:00:00Z"),
        }
        payload = ListReporter(switcher).build_list_payload(self._info(), clean)
        assert "lockstepUsageWarnings" not in payload


class TestStashAndRetentionStore:
    """CredentialStore: unclaimed stash + previous-generation retention."""

    def _switcher(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher

    def test_safety_copy_write_and_list(self, temp_home):
        """The store is write-only in production; bytes land as base64."""
        switcher = self._switcher(temp_home)
        store = switcher._store
        entry_id = store._write_unclaimed_credential(
            "secret-bytes", {"reason": "alien"},
        )
        assert _read_safety_copy(switcher, entry_id) == "secret-bytes"
        entries = store._list_unclaimed_credentials()
        assert entries[entry_id]["reason"] == "alien"
        assert entries[entry_id]["createdAt"]

    @pytest.mark.skipif(sys.platform == "win32", reason="File permissions work differently on Windows")
    def test_safety_copy_file_is_owner_only(self, temp_home):
        switcher = self._switcher(temp_home)
        store = switcher._store
        entry_id = store._write_unclaimed_credential("secret-bytes", {})
        mode = store._stash_entry_path(entry_id).stat().st_mode & 0o777
        assert mode == 0o600

    def test_two_snapshots_same_refresh_token_never_collide(self, temp_home):
        switcher = self._switcher(temp_home)
        store = switcher._store
        a = json.dumps({"claudeAiOauth": {"accessToken": "sk-a", "refreshToken": "rt"}})
        b = json.dumps({"claudeAiOauth": {"accessToken": "sk-b", "refreshToken": "rt"}})
        id_a = store._write_unclaimed_credential(a, {})
        id_b = store._write_unclaimed_credential(b, {})
        assert id_a != id_b
        assert _read_safety_copy(switcher, id_a) == a
        assert _read_safety_copy(switcher, id_b) == b

    def test_orphaned_entry_file_still_listed(self, temp_home):
        """Bytes without manifest metadata must stay visible, not vanish."""
        switcher = self._switcher(temp_home)
        store = switcher._store
        entry_id = store._write_unclaimed_credential("bytes", {})
        store._stash_manifest_path().unlink()
        entries = store._list_unclaimed_credentials()
        assert entry_id in entries

    def test_prev_generation_retained_on_overwrite(self, temp_home):
        switcher = self._switcher(temp_home)
        store = switcher._store
        store._write_account_credentials("1", "a@b.c", "gen-1")
        store._write_account_credentials("1", "a@b.c", "gen-2")
        assert store._read_account_credentials("1", "a@b.c") == "gen-2"
        assert store._read_previous_backup("1", "a@b.c") == "gen-1"
        # Same-value rewrite doesn't clobber the retained generation.
        store._write_account_credentials("1", "a@b.c", "gen-2")
        assert store._read_previous_backup("1", "a@b.c") == "gen-1"

    def test_prev_removed_with_account(self, temp_home):
        switcher = self._switcher(temp_home)
        store = switcher._store
        store._write_account_credentials("1", "a@b.c", "gen-1")
        store._write_account_credentials("1", "a@b.c", "gen-2")
        store._delete_account_credentials("1", "a@b.c")
        assert store._read_previous_backup("1", "a@b.c") == ""


class TestActiveRefreshProvenance:
    """_fetch_active_usage must not rotate-and-persist an unattributed
    credential — same hazard class as the switch-time blind backup."""

    _LIVE = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-live", "refreshToken": "rt-live", "expiresAt": 1000,
    }})

    def _switcher(self, sample_sequence_data):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        return switcher

    def test_unattributed_live_credential_is_never_refreshed(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        switcher = self._switcher(sample_sequence_data)
        backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-stored", "refreshToken": "rt-stored",
        }})
        seen = {}

        def mock_fetch(account_num, email, credentials, is_active,
                       persist_credentials=None):
            seen["is_active"] = is_active
            seen["persist"] = persist_credentials
            return oauth.UsageOutcome(None)

        reporter = ListReporter(switcher)
        with patch.object(switcher, "_read_credentials", return_value=self._LIVE), \
             patch.object(switcher, "_read_account_credentials", return_value=backup), \
             patch.object(reporter, "_active_cc_running", return_value=False), \
             patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=mock_fetch):
            result = reporter.fetch_active_usage("1", "test@example.com", self._LIVE)

        # Expired + unattributed → sentinel before any request; nothing written.
        assert result.sentinel is not None
        assert seen == {}  # not even a read-only fetch for an expired token
        write_live.assert_not_called()
        write_backup.assert_not_called()

    def test_same_lineage_live_credential_still_refreshes(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Access-token-only drift from the backup is same-lineage → the
        normal no-owner refresh path stays available."""
        switcher = self._switcher(sample_sequence_data)
        backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-older", "refreshToken": "rt-live",
        }})
        refreshed = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-new", "refreshToken": "rt-new",
            "expiresAt": 9_999_999_999_000,
        }})

        def mock_fetch(account_num, email, credentials, is_active,
                       persist_credentials=None):
            assert is_active is False
            persist_credentials(account_num, email, refreshed)
            return oauth.UsageOutcome({"five_hour": {"pct": 10}})

        reporter = ListReporter(switcher)
        with patch.object(switcher, "_read_credentials", return_value=self._LIVE), \
             patch.object(switcher, "_read_account_credentials", return_value=backup), \
             patch.object(reporter, "_active_cc_running", return_value=False), \
             patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=mock_fetch):
            result = reporter.fetch_active_usage("1", "test@example.com", self._LIVE)

        assert result.usage == {"five_hour": {"pct": 10}}
        write_live.assert_called_once_with(refreshed)
        write_backup.assert_called_once_with("1", "test@example.com", refreshed)


class TestDirectActivationPreservation:
    """Direct activation replaces the live credential without a backup step —
    invariant II requires the displaced credential to be stashed first."""

    def _setup(self, temp_home, live_identity_email="untracked@example.com"):
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": live_identity_email,
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        }))
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "one@example.com",
                    "uuid": "uuid-one",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                },
            },
        })
        target_creds = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-one", "refreshToken": "rt-one",
        }})
        switcher._write_account_credentials("1", "one@example.com", target_creds)
        switcher._write_account_config("1", "one@example.com", json.dumps({
            "oauthAccount": {"emailAddress": "one@example.com", "accountUuid": "uuid-one"},
        }))
        # Unmanaged live credential in the (temp-home) live store.
        unmanaged = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-unmanaged", "refreshToken": "rt-unmanaged",
        }})
        (temp_home / ".claude" / ".credentials.json").write_text(unmanaged)
        return switcher, unmanaged

    def test_unmanaged_live_login_stashed_before_activation(self, temp_home):
        switcher, unmanaged = self._setup(temp_home)
        with patch.object(switcher, "list_accounts"):
            switcher._perform_switch("1", emit_output=False)
        entries = switcher.list_unclaimed_credentials()
        assert len(entries) == 1
        (entry_id,) = entries
        assert _read_safety_copy(switcher, entry_id) == unmanaged
        assert entries[entry_id]["reason"] == "displaced-live-login"

    def test_stash_failure_aborts_direct_activation(self, temp_home):
        switcher, unmanaged = self._setup(temp_home)
        with patch.object(
            switcher._store, "_write_unclaimed_credential",
            side_effect=OSError("disk full"),
        ):
            with pytest.raises(SwitchError, match="preserve the live credential"):
                switcher._perform_switch("1", emit_output=False)
        # Live store untouched.
        live = (temp_home / ".claude" / ".credentials.json").read_text()
        assert live == unmanaged

    def test_force_proceeds_with_warning_when_stash_fails(self, temp_home):
        switcher, unmanaged = self._setup(temp_home)
        with patch.object(
            switcher._store, "_write_unclaimed_credential",
            side_effect=OSError("disk full"),
        ), patch.object(switcher, "list_accounts"):
            op = switcher._perform_switch(
                "1", emit_output=False, force_activate=True
            )
        assert any("--force" in w for w in op["warnings"])
        live = (temp_home / ".claude" / ".credentials.json").read_text()
        assert json.loads(live)["claudeAiOauth"]["accessToken"] == "sk-one"


class TestUuidConflictClassification:
    """An email+org match with a conflicting uuid is a different account
    wearing a recycled email — never the slot."""

    _setup_two_accounts = TestPerformSwitchPostDisplay._setup_two_accounts
    _install_store_patches = staticmethod(
        TestPerformSwitchPostDisplay._install_store_patches
    )

    def test_email_match_with_conflicting_uuid_is_not_the_slot(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        a1_backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1", "refreshToken": "rt-1",
        }})
        creds_store[("1", "test@example.com")] = a1_backup
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-x",
        }})
        live_state = {"creds": rotated}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch.object(switcher, "list_accounts"), \
                 _no_target_refresh(switcher), patch(
                # Same email/org as slot 1 — but a different, non-empty uuid
                # (slot 1 stores uuid-1). Must NOT classify as own-rotated.
                "claude_swap.oauth.fetch_oauth_profile",
                return_value={
                    "uuid": "uuid-recycled-email",
                    "email": "test@example.com",
                    "organizationUuid": "",
                },
            ):
                op = switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()
        # Slot 1 untouched; the conflicted credential was preserved as alien
        # (a positively-different account, so this is NOT the fail-open
        # path — a recycled email must never be backed into the slot).
        assert creds_store[("1", "test@example.com")] == a1_backup
        assert len(switcher.list_unclaimed_credentials()) == 1
        assert any(
            "does not match a managed account" in w for w in op["warnings"]
        )


class TestStashStorageHardening:
    """Round-2 review: append-only ids and manifest corruption handling."""

    def _store(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher._store

    def test_identical_bytes_same_second_get_distinct_ids(self, temp_home):
        store = self._store(temp_home)
        id_a = store._write_unclaimed_credential("same-bytes", {})
        id_b = store._write_unclaimed_credential("same-bytes", {})
        assert id_a != id_b
        for entry_id in (id_a, id_b):
            raw = store._stash_entry_path(entry_id).read_text().strip()
            assert base64.b64decode(raw, validate=True).decode() == "same-bytes"

    def test_corrupt_manifest_is_preserved_not_clobbered(self, temp_home):
        store = self._store(temp_home)
        entry_id = store._write_unclaimed_credential("bytes-1", {"reason": "x"})
        # Corrupt the manifest out-of-band.
        store._stash_manifest_path().write_text("{ not json !!!")
        new_id = store._write_unclaimed_credential("bytes-2", {"reason": "y"})
        # The corrupt file was set aside, not destroyed…
        corrupt = list(store._host.credentials_dir.glob(
            ".unclaimed-manifest.json.corrupt-*"
        ))
        assert len(corrupt) == 1
        assert "not json" in corrupt[0].read_text()
        # …the new manifest is valid, and the older entry's *bytes* are still
        # listed (as an orphan) even though its metadata row was lost.
        entries = store._list_unclaimed_credentials()
        assert new_id in entries and entries[new_id]["reason"] == "y"
        assert entry_id in entries
        raw = store._stash_entry_path(entry_id).read_text().strip()
        assert base64.b64decode(raw, validate=True).decode() == "bytes-1"
