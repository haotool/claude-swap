"""Multi-account switcher for Claude Code."""

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("claude-swap")
except PackageNotFoundError:
    with (Path(__file__).resolve().parents[2] / "pyproject.toml").open("rb") as f:
        __version__ = tomllib.load(f)["project"]["version"]

from claude_swap.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]
