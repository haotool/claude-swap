"""Tests for the printer module."""

from __future__ import annotations

import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from claude_swap import printer


@pytest.fixture(autouse=True)
def _reset_color_cache():
    """Reset the color detection cache before each test."""
    printer._colors_enabled = None
    yield
    printer._colors_enabled = None


class TestColorDetection:
    """Tests for color support detection."""

    def test_no_color_env_disables(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        assert printer._detect_color_support() is False

    def test_no_color_empty_value_disables(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "")
        assert printer._detect_color_support() is False

    def test_force_color_enables(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert printer._detect_color_support() is True

    def test_non_tty_disables(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.setattr(sys, "stdout", StringIO())
        assert printer._detect_color_support() is False

    def test_dumb_term_disables(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.setenv("TERM", "dumb")
        # Need a fake tty
        fake_stdout = StringIO()
        fake_stdout.isatty = lambda: True  # type: ignore[attr-defined]
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        if sys.platform != "win32":
            assert printer._detect_color_support() is False

    def test_colors_enabled_caches(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert printer.colors_enabled() is True
        # Even after removing FORCE_COLOR, cached value persists
        monkeypatch.delenv("FORCE_COLOR")
        assert printer.colors_enabled() is True


def _fake_ctypes(*, get_mode_rc: int, set_mode_rc: int) -> tuple[object, MagicMock]:
    """In-memory kernel32 so the VT-enable branch runs on any test host."""
    kernel32 = MagicMock()
    kernel32.GetStdHandle.return_value = 7
    kernel32.GetConsoleMode.return_value = get_mode_rc
    kernel32.SetConsoleMode.return_value = set_mode_rc
    fake = SimpleNamespace(
        windll=SimpleNamespace(kernel32=kernel32),
        c_ulong=lambda: SimpleNamespace(value=0),
        byref=lambda obj: obj,
    )
    return fake, kernel32


class TestEnableWindowsVt:
    """SetConsoleMode can refuse VT (legacy conhost); that must read False."""

    def test_true_when_console_accepts_vt(self, monkeypatch):
        fake, kernel32 = _fake_ctypes(get_mode_rc=1, set_mode_rc=1)
        monkeypatch.setattr(printer.sys, "platform", "win32")
        monkeypatch.setattr(printer, "ctypes", fake)
        assert printer._enable_windows_vt() is True
        kernel32.SetConsoleMode.assert_called_once()

    def test_false_when_set_console_mode_fails(self, monkeypatch):
        # Pretending success here made every style call emit bare escape
        # codes on consoles without VT support.
        fake, _ = _fake_ctypes(get_mode_rc=1, set_mode_rc=0)
        monkeypatch.setattr(printer.sys, "platform", "win32")
        monkeypatch.setattr(printer, "ctypes", fake)
        assert printer._enable_windows_vt() is False

    def test_false_when_get_console_mode_fails(self, monkeypatch):
        fake, kernel32 = _fake_ctypes(get_mode_rc=0, set_mode_rc=1)
        monkeypatch.setattr(printer.sys, "platform", "win32")
        monkeypatch.setattr(printer, "ctypes", fake)
        assert printer._enable_windows_vt() is False
        # A failed read means the mode value is garbage; it must never be
        # written back or the console loses its existing flags.
        kernel32.SetConsoleMode.assert_not_called()

    def test_true_on_non_windows(self, monkeypatch):
        monkeypatch.setattr(printer.sys, "platform", "linux")
        assert printer._enable_windows_vt() is True


class TestStyling:
    """Tests for styling functions."""

    def test_style_with_colors_disabled(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        assert printer.accent("hello") == "hello"
        assert printer.muted("hello") == "hello"
        assert printer.dimmed("hello") == "hello"
        assert printer.bolded("hello") == "hello"
        assert printer.bold_accent("hello") == "hello"

    def test_style_with_colors_enabled(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        result = printer.accent("hello")
        assert "hello" in result
        assert "\033[38;5;173m" in result
        assert "\033[0m" in result

    def test_muted_with_colors_enabled(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        result = printer.muted("org name")
        assert "\033[38;5;250m" in result
        assert "org name" in result

    def test_dimmed_with_colors_enabled(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        result = printer.dimmed("secondary")
        assert "\033[2m" in result
        assert "secondary" in result

    def test_bolded_with_colors_enabled(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        result = printer.bolded("header")
        assert "\033[1m" in result
        assert "header" in result

    def test_bold_accent_with_colors_enabled(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        result = printer.bold_accent("(active)")
        assert "\033[1m" in result
        assert "\033[38;5;173m" in result
        assert "(active)" in result


class TestLinePrinters:
    """Tests for line-level print functions."""

    def test_error_prints_to_stderr(self, monkeypatch, capsys):
        monkeypatch.setenv("NO_COLOR", "1")
        printer.error("something failed")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "something failed" in captured.err

    def test_error_with_color(self, monkeypatch, capsys):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        printer.error("something failed")
        captured = capsys.readouterr()
        assert "\033[31m" in captured.err

    def test_warning_prints_to_stdout(self, monkeypatch, capsys):
        monkeypatch.setenv("NO_COLOR", "1")
        printer.warning("be careful")
        captured = capsys.readouterr()
        assert "be careful" in captured.out

    def test_warning_with_color(self, monkeypatch, capsys):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        printer.warning("be careful")
        captured = capsys.readouterr()
        assert "\033[33m" in captured.out


def test_force_color_overrides_and_restores():
    from claude_swap import printer
    saved = printer._colors_enabled
    try:
        printer._colors_enabled = False
        with printer.force_color():
            assert printer.colors_enabled() is True
            assert printer.accent("X") == "\x1b[38;5;173mX\x1b[0m"
        assert printer._colors_enabled is False
    finally:
        printer._colors_enabled = saved
