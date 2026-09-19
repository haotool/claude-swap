"""Private pythonw entry point: restore the chosen context, then run the CLI.

No switching policy lives here. pythonw has no standard streams; give the
ordinary CLI a bounded, service-only output log instead of dropping its errors
or making a long-running StringIO grow without limit.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _decode_context(encoded: str) -> dict:
    value = json.loads(base64.b64decode(encoded, validate=True))
    if not isinstance(value, dict) or set(value) != {"version", "home", "config"}:
        raise ValueError("Invalid auto-service context")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unsupported auto-service context version")
    for key in ("home", "config"):
        path = value[key]
        if key == "config" and path is None:
            continue
        if not isinstance(path, str) or "\x00" in path or not Path(path).is_absolute():
            raise ValueError(f"Invalid auto-service {key}")
    return value


def _apply_context(context: dict) -> None:
    os.environ["USERPROFILE"] = context["home"]
    os.environ["HOME"] = context["home"]
    if context["config"] is None:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    else:
        os.environ["CLAUDE_CONFIG_DIR"] = context["config"]


class _LogStream(io.TextIOBase):
    """Line-buffered text sink with a bound even on unterminated writes."""

    def __init__(self, logger: logging.Logger):
        super().__init__()
        self._logger = logger
        self._pending = ""

    @property
    def encoding(self) -> str:
        return "utf-8"

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        size = len(text)
        # Do not retain an unbounded partial line or create an oversized record.
        for offset in range(0, size, 4096):
            self._pending += text[offset:offset + 4096]
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                if line:
                    self._logger.info("%s", line)
            if len(self._pending) >= 4096:
                self.flush()
        return size

    def flush(self) -> None:
        if self._pending:
            self._logger.info("%s", self._pending)
            self._pending = ""


def run(encoded: str) -> None:
    context = _decode_context(encoded)
    _apply_context(context)
    # Import only after installing the context: CLI startup resolves paths too.
    from claude_swap import cli, paths

    log = paths.get_backup_root() / "auto-service.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    logger = logging.Logger("claude-swap.auto-service", logging.INFO)
    logger.addHandler(handler)
    output = _LogStream(logger)
    old_argv, old_stdin = sys.argv, sys.stdin
    try:
        with open(os.devnull, "r", encoding="utf-8") as stdin:
            sys.stdin = stdin
            sys.argv = ["cswap", "auto"]
            with redirect_stdout(output), redirect_stderr(output):
                logger.info("Auto-service process started (pid %s).", os.getpid())
                try:
                    cli.main()
                except Exception:
                    logger.exception("Auto-service startup or execution failed")
                    raise
    finally:
        sys.argv, sys.stdin = old_argv, old_stdin
        output.flush()
        output.close()
        handler.close()


if __name__ == "__main__":
    if sys.platform != "win32" or len(sys.argv) != 2:
        raise SystemExit(2)
    run(sys.argv[1])
