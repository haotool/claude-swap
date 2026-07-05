"""Linux/WSL systemd --user backend for the auto-switch engine.

Manages a per-user unit under ``$XDG_CONFIG_HOME/systemd/user`` through
``systemctl --user``. Preflight is deliberately strict because the failure
modes are confusing at a distance: PID 1 must be systemd (WSL2 needs it
enabled in ``/etc/wsl.conf``) and the per-user manager must be reachable in
this session. Install also enables linger so the engine survives logout,
and on WSL prints the Task Scheduler keepalive guidance (see
``_WSL_KEEPALIVE_EXEC``).
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
from pathlib import Path

from claude_swap import service_spec
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.models import is_linux
from claude_swap.paths import get_backup_root, get_claude_config_home
from claude_swap.printer import bolded, dimmed, muted, warning
from claude_swap.protocols import ServiceHost, ServiceState

UNIT_NAME = "cswap-monitor.service"
_SYSTEMCTL = "systemctl"
_LOGinctl = "loginctl"
_ENV_LINE = re.compile(r"^Environment=(.+)$", re.MULTILINE)
_WINDOWS_MOUNT_RE = re.compile(r"^/mnt/[a-zA-Z]/")


def _config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg)
    return Path.home() / ".config"


def _unit_path() -> Path:
    return _config_home() / "systemd" / "user" / UNIT_NAME


def _pid1_is_systemd() -> bool:
    try:
        return Path("/proc/1/comm").read_text(encoding="utf-8").strip() == "systemd"
    except OSError:
        return False


def _user_manager_available() -> bool:
    """Best-effort check that the per-user systemd manager is reachable.

    ``_pid1_is_systemd`` only proves the *system* manager runs; ``systemctl
    --user`` can still be unreachable (no ``XDG_RUNTIME_DIR`` / D-Bus session —
    common on headless SSH or a WSL session without linger), which would make a
    later ``enable --now`` fail with a confusing message. Treat only an explicit
    "offline" status or a bus-connection failure as unavailable, so a
    present-but-degraded (or mocked) manager still passes.
    """
    proc = _systemctl("is-system-running", check=False)
    state = proc.stdout.strip().lower()
    if state == "offline":
        return False
    if not state and proc.returncode != 0 and "bus" in proc.stderr.lower():
        return False
    return True


def _require_systemd() -> None:
    if not is_linux():
        raise ClaudeSwitchError(
            "cswap service (systemd) requires Linux or WSL. "
            "Use `cswap auto` in the foreground on this platform."
        )
    if not _pid1_is_systemd():
        raise ClaudeSwitchError(
            "systemd is not running as PID 1 on this system. "
            "On WSL2, enable user systemd in /etc/wsl.conf:\n"
            "  [boot]\n"
            "  systemd=true\n"
            "Then run `wsl --shutdown` from Windows and reopen your distro."
        )
    if not _user_manager_available():
        raise ClaudeSwitchError(
            "systemd is running, but its per-user manager (systemctl --user) is "
            "not reachable in this session. Ensure you have a login session with "
            "XDG_RUNTIME_DIR set; on a headless/SSH or WSL session run "
            "`loginctl enable-linger <user>` and reopen the session."
        )


def _build_unit(switcher: ServiceHost) -> str:
    argv = service_spec.program_arguments()
    exec_start = " ".join(_systemd_escape(arg) for arg in argv)
    env_lines = [
        f'Environment="{key}={_systemd_escape_value(value)}"'
        for key, value in service_spec.passthrough_env().items()
    ]
    # Restart=on-failure restarts a crashed engine after RestartSec.
    # RestartSec=30 also keeps every restart outside the default start-limit
    # window (10s/5 tries), so the unit retries indefinitely.
    # No After=network.target: it is a no-op under the per-user manager and
    # the engine tolerates an unready network anyway (fetch failure → backoff).
    lines = [
        "[Unit]",
        "Description=Claude Swap auto-switch engine",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        "Restart=on-failure",
        "RestartSec=30",
        *env_lines,
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _systemd_escape(arg: str) -> str:
    # systemd expands % specifiers everywhere in a unit — inside quotes too —
    # so a literal % must always be doubled, before the quoting decision.
    if not arg:
        return '""'
    arg = arg.replace("%", "%%")
    if re.fullmatch(r"[A-Za-z0-9_/@:.,+%-]+", arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_escape_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def _unescape_env_value(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            out.append(value[index + 1])
            index += 2
        elif char == "%" and index + 1 < len(value) and value[index + 1] == "%":
            # Escaping doubles % after the backslash pass, so every %% pair
            # here denotes one literal % and never splits a \-escape.
            out.append("%")
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return service_spec.run_service_command([_SYSTEMCTL, "--user", *args], check=check)


def _loginctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return service_spec.run_service_command([_LOGinctl, *args], check=check)


def _installed_version() -> str | None:
    unit_path = _unit_path()
    try:
        text = unit_path.read_text(encoding="utf-8")
    except OSError:
        return None
    env_vars: dict[str, str] = {}
    for match in _ENV_LINE.finditer(text):
        pair = match.group(1).strip()
        if len(pair) >= 2 and pair.startswith('"') and pair.endswith('"'):
            # Current form: the whole KEY=value assignment is quoted.
            key, sep, value = _unescape_env_value(pair[1:-1]).partition("=")
        else:
            # Legacy form: Environment=KEY="value" (value optionally quoted).
            key, sep, value = pair.partition("=")
            value = value.strip()
            if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
                value = _unescape_env_value(value[1:-1])
        if not sep:
            continue
        env_vars[key.strip()] = value
    return service_spec.installed_version_from_env(env_vars)


# Keepalive command suggested for the Windows Task Scheduler logon task. It
# must leave a long-lived process attached to WSL's init: WSL idle-terminates
# the VM when no user-launched process remains, and processes started by
# systemd (like our engine unit) do not count. ``sleep infinity`` never
# exits, so the instance stays alive; it ships with coreutils, unlike the
# previously suggested ``dbus-launch`` (dbus-x11 package), which the default
# Ubuntu WSL image does not include. A bare ``--exec /usr/bin/true`` exits
# immediately and keeps nothing alive.
# Documented in README.md ("Run it in the background" → WSL2); a test asserts
# the two stay in sync.
_WSL_KEEPALIVE_EXEC = "sleep infinity"


def _print_wsl_guidance() -> None:
    distro = os.environ.get("WSL_DISTRO_NAME", "<distro>")
    user = os.environ.get("USER") or getpass.getuser()
    print(f"{bolded('WSL note:')} {muted('this service runs inside Linux/WSL only.')}")
    print(
        f"  {dimmed('Boot the distro at Windows login via Task Scheduler (At log on):')}"
    )
    print(f"  {dimmed(f'wsl.exe -d {distro} -u {user} --exec {_WSL_KEEPALIVE_EXEC}')}")
    print(
        f"  {dimmed('The command must leave a resident process behind (sleep infinity never exits) — WSL shuts the distro down when idle and systemd services do not keep it alive, stopping the engine.')}"
    )
    print(
        f"  {dimmed('WSL ~/.claude and Windows %USERPROFILE%\\.claude are separate; install cswap in the same environment as Claude Code.')}"
    )
    config_home = get_claude_config_home()
    if _WINDOWS_MOUNT_RE.match(str(config_home)):
        # Windows-side session PID files hold Windows PIDs, which WSL's PID
        # namespace cannot see — the engine would read every session as dead
        # (or, worse, alive by PID collision).
        warning(
            f"CLAUDE_CONFIG_DIR points at the Windows side ({config_home}). "
            "Windows Claude Code sessions are invisible to this WSL service; "
            "it can only watch Claude Code running inside WSL. Install cswap "
            "on the side where Claude Code actually runs."
        )
    backup_root = get_backup_root()
    if _WINDOWS_MOUNT_RE.match(str(backup_root)):
        # /mnt drive mounts go through 9p, which does not implement file
        # locking (microsoft/WSL#5762) — every FileLock acquire under the
        # backup dir fails and surfaces as a misleading lock timeout.
        warning(
            f"The cswap backup directory sits on a Windows drive mount "
            f"({backup_root}). The 9p filesystem behind /mnt does not support "
            "file locks, so lock operations there will fail. Keep "
            "XDG_DATA_HOME on the Linux filesystem (ext4) instead."
        )


class SystemdBackend:
    """Linux/WSL systemd --user supervisor implementing ``ServiceBackend``."""

    def install(self, switcher: ServiceHost) -> int:
        _require_systemd()
        unit_path = _unit_path()
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        log_dir = service_spec.log_dir(switcher)
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(switcher.backup_dir, 0o700)
        os.chmod(log_dir, 0o700)
        unit_path.write_text(_build_unit(switcher), encoding="utf-8")
        os.chmod(unit_path, 0o644)
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", UNIT_NAME)
        user = os.environ.get("USER") or getpass.getuser()
        linger = _loginctl("enable-linger", user)
        if linger.returncode != 0:
            warning(
                f"loginctl enable-linger {user} failed (rc={linger.returncode}): "
                f"{linger.stderr.strip() or linger.stdout.strip()}. "
                "The service may stop when you log out unless linger is enabled."
            )
        if service_spec.is_wsl():
            _print_wsl_guidance()
        command = service_spec.RUNNER_COMMAND_LABEL
        service_spec.print_install_success(
            switcher,
            artifact_path=unit_path,
            run_hint=f"runs `{command}` at login; stdout/stderr → systemd journal",
        )
        return 0

    def uninstall(self, switcher: ServiceHost) -> int:
        unit_path = _unit_path()
        existed = unit_path.exists()
        _systemctl("disable", "--now", UNIT_NAME, check=False)
        unit_path.unlink(missing_ok=True)
        if existed:
            _systemctl("daemon-reload", check=False)
        service_spec.print_uninstall_result(
            switcher,
            existed=existed,
            retained_hint="journal history retained on disk",
        )
        return 0

    def state(self) -> ServiceState:
        unit_path = _unit_path()
        if not unit_path.exists():
            return "not installed"
        active = _systemctl("is-active", UNIT_NAME, check=False)
        if active.returncode == 0 and active.stdout.strip() == "active":
            return "loaded"
        return "installed but not loaded"

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

        # ``show -p`` gives stable KEY=VALUE lines; ``systemctl status`` output
        # (``Active:`` / ``Main PID:``) is localized prose the filter in
        # print_status_loaded would drop entirely.
        proc = _systemctl(
            "show", "-p", "ActiveState", "-p", "MainPID", UNIT_NAME, check=False
        )
        service_spec.print_status_loaded(supervisor_stdout=proc.stdout)
        service_spec.print_status_decision_log(switcher)
        return 0

    def logs(self, switcher: ServiceHost, lines: int = 40) -> int:
        structured = switcher.backup_dir / "claude-swap.log"
        print(bolded("== claude-swap.log (structured) =="))
        print(f"  {dimmed(str(structured))}")
        if not structured.exists():
            print(f"  {dimmed('(none yet)')}")
        else:
            tail = structured.read_text(encoding="utf-8", errors="replace").splitlines()[
                -lines:
            ]
            for line in tail:
                print(f"  {muted(line)}")

        print(bolded(f"== journal ({UNIT_NAME}) =="))
        proc = _systemctl(
            "status",
            UNIT_NAME,
            check=False,
        )
        if proc.returncode != 0 and "could not be found" in proc.stderr.lower():
            print(f"  {dimmed('(unit not loaded yet)')}")
            return 0
        journal = service_spec.run_service_command(
            [
                "journalctl",
                "--user",
                "-u",
                UNIT_NAME,
                "-n",
                str(lines),
                "--no-pager",
            ],
            check=False,
        )
        if journal.returncode != 0:
            print(f"  {dimmed(journal.stderr.strip() or '(no journal entries yet)')}")
            return 0
        for line in journal.stdout.splitlines():
            print(f"  {muted(line)}")
        return 0
