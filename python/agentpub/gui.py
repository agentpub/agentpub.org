"""AgentPub Desktop GUI — tkinter single-window app for daemon configuration and control."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import queue
import random
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

# Set Windows taskbar app ID so it shows "AgentPub" instead of "Python"
import sys
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("agentpub.desktop")
    except Exception:
        pass

from agentpub.cli import (
    _CONFIG_DIR,
    _PROVIDERS,
    _load_config,
    _load_env_file,
    _save_config,
    _save_env_var,
)

logger = logging.getLogger(__name__)

_CHALLENGES_FILE = _CONFIG_DIR / "challenges.json"

# ---------------------------------------------------------------------------
# Optional theme imports
# ---------------------------------------------------------------------------

_HAS_SV_TTK = False
_IS_DARK = False

try:
    import sv_ttk  # type: ignore[import-untyped]
    _HAS_SV_TTK = True
except ImportError:
    pass

try:
    import darkdetect  # type: ignore[import-untyped]
except ImportError:
    darkdetect = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Font constants
# ---------------------------------------------------------------------------

_FONT_FAMILY = "Segoe UI"
_FONT_MONO = "Cascadia Code"

_DEFAULT_FONT_SIZE = 10

_FONT = (_FONT_FAMILY, _DEFAULT_FONT_SIZE)
_FONT_BOLD = (_FONT_FAMILY, _DEFAULT_FONT_SIZE + 1, "bold")
_FONT_SMALL = (_FONT_FAMILY, _DEFAULT_FONT_SIZE - 1)
_FONT_MONO_NORMAL = (_FONT_MONO, _DEFAULT_FONT_SIZE)
_FONT_MONO_SMALL = (_FONT_MONO, _DEFAULT_FONT_SIZE - 1)

# ---------------------------------------------------------------------------
# Theme colour helpers
# ---------------------------------------------------------------------------

_DARK_BG = "#1e1e1e"
_DARK_FG = "#dcdcdc"
_DARK_SELECT = "#264f78"
_DARK_INSERT = "#dcdcdc"
_DARK_FIELD = "#2d2d2d"

_LIGHT_BG = "#ffffff"
_LIGHT_FG = "#1c1c1c"
_LIGHT_SELECT = "#0078d4"
_LIGHT_INSERT = "#1c1c1c"
_LIGHT_FIELD = "#ffffff"


def _theme_scrolled_text(widget: tk.Text | scrolledtext.ScrolledText) -> None:
    """Apply dark/light colours to a Text or ScrolledText widget."""
    if _IS_DARK:
        widget.configure(
            bg=_DARK_FIELD, fg=_DARK_FG,
            insertbackground=_DARK_INSERT, selectbackground=_DARK_SELECT,
            relief=tk.FLAT,
        )
    else:
        widget.configure(
            bg=_LIGHT_FIELD, fg=_LIGHT_FG,
            insertbackground=_LIGHT_INSERT, selectbackground=_LIGHT_SELECT,
            relief=tk.FLAT,
        )


# ---------------------------------------------------------------------------
# Log capture — routes logging records to a queue for GUI consumption
# ---------------------------------------------------------------------------


class QueueLogHandler(logging.Handler):
    """Logging handler that puts formatted records into a queue."""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    _NOISY_LOGGERS = ("httpx", "urllib3", "hpack", "asyncio")

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(self._NOISY_LOGGERS):
            return
        try:
            msg = self.format(record)
            self.log_queue.put(msg)
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# TkDisplay — bridges the researcher display interface to tkinter widgets
# ---------------------------------------------------------------------------


class TkDisplay:
    """Display adapter that sends research pipeline updates to tkinter via a queue.

    Implements the same interface as NullDisplay / ResearchDisplay so it can
    be passed directly to PlaybookResearcher(display=...).  All methods are
    called from the daemon thread; they push dicts onto a queue that the
    main tkinter thread drains via after().
    """

    def __init__(self, display_queue: queue.Queue):
        self._q = display_queue

    def _put(self, kind: str, **data) -> None:
        self._q.put({"kind": kind, **data})

    def start(self) -> None:
        self._put("start")

    def stop(self) -> None:
        self._put("stop")

    def phase_start(self, phase_num: int, name: str | None = None) -> None:
        self._put("phase_start", phase=phase_num, name=name or "")

    def phase_done(self, phase_num: int) -> None:
        self._put("phase_done", phase=phase_num)

    def step(self, message: str) -> None:
        self._put("step", message=message)

    def section_start(self, name: str) -> None:
        self._put("section_start", name=name)

    def section_done(self, name: str, content: str = "") -> None:
        self._put("section_done", name=name, content=content)

    def set_title(self, text: str) -> None:
        self._put("set_title", text=text)

    def set_abstract(self, text: str) -> None:
        self._put("set_abstract", text=text)

    def tick(self) -> None:
        self._put("tick")

    def set_context(self, *, topic: str = "", provider: str = "", model: str = "", api_status: str = "") -> None:
        self._put("set_context", topic=topic, provider=provider, model=model, api_status=api_status)

    def add_reference(self, index: int, authors: str = "", year: str = "", title: str = "", url: str = "", doi: str = "") -> None:
        self._put("add_reference", index=index, authors=authors, year=year, title=title, url=url, doi=doi)

    def complete(self, message: str = "") -> None:
        self._put("complete", message=message)

    def stream_token(self, text: str, thinking: bool = False) -> None:
        # Throttle: skip individual tokens, only forward newlines
        if "\n" in text:
            self._put("stream_token", text=text, thinking=thinking)

    def update_tokens(self, input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0) -> None:
        self._put("update_tokens", input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)

    def heartbeat(self, elapsed: float, words: int, is_thinking: bool) -> None:
        mins, secs = divmod(int(elapsed), 60)
        status = "thinking" if is_thinking else f"{words} words"
        self._put("step", message=f"  ⏱ {mins}:{secs:02d} — {status}")

    def set_outline(self, outline: dict) -> None:
        self._put("set_outline", outline=outline)


# Phase names (matches display.py — all 10 phases)
_PHASE_NAMES = {
    1: "Question & Scope",
    2: "Outline & Thesis",
    3: "Search & Collect",
    4: "Read & Annotate",
    5: "Revise Outline",
    6: "Analyze & Discover",
    7: "Draft",
    8: "Revise & Verify",
    9: "Adversarial Review",
    10: "Submit",
}


# ---------------------------------------------------------------------------
# SearchableDropdown — replaces Combobox for long lists
# ---------------------------------------------------------------------------


class SearchableDropdown(ttk.Frame):
    """A button that opens a searchable popup with a scrollable listbox."""

    def __init__(self, parent: tk.Widget, **kwargs):
        super().__init__(parent)
        self._values: list[str] = []
        self._current_index: int = -1
        self._var = tk.StringVar(value="(select challenge)")

        self._btn = ttk.Button(self, textvariable=self._var, command=self._open_popup)
        self._btn.pack(fill=tk.X, expand=True)

        self._popup: tk.Toplevel | None = None
        self._on_select_callback = None

    def bind_select(self, callback) -> None:
        """Register a callback when an item is selected."""
        self._on_select_callback = callback

    def set_values(self, values: list[str]) -> None:
        self._values = list(values)

    def current(self) -> int:
        return self._current_index

    def get(self) -> str:
        return self._var.get()

    def set(self, text: str) -> None:
        self._var.set(text)

    def _open_popup(self) -> None:
        if self._popup and self._popup.winfo_exists():
            self._popup.destroy()
            self._popup = None
            return

        if not self._values:
            return

        popup = tk.Toplevel(self)
        popup.wm_overrideredirect(True)
        popup.wm_attributes("-topmost", True)
        self._popup = popup

        # Position below button
        x = self._btn.winfo_rootx()
        y = self._btn.winfo_rooty() + self._btn.winfo_height()
        w = max(self._btn.winfo_width(), 400)
        popup.wm_geometry(f"{w}x320+{x}+{y}")

        # Container
        container = ttk.Frame(popup, padding=2)
        container.pack(fill=tk.BOTH, expand=True)

        # Search entry
        search_var = tk.StringVar()
        search_entry = ttk.Entry(container, textvariable=search_var, font=_FONT)
        search_entry.pack(fill=tk.X, padx=2, pady=(2, 4))
        search_entry.focus_set()

        # Listbox with scrollbar
        lb_frame = ttk.Frame(container)
        lb_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))

        scrollbar = ttk.Scrollbar(lb_frame, orient=tk.VERTICAL)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        listbox = tk.Listbox(
            lb_frame, font=_FONT, yscrollcommand=scrollbar.set,
            activestyle="dotbox", selectmode=tk.SINGLE,
        )
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=listbox.yview)

        # Theme listbox colours
        if _IS_DARK:
            listbox.configure(bg=_DARK_FIELD, fg=_DARK_FG, selectbackground=_DARK_SELECT)
            search_entry_widget = search_entry
        else:
            listbox.configure(bg=_LIGHT_FIELD, fg=_LIGHT_FG, selectbackground=_LIGHT_SELECT)

        # Populate
        def _filter(*_args):
            term = search_var.get().lower()
            listbox.delete(0, tk.END)
            for val in self._values:
                if term in val.lower():
                    listbox.insert(tk.END, val)
            if listbox.size() > 0:
                listbox.selection_set(0)

        search_var.trace_add("write", _filter)
        _filter()  # initial fill

        def _select(_event=None):
            sel = listbox.curselection()
            if not sel:
                return
            text = listbox.get(sel[0])
            # Find original index
            try:
                self._current_index = self._values.index(text)
            except ValueError:
                self._current_index = -1
            self._var.set(text)
            popup.destroy()
            self._popup = None
            if self._on_select_callback:
                self._on_select_callback()

        listbox.bind("<ButtonRelease-1>", _select)
        listbox.bind("<Return>", _select)

        def _dismiss(_event=None):
            popup.destroy()
            self._popup = None

        popup.bind("<Escape>", _dismiss)
        search_entry.bind("<Escape>", _dismiss)

        # Close on focus loss
        def _focus_out(event):
            # Check if focus went to another widget within the popup
            try:
                if event.widget.winfo_toplevel() != popup:
                    _dismiss()
            except tk.TclError:
                pass

        popup.bind("<FocusOut>", _focus_out)

        # Allow arrow keys to move listbox selection from search entry
        def _key_down(_event):
            if listbox.size() > 0:
                cur = listbox.curselection()
                idx = cur[0] if cur else -1
                if idx < listbox.size() - 1:
                    listbox.selection_clear(0, tk.END)
                    listbox.selection_set(idx + 1)
                    listbox.see(idx + 1)

        def _key_up(_event):
            if listbox.size() > 0:
                cur = listbox.curselection()
                idx = cur[0] if cur else 1
                if idx > 0:
                    listbox.selection_clear(0, tk.END)
                    listbox.selection_set(idx - 1)
                    listbox.see(idx - 1)

        search_entry.bind("<Down>", _key_down)
        search_entry.bind("<Up>", _key_up)
        search_entry.bind("<Return>", _select)


# ---------------------------------------------------------------------------
# Main GUI
# ---------------------------------------------------------------------------


class AgentPubGUI(tk.Tk):
    """Desktop window for configuring and running the AgentPub daemon."""

    def __init__(self) -> None:
        super().__init__()

        # Detect and apply theme BEFORE building UI
        global _IS_DARK
        if darkdetect is not None:
            try:
                _IS_DARK = darkdetect.theme() == "Dark"
            except Exception:
                _IS_DARK = False
        if _HAS_SV_TTK:
            sv_ttk.set_theme("dark" if _IS_DARK else "light")

        # Combobox listbox colours (must be set before widget creation)
        if _IS_DARK:
            self.option_add("*TCombobox*Listbox.background", _DARK_FIELD)
            self.option_add("*TCombobox*Listbox.foreground", _DARK_FG)
            self.option_add("*TCombobox*Listbox.selectBackground", _DARK_SELECT)
        else:
            self.option_add("*TCombobox*Listbox.background", _LIGHT_FIELD)
            self.option_add("*TCombobox*Listbox.foreground", _LIGHT_FG)
            self.option_add("*TCombobox*Listbox.selectBackground", _LIGHT_SELECT)

        from agentpub import __version__
        self._version = __version__
        self.title(f"AgentPub Desktop v{__version__}")
        self.geometry("1020x800")
        self.minsize(900, 720)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Configure custom ttk styles
        self._configure_custom_styles()

        # Daemon state
        self._daemon = None
        self._daemon_thread: threading.Thread | None = None
        self._running = False
        self._exit_after_stop = False
        self._client = None
        self._stats_poll_counter = 0

        # Challenge state
        self._challenges: list[dict] = []
        self._selected_challenge_id: str | None = None

        # Queues
        self._log_queue: queue.Queue = queue.Queue()
        self._display_queue: queue.Queue = queue.Queue()

        # Font size (user-adjustable)
        self._font_size = _DEFAULT_FONT_SIZE

        # Display state (updated from display queue)
        self._phases: dict[int, dict] = {}
        for num, name in _PHASE_NAMES.items():
            self._phases[num] = {"name": name, "status": "pending", "steps": []}
        self._references: list[dict] = []
        self._paper_title: str = ""
        self._paper_abstract: str = ""
        self._paper_sections: dict[str, str] = {}
        self._paper_outline: dict = {}
        self._paper_completed: bool = False
        self._token_in: int = 0
        self._token_out: int = 0
        self._token_total: int = 0

        # Load persisted state and inject API keys into os.environ
        self._config = _load_config()
        self._env = _load_env_file()
        for k, v in self._env.items():
            if k not in os.environ and v:
                os.environ[k] = v

        # Initialize local paper library
        try:
            from agentpub.library import PaperLibrary
            self._paper_library = PaperLibrary()
            self._paper_library.ensure_dir()
        except Exception:
            self._paper_library = None

        # Create API client from saved session token (if logged in)
        _saved_key = self._config.get("api_key", "")
        if _saved_key:
            try:
                from agentpub.client import AgentPub
                self._client = AgentPub(api_key=_saved_key, base_url=os.environ.get("AA_BASE_URL"))
            except Exception:
                pass

        self._build_ui()
        self._load_state()
        self._load_challenges()
        self._refresh_agent_status()
        self._poll_queues()

        # Font zoom keyboard shortcuts
        self.bind("<Control-equal>", lambda _e: self._zoom_font(1))
        self.bind("<Control-plus>", lambda _e: self._zoom_font(1))
        self.bind("<Control-minus>", lambda _e: self._zoom_font(-1))

    # ------------------------------------------------------------------
    # Custom ttk styles
    # ------------------------------------------------------------------

    def _configure_custom_styles(self) -> None:
        style = ttk.Style()
        # Danger button (red text for STOP)
        if _IS_DARK:
            style.configure("Danger.TButton", foreground="#ff6b6b")
        else:
            style.configure("Danger.TButton", foreground="#cc0000")

    # ------------------------------------------------------------------
    # Menu bar
    # ------------------------------------------------------------------

    def _build_menu_bar(self) -> None:
        """Create the application menu bar."""
        menubar = tk.Menu(self)
        self._menubar = menubar
        self.configure(menu=menubar)

        # ── File menu ──
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Quit", accelerator="Ctrl+Q", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        # ── Settings menu ──
        settings_menu = tk.Menu(menubar, tearoff=0)
        settings_menu.add_command(label="LLM Configuration...", command=self._open_llm_settings)
        settings_menu.add_command(label="Academic Sources...", command=self._open_sources_settings)
        settings_menu.add_command(label="Token & Word Limits...", command=self._open_token_limits_settings)
        settings_menu.add_command(label="Pipeline Config...", command=self._open_pipeline_config)
        settings_menu.add_separator()
        settings_menu.add_command(label="Resource Limits...", command=self._open_resources_settings)
        menubar.add_cascade(label="Settings", menu=settings_menu)

        # ── Account menu ──
        account_menu = tk.Menu(menubar, tearoff=0)
        account_menu.add_command(label="Login...", command=self._menu_login)
        account_menu.add_command(label="Logout", command=self._menu_logout)
        account_menu.add_separator()
        account_menu.add_command(label="Rename Agent...", command=self._menu_rename)
        menubar.add_cascade(label="Account", menu=account_menu)

        # ── Tools menu ──
        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Evaluate Paper...", command=self._open_evaluate_dialog)
        tools_menu.add_command(label="Discuss Paper...", command=self._open_discuss_dialog)
        tools_menu.add_command(label="My Library...", command=self._open_library_dialog)
        tools_menu.add_separator()
        tools_menu.add_command(label="Writing Prompts...", command=self._open_prompts_dialog)
        tools_menu.add_command(label="Evaluator Prompt...", command=self._open_evaluator_prompt_dialog)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        # ── View menu ──
        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="Increase Font Size", accelerator="Ctrl++", command=lambda: self._zoom_font(1))
        view_menu.add_command(label="Decrease Font Size", accelerator="Ctrl+-", command=lambda: self._zoom_font(-1))
        if _HAS_SV_TTK:
            view_menu.add_separator()
            view_menu.add_command(label="Toggle Dark/Light Theme", command=self._toggle_theme)
        menubar.add_cascade(label="View", menu=view_menu)

        # ── Help menu ──
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Quick Guide", command=self._open_docs)
        help_menu.add_command(label="About AgentPub", command=self._open_about)
        menubar.add_cascade(label="Help", menu=help_menu)

    # ------------------------------------------------------------------
    # Menu: Account handlers
    # ------------------------------------------------------------------

    def _menu_login(self) -> None:
        """Account > Login — delegates to existing login dialog."""
        self._open_register()

    def _menu_logout(self) -> None:
        """Account > Logout — delegates to existing logout logic."""
        self._do_logout()

    def _menu_rename(self) -> None:
        """Account > Rename Agent — delegates to existing rename logic."""
        self._rename_agent()

    # ------------------------------------------------------------------
    # Summary bar (compact LLM + account info)
    # ------------------------------------------------------------------

    def _build_summary_bar(self) -> None:
        """Build a compact summary row: LLM provider/model + account name."""
        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill=tk.X)

        # LLM vars (must be created before _load_state)
        self._provider_var = tk.StringVar()
        self._model_var = tk.StringVar()
        self._llm_key_var = tk.StringVar()
        self._s2_key_var = tk.StringVar()
        self._serper_key_var = tk.StringVar()
        self._api_key_var = tk.StringVar()  # hidden, holds session token
        self._api_key_show = False
        self._account_status_var = tk.StringVar(value="Not logged in")

        # LLM summary label
        ttk.Label(bar, text="LLM:", font=_FONT).pack(side=tk.LEFT)
        self._llm_summary_var = tk.StringVar(value="(not configured)")
        ttk.Label(bar, textvariable=self._llm_summary_var, font=_FONT_BOLD).pack(side=tk.LEFT, padx=(4, 16))

        # Account summary label
        ttk.Label(bar, text="Account:", font=_FONT).pack(side=tk.LEFT)
        ttk.Label(bar, textvariable=self._account_status_var, font=_FONT_BOLD).pack(side=tk.LEFT, padx=(4, 0))

        # Keep provider/model in sync with summary
        def _update_summary(*_args):
            provider = self._provider_var.get() or "?"
            model = self._model_var.get() or "?"
            self._llm_summary_var.set(f"{provider} / {model}")
        self._provider_var.trace_add("write", _update_summary)
        self._model_var.trace_add("write", _update_summary)

    # ------------------------------------------------------------------
    # Run-time model selector (in daemon frame)
    # ------------------------------------------------------------------

    def _refresh_run_provider_list(self) -> None:
        """Refresh the provider/model dropdowns with only enabled providers."""
        self._config = _load_config()  # always re-read from disk
        enabled = set(self._config.get("enabled_providers", []))
        # If nothing explicitly enabled, fall back to providers that have an API key set
        if not enabled:
            for p in _PROVIDERS:
                env_var = p.get("env_var", "")
                if env_var and (self._env.get(env_var, "") or os.environ.get(env_var, "")):
                    enabled.add(p["name"])

        available = [p for p in _PROVIDERS if p["name"] in enabled]
        names = [p["name"] for p in available]

        if not hasattr(self, "_run_provider_combo"):
            return

        if not names:
            # No providers enabled — show placeholder
            self._run_provider_combo["values"] = ["(no providers enabled)"]
            self._run_provider_var.set("(no providers enabled)")
            self._run_model_combo["values"] = []
            self._run_model_var.set("")
            return

        self._run_provider_combo["values"] = names

        # Restore previous selection if still available
        current = self._run_provider_var.get()
        if current not in names:
            # Fall back to the legacy _provider_var or first available
            legacy = self._provider_var.get()
            if legacy in names:
                self._run_provider_var.set(legacy)
            elif names:
                self._run_provider_var.set(names[0])
            else:
                self._run_provider_var.set("")

        self._on_run_provider_change()

    def _on_run_provider_change(self, _event=None) -> None:
        """When the run provider dropdown changes, update the model dropdown."""
        pname = self._run_provider_var.get()
        provider = next((p for p in _PROVIDERS if p["name"] == pname), None)
        if not provider:
            self._run_model_combo["values"] = []
            self._run_model_var.set("")
            return

        self._run_model_combo["values"] = provider["models"]

        # Restore saved model or use default
        saved_model = self._config.get("last_model", "")
        if saved_model in provider["models"]:
            self._run_model_var.set(saved_model)
        elif not self._run_model_var.get() or self._run_model_var.get() not in provider["models"]:
            self._run_model_var.set(provider["default_model"])

        # Sync to legacy vars used by _start_daemon and summary bar
        self._provider_var.set(pname)
        self._model_var.set(self._run_model_var.get())

        # Load the API key for this provider
        env_var = provider.get("env_var", "")
        if env_var:
            key = self._env.get(env_var, "") or os.environ.get(env_var, "")
            self._llm_key_var.set(key)

    def _on_run_model_change(self, _event=None) -> None:
        """Sync run model selection to legacy model var and summary bar."""
        self._model_var.set(self._run_model_var.get())

    # ------------------------------------------------------------------
    # LLM Configuration dialog (Settings > LLM Configuration)
    # ------------------------------------------------------------------

    def _open_llm_settings(self) -> None:
        """Open a dialog showing all LLM providers with API key and enable/disable toggle."""
        dialog = tk.Toplevel(self)
        dialog.title("LLM Providers")
        dialog.geometry("620x440")
        dialog.transient(self)
        dialog.grab_set()

        f = ttk.Frame(dialog, padding=12)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="LLM Providers", font=_FONT_BOLD).pack(anchor=tk.W, pady=(0, 2))
        ttk.Label(
            f,
            text="Enable providers and enter API keys. "
            "Enabled providers with a valid key can be used for writing papers.",
            font=_FONT_SMALL, wraplength=580,
        ).pack(anchor=tk.W, pady=(0, 10))

        # Header
        hdr = ttk.Frame(f)
        hdr.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(hdr, text="Provider", font=_FONT_BOLD, width=20, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Label(hdr, text="API Key", font=_FONT_SMALL, anchor=tk.W, foreground="gray").pack(side=tk.LEFT, padx=(4, 0))

        ttk.Separator(f, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, 4))

        # Provider rows
        key_entries: dict[str, tuple[tk.StringVar, tk.BooleanVar]] = {}

        hints = {
            "openai": "platform.openai.com/api-keys",
            "anthropic": "console.anthropic.com/settings/keys",
            "google": "aistudio.google.com/apikey",
        }

        # Load saved enabled providers from config
        saved_enabled = set(self._config.get("enabled_providers", []))
        # If nothing saved yet, default to just the active provider
        if not saved_enabled:
            saved_enabled = {self._provider_var.get()} if self._provider_var.get() else set()

        for p in _PROVIDERS:
            pname = p["name"]
            pkey = p["key"]
            env_var = p.get("env_var", "")

            row = ttk.Frame(f)
            row.pack(fill=tk.X, pady=3)

            # Enable toggle — restore from saved state
            enabled_var = tk.BooleanVar(value=(pname in saved_enabled))
            ttk.Checkbutton(row, text=pname, variable=enabled_var, width=20).pack(side=tk.LEFT)

            if p.get("needs_key", True) and env_var:
                existing_key = self._env.get(env_var, "") or os.environ.get(env_var, "")
                key_var = tk.StringVar(value=existing_key)
                ke = ttk.Entry(row, textvariable=key_var, show="*", width=40, font=_FONT_SMALL)
                ke.pack(side=tk.LEFT, padx=(4, 0))

                show_var = tk.BooleanVar(value=False)
                def _make_toggle(entry=ke, sv=show_var):
                    def _toggle():
                        entry.configure(show="" if sv.get() else "*")
                    return _toggle
                ttk.Checkbutton(row, text="Show", variable=show_var, command=_make_toggle(), width=5).pack(side=tk.LEFT, padx=(4, 0))

                hint = hints.get(pkey, "")
                if hint:
                    ttk.Label(row, text=hint, font=_FONT_SMALL, foreground="gray").pack(side=tk.LEFT, padx=(8, 0))

                key_entries[pname] = (key_var, enabled_var)
            else:
                ttk.Label(row, text="No API key required (local)", font=_FONT_SMALL, foreground="gray").pack(side=tk.LEFT, padx=(4, 0))
                key_entries[pname] = (tk.StringVar(), enabled_var)

        # ── LLM Timeout setting ──
        ttk.Separator(f, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(10, 6))
        timeout_row = ttk.Frame(f)
        timeout_row.pack(fill=tk.X, pady=2)

        saved_timeout = self._config.get("llm_timeout", 600)
        timeout_var = tk.IntVar(value=saved_timeout)

        ttk.Label(timeout_row, text="Request Timeout:", font=_FONT).pack(side=tk.LEFT)
        timeout_spin = ttk.Spinbox(
            timeout_row, from_=120, to=1800, increment=60,
            textvariable=timeout_var, width=6, font=_FONT,
        )
        timeout_spin.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(timeout_row, text="seconds", font=_FONT_SMALL).pack(side=tk.LEFT)
        ttk.Label(
            timeout_row, text="(120–1800s. Higher = more patient for slow models)",
            font=_FONT_SMALL, foreground="gray",
        ).pack(side=tk.LEFT, padx=(8, 0))

        # Buttons
        btn_row = ttk.Frame(f)
        btn_row.pack(pady=(16, 0))

        def _save_and_close():
            # Save all keys
            for p in _PROVIDERS:
                pname = p["name"]
                ev = p.get("env_var", "")
                if ev and pname in key_entries:
                    val = key_entries[pname][0].get().strip()
                    if val:
                        _save_env_var(ev, val)
                        self._env[ev] = val

            # Save which providers are enabled
            enabled_list = [
                p["name"] for p in _PROVIDERS
                if p["name"] in key_entries and key_entries[p["name"]][1].get()
            ]

            # Clamp timeout to valid range
            tout = max(120, min(1800, timeout_var.get()))
            _save_config({"enabled_providers": enabled_list, "llm_timeout": tout})
            self._config = _load_config()  # reload so _refresh picks up changes

            # Refresh the run-time model selector in daemon frame
            self._refresh_run_provider_list()

            self._save_state()
            dialog.destroy()

        ttk.Button(btn_row, text="Save", command=_save_and_close, width=10).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="Cancel", command=dialog.destroy, width=10).pack(side=tk.LEFT, padx=4)

    # ------------------------------------------------------------------
    # Academic Sources dialog (Settings > Academic Sources)
    # ------------------------------------------------------------------

    def _open_sources_settings(self) -> None:
        """Open a dialog for configuring academic sources."""
        dialog = tk.Toplevel(self)
        dialog.title("Academic Sources")
        dialog.geometry("780x860")
        dialog.transient(self)
        dialog.grab_set()

        f = ttk.Frame(dialog, padding=12)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Academic Sources", font=_FONT_BOLD).pack(anchor=tk.W, pady=(0, 8))

        # Header row
        hdr = ttk.Frame(f)
        hdr.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(hdr, text="Source", font=_FONT_BOLD, width=18, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Label(hdr, text="Pricing", font=_FONT_SMALL, width=22, anchor=tk.W, foreground="gray").pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(hdr, text="API Key", font=_FONT_SMALL, width=24, anchor=tk.W, foreground="gray").pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(hdr, text="Info", font=_FONT_SMALL, anchor=tk.W, foreground="gray").pack(side=tk.LEFT, padx=(4, 0))

        ttk.Separator(f, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, 2))

        # Scrollable area
        canvas_frame = ttk.Frame(f)
        canvas_frame.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(canvas_frame, highlightthickness=0, height=240)
        scrollbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        if _IS_DARK:
            canvas.configure(bg=_DARK_BG)
        else:
            canvas.configure(bg=_LIGHT_BG)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_mousewheel(event):
            try:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except tk.TclError:
                pass  # canvas was destroyed, ignore stale scroll events
        canvas.bind_all("<MouseWheel>", _on_mousewheel, add="+")

        for _row_idx, (name, requires_key, env_var, pricing, desc, _default_on) in enumerate(self._ACADEMIC_SOURCES):
            row = ttk.Frame(inner)
            row.pack(fill=tk.X, pady=1)

            enabled_var = self._source_enabled_vars[name]
            cb = ttk.Checkbutton(row, text=name, variable=enabled_var, width=18)
            cb.pack(side=tk.LEFT)

            ttk.Label(row, text=pricing, font=_FONT_SMALL, width=22, anchor=tk.W, foreground="gray").pack(side=tk.LEFT, padx=(4, 0))

            if env_var:
                key_var = self._source_key_vars[name]
                key_entry = ttk.Entry(row, textvariable=key_var, show="*", width=24, font=_FONT_SMALL)
                key_entry.pack(side=tk.LEFT, padx=(4, 0))

                show_var = tk.BooleanVar(value=False)
                def _make_toggle(entry=key_entry, sv=show_var):
                    def _toggle():
                        entry.configure(show="" if sv.get() else "*")
                    return _toggle
                ttk.Checkbutton(row, text="Show", variable=show_var, command=_make_toggle(), width=5).pack(side=tk.LEFT, padx=(2, 0))
            else:
                ttk.Label(row, text="No key needed", font=_FONT_SMALL, width=24, anchor=tk.W, foreground="gray").pack(side=tk.LEFT, padx=(4, 0))
                ttk.Label(row, text="", width=5).pack(side=tk.LEFT, padx=(2, 0))

            ttk.Label(row, text=desc, font=_FONT_SMALL, anchor=tk.W, foreground="gray").pack(side=tk.LEFT, padx=(8, 0))

        # Buttons
        btn_row = ttk.Frame(f)
        btn_row.pack(pady=(12, 0))

        def _save_and_close():
            self._save_source_states()
            dialog.destroy()

        ttk.Button(btn_row, text="Save", command=_save_and_close, width=10).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="Cancel", command=dialog.destroy, width=10).pack(side=tk.LEFT, padx=4)

    # ------------------------------------------------------------------
    # Resource Limits dialog (Settings > Resource Limits)
    # ------------------------------------------------------------------

    def _open_resources_settings(self) -> None:
        """Open a dialog for configuring resource limits."""
        dialog = tk.Toplevel(self)
        dialog.title("Resource Limits")
        dialog.geometry("480x280")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        f = ttk.Frame(dialog, padding=16)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Resource Limits", font=_FONT_BOLD).pack(anchor=tk.W, pady=(0, 4))
        ttk.Label(f, text="Pause daemon when system resources exceed these limits. These limits only apply in Continuous Mode. When running a single paper, they are ignored.", font=_FONT_SMALL, wraplength=400).pack(anchor=tk.W, pady=(0, 12))

        # CPU threshold
        ttk.Label(f, text="CPU threshold:", font=_FONT).pack(anchor=tk.W)
        cpu_row = ttk.Frame(f)
        cpu_row.pack(fill=tk.X, anchor=tk.W)
        self._cpu_scale_dlg = ttk.Scale(cpu_row, from_=10, to=100, orient=tk.HORIZONTAL, variable=self._cpu_var, length=200)
        self._cpu_scale_dlg.pack(side=tk.LEFT)
        cpu_lbl = ttk.Label(cpu_row, text=f"{int(self._cpu_var.get())}%", width=5, font=_FONT)
        cpu_lbl.pack(side=tk.LEFT, padx=(4, 0))
        self._cpu_var.trace_add("write", lambda *_: cpu_lbl.configure(text=f"{int(self._cpu_var.get())}%"))

        # MEM threshold
        ttk.Label(f, text="MEM threshold:", font=_FONT).pack(anchor=tk.W, pady=(8, 0))
        mem_row = ttk.Frame(f)
        mem_row.pack(fill=tk.X, anchor=tk.W)
        self._mem_scale_dlg = ttk.Scale(mem_row, from_=10, to=100, orient=tk.HORIZONTAL, variable=self._mem_var, length=200)
        self._mem_scale_dlg.pack(side=tk.LEFT)
        mem_lbl = ttk.Label(mem_row, text=f"{int(self._mem_var.get())}%", width=5, font=_FONT)
        mem_lbl.pack(side=tk.LEFT, padx=(4, 0))
        self._mem_var.trace_add("write", lambda *_: mem_lbl.configure(text=f"{int(self._mem_var.get())}%"))

        # Buttons
        btn_row = ttk.Frame(f)
        btn_row.pack(pady=(16, 0))

        def _save_and_close():
            self._save_state()
            dialog.destroy()

        ttk.Button(btn_row, text="Save", command=_save_and_close, width=10).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="Cancel", command=dialog.destroy, width=10).pack(side=tk.LEFT, padx=4)

    # ------------------------------------------------------------------
    # Pipeline Config dialog (Settings > Pipeline Config)
    # ------------------------------------------------------------------

    _PIPELINE_CONFIG_FILE = _CONFIG_DIR / "pipeline_config.json"

    def _load_pipeline_config(self) -> dict:
        """Load pipeline config from disk."""
        try:
            if self._PIPELINE_CONFIG_FILE.exists():
                return json.loads(self._PIPELINE_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_pipeline_config(self, data: dict) -> None:
        """Save pipeline config to disk."""
        self._PIPELINE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._PIPELINE_CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get_pipeline_config(self) -> dict:
        """Return pipeline config dict for ResearchConfig fields."""
        return self._load_pipeline_config()

    def _open_pipeline_config(self) -> None:
        """Open dialog for pipeline configuration (search limits, expand passes, etc.)."""
        dialog = tk.Toplevel(self)
        dialog.title("Pipeline Configuration")
        dialog.geometry("480x400")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        f = ttk.Frame(dialog, padding=16)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Pipeline Configuration", font=_FONT_BOLD).pack(anchor=tk.W, pady=(0, 4))
        ttk.Label(f, text="Control search breadth, reference targets, and expansion behavior.",
                  font=_FONT_SMALL, wraplength=440).pack(anchor=tk.W, pady=(0, 12))

        cfg = self._load_pipeline_config()

        # Max search results per query
        row1 = ttk.Frame(f)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="Max results per search query:", font=_FONT, width=30, anchor=tk.W).pack(side=tk.LEFT)
        max_search_var = tk.IntVar(value=cfg.get("max_search_results", 30))
        ttk.Spinbox(row1, from_=5, to=100, textvariable=max_search_var, width=6, font=_FONT).pack(side=tk.LEFT)
        ttk.Label(row1, text="(default: 30)", font=_FONT_SMALL, foreground="gray").pack(side=tk.LEFT, padx=(6, 0))

        # Min references target
        row2 = ttk.Frame(f)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="Minimum references (warning):", font=_FONT, width=30, anchor=tk.W).pack(side=tk.LEFT)
        min_refs_var = tk.IntVar(value=cfg.get("min_references", 20))
        ttk.Spinbox(row2, from_=5, to=60, textvariable=min_refs_var, width=6, font=_FONT).pack(side=tk.LEFT)
        ttk.Label(row2, text="(default: 20)", font=_FONT_SMALL, foreground="gray").pack(side=tk.LEFT, padx=(6, 0))

        # Max expand passes
        row3 = ttk.Frame(f)
        row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="Max expand passes (per section):", font=_FONT, width=30, anchor=tk.W).pack(side=tk.LEFT)
        max_expand_var = tk.IntVar(value=cfg.get("max_expand_passes", 4))
        ttk.Spinbox(row3, from_=0, to=6, textvariable=max_expand_var, width=6, font=_FONT).pack(side=tk.LEFT)
        ttk.Label(row3, text="(default: 4)", font=_FONT_SMALL, foreground="gray").pack(side=tk.LEFT, padx=(6, 0))

        # Min total words
        row4 = ttk.Frame(f)
        row4.pack(fill=tk.X, pady=2)
        ttk.Label(row4, text="Min total words (submit gate):", font=_FONT, width=30, anchor=tk.W).pack(side=tk.LEFT)
        min_words_var = tk.IntVar(value=cfg.get("min_total_words", 4000))
        ttk.Spinbox(row4, from_=1000, to=10000, increment=500, textvariable=min_words_var, width=6, font=_FONT).pack(side=tk.LEFT)
        ttk.Label(row4, text="(default: 4000)", font=_FONT_SMALL, foreground="gray").pack(side=tk.LEFT, padx=(6, 0))

        # Buttons
        btn_row = ttk.Frame(f)
        btn_row.pack(pady=(16, 0))

        def _save_and_close():
            self._save_pipeline_config({
                "max_search_results": max_search_var.get(),
                "min_references": min_refs_var.get(),
                "max_expand_passes": max_expand_var.get(),
                "min_total_words": min_words_var.get(),
            })
            dialog.destroy()

        def _reset_defaults():
            max_search_var.set(30)
            min_refs_var.set(20)
            max_expand_var.set(4)
            min_words_var.set(4000)

        ttk.Button(btn_row, text="Save", command=_save_and_close, width=10).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="Defaults", command=_reset_defaults, width=10).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="Cancel", command=dialog.destroy, width=10).pack(side=tk.LEFT, padx=4)

    # ------------------------------------------------------------------
    # Token & Word Limits settings dialog
    # ------------------------------------------------------------------

    _TOKEN_LIMITS_FILE = _CONFIG_DIR / "token_limits.json"

    _SECTIONS_FOR_LIMITS = [
        "Introduction", "Related Work", "Methodology", "Results",
        "Discussion", "Limitations", "Conclusion", "Abstract",
    ]

    def _load_token_limits(self) -> dict:
        """Load saved token/word limits from config file."""
        if self._TOKEN_LIMITS_FILE.exists():
            try:
                return json.loads(self._TOKEN_LIMITS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_token_limits(self, data: dict) -> None:
        """Save token/word limits to config file."""
        self._TOKEN_LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._TOKEN_LIMITS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get_section_token_limits(self) -> dict[str, int] | None:
        """Return saved per-section token limits for ResearchConfig, or None if defaults."""
        data = self._load_token_limits()
        limits = data.get("token_limits")
        return limits if limits else None

    def get_section_word_targets(self) -> dict[str, int] | None:
        """Return saved per-section word targets for ResearchConfig, or None if defaults."""
        data = self._load_token_limits()
        targets = data.get("word_targets")
        return targets if targets else None

    def get_section_word_minimums(self) -> dict[str, int] | None:
        """Return saved per-section word minimums for ResearchConfig, or None if defaults."""
        data = self._load_token_limits()
        mins = data.get("word_minimums")
        return mins if mins else None

    def _open_token_limits_settings(self) -> None:
        """Open dialog for configuring per-section token limits and word targets."""
        from agentpub._constants import _SECTION_TOKEN_LIMITS, _SECTION_WORD_TARGETS, _SECTION_WORD_MINIMUMS

        dialog = tk.Toplevel(self)
        dialog.title("Token & Word Limits")
        dialog.geometry("680x520")
        dialog.resizable(True, True)
        dialog.transient(self)
        dialog.grab_set()

        f = ttk.Frame(dialog, padding=12)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Token & Word Limits per Section", font=_FONT_BOLD).pack(anchor=tk.W, pady=(0, 2))
        ttk.Label(
            f, text="Token limits are capped by each model's max output. Word targets guide section length.",
            font=_FONT_SMALL, wraplength=620,
        ).pack(anchor=tk.W, pady=(0, 8))

        # Load saved values
        saved = self._load_token_limits()
        saved_tokens = saved.get("token_limits", {})
        saved_targets = saved.get("word_targets", {})
        saved_mins = saved.get("word_minimums", {})

        # Scrollable grid
        canvas = tk.Canvas(f, highlightthickness=0)
        scrollbar = ttk.Scrollbar(f, orient=tk.VERTICAL, command=canvas.yview)
        grid_frame = ttk.Frame(canvas)

        grid_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=grid_frame, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Headers
        headers = ["Section", "Max Tokens", "Word Target", "Word Minimum"]
        for col, h in enumerate(headers):
            ttk.Label(grid_frame, text=h, font=_FONT_BOLD).grid(row=0, column=col, padx=6, pady=4, sticky=tk.W)

        # One row per section
        token_vars: dict[str, tk.StringVar] = {}
        target_vars: dict[str, tk.StringVar] = {}
        min_vars: dict[str, tk.StringVar] = {}

        for row, section in enumerate(self._SECTIONS_FOR_LIMITS, start=1):
            ttk.Label(grid_frame, text=section, font=_FONT).grid(row=row, column=0, padx=6, pady=2, sticky=tk.W)

            # Token limit
            default_tokens = _SECTION_TOKEN_LIMITS.get(section, 65000)
            tv = tk.StringVar(value=str(saved_tokens.get(section, default_tokens)))
            token_vars[section] = tv
            ttk.Entry(grid_frame, textvariable=tv, width=10, font=_FONT).grid(row=row, column=1, padx=6, pady=2)

            # Word target
            default_target = _SECTION_WORD_TARGETS.get(section, 1000)
            wt = tk.StringVar(value=str(saved_targets.get(section, default_target)))
            target_vars[section] = wt
            ttk.Entry(grid_frame, textvariable=wt, width=10, font=_FONT).grid(row=row, column=2, padx=6, pady=2)

            # Word minimum
            default_min = _SECTION_WORD_MINIMUMS.get(section, 200)
            wm = tk.StringVar(value=str(saved_mins.get(section, default_min)))
            min_vars[section] = wm
            ttk.Entry(grid_frame, textvariable=wm, width=10, font=_FONT).grid(row=row, column=3, padx=6, pady=2)

        # Buttons
        btn_row = ttk.Frame(f)
        btn_row.pack(pady=(12, 0))

        def _reset_defaults():
            for section in self._SECTIONS_FOR_LIMITS:
                token_vars[section].set(str(_SECTION_TOKEN_LIMITS.get(section, 65000)))
                target_vars[section].set(str(_SECTION_WORD_TARGETS.get(section, 1000)))
                min_vars[section].set(str(_SECTION_WORD_MINIMUMS.get(section, 200)))

        def _save_and_close():
            data = {"token_limits": {}, "word_targets": {}, "word_minimums": {}}
            for section in self._SECTIONS_FOR_LIMITS:
                try:
                    data["token_limits"][section] = int(token_vars[section].get())
                except ValueError:
                    pass
                try:
                    data["word_targets"][section] = int(target_vars[section].get())
                except ValueError:
                    pass
                try:
                    data["word_minimums"][section] = int(min_vars[section].get())
                except ValueError:
                    pass
            self._save_token_limits(data)
            dialog.destroy()

        ttk.Button(btn_row, text="Reset Defaults", command=_reset_defaults, width=14).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="Save", command=_save_and_close, width=10).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="Cancel", command=dialog.destroy, width=10).pack(side=tk.LEFT, padx=4)

    def _open_about(self) -> None:
        """Show an about dialog."""
        messagebox.showinfo(
            "About AgentPub Desktop",
            f"AgentPub Desktop v{self._version}\n\n"
            "Autonomous AI Research Agent\n"
            "Write, review, and cite academic papers.\n\n"
            "agentpub.org\n\n"
            "Check for updates: agentpub update",
        )

    # ------------------------------------------------------------------
    # Writing Prompts Editor
    # ------------------------------------------------------------------

    _PROMPTS_DIR = _CONFIG_DIR / "prompts"

    # Friendly display names for prompt keys
    _PROMPT_LABELS: dict[str, str] = {
        # Phase 1 — Scope
        "phase1_research_brief": "Research Brief",
        "phase2_outline": "Initial Outline",
        # Phase 3 — Search & Screen
        "phase3_search_strategy": "Search Strategy (databases, limits, year filters)",
        "phase3_screen": "Paper Screening",
        "phase3_outline": "Landscape Mapping",
        # Phase 4 — Read & Annotate
        "phase4_reading_memo": "Reading Memo",
        "phase4_deep_reading": "Deep Reading",
        "phase4_evidence_extraction": "Evidence Extraction",
        "phase4_synthesis": "Synthesis & Hypotheses",
        "phase5_revise_outline": "Revise Outline (after reading)",
        # Phase 6 — Analyze & Audit
        "phase6_evidence_map": "Evidence Map",
        "phase6_comparison_table": "Comparison Table",
        "phase6_editorial_review": "Editorial Review (overclaiming, fabrication, jargon)",
        "phase6_citation_cleanup": "Citation Cleanup (phantom, wrong year, pseudo-cites)",
        "phase6_abstract_crosscheck": "Abstract Cross-Check (claims vs body)",
        "phase6_methodology_fix": "Methodology Fix (pipeline numbers, fake stages)",
        "phase6_citation_justification": "Citation Justification",
        "phase6_structured_reflection": "Structured Reflection (pre-writing)",
        # Phase 7 — Write
        "synthesis_system": "System Prompt (all writing)",
        "phase7_write_section": "Write Section",
        "phase7_abstract": "Abstract",
        "phase7_expand_section": "Expand Section",
        "phase7_paragraph_plan": "Paragraph Plan (paragraph mode)",
        "phase7_write_paragraph": "Write Paragraph (paragraph mode)",
        "phase7_stitch_section": "Stitch Section (paragraph mode)",
        "phase7_dedup": "Deduplication",
        "phase7_weakness_guidance": "Weakness Guidance",
        # Phase 8 — Revise & Verify
        "phase8_self_critique": "Self Critique",
        "phase8_targeted_revision": "Targeted Revision",
        "phase8_verification": "Verification",
        "phase8b_verification": "Fact Check",
        "phase8_source_verification": "Source Verification",
        # Phase 9 — Adversarial Review
        "phase9_adversarial_review": "Adversarial Self-Review",
        "phase9_adversarial_fix": "Adversarial Fix",
        # Post-submission
        "fix_paper": "Fix Paper (on rejection)",
        "generate_references": "Reference Gap-Fill (search config)",
        "peer_review": "Peer Review",
        # Section guidance
        "guidance_introduction": "Introduction",
        "guidance_related_work": "Related Work",
        "guidance_methodology": "Methodology",
        "guidance_results": "Results",
        "guidance_discussion": "Discussion",
        "guidance_limitations": "Limitations",
        "guidance_conclusion": "Conclusion",
        # Paper-type guidance
        "paper_type_survey": "Survey",
        "paper_type_empirical": "Empirical",
        "paper_type_theoretical": "Theoretical",
        "paper_type_meta_analysis": "Meta-Analysis",
        "paper_type_position": "Position Paper",
        "paper_type_review": "Review (alias of Survey)",
        # Contribution-type guidance
        "contribution_testable_hypotheses": "Testable Hypotheses",
        "contribution_map_contradictions": "Map Contradictions",
        "contribution_quantitative_synthesis": "Quantitative Synthesis",
        "contribution_identify_gaps": "Identify Gaps",
        "contribution_challenge_wisdom": "Challenge Wisdom",
        "contribution_methodological_critique": "Methodological Critique",
        "contribution_cross_pollinate": "Cross-Pollinate Fields",
        # Writing rules & templates
        "writing_rules": "Writing Rules (all sections)",
        "section_writing_rules": "Section Writing Rules",
        "methodology_data_template": "Methodology Data Template",
        "abstract_grounding_rules": "Abstract Grounding Rules",
        # Daemon — Community Participation
        "daemon_challenge_select": "Challenge Selection (pick best challenge)",
        "daemon_collab_relevant": "Collaboration Relevance (accept/reject topic)",
        "daemon_review_expertise": "Review Expertise Check (within domain?)",
        "daemon_conference_match": "Conference Paper Match (best paper for conf)",
        "daemon_trending_select": "Trending Topic Selection (pick best match)",
    }

    # Organized sections for the prompt editor tree view
    _PROMPT_SECTIONS: list[tuple[str, list[str]]] = [
        ("Phase 1 — Scope", [
            "phase1_research_brief",
        ]),
        ("Phase 2 — Outline", [
            "phase2_outline",
        ]),
        ("Phase 3 — Search & Screen", [
            "phase3_search_strategy",
            "phase3_screen",
            "phase3_outline",
        ]),
        ("Phase 4 — Deep Reading", [
            "phase4_reading_memo",
            "phase4_deep_reading",
            "phase4_evidence_extraction",
            "phase4_synthesis",
        ]),
        ("Phase 5 — Revise Outline", [
            "phase5_revise_outline",
        ]),
        ("Phase 6 — Analyze & Audit", [
            "phase6_evidence_map",
            "phase6_comparison_table",
            "phase6_editorial_review",
            "phase6_citation_cleanup",
            "phase6_abstract_crosscheck",
            "phase6_methodology_fix",
            "phase6_citation_justification",
            "phase6_structured_reflection",
        ]),
        ("Phase 7 — Writing", [
            "synthesis_system",
            "phase7_write_section",
            "phase7_paragraph_plan",
            "phase7_write_paragraph",
            "phase7_stitch_section",
            "phase7_abstract",
            "phase7_expand_section",
            "phase7_dedup",
            "phase7_weakness_guidance",
        ]),
        ("Phase 8 — Audit", [
            "phase8_self_critique",
            "phase8_targeted_revision",
            "phase8_verification",
            "phase8b_verification",
            "phase8_source_verification",
        ]),
        ("Phase 9 — Adversarial Review", [
            "phase9_adversarial_review",
            "phase9_adversarial_fix",
        ]),
        ("Post-Submission", [
            "fix_paper",
            "generate_references",
            "peer_review",
        ]),
        ("Section Guidance", [
            "guidance_introduction",
            "guidance_related_work",
            "guidance_methodology",
            "guidance_results",
            "guidance_discussion",
            "guidance_limitations",
            "guidance_conclusion",
        ]),
        ("Paper Type Guidance", [
            "paper_type_survey",
            "paper_type_empirical",
            "paper_type_theoretical",
            "paper_type_meta_analysis",
            "paper_type_position",
            "paper_type_review",
        ]),
        ("Contribution Type Guidance", [
            "contribution_testable_hypotheses",
            "contribution_map_contradictions",
            "contribution_quantitative_synthesis",
            "contribution_identify_gaps",
            "contribution_challenge_wisdom",
            "contribution_methodological_critique",
            "contribution_cross_pollinate",
        ]),
        ("Writing Rules & Templates", [
            "writing_rules",
            "section_writing_rules",
            "methodology_data_template",
            "abstract_grounding_rules",
        ]),
        ("Daemon — Community Participation", [
            "daemon_challenge_select",
            "daemon_collab_relevant",
            "daemon_review_expertise",
            "daemon_conference_match",
            "daemon_trending_select",
        ]),
    ]

    def _load_writing_prompt(self, key: str) -> str | None:
        """Load a custom writing prompt override, or None if not customized."""
        path = self._PROMPTS_DIR / f"{key}.txt"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def _save_writing_prompt(self, key: str, text: str) -> None:
        """Save a custom writing prompt override."""
        self._PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        (self._PROMPTS_DIR / f"{key}.txt").write_text(text, encoding="utf-8")

    def _delete_writing_prompt(self, key: str) -> None:
        """Delete a custom writing prompt override (revert to default)."""
        path = self._PROMPTS_DIR / f"{key}.txt"
        if path.exists():
            path.unlink()

    def _get_full_prompt(self, key: str) -> str:
        """Build the full effective prompt for a given key.

        For phase7_write_section, expands the per-section guidance and
        paper-type overrides. Other prompts are shown as-is (they now
        include both SYSTEM and USER PROMPT TEMPLATE).
        """
        from agentpub.prompts import DEFAULT_PROMPTS, _SECTION_GUIDANCE, _ANTI_PATTERNS, _PAPER_TYPE_GUIDANCE

        base = DEFAULT_PROMPTS.get(key, "")

        # For phase7_write_section, show it expanded with each section's guidance
        if key == "phase7_write_section":
            parts = ["# PHASE 7 — WRITE SECTION\n"]
            parts.append("This prompt is used for EACH section of the paper. "
                         "The {section_name} and {section_guidance} placeholders "
                         "are filled per section.\n")
            parts.append("=" * 60 + "\n")
            parts.append("## System prompt template\n\n")
            parts.append(base)
            parts.append("\n\n" + "=" * 60 + "\n")
            parts.append("## Per-section guidance ({section_guidance})\n")
            for section, guidance in _SECTION_GUIDANCE.items():
                parts.append(f"\n### {section}\n{guidance}\n")
            parts.append("\n" + "=" * 60 + "\n")
            parts.append("## Paper-type guidance (injected alongside section guidance)\n")
            for ptype, sections in _PAPER_TYPE_GUIDANCE.items():
                parts.append(f"\n### Paper type: {ptype}\n")
                for sname, sguidance in sections.items():
                    parts.append(f"  {sname}: {sguidance}\n")
            return "\n".join(parts)

        return base

    def _open_prompts_dialog(self) -> None:
        """Open dialog to view and edit writing prompts used by the research pipeline."""
        from agentpub.prompts import DEFAULT_PROMPTS, load_prompts

        dialog = tk.Toplevel(self)
        dialog.title("Writing Prompts")
        dialog.geometry("920x680")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="Writing Prompts", font=_FONT_BOLD).pack(pady=(12, 2))
        ttk.Label(
            dialog,
            text="View and customize the prompts used by the research pipeline. "
            "Shows the full effective prompt including section guidance and writing rules. "
            "Custom prompts are saved locally and override the defaults.",
            font=_FONT_SMALL, wraplength=860,
        ).pack(pady=(0, 8))

        # Main content: list on left, editor on right
        content = ttk.Frame(dialog)
        content.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 4))
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)

        # Prompt tree (collapsible sections)
        list_frame = ttk.Frame(content)
        list_frame.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        list_frame.rowconfigure(1, weight=1)

        ttk.Label(list_frame, text="Prompts:", font=_FONT_SMALL).grid(row=0, column=0, sticky=tk.W)

        prompt_tree = ttk.Treeview(
            list_frame, show="tree", selectmode="browse",
        )
        prompt_tree.grid(row=1, column=0, sticky="nsew")
        prompt_tree.column("#0", width=320, minwidth=200)

        tree_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=prompt_tree.yview)
        tree_scroll.grid(row=1, column=1, sticky="ns")
        prompt_tree.configure(yscrollcommand=tree_scroll.set)

        if _IS_DARK:
            style = ttk.Style()
            style.configure("Treeview", background=_DARK_FIELD, foreground=_DARK_FG,
                            fieldbackground=_DARK_FIELD)
            style.map("Treeview", background=[("selected", _DARK_SELECT)])

        # Build a flat list of prompt_keys in section order (for index lookup)
        prompt_keys: list[str] = []
        # Map tree item IDs to prompt keys
        tree_item_to_key: dict[str, str] = {}

        for section_name, keys in self._PROMPT_SECTIONS:
            # Insert section header (not selectable as a prompt)
            section_id = prompt_tree.insert("", tk.END, text=f"\u25BC {section_name}", open=True)
            for key in keys:
                if key not in DEFAULT_PROMPTS:
                    continue
                label = self._PROMPT_LABELS.get(key, key)
                custom = self._load_writing_prompt(key)
                prefix = "\u2022 " if custom else "  "
                item_id = prompt_tree.insert(section_id, tk.END, text=f"{prefix}{label}")
                tree_item_to_key[item_id] = key
                prompt_keys.append(key)

        # Toggle section collapse on click
        def on_tree_toggle(event):
            item = prompt_tree.identify_row(event.y)
            if not item:
                return
            # Only toggle if it's a section header (has children)
            children = prompt_tree.get_children(item)
            if children:
                if prompt_tree.item(item, "open"):
                    prompt_tree.item(item, open=False)
                    text = prompt_tree.item(item, "text")
                    prompt_tree.item(item, text=text.replace("\u25BC", "\u25B6"))
                else:
                    prompt_tree.item(item, open=True)
                    text = prompt_tree.item(item, "text")
                    prompt_tree.item(item, text=text.replace("\u25B6", "\u25BC"))

        # Editor area
        editor_frame = ttk.Frame(content)
        editor_frame.grid(row=0, column=1, sticky="nsew")
        editor_frame.rowconfigure(1, weight=1)
        editor_frame.columnconfigure(0, weight=1)

        header_row = ttk.Frame(editor_frame)
        header_row.grid(row=0, column=0, sticky=tk.EW, pady=(0, 4))
        self._prompt_editor_label = ttk.Label(header_row, text="Select a prompt from the list", font=_FONT)
        self._prompt_editor_label.pack(side=tk.LEFT)
        self._prompt_editor_status = ttk.Label(header_row, text="", font=_FONT_SMALL)
        self._prompt_editor_status.pack(side=tk.RIGHT)

        prompt_text = scrolledtext.ScrolledText(editor_frame, wrap=tk.WORD, font=(_FONT_MONO, self._font_size))
        prompt_text.grid(row=1, column=0, sticky="nsew")
        _theme_scrolled_text(prompt_text)

        # Buttons
        btn_frame = ttk.Frame(editor_frame)
        btn_frame.grid(row=2, column=0, sticky=tk.W, pady=(8, 0))

        current_key = [None]  # mutable ref for closure

        def on_select(_event=None):
            sel = prompt_tree.selection()
            if not sel:
                return
            item_id = sel[0]
            key = tree_item_to_key.get(item_id)
            if not key:
                return  # Clicked on section header
            current_key[0] = key
            label = self._PROMPT_LABELS.get(key, key)
            self._prompt_editor_label.configure(text=label)

            custom = self._load_writing_prompt(key)
            prompt_text.configure(state=tk.NORMAL)
            prompt_text.delete("1.0", tk.END)
            if custom:
                prompt_text.insert(tk.END, custom)
                self._prompt_editor_status.configure(text="(customized)")
            else:
                prompt_text.insert(tk.END, self._get_full_prompt(key))
                self._prompt_editor_status.configure(text=f"(default — {len(self._get_full_prompt(key))} chars)")

        def _update_tree_item_text(key: str, customized: bool):
            """Update the tree item text to show/hide the bullet prefix."""
            for item_id, k in tree_item_to_key.items():
                if k == key:
                    label = self._PROMPT_LABELS.get(key, key)
                    prefix = "\u2022 " if customized else "  "
                    prompt_tree.item(item_id, text=f"{prefix}{label}")
                    break

        def save_prompt():
            key = current_key[0]
            if not key:
                return
            text = prompt_text.get("1.0", tk.END).rstrip()
            self._save_writing_prompt(key, text)
            self._prompt_editor_status.configure(text="(customized — saved)")
            _update_tree_item_text(key, True)

        def reset_prompt():
            key = current_key[0]
            if not key:
                return
            self._delete_writing_prompt(key)
            prompt_text.configure(state=tk.NORMAL)
            prompt_text.delete("1.0", tk.END)
            prompt_text.insert(tk.END, self._get_full_prompt(key))
            self._prompt_editor_status.configure(text="(default — reset)")
            _update_tree_item_text(key, False)

        def download_latest():
            """Fetch latest prompts from the AgentPub API."""
            try:
                remote = load_prompts()
                key = current_key[0]
                if key and key in remote:
                    prompt_text.configure(state=tk.NORMAL)
                    prompt_text.delete("1.0", tk.END)
                    prompt_text.insert(tk.END, remote[key])
                    self._prompt_editor_status.configure(text="(fetched from API — unsaved)")
                else:
                    messagebox.showinfo("Download", "No remote update available for this prompt.")
            except Exception as e:
                messagebox.showerror("Download Failed", str(e))

        ttk.Button(btn_frame, text="Save", command=save_prompt, width=10).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_frame, text="Reset to Default", command=reset_prompt, width=14).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Download Latest from AgentPub", command=download_latest).pack(side=tk.LEFT, padx=4)

        prompt_tree.bind("<<TreeviewSelect>>", on_select)
        prompt_tree.bind("<Double-1>", on_tree_toggle)

        # Select first prompt item (skip section header)
        if prompt_keys:
            for item_id, key in tree_item_to_key.items():
                prompt_tree.selection_set(item_id)
                on_select()
                break

        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=(4, 12))

    def _open_evaluator_prompt_dialog(self) -> None:
        """Open a standalone dialog for just the evaluator prompt editor."""
        dialog = tk.Toplevel(self)
        dialog.title("Evaluator Prompt")
        dialog.geometry("700x500")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="Evaluator Prompt", font=_FONT_BOLD).pack(pady=(12, 2))
        ttk.Label(
            dialog,
            text="The prompt sent to models when evaluating papers. "
            "Edit and save to customize, or download the latest from AgentPub.",
            font=_FONT_SMALL, wraplength=660,
        ).pack(pady=(0, 8))

        prompt_text = scrolledtext.ScrolledText(dialog, wrap=tk.WORD, font=(_FONT_MONO, self._font_size))
        prompt_text.pack(fill=tk.BOTH, expand=True, padx=12)
        _theme_scrolled_text(prompt_text)
        prompt_text.insert(tk.END, self._load_eval_prompt())

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=(8, 12))

        def save_prompt():
            text = prompt_text.get("1.0", tk.END).rstrip()
            self._save_eval_prompt(text)
            messagebox.showinfo("Saved", "Evaluator prompt saved.")

        def reset_prompt():
            try:
                from agentpub.paper_evaluator import EVALUATION_PROMPT
                if self._EVAL_PROMPT_FILE.exists():
                    self._EVAL_PROMPT_FILE.unlink()
                prompt_text.delete("1.0", tk.END)
                prompt_text.insert(tk.END, EVALUATION_PROMPT)
            except ImportError:
                messagebox.showerror("Error", "Could not load default prompt.")

        def download_latest():
            import httpx as _httpx
            try:
                resp = _httpx.get("https://api.agentpub.org/v1/evaluator/prompt", timeout=10)
                if resp.status_code == 200:
                    prompt_text.delete("1.0", tk.END)
                    prompt_text.insert(tk.END, resp.text)
                else:
                    raise RuntimeError(f"HTTP {resp.status_code}")
            except Exception:
                try:
                    resp = _httpx.get(
                        "https://raw.githubusercontent.com/agentpub/agentpub.org/main/python/EVALUATOR_PLAYBOOK.md",
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        prompt_text.delete("1.0", tk.END)
                        prompt_text.insert(tk.END, resp.text)
                    else:
                        messagebox.showerror("Download Failed", "Could not fetch prompt from API or GitHub.")
                except Exception as e2:
                    messagebox.showerror("Download Failed", str(e2))

        ttk.Button(btn_frame, text="Save", command=save_prompt, width=10).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Reset to Default", command=reset_prompt, width=14).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Download Latest from AgentPub", command=download_latest).pack(side=tk.LEFT, padx=4)

    def _zoom_font(self, delta: int) -> None:
        """Increase or decrease font size for the entire application."""
        new_size = max(7, min(18, self._font_size + delta))
        if new_size == self._font_size:
            return
        self._font_size = new_size

        # Update the default fonts so new widgets inherit the size
        import tkinter.font as tkFont
        mono = (_FONT_MONO, new_size)
        mono_small = (_FONT_MONO, new_size - 1)
        ui_font = (_FONT_FAMILY, new_size)
        ui_bold = (_FONT_FAMILY, new_size + 1, "bold")
        ui_small = (_FONT_FAMILY, new_size - 1)

        # Output panels
        for w in (self._progress_text, self._refs_text, self._paper_text):
            w.configure(font=mono)
        self._log_text.configure(font=mono_small)

        # Update all ttk widget fonts via style
        style = ttk.Style()
        style.configure(".", font=ui_font)
        style.configure("TLabel", font=ui_font)
        style.configure("TButton", font=ui_font)
        style.configure("TCheckbutton", font=ui_font)
        style.configure("TRadiobutton", font=ui_font)
        style.configure("TLabelframe.Label", font=ui_bold)
        style.configure("TNotebook.Tab", font=ui_font)

        # Treeview: update font + row height
        style.configure("Treeview", font=ui_font, rowheight=int(new_size * 2.2))
        style.configure("Treeview.Heading", font=ui_bold)

        # Menus: update all menu widgets in the app
        menu_font = (_FONT_FAMILY, new_size)
        if self._menubar:
            self._menubar.configure(font=menu_font)
            for i in range(self._menubar.index(tk.END) + 1):
                try:
                    submenu = self._menubar.nametowidget(self._menubar.entrycget(i, "menu"))
                    submenu.configure(font=menu_font)
                except (tk.TclError, KeyError):
                    pass

        # Update ALL ScrolledText, Text, Listbox, and Entry widgets recursively
        # Including Toplevel dialogs (like Writing Prompts)
        def _update_widget_fonts(widget):
            for child in widget.winfo_children():
                try:
                    wclass = child.winfo_class()
                    if wclass in ("Text", "ScrolledText"):
                        child.configure(font=mono)
                    elif wclass == "Listbox":
                        child.configure(font=ui_font)
                    elif wclass == "Entry":
                        child.configure(font=ui_font)
                    elif wclass == "Menu":
                        child.configure(font=menu_font)
                    elif wclass == "Toplevel":
                        # Recurse into Toplevel dialogs
                        _update_widget_fonts(child)
                        continue
                except tk.TclError:
                    pass
                _update_widget_fonts(child)
        _update_widget_fonts(self)
        # Also walk all Toplevel windows (they are children of the root)
        for w in self.winfo_children():
            if isinstance(w, tk.Toplevel):
                _update_widget_fonts(w)

        # Update named fonts if they exist
        for name in tkFont.names():
            try:
                f = tkFont.nametofont(name)
                if f.cget("family") in (_FONT_FAMILY, "TkDefaultFont"):
                    f.configure(size=new_size)
            except Exception:
                pass

        if hasattr(self, "_font_size_label"):
            self._font_size_label.configure(text=f"{new_size}pt")

    def _toggle_show_key(self) -> None:
        """Legacy — no longer used (token is hidden)."""
        pass

    def _auto_hide_key(self) -> None:
        """Legacy — no longer used."""
        pass

    def _update_account_display(self) -> None:
        """Update the account status label from config."""
        config = self._config
        api_key = config.get("api_key", "")
        if api_key:
            name = config.get("display_name", "")
            email = config.get("owner_email", "")
            if name and email:
                self._account_status_var.set(f"{name} ({email})")
            elif name:
                self._account_status_var.set(name)
            else:
                self._account_status_var.set("Logged in")
            self._register_btn.configure(text="Re-login")
        else:
            self._account_status_var.set("Not logged in")
            self._register_btn.configure(text="Login")

    def _do_logout(self) -> None:
        """Clear saved credentials and reset account display."""
        from tkinter import messagebox
        if not messagebox.askyesno("Logout", "Log out of AgentPub? You'll need to log in again to run the agent."):
            return
        _save_config({"api_key": "", "session_token": "", "owner_email": "", "display_name": "", "agent_id": ""})
        self._api_key_var.set("")
        self._config = _load_config()
        self._update_account_display()

    # ------------------------------------------------------------------
    # Paper Evaluator
    # ------------------------------------------------------------------

    _EVAL_PROMPT_FILE = _CONFIG_DIR / "evaluator_prompt.txt"

    def _load_eval_prompt(self) -> str:
        """Load the evaluation prompt — custom file or built-in default."""
        if self._EVAL_PROMPT_FILE.exists():
            return self._EVAL_PROMPT_FILE.read_text(encoding="utf-8")
        try:
            from agentpub.paper_evaluator import EVALUATION_PROMPT
            return EVALUATION_PROMPT
        except ImportError:
            return "(Could not load default prompt)"

    def _save_eval_prompt(self, text: str) -> None:
        """Save custom evaluation prompt."""
        self._EVAL_PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._EVAL_PROMPT_FILE.write_text(text, encoding="utf-8")

    def _open_library_dialog(self) -> None:
        """Open the local paper library management dialog."""
        from agentpub.library import PaperLibrary

        if not self._paper_library:
            self._paper_library = PaperLibrary()
            self._paper_library.ensure_dir()

        lib = self._paper_library
        dialog = tk.Toplevel(self)
        dialog.title("My Library")
        dialog.geometry("700x500")
        dialog.transient(self)

        # Explanation
        desc_frame = ttk.Frame(dialog, padding=(8, 8, 8, 0))
        desc_frame.pack(fill=tk.X)
        ttk.Label(desc_frame, text="Add your own reference papers here. The pipeline will automatically decide when to cite them based on relevance. If you want a specific paper to be used, mention it in your research topic or research questions.", font=("Segoe UI", 9), wraplength=660, foreground="gray").pack(anchor=tk.W)

        # Header
        header_frame = ttk.Frame(dialog, padding=8)
        header_frame.pack(fill=tk.X)

        count_var = tk.StringVar(value=f"{lib.count()} papers indexed")
        ttk.Label(header_frame, textvariable=count_var, font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)

        ttk.Button(header_frame, text="Open Folder", command=lambda: self._open_library_folder(lib)).pack(side=tk.RIGHT, padx=4)
        ttk.Button(header_frame, text="Reindex", command=lambda: self._reindex_library(lib, tree, count_var)).pack(side=tk.RIGHT, padx=4)
        ttk.Button(header_frame, text="Import Zotero...", command=lambda: self._open_zotero_dialog(lib, tree, count_var)).pack(side=tk.RIGHT, padx=4)
        ttk.Button(header_frame, text="Add Papers...", command=lambda: self._add_library_papers(lib, tree, count_var)).pack(side=tk.RIGHT, padx=4)

        # Paper list (treeview)
        tree_frame = ttk.Frame(dialog, padding=(8, 0, 8, 4))
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("title", "authors", "year", "words", "type")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        tree.heading("title", text="Title")
        tree.heading("authors", text="Authors")
        tree.heading("year", text="Year")
        tree.heading("words", text="Words")
        tree.heading("type", text="Type")
        tree.column("title", width=300, minwidth=150)
        tree.column("authors", width=180, minwidth=80)
        tree.column("year", width=50, minwidth=40)
        tree.column("words", width=60, minwidth=40)
        tree.column("type", width=50, minwidth=30)

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Populate tree
        self._refresh_library_tree(lib, tree)

        # Bottom buttons
        btn_frame = ttk.Frame(dialog, padding=8)
        btn_frame.pack(fill=tk.X)

        ttk.Button(btn_frame, text="Remove Selected", command=lambda: self._remove_library_papers(lib, tree, count_var)).pack(side=tk.LEFT, padx=4)
        ttk.Label(btn_frame, text=f"Library folder: {lib.library_dir}", font=("Segoe UI", 8), foreground="gray").pack(side=tk.LEFT, padx=12)
        ttk.Button(btn_frame, text="Close", command=dialog.destroy).pack(side=tk.RIGHT, padx=4)

    def _refresh_library_tree(self, lib, tree) -> None:
        """Populate the treeview with all library papers."""
        for item in tree.get_children():
            tree.delete(item)
        for paper in lib.get_all():
            authors = ", ".join(paper.authors[:2]) if paper.authors else "—"
            if len(paper.authors) > 2:
                authors += " et al."
            tree.insert("", tk.END, iid=paper.file_path, values=(
                paper.title[:80],
                authors,
                paper.year or "—",
                f"{paper.word_count:,}",
                paper.source_type,
            ))

    def _add_library_papers(self, lib, tree, count_var) -> None:
        """Open file dialog to add papers."""
        from tkinter import filedialog
        files = filedialog.askopenfilenames(
            title="Add Papers to Library",
            filetypes=[
                ("All supported", "*.pdf *.html *.htm *.txt *.md"),
                ("PDF files", "*.pdf"),
                ("HTML files", "*.html *.htm"),
                ("Text files", "*.txt *.md"),
            ],
        )
        if files:
            added = lib.add_files(list(files), copy_to_library=True)
            if added:
                self._refresh_library_tree(lib, tree)
                count_var.set(f"{lib.count()} papers indexed")
                messagebox.showinfo("Library", f"Added {len(added)} papers.")
            else:
                messagebox.showwarning("Library", "No papers could be indexed. Check file formats.")

    def _remove_library_papers(self, lib, tree, count_var) -> None:
        """Remove selected papers from the library index."""
        selected = tree.selection()
        if not selected:
            return
        for file_path in selected:
            paper_id = lib._paper_id(file_path)
            lib.remove_paper(paper_id)
        self._refresh_library_tree(lib, tree)
        count_var.set(f"{lib.count()} papers indexed")

    def _reindex_library(self, lib, tree, count_var) -> None:
        """Reindex the library folder."""
        changes = lib.reindex()
        self._refresh_library_tree(lib, tree)
        count_var.set(f"{lib.count()} papers indexed")
        messagebox.showinfo("Library", f"Reindex complete: {changes} changes.")

    def _open_library_folder(self, lib) -> None:
        """Open the library folder in the system file manager."""
        import subprocess
        lib.ensure_dir()
        if sys.platform == "win32":
            os.startfile(str(lib.library_dir))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(lib.library_dir)])
        else:
            subprocess.Popen(["xdg-open", str(lib.library_dir)])

    # ------------------------------------------------------------------
    # Zotero integration
    # ------------------------------------------------------------------

    def _open_zotero_dialog(self, lib, library_tree, count_var) -> None:
        """Open a dialog to connect to Zotero (local or web) and import papers."""
        dialog = tk.Toplevel(self)
        dialog.title("Import from Zotero")
        dialog.geometry("650x520")
        dialog.transient(self)

        notebook = ttk.Notebook(dialog)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # --- Tab 1: Local Zotero ---
        local_tab = ttk.Frame(notebook, padding=12)
        notebook.add(local_tab, text="  Local Zotero  ")

        ttk.Label(local_tab, text="Import from local Zotero database", font=_FONT_BOLD).pack(anchor=tk.W, pady=(0, 8))

        # Auto-detect status
        from agentpub.zotero import find_zotero_data_dir
        detected_dir = find_zotero_data_dir()

        status_var = tk.StringVar()
        if detected_dir:
            status_var.set(f"Found: {detected_dir}")
        else:
            status_var.set("Zotero data directory not found")

        dir_frame = ttk.Frame(local_tab)
        dir_frame.pack(fill=tk.X, pady=4)
        ttk.Label(dir_frame, text="Data dir:").pack(side=tk.LEFT)
        dir_entry = ttk.Entry(dir_frame, textvariable=status_var, width=50)
        dir_entry.pack(side=tk.LEFT, padx=4, fill=tk.X, expand=True)

        def browse_zotero_dir():
            from tkinter import filedialog
            d = filedialog.askdirectory(title="Select Zotero Data Directory")
            if d:
                status_var.set(d)

        ttk.Button(dir_frame, text="Browse...", command=browse_zotero_dir).pack(side=tk.RIGHT)

        # Collection filter
        coll_frame = ttk.Frame(local_tab)
        coll_frame.pack(fill=tk.X, pady=4)
        ttk.Label(coll_frame, text="Collection:").pack(side=tk.LEFT)

        coll_var = tk.StringVar(value="All collections")
        coll_combo = ttk.Combobox(coll_frame, textvariable=coll_var, state="readonly", width=40)
        coll_combo.pack(side=tk.LEFT, padx=4)

        local_collections: list[dict] = []

        def refresh_collections():
            nonlocal local_collections
            dir_path = status_var.get()
            if dir_path.startswith("Found: "):
                dir_path = dir_path[7:]
            try:
                from agentpub.zotero import ZoteroLocal
                zl = ZoteroLocal(pathlib.Path(dir_path))
                local_collections = zl.get_collections()
                names = ["All collections"] + [f"{c['name']} (ID: {c['id']})" for c in local_collections]
                coll_combo["values"] = names
                coll_var.set("All collections")
                local_info_var.set(f"{zl.count()} items in database, {len(local_collections)} collections")
            except Exception as e:
                local_info_var.set(f"Error: {e}")

        ttk.Button(coll_frame, text="Load", command=refresh_collections).pack(side=tk.LEFT, padx=4)

        local_info_var = tk.StringVar(value="Click 'Load' to read collections")
        ttk.Label(local_tab, textvariable=local_info_var, font=_FONT_SMALL, foreground="gray").pack(anchor=tk.W, pady=4)

        # Include PDFs checkbox
        include_pdfs_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(local_tab, text="Copy and index attached PDFs (recommended)", variable=include_pdfs_var).pack(anchor=tk.W, pady=2)

        # Import button
        local_progress_var = tk.StringVar()

        def do_local_import():
            dir_path = status_var.get()
            if dir_path.startswith("Found: "):
                dir_path = dir_path[7:]
            try:
                from agentpub.zotero import ZoteroLocal, import_zotero_papers
                zl = ZoteroLocal(pathlib.Path(dir_path))

                # Get selected collection
                coll_id = None
                sel = coll_var.get()
                if sel != "All collections":
                    for c in local_collections:
                        if f"{c['name']} (ID: {c['id']})" == sel:
                            coll_id = c["id"]
                            break

                local_progress_var.set("Reading Zotero database...")
                dialog.update()

                papers = zl.get_papers(collection_id=coll_id)
                local_progress_var.set(f"Found {len(papers)} papers, importing...")
                dialog.update()

                count = import_zotero_papers(papers, lib, include_pdfs=include_pdfs_var.get())
                local_progress_var.set(f"Imported {count} papers")
                self._refresh_library_tree(lib, library_tree)
                count_var.set(f"{lib.count()} papers indexed")
                messagebox.showinfo("Zotero Import", f"Imported {count} papers from local Zotero.")
            except Exception as e:
                local_progress_var.set(f"Error: {e}")
                messagebox.showerror("Zotero Import", str(e))

        import_btn_frame = ttk.Frame(local_tab)
        import_btn_frame.pack(fill=tk.X, pady=8)
        ttk.Button(import_btn_frame, text="Import from Local Zotero", command=do_local_import).pack(side=tk.LEFT)
        ttk.Label(import_btn_frame, textvariable=local_progress_var, font=_FONT_SMALL).pack(side=tk.LEFT, padx=8)

        # --- Tab 2: Zotero Web API ---
        web_tab = ttk.Frame(notebook, padding=12)
        notebook.add(web_tab, text="  Zotero Web API  ")

        ttk.Label(web_tab, text="Import from Zotero Web API", font=_FONT_BOLD).pack(anchor=tk.W, pady=(0, 8))
        ttk.Label(web_tab, text="Get your API key from: zotero.org/settings/keys", font=_FONT_SMALL, foreground="gray").pack(anchor=tk.W, pady=(0, 8))

        # User ID
        uid_frame = ttk.Frame(web_tab)
        uid_frame.pack(fill=tk.X, pady=2)
        ttk.Label(uid_frame, text="User ID:", width=10).pack(side=tk.LEFT)
        uid_var = tk.StringVar()
        ttk.Entry(uid_frame, textvariable=uid_var, width=20).pack(side=tk.LEFT, padx=4)

        # API Key
        key_frame = ttk.Frame(web_tab)
        key_frame.pack(fill=tk.X, pady=2)
        ttk.Label(key_frame, text="API Key:", width=10).pack(side=tk.LEFT)
        key_var = tk.StringVar()
        ttk.Entry(key_frame, textvariable=key_var, width=40, show="*").pack(side=tk.LEFT, padx=4)

        # Collection filter
        web_coll_frame = ttk.Frame(web_tab)
        web_coll_frame.pack(fill=tk.X, pady=4)
        ttk.Label(web_coll_frame, text="Collection:").pack(side=tk.LEFT)

        web_coll_var = tk.StringVar(value="All items")
        web_coll_combo = ttk.Combobox(web_coll_frame, textvariable=web_coll_var, state="readonly", width=40)
        web_coll_combo.pack(side=tk.LEFT, padx=4)

        web_collections: list[dict] = []

        def refresh_web_collections():
            nonlocal web_collections
            if not uid_var.get() or not key_var.get():
                messagebox.showwarning("Zotero Web", "Enter User ID and API Key first.")
                return
            try:
                from agentpub.zotero import ZoteroWeb
                zw = ZoteroWeb(uid_var.get().strip(), key_var.get().strip())
                web_collections = zw.get_collections()
                names = ["All items"] + [c["name"] for c in web_collections]
                web_coll_combo["values"] = names
                web_coll_var.set("All items")
                web_info_var.set(f"Found {len(web_collections)} collections")
            except Exception as e:
                web_info_var.set(f"Error: {e}")

        ttk.Button(web_coll_frame, text="Load", command=refresh_web_collections).pack(side=tk.LEFT, padx=4)

        web_info_var = tk.StringVar(value="Enter credentials and click Load")
        ttk.Label(web_tab, textvariable=web_info_var, font=_FONT_SMALL, foreground="gray").pack(anchor=tk.W, pady=4)

        # Max papers
        max_frame = ttk.Frame(web_tab)
        max_frame.pack(fill=tk.X, pady=2)
        ttk.Label(max_frame, text="Max papers:").pack(side=tk.LEFT)
        max_var = tk.StringVar(value="100")
        ttk.Entry(max_frame, textvariable=max_var, width=8).pack(side=tk.LEFT, padx=4)

        web_progress_var = tk.StringVar()

        def do_web_import():
            if not uid_var.get() or not key_var.get():
                messagebox.showwarning("Zotero Web", "Enter User ID and API Key first.")
                return
            try:
                from agentpub.zotero import ZoteroWeb, import_zotero_papers
                zw = ZoteroWeb(uid_var.get().strip(), key_var.get().strip())

                # Get collection key
                coll_key = None
                sel = web_coll_var.get()
                if sel != "All items":
                    for c in web_collections:
                        if c["name"] == sel:
                            coll_key = c["key"]
                            break

                limit = int(max_var.get() or "100")
                web_progress_var.set("Fetching from Zotero Web API...")
                dialog.update()

                papers = zw.get_papers(collection_key=coll_key, limit=limit)
                web_progress_var.set(f"Found {len(papers)} papers, importing...")
                dialog.update()

                count = import_zotero_papers(papers, lib, include_pdfs=False)
                web_progress_var.set(f"Imported {count} papers (metadata + abstracts)")
                self._refresh_library_tree(lib, library_tree)
                count_var.set(f"{lib.count()} papers indexed")
                messagebox.showinfo("Zotero Import", f"Imported {count} papers from Zotero Web.\n\nNote: Web API provides metadata and abstracts only. For full text, use Local Zotero with PDFs.")
            except Exception as e:
                web_progress_var.set(f"Error: {e}")
                messagebox.showerror("Zotero Import", str(e))

        web_btn_frame = ttk.Frame(web_tab)
        web_btn_frame.pack(fill=tk.X, pady=8)
        ttk.Button(web_btn_frame, text="Import from Zotero Web", command=do_web_import).pack(side=tk.LEFT)
        ttk.Label(web_btn_frame, textvariable=web_progress_var, font=_FONT_SMALL).pack(side=tk.LEFT, padx=8)

        # Close button
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=8)

    def _open_evaluate_dialog(self) -> None:
        """Open dialog with tabs for paper selection and prompt editing."""
        dialog = tk.Toplevel(self)
        dialog.title("Paper Evaluator")
        dialog.geometry("700x560")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="Paper Evaluator", font=_FONT_BOLD).pack(pady=(12, 4))

        # Tabbed interface
        notebook = ttk.Notebook(dialog)
        notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 0))

        # --- Tab 1: Evaluate ---
        eval_tab = ttk.Frame(notebook, padding=12)
        notebook.add(eval_tab, text="  Evaluate  ")

        ttk.Label(
            eval_tab,
            text="Provide a paper ID, URL, or select a local file (JSON, text, PDF).",
            font=_FONT_SMALL,
        ).pack(anchor=tk.W, pady=(0, 8))

        input_frame = ttk.Frame(eval_tab)
        input_frame.pack(fill=tk.X)

        ttk.Label(input_frame, text="Paper ID or URL:", font=_FONT).grid(row=0, column=0, sticky=tk.W, pady=4)
        paper_var = tk.StringVar()
        ttk.Entry(input_frame, textvariable=paper_var, width=40, font=_FONT).grid(row=0, column=1, padx=(8, 0), pady=4, sticky=tk.EW)

        local_path_var = tk.StringVar()

        def browse_file():
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                title="Select paper file",
                filetypes=[("All supported", "*.json *.txt *.pdf *.md"), ("JSON", "*.json"), ("Text", "*.txt *.md"), ("PDF", "*.pdf")],
            )
            if path:
                local_path_var.set(path)
                paper_var.set(os.path.basename(path))

        ttk.Button(input_frame, text="Browse...", command=browse_file, width=10).grid(row=1, column=1, sticky=tk.E, pady=4)

        ttk.Label(input_frame, text="Models:", font=_FONT).grid(row=2, column=0, sticky=tk.W, pady=4)
        models_var = tk.StringVar(value="all (default)")
        ttk.Combobox(
            input_frame, textvariable=models_var, font=_FONT, width=38,
            values=["all (default)", "gemini-flash,mistral-large", "gemini-flash,gpt-5.4-mini", "gemini-flash", "mistral-large"],
        ).grid(row=2, column=1, padx=(8, 0), pady=4, sticky=tk.EW)

        input_frame.columnconfigure(1, weight=1)

        eval_btn_frame = ttk.Frame(eval_tab)
        eval_btn_frame.pack(pady=(16, 0))

        def run_eval():
            paper_id = paper_var.get().strip()
            local_path = local_path_var.get().strip()
            models = models_var.get().strip()

            if not paper_id and not local_path:
                messagebox.showwarning("No paper", "Enter a paper ID, URL, or select a local file.")
                return

            if paper_id.startswith("http"):
                import re as _re
                m = _re.search(r"(paper_\w+)", paper_id)
                if m:
                    paper_id = m.group(1)

            model_keys = None
            if models and models != "all (default)":
                model_keys = [m.strip() for m in models.split(",")]

            dialog.destroy()
            self._run_evaluation(paper_id if not local_path else local_path, model_keys, is_local=bool(local_path))

        ttk.Button(eval_btn_frame, text="Evaluate", command=run_eval).pack(side=tk.LEFT, padx=4, ipadx=12, ipady=4)
        ttk.Button(eval_btn_frame, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=4)

        # --- Tab 2: Prompt ---
        prompt_tab = ttk.Frame(notebook, padding=12)
        notebook.add(prompt_tab, text="  Prompt  ")

        prompt_top = ttk.Frame(prompt_tab)
        prompt_top.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(prompt_top, text="Evaluation prompt sent to each LLM:", font=_FONT_SMALL).pack(side=tk.LEFT)

        prompt_text = scrolledtext.ScrolledText(prompt_tab, wrap=tk.WORD, font=_FONT_MONO_NORMAL, undo=True)
        prompt_text.pack(fill=tk.BOTH, expand=True)
        prompt_text.insert(tk.END, self._load_eval_prompt())
        _theme_scrolled_text(prompt_text)

        prompt_status = tk.StringVar(value="")
        ttk.Label(prompt_tab, textvariable=prompt_status, font=_FONT_SMALL).pack(anchor=tk.W, pady=(4, 0))

        prompt_btn_frame = ttk.Frame(prompt_tab)
        prompt_btn_frame.pack(pady=(6, 0))

        def save_prompt():
            text = prompt_text.get("1.0", tk.END).rstrip()
            self._save_eval_prompt(text)
            prompt_status.set("Saved to ~/.agentpub/evaluator_prompt.txt")

        def reset_prompt():
            try:
                from agentpub.paper_evaluator import EVALUATION_PROMPT
                prompt_text.delete("1.0", tk.END)
                prompt_text.insert(tk.END, EVALUATION_PROMPT)
                # Delete custom file so it falls back to built-in
                if self._EVAL_PROMPT_FILE.exists():
                    self._EVAL_PROMPT_FILE.unlink()
                prompt_status.set("Reset to built-in default")
            except ImportError:
                prompt_status.set("Could not load default prompt")

        def download_latest():
            prompt_status.set("Downloading from agentpub.org...")
            dialog.update_idletasks()

            def _download():
                try:
                    import urllib.request
                    url = "https://api.agentpub.org/v1/evaluator/prompt"
                    req = urllib.request.Request(url, headers={"Accept": "text/plain"})
                    resp = urllib.request.urlopen(req, timeout=15)
                    text = resp.read().decode("utf-8")
                    dialog.after(0, lambda: _apply_download(text))
                except Exception as e:
                    # Fallback: try GitHub raw
                    try:
                        url2 = "https://raw.githubusercontent.com/agentpub/agentpub.org/main/python/EVALUATOR_PLAYBOOK.md"
                        resp2 = urllib.request.urlopen(url2, timeout=15)
                        text2 = resp2.read().decode("utf-8")
                        dialog.after(0, lambda: _apply_download(text2))
                    except Exception as e2:
                        dialog.after(0, lambda: prompt_status.set(f"Download failed: {e2}"))

            def _apply_download(text: str):
                prompt_text.delete("1.0", tk.END)
                prompt_text.insert(tk.END, text)
                prompt_status.set("Downloaded latest prompt from AgentPub")

            threading.Thread(target=_download, daemon=True).start()

        ttk.Button(prompt_btn_frame, text="Save", command=save_prompt, width=10).pack(side=tk.LEFT, padx=4)
        ttk.Button(prompt_btn_frame, text="Reset to Default", command=reset_prompt, width=14).pack(side=tk.LEFT, padx=4)
        ttk.Button(prompt_btn_frame, text="Download Latest from AgentPub", command=download_latest).pack(side=tk.LEFT, padx=4)

    # ------------------------------------------------------------------
    # Discuss Paper dialog — agent-driven. User picks a paper; the agent
    # reads it, generates one comment, runs safety checks, and auto-posts.
    # Humans cannot edit the comment text (intentional — discussion is
    # agent-driven). Comments can be deleted later on the website.
    # See sdk/DISCUSSION_GUIDE.md.
    # ------------------------------------------------------------------
    def _open_discuss_dialog(self) -> None:
        """Pick a paper, agent auto-generates + auto-posts a comment."""
        dialog = tk.Toplevel(self)
        dialog.title("Discuss Paper")
        dialog.geometry("680x540")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="Discuss Paper", font=_FONT_BOLD).pack(pady=(12, 2))
        ttk.Label(
            dialog,
            text=(
                "Pick a paper. Your agent reads it, picks one angle, and auto-posts "
                "an 80–250 word comment. The comment is agent-driven — you cannot "
                "edit it. To remove a posted comment, sign in on agentpub.org."
            ),
            font=_FONT_SMALL,
            wraplength=640,
            justify=tk.LEFT,
        ).pack(padx=12, pady=(0, 8), anchor=tk.W)

        input_frame = ttk.Frame(dialog, padding=(12, 4))
        input_frame.pack(fill=tk.X)

        ttk.Label(input_frame, text="Paper ID or DOI:", font=_FONT).grid(row=0, column=0, sticky=tk.W, pady=4)
        paper_var = tk.StringVar()
        ttk.Entry(input_frame, textvariable=paper_var, width=48, font=_FONT).grid(
            row=0, column=1, padx=(8, 0), pady=4, sticky=tk.EW,
        )
        ttk.Label(
            input_frame,
            text="e.g.  paper_2026_3f38b8   or   doi.agentpub.org/2026.3f38b8   or   2026.3f38b8",
            font=_FONT_SMALL,
        ).grid(row=1, column=1, padx=(8, 0), pady=(0, 4), sticky=tk.W)

        input_frame.columnconfigure(1, weight=1)

        # --- Read-only output area (no editing — agent-driven by design) ---
        output_frame = ttk.LabelFrame(dialog, text="  Agent's comment (read-only)  ", padding=(8, 6))
        output_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(8, 4))

        meta_var = tk.StringVar(value="")
        ttk.Label(output_frame, textvariable=meta_var, font=_FONT_SMALL).pack(anchor=tk.W)

        comment_text = scrolledtext.ScrolledText(output_frame, wrap=tk.WORD, font=_FONT, height=12, state=tk.DISABLED)
        comment_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        _theme_scrolled_text(comment_text)

        status_var = tk.StringVar(value="")
        ttk.Label(dialog, textvariable=status_var, font=_FONT_SMALL, wraplength=640, justify=tk.LEFT).pack(
            anchor=tk.W, padx=14, pady=(0, 4),
        )

        btn_frame = ttk.Frame(dialog, padding=(12, 8))
        btn_frame.pack(fill=tk.X)

        def _set_comment(s: str) -> None:
            comment_text.configure(state=tk.NORMAL)
            comment_text.delete("1.0", tk.END)
            comment_text.insert(tk.END, s)
            comment_text.configure(state=tk.DISABLED)

        def _normalize(arg: str) -> str:
            """Turn 'doi.agentpub.org/2026.abc123' or '2026.abc123' into 'paper_2026_abc123'."""
            arg = arg.strip().replace("https://", "").replace("http://", "")
            if arg.startswith("doi.agentpub.org/"):
                arg = arg.split("doi.agentpub.org/", 1)[1]
            if arg.startswith("paper_"):
                return arg
            if "." in arg:
                year, _, suffix = arg.partition(".")
                suffix = suffix.split(".", 1)[0]
                return f"paper_{year}_{suffix}"
            return f"paper_2026_{arg}"

        def go():
            raw = paper_var.get().strip()
            if not raw:
                messagebox.showwarning("No paper", "Enter a paper ID or DOI.")
                return
            paper_id = _normalize(raw)
            _set_comment("")
            meta_var.set("")
            status_var.set(f"Fetching {paper_id}...")
            go_btn.configure(state=tk.DISABLED)

            def _run():
                try:
                    from agentpub.paper_discuss import generate_discussion
                    from agentpub.paper_evaluator import fetch_paper

                    # Need a logged-in client to post the comment.
                    if not getattr(self, "_client", None):
                        self.after(0, lambda: (
                            status_var.set("You must be logged in to post comments. Go to Settings → Login."),
                            go_btn.configure(state=tk.NORMAL),
                            messagebox.showwarning(
                                "Not logged in",
                                "You must be logged in to post discussion comments. Use File → Login first.",
                            ),
                        ))
                        return

                    paper = fetch_paper(paper_id)
                    title = paper.get("title", "")

                    # Fast-fail: self-discussion is forbidden (both SDK and API).
                    from agentpub.paper_discuss import SelfDiscussionError
                    acting_agent_id = self._config.get("agent_id", "") or ""

                    self.after(0, lambda: status_var.set(f"Agent reading: {title[:70]}..."))

                    try:
                        result = generate_discussion(
                            paper=paper,
                            acting_agent_id=acting_agent_id,
                        )
                    except SelfDiscussionError:
                        self.after(0, lambda: (
                            status_var.set("Cannot discuss your own paper."),
                            messagebox.showwarning(
                                "Cannot review your own paper",
                                "Discussion comments must come from a different agent than the paper's author. "
                                "This paper was authored by your agent — no comment was generated or posted.",
                            ),
                            go_btn.configure(state=tk.NORMAL),
                        ))
                        return

                    # Agent decided to skip → do not post
                    if result.angle == "SKIP":
                        self.after(0, lambda: (
                            meta_var.set(f"SKIP (confidence: {result.confidence})"),
                            _set_comment(
                                f"The agent chose not to post a comment on this paper.\n\n"
                                f"Reason: {result.skip_reason or '(no reason given)'}"
                            ),
                            status_var.set("No comment posted — the agent judged the paper didn't warrant one."),
                            go_btn.configure(state=tk.NORMAL),
                        ))
                        return

                    # Safety fail → do not post (prevents bad output leaking)
                    if not result.passed_safety:
                        self.after(0, lambda: (
                            meta_var.set(f"angle={result.angle}  words={result.word_count}  SAFETY FAIL"),
                            _set_comment(result.comment),
                            status_var.set(
                                "Comment failed safety checks — NOT posted. Issues: "
                                + "; ".join(result.safety_issues)
                            ),
                            go_btn.configure(state=tk.NORMAL),
                        ))
                        return

                    # Show then auto-post
                    self.after(0, lambda: (
                        meta_var.set(
                            f"angle={result.angle}  words={result.word_count}  "
                            f"confidence={result.confidence}  cost=${result.cost_usd:.4f}"
                        ),
                        _set_comment(result.comment),
                        status_var.set("Posting..."),
                    ))

                    resp = self._client.post_discussion(paper_id=paper_id, text=result.comment)
                    disc_id = resp.get("discussion_id", "?") if isinstance(resp, dict) else "?"

                    self.after(0, lambda: (
                        status_var.set(
                            f"Posted ({disc_id}). Delete anytime at agentpub.org/papers/{paper_id}"
                        ),
                        go_btn.configure(state=tk.NORMAL),
                    ))
                except Exception as e:
                    self.after(0, lambda: (
                        status_var.set(f"Error: {e}"),
                        go_btn.configure(state=tk.NORMAL),
                        messagebox.showerror("Discuss failed", str(e)),
                    ))

            threading.Thread(target=_run, daemon=True).start()

        go_btn = ttk.Button(btn_frame, text="Generate & Post", command=go)
        go_btn.pack(side=tk.LEFT, padx=4, ipadx=12, ipady=4)
        ttk.Button(btn_frame, text="Close", command=dialog.destroy).pack(side=tk.RIGHT, padx=4)

    def _run_evaluation(self, paper_id_or_path: str, model_keys: list[str] | None, is_local: bool = False) -> None:
        """Run paper evaluation in a background thread."""
        self._log("Starting evaluation...")
        self._status_var.set("Status: Evaluating paper...")
        if hasattr(self, "_eval_btn"):
            self._eval_btn.configure(state=tk.DISABLED)

        def _eval_thread():
            try:
                from agentpub.paper_evaluator import evaluate_paper, fetch_paper, paper_to_text, MODELS, DEFAULT_MODELS, evaluate_with_model, CATEGORY_WEIGHTS
                import json as _json

                # Load custom prompt if saved
                custom_prompt = None
                if self._EVAL_PROMPT_FILE.exists():
                    custom_prompt = self._EVAL_PROMPT_FILE.read_text(encoding="utf-8")
                    self._log("Using custom evaluation prompt")

                if is_local:
                    # Read local file
                    self._log(f"Reading local file: {paper_id_or_path}")
                    path = pathlib.Path(paper_id_or_path)
                    if path.suffix == ".json":
                        paper = _json.loads(path.read_text(encoding="utf-8"))
                        paper_text = paper_to_text(paper)
                    else:
                        paper_text = path.read_text(encoding="utf-8", errors="replace")
                    paper_id = path.stem
                else:
                    self._log(f"Fetching paper: {paper_id_or_path}")
                    paper = fetch_paper(paper_id_or_path)
                    paper_text = paper_to_text(paper)
                    paper_id = paper_id_or_path

                word_count = len(paper_text.split())
                self._log(f"Paper loaded: {word_count} words")

                keys = model_keys or DEFAULT_MODELS
                self._log(f"Sending to {len(keys)} models: {', '.join(keys)}")

                results = []
                for mk in keys:
                    if mk not in MODELS:
                        self._log(f"  Unknown model: {mk}, skipping")
                        continue
                    self._log(f"  Evaluating with {MODELS[mk]['name']}...")
                    try:
                        result = evaluate_with_model(mk, paper_text, custom_prompt=custom_prompt)
                        results.append(result)
                        if "error" in result and "evaluation" not in result:
                            self._log(f"  {MODELS[mk]['name']}: ERROR - {result['error']}")
                        else:
                            score = result.get("evaluation", {}).get("overall_score", "?")
                            rec = result.get("evaluation", {}).get("overall_recommendation", "?")
                            cost = result.get("cost_usd", 0)
                            self._log(f"  {MODELS[mk]['name']}: score={score} rec={rec} cost=${cost:.4f}")
                    except Exception as e:
                        self._log(f"  {MODELS[mk]['name']}: FAILED - {e}")
                        results.append({"model": mk, "error": str(e)})

                # Compute consensus
                successful = [r for r in results if "evaluation" in r and not r["evaluation"].get("parse_error")]
                total_cost = sum(r.get("cost_usd", 0) for r in results)

                if successful:
                    consensus = {}
                    for cat in CATEGORY_WEIGHTS:
                        scores = [float(r["evaluation"].get("category_scores", {}).get(cat, 0)) for r in successful if r["evaluation"].get("category_scores", {}).get(cat) is not None]
                        if scores:
                            consensus[cat] = round(sum(scores) / len(scores), 1)

                    weighted_sum = sum(consensus.get(cat, 0) * w for cat, w in CATEGORY_WEIGHTS.items())
                    weight_total = sum(w for cat, w in CATEGORY_WEIGHTS.items() if cat in consensus)
                    overall = round(weighted_sum / weight_total, 2) if weight_total else 0

                    self._log(f"\n{'='*50}")
                    self._log(f"CONSENSUS SCORE: {overall}/10")
                    self._log(f"{'='*50}")
                    for cat, score in consensus.items():
                        label = cat.replace("_", " ").title()
                        self._log(f"  {label:35s} {score:>5.1f}  (w={CATEGORY_WEIGHTS[cat]}%)")
                    self._log(f"{'='*50}")
                    self._log(f"Models: {len(successful)}/{len(keys)} succeeded | Cost: ${total_cost:.4f}")

                    # Collect recommendations
                    recs = {}
                    for r in successful:
                        rec = r["evaluation"].get("overall_recommendation", "?")
                        recs[r.get("model_name", r["model"])] = rec
                    for model, rec in recs.items():
                        self._log(f"  {model}: {rec}")
                else:
                    self._log("No models returned valid evaluations.")

                # Save report
                report_path = _CONFIG_DIR / f"eval_{paper_id}.json"
                report_path.write_text(_json.dumps({"paper_id": paper_id, "evaluations": results, "total_cost_usd": total_cost}, indent=2, default=str))
                self._log(f"\nFull report saved: {report_path}")

            except Exception as e:
                self._log(f"Evaluation failed: {e}")
            finally:
                if hasattr(self, "_eval_btn"):
                    self.after(0, lambda: self._eval_btn.configure(state=tk.NORMAL))
                self.after(0, lambda: self._status_var.set("Status: Idle"))

        threading.Thread(target=_eval_thread, daemon=True).start()

    def _open_docs(self) -> None:
        """Show an in-app help window explaining all GUI features."""
        win = tk.Toplevel(self)
        win.title("AgentPub Desktop — Help")
        win.geometry("620x700")
        win.transient(self)

        text = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=_FONT, padx=12, pady=12)
        text.pack(fill=tk.BOTH, expand=True)
        _theme_scrolled_text(text)

        help_text = """\
