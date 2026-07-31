"""Shared fixtures for the SDK tests.

The GUI tests here drive real Tk widgets, because the bugs they exist to catch
are only visible once a widget is laid out: a dialog that opens smaller than its
contents, a label without a wrap that pushes buttons off-screen, a button with
no callback. None of that is reachable by reading the source.

That means they need a display. On a headless machine they skip rather than
fail, so the suite stays runnable in CI without pretending it verified anything.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def tk_available() -> bool:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001 — no display, no Tk build, anything
        return False


@pytest.fixture(scope="session")
def gui_app(tk_available):
    """A built AgentPubGUI with modal dialogs stubbed out.

    Message boxes block forever with no user to dismiss them, so every
    ``messagebox`` entry point is replaced. ``askyesno``/``askokcancel`` answer
    False — the safe choice, and it means a test that accidentally triggers a
    confirmation does not silently take the destructive branch.

    Session-scoped deliberately: Tk does not tolerate many roots created and
    destroyed inside one process, and starts failing with "tk wasn't installed
    properly" — a misleading message for what is really resource exhaustion.
    One root for the whole run. Tests must therefore close any dialog they
    open (``close_dialog``) rather than relying on teardown.
    """
    if not tk_available:
        pytest.skip("no display available for Tk")

    import agentpub.gui as gui

    saved = {
        name: getattr(gui.messagebox, name)
        for name in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel")
    }
    for name in ("showinfo", "showwarning", "showerror"):
        setattr(gui.messagebox, name, lambda *a, **k: None)
    gui.messagebox.askyesno = lambda *a, **k: False
    gui.messagebox.askokcancel = lambda *a, **k: False

    app = gui.AgentPubGUI()
    # Mapped, but parked far off-screen. It has to be mapped: an unmapped
    # window never runs its idle queue, so the fit-to-content pass would not
    # fire and every layout assertion would measure a 1x1 phantom. Off-screen
    # keeps it out of the way of whoever is running the tests.
    app.geometry("1200x800+4000+4000")
    app.update()
    try:
        yield app
    finally:
        try:
            app.destroy()
        except Exception:  # noqa: BLE001
            pass
        for name, fn in saved.items():
            setattr(gui.messagebox, name, fn)


#: Every dialog the app can open, by the method that opens it. Kept in one place
#: so a new dialog is covered by all the sweeps at once — the point of these
#: tests is that they apply to the whole class, not to the instance that
#: happened to break.
DIALOG_OPENERS = [
    "_open_llm_settings",
    "_open_sources_settings",
    "_open_resources_settings",
    "_open_pipeline_config",
    "_open_token_limits_settings",
    "_open_evaluate_dialog",
    "_open_discuss_dialog",
    "_open_prompts_dialog",
    "_open_evaluator_prompt_dialog",
    "_open_library_dialog",
    "_open_about",
    "_open_register",
    "_open_docs",
]


def open_dialog(app, opener: str):
    """Open one dialog and return its Toplevel, or None if it made no window."""
    import tkinter as tk

    before = set(app.winfo_children())
    getattr(app, opener)()
    # Let the dialog map and its idle callbacks (notably the fit-to-content
    # pass) actually run before anything is measured.
    for _ in range(3):
        app.update()
    new = [w for w in set(app.winfo_children()) - before if isinstance(w, tk.Toplevel)]
    if not new:
        return None
    win = new[0]
    try:
        win.geometry("+4200+4200")
        win.update()
    except tk.TclError:
        pass
    return win


def close_dialog(win) -> None:
    import tkinter as tk

    try:
        win.grab_release()
        win.destroy()
    except tk.TclError:
        pass


def walk_widgets(root, kind):
    """Every descendant of *root* that is an instance of *kind*."""
    found = []

    def _walk(w):
        for child in w.winfo_children():
            if isinstance(child, kind):
                found.append(child)
            _walk(child)

    _walk(root)
    return found
