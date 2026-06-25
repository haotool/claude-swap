"""Tests for managed API-key (sk-ant-api) accounts (plan 021).

Exercised on the Linux/file backend (no Keychain): the managed key lands in
``~/.claude.json`` ``primaryApiKey`` and ``customApiKeyResponses.approved``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_swap.credentials import (
    CredentialStore,
    approved_form,
    looks_like_api_key,
)
from claude_swap.exceptions import SessionError, ValidationError
from claude_swap.models import Platform
from claude_swap.monitor import MonitorRuntimeState, monitor_step
from claude_swap.session import SessionManager
from claude_swap.switcher import ClaudeAccountSwitcher

API_KEY = "sk-ant-api03-abcdefghij1234567890XYZ"
OAUTH_CREDS = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-tok"}})


def _linux_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


# -- detection helpers --------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("sk-ant-api03-xyz", True),
        ("sk-ant-api-anything", True),
        ("sk-ant-oat01-xyz", False),
        ('{"claudeAiOauth": {}}', False),
        ("", False),
        (None, False),
    ],
)
def test_looks_like_api_key(value, expected):
    assert looks_like_api_key(value) is expected


def test_approved_form_is_last_20_chars():
    assert approved_form(API_KEY) == API_KEY[-20:]
    assert len(approved_form(API_KEY)) == 20


# -- CredentialStore storage + mutual exclusion (Linux/file) ------------------


def _store(temp_home: Path) -> CredentialStore:
    host = SimpleNamespace(
        platform=Platform.LINUX,
        credentials_dir=temp_home / ".claude-backup" / "credentials",
        _logger=__import__("logging").getLogger("test.apikey"),
    )
    host.credentials_dir.mkdir(parents=True, exist_ok=True)
    return CredentialStore(host)


def test_write_managed_key_persists_to_global_config(temp_home: Path):
    store = _store(temp_home)
    store._write_credentials(API_KEY)
    cfg = json.loads((temp_home / ".claude.json").read_text())
    assert cfg["primaryApiKey"] == API_KEY
    assert API_KEY[-20:] in cfg["customApiKeyResponses"]["approved"]
    # And it reads back as the raw key.
    assert store._read_credentials() == API_KEY
    assert looks_like_api_key(store._read_credentials())


def test_writing_api_key_clears_oauth(temp_home: Path):
    store = _store(temp_home)
    store._write_credentials(OAUTH_CREDS)          # OAuth active
    assert store._read_credentials() == OAUTH_CREDS
    store._write_credentials(API_KEY)              # switch to API key
    # OAuth .credentials.json removed; managed key wins.
    assert store._read_credentials() == API_KEY


def test_writing_oauth_clears_managed_key(temp_home: Path):
    store = _store(temp_home)
    store._write_credentials(API_KEY)
    store._write_credentials(OAUTH_CREDS)
    assert store._read_credentials() == OAUTH_CREDS
    cfg = json.loads((temp_home / ".claude.json").read_text())
    assert cfg.get("primaryApiKey") is None        # managed key cleared
    # approved list is intentionally left intact (removeApiKey semantics).
    assert API_KEY[-20:] in cfg["customApiKeyResponses"]["approved"]


def test_update_global_config_preserves_other_keys(temp_home: Path):
    (temp_home / ".claude.json").write_text(json.dumps({"projects": {"x": 1}}))
    store = _store(temp_home)
    store._write_credentials(API_KEY)
    cfg = json.loads((temp_home / ".claude.json").read_text())
    assert cfg["projects"] == {"x": 1}             # untouched
    assert cfg["primaryApiKey"] == API_KEY


# -- registration via add_account_from_token ----------------------------------


def test_add_token_autodetects_api_key(temp_home: Path):
    s = _linux_switcher()
    s.add_account_from_token(API_KEY)
    data = s._get_sequence_data()
    num = next(iter(data["accounts"]))
    assert data["accounts"][num]["kind"] == "api_key"
    assert data["accounts"][num]["email"] == f"api-key-{num}@token.local"
    assert s._account_kind(num) == "api_key"
    # Credential stored raw (not wrapped in OAuth JSON).
    assert s._read_account_credentials(num, data["accounts"][num]["email"]) == API_KEY


def test_add_token_oauth_has_no_kind(temp_home: Path):
    s = _linux_switcher()
    s.add_account_from_token("sk-ant-oat01-plaintoken")
    data = s._get_sequence_data()
    num = next(iter(data["accounts"]))
    assert "kind" not in data["accounts"][num]
    assert s._account_kind(num) == "oauth"


def test_cross_kind_collision_rejected(temp_home: Path):
    s = _linux_switcher()
    s.add_account_from_token(API_KEY, email="shared@example.com")
    with pytest.raises(ValidationError, match="already exists as an API-key"):
        s.add_account_from_token("sk-ant-oat01-tok", email="shared@example.com")


def test_account_kind_unknown_slot_is_oauth(temp_home: Path):
    s = _linux_switcher()
    assert s._account_kind(None) == "oauth"
    assert s._account_kind("999") == "oauth"


# -- usage / status -----------------------------------------------------------


def test_fetch_account_usage_api_key_no_quota(temp_home: Path):
    s = _linux_switcher()
    result = s._fetch_account_usage((1, "api-key-1@token.local", "", "", False, API_KEY))
    assert result == "API key (no quota)"


# -- monitor: active API-key account is idle, not usage_unavailable -----------


def test_monitor_active_api_key_is_idle(temp_home: Path):
    s = _linux_switcher()
    state = MonitorRuntimeState()
    with (
        patch.object(s, "get_auto_switch_config",
                     return_value={"enabled": True, "threshold": 95}),
        patch.object(s, "_live_default_mode_claude_pids", return_value=[123]),
        patch.object(s, "get_active_usage_pct", return_value=None),
        patch.object(s, "active_account_is_api_key", return_value=True),
    ):
        result = monitor_step(s, state)
    assert result.kind == "idle"
    assert result.pct_text == "api-key"
    assert result.consecutive_failures == 0


def test_monitor_real_unavailable_still_backs_off(temp_home: Path):
    """Non-api-key None usage must still be a failure (backoff), unchanged."""
    s = _linux_switcher()
    state = MonitorRuntimeState()
    with (
        patch.object(s, "get_auto_switch_config",
                     return_value={"enabled": True, "threshold": 95}),
        patch.object(s, "_live_default_mode_claude_pids", return_value=[123]),
        patch.object(s, "get_active_usage_pct", return_value=None),
        patch.object(s, "active_account_is_api_key", return_value=False),
    ):
        result = monitor_step(s, state)
    assert result.kind == "usage_unavailable"
    assert result.consecutive_failures == 1


# -- session mode rejects API-key accounts ------------------------------------


def test_session_setup_rejects_api_key(temp_home: Path):
    s = _linux_switcher()
    s.add_account_from_token(API_KEY)
    data = s._get_sequence_data()
    num = next(iter(data["accounts"]))
    mgr = SessionManager(s)
    with pytest.raises(SessionError, match="API-key account"):
        mgr.setup_session(num, share=True)


# -- transfer export/import round-trip -----------------------------------------


def test_transfer_round_trips_api_key(temp_home: Path):
    from claude_swap.transfer import export_accounts, import_accounts

    src = _linux_switcher()
    src.add_account_from_token(API_KEY, email="key@example.com")
    out = temp_home / "export.cswap"
    export_accounts(src, str(out))

    payload = json.loads(out.read_text())
    entry = payload["accounts"][0]
    assert entry["kind"] == "api_key"
    assert entry["credentials"] == API_KEY      # raw string, not JSON object

    dst = _linux_switcher()
    import_accounts(dst, str(out))
    data = dst._get_sequence_data()
    num = next(n for n, a in data["accounts"].items() if a["email"] == "key@example.com")
    assert data["accounts"][num]["kind"] == "api_key"
    assert dst._read_account_credentials(num, "key@example.com") == API_KEY
