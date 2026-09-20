"""Restore the chosen context, then run the ordinary CLI under pythonw."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
import traceback
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


class _ServiceLogHandler(RotatingFileHandler):
    """Propagate sink failures instead of writing back into redirected stderr."""

    def handleError(self, record: logging.LogRecord) -> None:
        raise


class _LogStream(io.TextIOBase):
    """Write through the handler's lock; retain no partial-line state."""

    def __init__(self, handler: RotatingFileHandler):
        super().__init__()
        self._handler = handler
        self._handler.terminator = ""

    @property
    def encoding(self) -> str:
        return "utf-8"

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if self.closed:
            raise ValueError("write to closed auto-service output")
        # Bounded records keep even a huge unterminated write within the
        # rotation budget. handle() serializes emission and flushes each record.
        for offset in range(0, len(text), 4096):
            self._handler.handle(
                logging.makeLogRecord({"msg": text[offset : offset + 4096]})
            )
        return len(text)


def run(encoded: str) -> None:
    _apply_context(_decode_context(encoded))
    from claude_swap import paths

    log = paths.get_backup_root() / "auto-service.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    handler = _ServiceLogHandler(
        log, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
    )
    old_argv, old_stdin = sys.argv, sys.stdin
    try:
        with _LogStream(handler) as output, open(os.devnull, encoding="utf-8") as stdin:
            sys.stdin = stdin
            sys.argv = ["cswap", "auto"]
            with redirect_stdout(output), redirect_stderr(output):
                print(f"Auto-service process started (pid {os.getpid()}).")
                try:
                    # CLI imports can fail too; capture them after setting up
                    # output, but only after restoring the selected context.
                    from claude_swap import cli

                    cli.main()
                except Exception:
                    traceback.print_exc()
                    raise
    finally:
        sys.argv, sys.stdin = old_argv, old_stdin
        handler.close()


if __name__ == "__main__":
    if sys.platform != "win32" or len(sys.argv) != 2:
        raise SystemExit(2)
    run(sys.argv[1])
