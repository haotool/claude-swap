# Upstream sync

Short reference for regenerating `publish/clean` from [realiti4/claude-swap](https://github.com/realiti4/claude-swap) `main`.

## When to run

After merging upstream changes or when preparing a clean publish branch from integration work (e.g. `converge/095-final`).

## Steps

```bash
git fetch upstream
# Regenerate publish/clean via the maintainer's build-clean-history.sh helper
# (kept in the maintainer's working repo — NOT shipped in this tree): back up
# the branch, replay the fork's logical commit groups on top of upstream/main,
# verify each commit is green and the final tree matches the backup, then
# force-with-lease push. SOURCE=<integration branch, e.g. converge/095-final>  TARGET=publish/clean
```

Review the log:

```bash
git log upstream/main..publish/clean --oneline
uv run pytest
```

Push:

```bash
git push --force-with-lease haotool publish/clean
```

## Safety

- The script creates `backup/pre-clean-rewrite-YYYYMMDD` from `SOURCE` before rewriting.
- Use `--force-with-lease`, not bare `--force`.
- Do not delete the backup until the remote branch looks correct.
