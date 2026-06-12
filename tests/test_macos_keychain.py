"""Unit tests for the macOS ``security``-CLI wrapper (claude_swap.macos_keychain).

These mock ``subprocess.run`` so they exercise the wrapper's argv/stdin shaping,
hex encoding, and return-code handling without ever invoking the real
``security`` binary. (The autouse ``block_real_keychain`` guard replaces the
module's functions for *other* tests; here we patch ``subprocess`` so the real
function bodies run against a fake process.)
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import macos_keychain

# Every test here drives the *real* wrapper bodies (mocking subprocess) or runs
# against a temp keychain on CI, so opt the whole module out of the in-memory
# Keychain guard that replaces these functions for other tests.
pytestmark = pytest.mark.no_keychain_fake


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["security"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# get_password
# ---------------------------------------------------------------------------


def test_get_password_returns_value_on_rc0():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0, stdout="the-secret\n")
        assert macos_keychain.get_password("svc", "acct") == "the-secret"
        args = run.call_args.args[0]
        assert args[:2] == ["/usr/bin/security", "find-generic-password"]
        assert "-a" in args and "acct" in args and "svc" in args


def test_get_password_returns_none_only_on_rc44():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(44)
        assert macos_keychain.get_password("svc", "acct") is None


def test_get_password_raises_on_other_nonzero():
    # e.g. locked / denied / unavailable — must NOT be masked as "not found".
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(51, stderr="boom")
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.get_password("svc", "acct")


# ---------------------------------------------------------------------------
# set_password — stdin (security -i) vs argv fallback
# ---------------------------------------------------------------------------


def test_set_password_small_payload_uses_security_i_stdin():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        macos_keychain.set_password("svc", "acct", "short-secret")

        args = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        assert args == ["/usr/bin/security", "-i"]  # stdin path
        # Secret is NOT in argv; it rides in on stdin as a hex `-X` value.
        assert "short-secret" not in args
        stdin = kwargs["input"]
        assert stdin.startswith("add-generic-password -U")
        assert "-X " + "short-secret".encode().hex() in stdin
        # -a/-s are quoted in the stdin command line.
        assert '-a "acct"' in stdin and '-s "svc"' in stdin


def test_set_password_large_payload_falls_back_to_argv():
    big = "x" * macos_keychain.SECURITY_STDIN_LINE_LIMIT  # hex doubles the length
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        macos_keychain.set_password("svc", "acct", big)

        args = run.call_args.args[0]
        assert args[:3] == ["/usr/bin/security", "add-generic-password", "-U"]  # argv path
        assert "input" not in run.call_args.kwargs  # not via stdin
        # Hex value passed as a raw list element (no shell, no quoting).
        assert big.encode().hex() in args
        assert "acct" in args and "svc" in args


def test_set_password_raises_on_nonzero():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(45, stderr="nope")
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.set_password("svc", "acct", "secret")


def test_set_get_roundtrip_hex_is_decodable():
    # The hex written on set must decode back to the original UTF-8 secret.
    secret = 'token-with "quotes" and \\ backslash and é'
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _completed(0)

    with patch("claude_swap.macos_keychain.subprocess.run", side_effect=fake_run):
        macos_keychain.set_password("svc", "acct", secret)
    stdin = captured["kwargs"]["input"]
    hex_token = stdin.split("-X ", 1)[1].strip()
    assert bytes.fromhex(hex_token).decode("utf-8") == secret


# ---------------------------------------------------------------------------
# delete_password
# ---------------------------------------------------------------------------


def test_delete_password_rc0_and_rc44_are_success():
    for rc in (0, 44):
        with patch("claude_swap.macos_keychain.subprocess.run") as run:
            run.return_value = _completed(rc)
            macos_keychain.delete_password("svc", "acct")  # no raise


def test_delete_password_raises_on_other_nonzero():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(51, stderr="locked")
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.delete_password("svc", "acct")


# ---------------------------------------------------------------------------
# Real-Keychain round-trip (GitHub Actions macOS only, temp keychain)
# ---------------------------------------------------------------------------


@pytest.mark.no_keychain_fake
@pytest.mark.skipif(
    __import__("os").environ.get("GITHUB_ACTIONS") != "true"
    or __import__("sys").platform != "darwin",
    reason="Modifies a keychain — runs on GitHub Actions macOS only",
)
def test_wrapper_roundtrip_real_keychain(tmp_path):
    """set → get → delete against a real (temporary, default) keychain."""
    import os
    import subprocess as sp

    kc = str(tmp_path / "wrap.keychain")
    sp.run(["security", "create-keychain", "-p", "", kc], check=True)
    sp.run(["security", "unlock-keychain", "-p", "", kc], check=True)
    # CI runners don't reliably have a default keychain configured (rc 1,
    # "A default keychain could not be found") — and an earlier swap/restore
    # cycle in this job may have cleared it. Capture it only if present and
    # skip the restore otherwise, rather than failing setup.
    default_proc = sp.run(
        ["security", "default-keychain"], capture_output=True, text=True
    )
    orig_default = (
        default_proc.stdout.strip().strip('"')
        if default_proc.returncode == 0
        else None
    )
    list_proc = sp.run(
        ["security", "list-keychains", "-d", "user"],
        capture_output=True, text=True,
    )
    orig_list = [
        line.strip().strip('"')
        for line in (list_proc.stdout if list_proc.returncode == 0 else "").splitlines()
        if line.strip()
    ]
    try:
        sp.run(["security", "default-keychain", "-s", kc], check=True)
        sp.run(["security", "list-keychains", "-d", "user", "-s", kc], check=True)

        macos_keychain.set_password("claude-swap-test", "acct-1", "round-trip-token")
        assert macos_keychain.get_password("claude-swap-test", "acct-1") == "round-trip-token"
        macos_keychain.delete_password("claude-swap-test", "acct-1")
        assert macos_keychain.get_password("claude-swap-test", "acct-1") is None
    finally:
        # Restore the search list BEFORE the default: macOS won't report a
        # default keychain that isn't in the search list, so the reverse order
        # leaves the default dangling for whatever runs next in this job.
        if orig_list:
            sp.run(["security", "list-keychains", "-d", "user", "-s", *orig_list], check=False)
        if orig_default:
            sp.run(["security", "default-keychain", "-s", orig_default], check=False)
        sp.run(["security", "delete-keychain", kc], check=False)
