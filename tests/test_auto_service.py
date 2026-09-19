"""The private runner restores context before entering the ordinary CLI."""

import base64
import io
import json
import logging
import os
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from claude_swap import _auto_service as runner


def encoded(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


def context(tmp_path, config=None):
    return {"version": 1, "home": str(tmp_path), "config": config}


@pytest.mark.parametrize("patch", [
    {"version": True}, {"version": 2}, {"home": "relative"},
    {"config": "relative"}, {"config": 123}, {"home": None},
    {"home": "bad\x00path"}, {"extra": "not permitted"},
])
def test_invalid_context_is_rejected(tmp_path, patch):
    value = context(tmp_path)
    value.update(patch)
    with pytest.raises(ValueError):
        runner._decode_context(encoded(value))


@pytest.mark.parametrize("value", [None, [], "a string", {}, 1])
def test_context_must_be_complete_object(value):
    with pytest.raises(ValueError):
        runner._decode_context(encoded(value))


def test_stale_scheduler_config_is_cleared_for_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "stale"))
    monkeypatch.setenv("HOME", "stale-home")
    monkeypatch.setenv("USERPROFILE", "stale-home")
    runner._apply_context(context(tmp_path))
    assert "CLAUDE_CONFIG_DIR" not in os.environ
    assert os.environ["HOME"] == str(tmp_path)
    assert os.environ["USERPROFILE"] == str(tmp_path)


def test_custom_config_survives_encoding_and_runtime(tmp_path, monkeypatch):
    custom = str(tmp_path / "空白 & o'brien")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "stale")
    monkeypatch.setenv("HOME", "stale")
    monkeypatch.setenv("USERPROFILE", "stale")
    value = runner._decode_context(encoded(context(tmp_path, custom)))
    runner._apply_context(value)
    assert os.environ["CLAUDE_CONFIG_DIR"] == custom


def test_output_stream_flushes_partial_lines_once():
    logger = Mock()
    output = runner._LogStream(logger)
    assert output.write("one\ntwo") == 7
    assert logger.info.call_args_list[0].args == ("%s", "one")
    output.close()
    assert logger.info.call_args_list[-1].args == ("%s", "two")
    assert logger.info.call_count == 2


def test_unterminated_output_has_bounded_records_and_buffer():
    logger = Mock()
    output = runner._LogStream(logger)
    output.write("x" * 100_000)
    assert len(output._pending) < 4096
    output.close()
    records = [call.args[1] for call in logger.info.call_args_list]
    assert "".join(records) == "x" * 100_000
    assert max(map(len, records)) <= 8192


def test_output_failure_does_not_recurse_through_redirected_stderr(tmp_path, monkeypatch):
    handler = runner._ServiceLogHandler(tmp_path / "output.log", delay=True)
    logger = logging.Logger("service-test")
    logger.addHandler(handler)
    output = runner._LogStream(logger)
    monkeypatch.setattr(handler, "_open", Mock(side_effect=OSError("disk unavailable")))
    monkeypatch.setattr(sys, "stderr", output)
    try:
        with pytest.raises(OSError, match="disk unavailable"):
            output.write("message\n")
    finally:
        output.close()
        handler.close()


def test_runner_calls_normal_cli_with_context_and_restores_streams(tmp_path, monkeypatch):
    from claude_swap import cli, paths

    for name in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(name, "stale")
    monkeypatch.setattr(paths, "get_backup_root", lambda: tmp_path / "backup")
    before_argv, before_stdin = sys.argv, sys.stdin
    before_out, before_err = sys.stdout, sys.stderr
    seen = []
    custom = str(tmp_path / "selected profile")

    def main():
        seen.append((sys.argv[:], os.environ["CLAUDE_CONFIG_DIR"]))
        assert sys.stdin.read() == ""
        assert sys.stdout.encoding == "utf-8"
        print("ordinary CLI output")
        print("ordinary CLI error", file=sys.stderr)
        raise SystemExit(0)

    monkeypatch.setattr(cli, "main", main)
    with pytest.raises(SystemExit) as result:
        runner.run(encoded(context(tmp_path, custom)))
    assert result.value.code == 0
    assert seen == [(["cswap", "auto"], custom)]
    assert sys.argv is before_argv and sys.stdin is before_stdin
    assert sys.stdout is before_out and sys.stderr is before_err
    log = (tmp_path / "backup" / "auto-service.log").read_text()
    assert "ordinary CLI output" in log and "ordinary CLI error" in log


def test_runner_does_not_relabel_cli_failure_as_success(tmp_path, monkeypatch):
    from claude_swap import cli, paths

    for name in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(name, "stale")
    monkeypatch.setattr(paths, "get_backup_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "main", Mock(side_effect=SystemExit(1)))
    with pytest.raises(SystemExit) as result:
        runner.run(encoded(context(tmp_path)))
    assert result.value.code == 1


def test_default_and_explicit_default_keep_distinct_global_config_paths(tmp_path, monkeypatch):
    from claude_swap import paths

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(name, "stale")
    runner._apply_context(context(tmp_path))
    assert paths.get_global_config_path() == tmp_path / ".claude.json"
    runner._apply_context(context(tmp_path, str(tmp_path / ".claude")))
    assert paths.get_global_config_path() == tmp_path / ".claude" / ".claude.json"
