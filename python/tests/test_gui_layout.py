"""Dialogs must not open smaller than their own contents.

Six of thirteen dialogs did. The Discuss dialog opened 42px shorter than it
needed, which put its action buttons below the fold; Token Limits was clipped by
320px of width. Nobody drags a dialog bigger to look for missing buttons — they
conclude it is broken and stop.

The related fault is a label with no ``wraplength``. A long server message is
then laid out as one unbroken line, the grid column grows to fit it, and the
window's contents are pushed outside its frame. That is what a wrong password
did to the login dialog: content demanding 1,674px inside a 460px window.

Both are invisible to a syntax check and obvious to a measurement.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import pytest

from tests.conftest import DIALOG_OPENERS, close_dialog, open_dialog, walk_widgets

# A long error, of the kind the API actually returns on a bad login.
LONG_SERVER_MESSAGE = (
    "Invalid email or password. If the owner has forgotten it, they can request "
    "a reset link from the sign-in dialog at https://agentpub.org, or you can "
    "call POST /v1/auth/forgot-password with their address. Note that /settings "
    "can only change a password you already know, so it is no help here."
)


@pytest.mark.parametrize("opener", DIALOG_OPENERS)
def test_dialog_is_at_least_as_large_as_its_contents(gui_app, opener):
    """The requested geometry is a minimum, never a ceiling."""
    if not hasattr(gui_app, opener):
        pytest.skip(f"{opener} not present")

    win = open_dialog(gui_app, opener)
    if win is None:
        pytest.skip(f"{opener} opened no window")
    try:
        # _fit_to_content runs on the idle queue; give it a chance.
        gui_app.update_idletasks()
        gui_app.update()
        win.update_idletasks()

        need_w, need_h = win.winfo_reqwidth(), win.winfo_reqheight()
        have_w, have_h = win.winfo_width(), win.winfo_height()

        # The actual window size is the yardstick — minsize is deliberately
        # capped so a dialog can still be shrunk by hand, and is therefore
        # allowed to sit below the content size.
        assert have_w + 2 >= need_w, (
            f"{opener}: content wants {need_w}px wide, window is {have_w}px — "
            f"{need_w - have_w}px is off-screen"
        )
        assert have_h + 2 >= need_h, (
            f"{opener}: content wants {need_h}px tall, window is {have_h}px — "
            f"{need_h - have_h}px, likely the buttons, is below the fold"
        )
    finally:
        close_dialog(win)


@pytest.mark.parametrize("opener", DIALOG_OPENERS)
def test_every_dialog_can_be_resized(gui_app, opener):
    """The last line of defence when a layout goes wrong.

    A fixed, non-resizable dialog whose content overflows cannot be recovered by
    the user at all. The login dialog was exactly that.
    """
    if not hasattr(gui_app, opener):
        pytest.skip(f"{opener} not present")
    win = open_dialog(gui_app, opener)
    if win is None:
        pytest.skip(f"{opener} opened no window")
    try:
        rz = win.wm_resizable()
        normalised = tuple(str(v) in ("1", "True", "true") for v in rz)
        assert all(normalised), f"{opener}: not resizable ({rz})"
    finally:
        close_dialog(win)


def test_login_survives_a_long_error_message(gui_app):
    """The reported bug, pinned.

    Without a wraplength this demanded ~1,674px inside a 460px window and the
    buttons were pushed out of view.
    """
    win = open_dialog(gui_app, "_open_register")
    assert win is not None
    try:
        labels = walk_widgets(win, ttk.Label)
        wrapped = [l for l in labels if int(l.cget("wraplength") or 0) > 0]
        assert wrapped, (
            "no label in the login dialog wraps; a long server message will "
            "expand the grid and push the buttons outside the window"
        )

        for lbl in labels:
            var = lbl.cget("textvariable")
            if var:
                win.setvar(var, LONG_SERVER_MESSAGE)
        win.update_idletasks()

        need_w = win.winfo_reqwidth()
        have_w = win.winfo_width()
        assert have_w + 2 >= need_w, (
            f"a long error made the login dialog want {need_w}px inside a "
            f"{have_w}px window — {need_w - have_w}px, including buttons, off-screen"
        )
    finally:
        close_dialog(win)


def test_login_offers_a_way_out_of_a_forgotten_password(gui_app):
    """The error text points at a 'Forgot password?' control, so it must exist."""
    win = open_dialog(gui_app, "_open_register")
    assert win is not None
    try:
        labels = [b.cget("text") for b in walk_widgets(win, ttk.Button)]
        assert any("orgot" in t for t in labels), f"buttons were {labels}"
    finally:
        close_dialog(win)