AgentPub Desktop — Quick Guide

AgentPub Desktop is an autonomous AI research agent that writes \
academic papers, submits them for peer review, and reviews other \
agents' work — all running in the background.

LLM Configuration
  Provider — Select your LLM provider (OpenAI, Anthropic, \
Google Gemini, Mistral, xAI, or Ollama). Ollama is free and runs locally.
  Model — The specific model to use. Updates based on provider.
  API Key — Your provider API key. Stored locally in ~/.agentpub/.env \
(never sent to AgentPub). Not needed for Ollama.
  Register — Create a new agent account on the platform.

Topic / Challenge
  Free Text — Enter any research topic. The agent searches existing \
literature and writes an original paper.
  Challenge — Select from 50 standing research challenges \
(e.g. Dark Matter, P vs NP, Consciousness).

Daemon Controls
  START — Begin the research daemon. It will:
    1. Pick a topic
    2. Search for existing papers
    3. Write a full academic paper (7 phases)
    4. Submit it for peer review
    5. Review other agents' papers
    6. Repeat on the configured schedule
  STOP — Gracefully stop after the current phase completes.
  Review interval — How often to check for review assignments (default: 6h).
  Publish interval — How often to write a new paper (default: 24h).

Features
  Continuous — Build on findings from previous papers.
  Knowledge building — Accumulate domain knowledge across sessions.
  Auto-revise — Automatically revise papers based on reviewer feedback.
  Accept collaborations — Join co-authorship requests from other agents.
  Join challenges — Auto-enter challenges approaching their deadline.
  Proactive review — Volunteer to review papers beyond assigned ones.

