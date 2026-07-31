"""Controls must do something, and failures must be visible.

Two sweeps that a syntax check cannot perform:

* every ``command=`` points at a callable that exists — a renamed or deleted
  method leaves a button that raises AttributeError on click, and nothing in
  the source flags it;
* every dialog has buttons, and every button has a callback bound — a control
  that silently does nothing is indistinguishable from a frozen app.

The third test guards the mechanism that let a month-long outage go unnoticed:
an exception handler that does nothing at all. Those are fine for cosmetic
guards (a font that will not apply, a link that will not open) and not fine
anywhere the user is waiting for a result.
"""

from __future__ import annotations

import re
from pathlib import Path
from tkinter import ttk

import pytest

import agentpub.gui as gui

from tests.conftest import DIALOG_OPENERS, close_dialog, open_dialog, walk_widgets

SOURCE = Path(gui.__file__).read_text(encoding="utf-8")


def test_every_command_target_exists():
    """``command=self.X`` where X was renamed leaves a button that raises."""
    targets = sorted(set(re.findall(r"command=self\.(\w+)", SOURCE)))
    assert targets, "no command targets found — the pattern must have changed"

    # A target may live on the main window or on one of the custom widget
    # classes in this module, so check them all.
    classes = [
        obj for obj in vars(gui).values()
        if isinstance(obj, type) and obj.__module__ == gui.__name__
    ]
    missing = [t for t in targets if not any(hasattr(c, t) for c in classes)]
    assert not missing, f"buttons point at methods that do not exist: {missing}"


def test_every_menu_command_exists():
    menus = re.findall(r"add_command\(\s*label=\"([^\"]+)\",\s*command=self\.(\w+)", SOURCE)
    assert menus, "no menu commands found — the pattern must have changed"
    broken = [(label, fn) for label, fn in menus if not hasattr(gui.AgentPubGUI, fn)]
    assert not broken, f"menu entries point at missing methods: {broken}"


@pytest.mark.parametrize("opener", DIALOG_OPENERS)
def test_dialog_buttons_are_bound(gui_app, opener):
    """A button with no callback looks identical to a hung application."""
    if not hasattr(gui_app, opener):
        pytest.skip(f"{opener} not present")
    win = open_dialog(gui_app, opener)
    if win is None:
        pytest.skip(f"{opener} opened no window")
    try:
        buttons = walk_widgets(win, ttk.Button)
        assert buttons, f"{opener}: dialog has no buttons at all — no way to act or dismiss"
        dead = [b.cget("text") for b in buttons if not str(b.cget("command")).strip()]
        assert not dead, f"{opener}: buttons with no command bound: {dead}"
    finally:
        close_dialog(win)


def test_no_new_silent_exception_handlers():
    """A budget, not a ban.

    ``except ...: pass`` is legitimate for cosmetic guards and wrong anywhere a
    user is waiting for something. Twenty-one existed when this was written and
    one of them was hiding a real bug — settings silently discarded. The budget
    exists so the count cannot quietly grow; lower it when you remove one.
    """
    lines = SOURCE.split("\n")
    silent = [
        i + 1
        for i, line in enumerate(lines)
        if re.match(r"\s*except\b.*:\s*$", line)
        and i + 1 < len(lines)
        and lines[i + 1].strip() == "pass"
    ]
    budget = 21
    assert len(silent) <= budget, (
        f"{len(silent)} handlers swallow their exception silently (budget {budget}). "
        f"New ones at lines {silent[budget:]}. If a user is waiting on that code, "
        f"report the failure instead — see _explain_error()."
    )


def test_user_facing_errors_go_through_the_translator():
    """Raw exception text in a message box or log line is not a sentence.

    ``_explain_error``/``_error_line`` exist so a failure reads as something a
    person can act on. This checks the raw-interpolation habit does not creep
    back into the paths the user actually reads.
    """
    offenders = []
    for i, line in enumerate(SOURCE.split("\n"), 1):
        if re.search(r"(showerror|showwarning|_log)\(f?\"[^\"]*\{(e|exc|err)\}", line):
            if "_error_line" not in line and "_explain_error" not in line:
                offenders.append(i)
    assert not offenders, (
        f"raw exception text shown to the user at lines {offenders} — "
        f"wrap it in _error_line()"
    )
