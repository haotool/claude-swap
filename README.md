# claude-swap

Multi-account switcher for Claude Code. Easily switch between multiple Claude accounts without logging out. Works with both the Claude Code CLI and the VS Code extension.

> **Fork.** This is the [haotool/claude-swap](https://github.com/haotool/claude-swap) fork of [realiti4/claude-swap](https://github.com/realiti4/claude-swap), adding **auto-switch at usage limit** and a **cross-platform background service** (macOS launchd, Linux/WSL systemd, Windows Task Scheduler). **Not published to PyPI** — install from source or git (see below). Upstream releases remain on PyPI as `claude-swap`.

### Versioning (this fork)

| Item | Value |
|------|-------|
| **Release version (SSOT)** | `pyproject.toml` → `[project].version` (currently `0.17.1+haotool.1`) |
| **Scheme** | [PEP 440](https://peps.python.org/pep-0440/) with a **local version label** (`+haotool.1`) to distinguish this fork from upstream |
| **PyPI** | **Not publishable** — PyPI rejects local version segments (`+…`); this fork is installed from git/source only |
| **Upstream PyPI** | Publishes plain semver (e.g. `0.15.1`) without the `+haotool.*` suffix |

When bumping a release, edit **only** `[project].version` in `pyproject.toml` and record the change in `CHANGELOG.md`. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup.

## Installation

### From source (this fork)

```bash
git clone https://github.com/haotool/claude-swap.git
cd claude-swap
uv sync --all-groups
uv run cswap --help
```

Or install into your path:

```bash
uv tool install --editable .
# or: pip install -e .
```

### Upstream (PyPI)

The upstream project publishes to PyPI. These installs do **not** include this fork's auto-switch or background service:

```bash
uv tool install claude-swap
# or: pipx install claude-swap
```

### Updating

**This fork:** pull latest and reinstall:

```bash
git pull
uv sync
# if installed with uv tool / pip -e:
uv tool upgrade --reinstall claude-swap  # when using uv tool install --editable .
```

**Upstream PyPI installs** (`uv tool` / `pipx`):

```bash
cswap upgrade          # auto-detects uv/pipx on macOS/Linux
uv tool upgrade claude-swap
pipx upgrade claude-swap
```

## Usage

### Add your first account

Log into Claude Code with your first account, then:

```bash
cswap add
```

### Add more accounts

Log in with another account, then:

```bash
cswap add
```

### Switch accounts

Rotate to the next account:

```bash
cswap switch
```

Or switch to a specific account:

```bash
cswap switch 2
cswap switch user@example.com
```

Not sure which one? `cswap list` is the dashboard — every account's 5-hour and 7-day usage and reset times at a glance:

```bash
cswap list
```

Or let claude-swap auto-pick by remaining quota — `cswap switch --strategy best` (most quota left) or `--strategy next-available` (skip rate-limited accounts).

**Note:** You usually don't need to restart — on Linux/Windows the new account is picked up automatically, and on macOS after the Keychain cache expires. To apply it instantly, restart Claude Code or reopen the VS Code extension tab. See [Tips](#tips) for the per-platform details.

### Automatic switching

Let claude-swap watch your usage and switch for you. When the active account's 5-hour or 7-day window reaches the threshold (default 90%), it switches to the account with the most quota left — before you hit the limit, and safe to run while Claude Code is working:

```bash
cswap auto                     # foreground loop, polls every 60s
cswap auto --threshold 80      # switch earlier
cswap auto --once              # single check-and-switch, for cron/scripts
cswap auto --dry-run           # log what it would do, never switch
```

<details>
<summary>How it behaves & advanced usage</summary>

- Runs safely alongside Claude Code: switches take the same credential locks Claude Code uses, so a swap never collides with a token refresh.
- A cooldown (default 5 min) and a hysteresis margin stop it flip-flopping near the threshold; when every account is exhausted it sleeps until the earliest reset.
- Usage polling is adaptive — a couple of accounts per check, busy alternates watched more closely, exhausted ones left alone until they reset — so API traffic stays flat no matter how many accounts you manage.
- It fails safe: if a usage check errors it keeps trusting the last-known numbers while retries back off, and an expired token on an idle machine makes it hold rather than fail over (Claude Code refreshes the token on your next message).
- An account whose refresh token has died is quarantined and reported until you log in with it and re-run `cswap add --slot N`. API-key accounts are never rotated onto unless you pass `--include-api-key-accounts`.

For cron/systemd timers, `--once` reports the outcome in its exit code (`0` switched, `1` error, `2` nothing to do, `3` blocked — no viable target), and `--json` emits one JSON event per line:

```bash
*/5 * * * * cswap auto --once --json >> ~/.cswap-auto.log 2>&1
```

Defaults like the threshold and cooldown are configurable with `cswap config set autoswitch.threshold 80` — flags override them (see [Configuration](#configuration)).

</details>

### Run multiple accounts at the same time (session mode)

Launch Claude Code as a specific account in the current terminal only — every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel.

```bash
cswap run 2                     # launch Claude Code as account 2, here only
cswap run user@example.com      # by email
cswap run 2 -- --resume         # everything after '--' is forwarded to claude
cswap run 2 --share-history     # share your chat history with this account too
```

Sessions use your normal `~/.claude` setup (settings, CLAUDE.md, skills, etc.), but each account keeps its own chat history. Pass `--share-history` if you want your accounts to continue the same conversations — a session started under one account shows up in `--resume` under the others, and nothing already saved is lost. Not supported on Windows yet.

### The interactive menu

Launch the arrow-key menu with `cswap --tui`. Alongside the account actions it
offers **Watch (live status + usage)** — a read-only dashboard that redraws in
place, polling through the usage store like `cswap auto` (active account plus
one alternate per pass) and flashing rows whose usage just refreshed — and
**Auto-switch at limit**, a frontend over the same engine as `cswap auto`:

```bash
cswap --tui
```

From there you can set the switch threshold (persisted in `settings.json`),
install or remove the background service, and run the engine in the foreground
with a live event feed. Press `s` to check immediately, `q`/`Esc` to stop.

Because switching doesn't require a Claude Code restart (see the note above),
the new account takes effect on your next message — on macOS once the Keychain
cache expires. The engine refreshes the target's OAuth token *before*
activation when it is close to expiry, so the first API call after handoff
uses a token with plenty of lifetime left. Manual `cswap --switch` still uses
predictable round-robin.

#### Run it in the background

Install and run `cswap` in the **same environment as Claude Code** — WSL `~/.claude` and Windows `%USERPROFILE%\.claude` are separate credential stores.

```bash
cswap service install      # start at login (platform-native supervisor)
cswap service status       # is it loaded? last exit?
cswap service logs         # tail recent engine output
cswap service uninstall    # stop and remove
```

The service supervises the same `cswap auto` loop (see [Automatic switching](#automatic-switching)), restarting on failure. Engine events go to the supervisor's log files and are mirrored into the structured log at `<backup_dir>/claude-swap.log` — the surface to check on Windows, where the hidden task has no visible stdout.

| Platform | Supervisor | What `install` writes |
|----------|------------|------------------------|
| **macOS** | launchd LaunchAgent | `~/Library/LaunchAgents/com.claude-swap.monitor.plist` — stdout/stderr → `<backup_dir>/logs/monitor.{out,err}` |
| **Linux** | systemd user unit | `~/.config/systemd/user/cswap-monitor.service` — `systemctl --user enable --now`; also runs `loginctl enable-linger $USER` so the engine survives logout |
| **Windows** | Task Scheduler | Per-user **At log on** task `cswap-monitor` (no admin, hidden via `pythonw.exe`); task XML saved under `<backup_dir>/logs/` |
| **WSL2** | systemd user unit (inside the distro) | Same as Linux; see below |

**Linux / WSL (systemd --user).** The unit runs `<absolute python> -m claude_swap auto` with `Restart=on-failure` and `RestartSec=30`. After install, check with `cswap service status` or `journalctl --user -u cswap-monitor -f`. If `loginctl enable-linger` fails, the engine may stop when you log out — enable linger manually for your user.

**WSL2.** Install `cswap` **inside** the WSL distro where Claude Code runs, not on Windows native. User systemd must be enabled in `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Then from Windows: `wsl --shutdown`, reopen the distro, and run `cswap service install` there. WSL shuts the distro down when idle, and [systemd services do not keep it alive](https://learn.microsoft.com/en-us/windows/wsl/systemd) — so the engine stops unless a user-launched process holds the instance open. To boot the distro at Windows login **and** keep it alive, add a **Task Scheduler** task (At log on, no admin):

```text
wsl.exe -d <distro> -u <user> --exec sleep infinity
```

`sleep infinity` never exits, which is what keeps the instance from idle-terminating (a command that exits immediately would not), and it ships with coreutils on every distro. Replace `<distro>` with `echo $WSL_DISTRO_NAME` inside WSL and `<user>` with your Linux username. `cswap service install` prints this guidance on WSL.

Register it in one shot from Windows PowerShell (no admin needed):

```powershell
Register-ScheduledTask -TaskName 'cswap-wsl-keepalive' -Action (New-ScheduledTaskAction -Execute 'wsl.exe' -Argument '-d <distro> -u <user> --exec sleep infinity') -Trigger (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME") -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries) -Force
```

Two Task Scheduler defaults matter here: an `AtLogOn` trigger not scoped to a user is denied without elevation (`0x80070005`), and the GUI default **execution time limit (3 days)** silently kills the keepalive after 72 hours — stopping the engine with it. The command above scopes the trigger to your user and disables the limit (`[TimeSpan]::Zero`). To remove it later: `Unregister-ScheduledTask -TaskName 'cswap-wsl-keepalive' -Confirm:$false`.

**Windows (native).** The scheduled task runs at logon under your user account (`RunLevel` limited — no elevation). Engine events go to the structured log; use `cswap service logs` to inspect it.

#### Failure modes

The engine polls the usage API directly each tick, so there is no cache to
pre-seed. Common event lines and what they mean:

| Event | Meaning | What to do |
|---------|---------|------------|
| `no switch: cooldown` | A switch just happened; the cooldown floor bounds the switch rate | Expected — wait it out, or lower `cooldownSeconds` in `settings.json` |
| `no switch: no-viable-target` | No candidate has meaningfully more headroom than the active account | Expected near-uniform usage; add an account if it persists |
| `all accounts exhausted` | Every account is at its limit; the engine sleeps until the earliest reset | Expected — it wakes and switches on its own |
| `account quarantined` | An account's refresh token has died | Log in with it and re-run `cswap --add-account --slot N` |

After upgrading, reinstall the service once (`cswap service install`) so the
supervisor picks up the new entry point, then check `cswap service status`.

### Refresh expired tokens

If an account's token expires, log back into Claude Code with that account and re-run:

```bash
cswap add
```

This will update the stored credentials without creating a duplicate.

### Other commands

```bash
cswap run 2                     # Run an account in this terminal only (session mode)
cswap auto                      # Auto-switch when nearing rate limits (see above)
cswap config                    # Show or edit settings (see Configuration below)
cswap list                      # Show all accounts with 5h/7d usage and reset times
cswap list --token-status       # list plus per-account OAuth token status (valid/expired)
cswap --health                  # Show account health, usage, and OAuth token status
cswap status                    # Show current account
cswap add --slot 3              # Add account to a specific slot (prompts before overwrite)
cswap remove 2                  # Remove an account
cswap tui                       # Launch the interactive arrow-key menu (incl. Watch + auto-switch)
cswap upgrade                   # Upgrade upstream/PyPI installs; fork builds use git pull
cswap purge                     # Remove all claude-swap data
```

The original flag spellings (`cswap --switch`, `cswap --list`, ...) keep working.

## Tips

- **Do you need to restart after switching?** Usually not. On **Linux and Windows**, credentials are stored in a file and Claude Code re-reads them whenever that file changes, so the new account takes effect on your next message — no restart needed. On **macOS**, credentials live in the Keychain, which Claude Code caches for about 30 seconds; a running session picks up the switch once that cache expires. Restart Claude Code (or close and reopen the VS Code extension tab) only if you want the change to apply instantly.
- **Continuing sessions after switching:** You can keep using the same Claude Code session after switching — run `cswap switch` in any terminal and carry on. If you'd prefer a clean start, close and reopen Claude Code (or the VS Code extension tab) and use `--resume` to pick your previous session. Either way, the first message on the new account may use extra usage as its conversation cache rebuilds.

## How it works

- Backs up OAuth tokens and config when you add an account
- Swaps credentials when you switch accounts
- Account credentials stored securely using platform-appropriate methods
- Switches (manual and automatic) hold Claude Code's own credential locks while writing, so a swap never interleaves with a token refresh
- Auto-switch freshens a target's token before activating it, and quarantines accounts whose refresh token has died (recover with `cswap add --slot N`)

## Data locations

| Platform | Credentials | Config backups |
|----------|-------------|----------------|
| Windows | File-based (inside the backup directory, under `credentials/`) | `~/.claude-swap-backup/` |
| macOS | macOS Keychain | `~/.claude-swap-backup/` |
| Linux / WSL | File-based (inside the backup directory, under `credentials/`) | `${XDG_DATA_HOME:-~/.local/share}/claude-swap/` |

Session-mode profiles (`cswap run`) live under the backup directory in `sessions/`. Tool preferences (`settings.json`) and auto-switch state (`autoswitch_state.json` — cooldown and quarantined accounts; delete it to reset) live in the backup directory root.

On Linux/WSL, set `XDG_DATA_HOME` to override the default location.

## Advanced

### Configuration

Tool preferences live in `settings.json` in the backup root; `cswap config` reads and edits it with validation, so you never have to find the file or guess valid ranges.

<details>
<summary>Commands & usage</summary>

```bash
cswap config                              # list effective settings ("(default)" = not set)
cswap config get autoswitch.threshold
cswap config set autoswitch.threshold 80  # validated: rejects out-of-range values loudly
cswap config unset autoswitch.threshold   # back to the default
cswap config path                         # where settings.json lives
```

`cswap config --help` lists every key with its valid range and default. Hand-editing the file still works — `cswap config` is just a safer front door. `list` and `get` take `--json` for scripting.

</details>

### Backup and migration

Move account data between machines or back it up:

```bash
cswap export backup.cswap                    # All accounts to a file
cswap export backup.cswap --account 2        # One account
cswap export backup.cswap --full             # Include full local ~/.claude.json (same-PC backup)
cswap import backup.cswap                    # Skips accounts that already exist
cswap import backup.cswap --force            # Overwrite existing
```

The export file is plaintext JSON. If you need encryption, pipe through your tool of choice (e.g. `cswap export - | gpg -c > backup.gpg`).

If an imported account is the one you're currently logged in as, activate the imported credentials with `cswap switch N --force` (a plain `switch` to the current account is a safe no-op and won't touch the import).

### JSON output for scripting

Add `--json` to `list`, `status`, or `switch` to emit a single machine-readable JSON object on stdout (human-readable notices go to stderr). Useful for scripting auto-swap and quota tracking.

```bash
cswap list --json                   # all accounts with usage/quota
cswap status --json                 # current active account
cswap switch --strategy best --json # switch, then report the result
cswap switch 2 --json
```

<details>
<summary>Example output & schema notes</summary>

```json
{
  "schemaVersion": 1,
  "activeAccountNumber": 2,
  "accounts": [
    { "number": 2, "email": "you@example.com", "active": true, "usageStatus": "ok",
      "usage": { "fiveHour": { "pct": 25.0, "resetsAt": "2026-06-22T23:29:59Z" },
                 "sevenDay": { "pct": 16.0, "resetsAt": "2026-06-26T17:59:59Z" } } }
  ]
}
```

Every payload carries a `schemaVersion` (currently `1`); on a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code. `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

Usage is served from a per-account cache: when the usage API is briefly unreachable, the last-known numbers are shown instead of nothing (the human view marks them with their age, e.g. `· 2m ago`). Rows with usage carry additive `usageFetchedAt`/`usageAgeSeconds` fields telling you how old the measurement is.

</details>

`cswap auto --json` emits an event *stream* instead — one JSON object per line (`{"schemaVersion":1,"event":"switch","ts":…, …}` with kinds like `poll`, `switch`, `no-switch`, `account-quarantined`, `all-exhausted`, `error`). The contract is additive: new kinds and fields may appear, so scripts should ignore unknown ones.

### Add an account from a raw token or API key

If you only have a long-lived setup-token (e.g., produced by `claude setup-token`)
or a managed API key (`sk-ant-api...`) and you don't want to log in via the browser
flow first — useful on headless servers or when receiving a token from another
machine — register it directly. The token type is auto-detected:

```bash
cswap add-token sk-ant-oat01-...             # OAuth setup-token
cswap add-token sk-ant-api03-...             # managed API key
cswap add-token sk-ant-oat01-... --slot 3
cswap add-token - --slot 3                   # read token from stdin
cswap add-token --email user@example.com     # optional label override
```

`--email` is optional; omitted values use `setup-token-{slot}@token.local`
(or `api-key-{slot}@token.local` for API keys). No Anthropic API calls are made.

**API-key accounts.** An `sk-ant-api...` value registers a managed API-key account
(the kind Claude Code uses after `/login` with a key) rather than an OAuth
setup-token. It switches like any other account; since API keys have no subscription
quota, they show no usage and the usage-aware `switch` strategies never skip them as
rate-limited.

## Uninstall

Remove all data:

```bash
cswap purge
```

Then uninstall the tool:

```bash
uv tool uninstall claude-swap
# or
pipx uninstall claude-swap
```

## Requirements

- Python 3.12+
- Claude Code installed and logged in

## License

MIT
