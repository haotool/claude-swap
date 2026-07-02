"""Structural host Protocols for narrow switcher dependencies.

Consumers depend on these views instead of the full ``ClaudeAccountSwitcher`` so
tests can inject minimal fakes and production code stays decoupled from switcher
internals it does not call. Underscore-prefixed members are part of the host
contract on purpose: the leading underscore marks "not a stable public CLI
surface", not "private to one module" — these views are the seam.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, Protocol

from claude_swap.models import (
    AutoSwitchDecisionContext,
    SwitchIntent,
    SwitchPreconditions,
)
from claude_swap.credentials import ActiveCredentials
from claude_swap.oauth import UsageFetchError

ServiceState = Literal["not installed", "installed but not loaded", "loaded"]


class MonitorHost(Protocol):
    """Switch surface the auto-switch monitor reads and plans against."""

    backup_dir: Path
    _logger: logging.Logger

    def get_auto_switch_config(self) -> dict[str, Any]: ...
    def ensure_auto_switch_enabled(self) -> dict[str, Any]: ...
    def get_active_usage_pct(self) -> float | None: ...
    def get_active_usage_breakdown(self) -> dict[str, float] | None: ...
    def get_active_usage_retry_after(self) -> int | None: ...
    def active_account_is_api_key(self) -> bool: ...
    def build_auto_switch_decision(
        self,
        threshold: int,
        active_usage_pct: float | None,
    ) -> AutoSwitchDecisionContext: ...
    def switch(
        self,
        intent: SwitchIntent | None = None,
        *,
        strategy: str | None = None,
        json_output: bool = False,
    ) -> dict[str, Any] | bool | None: ...

    def _live_default_mode_claude_pids(self) -> list[int]: ...
    def _get_sequence_data(self) -> dict[str, Any] | None: ...
    def _account_is_switchable(self, account_num: str) -> bool: ...
    def _trusted_usage_snapshots(self) -> dict[str, dict[str, Any]]: ...
    def _refresh_switchable_usage_cache(self) -> None: ...


class RefreshHost(Protocol):
    """Switch surface ``CredentialRefresher`` uses for OAuth read/write paths."""

    lock_file: Path
    _logger: logging.Logger

    def _read_credentials(self) -> str | None: ...
    def _read_account_credentials(self, account_num: str, email: str) -> str: ...
    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str,
    ) -> None: ...
    def _live_session_pids(self, account_num: str, email: str) -> list[int]: ...


class ServiceHost(Protocol):
    """Minimal switcher view the service layer needs: just the backup root.

    The supervisor backends only resolve log/artifact paths under ``backup_dir``,
    so they depend on this one-field view instead of the whole switcher.
    """

    backup_dir: Path


class ServiceBackend(Protocol):
    """Platform-native supervisor for the auto-switch monitor.

    Methods take a ``ServiceHost`` (not the concrete switcher); the underscore is
    not relevant here — this is the public supervisor contract.
    """

    @property
    def platform_label(self) -> str: ...

    def describe(self) -> str: ...

    def install(self, switcher: ServiceHost) -> int: ...

    def uninstall(self, switcher: ServiceHost) -> int: ...

    def state(self) -> ServiceState: ...

    def status(self, switcher: ServiceHost) -> int: ...

    def logs(self, switcher: ServiceHost, lines: int = 40) -> int: ...


class ListHost(Protocol):
    """Switch surface ``list_reporter`` uses for read-only list/status rendering."""

    sequence_file: Path
    lock_file: Path
    _logger: logging.Logger

    @property
    def usage_cache_path(self) -> Path: ...

    def _get_sequence_data_migrated(self) -> dict[str, Any] | None: ...
    def _get_current_account(self) -> tuple[str, str] | None: ...
    def _find_account_slot(
        self, data: dict[str, Any], email: str, organization_uuid: str,
    ) -> str | None: ...
    def _read_credentials(self) -> str | None: ...
    def _read_active_credentials(self) -> ActiveCredentials: ...
    def _read_account_credentials(self, account_num: str, email: str) -> str: ...
    def _write_credentials(self, credentials: str) -> None: ...
    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str,
    ) -> None: ...
    def _sync_live_account_credentials_to_backup(
        self, account_num: str, email: str, credentials: str,
    ) -> None: ...
    def _refresh_inactive_credentials_if_needed(
        self, account_num: str, email: str, credentials: str,
    ) -> tuple[str, str | None]: ...
    def _live_session_pids(self, account_num: str, email: str) -> list[int]: ...
    def _get_display_tag(
        self, email: str, org_name: str, org_uuid: str,
    ) -> str: ...
    def _first_run_setup(self) -> None: ...
    def _usage_cache_fresh(
        self, cached: dict[str, Any], account_keys: set[str],
    ) -> bool: ...
    def _merge_usage_cache(self, updates: dict[str, object]) -> None: ...


class SwitchCliHost(Protocol):
    """Switch surface ``switch_cli`` uses for strategy-aware CLI dispatch."""

    def _classify_switch_preconditions(self) -> SwitchPreconditions: ...
    def _find_account_slot(
        self, data: dict[str, Any], email: str, organization_uuid: str,
    ) -> str | None: ...
    def _resolve_fresh_machine_target(
        self,
        *,
        quiet: bool = False,
        warnings: list[str] | None = None,
    ) -> str: ...
    def _perform_switch(
        self,
        target_account: str,
        *,
        intent: SwitchIntent | None = None,
        emit_output: bool = True,
    ) -> dict[str, Any] | None: ...
    def _switch_result_from_op(
        self, op: dict[str, Any], strategy: str, extra_warnings: list[str] | None = None,
    ) -> dict[str, Any]: ...
    def _switch_noop(
        self,
        *,
        strategy: str,
        reason: str,
        message: str,
        from_ref: dict[str, Any] | None = None,
        to_ref: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]: ...
    def add_account(self, slot: int | None = None) -> None: ...
    def _get_sequence_data(self) -> dict[str, Any] | None: ...
    def _select_best_switchable(
        self, current_num: str | None,
    ) -> tuple[str | None, str]: ...
    def _usage_by_account(
        self,
    ) -> dict[str, dict[str, Any] | str | UsageFetchError | None]: ...
    def _account_is_switchable(self, account_num: str) -> bool: ...
