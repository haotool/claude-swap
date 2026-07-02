"""Unit tests for the typed ``sequence.json`` model and store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from claude_swap.sequence_store import (
    DEFAULT_AUTO_SWITCH_THRESHOLD,
    AccountRecord,
    AutoSwitchConfig,
    SequenceData,
    SequenceStore,
)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _store(tmp_path: Path) -> SequenceStore:
    return SequenceStore(
        tmp_path / "sequence.json",
        read_json=_read_json,
        write_json=_write_json,
    )


# --- AutoSwitchConfig ------------------------------------------------------


def test_auto_switch_defaults_when_absent() -> None:
    cfg = AutoSwitchConfig.from_raw(None)
    assert cfg.enabled is False
    assert cfg.threshold == DEFAULT_AUTO_SWITCH_THRESHOLD


def test_auto_switch_tolerates_bad_threshold() -> None:
    cfg = AutoSwitchConfig.from_raw({"enabled": True, "threshold": "oops"})
    assert cfg.enabled is True
    assert cfg.threshold == DEFAULT_AUTO_SWITCH_THRESHOLD


# --- AccountRecord ---------------------------------------------------------


def test_account_record_create_oauth_key_order() -> None:
    rec = AccountRecord.create(
        email="a@b.co",
        uuid="u1",
        organization_uuid="org",
        organization_name="Org",
        added="2026-01-01",
    )
    assert list(rec.to_dict().keys()) == [
        "email",
        "uuid",
        "organizationUuid",
        "organizationName",
        "added",
    ]
    assert rec.kind == "oauth"
    assert "kind" not in rec.to_dict()


def test_account_record_create_api_key_appends_kind() -> None:
    rec = AccountRecord.create(email="a@b.co", added="t", is_api_key=True)
    assert rec.to_dict()["kind"] == "api_key"
    assert rec.kind == "api_key"


def test_account_record_kindless_reads_as_oauth() -> None:
    assert AccountRecord({"email": "a@b.co"}).kind == "oauth"


def test_account_record_preserves_unknown_keys() -> None:
    raw = {"email": "a@b.co", "futureField": 42}
    assert AccountRecord(raw).to_dict()["futureField"] == 42


def test_account_record_to_dict_is_independent_copy() -> None:
    rec = AccountRecord.create(email="a@b.co", added="t")
    dumped = rec.to_dict()
    dumped["email"] = "mutated"
    assert rec.email == "a@b.co"


# --- SequenceData ----------------------------------------------------------


def test_empty_shape_matches_init_file() -> None:
    data = SequenceData.empty()
    d = data.to_dict()
    assert list(d.keys()) == [
        "activeAccountNumber",
        "lastUpdated",
        "sequence",
        "accounts",
    ]
    assert d["activeAccountNumber"] is None
    assert d["sequence"] == []
    assert d["accounts"] == {}


def test_roundtrips_unknown_top_level_keys() -> None:
    raw = {
        "activeAccountNumber": 1,
        "lastUpdated": "t",
        "sequence": [1],
        "accounts": {"1": {"email": "a@b.co"}},
        "someFutureKey": {"nested": True},
    }
    assert SequenceData(raw).to_dict() == raw


def test_register_slot_sorts_and_sets_active() -> None:
    data = SequenceData.empty()
    data = data.register_slot(
        "2", AccountRecord.create(email="two@x", added="t"), set_active=False
    )
    data = data.register_slot(
        "1", AccountRecord.create(email="one@x", added="t"), set_active=True
    )
    assert data.sequence == (1, 2)
    assert data.active_account_number == 1
    assert data.get("2") is not None
    assert data.accounts["1"].email == "one@x"


def test_register_slot_no_duplicate_sequence_entry() -> None:
    data = SequenceData.empty().register_slot(
        "1", AccountRecord.create(email="a@x", added="t"), set_active=False
    )
    data = data.register_slot(
        "1", AccountRecord.create(email="a2@x", added="t"), set_active=False
    )
    assert data.sequence == (1,)
    assert data.accounts["1"].email == "a2@x"


def test_remove_slot() -> None:
    data = SequenceData.empty()
    data = data.register_slot(
        "1", AccountRecord.create(email="a@x", added="t"), set_active=True
    )
    data = data.register_slot(
        "2", AccountRecord.create(email="b@x", added="t"), set_active=False
    )
    data = data.remove_slot("1")
    assert data.sequence == (2,)
    assert data.get("1") is None


def test_remove_slot_drops_all_duplicate_sequence_entries() -> None:
    # A corrupt sequence with duplicate ids must be fully cleaned on remove,
    # matching the pre-extraction list-filter (not list.remove which drops one).
    data = SequenceData(
        {
            "activeAccountNumber": 2,
            "lastUpdated": "",
            "sequence": [1, 1, 2, 1],
            "accounts": {
                "1": AccountRecord.create(email="a@x", added="t").to_dict(),
                "2": AccountRecord.create(email="b@x", added="t").to_dict(),
            },
        }
    )
    data = data.remove_slot("1")
    assert data.sequence == (2,)
    assert data.get("1") is None


def test_register_slot_preserves_sibling_unknown_keys() -> None:
    # Mutating one slot must round-trip unknown/future keys on untouched slots
    # (the main switcher mutation path replaces records loaded from disk).
    data = SequenceData(
        {
            "activeAccountNumber": 1,
            "lastUpdated": "",
            "sequence": [1],
            "accounts": {
                "1": {"email": "a@x", "added": "t", "futureField": "keep-me"},
            },
        }
    )
    data = data.register_slot(
        "2", AccountRecord.create(email="b@x", added="t"), set_active=False
    )
    assert data.to_dict()["accounts"]["1"]["futureField"] == "keep-me"


def test_mutations_are_immutable() -> None:
    original = SequenceData.empty()
    original.register_slot(
        "1", AccountRecord.create(email="a@x", added="t"), set_active=True
    )
    # original is untouched by the discarded result above
    assert original.sequence == ()
    assert original.active_account_number is None


def test_with_auto_switch_merges_fields() -> None:
    data = SequenceData.empty().with_auto_switch(enabled=True)
    assert data.auto_switch.enabled is True
    assert data.auto_switch.threshold == DEFAULT_AUTO_SWITCH_THRESHOLD
    data = data.with_auto_switch(threshold=50)
    # enabled preserved from the prior merge
    assert data.auto_switch.enabled is True
    assert data.auto_switch.threshold == 50


def test_set_active_none() -> None:
    data = SequenceData.empty().register_slot(
        "1", AccountRecord.create(email="a@x", added="t"), set_active=True
    )
    assert data.set_active(None).active_account_number is None


# --- SequenceStore ---------------------------------------------------------


def test_store_load_missing_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.load() is None
    assert store.load_raw() is None
    assert store.load_or_empty().sequence == ()


def test_store_init_if_missing_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.init_if_missing()
    assert (tmp_path / "sequence.json").exists()
    first = store.load_raw()
    store.init_if_missing()  # must not overwrite
    assert store.load_raw() == first


def test_store_save_stamps_last_updated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = SequenceData({"activeAccountNumber": None, "sequence": [], "accounts": {}})
    store.save(data)
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.last_updated != ""


def test_store_corrupt_file_loads_as_none(tmp_path: Path) -> None:
    path = tmp_path / "sequence.json"
    path.write_text("{ not json", encoding="utf-8")
    store = _store(tmp_path)
    assert store.load() is None
    assert store.load_or_empty().sequence == ()


def test_store_roundtrip_preserves_records(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = SequenceData.empty().register_slot(
        "1",
        AccountRecord.create(
            email="a@b.co",
            uuid="u",
            organization_uuid="o",
            organization_name="O",
            added="t",
            is_api_key=True,
        ),
        set_active=True,
    )
    store.save(data)
    loaded = store.load()
    assert loaded is not None
    rec = loaded.accounts["1"]
    assert rec.email == "a@b.co"
    assert rec.kind == "api_key"
    assert loaded.active_account_number == 1


def test_store_does_not_lock(tmp_path: Path) -> None:
    """save/load use injected helpers only — no FileLock acquisition."""
    calls: list[str] = []

    def read(path: Path) -> dict[str, Any] | None:
        calls.append("read")
        return _read_json(path)

    def write(path: Path, data: dict[str, Any]) -> None:
        calls.append("write")
        _write_json(path, data)

    store = SequenceStore(tmp_path / "sequence.json", read_json=read, write_json=write)
    store.save(SequenceData.empty())
    store.load()
    assert calls == ["write", "read"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
