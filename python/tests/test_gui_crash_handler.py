"""Tests for the GUI startup crash handler.

A --windowed PyInstaller build has no console, so an unhandled exception during
GUI startup previously left the user with a double-clicked icon and no feedback.
These tests verify that the crash-capture path writes a usable log instead of
failing silently. They exercise the pure logging helper (no Tk window needed).
"""
from __future__ import annotations

import pathlib

import agentpub.gui as gui


def _read(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def test_write_crash_log_captures_traceback(tmp_path, monkeypatch):
    """A raised exception is written to gui-crash.log with its traceback."""
    monkeypatch.setattr(gui, "_CONFIG_DIR", tmp_path)

    try:
        raise ValueError("boom-during-startup")
    except ValueError as exc:
        log_path = gui._write_crash_log(exc, context="startup")

    assert pathlib.Path(log_path).exists()
    body = _read(log_path)
    assert "startup crash" in body
    assert "ValueError" in body
    assert "boom-during-startup" in body
    assert "Traceback" in body
    # Metadata that makes a user-submitted log actionable.
    assert "Version:" in body
    assert "Python:" in body


def test_write_crash_log_appends_not_clobbers(tmp_path, monkeypatch):
    """A second crash is appended so an earlier startup log is not lost."""
    monkeypatch.setattr(gui, "_CONFIG_DIR", tmp_path)

    gui._write_crash_log(RuntimeError("first-failure"), context="startup")
    log_path = gui._write_crash_log(RuntimeError("second-failure"), context="callback")

    body = _read(log_path)
    assert "first-failure" in body
    assert "second-failure" in body
    assert "startup crash" in body
    assert "callback crash" in body


def test_write_crash_log_creates_missing_config_dir(tmp_path, monkeypatch):
    """The log is written even when ~/.agentpub does not exist yet."""
    target = tmp_path / "does-not-exist-yet"
    monkeypatch.setattr(gui, "_CONFIG_DIR", target)

    log_path = gui._write_crash_log(KeyError("no-config"), context="startup")

    assert pathlib.Path(log_path).exists()
    assert target.is_dir()


def test_write_crash_log_never_raises(monkeypatch):
    """Logging must never itself raise, even if the path is unwritable."""
    # Point _CONFIG_DIR at something whose mkdir/open will fail, and ensure we
    # still get a string back instead of an exception propagating to the user.
    class _BadPath(pathlib.PurePosixPath):
        def mkdir(self, *a, **k):
            raise OSError("read-only filesystem")

    monkeypatch.setattr(gui, "_CONFIG_DIR", _BadPath("/nope"))

    # Should not raise.
    result = gui._write_crash_log(ValueError("x"), context="startup")
    assert isinstance(result, str)


def test_main_is_guarded(monkeypatch, tmp_path):
    """main() converts a startup failure into a crash log + SystemExit(1),
    not an unhandled traceback to a non-existent console."""
    monkeypatch.setattr(gui, "_CONFIG_DIR", tmp_path)

    def _boom():
        raise RuntimeError("init-exploded")

    # Construction fails...
    monkeypatch.setattr(gui, "AgentPubGUI", _boom)
    # ...and the fatal dialog is stubbed so the test stays headless.
    shown = {}
    monkeypatch.setattr(
        gui, "_show_fatal_dialog",
        lambda exc, path: shown.update(exc=exc, path=path),
    )

    import pytest
    with pytest.raises(SystemExit) as excinfo:
        gui.main()

    assert excinfo.value.code == 1
    assert isinstance(shown["exc"], RuntimeError)
    assert pathlib.Path(shown["path"]).exists()
    assert "init-exploded" in _read(shown["path"])
