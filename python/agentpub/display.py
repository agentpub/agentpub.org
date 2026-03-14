"""Live research dashboard using Rich.

Provides a 3-column TUI: progress | references | paper.
Falls back to NullDisplay when stdout is not a TTY (piped output).
"""

from __future__ import annotations

import signal
import shutil
import time
from collections import OrderedDict

_PHASE_NAMES = {
    1: "Question & Scope",
    2: "Search & Collect",
    3: "Read & Annotate",
    4: "Analyze & Discover",
    5: "Draft",
    6: "Revise & Verify",
    7: "Verify & Harden",
}

_TOTAL_PHASES = 7


class NullDisplay:
    """No-op display for non-interactive / piped output."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def phase_start(self, phase_num: int, name: str | None = None) -> None: ...
    def phase_done(self, phase_num: int) -> None: ...
    def step(self, message: str) -> None: ...
    def section_start(self, name: str) -> None: ...
    def section_done(self, name: str, content: str = "") -> None: ...
    def set_title(self, text: str) -> None: ...
    def set_abstract(self, text: str) -> None: ...
    def tick(self) -> None: ...
    def set_context(self, *, topic: str = "", provider: str = "", model: str = "", api_status: str = "") -> None: ...
    def add_reference(self, index: int, authors: str = "", year: str = "", title: str = "", url: str = "", doi: str = "") -> None: ...
    def complete(self, message: str = "") -> None: ...
    def stream_token(self, text: str, thinking: bool = False) -> None: ...
    def update_tokens(self, input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0) -> None: ...
    def set_outline(self, outline: dict) -> None: ...


class ResearchDisplay:
    """Rich live dashboard for the 7-phase research pipeline."""

    def __init__(self, verbose: bool = False):
        from rich.console import Console

        self.verbose = verbose
        self.console = Console()

        # Phase tracking
        self.phases: dict[int, dict] = {}
        for num, name in _PHASE_NAMES.items():
            self.phases[num] = {"name": name, "status": "pending", "steps": []}
        self.current_phase: int | None = None

        # Paper preview
        self.title: str = ""
        self.abstract: str = ""
        self.paper_sections: OrderedDict[str, str] = OrderedDict()
        self._active_section: str | None = None

        # Context info
        self._topic: str = ""
        self._provider: str = ""
        self._model: str = ""
        self._api_status: str = ""

        # References
        self._references: list[dict] = []

        # Completion state
        self._completed: bool = False
        self._complete_message: str = ""

        # Progress — phase-based, no hardcoded total
        self.step_count = 0
        self._phases_completed = 0
        self.start_time: float = 0.0

        # Token tracking
        self._input_tokens: int = 0
        self._output_tokens: int = 0
        self._total_tokens: int = 0

        # Paper outline (shown before sections are drafted)
        self._outline: dict = {}

        # Live LLM output stream (ring buffer of recent lines)
        self._llm_buffer: str = ""  # raw text accumulator
        self._llm_lines: list[tuple[str, bool]] = []  # (line_text, is_thinking)
        self._llm_max_lines: int = 8  # how many lines to show
        self._llm_thinking: bool = False  # current thinking state

        # Rich Live handle
        self._live = None
        self._prev_sigint = None

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def set_context(
        self,
        *,
        topic: str = "",
        provider: str = "",
        model: str = "",
        api_status: str = "",
    ) -> None:
        self._topic = topic
        self._provider = provider
        self._model = model
        self._api_status = api_status

    # ------------------------------------------------------------------
    # References
    # ------------------------------------------------------------------

    def add_reference(
        self,
        index: int,
        authors: str = "",
        year: str = "",
        title: str = "",
        url: str = "",
        doi: str = "",
    ) -> None:
        self._references.append({
            "index": index,
            "authors": authors,
            "year": year,
            "title": title,
            "url": url,
            "doi": doi,
        })
        self._refresh()

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def complete(self, message: str = "") -> None:
        self._completed = True
        self._complete_message = message or "Completed"
        self._refresh()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        from rich.live import Live

        self.start_time = time.time()
        self._live = Live(
            self._build_layout(),
            console=self.console,
            refresh_per_second=4,
            screen=True,
            vertical_overflow="visible",
        )
        self._live.start()

        # Install SIGINT handler so Ctrl+C works reliably inside Rich screen mode
        def _on_sigint(signum, frame):
            self.stop()
            raise KeyboardInterrupt

        try:
            self._prev_sigint = signal.signal(signal.SIGINT, _on_sigint)
        except (OSError, ValueError):
            pass  # not main thread or unsupported

    def stop(self) -> None:
        # Restore original SIGINT handler
        if self._prev_sigint is not None:
            try:
                signal.signal(signal.SIGINT, self._prev_sigint)
            except (OSError, ValueError):
                pass
            self._prev_sigint = None
        if self._live:
            self._live.stop()
            self._live = None

    # ------------------------------------------------------------------
    # Phase tracking
    # ------------------------------------------------------------------

    def phase_start(self, phase_num: int, name: str | None = None) -> None:
        if name:
            self.phases[phase_num]["name"] = name
        self.phases[phase_num]["status"] = "active"
        self.current_phase = phase_num
        self._refresh()

    def phase_done(self, phase_num: int) -> None:
        self.phases[phase_num]["status"] = "done"
        self._phases_completed = sum(
            1 for p in self.phases.values() if p["status"] == "done"
        )
        self._refresh()

    def step(self, message: str) -> None:
        if self.current_phase and self.current_phase in self.phases:
            self.phases[self.current_phase]["steps"].append(message)
        # Clear LLM output area on each new step — fresh slate for next LLM call
        self._clear_llm_output()
        self._refresh()

    # ------------------------------------------------------------------
    # Section tracking (Phase 5 drafting)
    # ------------------------------------------------------------------

    def section_start(self, name: str) -> None:
        self._active_section = name
        self.paper_sections[name] = "(writing...)"
        self._refresh()

    def section_done(self, name: str, content: str = "") -> None:
        # Fix literal \\n -> actual newlines
        if content:
            content = content.replace("\\n\\n", "\n\n").replace("\\n", "\n")
        self.paper_sections[name] = content.strip() if content else ""
        if self._active_section == name:
            self._active_section = None
        self._refresh()

    def set_title(self, text: str) -> None:
        self.title = text
        self._refresh()

    def set_abstract(self, text: str) -> None:
        self.abstract = text
        self._refresh()

    def update_tokens(self, input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0) -> None:
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._total_tokens = total_tokens
        self._refresh()

    def set_outline(self, outline: dict) -> None:
        self._outline = outline
        self._refresh()

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------

    def tick(self) -> None:
        self.step_count += 1
        self._refresh()

    # ------------------------------------------------------------------
    # Live LLM output stream
    # ------------------------------------------------------------------

    def _clear_llm_output(self) -> None:
        """Reset the LLM output area for a new LLM call."""
        self._llm_buffer = ""
        self._llm_lines = []
        self._llm_thinking = False

    def stream_token(self, text: str, thinking: bool = False) -> None:
        """Receive a streaming token from the LLM and update the live output area."""
        self._llm_thinking = thinking
        self._llm_buffer += text

        # Split buffer into lines, keeping the last incomplete line in the buffer
        had_newline = False
        while "\n" in self._llm_buffer:
            line, self._llm_buffer = self._llm_buffer.split("\n", 1)
            self._llm_lines.append((line, thinking))
            had_newline = True
            # Keep only the last N lines
            if len(self._llm_lines) > self._llm_max_lines * 3:
                self._llm_lines = self._llm_lines[-self._llm_max_lines * 3:]

        # Throttle refreshes: only update on newlines or every ~100ms
        now = time.time()
        if had_newline or (now - getattr(self, "_last_stream_refresh", 0)) > 0.1:
            self._last_stream_refresh = now
            self._refresh()

    # ------------------------------------------------------------------
    # Layout builder
    # ------------------------------------------------------------------

    def _get_terminal_height(self) -> int:
        try:
            return shutil.get_terminal_size().lines
        except Exception:
            return 40

    def _build_layout(self):
        from rich.layout import Layout
        from rich.panel import Panel
        from rich.text import Text

        term_height = self._get_terminal_height()
        # Usable lines inside a panel (borders + title take ~3 lines)
        usable_lines = max(term_height - 4, 20)

        # ---- Left panel: context + progress ----
        left = self._build_progress_panel(usable_lines)
        left_panel = Panel(
            left, title="Progress", title_align="left", border_style="blue"
        )

        # ---- Middle panel: references ----
        mid = self._build_references_panel(usable_lines)
        mid_panel = Panel(
            mid, title="References", title_align="left", border_style="magenta"
        )

        # ---- Right panel: paper preview ----
        right = self._build_paper_panel(usable_lines)
        right_panel = Panel(
            right, title="Paper", title_align="left", border_style="green"
        )

        # ---- 3-column layout ----
        layout = Layout()
        layout.split_row(
            Layout(left_panel, name="progress", ratio=2, minimum_size=30),
            Layout(mid_panel, name="references", ratio=2, minimum_size=25),
            Layout(right_panel, name="paper", ratio=3),
        )

        return layout

    def _build_progress_panel(self, usable_lines: int):
        from rich.text import Text

        left = Text()

        # --- Context header ---
        if self._topic:
            left.append("Topic: ", style="bold")
            # Wrap long topics
            topic_display = self._topic[:60]
            if len(self._topic) > 60:
                topic_display += "..."
            left.append(f"{topic_display}\n")
        if self._provider or self._model:
            left.append("Model: ", style="bold")
            left.append(f"{self._provider}")
            if self._model:
                left.append(f" / {self._model}")
            left.append("\n")
        if self._api_status:
            left.append("API: ", style="bold")
            style = "green" if self._api_status == "verified" else "yellow"
            left.append(f"{self._api_status}\n", style=style)
        if self._topic or self._provider or self._api_status:
            left.append("\n")

        # --- Phase list ---
        for num in sorted(self.phases):
            ph = self.phases[num]
            status = ph["status"]

            if status == "active":
                left.append(f"Phase {num}: {ph['name']}", style="bold yellow")
                left.append("  ")
                left.append("[active]", style="yellow")
            elif status == "done":
                left.append(f"Phase {num}: {ph['name']}", style="green")
                left.append("  ")
                left.append("[done]", style="green")
            else:
                left.append(f"Phase {num}: {ph['name']}", style="dim")
            left.append("\n")

            # Show recent steps
            steps = ph["steps"]
            show_steps = steps if self.verbose else steps[-3:]
            for s in show_steps:
                if status == "done":
                    left.append(f"  {s}\n", style="dim")
                elif status == "active":
                    left.append(f"  {s}\n")
                else:
                    left.append(f"  {s}\n", style="dim")

            left.append("\n")

        # --- Phase-based progress bar ---
        pct = self._phases_completed / _TOTAL_PHASES
        elapsed = time.time() - self.start_time if self.start_time else 0
        mins, secs = divmod(int(elapsed), 60)

        bar_width = 24
        filled = int(bar_width * pct)
        left.append("━" * filled, style="green")
        left.append("━" * (bar_width - filled), style="dim")
        left.append(f" {int(pct * 100)}%\n")
        left.append(
            f"Phase {self._phases_completed}/{_TOTAL_PHASES} · Step {self.step_count}"
        )
        left.append(f" · {mins}m {secs:02d}s elapsed\n", style="dim")

        # --- Token usage ---
        if self._total_tokens > 0:
            left.append("\n")
            left.append("Tokens: ", style="bold")
            left.append(f"In: {self._input_tokens:,}  Out: {self._output_tokens:,}  ")
            left.append(f"Total: {self._total_tokens:,}\n", style="cyan")

        # --- Live LLM output ---
        if self._llm_lines or self._llm_buffer:
            left.append("\n")
            left.append("LLM Output", style="bold dim")
            left.append("\n")
            # Show last N lines from the ring buffer
            recent = self._llm_lines[-self._llm_max_lines:]
            for line_text, is_think in recent:
                display_line = line_text[:60]
                if is_think:
                    left.append(f"  {display_line}\n", style="dim italic magenta")
                else:
                    left.append(f"  {display_line}\n", style="dim")
            # Show current incomplete line
            if self._llm_buffer:
                partial = self._llm_buffer[:60]
                style = "dim italic magenta" if self._llm_thinking else "dim"
                left.append(f"  {partial}", style=style)
                left.append("▌\n", style="blink")

        # --- Completion banner ---
        if self._completed:
            left.append("\n")
            left.append(" COMPLETED ", style="bold white on green")
            left.append(f" {self._complete_message}\n")
        else:
            left.append("\n")
            left.append("Press Ctrl+C to pause", style="dim italic")

        return left

    def _build_references_panel(self, usable_lines: int):
        from rich.text import Text

        mid = Text()

        if not self._references:
            mid.append("Waiting for sources...\n", style="dim italic")
            return mid

        # Auto-scroll: show last N references that fit
        # Each reference takes ~4-5 lines
        lines_per_ref = 5
        max_refs = max(usable_lines // lines_per_ref, 3)
        refs_to_show = self._references[-max_refs:]
        skipped = len(self._references) - len(refs_to_show)

        if skipped > 0:
            mid.append(f"... ({skipped} refs above)\n\n", style="dim")

        for ref in refs_to_show:
            idx = ref["index"]
            authors = ref.get("authors", "")
            year = ref.get("year", "")
            title = ref.get("title", "")
            url = ref.get("url", "")
            doi = ref.get("doi", "")

            # Format: [1] Authors (Year)
            mid.append(f"[{idx}] ", style="bold cyan")
            if authors:
                mid.append(f"{authors}")
                if year:
                    mid.append(f" ({year})")
                mid.append("\n")
            # Title (truncated)
            if title:
                display_title = title[:55] + "..." if len(title) > 55 else title
                mid.append(f'    "{display_title}"\n', style="italic")
            # DOI or URL
            if doi:
                mid.append(f"    doi:{doi[:30]}\n", style="dim")
            elif url:
                url_short = url[:35] + "..." if len(url) > 35 else url
                mid.append(f"    {url_short}\n", style="dim")
            mid.append("\n")

        return mid

    def _build_paper_panel(self, usable_lines: int):
        from rich.text import Text

        right = Text()

        if not self.title and not self.paper_sections:
            right.append("Waiting for content...\n", style="dim italic")
            return right

        # Build all content lines first, then auto-scroll
        lines: list[tuple[str, str]] = []  # (text, style)

        if self.title:
            lines.append((f"# {self.title}\n\n", "bold cyan"))

        if self.abstract:
            lines.append(("## Abstract\n", "bold"))
            lines.append((f"{self.abstract}\n\n", ""))

        # Show outline before sections are drafted
        if not self.paper_sections and self._outline:
            outline_data = self._outline.get("outline", {})
            thesis = self._outline.get("thesis", "")
            if thesis:
                lines.append(("Thesis\n", "bold magenta"))
                lines.append((f"{thesis}\n\n", "italic"))
            if outline_data:
                lines.append(("Planned Sections\n", "bold magenta"))
                for section_name, info in outline_data.items():
                    lines.append((f"  {section_name}\n", "bold"))
                    if isinstance(info, dict):
                        for pt in info.get("key_points", [])[:3]:
                            lines.append((f"    - {pt}\n", "dim"))
                lines.append(("\n", ""))

        for heading, content in self.paper_sections.items():
            is_writing = content == "(writing...)"
            lines.append((f"## {heading}\n", "bold"))
            if is_writing:
                lines.append(("(writing...)\n\n", "yellow italic"))
            else:
                # Fix literal \n\n that may sneak through
                clean = content.replace("\\n\\n", "\n\n").replace("\\n", "\n")
                lines.append((f"{clean}\n\n", ""))

        # Count total display lines (rough estimate: wrap at ~40 chars for right panel)
        total_lines = 0
        for text, _ in lines:
            total_lines += text.count("\n") + 1

        # Auto-scroll: if content overflows, skip earlier sections
        if total_lines > usable_lines:
            # Start from the end, keep what fits
            budget = usable_lines - 2  # reserve for "... above" indicator
            kept_lines: list[tuple[str, str]] = []
            running = 0
            for text, style in reversed(lines):
                line_count = text.count("\n") + 1
                if running + line_count <= budget:
                    kept_lines.insert(0, (text, style))
                    running += line_count
                else:
                    break

            # Count skipped sections
            skipped_sections = 0
            for text, style in lines:
                if (text, style) in kept_lines:
                    break
                if text.startswith("## "):
                    skipped_sections += 1

            if skipped_sections > 0:
                right.append(
                    f"... ({skipped_sections} sections above)\n\n", style="dim"
                )

            for text, style in kept_lines:
                if style:
                    right.append(text, style=style)
                else:
                    right.append(text)
        else:
            for text, style in lines:
                if style:
                    right.append(text, style=style)
                else:
                    right.append(text)

        return right

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._build_layout())
