"""Unit tests for the corpus-consistency validator.

Locks in the behaviour that distinguishes a real bug (the writer claiming two
different corpus sizes in two sections) from legitimate sub-counts and search-
funnel narration.
"""
from __future__ import annotations

from agentpub.corpus_consistency_validator import (
    _build_canonical,
    validate_corpus_consistency,
)


def _refs(n: int) -> list[dict]:
    return [{"id": f"r{i}"} for i in range(n)]


# ---------------------------------------------------------------------------
# canonical set
# ---------------------------------------------------------------------------

def test_build_canonical_pulls_funnel_and_refs():
    canonical = _build_canonical(
        {"total_retrieved": 200, "total_after_filter": 30, "total_included": 25, "bogus": -1},
        _refs(25),
    )
    assert canonical["total_retrieved"] == 200
    assert canonical["total_after_filter"] == 30
    assert canonical["total_included"] == 25
    assert canonical["total_references"] == 25
    assert "bogus" not in canonical  # negative / non-positive ignored


# ---------------------------------------------------------------------------
# cross-section drift (the blocking case)
# ---------------------------------------------------------------------------

def test_cross_section_drift_is_blocking():
    sections = {
        "abstract": "We synthesize findings from 15 papers on the topic.",
        "methodology": "After screening, 22 papers were included in this review.",
    }
    report = validate_corpus_consistency(sections, search_audit=None, references=[])
    assert report.has_blocking_issues()
    drift = report.cross_section_drift[0]
    assert drift["noun"] == "papers"
    assert drift["counts_seen"] == [15, 22]


def test_consistent_counts_do_not_drift():
    sections = {
        "abstract": "We synthesize findings from 20 papers.",
        "methodology": "All 20 papers were included after screening.",
    }
    report = validate_corpus_consistency(sections, search_audit=None, references=_refs(20))
    assert not report.has_blocking_issues()


def test_same_section_subcounts_not_flagged_as_drift():
    # Two different counts in ONE section is a legitimate sub-count, not drift.
    sections = {
        "results": "Of these, 12 papers reported gains and 8 papers reported no effect.",
    }
    report = validate_corpus_consistency(sections, search_audit=None, references=[])
    assert not report.has_blocking_issues()


# ---------------------------------------------------------------------------
# funnel-drift suppression
# ---------------------------------------------------------------------------

def test_search_funnel_drift_is_suppressed():
    # Methodology cites the post-filter count, intro cites the final corpus.
    # Both are canonical funnel stages and the final corpus is anchored, so this
    # is narration, not contradiction.
    sections = {
        "introduction": "This review covers 32 papers spanning a decade.",
        "methodology": "30 papers remained after full-text screening.",
    }
    report = validate_corpus_consistency(
        sections,
        search_audit={"total_after_filter": 30, "total_included": 32},
        references=_refs(32),
    )
    assert not report.has_blocking_issues()


def test_drift_not_suppressed_when_final_corpus_not_anchored():
    # Same numbers, but neither equals total_references → funnel anchor missing,
    # so the drift is still reported.
    sections = {
        "introduction": "This review covers 32 papers.",
        "methodology": "30 papers remained after screening.",
    }
    report = validate_corpus_consistency(
        sections,
        search_audit={"total_after_filter": 30, "total_included": 32},
        references=_refs(99),
    )
    assert report.has_blocking_issues()


# ---------------------------------------------------------------------------
# off-canonical warnings & filters
# ---------------------------------------------------------------------------

def test_off_canonical_single_mention_warns_but_does_not_block():
    sections = {"abstract": "We reviewed 50 studies in this analysis."}
    report = validate_corpus_consistency(sections, search_audit=None, references=_refs(10))
    assert not report.has_blocking_issues()
    assert report.summary()["off_canonical_count"] == 1


def test_year_shaped_and_tiny_counts_are_ignored():
    sections = {"intro": "Between 2000 and 2020 articles proliferated; we cite 2 studies here."}
    report = validate_corpus_consistency(sections, search_audit=None, references=[])
    # "2020 articles" is year-shaped; "2 studies" is below the n>=3 floor.
    assert report.summary()["total_mentions"] == 0
