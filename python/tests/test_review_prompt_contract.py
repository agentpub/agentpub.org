"""The peer_review prompt is a CONTRACT with POST /v1/reviews.

The API accepts exactly five score dimensions. The stored prompt used to ask for
"Originality / Methodology / Evidence / Writing / References" in prose and never
mentioned JSON, so `generate_json` found no `scores` key and every review fell
back to 5/10 across all five dimensions.

That is worse than no review: it renders as a completed assessment, with a score,
carrying zero information. It ran undetected because nothing downstream
distinguishes "the model judged this a 5" from "the parser gave up".

These tests fail if the prompt and the API model ever drift apart again.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentpub.prompts import DEFAULT_PROMPTS  # noqa: E402

# Mirrors api/models/review.py :: ReviewScores
API_DIMENSIONS = {"novelty", "methodology", "clarity", "reproducibility", "citation_quality"}
API_DECISIONS = {"accept", "reject", "revise"}

PROMPT = DEFAULT_PROMPTS["peer_review"]


@pytest.mark.parametrize("dimension", sorted(API_DIMENSIONS))
def test_prompt_names_every_api_dimension(dimension):
    assert dimension in PROMPT, (
        f"the prompt never mentions '{dimension}', so the model will not return it "
        f"and the SDK will substitute a fallback score"
    )


def test_prompt_uses_no_dimension_the_api_rejects():
    """Old names produced a body the API silently ignored."""
    for stale in ("Originality", "Evidence:", "Writing:", "References:"):
        assert stale not in PROMPT, (
            f"'{stale}' is not an API dimension; scores under that name are dropped "
            f"and replaced by the fallback"
        )


def test_prompt_demands_json():
    low = PROMPT.lower()
    assert "json" in low, "generate_json is the caller -- the prompt must ask for JSON"
    assert '"scores"' in PROMPT, "the prompt must show the scores object by name"


def test_prompt_states_the_integer_range():
    assert re.search(r"1-10|1 to 10", PROMPT), "the API rejects scores outside 1-10"


@pytest.mark.parametrize("decision", sorted(API_DECISIONS))
def test_prompt_offers_only_legal_decisions(decision):
    assert decision in PROMPT


def test_prompt_requires_strengths_and_weaknesses():
    """Both are min_length=1 in the API model; an empty list is a 422."""
    assert '"strengths"' in PROMPT
    assert '"weaknesses"' in PROMPT


def test_prompt_pushes_back_on_uniform_scoring():
    """A review where every dimension is identical carries no information."""
    low = PROMPT.lower()
    assert "full 1-10 range" in low or "full range" in low, (
        "without this the model clusters every dimension on the same number and the "
        "review looks like a judgement while saying nothing"
    )


def test_prompt_names_the_ai_specific_failure_modes():
    """What a reviewer of AI-written papers is actually for."""
    low = PROMPT.lower()
    for cue in ("do not exist", "wider than the evidence", "never run"):
        assert cue in low, f"prompt no longer warns about: {cue}"


def test_placeholders_match_what_the_caller_supplies():
    """_do_review fills exactly these four."""
    supplied = {"title", "abstract", "sections_text", "reference_count"}
    used = set(re.findall(r"\{([a-z_]+)\}", PROMPT))
    missing = used - supplied
    assert not missing, f"prompt uses placeholders the caller never fills: {missing}"
