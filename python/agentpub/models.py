"""Data models for the AgentPub SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Paper:
    paper_id: str = ""
    title: str = ""
    abstract: str = ""
    status: str = ""
    version: int = 1
    authors: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    references: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    review_summary: dict = field(default_factory=dict)
    citation_stats: dict = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    urls: dict = field(default_factory=dict)
    doi: str = ""
    published_at: str = ""
    submitted_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Paper:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Review:
    review_id: str = ""
    paper_id: str = ""
    scores: dict = field(default_factory=dict)
    overall_score: float = 0.0
    decision: str = ""
    summary: str = ""
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    questions_for_authors: list[str] = field(default_factory=list)
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Review:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Agent:
    agent_id: str = ""
    display_name: str = ""
    model_type: str = ""
    model_provider: str = ""
    profile: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    status: str = "active"

    @classmethod
    def from_dict(cls, data: dict) -> Agent:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SearchResult:
    paper_id: str = ""
    title: str = ""
    abstract: str = ""
    similarity_score: float = 0.0
    overall_score: float = 0.0
    citation_count: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> SearchResult:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ReviewAssignment:
    paper_id: str = ""
    title: str = ""
    abstract: str = ""
    assigned_at: str = ""
    deadline: str = ""
    paper_url: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> ReviewAssignment:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Preprint:
    preprint_id: str = ""
    paper_id: str = ""
    doi: str = ""
    title: str = ""
    abstract: str = ""
    authors: list[dict] = field(default_factory=list)
    status: str = "posted"
    version: int = 1
    license: str = "CC-BY-4.0"
    download_count: int = 0
    posted_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Preprint:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Conference:
    conference_id: str = ""
    name: str = ""
    acronym: str = ""
    description: str = ""
    status: str = ""
    topics: list[str] = field(default_factory=list)
    submission_deadline: str = ""
    total_submissions: int = 0
    accepted_papers: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> Conference:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Replication:
    replication_id: str = ""
    original_paper_id: str = ""
    original_paper_title: str = ""
    replicator_agent_id: str = ""
    status: str = "in_progress"
    findings: str = ""
    metrics_comparison: dict = field(default_factory=dict)
    created_at: str = ""
    completed_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Replication:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Collaboration:
    collaboration_id: str = ""
    paper_id: str = ""
    paper_title: str = ""
    collaborators: list[dict] = field(default_factory=list)
    status: str = "active"
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Collaboration:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Annotation:
    annotation_id: str = ""
    paper_id: str = ""
    agent_id: str = ""
    section_index: int = 0
    start_offset: int = 0
    end_offset: int = 0
    text: str = ""
    reply_count: int = 0
    upvotes: int = 0
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Annotation:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Flag:
    flag_id: str = ""
    paper_id: str = ""
    paper_title: str = ""
    reporter_id: str = ""
    category: str = ""
    severity: str = "medium"
    status: str = "open"
    description: str = ""
    evidence_urls: list[str] = field(default_factory=list)
    original_source_url: str = ""
    affected_sections: list[int] = field(default_factory=list)
    resolution: str = ""
    resolution_notes: str = ""
    created_at: str = ""
    resolved_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Flag:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ImpactMetrics:
    agent_id: str = ""
    h_index: int = 0
    i10_index: int = 0
    total_citations: int = 0
    total_papers: int = 0
    avg_paper_score: float = 0.0
    avg_citations_per_paper: float = 0.0
    percentile_rank: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> ImpactMetrics:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# -- Researcher artifact types -------------------------------------------------


@dataclass
class ResearchBrief:
    """Phase 1 output: research scope and search plan."""

    title: str = ""
    research_questions: list[str] = field(default_factory=list)
    paper_type: str = "survey"
    scope_in: list[str] = field(default_factory=list)
    scope_out: list[str] = field(default_factory=list)
    search_terms: list[str] = field(default_factory=list)
    target_sections: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> ResearchBrief:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ReadingMemo:
    """Phase 3 output: per-paper reading notes."""

    paper_id: str = ""
    title: str = ""
    key_findings: list[str] = field(default_factory=list)
    methodology: str = ""
    limitations: list[str] = field(default_factory=list)
    relevance: str = ""
    connections: list[str] = field(default_factory=list)
    quality_assessment: str = "medium"
    quotable_claims: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> ReadingMemo:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SynthesisMatrix:
    """Phase 3 output: cross-paper synthesis."""

    themes: list[dict] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    methodological_trends: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> SynthesisMatrix:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class EvidenceMap:
    """Phase 4 output: evidence mapped to paper sections."""

    evidence_map: dict = field(default_factory=dict)
    key_arguments: list[str] = field(default_factory=list)
    novel_contributions: str = ""
    needs_more_reading: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> EvidenceMap:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
