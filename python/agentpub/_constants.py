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
    "Related Work": 900,
    "Methodology": 600,
    "Results": 900,
    "Discussion": 900,
    "Limitations": 250,
    "Conclusion": 250,
}

# ---------------------------------------------------------------------------
# Token limits per section (max_tokens passed to LLM generate)
# These are capped by each model's actual limit via _effective_max_tokens()
# ---------------------------------------------------------------------------

_SECTION_TOKEN_LIMITS: dict[str, int] = {
    "Introduction": 65000,
    "Related Work": 65000,
    "Methodology": 65000,
    "Results": 65000,
    "Discussion": 65000,
    "Limitations": 65000,
    "Conclusion": 65000,
    "Abstract": 16000,
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
# CorpusManifest — single source of truth for corpus counts (Change 1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorpusManifest:
    """Frozen record of corpus counts at each pipeline stage.

    Created once after the research phase.  Every part of the pipeline
    that needs "how many papers" uses ``display_count`` — no other
    source of truth exists.
    """

    total_retrieved: int = 0
    total_after_dedup: int = 0
    total_after_filter: int = 0
    total_included: int = 0
    total_in_final_refs: int = 0
    full_text_count: int = 0
    abstract_only_count: int = 0
    databases: tuple[str, ...] = ()
    year_range: str = ""

    @property
    def display_count(self) -> int:
        """The ONE number to use everywhere for 'N studies reviewed'."""
        return self.total_in_final_refs if self.total_in_final_refs else self.total_included


# ---------------------------------------------------------------------------
# PipelineStep — structured process log entry (Change 3)
# ---------------------------------------------------------------------------

@dataclass
class PipelineStep:
    """A single recorded step in the pipeline process log."""

    name: str           # e.g. "search", "dedup", "filter", "enrich", "write", "validate"
    description: str    # human-readable summary of what happened
    timestamp: float    # time.time() when step completed
    input_count: int = 0   # items entering this step
    output_count: int = 0  # items leaving this step
    details: dict = None   # type: ignore[assignment]

    def __post_init__(self):
        if self.details is None:
            self.details = {}


# ---------------------------------------------------------------------------
# Reference targets by paper complexity (Fix 2A)
# ---------------------------------------------------------------------------

_REF_TARGETS: dict[str, dict[str, int]] = {
    "single_domain": {"min": 20, "target": 28},
    "cross_domain": {"min": 35, "target": 45},
    "meta_analysis": {"min": 40, "target": 50},
}


@dataclass
class ParagraphSpec:
    """Specification for a single paragraph to be written."""

    paragraph_id: str           # "results_p3"
    section: str                # "Results"
    goal: str                   # "Compare SWS vs REM effect sizes on declarative memory"
    claim_type: str             # "descriptive_synthesis" | "corpus_bounded_inference" | "gap_identification"
    evidence_indices: list[int] = None  # paper indices from curated list  # type: ignore[assignment]
    allowed_citations: list[str] = None  # ["[Gais and Born, 2004]", "[Rasch and Born, 2013]"]  # type: ignore[assignment]
    allowed_strength: str = "strong"  # "strong" | "moderate" | "weak"
    transition_from: str | None = None  # previous paragraph_id
    target_words: int = 160

    def __post_init__(self):
        if self.evidence_indices is None:
            self.evidence_indices = []
        if self.allowed_citations is None:
            self.allowed_citations = []


@dataclass
class WrittenParagraph:
    """A single written paragraph with metadata."""

    paragraph_id: str
    section: str
    text: str
    citations_used: list[str] = None  # type: ignore[assignment]
    word_count: int = 0

    def __post_init__(self):
        if self.citations_used is None:
            self.citations_used = []
        if not self.word_count and self.text:
            self.word_count = len(self.text.split())


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
    pipeline_mode: str = "paragraph"  # "paragraph" (per-paragraph) | "section" (per-section legacy)
    # Per-section token limits (override _SECTION_TOKEN_LIMITS defaults)
    section_token_limits: dict | None = None
    # Per-section word targets (override _SECTION_WORD_TARGETS defaults)
    section_word_targets: dict | None = None
    # Per-section word minimums (override _SECTION_WORD_MINIMUMS defaults)
    section_word_minimums: dict | None = None
    # Adversarial review loop (harness engineering pattern)
    adversarial_review_enabled: bool = True
    adversarial_max_cycles: int = 2
    adversarial_fix_majors: bool = True  # fix MAJOR findings too, not just FATAL
    # Paragraph-level writing (pipeline_mode="paragraph")
    paragraph_stitch: bool = True       # enable transition smoothing between paragraphs
    paragraph_target_words: int = 160   # default per paragraph
    # Novelty check (inspired by AI Scientist-v2)
    novelty_check_enabled: bool = True
    novelty_similarity_threshold: float = 0.7
    # Structured reflection pass
    structured_reflection_enabled: bool = True
    # Citation gap fill during writing
    citation_gap_fill_enabled: bool = True
    max_gap_fills_per_section: int = 3
    # Citation justification audit
    citation_justification_audit: bool = True
    # Review model routing
    review_model: str | None = None  # optional separate model for review passes
    review_provider: str | None = None  # optional separate provider for review passes


@dataclass
class ReviewFinding:
    """A single finding from the adversarial review."""

    severity: str  # "FATAL", "MAJOR", "MINOR"
    category: str  # e.g. "citation_mismatch", "fabrication", "overclaiming"
    section: str  # affected section name
    quote: str  # exact text from the paper
    problem: str  # what is wrong
    suggested_fix: str  # how to fix it
    resolved: bool = False


@dataclass
class AdversarialReviewReport:
    """Result of one adversarial review cycle."""

    cycle: int
    findings: list  # list[ReviewFinding]

    @property
    def fatal_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "FATAL")

    @property
    def major_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "MAJOR")

    @property
    def minor_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "MINOR")

    @property
    def needs_fixes(self) -> bool:
        return self.fatal_count > 0
