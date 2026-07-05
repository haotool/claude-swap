"""Structural Protocol satisfaction for the credential refresh host."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from claude_swap.credential_refresh import CredentialRefresher


def test_credential_refresher_accepts_minimal_refresh_host(tmp_path: Path):
    lock_file = tmp_path / ".lock"
    store: dict[tuple[str, str], str] = {}
    live = {
        "creds": '{"claudeAiOauth":{"accessToken":"t","refreshToken":"r","expiresAt":9}}'
    }
    host = SimpleNamespace(
        lock_file=lock_file,
        _logger=logging.getLogger("test.refresh_host"),
        _read_credentials=lambda: live["creds"],
        _read_account_credentials=lambda num, email: store.get((num, email), ""),
        _write_account_credentials=lambda num, email, creds: store.__setitem__(
            (num, email),
            creds,
        ),
        _live_session_pids=lambda num, email: [],
    )
    refresher = CredentialRefresher(host)
    result = refresher.write_verified_live("1", "a@example.com", live["creds"])
    assert result == live["creds"]
