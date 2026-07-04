"""Tool settings persisted at ``<backup_root>/settings.json``.

One versioned JSON file for user-tunable claude-swap preferences, written
atomically with the backup dir's 0600/0700 modes. v1 carries only the
``autoswitch`` section; other sections can be added additively. Unknown keys
(future fields, other tools' experiments) survive a round trip.

Reading is forgiving — a missing or corrupt file yields defaults with a logged
warning, never a crash — so a bad hand edit degrades to default behavior.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SETTINGS_SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"

_logger = logging.getLogger("claude-swap")


@dataclass(frozen=True)
class AutoSwitchSettings:
    """Policy knobs for the auto-switch engine (``cswap auto``).

    ``threshold`` is binding-window utilization (max of the 5h/7d percentages):
    at or above it the engine looks for a better account. 90 rather than 95
    leaves margin for the macOS ~30s Keychain pickup tail and for heavy
    subagent turns burning past the mark before a swap lands. A candidate only
    qualifies while its own utilization sits at least ``hysteresis_pct`` below
    the threshold, so two accounts hovering at the line never ping-pong.
    """

    threshold: float = 90.0
    interval_seconds: float = 60.0
    cooldown_seconds: float = 300.0
    hysteresis_pct: float = 10.0
    strategy: str = "best"  # reserved for future strategies; only "best" in v1
    include_api_key_accounts: bool = False
    unhealthy_ticks: int = 3


# settings.json uses camelCase (matching the repo's other JSON artifacts);
# dataclass fields stay snake_case.
_AUTOSWITCH_KEYS: dict[str, str] = {
    "threshold": "threshold",
    "interval_seconds": "intervalSeconds",
    "cooldown_seconds": "cooldownSeconds",
    "hysteresis_pct": "hysteresisPct",
    "strategy": "strategy",
    "include_api_key_accounts": "includeApiKeyAccounts",
    "unhealthy_ticks": "unhealthyTicks",
}


def settings_path(backup_root: Path) -> Path:
    return backup_root / SETTINGS_FILENAME


def _clamped(settings: AutoSwitchSettings) -> AutoSwitchSettings:
    """Clamp values into sane ranges; fall back to the default on bad types."""
    defaults = AutoSwitchSettings()

    def num(value: object, default: float, lo: float, hi: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(min(max(value, lo), hi))

    strategy = settings.strategy if settings.strategy == "best" else defaults.strategy
    if strategy != settings.strategy:
        _logger.warning(
            "settings.json: unsupported autoswitch strategy %r; using 'best'",
            settings.strategy,
        )
    return AutoSwitchSettings(
        threshold=num(settings.threshold, defaults.threshold, 50.0, 99.9),
        interval_seconds=num(
            settings.interval_seconds, defaults.interval_seconds, 15.0, 3600.0
        ),
        cooldown_seconds=num(
            settings.cooldown_seconds, defaults.cooldown_seconds, 0.0, 86400.0
        ),
        hysteresis_pct=num(settings.hysteresis_pct, defaults.hysteresis_pct, 0.0, 50.0),
        strategy=strategy,
        include_api_key_accounts=bool(settings.include_api_key_accounts),
        unhealthy_ticks=int(
            num(settings.unhealthy_ticks, defaults.unhealthy_ticks, 1, 100)
        ),
    )


def _read_raw(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _logger.warning("Could not read %s (%s); using defaults", path, e)
        return {}
    if not isinstance(raw, dict):
        _logger.warning("%s is not a JSON object; using defaults", path)
        return {}
    return raw


def load_settings(backup_root: Path) -> AutoSwitchSettings:
    """Load the autoswitch section; missing/corrupt file or fields → defaults."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        return AutoSwitchSettings()
    kwargs = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        if json_key in section:
            kwargs[field] = section[json_key]
    try:
        settings = AutoSwitchSettings(**kwargs)
    except TypeError:
        settings = AutoSwitchSettings()
    return _clamped(settings)


def save_settings(backup_root: Path, settings: AutoSwitchSettings) -> None:
    """Write the autoswitch section, preserving unknown keys and sections."""
    path = settings_path(backup_root)
    raw = _read_raw(path)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        section = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        section[json_key] = getattr(settings, field)
    raw["autoswitch"] = section
    atomic_write_json(path, raw)


def merged_with_cli(
    settings: AutoSwitchSettings, args: argparse.Namespace
) -> AutoSwitchSettings:
    """Overlay non-None CLI overrides (argparse Namespace) onto settings."""
    overrides = {}
    for attr, field in (
        ("threshold", "threshold"),
        ("interval", "interval_seconds"),
        ("cooldown", "cooldown_seconds"),
        ("include_api_key_accounts", "include_api_key_accounts"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            overrides[field] = value
    if not overrides:
        return settings
    return _clamped(dataclasses.replace(settings, **overrides))


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON with the backup dir's 0600/0700 modes.

    Shared by settings.json and the autoswitch state file (and any future
    machine-local state files beside them).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(path.parent, 0o700)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(path))
        if sys.platform != "win32":
            os.chmod(str(path), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
