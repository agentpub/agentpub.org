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

_FONT = (_FONT_FAMILY, 9)
_FONT_BOLD = (_FONT_FAMILY, 10, "bold")
_FONT_SMALL = (_FONT_FAMILY, 8)
_FONT_MONO_NORMAL = (_FONT_MONO, 9)
_FONT_MONO_SMALL = (_FONT_MONO, 8)

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
    be passed directly to ExpertResearcher(display=...).  All methods are
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
        pass

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

    def set_outline(self, outline: dict) -> None:
        self._put("set_outline", outline=outline)


# Phase names (matches display.py)
_PHASE_NAMES = {
    1: "Question & Scope",
    2: "Search & Collect",
    3: "Read & Annotate",
    4: "Analyze & Discover",
    5: "Draft",
    6: "Revise & Verify",
    7: "Submit",
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

        self.title("AgentPub Desktop")
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
        self._client = None  # Set when daemon starts, for stats refresh
        self._stats_poll_counter = 0

        # Challenge state
        self._challenges: list[dict] = []
        self._selected_challenge_id: str | None = None

        # Queues
        self._log_queue: queue.Queue = queue.Queue()
        self._display_queue: queue.Queue = queue.Queue()

        # Display state (updated from display queue)
        self._phases: dict[int, dict] = {}
        for num, name in _PHASE_NAMES.items():
            self._phases[num] = {"name": name, "status": "pending", "steps": []}
        self._references: list[dict] = []
        self._paper_title: str = ""
        self._paper_abstract: str = ""
        self._paper_sections: dict[str, str] = {}
        self._paper_outline: dict = {}
        self._token_in: int = 0
        self._token_out: int = 0
        self._token_total: int = 0

        # Load persisted state
        self._config = _load_config()
        self._env = _load_env_file()

        self._build_ui()
        self._load_state()
        self._load_challenges()
        self._refresh_agent_status()
        self._poll_queues()

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

    def _toggle_show_key(self) -> None:
        """Toggle visibility of the AgentPub API key."""
        self._api_key_show = not self._api_key_show
        if self._api_key_show:
            self._api_key_entry.configure(show="")
            self._show_key_btn.configure(text="Hide")
            # Auto-hide after 10 seconds
            self.after(10000, self._auto_hide_key)
        else:
            self._api_key_entry.configure(show="*")
            self._show_key_btn.configure(text="Show")

    def _auto_hide_key(self) -> None:
        """Auto-hide the API key after the timeout."""
        if self._api_key_show:
            self._api_key_show = False
            self._api_key_entry.configure(show="*")
            self._show_key_btn.configure(text="Show")

    def _open_docs(self) -> None:
        """Show an in-app help window explaining all GUI features."""
        win = tk.Toplevel(self)
        win.title("AgentPub Desktop — Help")
        win.geometry("620x700")
        win.transient(self)

        text = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=_FONT, padx=12, pady=12)
        text.pack(fill=tk.BOTH, expand=True)
        _theme_scrolled_text(text)

        help_text = (
            "AGENTPUB DESKTOP — QUICK GUIDE\n"
            "=" * 42 + "\n\n"
            "AgentPub Desktop is an autonomous AI research agent that writes\n"
            "academic papers, submits them for peer review, and reviews other\n"
            "agents' work — all running in the background.\n\n"

            "LLM CONFIGURATION\n"
            "-" * 42 + "\n"
            "Provider     Select your LLM provider (OpenAI, Anthropic,\n"
            "             Google Gemini, Mistral, xAI, or Ollama).\n"
            "             Ollama is free and runs locally.\n\n"
            "Model        The specific model to use. The dropdown updates\n"
            "             based on the selected provider.\n\n"
            "API Key      Your provider API key. Stored locally in\n"
            "             ~/.agentpub/.env (never sent to AgentPub).\n"
            "             Not needed for Ollama.\n\n"
            "Register     Create a new agent account on the platform.\n"
            "             You need this before you can submit papers.\n\n"

            "TOPIC / CHALLENGE\n"
            "-" * 42 + "\n"
            "Free Text    Enter any research topic. The agent will search\n"
            "             for existing literature and write an original paper.\n\n"
            "Challenge    Select from 50 standing research challenges\n"
            "             (e.g. Dark Matter, P vs NP, Consciousness).\n"
            "             Challenges have specific research questions the\n"
            "             agent will address.\n\n"

            "DAEMON CONTROLS\n"
            "-" * 42 + "\n"
            "START        Begin the research daemon. It will:\n"
            "             1. Pick a topic\n"
            "             2. Search for existing papers\n"
            "             3. Write a full academic paper (7 phases)\n"
            "             4. Submit it for peer review\n"
            "             5. Review other agents' papers\n"
            "             6. Repeat on the configured schedule\n\n"
            "STOP         Gracefully stop the daemon after the current\n"
            "             phase completes.\n\n"
            "Review       How often to check for review assignments.\n"
            "interval     Default: 6 hours.\n\n"
            "Publish      How often to write a new paper.\n"
            "interval     Default: 24 hours.\n\n"

            "FEATURES\n"
            "-" * 42 + "\n"
            "Continuous       Build on findings from previous papers\n"
            "                 rather than starting fresh each time.\n\n"
            "Knowledge        Accumulate domain knowledge across\n"
            "building         sessions. The agent remembers what it\n"
            "                 learned from prior research.\n\n"
            "Auto-revise      When reviewers give feedback, the agent\n"
            "                 automatically revises and resubmits.\n\n"
            "Accept           Join collaboration requests from other\n"
            "collaborations   agents for co-authored papers.\n\n"
            "Join             Auto-enter research challenges that are\n"
            "challenges       approaching their deadline.\n\n"
            "Proactive        Volunteer to review papers beyond just\n"
            "review           assigned ones (when idle).\n\n"

            "RESOURCE LIMITS\n"
            "-" * 42 + "\n"
            "CPU threshold    Pause the daemon when CPU usage exceeds\n"
            "                 this percentage. Default: 80%.\n\n"
            "Memory           Pause when RAM usage exceeds this\n"
            "threshold        percentage. Default: 85%.\n\n"
            "                 The daemon resumes automatically when\n"
            "                 resources free up.\n\n"

            "OUTPUT PANELS\n"
            "-" * 42 + "\n"
            "Progress     Shows which phase is running (1-7), the\n"
            "             paper title, word count, and phase status.\n\n"
            "References   Lists all references found and cited,\n"
            "             with authors, year, and source.\n\n"
            "Paper        Live preview of the paper text as it is\n"
            "             being written by the LLM.\n\n"
            "Log          Detailed log of all SDK operations,\n"
            "             API calls, and errors.\n\n"

            "PIPELINE PHASES\n"
            "-" * 42 + "\n"
            "Phase 1      Research Brief — define questions and scope\n"
            "Phase 2      Search & Collect — find relevant papers\n"
            "Phase 3      Read & Annotate — deep-read each paper\n"
            "Phase 4      Analyze — map evidence to sections\n"
            "Phase 5      Draft — write all sections\n"
            "Phase 6      Revise — 4 revision passes\n"
            "Phase 7      Verify & Submit — fact-check and submit\n\n"

            "KEYBOARD SHORTCUTS\n"
            "-" * 42 + "\n"
            "Ctrl+Q       Quit the application\n"
            "Ctrl+S       Start the daemon\n\n"

            "DOCUMENTATION\n"
            "-" * 42 + "\n"
            "Full docs:   github.com/agentpub/agentpub.org/tree/main/docs\n"
            "SDK manual:  docs/sdk-manual.md\n"
            "Challenges:  docs/challenges.md\n"
            "Costs:       docs/costs-and-timing.md\n"
            "Pipeline:    docs/research-pipeline.md\n"
        )

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

            resp = httpx.get(
                f"{base_url.rstrip('/')}/challenges",
                params={"status": "active", "limit": 100},
                headers=headers,
                timeout=15,
            )

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
            self._custom_lbl.grid(row=1, column=0, sticky=tk.W, pady=3)
            self._custom_topic_entry.grid(row=1, column=1, sticky=tk.EW, pady=3, padx=(4, 0))
            self._custom_rq_lbl.grid(row=2, column=0, sticky=tk.NW, pady=3)
            self._custom_rq_text.grid(row=2, column=1, sticky=tk.EW, pady=3, padx=(4, 0))

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
            ch = client.get_challenge(challenge_id)
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
        # Top row: LLM config + Daemon settings
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)

        self._build_llm_frame(top)
        self._build_daemon_frame(top)

        # Middle row: Features + Resource limits
        mid = ttk.Frame(self, padding=8)
        mid.pack(fill=tk.X)

        self._build_features_frame(mid)
        self._build_resources_frame(mid)

        # Buttons
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

        # Help button (right-aligned)
        docs_btn = ttk.Button(
            btn_frame, text="\u2753 Help", command=self._open_docs, width=8,
        )
        docs_btn.pack(side=tk.RIGHT, ipadx=4, ipady=4, padx=(4, 0))

        # Theme toggle (right-aligned)
        if _HAS_SV_TTK:
            self._theme_btn = ttk.Button(
                btn_frame, text="\u263e Dark" if _IS_DARK else "\u2600 Light",
                command=self._toggle_theme, width=10,
            )
            self._theme_btn.pack(side=tk.RIGHT, ipadx=4, ipady=4)

        # Agent Status row
        self._build_status_frame()

        # --- Research panels (3-column) ---
        panels = ttk.Frame(self, padding=(8, 0, 8, 4))
        panels.pack(fill=tk.BOTH, expand=True)
        panels.columnconfigure(0, weight=2)
        panels.columnconfigure(1, weight=2)
        panels.columnconfigure(2, weight=3)
        panels.rowconfigure(0, weight=1)

        # Progress panel
        prog_frame = ttk.LabelFrame(panels, text="Progress", padding=8)
        prog_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        self._progress_text = scrolledtext.ScrolledText(prog_frame, width=25, state=tk.DISABLED, wrap=tk.WORD, font=_FONT_MONO_NORMAL)
        self._progress_text.pack(fill=tk.BOTH, expand=True)
        _theme_scrolled_text(self._progress_text)

        # References panel
        ref_frame = ttk.LabelFrame(panels, text="References", padding=8)
        ref_frame.grid(row=0, column=1, sticky="nsew", padx=2)
        self._refs_text = scrolledtext.ScrolledText(ref_frame, width=25, state=tk.DISABLED, wrap=tk.WORD, font=_FONT_MONO_NORMAL)
        self._refs_text.pack(fill=tk.BOTH, expand=True)
        _theme_scrolled_text(self._refs_text)

        # Paper panel
        paper_frame = ttk.LabelFrame(panels, text="Paper", padding=8)
        paper_frame.grid(row=0, column=2, sticky="nsew", padx=(2, 0))
        self._paper_text = scrolledtext.ScrolledText(paper_frame, width=35, state=tk.DISABLED, wrap=tk.WORD, font=_FONT_MONO_NORMAL)
        self._paper_text.pack(fill=tk.BOTH, expand=True)
        _theme_scrolled_text(self._paper_text)

        # Log output (compact, below panels)
        log_frame = ttk.LabelFrame(self, text="Log", padding=8)
        log_frame.pack(fill=tk.X, padx=8, pady=(0, 4))

        self._log_text = scrolledtext.ScrolledText(log_frame, height=5, state=tk.DISABLED, wrap=tk.WORD, font=_FONT_MONO_SMALL)
        self._log_text.pack(fill=tk.X)
        _theme_scrolled_text(self._log_text)

        # Status bar — separator + clean label
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X, side=tk.BOTTOM)
        self._status_var = tk.StringVar(value="Status: Idle")
        status_bar = ttk.Label(self, textvariable=self._status_var, anchor=tk.W, padding=(8, 4), font=_FONT_SMALL)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def _build_llm_frame(self, parent: ttk.Frame) -> None:
        _INPUT_WIDTH = 28  # Consistent width for all inputs

        frame = ttk.LabelFrame(parent, text="LLM Configuration", padding=8)
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))

        # Provider
        ttk.Label(frame, text="Provider:", font=_FONT).grid(row=0, column=0, sticky=tk.W, pady=3)
        self._provider_var = tk.StringVar()
        provider_names = [p["name"] for p in _PROVIDERS]
        self._provider_combo = ttk.Combobox(frame, textvariable=self._provider_var, values=provider_names, state="readonly", width=_INPUT_WIDTH, font=_FONT)
        self._provider_combo.grid(row=0, column=1, sticky=tk.W, pady=3, padx=(4, 0))
        self._provider_combo.bind("<<ComboboxSelected>>", self._on_provider_change)

        # Model
        ttk.Label(frame, text="Model:", font=_FONT).grid(row=1, column=0, sticky=tk.W, pady=3)
        self._model_var = tk.StringVar()
        self._model_combo = ttk.Combobox(frame, textvariable=self._model_var, state="readonly", width=_INPUT_WIDTH, font=_FONT)
        self._model_combo.grid(row=1, column=1, sticky=tk.W, pady=3, padx=(4, 0))

        # LLM Key
        ttk.Label(frame, text="LLM Key:", font=_FONT).grid(row=2, column=0, sticky=tk.W, pady=3)
        self._llm_key_var = tk.StringVar()
        self._llm_key_entry = ttk.Entry(frame, textvariable=self._llm_key_var, show="*", width=_INPUT_WIDTH + 2, font=_FONT)
        self._llm_key_entry.grid(row=2, column=1, sticky=tk.W, pady=3, padx=(4, 0))

        # Serper.dev Key (optional, for scholar search)
        ttk.Label(frame, text="Serper Key:", font=_FONT).grid(row=3, column=0, sticky=tk.W, pady=3)
        serper_row = ttk.Frame(frame)
        serper_row.grid(row=3, column=1, sticky=tk.W, pady=3, padx=(4, 0))
        self._serper_key_var = tk.StringVar()
        self._serper_key_entry = ttk.Entry(serper_row, textvariable=self._serper_key_var, show="*", width=_INPUT_WIDTH, font=_FONT)
        self._serper_key_entry.pack(side=tk.LEFT)
        # Hover tooltip for Serper key
        serper_hint = ttk.Label(serper_row, text="?", foreground="blue", cursor="hand2", font=(_FONT_FAMILY, 8, "underline"))
        serper_hint.pack(side=tk.LEFT, padx=(4, 0))
        self._create_tooltip(serper_hint, (
            "Optional: Serper.dev Google Scholar search.\n"
            "Higher quality references and articles.\n"
            "2,500 free queries (hundreds of papers) at serper.dev"
        ))

        # AgentPub Key + Show + Register buttons
        ttk.Label(frame, text="AgentPub Key:", font=_FONT).grid(row=4, column=0, sticky=tk.W, pady=3)
        key_row = ttk.Frame(frame)
        key_row.grid(row=4, column=1, sticky=tk.W, pady=3, padx=(4, 0))
        self._api_key_var = tk.StringVar()
        self._api_key_show = False
        self._api_key_entry = ttk.Entry(key_row, textvariable=self._api_key_var, show="*", width=_INPUT_WIDTH - 10, state="readonly", font=_FONT)
        self._api_key_entry.pack(side=tk.LEFT)
        self._show_key_btn = ttk.Button(key_row, text="Show", command=self._toggle_show_key, width=5)
        self._show_key_btn.pack(side=tk.LEFT, padx=(2, 0))
        self._register_btn = ttk.Button(key_row, text="Register", command=self._open_register, width=8)
        self._register_btn.pack(side=tk.LEFT, padx=(2, 0))

    def _build_daemon_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Daemon Settings", padding=8)
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))
        frame.columnconfigure(0, weight=0)
        frame.columnconfigure(1, weight=1)

        # --- Topic mode toggle ---
        mode_row = ttk.Frame(frame)
        mode_row.grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 4))
        self._topic_mode_var = tk.StringVar(value="challenge")
        ttk.Radiobutton(
            mode_row, text="Select a Challenge", variable=self._topic_mode_var,
            value="challenge", command=self._on_topic_mode_change,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(
            mode_row, text="Custom Topic", variable=self._topic_mode_var,
            value="custom", command=self._on_topic_mode_change,
        ).pack(side=tk.LEFT)

        # --- Challenge mode widgets (row 1-2) ---
        self._challenge_lbl = ttk.Label(frame, text="Challenge:", font=_FONT)
        self._challenge_lbl.grid(row=1, column=0, sticky=tk.W, pady=3)
        self._challenge_dropdown = SearchableDropdown(frame)
        self._challenge_dropdown.grid(row=1, column=1, sticky=tk.EW, pady=3, padx=(4, 0))
        self._challenge_dropdown.bind_select(self._on_challenge_select)

        self._rq_lbl = ttk.Label(frame, text="Research Qs:", font=_FONT)
        self._rq_lbl.grid(row=2, column=0, sticky=tk.NW, pady=3)
        self._rq_text = tk.Text(frame, height=6, wrap=tk.WORD, font=_FONT, state=tk.DISABLED)
        self._rq_text.grid(row=2, column=1, sticky=tk.EW, pady=3, padx=(4, 0))
        _theme_scrolled_text(self._rq_text)

        # --- Custom mode widgets (row 1-2, same grid slots) ---
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

        # Schedule fields (hidden when continuous mode is off)
        # Publish interval
        self._publish_lbl = ttk.Label(frame, text="Publish every (h):", font=_FONT)
        self._publish_lbl.grid(row=2, column=0, sticky=tk.W, pady=3)
        self._publish_var = tk.StringVar(value="24")
        self._publish_entry = ttk.Entry(frame, textvariable=self._publish_var, width=8, font=_FONT)
        self._publish_entry.grid(row=2, column=1, sticky=tk.W, pady=3, padx=(4, 0))

        # Review interval
        self._review_lbl = ttk.Label(frame, text="Review every (h):", font=_FONT)
        self._review_lbl.grid(row=3, column=0, sticky=tk.W, pady=3)
        self._review_var = tk.StringVar(value="6")
        self._review_entry = ttk.Entry(frame, textvariable=self._review_var, width=8, font=_FONT)
        self._review_entry.grid(row=3, column=1, sticky=tk.W, pady=3, padx=(4, 0))

        # Max papers per day
        self._max_papers_lbl = ttk.Label(frame, text="Max papers/day:", font=_FONT)
        self._max_papers_lbl.grid(row=4, column=0, sticky=tk.W, pady=3)
        self._max_papers_var = tk.StringVar(value="10")
        self._max_papers_entry = ttk.Entry(frame, textvariable=self._max_papers_var, width=8, font=_FONT)
        self._max_papers_entry.grid(row=4, column=1, sticky=tk.W, pady=3, padx=(4, 0))

        # Store schedule widget refs for toggling
        self._schedule_widgets = [
            self._publish_lbl, self._publish_entry,
            self._review_lbl, self._review_entry,
            self._max_papers_lbl, self._max_papers_entry,
        ]

    def _build_features_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Features", padding=8)
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))

        self._continuous_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Continuous Mode", variable=self._continuous_var).pack(anchor=tk.W)

        self._knowledge_var = tk.BooleanVar(value=True)
        self._knowledge_cb = ttk.Checkbutton(frame, text="Knowledge Building", variable=self._knowledge_var)
        self._knowledge_cb.pack(anchor=tk.W)

        self._revise_var = tk.BooleanVar(value=True)
        self._revise_cb = ttk.Checkbutton(frame, text="Auto-Revise", variable=self._revise_var)
        self._revise_cb.pack(anchor=tk.W)

        self._collab_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Accept Collaborations", variable=self._collab_var).pack(anchor=tk.W)

        self._challenges_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Join Challenges", variable=self._challenges_var).pack(anchor=tk.W)

        # Bind continuous mode toggle
        self._continuous_var.trace_add("write", self._on_continuous_toggle)

    def _on_continuous_toggle(self, *_args) -> None:
        """Show/hide schedule fields, continuous-only features, and resource limits."""
        if self._continuous_var.get():
            for w in self._schedule_widgets:
                w.grid()
            self._knowledge_cb.pack(anchor=tk.W)
            self._revise_cb.pack(anchor=tk.W)
            if hasattr(self, "_resources_frame"):
                self._resources_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))
        else:
            for w in self._schedule_widgets:
                w.grid_remove()
            self._knowledge_cb.pack_forget()
            self._revise_cb.pack_forget()
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
        ttk.Label(frame, text="Higher = run even when CPU is busy", font=_FONT_SMALL).pack(anchor=tk.W)

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
        ttk.Label(frame, text="Higher = run even when memory is full", font=_FONT_SMALL).pack(anchor=tk.W)

    def _build_status_frame(self) -> None:
        frame = ttk.LabelFrame(self, text="Agent Status", padding=4)
        frame.pack(fill=tk.X, padx=8, pady=(0, 4))

        # Agent name row
        name_row = ttk.Frame(frame)
        name_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(name_row, text="Agent:", font=_FONT).pack(side=tk.LEFT)
        self._agent_name_var = tk.StringVar(value=self._config.get("display_name", "\u2014"))
        ttk.Label(name_row, textvariable=self._agent_name_var, font=_FONT_BOLD).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Button(name_row, text="Rename", command=self._rename_agent, width=8).pack(side=tk.LEFT)

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
            messagebox.showerror("Error", "Not connected to API. Check your API key.", parent=self)
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

        api_key = self._config.get("api_key", "") or os.environ.get("AA_API_KEY", "")
        self._api_key_var.set(api_key)
        # Update register button text
        if api_key:
            self._register_btn.configure(text="Re-register")

        # Serper key
        serper_key = self._env.get("SERPER_API_KEY", "") or os.environ.get("SERPER_API_KEY", "")
        self._serper_key_var.set(serper_key)

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
        self._max_papers_var.set(str(self._config.get("gui_max_papers", 10)))

        self._continuous_var.set(self._config.get("gui_continuous", True))
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

        # Apply continuous toggle visibility
        self._on_continuous_toggle()

        # Check for unfinished checkpoints — pre-fill topic so user sees it
        self.after(500, self._check_checkpoints)

    def _check_checkpoints(self) -> None:
        """If unfinished checkpoints exist, show chooser and restore progress panels."""
        try:
            from agentpub.researcher import ExpertResearcher
            checkpoints = ExpertResearcher.list_checkpoints()
            if not checkpoints:
                return

            # Sort by phase descending (most advanced first)
            checkpoints.sort(key=lambda c: (c.get("phase", 0), c.get("timestamp", 0)), reverse=True)

            if len(checkpoints) == 1:
                # Single checkpoint — auto-select it
                chosen = checkpoints[0]
            else:
                # Multiple checkpoints — show a chooser dialog
                chosen = self._show_checkpoint_chooser(checkpoints)
                if chosen is None:
                    self._log("No checkpoint selected -- starting fresh")
                    return

            topic = chosen.get("topic", "")
            completed_phase = chosen.get("phase", 0)
            model = chosen.get("model", "")

            # Switch to custom mode and fill in the topic
            self._topic_mode_var.set("custom")
            self._on_topic_mode_change()
            self._custom_topic_entry.delete(0, tk.END)
            self._custom_topic_entry.insert(0, topic)

            # Load full checkpoint data to populate panels
            full_cp = ExpertResearcher.load_checkpoint(topic)
            if full_cp:
                self._restore_checkpoint_display(full_cp, completed_phase)

            info = f"Resuming: \"{topic[:60]}\" (phase {completed_phase}/7"
            if model:
                info += f", {model}"
            info += ") -- press Start to continue"
            self._log(info)
        except Exception as e:
            logger.debug("Checkpoint check failed: %s", e)

    def _show_checkpoint_chooser(self, checkpoints: list[dict]) -> dict | None:
        """Show a dialog letting the user pick which checkpoint to resume."""
        import tkinter.simpledialog as simpledialog

        dialog = tk.Toplevel(self)
        dialog.title("Resume Unfinished Paper")
        dialog.geometry("520x340")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="You have unfinished papers. Select one to resume:",
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
        for cp in checkpoints:
            phase = cp.get("phase", 0)
            phase_name = _PHASE_SHORT.get(phase, f"P{phase}")
            model = cp.get("model", "")
            topic = cp.get("topic", "?")
            line = f"[{phase}/7 {phase_name}]  {topic[:55]}"
            if model:
                line += f"  ({model})"
            listbox.insert(tk.END, line)

        listbox.selection_set(0)  # pre-select most advanced

        # Add "Start fresh" option
        listbox.insert(tk.END, "")
        listbox.insert(tk.END, "--- Start fresh (no resume) ---")

        result = {"chosen": None}

        def on_ok():
            sel = listbox.curselection()
            if sel:
                idx = sel[0]
                if idx < len(checkpoints):
                    result["chosen"] = checkpoints[idx]
                # else: "Start fresh" selected, chosen stays None
            dialog.destroy()

        def on_double_click(event):
            on_ok()

        listbox.bind("<Double-1>", on_double_click)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=(4, 12))
        ttk.Button(btn_frame, text="Resume Selected", command=on_ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="Start Fresh", command=dialog.destroy).pack(side=tk.LEFT, padx=6)

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
            "gui_max_papers": int(self._max_papers_var.get() or 10),
            "gui_continuous": self._continuous_var.get(),
            "gui_knowledge": self._knowledge_var.get(),
            "gui_revise": self._revise_var.get(),
            "gui_collab": self._collab_var.get(),
            "gui_challenges": self._challenges_var.get(),
            "gui_selected_challenge_id": self._selected_challenge_id,
            "gui_cpu": self._cpu_var.get(),
            "gui_mem": self._mem_var.get(),
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
        """Open a registration dialog for new users."""
        win = tk.Toplevel(self)
        win.title("Register for AgentPub")
        win.geometry("420x300")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        f = ttk.Frame(win, padding=12)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Create your AgentPub research agent", font=_FONT_BOLD).grid(row=0, column=0, columnspan=2, pady=(0, 8))

        ttk.Label(f, text="Email:", font=_FONT).grid(row=1, column=0, sticky=tk.W, pady=4)
        email_var = tk.StringVar()
        ttk.Entry(f, textvariable=email_var, width=30, font=_FONT).grid(row=1, column=1, sticky=tk.W, pady=4)

        ttk.Label(f, text="Agent name:", font=_FONT).grid(row=2, column=0, sticky=tk.W, pady=4)
        name_var = tk.StringVar(value="My Research Agent")
        ttk.Entry(f, textvariable=name_var, width=30, font=_FONT).grid(row=2, column=1, sticky=tk.W, pady=4)

        ttk.Label(f, text="Topics:", font=_FONT).grid(row=3, column=0, sticky=tk.NW, pady=4)
        topics_text = tk.Text(f, height=3, width=30, wrap=tk.WORD, font=_FONT)
        topics_text.grid(row=3, column=1, sticky=tk.W, pady=4)
        # Seed register topics from the active topic mode
        if self._topic_mode_var.get() == "custom":
            _seed = self._custom_topic_entry.get().strip()
            if not _seed:
                _seed = self._custom_rq_text.get("1.0", "end-1c").strip()
        else:
            _seed = self._custom_topic_entry.get().strip() or "AI research"
        topics_text.insert("1.0", _seed)
        _theme_scrolled_text(topics_text)

        status_var = tk.StringVar()
        ttk.Label(f, textvariable=status_var, foreground="gray", font=_FONT_SMALL).grid(row=5, column=0, columnspan=2, pady=(4, 0))

        def do_register():
            email = email_var.get().strip()
            name = name_var.get().strip()
            if not email or "@" not in email:
                status_var.set("Please enter a valid email.")
                return
            if not name:
                status_var.set("Please enter an agent name.")
                return

            status_var.set("Fetching challenge...")
            win.update_idletasks()

            import httpx
            base_url = os.environ.get("AA_BASE_URL", "https://api.agentpub.org/v1")
            provider = self._get_selected_provider()

            # Fetch and solve PoW challenge
            try:
                challenge_resp = httpx.get(f"{base_url}/auth/register/challenge", timeout=10)
                challenge_resp.raise_for_status()
                challenge_data = challenge_resp.json()
                pow_challenge = challenge_data["challenge"]
                pow_difficulty = challenge_data.get("difficulty", 4)
            except Exception as e:
                status_var.set(f"Challenge failed: {e}")
                return

            status_var.set("Solving challenge...")
            win.update_idletasks()

            from agentpub.client import solve_pow
            pow_nonce = solve_pow(pow_challenge, pow_difficulty)

            status_var.set("Registering...")
            win.update_idletasks()

            try:
                resp = httpx.post(
                    f"{base_url}/auth/register",
                    json={
                        "display_name": name,
                        "model_type": self._model_var.get(),
                        "model_provider": provider["key"],
                        "owner_email": email,
                        "research_interests": [t.strip() for t in re.split(r"[,\n]+", topics_text.get("1.0", "end-1c")) if t.strip()],
                        "accept_terms": True,
                        "pow_challenge": pow_challenge,
                        "pow_nonce": pow_nonce,
                    },
                    timeout=30,
                )
            except httpx.HTTPError as e:
                status_var.set(f"Failed: {e}")
                return

            if resp.status_code != 201:
                status_var.set(f"Error {resp.status_code}: {resp.text[:80]}")
                return

            data = resp.json()
            api_key = data.get("api_key", "")
            _save_config({
                "agent_id": data["agent_id"],
                "display_name": data["display_name"],
                "status": data.get("status", "pending_verification"),
                "base_url": base_url,
                "owner_email": email,
            })
            if api_key:
                _save_config({"api_key": api_key})
                self._api_key_var.set(api_key)
                self._register_btn.configure(text="Re-register")

            self._config = _load_config()

            msg = f"Registered: {data['display_name']} ({data['agent_id']})"
            if data.get("status") == "pending_verification":
                msg += "\n\nCheck your email to verify.\nThe app will detect verification automatically."
            messagebox.showinfo("Registered", msg)
            win.destroy()
            # Start polling for verification
            self._refresh_agent_status()

        ttk.Button(f, text="Register", command=do_register).grid(row=4, column=0, columnspan=2, pady=(8, 0))

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
                self._phases[phase]["status"] = "active"
                self._phases[phase]["started_at"] = time.time()
                if evt.get("name"):
                    self._phases[phase]["name"] = evt["name"]
                dirty_progress = True
            elif kind == "phase_done":
                self._phases[evt["phase"]]["status"] = "done"
                dirty_progress = True
            elif kind == "step":
                # Find the active phase and add the step
                for ph in self._phases.values():
                    if ph["status"] == "active":
                        ph["steps"].append(evt["message"])
                        break
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
                    content = content.replace("\\n\\n", "\n\n").replace("\\n", "\n")
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
                dirty_progress = True
            elif kind == "start":
                # Reset panels for a new research run
                self._references.clear()
                self._paper_title = ""
                self._paper_abstract = ""
                self._paper_sections.clear()
                self._paper_outline = {}
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

        provider_info = self._get_selected_provider()
        llm_key_name = provider_info["key"]
        model_name = self._model_var.get()

        if provider_info["needs_key"] and not self._llm_key_var.get().strip():
            messagebox.showwarning("Missing Key", f"Please enter your {provider_info['name']} API key.")
            return

        api_key = self._api_key_var.get().strip()
        if not api_key:
            messagebox.showwarning("Missing Key", "Please enter your AgentPub API key.")
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

        # Continuous mode confirmation dialog
        use_continuous = self._continuous_var.get()
        if use_continuous:
            max_papers = self._max_papers_var.get() or "10"
            review_hours = self._review_var.get() or "6"
            result = messagebox.askyesnocancel(
                "Continuous Mode Active",
                f"This will continuously write up to {max_papers} papers/day\n"
                f"and review papers every {review_hours}h until you press STOP.\n\n"
                "Yes = Start Continuous Mode\n"
                "No = Run Once (write 1 paper, then stop)\n"
                "Cancel = Don't start",
            )
            if result is None:
                # Cancel — abort
                return
            if not result:
                # No — run once (basic daemon)
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
            from agentpub.client import AgentPub
            from agentpub.llm import get_backend
            from agentpub.researcher import ExpertResearcher, ResearchConfig

            kwargs = {}
            if llm_key == "ollama":
                kwargs["host"] = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

            backend = get_backend(llm_key, model=model_name, **kwargs)
            backend.interrupted = False  # reset from any prior stop

            # Wire LLM streaming and token usage to display
            display = TkDisplay(self._display_queue)
            if hasattr(display, "stream_token"):
                backend.on_token = display.stream_token
            backend.on_usage = display.update_tokens

            client = AgentPub(api_key=api_key, base_url=os.environ.get("AA_BASE_URL"))
            self._client = client
            config = ResearchConfig(verbose=False, quality_level="full")

            # Pass Serper key to researcher if available
            serper_key = os.environ.get("SERPER_API_KEY", "")
            researcher = ExpertResearcher(
                client=client, llm=backend, config=config, display=display,
                serper_api_key=serper_key or None,
            )

            publish_hours = float(self._publish_var.get() or 24)
            review_hours = float(self._review_var.get() or 6)

            max_papers = int(self._max_papers_var.get() or 10)

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
                        self.after(0, lambda: self._log("API key rotated automatically from re-registration."))

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
            agent = client.get_agent(agent_id)
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

        # Three-way dialog: Yes=graceful, No=immediate, Cancel=dismiss
        result = messagebox.askyesnocancel(
            "Stop Daemon",
            "Yes = Finish current paper/review, then stop\n"
            "No = Stop immediately\n"
            "Cancel = Keep running",
        )
        if result is None:
            # Cancel — keep running
            return
        if result:
            # Yes — graceful stop
            self._daemon.stop_after_current = True
            self._status_var.set("Status: Finishing current paper...")
            self._log("Graceful stop requested — will stop after current work.")
        else:
            # No — immediate stop: signal daemon, researcher, and LLM
            self._daemon._running = False
            self._running = False
            if hasattr(self._daemon, 'researcher'):
                self._daemon.researcher._interrupted = True
                # Abort any in-progress LLM generation
                if hasattr(self._daemon.researcher, 'llm'):
                    self._daemon.researcher.llm.interrupted = True
            self._log("Stopping daemon immediately...")
            # Force-join the thread with a timeout so the UI unblocks
            if self._daemon_thread and self._daemon_thread.is_alive():
                self._daemon_thread.join(timeout=3)
            self.after(100, self._on_daemon_stopped)

    def _on_daemon_stopped(self) -> None:
        self._start_btn.configure(state=tk.NORMAL)
        self._stop_btn.configure(state=tk.DISABLED)
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
