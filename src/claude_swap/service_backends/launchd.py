"""macOS launchd backend for the auto-switch monitor."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any

from claude_swap import service_spec
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.printer import bolded, dimmed, muted
from claude_swap.protocols import ServiceHost, ServiceState

# Resolve launchctl at import time rather than trusting PATH. macOS 26 moved
# the binary from /usr/bin to /bin — probe both known Apple system locations.
_LAUNCHCTL = (
    "/bin/launchctl"
    if os.path.exists("/bin/launchctl")
    else "/usr/bin/launchctl"
)


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{service_spec.SERVICE_LABEL}.plist"


def _build_plist(switcher: ServiceHost) -> dict[str, Any]:
    log_dir = service_spec.log_dir(switcher)
    return {
        "Label": service_spec.SERVICE_LABEL,
        "ProgramArguments": service_spec.program_arguments(),
        "RunAtLoad": True,
        # Dict form is mandatory: ``KeepAlive=True`` would resurrect the agent
        # immediately after ``launchctl bootout``, defeating uninstall.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(log_dir / "monitor.out"),
        "StandardErrorPath": str(log_dir / "monitor.err"),
        "EnvironmentVariables": service_spec.passthrough_env(),
    }


def _uid() -> int:
    return os.getuid()


def _launchd_domain() -> str:
    return f"gui/{_uid()}"


def _launchd_service_target() -> str:
    return f"{_launchd_domain()}/{service_spec.SERVICE_LABEL}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Invoke launchctl; surface failures as ``ClaudeSwitchError``."""
    from claude_swap import service

    try:
        proc: subprocess.CompletedProcess[str] = service.subprocess.run(
            [service._LAUNCHCTL, *args],
            capture_output=True,
            text=True,
            timeout=service_spec.SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ClaudeSwitchError(
            f"launchctl {' '.join(args)} timed out after "
            f"{service_spec.SUBPROCESS_TIMEOUT}s"
        )
    if check and proc.returncode != 0:
        raise ClaudeSwitchError(
            f"launchctl {' '.join(args)} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc


def _installed_version() -> str | None:
    """Return the cswap version recorded in the installed plist, or ``None``."""
    from claude_swap import service

    try:
        with service._plist_path().open("rb") as fh:
            data = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException):
        return None
    if not isinstance(data, dict):
        return None
    env_vars = data.get("EnvironmentVariables")
    return service_spec.installed_version_from_env(env_vars)


class LaunchdBackend:
    """macOS LaunchAgent supervisor implementing ``ServiceBackend``."""

    @property
    def platform_label(self) -> str:
        return "launchd"

    def describe(self) -> str:
        return "macOS LaunchAgent (launchd)"

    def install(self, switcher: ServiceHost) -> int:
        from claude_swap import service

        plist_path = service._plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        log_dir = service_spec.log_dir(switcher)
        log_dir.mkdir(parents=True, exist_ok=True)
        # launchd stdout/stderr land here; keep the backup root and logs owner-only
        # even on a first install that runs before _setup_directories().
        os.chmod(switcher.backup_dir, 0o700)
        os.chmod(log_dir, 0o700)
        tmp_path = plist_path.with_suffix(plist_path.suffix + ".tmp")
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                plistlib.dump(_build_plist(switcher), fh)
            os.replace(tmp_path, plist_path)
            os.chmod(plist_path, 0o600)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        _launchctl("bootout", _launchd_service_target(), check=False)
        _launchctl("bootstrap", _launchd_domain(), str(plist_path))
        log_path = service_spec.log_dir(switcher)
        service_spec.print_install_success(
            switcher,
            artifact_path=plist_path,
            run_hint=f"runs `cswap --monitor` at login; launchd output → {log_path}",
        )
        return 0

    def uninstall(self, switcher: ServiceHost) -> int:
        from claude_swap import service

        _launchctl("bootout", _launchd_service_target(), check=False)
        plist_path = service._plist_path()
        existed = plist_path.exists()
        plist_path.unlink(missing_ok=True)
        service_spec.print_uninstall_result(
            switcher,
            existed=existed,
            retained_hint=f"launchd output retained → {service_spec.log_dir(switcher)}",
        )
        return 0

    def state(self) -> ServiceState:
        from claude_swap import service

        if not service._plist_path().exists():
            return "not installed"
        proc = _launchctl("print", _launchd_service_target(), check=False)
        if proc.returncode != 0:
            return "installed but not loaded"
        return "loaded"

    def status(self, switcher: ServiceHost) -> int:
        state = self.state()
        if state == "not installed":
            service_spec.print_status_not_installed()
            return 0

        installed_ver = _installed_version()
        service_spec.warn_version_drift(installed_ver)

        if state == "installed but not loaded":
            service_spec.print_status_installed_but_not_loaded()
            return 0

        proc = _launchctl("print", _launchd_service_target(), check=False)
        service_spec.print_status_loaded(supervisor_stdout=proc.stdout)
        service_spec.print_status_decision_log(switcher)
        return 0

    def logs(self, switcher: ServiceHost, lines: int = 40) -> int:
        structured = switcher.backup_dir / "claude-swap.log"
        for path, label in [
            (structured, "claude-swap.log (structured)"),
            (service_spec.log_dir(switcher) / "monitor.err", "monitor.err (launchd stderr)"),
            (service_spec.log_dir(switcher) / "monitor.out", "monitor.out (launchd stdout)"),
        ]:
            print(bolded(f"== {label} =="))
            print(f"  {dimmed(str(path))}")
            if not path.exists():
                print(f"  {dimmed('(none yet)')}")
                continue
            tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
            for line in tail:
                print(f"  {muted(line)}")
        return 0


