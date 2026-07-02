# claude-swap

Multi-account switcher for Claude Code. Easily switch between multiple Claude accounts without logging out. Works with both the Claude Code CLI and the VS Code extension.

> **Fork.** This is the [haotool/claude-swap](https://github.com/haotool/claude-swap) fork of [realiti4/claude-swap](https://github.com/realiti4/claude-swap), adding **auto-switch at usage limit** and a **cross-platform background service** (macOS launchd, Linux/WSL systemd, Windows Task Scheduler). **Not published to PyPI** — install from source or git (see below). Upstream releases remain on PyPI as `claude-swap`.

### Versioning (this fork)

| Item | Value |
|------|-------|
| **Release version (SSOT)** | `pyproject.toml` → `[project].version` (currently `0.15.0b2+haotool.1`) |
| **Scheme** | [PEP 440](https://peps.python.org/pep-0440/) with a **local version label** (`+haotool.1`) to distinguish this fork from upstream |
| **PyPI** | **Not publishable** — PyPI rejects local version segments (`+…`); this fork is installed from git/source only |
| **Upstream PyPI** | Publishes plain semver (e.g. `0.15.0b2`) without the `+haotool.*` suffix |

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
cswap --upgrade        # auto-detects uv/pipx on macOS/Linux
uv tool upgrade claude-swap
pipx upgrade claude-swap
```

## Usage

### Add your first account

Log into Claude Code with your first account, then:

```bash
cswap --add-account
```

### Add more accounts

Log in with another account, then:

```bash
cswap --add-account
```

### Switch accounts

Rotate to the next account:

```bash
cswap --switch
```

Or switch to a specific account:

```bash
cswap --switch-to 2
cswap --switch-to user@example.com
```

Or let claude-swap auto-pick by remaining quota — `cswap --switch --strategy best` (most quota left) or `--strategy next-available` (skip rate-limited accounts).

**Note:** You usually don't need to restart — on Linux/Windows the new account is picked up automatically, and on macOS after the Keychain cache expires. To apply it instantly, restart Claude Code or reopen the VS Code extension tab. See [Tips](#tips) for the per-platform details.

### Run multiple accounts at the same time (session mode)

Launch Claude Code as a specific account in the current terminal only — every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel.

```bash
cswap run 2                     # launch Claude Code as account 2, here only
cswap run user@example.com      # by email
cswap run 2 -- --resume         # everything after '--' is forwarded to claude
cswap run 2 --no-share          # don't share your ~/.claude customizations
```

Your `~/.claude` customizations (settings, keybindings, CLAUDE.md, skills, commands, agents) are shared into the session by default — use `--no-share` for a bare profile. Conversation history stays per-account.

### Auto-switch at usage limit (Beta)

Launch the interactive menu with `cswap --tui`. Alongside the account actions it
offers **Watch (live status + usage)** — a read-only dashboard that re-captures
`cswap --list` on an interval and redraws in place — and **Auto-switch at limit
(Beta)**:

```bash
cswap --tui
```

Or start the same foreground monitor directly from the CLI:

```bash
cswap --monitor
```

From there you can:

- **Enable/Disable** automatic switching (the setting persists across runs).
- **Set threshold** — the usage percentage that triggers a switch (default `95%`).
- **Start monitor now** — runs the same adaptive auto-switch engine as
  `cswap --monitor` (typically 5–60s polling based on usage velocity).
  When usage reaches the threshold, it picks the best target using trusted
  usage snapshots: unsaturated accounts first, otherwise the soonest
  cooldown reset. If you are already on that account, it holds until the
  window frees. Press `s` to check immediately, or `q`/`Esc` to stop.

Because switching doesn't require a Claude Code restart (see the note above),
the new account takes effect on your next message — on macOS once the Keychain
cache expires. For automated paths (TUI monitor + background service) the target
account's OAuth token is force-refreshed *before* activation, so the first API
call after handoff uses a freshly-issued token with maximum remaining lifetime.
The TUI's **Start monitor now** is foreground-only; `cswap --monitor` and
`cswap service install` run the same engine in the background. `cswap --monitor`
records its PID and exits without starting another monitor if one is already
running. Manual
`cswap --switch` still uses predictable round-robin; automated paths never
guess from stale cache — they require fresh usage snapshots (from polling or
`cswap --list`).

> **Beta:** automated switching is new. Usage percentages come from the same
> API as `cswap --list`. Please report any rough
> edges via [Issues](https://github.com/haotool/claude-swap/issues).

#### Run it in the background

Install and run `cswap` in the **same environment as Claude Code** — WSL `~/.claude` and Windows `%USERPROFILE%\.claude` are separate credential stores.

```bash
cswap service install      # start at login (platform-native supervisor)
cswap service status       # is it loaded? last exit?
cswap service logs         # tail recent monitor output
cswap service uninstall    # stop and remove
```

The service runs the same `cswap --monitor` loop, restarting on failure and writing a structured log to `<backup_dir>/claude-swap.log`. `cswap --monitor` remains available for a foreground run.

| Platform | Supervisor | What `install` writes |
|----------|------------|------------------------|
| **macOS** | launchd LaunchAgent | `~/Library/LaunchAgents/com.claude-swap.monitor.plist` — stdout/stderr → `<backup_dir>/logs/monitor.{out,err}` |
| **Linux** | systemd user unit | `~/.config/systemd/user/cswap-monitor.service` — `systemctl --user enable --now`; also runs `loginctl enable-linger $USER` so the monitor survives logout |
| **Windows** | Task Scheduler | Per-user **At log on** task `cswap-monitor` (no admin, hidden via `pythonw.exe`); task XML saved under `<backup_dir>/logs/` |
| **WSL2** | systemd user unit (inside the distro) | Same as Linux; see below |

**Linux / WSL (systemd --user).** The unit runs `<absolute python> -m claude_swap --monitor` with `Restart=on-failure` and `RestartSec=30`. After install, check with `cswap service status` or `journalctl --user -u cswap-monitor -f`. If `loginctl enable-linger` fails, the monitor may stop when you log out — enable linger manually for your user.

**WSL2.** Install `cswap` **inside** the WSL distro where Claude Code runs, not on Windows native. User systemd must be enabled in `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Then from Windows: `wsl --shutdown`, reopen the distro, and run `cswap service install` there. WSL may shut down the distro when idle — the monitor stops until WSL restarts. To boot the distro at Windows login, add a **Task Scheduler** task (At log on, no admin):

```text
wsl.exe -d <distro> -u <user> --exec /usr/bin/true
```

Replace `<distro>` with `echo $WSL_DISTRO_NAME` inside WSL and `<user>` with your Linux username. `cswap service install` prints this guidance on WSL.

**Windows (native).** The scheduled task runs at logon under your user account (`RunLevel` limited — no elevation). Monitor output goes to the structured log; use `cswap service logs` to inspect it.

#### Failure modes and upgrade

Automated switching is **fail-closed**: it never guesses from stale cache.
Manual `cswap --switch` still uses round-robin.

| Symptom | Meaning | What to do |
|---------|---------|------------|
| `no trusted usage snapshots` / `Cannot choose auto-switch target` | Usage cache is cold or expired at threshold — the monitor will not rotate blindly | Run `cswap --list` to seed fresh snapshots (do this on every machine with auto-switch enabled **before** upgrading) |
| `already on optimal` / hold at threshold | You are already on the soonest-to-free account; rotation would not help | Expected — wait for the cooldown window to reset |
| `Only one account` (background monitor) | Auto-switch needs at least two managed accounts | Add another account with `cswap --add-account` |
| Monitor idle / no switches after upgrade | Background service may still be on an old build or cold cache | After upgrading: run `cswap --list`, then `cswap service install`, then `cswap service status` |

**Upgrade checklist (beta)**

1. **Before deploy:** `cswap --list` on each machine with auto-switch enabled.
2. **After deploy:** `cswap service install` (background users), verify `cswap service status`, tail `cswap service logs` or `claude-swap.log` for the first threshold event.

### Refresh expired tokens

If an account's token expires, log back into Claude Code with that account and re-run:

```bash
cswap --add-account
```

This will update the stored credentials without creating a duplicate.

### Other commands

```bash
cswap run 2                     # Run an account in this terminal only (session mode)
cswap --list                    # Show all accounts with 5h/7d usage and reset times
cswap --list --token-status     # --list plus per-account OAuth token status (valid/expired)
cswap --health                  # Show account health, usage, and OAuth token status
cswap --status                  # Show current account
cswap --add-account --slot 3    # Add account to a specific slot (prompts before overwrite)
cswap --remove-account 2        # Remove an account
cswap --tui                     # Launch the interactive arrow-key menu (incl. Watch + auto-switch, Beta)
cswap --monitor                 # Run the foreground auto-switch monitor
cswap --upgrade                 # Upgrade upstream/PyPI installs; fork builds use git pull
cswap --purge                   # Remove all claude-swap data
```

## Tips

- **Do you need to restart after switching?** Usually not. On **Linux and Windows**, credentials are stored in a file and Claude Code re-reads them whenever that file changes, so the new account takes effect on your next message — no restart needed. On **macOS**, credentials live in the Keychain, which Claude Code caches for about 30 seconds; a running session picks up the switch once that cache expires. Restart Claude Code (or close and reopen the VS Code extension tab) only if you want the change to apply instantly.
- **Continuing sessions after switching:** You can keep using the same Claude Code session after switching — run `cswap --switch` in any terminal and carry on. If you'd prefer a clean start, close and reopen Claude Code (or the VS Code extension tab) and use `--resume` to pick your previous session. Either way, the first message on the new account may use extra usage as its conversation cache rebuilds.

## How it works

- Backs up OAuth tokens and config when you add an account
- Swaps credentials when you switch accounts
- Account credentials stored securely using platform-appropriate methods

## Data locations

| Platform | Credentials | Config backups |
|----------|-------------|----------------|
| Windows | File-based (inside the backup directory, under `credentials/`) | `~/.claude-swap-backup/` |
| macOS | macOS Keychain | `~/.claude-swap-backup/` |
| Linux / WSL | File-based (inside the backup directory, under `credentials/`) | `${XDG_DATA_HOME:-~/.local/share}/claude-swap/` |

Session-mode profiles (`cswap run`) live under the backup directory in `sessions/`.

On Linux/WSL, set `XDG_DATA_HOME` to override the default location. Data from older installs under `~/.claude-swap-backup/` is migrated automatically on first run.

## Advanced

### Backup and migration

Move account data between machines or back it up:

```bash
cswap --export backup.cswap                  # All accounts to a file
cswap --export backup.cswap --account 2      # One account
cswap --export backup.cswap --full           # Include full local ~/.claude.json (same-PC backup)
cswap --import backup.cswap                  # Skips accounts that already exist
cswap --import backup.cswap --force          # Overwrite existing
```

The export file is plaintext JSON. If you need encryption, pipe through your tool of choice (e.g. `cswap --export - | gpg -c > backup.gpg`).

### JSON output for scripting

Add `--json` to `--list`, `--status`, `--switch`, or `--switch-to` to emit a single machine-readable JSON object on stdout (human-readable notices go to stderr). Useful for scripting auto-swap and quota tracking.

```bash
cswap --list --json                 # all accounts with usage/quota
cswap --status --json               # current active account
cswap --switch --strategy best --json   # switch, then report the result
cswap --switch-to 2 --json
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

</details>

### Add an account from a raw token or API key

If you only have a long-lived setup-token (e.g., produced by `claude setup-token`)
or a managed API key (`sk-ant-api...`) and you don't want to log in via the browser
flow first — useful on headless servers or when receiving a token from another
machine — register it directly. The token type is auto-detected:

```bash
cswap --add-token sk-ant-oat01-...           # OAuth setup-token
cswap --add-token sk-ant-api03-...           # managed API key
cswap --add-token sk-ant-oat01-... --slot 3
cswap --add-token - --slot 3                 # read token from stdin
cswap --add-token --email user@example.com   # optional label override
```

`--email` is optional; omitted values use `setup-token-{slot}@token.local`
(or `api-key-{slot}@token.local` for API keys). No Anthropic API calls are made.

**API-key accounts.** An `sk-ant-api...` value registers a managed API-key account
(the kind Claude Code uses after `/login` with a key) rather than an OAuth
setup-token. It switches like any other account; since API keys have no subscription
quota, they show no usage and the usage-aware `--switch` strategies never skip them as
rate-limited.

## Uninstall

Remove all data:

```bash
cswap --purge
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
