"""A review that was never accepted by the API must never be reported as OK.

On 18 Aug 2026 `agentpub agent review` printed:

    Failed to submit review: Client error '429 Too Many Requests'
      OK   [paper_2026_769eac]
      OK   [paper_2026_d4a043]
    Reviewed 5 papers.

Zero reviews existed on the platform afterwards. Two bugs combined:

1. `_do_review` caught the submit exception, logged a warning, and then returned
   the success dict anyway.
2. `generate_json` can return a JSON *array*; `review.get(...)` then raised
   "'list' object has no attribute 'get'" and the review died before submission.

Both are silent from the operator's side, which is what makes them expensive:
the reviewer queue looked serviced while nothing was being written.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentpub.playbook_researcher import PlaybookResearcher  # noqa: E402


class _Paper(SimpleNamespace):
    pass


def _paper():
    return _Paper(
        paper_id="paper_test_0001",
        title="A Paper",
        abstract="An abstract.",
        sections=[{"heading": "Intro", "content": "Body text."}],
        references=[],
    )


def _researcher(llm_returns, submit_raises=None):
    """A researcher with the LLM and client stubbed, nothing else."""
    r = object.__new__(PlaybookResearcher)

    calls = []

    class _LLM:
        def generate_json(self, system, user):
            return llm_returns

    class _Client:
        def submit_review(self, **kw):
            calls.append(kw)
            if submit_raises:
                raise submit_raises
            return {"review_id": "rev_1"}

    r.llm = _LLM()
    r.client = _Client()
    r._get_prompt = lambda *a, **k: (None, None)
    return r, calls


def test_submit_failure_is_reported_as_failure():
    """The regression: a 429 must not come back as a success dict."""
    r, calls = _researcher({"scores": {}}, submit_raises=Exception("Client error '429 Too Many Requests'"))
    out = r._do_review(_paper())
    assert "error" in out, f"submit failed but _do_review returned {out}"
    assert out.get("rate_limited") is True
    assert "decision" not in out, "a failed review must not carry a success payload"


def test_generic_submit_failure_also_reported():
    r, _ = _researcher({"scores": {}}, submit_raises=Exception("500 Internal Server Error"))
    out = r._do_review(_paper())
    assert "error" in out
    assert out.get("rate_limited") is False


def test_successful_submit_reports_success():
    r, calls = _researcher({
        "scores": {"novelty": 7, "methodology": 6, "clarity": 8,
                   "reproducibility": 5, "citation_quality": 6},
        "decision": "revise", "summary": "s",
        "strengths": ["a"], "weaknesses": ["b"],
    })
    out = r._do_review(_paper())
    assert "error" not in out
    assert out["decision"] == "revise"
    assert len(calls) == 1


def test_llm_returning_a_list_does_not_crash():
    """'list' object has no attribute 'get' -- 3 of 5 papers died on this."""
    r, calls = _researcher([{"scores": {"novelty": 9}, "decision": "accept",
                            "summary": "s", "strengths": ["a"], "weaknesses": ["b"]}])
    out = r._do_review(_paper())
    assert "error" not in out, f"list-wrapped JSON still fails: {out}"
    assert calls[0]["scores"]["novelty"] == 9
    assert calls[0]["decision"] == "accept"


def test_llm_returning_junk_still_submits_a_valid_body():
    """Garbage in must still produce something the API accepts, not a crash."""
    for junk in ([], ["not a dict"], "a string", None, 42):
        r, calls = _researcher(junk)
        out = r._do_review(_paper())
        assert "error" not in out, f"{junk!r} produced {out}"
        body = calls[0]
        assert set(body["scores"]) == {
            "novelty", "methodology", "clarity", "reproducibility", "citation_quality"}
        assert all(isinstance(v, int) and 1 <= v <= 10 for v in body["scores"].values())
        assert body["decision"] in {"accept", "reject", "revise"}
        assert body["strengths"] and body["weaknesses"]


def test_scores_are_coerced_into_the_api_contract():
    """The API takes integers 1-10 and 422s on anything else."""
    r, calls = _researcher({
        "scores": {"novelty": 12, "methodology": 0, "clarity": 7.6,
                   "reproducibility": "high", "citation_quality": -3},
        "decision": "maybe",
    })
    r._do_review(_paper())
    s = calls[0]["scores"]
    assert s["novelty"] == 10, "above-range score must clamp, not 422"
    assert s["methodology"] == 1, "below-range score must clamp"
    assert s["clarity"] == 8, "float must round to int"
    assert s["reproducibility"] == 5, "non-numeric falls back to the midpoint"
    assert s["citation_quality"] == 1
    assert calls[0]["decision"] == "revise", "invalid decision must fall back to a legal one"


def test_string_strengths_are_wrapped_not_dropped():
    """min_length=1 on both lists -- a bare string would otherwise 422."""
    r, calls = _researcher({"scores": {}, "strengths": "one strength", "weaknesses": []})
    r._do_review(_paper())
    assert calls[0]["strengths"] == ["one strength"]
    assert calls[0]["weaknesses"] == ["Not specified."]
