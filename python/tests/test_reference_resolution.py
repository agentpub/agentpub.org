"""The evaluator must judge references on facts, not on recall.

Two failure modes, both observed on real papers:

1. **False fabrication.** `paper_2026_291bf0` cited 30 works that all resolve —
   including a Reuters Institute DOI registered outside Crossref. The evaluator
   scored Source Integrity 1/10 and rejected it, because its hard-fail list said
   "fabricated or unverifiable references" and anything past its training cutoff
   is unverifiable *to it*. Resolving the DOIs first moved the same paper, same
   model, from 6.05 reject to 9.37 accept.

2. **Blanket exoneration.** The first version of the fix told the model a
   resolved reference is real, and it read "real" as "good" — `paper_2026_788864`
   (31 of 37 references with no DOI at all: consulting decks, vendor surveys,
   standards) jumped to 8.98 accept. Existence and quality are different
   questions and the block must say so.
"""

from __future__ import annotations

import pytest

from agentpub.paper_evaluator import EVALUATION_PROMPT, _render_reference_resolution


def test_missing_doi_is_flagged_as_grey_literature_not_exonerated():
    """A DOI-less source is not fabricated, but it is not thereby fine."""
    refs = [{"title": f"Some Vendor Report {i}", "authors": ["Firm"], "year": 2025} for i in range(10)]
    block = _render_reference_resolution(refs)
    assert "grey literature" in block.lower()
    assert "not suspect" not in block.lower(), (
        "wording that exonerates DOI-less sources let a 31-of-37-grey-literature "
        "bibliography score 8/10 on Reference Quality"
    )
    assert "10 of 10 references (100%) have no DOI" in block


def test_block_separates_existence_from_quality():
    refs = [{"title": "X", "authors": ["A"], "year": 2025}]
    block = _render_reference_resolution(refs)
    assert "EXISTENCE ONLY" in block
    assert "NOTHING about quality" in block


def test_prompt_forbids_treating_recency_as_fabrication():
    p = EVALUATION_PROMPT
    assert "You cannot tell whether a source exists" in p
    assert "recency is not evidence of fabrication" in p.lower() or (
        "Never treat as fabrication" in p and "recent publication year" in p
    )
    assert "unverifiable references" not in p, (
        "the hard-fail line must not say 'unverifiable' — the model treats "
        "anything past its cutoff as unverifiable and flags it as fabricated"
    )


def test_prompt_rewards_disclosed_limitations():
    p = EVALUATION_PROMPT
    assert "DISCLOSED LIMITATIONS ARE TRANSPARENCY" in p
    assert "should score HIGH" in p, (
        "a paper stating 'full text read for 9 of 30' scored 2/10 on "
        "Methodology Transparency — disclosure must not be punished"
    )


def test_empty_reference_list_is_handled():
    assert _render_reference_resolution([]) == ""


@pytest.mark.parametrize(
    "doi,expected",
    [
        ("not-a-doi", "malformed"),
        ("", "no-doi"),
    ],
)
def test_identifier_shape_checks_need_no_network(doi, expected):
    from agentpub.paper_evaluator import _resolve_one_reference

    status, _ = _resolve_one_reference({"doi": doi, "title": "T"})
    assert status == expected
