"""Shared constants and configuration for AgentPub research pipelines."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Section ordering
# ---------------------------------------------------------------------------

_WRITE_ORDER = [
    "Methodology",
    "Results",
    "Discussion",
    "Related Work",
    "Introduction",
    "Limitations",
    "Conclusion",
]

_SUBMIT_ORDER = [
    "Introduction",
    "Related Work",
    "Methodology",
    "Results",
    "Discussion",
    "Limitations",
    "Conclusion",
]

# ---------------------------------------------------------------------------
# Word count targets and minimums per section
# ---------------------------------------------------------------------------

_SECTION_WORD_TARGETS: dict[str, int] = {
    "Introduction": 700,
    "Related Work": 1400,
    "Methodology": 1050,
    "Results": 1400,
    "Discussion": 1400,
    "Limitations": 350,
    "Conclusion": 350,
}

_SECTION_WORD_MINIMUMS: dict[str, int] = {
    "Introduction": 500,
    "Related Work": 1000,
    "Methodology": 700,
    "Results": 1000,
    "Discussion": 1000,
    "Limitations": 250,
    "Conclusion": 250,
}

# ---------------------------------------------------------------------------
# Checkpoint directory
# ---------------------------------------------------------------------------

_CHECKPOINT_DIR = pathlib.Path.home() / ".agentpub" / "checkpoints"

# ---------------------------------------------------------------------------
# Default empty research brief
# ---------------------------------------------------------------------------

_EMPTY_BRIEF: dict = {
    "title": "",
    "search_terms": [],
    "research_questions": [],
    "paper_type": "survey",
}

# ---------------------------------------------------------------------------
# Pipeline configuration
# ---------------------------------------------------------------------------


class ResearchInterrupted(Exception):
    """Raised when the user interrupts research with Ctrl+C."""

    def __init__(self, phase: int, artifacts: dict):
        self.phase = phase
        self.artifacts = artifacts
        super().__init__(f"Research interrupted during phase {phase}")


# ---------------------------------------------------------------------------
# Reference targets by paper complexity (Fix 2A)
# ---------------------------------------------------------------------------

_REF_TARGETS: dict[str, dict[str, int]] = {
    "single_domain": {"min": 20, "target": 28},
    "cross_domain": {"min": 35, "target": 45},
    "meta_analysis": {"min": 40, "target": 50},
}


@dataclass
class ResearchConfig:
    """Tuneable knobs for the research pipeline."""

    max_search_results: int = 30
    min_references: int = 20
    max_papers_to_read: int = 20
    max_reread_loops: int = 2
    api_delay_seconds: float = 0.5
    quality_level: str = "full"  # "full" or "lite" (for weaker models)
    verbose: bool = False
    min_total_words: int = 4000
    max_total_words: int = 15000
    target_words_per_section: int = 1000
    max_expand_passes: int = 4
    web_search: bool = True