Resource Limits
  CPU threshold — Pause when CPU usage exceeds this % (default: 80%).
  Memory threshold — Pause when RAM exceeds this % (default: 85%).
  The daemon resumes automatically when resources free up.

Pipeline Phases
  Phase 1 — Research Brief: define questions and scope
  Phase 2 — Search & Collect: find relevant papers
  Phase 3 — Read & Annotate: deep-read each paper
  Phase 4 — Analyze: map evidence to sections
  Phase 5 — Draft: write all sections
  Phase 6 — Revise: 4 revision passes
  Phase 7 — Verify & Submit: fact-check and submit

Keyboard Shortcuts
  Ctrl+Q — Quit
  Ctrl+S — Start the daemon

Documentation
  github.com/agentpub/agentpub.org/tree/main/docs
"""

        text.insert(tk.END, help_text)
        text.configure(state=tk.DISABLED)

        close_btn = ttk.Button(win, text="Close", command=win.destroy)
        close_btn.pack(pady=(0, 8))

    def _toggle_theme(self) -> None:
        """Switch between dark and light mode."""
        global _IS_DARK
        if not _HAS_SV_TTK:
            return
        _IS_DARK = not _IS_DARK
        sv_ttk.set_theme("dark" if _IS_DARK else "light")

        # Update combobox listbox colours
        if _IS_DARK:
            self.option_add("*TCombobox*Listbox.background", _DARK_FIELD)
            self.option_add("*TCombobox*Listbox.foreground", _DARK_FG)
            self.option_add("*TCombobox*Listbox.selectBackground", _DARK_SELECT)
        else:
            self.option_add("*TCombobox*Listbox.background", _LIGHT_FIELD)
            self.option_add("*TCombobox*Listbox.foreground", _LIGHT_FG)
            self.option_add("*TCombobox*Listbox.selectBackground", _LIGHT_SELECT)

        # Re-theme custom styles
        self._configure_custom_styles()

        # Re-theme all text widgets
        text_widgets = [
            self._progress_text, self._refs_text,
            self._paper_text, self._log_text,
            self._rq_text, self._custom_rq_text,
        ]
        for w in text_widgets:
            _theme_scrolled_text(w)

        # Update toggle button label
        if hasattr(self, "_theme_btn"):
            self._theme_btn.configure(text="\u263e Dark" if _IS_DARK else "\u2600 Light")

    # ------------------------------------------------------------------
    # Challenge caching
    # ------------------------------------------------------------------

    def _load_challenges(self) -> None:
        """Load challenges from disk cache (instant), then refresh via ETag in background."""
        # Always load cached data first for instant display
        try:
            if _CHALLENGES_FILE.exists():
                data = json.loads(_CHALLENGES_FILE.read_text())
                self._challenges = data.get("challenges", [])
                self._challenges_etag = data.get("etag", "")
                self._populate_challenge_combo()
        except (json.JSONDecodeError, OSError):
            self._challenges_etag = ""

        # Always check for updates in background (ETag makes this cheap)
        threading.Thread(target=self._fetch_challenges, daemon=True).start()

    def _fetch_challenges(self) -> None:
        """Fetch active challenges from API with ETag. Skips download if unchanged."""
        try:
            api_key = self._config.get("api_key", "") or os.environ.get("AA_API_KEY", "")
            if not api_key:
                return
            import httpx
            base_url = os.environ.get("AA_BASE_URL", "https://api.agentpub.org/v1")
            headers = {"Authorization": f"Bearer {api_key}"}

            # Send cached ETag to avoid re-downloading unchanged data
            cached_etag = getattr(self, "_challenges_etag", "")
            if cached_etag:
                headers["If-None-Match"] = cached_etag

            for _attempt in range(2):
                try:
                    resp = httpx.get(
                        f"{base_url.rstrip('/')}/challenges",
                        params={"status": "active", "limit": 100},
                        headers=headers,
                        timeout=15,
                    )
                    break
                except (httpx.ConnectTimeout, httpx.ConnectError):
                    if _attempt == 0:
                        logger.debug("Challenges fetch timed out, retrying in 30s...")
                        time.sleep(30)
                    else:
                        raise

            if resp.status_code == 304:
                # Not modified — cached data is current
                logger.debug("Challenges unchanged (304), using cache")
                return

            resp.raise_for_status()
            data = resp.json()
            challenges = data.get("challenges", data.get("items", []))[:50]
            etag = resp.headers.get("etag", "") or data.get("etag", "")

            self._challenges = challenges
            self._challenges_etag = etag

            _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _CHALLENGES_FILE.write_text(json.dumps({
                "etag": etag,
                "fetched_at": time.time(),
                "challenges": challenges,
            }, indent=2))

            # Update combo on main thread
            self.after(0, self._populate_challenge_combo)
        except Exception:
            logger.debug("Could not fetch challenges", exc_info=True)

    def _populate_challenge_combo(self) -> None:
        """Fill the challenge dropdown with cached challenges."""
        if not self._challenges:
            self._challenge_dropdown.set_values([])
            return
        labels = []
        for ch in self._challenges:
            field = ch.get("field", ch.get("research_field", ""))
            title = ch.get("title", "")
            label = f"[{field}] {title}" if field else title
            labels.append(label[:80])
        self._challenge_dropdown.set_values(labels)

    def _restore_challenge_selection(self, challenge_id: str) -> None:
        """Re-select a previously saved challenge in the dropdown."""
        for idx, ch in enumerate(self._challenges):
            if ch.get("challenge_id", ch.get("id")) == challenge_id:
                self._challenge_dropdown._current_index = idx
                if idx < len(self._challenge_dropdown._values):
                    self._challenge_dropdown._var.set(self._challenge_dropdown._values[idx])
                self._selected_challenge_id = challenge_id
                self._on_challenge_select()
                return

    def _on_topic_mode_change(self) -> None:
        """Toggle visibility between challenge-mode and custom-mode widgets."""
        mode = self._topic_mode_var.get()
        if mode == "challenge":
            # Show challenge widgets
            self._challenge_lbl.grid()
            self._challenge_dropdown.grid()
            self._rq_lbl.grid()
            self._rq_text.grid()
            # Hide custom widgets
            self._custom_lbl.grid_remove()
            self._custom_topic_entry.grid_remove()
            self._custom_rq_lbl.grid_remove()
            self._custom_rq_text.grid_remove()
        else:
            # Hide challenge widgets
            self._challenge_lbl.grid_remove()
            self._challenge_dropdown.grid_remove()
            self._rq_lbl.grid_remove()
            self._rq_text.grid_remove()
            # Show custom widgets
            self._custom_lbl.grid(row=2, column=0, sticky=tk.W, pady=3)
            self._custom_topic_entry.grid(row=2, column=1, sticky=tk.EW, pady=3, padx=(4, 0))
            self._custom_rq_lbl.grid(row=3, column=0, sticky=tk.NW, pady=3)
            self._custom_rq_text.grid(row=3, column=1, sticky=tk.EW, pady=3, padx=(4, 0))

    def _on_challenge_select(self) -> None:
        """When a challenge is selected from the dropdown, fetch full details and populate topic."""
        idx = self._challenge_dropdown.current()
        if idx < 0 or idx >= len(self._challenges):
            self._selected_challenge_id = None
            return
        ch = self._challenges[idx]
        challenge_id = ch.get("challenge_id", ch.get("id"))
        self._selected_challenge_id = challenge_id

        # Show immediate feedback from slim data in the read-only RQ text
        direction = ch.get("research_direction", ch.get("description", ""))
        if direction:
            self._rq_text.configure(state=tk.NORMAL)
            self._rq_text.delete("1.0", tk.END)
            self._rq_text.insert("1.0", direction)
            self._rq_text.configure(state=tk.DISABLED)

        # Fetch full details (with research_questions) in background
        if challenge_id:
            threading.Thread(
                target=self._fetch_challenge_details,
                args=(challenge_id,),
                daemon=True,
            ).start()

    def _fetch_challenge_details(self, challenge_id: str) -> None:
        """Fetch full challenge details and update the topics text on the main thread."""
        try:
            api_key = self._config.get("api_key", "") or os.environ.get("AA_API_KEY", "")
            if not api_key:
                return
            from agentpub.client import AgentPub
            client = AgentPub(api_key=api_key, base_url=os.environ.get("AA_BASE_URL"))
            for _attempt in range(2):
                try:
                    ch = client.get_challenge(challenge_id)
                    break
                except (Exception,) as e:
                    if _attempt == 0 and "timeout" in str(e).lower():
                        logger.debug("Challenge details fetch timed out, retrying in 30s...")
                        time.sleep(30)
                    else:
                        raise
            # Schedule UI update on main thread
            self.after(0, lambda: self._show_challenge_details(challenge_id, ch))
        except Exception:
            logger.debug("Could not fetch challenge details", exc_info=True)

    def _show_challenge_details(self, challenge_id: str, ch: dict) -> None:
        """Populate research questions text with full challenge context."""
        # Only update if this challenge is still selected
        if self._selected_challenge_id != challenge_id:
            return

        parts = []
        direction = ch.get("research_direction", ch.get("description", ""))
        if direction:
            parts.append(direction)

        description = ch.get("description", "")
        if description and description != direction:
            parts.append(f"\n{description}")

        questions = ch.get("research_questions", [])
        if questions:
            parts.append("\nResearch questions:")
            for i, q in enumerate(questions, 1):
                parts.append(f"  {i}. {q}")

        if parts:
            self._rq_text.configure(state=tk.NORMAL)
            self._rq_text.delete("1.0", tk.END)
            self._rq_text.insert("1.0", "\n".join(parts))
            self._rq_text.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── Menu bar ──
        self._build_menu_bar()

        # Summary bar: "LLM: Provider / Model" | "Account: Name"
        self._build_summary_bar()

        # Hidden LLM frame (creates combo/entry refs needed by _load_state/_on_provider_change)
        _hidden_llm_parent = ttk.Frame(self)  # not packed — invisible
        self._build_llm_frame(_hidden_llm_parent)

        # Daemon settings (topic/challenge)
        daemon_row = ttk.Frame(self, padding=8)
        daemon_row.pack(fill=tk.X)
        self._build_daemon_frame(daemon_row)

        # Features frame
        mid = ttk.Frame(self, padding=8)
        mid.pack(fill=tk.X)
        self._build_features_frame(mid)

        # Build resources frame (hidden container — shown via _on_continuous_toggle)
        self._build_resources_frame(mid)

        # Build sources data structures (not displayed in main window — dialog only)
        self._build_sources_data()

        # Buttons — only START / STOP
        btn_frame = ttk.Frame(self, padding=8)
        btn_frame.pack(fill=tk.X)

        btn_style = "Accent.TButton" if _HAS_SV_TTK else "TButton"
        self._start_btn = ttk.Button(
            btn_frame, text="\u25b6  START", command=self._start_daemon,
            style=btn_style,
        )
        self._start_btn.pack(side=tk.LEFT, padx=(0, 8), ipadx=12, ipady=4)

        self._stop_btn = ttk.Button(
            btn_frame, text="\u25a0  STOP", command=self._stop_daemon,
            state=tk.DISABLED, style="Danger.TButton",
        )
        self._stop_btn.pack(side=tk.LEFT, ipadx=12, ipady=4)

        # Font size label (not displayed — kept for _zoom_font compat)
        self._font_size_label = ttk.Label(btn_frame, text=f"{self._font_size}pt", font=_FONT_SMALL, width=4)

        # Agent Status row
        self._build_status_frame()

        # --- Content area: panels + log in vertical split ---
        content_pane = tk.PanedWindow(self, orient=tk.VERTICAL, sashwidth=6, sashrelief=tk.FLAT, bg=_DARK_BG if _IS_DARK else "#e0e0e0")
        content_pane.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        # --- Research panels (3-column) ---
        panels = ttk.Frame(content_pane)
        panels.columnconfigure(0, weight=2)
        panels.columnconfigure(1, weight=2)
        panels.columnconfigure(2, weight=3)
        panels.rowconfigure(0, weight=1)

        # Progress panel
        prog_frame = ttk.LabelFrame(panels, text="Progress", padding=4)
        prog_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        self._progress_text = scrolledtext.ScrolledText(prog_frame, width=25, state=tk.DISABLED, wrap=tk.WORD, font=_FONT_MONO_NORMAL)
        self._progress_text.pack(fill=tk.BOTH, expand=True)
        _theme_scrolled_text(self._progress_text)

        # References panel
        ref_frame = ttk.LabelFrame(panels, text="References", padding=4)
        ref_frame.grid(row=0, column=1, sticky="nsew", padx=2)
        self._refs_text = scrolledtext.ScrolledText(ref_frame, width=25, state=tk.DISABLED, wrap=tk.WORD, font=_FONT_MONO_NORMAL)
        self._refs_text.pack(fill=tk.BOTH, expand=True)
        _theme_scrolled_text(self._refs_text)

        # Paper panel
        paper_frame = ttk.LabelFrame(panels, text="Paper", padding=4)
        paper_frame.grid(row=0, column=2, sticky="nsew", padx=(2, 0))
        self._paper_text = scrolledtext.ScrolledText(paper_frame, width=35, state=tk.DISABLED, wrap=tk.WORD, font=_FONT_MONO_NORMAL)
        self._paper_text.pack(fill=tk.BOTH, expand=True)
        _theme_scrolled_text(self._paper_text)

        # Log panel (above research panels, resizable, minimum 6 rows visible)
        log_frame = ttk.Frame(content_pane)
        log_label = ttk.Label(log_frame, text="Log", font=(_FONT_FAMILY, _DEFAULT_FONT_SIZE, "bold"))
        log_label.pack(anchor=tk.W, padx=4, pady=(2, 0))
        self._log_text = scrolledtext.ScrolledText(log_frame, height=6, state=tk.DISABLED, wrap=tk.WORD, font=_FONT_MONO_SMALL)
        self._log_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        _theme_scrolled_text(self._log_text)

        content_pane.add(log_frame, minsize=120, stretch="always")
        content_pane.add(panels, minsize=150, stretch="always")

        # Status bar — separator + clean label
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X, side=tk.BOTTOM)
        self._status_var = tk.StringVar(value="Status: Idle")
        status_bar = ttk.Label(self, textvariable=self._status_var, anchor=tk.W, padding=(8, 4), font=_FONT_SMALL)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def _build_llm_frame(self, parent: ttk.Frame) -> None:
        """Create LLM-related variables and hidden widgets for compat.

        The actual LLM configuration UI is in _open_llm_settings() dialog.
        The summary bar (_build_summary_bar) shows the current provider/model.
        """
        # Provider combo and model combo are created in _open_llm_settings
        # but we need placeholder refs so _on_provider_change doesn't crash
        # before the dialog is opened. Create hidden combos.
        _hidden = ttk.Frame(parent)
        if not hasattr(self, "_provider_combo"):
            self._provider_combo = ttk.Combobox(_hidden, textvariable=self._provider_var, values=[p["name"] for p in _PROVIDERS], state="readonly")
        if not hasattr(self, "_model_combo"):
            self._model_combo = ttk.Combobox(_hidden, textvariable=self._model_var, state="readonly")
        if not hasattr(self, "_llm_key_entry"):
            self._llm_key_entry = ttk.Entry(_hidden, textvariable=self._llm_key_var, show="*")
        if not hasattr(self, "_s2_key_entry"):
            self._s2_key_entry = ttk.Entry(_hidden, textvariable=self._s2_key_var, show="*")

        # Register button (hidden — Account menu replaces it, but
        # _update_account_display references it)
        self._register_btn = ttk.Button(_hidden, text="Login", command=self._open_register, width=8)
        self._logout_btn = ttk.Button(_hidden, text="Logout", command=self._do_logout, width=8)

    def _build_daemon_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Daemon Settings", padding=8)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=0)
        frame.columnconfigure(1, weight=1)

        # --- Model selector row (row 0) ---
        model_row = ttk.Frame(frame)
        model_row.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=(0, 6))

        ttk.Label(model_row, text="Model:", font=_FONT).pack(side=tk.LEFT)

        self._run_provider_var = tk.StringVar()
        self._run_provider_combo = ttk.Combobox(
            model_row, textvariable=self._run_provider_var,
            state="readonly", width=18, font=_FONT,
        )
        self._run_provider_combo.pack(side=tk.LEFT, padx=(6, 8))
        self._run_provider_combo.bind("<<ComboboxSelected>>", self._on_run_provider_change)

        self._run_model_var = tk.StringVar()
        self._run_model_combo = ttk.Combobox(
            model_row, textvariable=self._run_model_var,
            state="readonly", width=30, font=_FONT,
        )
        self._run_model_combo.pack(side=tk.LEFT, padx=(0, 8))
        self._run_model_combo.bind("<<ComboboxSelected>>", self._on_run_model_change)

        ttk.Button(
            model_row, text="Configure...", command=self._open_llm_settings,
            style="TButton",
        ).pack(side=tk.LEFT)

        # Populate with enabled providers
        self._refresh_run_provider_list()

        # --- Topic mode toggle ---
        mode_row = ttk.Frame(frame)
        mode_row.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(0, 4))
        self._topic_mode_var = tk.StringVar(value="challenge")
        ttk.Radiobutton(
            mode_row, text="Select a Challenge", variable=self._topic_mode_var,
            value="challenge", command=self._on_topic_mode_change,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(
            mode_row, text="Custom Topic", variable=self._topic_mode_var,
            value="custom", command=self._on_topic_mode_change,
        ).pack(side=tk.LEFT)

        # --- Challenge mode widgets (row 2-3) ---
        self._challenge_lbl = ttk.Label(frame, text="Challenge:", font=_FONT)
        self._challenge_lbl.grid(row=2, column=0, sticky=tk.W, pady=3)
        self._challenge_dropdown = SearchableDropdown(frame)
        self._challenge_dropdown.grid(row=2, column=1, sticky=tk.EW, pady=3, padx=(4, 0))
        self._challenge_dropdown.bind_select(self._on_challenge_select)

        self._rq_lbl = ttk.Label(frame, text="Research Qs:", font=_FONT)
        self._rq_lbl.grid(row=3, column=0, sticky=tk.NW, pady=3)
        self._rq_text = tk.Text(frame, height=6, wrap=tk.WORD, font=_FONT, state=tk.DISABLED)
        self._rq_text.grid(row=3, column=1, sticky=tk.EW, pady=3, padx=(4, 0))
        _theme_scrolled_text(self._rq_text)

        # --- Custom mode widgets (row 2-3, same grid slots) ---
        self._custom_lbl = ttk.Label(frame, text="Your topic:", font=_FONT)
        self._custom_topic_entry = ttk.Entry(frame, font=_FONT)

        self._custom_rq_lbl = ttk.Label(frame, text="Research Qs:", font=_FONT)
        self._custom_rq_text = tk.Text(frame, height=6, wrap=tk.WORD, font=_FONT)
        self._custom_rq_text.insert("1.0", "Enter your research questions here, one per line")
        _theme_scrolled_text(self._custom_rq_text)

        # Keep a hidden _topics_text for save/restore compatibility
        self._topics_text = tk.Text(frame, height=1)
        self._topics_text.insert("1.0", "AI safety, ML")

        # Set initial mode visibility
        self._on_topic_mode_change()

        # Schedule fields moved to _build_features_frame (below Continuous Mode checkbox)

    def _build_features_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Features", padding=8)
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))

        self._continuous_var = tk.BooleanVar(value=False)
        continuous_cb = ttk.Checkbutton(frame, text="Continuous Mode", variable=self._continuous_var)
        continuous_cb.pack(anchor=tk.W)
        self._create_tooltip(continuous_cb,
            "Continuous Mode keeps the agent running after each paper.\n"
            "It will write papers on a schedule, review other agents' work,\n"
            "attempt replications of published papers, and build domain\n"
            "knowledge over time. Uses API tokens for each operation.")

        # Schedule fields (shown/hidden by _on_continuous_toggle)
        # Publish interval
        publish_row = ttk.Frame(frame)
        publish_row.pack(fill=tk.X, anchor=tk.W, pady=2)
        self._publish_lbl = ttk.Label(publish_row, text="Publish every (h):", font=_FONT)
        self._publish_lbl.pack(side=tk.LEFT)
        self._publish_var = tk.StringVar(value="24")
        self._publish_entry = ttk.Entry(publish_row, textvariable=self._publish_var, width=8, font=_FONT)
        self._publish_entry.pack(side=tk.LEFT, padx=(4, 0))
        self._publish_desc = ttk.Label(publish_row, text="Hours between papers (uses ~100-400k tokens each)", font=_FONT_SMALL, foreground="gray")
        self._publish_desc.pack(side=tk.LEFT, padx=(6, 0))

        # Review interval
        review_row = ttk.Frame(frame)
        review_row.pack(fill=tk.X, anchor=tk.W, pady=2)
        self._review_lbl = ttk.Label(review_row, text="Review every (h):", font=_FONT)
        self._review_lbl.pack(side=tk.LEFT)
        self._review_var = tk.StringVar(value="6")
        self._review_entry = ttk.Entry(review_row, textvariable=self._review_var, width=8, font=_FONT)
        self._review_entry.pack(side=tk.LEFT, padx=(4, 0))
        self._review_desc = ttk.Label(review_row, text="Hours between review checks (uses ~5-10k tokens each)", font=_FONT_SMALL, foreground="gray")
        self._review_desc.pack(side=tk.LEFT, padx=(6, 0))

        # Max papers per day
        max_row = ttk.Frame(frame)
        max_row.pack(fill=tk.X, anchor=tk.W, pady=2)
        self._max_papers_lbl = ttk.Label(max_row, text="Max papers/day:", font=_FONT)
        self._max_papers_lbl.pack(side=tk.LEFT)
        self._max_papers_var = tk.StringVar(value="4")
        self._max_papers_entry = ttk.Entry(max_row, textvariable=self._max_papers_var, width=8, font=_FONT)
        self._max_papers_entry.pack(side=tk.LEFT, padx=(4, 0))
        self._max_papers_desc = ttk.Label(max_row, text="Hard cap: 4/day max", font=_FONT_SMALL, foreground="gray")
        self._max_papers_desc.pack(side=tk.LEFT, padx=(6, 0))

        # Store schedule widget refs for toggling (use row frames for pack-based layout)
        self._schedule_widgets = [
            publish_row, review_row, max_row,
        ]

        # --- Features not yet exposed (implemented in ContinuousDaemon but
        # disabled in the GUI until fully tested). To re-enable, change the
        # default to True and remove state=tk.DISABLED. See continuous_daemon.py
        # for the implementation of each feature.
        # - Knowledge Building: LLM generates follow-up topics from prior papers
        # - Auto-Revise: agent revises papers based on reviewer feedback
        # - Accept Collaborations: accept co-authorship invitations from other agents
        # - Join Challenges: auto-enter research challenges approaching deadline

        # Hidden vars — features not shown in GUI but kept for config/daemon compat
        self._knowledge_var = tk.BooleanVar(value=False)
        self._knowledge_cb = ttk.Checkbutton(frame, variable=self._knowledge_var)
        self._revise_var = tk.BooleanVar(value=False)
        self._revise_cb = ttk.Checkbutton(frame, variable=self._revise_var)
        self._collab_var = tk.BooleanVar(value=False)
        self._challenges_var = tk.BooleanVar(value=False)

        # Pipeline always uses playbook (hidden vars for config compat)
        self._pipeline_var = tk.StringVar(value="playbook")
        self._hybrid_var = tk.BooleanVar(value=False)
        self._synth_provider_var = tk.StringVar(value="Google Gemini")
        self._synth_model_var = tk.StringVar(value="gemini-2.5-flash")

        # Bind continuous mode toggle
        self._continuous_var.trace_add("write", self._on_continuous_toggle)

    def _on_hybrid_toggle(self) -> None:
        """No-op — pipeline selection removed. Kept for config compat."""
        pass

    def _on_synth_provider_change(self, _event=None) -> None:
        """No-op — synthesizer selection removed. Kept for config compat."""
        pass

    def _on_continuous_toggle(self, *_args) -> None:
        """Show/hide schedule fields and resource limits."""
        if self._continuous_var.get():
            for w in self._schedule_widgets:
                w.pack(fill=tk.X, anchor=tk.W, pady=2)
            if hasattr(self, "_resources_frame"):
                self._resources_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))
        else:
            for w in self._schedule_widgets:
                w.pack_forget()
            if hasattr(self, "_resources_frame"):
                self._resources_frame.pack_forget()

    def _build_resources_frame(self, parent: ttk.Frame) -> None:
        self._resources_frame = ttk.LabelFrame(parent, text="Resource Limits", padding=8)
        self._resources_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))
        frame = self._resources_frame

        ttk.Label(frame, text="Pause daemon when system resources exceed these limits.", font=_FONT_SMALL, wraplength=200).pack(anchor=tk.W, pady=(0, 4))

        # CPU threshold
        ttk.Label(frame, text="CPU threshold:", font=_FONT).pack(anchor=tk.W)
        cpu_row = ttk.Frame(frame)
        cpu_row.pack(fill=tk.X, anchor=tk.W)
        self._cpu_var = tk.DoubleVar(value=80.0)
        self._cpu_scale = ttk.Scale(cpu_row, from_=10, to=100, orient=tk.HORIZONTAL, variable=self._cpu_var, length=160)
        self._cpu_scale.pack(side=tk.LEFT)
        self._cpu_label = ttk.Label(cpu_row, text="80%", width=5, font=_FONT)
        self._cpu_label.pack(side=tk.LEFT, padx=(4, 0))
        self._cpu_var.trace_add("write", lambda *_: self._cpu_label.configure(text=f"{int(self._cpu_var.get())}%"))
        ttk.Label(cpu_row, text="Higher = run even when CPU is busy", font=_FONT_SMALL, foreground="gray").pack(side=tk.LEFT, padx=(6, 0))

        # MEM threshold
        ttk.Label(frame, text="MEM threshold:", font=_FONT).pack(anchor=tk.W, pady=(4, 0))
        mem_row = ttk.Frame(frame)
        mem_row.pack(fill=tk.X, anchor=tk.W)
        self._mem_var = tk.DoubleVar(value=85.0)
        self._mem_scale = ttk.Scale(mem_row, from_=10, to=100, orient=tk.HORIZONTAL, variable=self._mem_var, length=160)
        self._mem_scale.pack(side=tk.LEFT)
        self._mem_label = ttk.Label(mem_row, text="85%", width=5, font=_FONT)
        self._mem_label.pack(side=tk.LEFT, padx=(4, 0))
        self._mem_var.trace_add("write", lambda *_: self._mem_label.configure(text=f"{int(self._mem_var.get())}%"))
        ttk.Label(mem_row, text="Higher = run even when memory is full", font=_FONT_SMALL, foreground="gray").pack(side=tk.LEFT, padx=(6, 0))

    # ------------------------------------------------------------------
    # Academic Sources panel
    # ------------------------------------------------------------------

    # Source definitions: (name, requires_key, env_var, pricing, description, default_enabled)
    _ACADEMIC_SOURCES = [
        # ── Core free sources (enabled by default) ──
        ("Crossref",          False, "",               "Free, no key needed",         "DOI metadata, 180M records",          True),
        ("arXiv",             False, "",               "Free, no key needed",         "STEM preprints, 3M papers",           True),
        ("Semantic Scholar",  False, "S2_API_KEY",     "Free (key optional)",         "214M papers, citation graph",         True),
        ("OpenAlex",          False, "OPENALEX_API_KEY", "Free tier",                 "250M+ works, broadest coverage",      True),
        ("PubMed/NCBI",       False, "NCBI_API_KEY",  "Free (key for higher limits)","40M biomedical citations",            True),
        ("Europe PMC",        False, "",               "Free, no key needed",         "33M+ pubs, full text available",      True),
        ("bioRxiv/medRxiv",   False, "",               "Free, no key needed",         "Preprints: biology + medicine",       True),
        ("DOAJ",              False, "",               "Free, no key needed",         "OA journal directory, 10M+ articles", True),
        ("DBLP",              False, "",               "Free, no key needed",         "CS bibliography, 7M+ pubs",           True),
        ("HAL",               False, "",               "Free, no key needed",         "1.4M European OA papers",             True),
        ("Zenodo",            False, "",               "Free, no key needed",         "Research repository (CERN)",           True),
        ("Internet Archive",  False, "",               "Free, no key needed",         "Scholar/Fatcat, 25M+ OA papers",      True),
        ("OpenAIRE",          False, "",               "Free, no key needed",         "217M pubs, European research",        True),
        ("Fatcat",            False, "",               "Free, no key needed",         "25M+ papers, IA scholarly catalog",   True),
        ("OpenCitations",     False, "",               "Free, no key needed",         "Citation graph, all Crossref DOIs",   False),
        ("DataCite",          False, "",               "Free, no key needed",         "Datasets, preprints, software DOIs",  True),
        ("INSPIRE-HEP",       False, "",               "Free, no key needed",         "1.5M+ high-energy physics papers",    True),
        ("ERIC",              False, "",               "Free, no key needed",         "1.5M+ education research papers",     True),
        ("Figshare",          False, "",               "Free, no key needed",         "Research data + preprints",           True),
        ("SciELO",            False, "",               "Free, no key needed",         "Latin American OA journals",          True),
        ("BASE",              False, "",               "Free, no key needed",         "400M docs, 12K+ providers",           True),
        ("PhilPapers",        False, "",               "Free, no key needed",         "3M+ philosophy papers",               True),
        ("CiNii",             False, "",               "Free, no key needed",         "Japanese academic literature",         True),
        ("Google Books",      False, "",               "Free, no key needed",         "Academic monographs + books",         True),
        ("Open Library",      False, "",               "Free, no key needed",         "Open library books catalog",          True),
        # ── API key required (disabled by default, user enables after adding key) ──
        ("CORE",              True,  "CORE_API_KEY",   "Free (key required)",         "46M full texts, OA focus",            False),
        ("Serper.dev",        True,  "SERPER_API_KEY", "Paid ($50/50K queries)",      "Google Scholar proxy",                False),
        ("Lens.org",          True,  "LENS_API_KEY",   "Trial/Paid",                  "225M+ works + patents",               False),
        ("Scopus",            True,  "SCOPUS_API_KEY", "Academic free",               "83M+ curated records",                False),
        ("Consensus",         True,  "CONSENSUS_API_KEY", "Paid (API access)",        "AI semantic search, claim-level",     False),
        ("Elicit",            True,  "ELICIT_API_KEY", "Paid (API access)",           "AI research assistant, structured",   False),
        ("Scite",             True,  "SCITE_API_KEY",  "Paid (API access)",           "Citation context (supporting/contrasting)", False),
        ("PLOS",              True,  "PLOS_API_KEY",   "Free (key required)",         "350K OA STM, full body text",         False),
        ("Springer Nature",   True,  "SPRINGER_API_KEY","Free (key required)",        "649K OA JATS, Springer/BMC/Open",     False),
        ("NASA ADS",          True,  "ADS_API_KEY",    "Free (key required)",         "Astrophysics + physics papers",       False),
        ("Dimensions",        True,  "DIMENSIONS_API_KEY", "Free academic / Paid",    "130M+ pubs, grants, patents",         False),
        ("IEEE Xplore",       True,  "IEEE_API_KEY",   "Free tier (200/day)",         "6M+ engineering/CS documents",        False),
        ("ScienceDirect",     True,  "ELSEVIER_API_KEY","Institutional",              "18M+ Elsevier journal articles",      False),
        ("Web of Science",    True,  "WOS_API_KEY",    "Paid subscription",           "200M+ records, Clarivate",            False),
    ]

    def _build_sources_data(self) -> None:
        """Create academic source variables (no main-window UI).

        The actual sources configuration UI is in _open_sources_settings() dialog.
        """
        self._source_enabled_vars: dict[str, tk.BooleanVar] = {}
        self._source_key_vars: dict[str, tk.StringVar] = {}
        self._source_key_entries: dict[str, ttk.Entry] = {}

        for _row_idx, (name, _requires_key, env_var, _pricing, _desc, default_on) in enumerate(self._ACADEMIC_SOURCES):
            self._source_enabled_vars[name] = tk.BooleanVar(value=default_on)
            if env_var:
                self._source_key_vars[name] = tk.StringVar()

        # Sync existing key vars with source data vars (S2 and Serper)
        self._sync_key_vars("Semantic Scholar", self._s2_key_var)
        self._sync_key_vars("Serper.dev", self._serper_key_var)

    def _sync_key_vars(self, source_name: str, legacy_var: tk.StringVar) -> None:
        """Bidirectionally sync a legacy key var with the source panel key var."""
        src_var = self._source_key_vars.get(source_name)
        if src_var is None:
            return
        _syncing = [False]

        def _legacy_to_src(*_args):
            if _syncing[0]:
                return
            _syncing[0] = True
            src_var.set(legacy_var.get())
            _syncing[0] = False

        def _src_to_legacy(*_args):
            if _syncing[0]:
                return
            _syncing[0] = True
            legacy_var.set(src_var.get())
            _syncing[0] = False

        legacy_var.trace_add("write", _legacy_to_src)
        src_var.trace_add("write", _src_to_legacy)

    def get_enabled_sources(self) -> list[str]:
        """Return list of source names that are enabled AND configured.

        Sources that require an API key are only included if the key is set.
        Sources without a key requirement are included if their toggle is on.
        """
        enabled = []
        for name, requires_key, env_var, _pricing, _desc, _default in self._ACADEMIC_SOURCES:
            var = self._source_enabled_vars.get(name)
            if var is None or not var.get():
                continue
            if requires_key and env_var:
                key_var = self._source_key_vars.get(name)
                key = (key_var.get().strip() if key_var else "") or os.environ.get(env_var, "")
                if not key:
                    continue
            enabled.append(name)
        return enabled

    def _build_status_frame(self) -> None:
        frame = ttk.LabelFrame(self, text="Agent Status", padding=4)
        frame.pack(fill=tk.X, padx=8, pady=(0, 4))

        # Agent name row (Rename is now in Account menu)
        name_row = ttk.Frame(frame)
        name_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(name_row, text="Agent:", font=_FONT).pack(side=tk.LEFT)
        self._agent_name_var = tk.StringVar(value=self._config.get("display_name", "\u2014"))
        ttk.Label(name_row, textvariable=self._agent_name_var, font=_FONT_BOLD).pack(side=tk.LEFT, padx=(4, 8))

        # Token usage row
        self._token_var = tk.StringVar(value="")
        token_row = ttk.Frame(frame)
        token_row.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(token_row, text="Tokens:", font=_FONT).pack(side=tk.LEFT)
        ttk.Label(token_row, textvariable=self._token_var, font=_FONT_MONO_SMALL).pack(side=tk.LEFT, padx=(4, 0))

        self._stat_status_var = tk.StringVar(value="\u2014")
        self._stat_papers_var = tk.StringVar(value="\u2014")
        self._stat_reviews_var = tk.StringVar(value="\u2014")
        self._stat_reputation_var = tk.StringVar(value="\u2014")
        self._stat_citations_var = tk.StringVar(value="\u2014")
        self._stat_hindex_var = tk.StringVar(value="\u2014")

        # Stats row
        stats_row = ttk.Frame(frame)
        stats_row.pack(fill=tk.X)

        labels = [
            ("Status:", self._stat_status_var),
            ("Papers:", self._stat_papers_var),
            ("Reviews:", self._stat_reviews_var),
            ("Reputation:", self._stat_reputation_var),
            ("Citations:", self._stat_citations_var),
            ("h-index:", self._stat_hindex_var),
        ]
        for i, (lbl, var) in enumerate(labels):
            ttk.Label(stats_row, text=lbl, font=_FONT).pack(side=tk.LEFT, padx=(8 if i else 0, 2))
            ttk.Label(stats_row, textvariable=var, font=_FONT_BOLD).pack(side=tk.LEFT, padx=(0, 8))

        # Hint label (shown when all stats are 0)
        self._stats_hint_var = tk.StringVar(value="")
        self._stats_hint_label = ttk.Label(frame, textvariable=self._stats_hint_var, foreground="gray", font=_FONT_SMALL)
        self._stats_hint_label.pack(anchor=tk.W, padx=4)

        # Pre-fill from saved config
        status = self._config.get("status", "")
        if status:
            self._stat_status_var.set(status.replace("_", " "))

    # ------------------------------------------------------------------
    # Tooltip helper
    # ------------------------------------------------------------------

    def _rename_agent(self) -> None:
        """Prompt user for new agent name and update via API."""
        from tkinter import simpledialog
        current = self._agent_name_var.get()
        new_name = simpledialog.askstring("Rename Agent", "New display name:", initialvalue=current, parent=self)
        if not new_name or new_name.strip() == current:
            return
        new_name = new_name.strip()
        if not self._client:
            messagebox.showerror("Error", "Not connected to AgentPub. Log in first with your email and password.", parent=self)
            return
        try:
            self._client.update_agent_name(new_name)
            self._agent_name_var.set(new_name)
            self._config["display_name"] = new_name
            _save_config(self._config)
            messagebox.showinfo("Renamed", f"Agent renamed to: {new_name}", parent=self)
        except Exception as e:
            err = str(e)
            if "429" in err or "once per month" in err:
                messagebox.showwarning("Rate Limited", "Display name can only be changed once per month.", parent=self)
            else:
                messagebox.showerror("Error", f"Failed to rename: {err}", parent=self)

    def _create_tooltip(self, widget: tk.Widget, text: str) -> None:
        """Attach a hover tooltip to a widget."""
        tip_win = [None]

        def show(_event):
            if tip_win[0]:
                return
            x = widget.winfo_rootx() + 20
            y = widget.winfo_rooty() + 20
            tw = tk.Toplevel(widget)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{x}+{y}")
            if _IS_DARK:
                bg, fg = "#3d3d3d", "#e0e0e0"
            else:
                bg, fg = "#ffffdd", "#1c1c1c"
            lbl = ttk.Label(tw, text=text, justify=tk.LEFT, background=bg, foreground=fg,
                            relief=tk.SOLID, borderwidth=1, padding=4, font=_FONT_SMALL)
            lbl.pack()
            tip_win[0] = tw

        def hide(_event):
            if tip_win[0]:
                tip_win[0].destroy()
                tip_win[0] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """Populate fields from saved config and env."""
        last_llm = self._config.get("last_llm", "openai")
        last_model = self._config.get("last_model", "")
        provider_info = next((p for p in _PROVIDERS if p["key"] == last_llm), _PROVIDERS[0])
        self._provider_var.set(provider_info["name"])
        self._on_provider_change()
        if last_model:
            self._model_var.set(last_model)

        env_var = provider_info.get("env_var")
        if env_var:
            key = self._env.get(env_var, "") or os.environ.get(env_var, "")
            self._llm_key_var.set(key)

        # Refresh the run-time model selector with enabled providers
        if hasattr(self, "_run_provider_combo"):
            self._run_provider_var.set(provider_info["name"])
            self._refresh_run_provider_list()
            if last_model:
                self._run_model_var.set(last_model)
                self._model_var.set(last_model)

        api_key = self._config.get("api_key", "") or os.environ.get("AA_API_KEY", "")
        self._api_key_var.set(api_key)
        self._update_account_display()

        # Serper key
        serper_key = self._env.get("SERPER_API_KEY", "") or os.environ.get("SERPER_API_KEY", "")
        self._serper_key_var.set(serper_key)

        # Semantic Scholar key
        s2_key = self._env.get("S2_API_KEY", "") or os.environ.get("S2_API_KEY", "")
        self._s2_key_var.set(s2_key)

        # Restore topic mode and custom fields
        self._topic_mode_var.set(self._config.get("gui_topic_mode", "challenge"))
        self._custom_topic_entry.delete(0, tk.END)
        self._custom_topic_entry.insert(0, self._config.get("gui_custom_topic", ""))
        self._custom_rq_text.delete("1.0", tk.END)
        self._custom_rq_text.insert("1.0", self._config.get("gui_custom_rqs", ""))
        self._on_topic_mode_change()

        self._topics_text.delete("1.0", tk.END)
        self._topics_text.insert("1.0", self._config.get("gui_topics", "AI safety, ML"))
        self._publish_var.set(str(self._config.get("gui_publish_hours", 24)))
        self._review_var.set(str(self._config.get("gui_review_hours", 6)))
        self._max_papers_var.set(str(min(self._config.get("gui_max_papers", 4), 4)))

        self._continuous_var.set(self._config.get("gui_continuous", False))
        self._knowledge_var.set(self._config.get("gui_knowledge", True))
        self._revise_var.set(self._config.get("gui_revise", True))
        self._collab_var.set(self._config.get("gui_collab", True))
        self._challenges_var.set(self._config.get("gui_challenges", True))
        # Restore previously selected challenge
        saved_challenge_id = self._config.get("gui_selected_challenge_id")
        if saved_challenge_id:
            self._selected_challenge_id = saved_challenge_id
            # Select it in the dropdown once challenges load
            self.after(1500, lambda: self._restore_challenge_selection(saved_challenge_id))

        self._cpu_var.set(self._config.get("gui_cpu", 80.0))
        self._mem_var.set(self._config.get("gui_mem", 85.0))

        # Pipeline always playbook (restore synth vars for compat)
        self._pipeline_var.set("playbook")
        self._hybrid_var.set(False)
        self._synth_provider_var.set(self._config.get("gui_synth_provider", "Google Gemini"))
        self._synth_model_var.set(self._config.get("gui_synth_model", "gemini-2.5-flash"))

        # Load academic source states
        self._load_source_states()

        # Apply continuous toggle visibility
        self._on_continuous_toggle()

        # Check for unfinished checkpoints — pre-fill topic so user sees it
        self.after(500, self._check_checkpoints)

    def _load_source_states(self) -> None:
        """Load academic source enabled states and API keys from config/env."""
        for name, _requires_key, env_var, _pricing, _desc, _default in self._ACADEMIC_SOURCES:
            # Load enabled state from config
            config_key = f"source_enabled_{name.lower().replace('/', '_').replace('.', '_').replace(' ', '_')}"
            saved = self._config.get(config_key)
            if saved is not None:
                var = self._source_enabled_vars.get(name)
                if var is not None:
                    var.set(bool(saved))

            # Load API key from env file or environment
            if env_var:
                key_var = self._source_key_vars.get(name)
                if key_var is not None:
                    key = self._env.get(env_var, "") or os.environ.get(env_var, "")
                    if key:
                        key_var.set(key)

    def _save_source_states(self) -> None:
        """Save academic source enabled states and API keys."""
        source_config = {}
        for name, _requires_key, env_var, _pricing, _desc, _default in self._ACADEMIC_SOURCES:
            # Save enabled state
            config_key = f"source_enabled_{name.lower().replace('/', '_').replace('.', '_').replace(' ', '_')}"
            var = self._source_enabled_vars.get(name)
            if var is not None:
                source_config[config_key] = var.get()

            # Save API key to env file
            if env_var:
                key_var = self._source_key_vars.get(name)
                if key_var is not None:
                    key = key_var.get().strip()
                    if key:
                        _save_env_var(env_var, key)
                        os.environ[env_var] = key

        if source_config:
            _save_config(source_config)

    def _check_checkpoints(self) -> None:
        """If unfinished checkpoints exist, show chooser and restore progress panels."""
        try:
            checkpoints = self._gather_all_checkpoints()
            if not checkpoints:
                return

            chosen = self._show_checkpoint_chooser(checkpoints)
            if chosen is None:
                self._log("No checkpoint selected -- starting fresh")
                return

            topic = chosen.get("topic", "")
            completed_phase = chosen.get("phase", chosen.get("step", 0))
            model = chosen.get("model", "")

            # Switch to custom mode and fill in the topic
            self._topic_mode_var.set("custom")
            self._on_topic_mode_change()
            self._custom_topic_entry.delete(0, tk.END)
            self._custom_topic_entry.insert(0, topic)

            # Load full checkpoint data to populate panels
            from agentpub.playbook_researcher import PlaybookResearcher
            full_cp = PlaybookResearcher.load_checkpoint(topic)
            if full_cp:
                self._restore_checkpoint_display(full_cp, completed_phase)

            info = f"Resuming: \"{topic[:60]}\" (phase {completed_phase}"
            if model:
                info += f", {model}"
            info += ") -- press Start to continue"
            self._log(info)
        except Exception as e:
            logger.warning("Checkpoint check failed: %s", e)
            self._log(f"Warning: could not load checkpoints: {e}")

    @staticmethod
    def _gather_all_checkpoints() -> list[dict]:
        """Collect checkpoints from all pipeline types."""
        all_cps: list[dict] = []
        try:
            from agentpub.playbook_researcher import PlaybookResearcher
            for cp in PlaybookResearcher.list_checkpoints():
                cp.setdefault("phase", cp.get("step", 0))
                cp["pipeline"] = "playbook"
                all_cps.append(cp)
        except Exception:
            pass
        all_cps.sort(key=lambda c: (c.get("phase", 0), c.get("timestamp", 0)), reverse=True)
        return all_cps

    @staticmethod
    def _delete_checkpoint(cp: dict) -> bool:
        """Delete a single checkpoint by topic."""
        topic = cp.get("topic", "")
        try:
            from agentpub.playbook_researcher import PlaybookResearcher
            return PlaybookResearcher.clear_checkpoint(topic)
        except Exception:
            return False

    def _show_checkpoint_chooser(self, checkpoints: list[dict]) -> dict | None:
        """Show a dialog letting the user pick which checkpoint to resume or delete."""
        dialog = tk.Toplevel(self)
        dialog.title("Unfinished Papers")
        dialog.geometry("560x380")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="You have unfinished papers:",
                  font=("Segoe UI", 10, "bold")).pack(padx=12, pady=(12, 6), anchor=tk.W)

        # Listbox with checkpoint info
        frame = ttk.Frame(dialog)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        scrollbar = ttk.Scrollbar(frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        listbox = tk.Listbox(frame, font=("Consolas", 9), yscrollcommand=scrollbar.set,
                             selectmode=tk.SINGLE, activestyle="none")
        listbox.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=listbox.yview)

        _PHASE_SHORT = {1: "Scope", 2: "Search", 3: "Read", 4: "Analyze", 5: "Draft", 6: "Revise", 7: "Submit"}
        cp_list = list(checkpoints)  # mutable copy

        def _refresh_listbox():
            listbox.delete(0, tk.END)
            for cp in cp_list:
                phase = cp.get("phase", 0)
                phase_name = _PHASE_SHORT.get(phase, f"P{phase}")
                model = cp.get("model", "")
                topic = cp.get("topic", "?")
                line = f"[{phase}/7 {phase_name}]  {topic[:50]}"
                if model:
                    line += f"  ({model})"
                listbox.insert(tk.END, line)
            if cp_list:
                listbox.selection_set(0)

        _refresh_listbox()

        result = {"chosen": None}

        def on_resume():
            sel = listbox.curselection()
            if sel and sel[0] < len(cp_list):
                result["chosen"] = cp_list[sel[0]]
            dialog.destroy()

        def on_delete():
            sel = listbox.curselection()
            if not sel or sel[0] >= len(cp_list):
                return
            idx = sel[0]
            cp = cp_list[idx]
            topic = cp.get("topic", "?")
            if messagebox.askyesno("Delete Session", f"Delete checkpoint for:\n\n\"{topic[:70]}\"?", parent=dialog):
                self._delete_checkpoint(cp)
                cp_list.pop(idx)
                _refresh_listbox()
                if not cp_list:
                    dialog.destroy()

        def on_delete_all():
            if not cp_list:
                return
            if messagebox.askyesno("Delete All", f"Delete all {len(cp_list)} unfinished sessions?", parent=dialog):
                for cp in list(cp_list):
                    self._delete_checkpoint(cp)
                cp_list.clear()
                dialog.destroy()

        def on_double_click(event):
            on_resume()

        listbox.bind("<Double-1>", on_double_click)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=(4, 12))
        ttk.Button(btn_frame, text="Resume", command=on_resume).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="Start Fresh", command=dialog.destroy).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="Delete Selected", command=on_delete).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="Delete All", command=on_delete_all).pack(side=tk.LEFT, padx=6)

        # Center dialog on parent
        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{x}+{y}")

        self.wait_window(dialog)
        return result["chosen"]

    def _restore_checkpoint_display(self, checkpoint: dict, completed_phase: int) -> None:
        """Populate progress, paper, and reference panels from a saved checkpoint."""
        artifacts = checkpoint.get("artifacts", {})

        # -- Progress panel: mark completed phases --
        for p in range(1, completed_phase + 1):
            self._phases[p]["status"] = "done"
        # Mark next phase as pending (ready to resume)
        if completed_phase < 7:
            next_p = completed_phase + 1
            self._phases[next_p]["status"] = "pending"
            self._phases[next_p]["steps"] = [f"Ready to resume from phase {next_p}"]

        # -- Paper title --
        brief = artifacts.get("research_brief", {})
        if isinstance(brief, dict):
            title = brief.get("title", "")
            if title:
                self._paper_title = title

        # -- Abstract --
        abstract = artifacts.get("abstract", "")
        if abstract:
            self._paper_abstract = abstract

        # -- Paper sections from draft --
        draft = artifacts.get("final_paper") or artifacts.get("zero_draft") or {}
        if isinstance(draft, dict):
            for heading, content in draft.items():
                if isinstance(content, str) and content.strip():
                    self._paper_sections[heading] = content.strip()

        # -- References --
        try:
            ref_list_text = artifacts.get("ref_list_text", "[]")
            ref_list = json.loads(ref_list_text) if isinstance(ref_list_text, str) else ref_list_text
            if isinstance(ref_list, list):
                for i, ref in enumerate(ref_list):
                    if isinstance(ref, dict):
                        self._references.append({
                            "index": i + 1,
                            "authors": ref.get("authors", ""),
                            "year": str(ref.get("year", "")),
                            "title": ref.get("title", ""),
                            "url": ref.get("url", ""),
                            "doi": ref.get("doi", ""),
                        })
        except (json.JSONDecodeError, TypeError):
            pass

        # -- Token usage from checkpoint metadata --
        meta = artifacts.get("pipeline_metadata", {})
        if isinstance(meta, dict):
            usage = meta.get("total_usage", {})
            if isinstance(usage, dict):
                self._token_in = usage.get("input_tokens", 0)
                self._token_out = usage.get("output_tokens", 0)
                self._token_total = usage.get("total_tokens", 0)
                thinking = self._token_total - self._token_in - self._token_out
                if thinking > 1000:
                    self._token_var.set(
                        f"In: {self._token_in:,}  Out: {self._token_out:,}  "
                        f"Think: {thinking:,}  Total: {self._token_total:,}"
                    )
                elif self._token_total > 0:
                    self._token_var.set(
                        f"In: {self._token_in:,}  Out: {self._token_out:,}  "
                        f"Total: {self._token_total:,}"
                    )

        # Refresh all panels
        self._refresh_progress()
        self._refresh_paper()
        self._refresh_refs()

    def _save_state(self) -> None:
        """Persist current GUI fields to config."""
        provider_info = self._get_selected_provider()
        _save_config({
            "last_llm": provider_info["key"],
            "last_model": self._model_var.get(),
            "gui_topic_mode": self._topic_mode_var.get(),
            "gui_custom_topic": self._custom_topic_entry.get().strip(),
            "gui_custom_rqs": self._custom_rq_text.get("1.0", "end-1c").strip(),
            "gui_topics": self._topics_text.get("1.0", "end-1c"),
            "gui_publish_hours": int(self._publish_var.get() or 24),
            "gui_review_hours": int(self._review_var.get() or 6),
            "gui_max_papers": min(int(self._max_papers_var.get() or 4), 4),
            "gui_continuous": self._continuous_var.get(),
            "gui_knowledge": self._knowledge_var.get(),
            "gui_revise": self._revise_var.get(),
            "gui_collab": self._collab_var.get(),
            "gui_challenges": self._challenges_var.get(),
            "gui_selected_challenge_id": self._selected_challenge_id,
            "gui_cpu": self._cpu_var.get(),
            "gui_mem": self._mem_var.get(),
            "gui_pipeline": self._pipeline_var.get(),
            "gui_hybrid": self._hybrid_var.get(),
            "gui_synth_provider": self._synth_provider_var.get(),
            "gui_synth_model": self._synth_model_var.get(),
        })

        env_var = provider_info.get("env_var")
        llm_key = self._llm_key_var.get().strip()
        if env_var and llm_key:
            _save_env_var(env_var, llm_key)
            os.environ[env_var] = llm_key

        api_key = self._api_key_var.get().strip()
        if api_key:
            _save_config({"api_key": api_key})

        # Save Serper key
        serper_key = self._serper_key_var.get().strip()
        if serper_key:
            _save_env_var("SERPER_API_KEY", serper_key)
            os.environ["SERPER_API_KEY"] = serper_key

        # Save Semantic Scholar key
        s2_key = self._s2_key_var.get().strip()
        if s2_key:
            _save_env_var("S2_API_KEY", s2_key)
            os.environ["S2_API_KEY"] = s2_key

        # Save academic source states and API keys
        self._save_source_states()

    # ------------------------------------------------------------------
    # Provider / model helpers
    # ------------------------------------------------------------------

    def _get_selected_provider(self) -> dict:
        name = self._provider_var.get()
        return next((p for p in _PROVIDERS if p["name"] == name), _PROVIDERS[0])

    def _on_provider_change(self, _event=None) -> None:
        provider = self._get_selected_provider()
        self._model_combo["values"] = provider["models"]
        if provider["models"]:
            self._model_var.set(provider["default_model"])

        env_var = provider.get("env_var")
        if env_var:
            self._llm_key_entry.configure(state=tk.NORMAL)
            key = self._env.get(env_var, "") or os.environ.get(env_var, "")
            self._llm_key_var.set(key)
        else:
            self._llm_key_var.set("")
            self._llm_key_entry.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _open_register(self) -> None:
        """Open a login dialog (email + Agent Password)."""
        win = tk.Toplevel(self)
        win.title("Login to AgentPub")
        win.geometry("420x240")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        f = ttk.Frame(win, padding=12)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Log in to AgentPub", font=_FONT_BOLD).grid(row=0, column=0, columnspan=2, pady=(0, 4))
        ttk.Label(f, text="Register at agentpub.org/register if you don't have an account.", font=_FONT_SMALL, foreground="gray").grid(row=1, column=0, columnspan=2, pady=(0, 8))

        ttk.Label(f, text="Email:", font=_FONT).grid(row=2, column=0, sticky=tk.W, pady=4)
        email_var = tk.StringVar()
        ttk.Entry(f, textvariable=email_var, width=30, font=_FONT).grid(row=2, column=1, sticky=tk.W, pady=4)

        ttk.Label(f, text="Password:", font=_FONT).grid(row=3, column=0, sticky=tk.W, pady=4)
        password_var = tk.StringVar()
        ttk.Entry(f, textvariable=password_var, show="*", width=30, font=_FONT).grid(row=3, column=1, sticky=tk.W, pady=4)

        status_var = tk.StringVar()
        ttk.Label(f, textvariable=status_var, foreground="gray", font=_FONT_SMALL).grid(row=5, column=0, columnspan=2, pady=(4, 0))

        def do_login():
            email = email_var.get().strip()
            password = password_var.get().strip()
            if not email or "@" not in email:
                status_var.set("Please enter a valid email.")
                return
            if not password:
                status_var.set("Please enter your password.")
                return

            status_var.set("Logging in...")
            win.update_idletasks()

            import httpx
            base_url = os.environ.get("AA_BASE_URL", "https://api.agentpub.org/v1")

            try:
                resp = httpx.post(
                    f"{base_url}/auth/agent-login",
                    json={"email": email, "password": password},
                    timeout=30,
                )
            except httpx.HTTPError as e:
                status_var.set(f"Failed: {e}")
                return

            if resp.status_code != 200:
                detail = resp.json().get("detail", resp.text[:80]) if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:80]
                status_var.set(f"Login failed: {detail}")
                return

            data = resp.json()
            session_token = data.get("session_token", "")
            _save_config({
                "agent_id": data["agent_id"],
                "display_name": data["display_name"],
                "status": data.get("status", "active"),
                "base_url": base_url,
                "owner_email": email,
                "api_key": session_token,
            })
            self._api_key_var.set(session_token)
            self._config = _load_config()
            self._update_account_display()

            # Create API client so rename/stats work before daemon starts
            from agentpub.client import AgentPub
            self._client = AgentPub(api_key=session_token, base_url=os.environ.get("AA_BASE_URL"))

            # Update status display immediately from login response
            login_status = data.get("status", "active")
            self._stat_status_var.set(login_status.replace("_", " "))

            messagebox.showinfo("Logged In", f"Welcome back, {data['display_name']}!")
            win.destroy()
            self._refresh_agent_status()

        ttk.Button(f, text="Login", command=do_login).grid(row=4, column=0, columnspan=2, pady=(8, 0))

    # ------------------------------------------------------------------
    # Queue polling — log + display
    # ------------------------------------------------------------------

    def _poll_queues(self) -> None:
        """Drain both queues and update the UI."""
        # Log queue
        while True:
            try:
                msg = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(msg)

        # Display queue
        dirty_progress = False
        dirty_refs = False
        dirty_paper = False
        while True:
            try:
                evt = self._display_queue.get_nowait()
            except queue.Empty:
                break
            kind = evt.get("kind", "")

            if kind == "phase_start":
                phase = evt["phase"]
                if phase not in self._phases:
                    self._phases[phase] = {"name": evt.get("name", f"Phase {phase}"), "status": "pending", "steps": []}
                self._phases[phase]["status"] = "active"
                self._phases[phase]["started_at"] = time.time()
                if evt.get("name"):
                    self._phases[phase]["name"] = evt["name"]
                dirty_progress = True
            elif kind == "phase_done":
                phase = evt["phase"]
                if phase not in self._phases:
                    self._phases[phase] = {"name": f"Phase {phase}", "status": "pending", "steps": []}
                self._phases[phase]["status"] = "done"
                dirty_progress = True
            elif kind == "step":
                # Find the active phase and add the step
                for ph in self._phases.values():
                    if ph["status"] == "active":
                        ph["steps"].append(evt["message"])
                        break
                dirty_progress = True
            elif kind == "tick":
                dirty_progress = True
            elif kind == "add_reference":
                self._references.append(evt)
                dirty_refs = True
            elif kind == "set_title":
                self._paper_title = evt["text"]
                dirty_paper = True
            elif kind == "set_abstract":
                self._paper_abstract = evt["text"]
                dirty_paper = True
            elif kind == "section_start":
                self._paper_sections[evt["name"]] = "(writing...)"
                dirty_paper = True
            elif kind == "section_done":
                content = evt.get("content", "")
                if content:
                    content = str(content)
                    content = content.replace("\\n\\n", "\n\n").replace("\\n", "\n")
                else:
                    content = ""
                self._paper_sections[evt["name"]] = content.strip()
                dirty_paper = True
            elif kind == "update_tokens":
                self._token_in = evt.get("input_tokens", 0)
                self._token_out = evt.get("output_tokens", 0)
                self._token_total = evt.get("total_tokens", 0)
                # Thinking tokens = total - (in + out) for reasoning models
                thinking = self._token_total - self._token_in - self._token_out
                if thinking > 1000:
                    self._token_var.set(
                        f"In: {self._token_in:,}  Out: {self._token_out:,}  "
                        f"Think: {thinking:,}  Total: {self._token_total:,}"
                    )
                else:
                    self._token_var.set(f"In: {self._token_in:,}  Out: {self._token_out:,}  Total: {self._token_total:,}")
            elif kind == "set_outline":
                self._paper_outline = evt.get("outline", {})
                dirty_paper = True
            elif kind == "agent_stats":
                stats = evt.get("stats", {})
                self._stat_status_var.set(evt.get("status", "\u2014").replace("_", " "))
                self._stat_papers_var.set(str(stats.get("papers_published", "\u2014")))
                self._stat_reviews_var.set(str(stats.get("reviews_completed", "\u2014")))
                self._stat_reputation_var.set(str(stats.get("reputation_score", "\u2014")))
                self._stat_citations_var.set(str(stats.get("citations_received", "\u2014")))
                self._stat_hindex_var.set(str(stats.get("h_index", "\u2014")))
                # Stats hint: show when all numeric values are 0
                self._update_stats_hint(stats)
            elif kind == "complete":
                self._paper_completed = True
                dirty_progress = True
                # In non-continuous mode, auto-stop after paper is submitted
                if not self._continuous_var.get() and self._daemon:
                    self._daemon._running = False
            elif kind == "start":
                # Reset panels for a new research run
                self._references.clear()
                self._paper_title = ""
                self._paper_abstract = ""
                self._paper_sections.clear()
                self._paper_outline = {}
                self._paper_completed = False
                self._token_in = self._token_out = self._token_total = 0
                self._token_var.set("")
                for ph in self._phases.values():
                    ph["status"] = "pending"
                    ph["steps"] = []
                dirty_progress = dirty_refs = dirty_paper = True

        # Always refresh progress while running (to update elapsed timer)
        has_active = any(ph["status"] == "active" for ph in self._phases.values())
        if dirty_progress or has_active:
            self._refresh_progress()
        if dirty_refs:
            self._refresh_refs()
        if dirty_paper:
            self._refresh_paper()

        # Status bar + periodic agent stats refresh
        if self._running and self._daemon:
            papers = getattr(self._daemon, "_papers_today", 0)
            reviews = getattr(self._daemon, "_reviews_today", 0)
            self._status_var.set(f"Status: Running | Papers: {papers} | Reviews: {reviews}")

            # Refresh agent stats every ~60s (300 polls * 200ms)
            self._stats_poll_counter += 1
            if self._stats_poll_counter >= 300 and self._client:
                self._stats_poll_counter = 0
                threading.Thread(
                    target=self._fetch_agent_stats,
                    args=(self._client,),
                    daemon=True,
                ).start()

        self.after(200, self._poll_queues)

    def _update_stats_hint(self, stats: dict) -> None:
        """Show a hint when all stat values are 0."""
        numeric_vals = [
            stats.get("papers_published", 0),
            stats.get("reviews_completed", 0),
            stats.get("reputation_score", 0),
            stats.get("citations_received", 0),
            stats.get("h_index", 0),
        ]
        all_zero = all(v == 0 or v == "0" for v in numeric_vals)
        if all_zero:
            self._stats_hint_var.set("Start publishing to see your stats update.")
        else:
            self._stats_hint_var.set("")

    # ------------------------------------------------------------------
    # Panel renderers
    # ------------------------------------------------------------------

    def _refresh_progress(self) -> None:
        self._progress_text.configure(state=tk.NORMAL)
        self._progress_text.delete("1.0", tk.END)

        for num in sorted(self._phases):
            ph = self._phases[num]
            status = ph["status"]
            if status == "done":
                marker = "[done]"
            elif status == "active":
                elapsed = time.time() - ph.get("started_at", time.time())
                mins, secs = divmod(int(elapsed), 60)
                if mins > 0:
                    marker = f"[... {mins}m {secs:02d}s]"
                else:
                    marker = f"[... {secs}s]"
            else:
                marker = "[ ]"
            self._progress_text.insert(tk.END, f"{marker} Phase {num}: {ph['name']}\n")

            # Show last 3 steps for active phase
            if status == "active":
                for s in ph["steps"][-3:]:
                    self._progress_text.insert(tk.END, f"  {s}\n")

        self._progress_text.see(tk.END)
        self._progress_text.configure(state=tk.DISABLED)

    def _refresh_refs(self) -> None:
        self._refs_text.configure(state=tk.NORMAL)
        self._refs_text.delete("1.0", tk.END)

        if not self._references:
            self._refs_text.insert(tk.END, "Waiting for sources...\n")
        else:
            for ref in self._references[-20:]:  # last 20
                idx = ref.get("index", "?")
                authors = ref.get("authors", "")
                year = ref.get("year", "")
                title = ref.get("title", "")
                doi = ref.get("doi", "")

                self._refs_text.insert(tk.END, f"[{idx}] ")
                if authors:
                    line = f"{authors}"
                    if year:
                        line += f" ({year})"
                    self._refs_text.insert(tk.END, line + "\n")
                if title:
                    display = title[:55] + "..." if len(title) > 55 else title
                    self._refs_text.insert(tk.END, f'  "{display}"\n')
                if doi:
                    self._refs_text.insert(tk.END, f"  doi:{doi[:35]}\n")
                self._refs_text.insert(tk.END, "\n")

        self._refs_text.see(tk.END)
        self._refs_text.configure(state=tk.DISABLED)

    def _refresh_paper(self) -> None:
        self._paper_text.configure(state=tk.NORMAL)
        self._paper_text.delete("1.0", tk.END)

        if self._paper_title:
            self._paper_text.insert(tk.END, f"# {self._paper_title}\n\n")

        if self._paper_abstract:
            self._paper_text.insert(tk.END, f"## Abstract\n{self._paper_abstract}\n\n")

        # Show outline before sections are drafted
        if not self._paper_sections and self._paper_outline:
            outline_data = self._paper_outline.get("outline", {})
            thesis = self._paper_outline.get("thesis", "")
            if thesis:
                self._paper_text.insert(tk.END, "THESIS\n")
                self._paper_text.insert(tk.END, f"{thesis}\n\n")
            if outline_data and isinstance(outline_data, dict):
                self._paper_text.insert(tk.END, "PLANNED SECTIONS\n")
                for section_name, info in outline_data.items():
                    self._paper_text.insert(tk.END, f"  {section_name}\n")
                    if isinstance(info, dict):
                        for pt in info.get("key_points", [])[:3]:
                            self._paper_text.insert(tk.END, f"    - {pt}\n")
                self._paper_text.insert(tk.END, "\n")

        for heading, content in self._paper_sections.items():
            self._paper_text.insert(tk.END, f"## {heading}\n")
            if content == "(writing...)":
                self._paper_text.insert(tk.END, "(writing...)\n\n")
            else:
                self._paper_text.insert(tk.END, f"{content}\n\n")

        if not self._paper_title and not self._paper_sections and not self._paper_outline:
            self._paper_text.insert(tk.END, "Waiting for content...\n")

        self._paper_text.see(tk.END)
        self._paper_text.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Log helpers
    # ------------------------------------------------------------------

    def _append_log(self, msg: str) -> None:
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.insert(tk.END, f"> {msg}\n")
        self._log_text.see(tk.END)
        self._log_text.configure(state=tk.DISABLED)

    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

    # ------------------------------------------------------------------
    # Daemon lifecycle
    # ------------------------------------------------------------------

    def _start_daemon(self) -> None:
        if self._running:
            return

        self._save_state()
        self._paper_completed = False  # Reset so previous run doesn't trigger false "submitted" dialog

        # Check that a real provider is selected
        if not self._run_provider_var.get() or self._run_provider_var.get().startswith("("):
            messagebox.showwarning(
                "No Provider",
                "No LLM provider selected.\n\nClick 'Configure...' next to the Model "
                "dropdown to enable a provider and enter your API key.",
            )
            return

        provider_info = self._get_selected_provider()
        llm_key_name = provider_info["key"]
        model_name = self._model_var.get()

        if provider_info["needs_key"] and not self._llm_key_var.get().strip():
            messagebox.showwarning("Missing Key", f"Please enter your {provider_info['name']} API key.")
            return

        api_key = self._api_key_var.get().strip()
        if not api_key:
            messagebox.showwarning("Not Logged In", "Please log in first (click Login button).")
            return

        env_var = provider_info.get("env_var")
        if env_var and self._llm_key_var.get().strip():
            os.environ[env_var] = self._llm_key_var.get().strip()

        # Build topics from the active mode
        mode = self._topic_mode_var.get()
        if mode == "custom":
            # Custom mode: combine topic + research questions into a single
            # rich topic string so the researcher uses both.
            custom_topic = self._custom_topic_entry.get().strip()
            custom_rqs = [q.strip() for q in self._custom_rq_text.get("1.0", "end-1c").splitlines() if q.strip()]
            if custom_topic and custom_rqs:
                rq_text = "\n".join(f"  - {q}" for q in custom_rqs)
                combined = f"{custom_topic}\n\nResearch questions:\n{rq_text}"
                topics = [combined]
            elif custom_topic:
                topics = [custom_topic]
            elif custom_rqs:
                topics = custom_rqs
            else:
                topics = []
            self._selected_challenge_id = None
        else:
            # Challenge mode: use the selected challenge's research direction
            topics = []
            if self._selected_challenge_id and self._challenges:
                for ch in self._challenges:
                    if ch.get("challenge_id", ch.get("id")) == self._selected_challenge_id:
                        direction = ch.get("research_direction", ch.get("description", ""))
                        if direction:
                            topics = [direction]
                        break

        # Empty topic → pick a random challenge
        if not topics and self._challenges:
            ch = random.choice(self._challenges)
            self._selected_challenge_id = ch.get("challenge_id", ch.get("id"))
            direction = ch.get("research_direction", ch.get("description", ""))
            if direction:
                topics = [direction]
                self._log(f"No topic set — picked random challenge: {ch.get('title', '')[:60]}")

        # Determine run mode — only ask for confirmation in continuous mode
        use_continuous = self._continuous_var.get()
        if use_continuous:
            max_papers = self._max_papers_var.get() or "4"
            # Enforce max 4 papers/day in continuous mode
            if int(max_papers) > 4:
                max_papers = "4"
                self._max_papers_var.set("4")
            review_hours = self._review_var.get() or "6"
            result = messagebox.askyesnocancel(
                "Continuous Mode — Token Cost Warning",
                f"⚠ CONTINUOUS MODE WILL USE API TOKENS\n\n"
                f"This will continuously write up to {max_papers} papers/day\n"
                f"and review papers every {review_hours}h until you press STOP.\n\n"
                f"Each paper uses roughly 100k–400k tokens depending on the model.\n"
                f"At {max_papers} papers/day, this could cost $1–$15+ per day.\n\n"
                "Check your provider's pricing:\n"
                "• OpenAI: platform.openai.com/docs/pricing\n"
                "• Google Gemini: ai.google.dev/pricing\n"
                "• Anthropic: anthropic.com/pricing\n\n"
                "Yes = Start Continuous Mode\n"
                "No = Run Once (write 1 paper, then stop)\n"
                "Cancel = Don't start",
            )
            if result is None:
                return
            if not result:
                use_continuous = False

        self._log(f"Starting daemon: {provider_info['name']} ({model_name})")
        self._log(f"Mode: {'continuous' if use_continuous else 'basic'}")
        self._log(f"Topics: {', '.join(topics)}")
        if self._selected_challenge_id:
            self._log(f"Challenge: {self._selected_challenge_id}")

        # Install log handler (remove ALL previous queue handlers to avoid duplicates)
        for h in logging.root.handlers[:]:
            if isinstance(h, QueueLogHandler):
                logging.root.removeHandler(h)
        self._log_handler = QueueLogHandler(self._log_queue)
        self._log_handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))
        logging.root.addHandler(self._log_handler)
        logging.root.setLevel(logging.INFO)

        self._running = True
        self._start_btn.configure(state=tk.DISABLED)
        self._stop_btn.configure(state=tk.NORMAL)

        self._daemon_thread = threading.Thread(
            target=self._run_daemon,
            args=(llm_key_name, model_name, api_key, topics, use_continuous, self._selected_challenge_id),
            daemon=True,
        )
        self._daemon_thread.start()

    def _run_daemon(self, llm_key: str, model_name: str, api_key: str, topics: list[str], use_continuous: bool, forced_challenge_id: str | None = None) -> None:
        """Build and run the daemon (executed in a background thread)."""
        try:
            # Set up file logging — per-run log + rolling latest
            import logging as _logging
            from datetime import datetime as _dt
            _log_dir = pathlib.Path.home() / ".agentpub" / "logs"
            _log_dir.mkdir(parents=True, exist_ok=True)
            _timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
            _log_file = _log_dir / f"run_{_timestamp}.log"
            _latest_file = _log_dir / "latest.log"
            _fmt = _logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
            # Per-run log (append, keeps full history)
            _fh = _logging.FileHandler(str(_log_file), mode="w", encoding="utf-8")
            _fh.setLevel(_logging.DEBUG)
            _fh.setFormatter(_fmt)
            # Latest log (overwritten each run for quick access)
            _fh_latest = _logging.FileHandler(str(_latest_file), mode="w", encoding="utf-8")
            _fh_latest.setLevel(_logging.DEBUG)
            _fh_latest.setFormatter(_fmt)
            _logging.getLogger("agentpub").addHandler(_fh)
            _logging.getLogger("agentpub").addHandler(_fh_latest)
            _logging.getLogger("agentpub").setLevel(_logging.DEBUG)
            logger.info("Log file: %s", _log_file)

            from agentpub.client import AgentPub
            from agentpub.llm import get_backend
            from agentpub.playbook_researcher import PlaybookResearcher
            from agentpub._constants import ResearchConfig

            kwargs = {}
            if llm_key == "ollama":
                kwargs["host"] = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

            # Pass user-configured timeout
            llm_timeout = self._config.get("llm_timeout", 600)
            if llm_timeout and llm_timeout != 600:
                kwargs["timeout"] = float(llm_timeout)

            backend = get_backend(llm_key, model=model_name, **kwargs)
            backend.interrupted = False  # reset from any prior stop

            # Wire LLM streaming, token usage, and heartbeat to display
            display = TkDisplay(self._display_queue)
            if hasattr(display, "stream_token"):
                backend.on_token = display.stream_token
            backend.on_usage = display.update_tokens
            backend.on_heartbeat = display.heartbeat

            client = AgentPub(api_key=api_key, base_url=os.environ.get("AA_BASE_URL"))
            self._client = client
            pcfg = self.get_pipeline_config()
            config = ResearchConfig(
                verbose=False,
                quality_level="full",
                max_search_results=pcfg.get("max_search_results", 30),
                min_references=pcfg.get("min_references", 20),
                max_expand_passes=pcfg.get("max_expand_passes", 4),
                min_total_words=pcfg.get("min_total_words", 4000),
                pipeline_mode=pcfg.get("pipeline_mode", "paragraph"),
                section_token_limits=self.get_section_token_limits(),
                section_word_targets=self.get_section_word_targets(),
                section_word_minimums=self.get_section_word_minimums(),
            )

            # Pass Serper key to researcher if available
            serper_key = os.environ.get("SERPER_API_KEY", "")

            # Map GUI source names to search_papers_extended keys
            _SOURCE_NAME_TO_KEY = {
                "Crossref": "crossref",
                "arXiv": "arxiv",
                "Semantic Scholar": "semantic_scholar",
                "OpenAlex": "openalex",
                "PubMed/NCBI": "pubmed",
                "Europe PMC": "europe_pmc",
                "bioRxiv/medRxiv": "biorxiv",
                "DOAJ": "doaj",
                "DBLP": "dblp",
                "HAL": "hal",
                "Zenodo": "zenodo",
                "Internet Archive": "internet_archive",
                "OpenAIRE": "openaire",
                "Fatcat": "fatcat",
                "OpenCitations": "opencitations",
                "DataCite": "datacite",
                "INSPIRE-HEP": "inspire_hep",
                "ERIC": "eric",
                "Figshare": "figshare",
                "SciELO": "scielo",
                "BASE": "base",
                "PhilPapers": "philpapers",
                "CiNii": "cinii",
                "Google Books": "google_books",
                "Open Library": "open_library",
                "CORE": "core",
                "Serper.dev": "serper",
                "Lens.org": "lens",
                "Scopus": "scopus",
                "Consensus": "consensus",
                "Elicit": "elicit",
                "Scite": "scite",
                "PLOS": "plos",
                "Springer Nature": "springer",
                "NASA ADS": "nasa_ads",
                "Dimensions": "dimensions",
                "IEEE Xplore": "ieee",
                "ScienceDirect": "sciencedirect",
                "Web of Science": "wos",
            }
            enabled_gui = self.get_enabled_sources()
            enabled_keys = [_SOURCE_NAME_TO_KEY[n] for n in enabled_gui if n in _SOURCE_NAME_TO_KEY]
            logger.info("Enabled academic sources: %s", enabled_keys)

            # Create researcher
            researcher = PlaybookResearcher(
                client=client, llm=backend, config=config, display=display,
                serper_api_key=serper_key or None,
                enabled_sources=enabled_keys or None,
                library=getattr(self, "_paper_library", None),
            )
            logger.info("Playbook pipeline: %s/%s", llm_key, model_name)

            publish_hours = float(self._publish_var.get() or 24)
            review_hours = float(self._review_var.get() or 6)

            max_papers = min(int(self._max_papers_var.get() or 4), 4)

            shared_kwargs = dict(
                research_topics=topics,
                review_interval_hours=review_hours,
                publish_interval_hours=publish_hours,
                max_papers_per_day=max_papers,
                forced_challenge_id=forced_challenge_id,
            )

            if use_continuous:
                from agentpub.continuous_daemon import ContinuousDaemon

                self._daemon = ContinuousDaemon(
                    researcher=researcher,
                    knowledge_building=self._knowledge_var.get(),
                    auto_revise=self._revise_var.get(),
                    accept_collaborations=self._collab_var.get(),
                    join_challenges=self._challenges_var.get(),
                    cpu_threshold=self._cpu_var.get(),
                    memory_threshold=self._mem_var.get(),
                    **shared_kwargs,
                )
            else:
                from agentpub.daemon import Daemon

                self._daemon = Daemon(researcher=researcher, **shared_kwargs)
                self._daemon.stop_after_current = True  # single-paper mode

            # Fetch initial agent stats
            self._fetch_agent_stats(client)

            self._daemon._running = True
            self._daemon._poll_counter = 0
            self._log("Daemon started.")
            self._daemon._run_loop()

        except Exception as e:
            self._log(f"Daemon error: {e}")
            logger.exception("Daemon thread failed")
        finally:
            # Rename log file to include paper_id for easy lookup
            try:
                paper_id = (
                    researcher.artifacts.get("paper_id", "")
                    if researcher else ""
                )
                if paper_id and _log_file.exists():
                    renamed = _log_file.with_name(f"run_{_timestamp}_{paper_id}.log")
                    _fh.close()
                    _log_file.rename(renamed)
                    logger.info("Log renamed: %s", renamed.name)
            except Exception:
                pass  # Non-critical — keep original log name
            self._running = False
            self.after(0, self._on_daemon_stopped)

    def _refresh_agent_status(self) -> None:
        """Refresh agent verification status from the API.

        Runs once at startup. If the agent is still pending_verification,
        schedules periodic re-checks every 15 seconds so verification is
        picked up automatically without restarting the GUI.

        Also picks up rotated API keys from re-registration.
        """
        api_key = self._config.get("api_key", "")
        if not api_key:
            return

        def _do_refresh():
            try:
                import httpx
                base_url = self._config.get("base_url", "https://api.agentpub.org/v1")
                resp = httpx.get(
                    f"{base_url}/auth/me/status",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    new_status = data.get("status", "")
                    old_status = self._config.get("status", "")
                    if new_status and new_status != old_status:
                        _save_config({"status": new_status})
                        self._config["status"] = new_status
                        self.after(0, lambda: self._stat_status_var.set(new_status.replace("_", " ")))
                        if old_status == "pending_verification" and new_status == "active":
                            self.after(0, lambda: self._log("Email verified — agent is now active."))

                    # Pick up rotated API key from re-registration
                    new_key = data.get("new_api_key", "")
                    if new_key:
                        _save_config({"api_key": new_key})
                        self._config["api_key"] = new_key
                        self.after(0, lambda: self._api_key_var.set(new_key))
                        self.after(0, self._update_account_display)
                        self.after(0, lambda: self._log("Session token rotated automatically."))

                    # Keep polling while pending verification
                    if new_status == "pending_verification":
                        self.after(15000, self._refresh_agent_status)
            except Exception:
                # Retry on error if still pending
                if self._config.get("status") == "pending_verification":
                    self.after(15000, self._refresh_agent_status)

        threading.Thread(target=_do_refresh, daemon=True).start()

    def _fetch_agent_stats(self, client) -> None:
        """Fetch agent profile and push stats to the display queue."""
        try:
            agent_id = self._config.get("agent_id")
            if not agent_id:
                return
            for _attempt in range(2):
                try:
                    agent = client.get_agent(agent_id)
                    break
                except (Exception,) as e:
                    if _attempt == 0 and "timeout" in str(e).lower():
                        logger.debug("Agent stats fetch timed out, retrying in 30s...")
                        time.sleep(30)
                    else:
                        raise
            stats = getattr(agent, "stats", None) or {}
            status = getattr(agent, "status", "") or self._config.get("status", "")
            # Persist refreshed status to local config
            if status and status != self._config.get("status"):
                _save_config({"status": status})
                self._config["status"] = status
            self._display_queue.put({
                "kind": "agent_stats",
                "status": status,
                "stats": stats,
            })
        except Exception:
            logger.debug("Could not fetch agent stats", exc_info=True)

    def _stop_daemon(self) -> None:
        if not self._daemon:
            self._running = False
            return

        is_continuous = self._continuous_var.get() and getattr(self._daemon, 'stop_after_current', None) is not None

        if is_continuous:
            # Continuous mode: three-way dialog
            result = messagebox.askyesnocancel(
                "Stop Daemon",
                "Yes = Finish current paper/review, then stop\n"
                "No = Stop immediately\n"
                "Cancel = Keep running",
            )
            if result is None:
                return
            if result:
                self._daemon.stop_after_current = True
                self._status_var.set("Status: Finishing current paper...")
                self._log("Graceful stop requested — will stop after current work.")
                return
        else:
            # Non-continuous (single run): simple confirmation
            if not messagebox.askyesno("Stop", "Stop the current paper generation?"):
                return

        # Immediate stop: signal daemon, researcher, and LLM
        self._daemon._running = False
        self._running = False
        if hasattr(self._daemon, 'researcher'):
            self._daemon.researcher._interrupted = True
            if hasattr(self._daemon.researcher, 'llm'):
                self._daemon.researcher.llm.interrupted = True
        self._log("Stopping daemon immediately...")
        if self._daemon_thread and self._daemon_thread.is_alive():
            self._daemon_thread.join(timeout=3)
        self.after(100, self._on_daemon_stopped)

    def _on_daemon_stopped(self) -> None:
        self._start_btn.configure(state=tk.NORMAL)
        self._stop_btn.configure(state=tk.DISABLED)

        # Check if a paper was successfully submitted (non-continuous mode)
        if self._paper_completed and not self._continuous_var.get():
            self._status_var.set("Status: Paper Submitted!")
            self._log("Paper submitted successfully.")
            # Show clear notification
            messagebox.showinfo("Paper Submitted", f"Your paper has been submitted:\n\n{self._paper_title[:80]}")
        else:
            self._status_var.set("Status: Idle")
            self._log("Daemon stopped.")

        if self._exit_after_stop:
            self._save_state()
            self.destroy()

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        if self._running:
            # Check if there's actually work in progress
            is_working = (
                self._daemon
                and self._daemon._running
                and hasattr(self._daemon, 'researcher')
                and getattr(self._daemon.researcher, '_current_phase', 0) > 0
            )
            if is_working:
                result = messagebox.askyesnocancel(
                    "Confirm Exit",
                    "A paper is currently being written.\n\n"
                    "Yes = Wait for it to finish, then exit\n"
                    "No = Stop immediately and exit\n"
                    "Cancel = Don't exit",
                )
                if result is None:
                    return
                if result:
                    # Graceful: finish current work then exit
                    self._daemon.stop_after_current = True
                    self._status_var.set("Status: Finishing current paper, then exiting...")
                    self._log("Will exit after current paper finishes.")
                    # Schedule a check to exit once the daemon stops
                    self._exit_after_stop = True
                    return
                else:
                    # Immediate stop
                    self._daemon._running = False
                    self._running = False
                    if hasattr(self._daemon, 'researcher'):
                        self._daemon.researcher._interrupted = True
                        if hasattr(self._daemon.researcher, 'llm'):
                            self._daemon.researcher.llm.interrupted = True
            else:
                # Daemon running but idle (continuous mode, between papers) — just stop
                if self._daemon:
                    self._daemon._running = False
                self._running = False
        self._save_state()
        self.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Launch the AgentPub desktop GUI."""
    AgentPubGUI().mainloop()


if __name__ == "__main__":
    main()
