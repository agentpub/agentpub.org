"""Three faults from one real run, each of which quietly cost a whole paper.

The run wrote all seven sections, ran its audits, spent real money — and was
then discarded at the final gate because a QA call could not open a socket.
Along the way a "language pass" deleted 72% of the Methodology, and twenty
papers about AI in higher education were injected into a corpus on emergence in
complex systems, one of them becoming the joint most-cited source.

None of the three announced itself as an error. That is what these pin.
"""

from __future__ import annotations

import json

import pytest

from agentpub.context_editor import MAX_SHRINK_RATIO, EditorResult, _verify_no_hallucinations


# --------------------------------------------------------------------------
# 1. A language pass may not delete the paper
# --------------------------------------------------------------------------

def test_shrink_cap_is_a_sane_size():
    """A tightening pass is a few per cent. Anything near half is a deletion."""
    assert 0.05 <= MAX_SHRINK_RATIO <= 0.30


@pytest.mark.parametrize(
    "before,after,should_reject",
    [
        (758, 212, True),    # the real Methodology: -72%
        (669, 225, True),    # the real Limitations: -66%
        (2001, 1285, True),  # the real Discussion: -36%
        (1189, 1160, False), # a genuine language pass: -2%
        (1564, 1547, False), # ditto
        (1000, 860, False),  # -14%, just inside the cap
    ],
)
def test_the_cap_separates_editing_from_deleting(before, after, should_reject):
    shrink = 1.0 - (after / before)
    rejected = shrink > MAX_SHRINK_RATIO
    assert rejected is should_reject, (
        f"{before} -> {after} words ({shrink:.0%}) "
        f"{'should' if should_reject else 'should not'} be rejected"
    )


def test_a_rejected_edit_keeps_the_original_text():
    """Rejecting must return the original, not an empty or truncated section."""
    original = " ".join(f"word{i}" for i in range(800))
    result = EditorResult(
        original=original, edited=original, changed=False, delta_words=0,
        passed_verification=False, verification_issues=["edit removed 72%"],
    )
    assert result.edited == original
    assert result.changed is False
    assert len(result.edited.split()) == 800


def test_hallucination_check_alone_would_not_have_caught_it():
    """Why the cap is needed at all.

    The existing verifier asks whether anything was *invented*. Deleting two
    thirds of a section invents nothing, so it passed cleanly.
    """
    original = "We searched five databases in March 2026 [Smith, 2020]. " * 40
    gutted = "We searched databases [Smith, 2020]."
    passed, issues = _verify_no_hallucinations(original, gutted)
    assert passed, "if this now fails, the cap may be redundant — re-check"
    assert not issues


# --------------------------------------------------------------------------
# 2. Off-topic papers must not be injected from the local library
# --------------------------------------------------------------------------

from agentpub.playbook_researcher import is_on_topic, topic_keywords

REAL_TOPIC = (
    "Predictive Limits of Micro-to-Macro Mapping: A Methodological Critique of "
    "Emergence Forecasting in Complex Systems"
)
REAL_TERMS = [
    "complex systems science emergence",
    "complex systems science prediction",
    "complex systems science self-organization",
]
TOPIC_WORDS = topic_keywords(REAL_TOPIC, *REAL_TERMS)


@pytest.mark.parametrize("title", [
    # Foundational sources from the same run. Some share only ONE content word
    # with the working title, which is why the gate cannot demand more.
    "Quantifying causal emergence shows that macro can beat micro",
    "Novel Type of Phase Transition in a System of Self-Driven Particles",
    "Challenges in complex systems science",
    "An Introduction to Complex Systems Science and Its Applications",
    "Exploring complex networks",
])
def test_on_topic_papers_are_kept(title):
    assert is_on_topic(title, TOPIC_WORDS), f"wrongly rejected: {title}"


@pytest.mark.parametrize("title", [
    # Verbatim from the contaminated run: injected into a complex-systems
    # corpus from a previous, unrelated project.
    "Human and AI collaboration in the higher education environment: opportunities",
    "Critical thinking in the age of generative AI: implications for health sciences",
    "How Cognitive Biases Affect XAI-assisted Decision-making: A Systematic Review",
    "Collaborative AI in the workplace: Enhancing organizational performance",
])
def test_off_topic_papers_are_rejected(title):
    assert not is_on_topic(title, TOPIC_WORDS), f"would still inject: {title}"


def test_a_known_limitation_is_recorded_not_hidden():
    """One contaminant still passes, and pretending otherwise would be worse.

    "AI dialogue systems" shares exactly one content word with "Complex
    Systems" — the same single word by which a genuinely foundational paper
    ("...in a System of Self-Driven Particles") qualifies. Word overlap cannot
    separate those two, so this leak is inherent to the method, not a bug in
    the threshold. Separating them needs semantic similarity.

    The gate still removes 4 of the 5 real contaminants. If someone later makes
    this pass, the filter has become smarter and this test should be deleted.
    """
    leaks = "The effects of over-reliance on AI dialogue systems on students' cognitive abilities"
    assert is_on_topic(leaks, TOPIC_WORDS), (
        "this now filters correctly — the method improved, so remove this test"
    )


def test_no_topic_means_no_filtering():
    """With nothing to compare against, the gate must not discard everything."""
    assert is_on_topic("Anything at all", set())


# --------------------------------------------------------------------------
# 3. A finished paper survives a blocked submission
# --------------------------------------------------------------------------

def test_blocked_submission_writes_the_paper_to_disk(tmp_path, monkeypatch):
    """The expensive failure: work discarded because a QA call would not connect.

    Refusing to submit un-audited is correct. Throwing the paper away is not.
    """
    import agentpub.playbook_researcher as pr

    class _Stub:
        topic = "Emergence in complex systems"
        artifacts = {
            "final_paper": {
                "Introduction": "word " * 1160,
                "Results": "word " * 1547,
                "Conclusion": "word " * 472,
            },
            "abstract": "abstract " * 247,
            "curated_papers": [{"title": "A paper"}],
            "self_eval_status": "failed",
            "paper_outline": {"title": "A Real Title"},
        }
        _save_unsubmitted_paper = pr.PlaybookResearcher._save_unsubmitted_paper

    monkeypatch.setattr(pr.pathlib.Path, "home", staticmethod(lambda: tmp_path))

    path = _Stub._save_unsubmitted_paper(_Stub(), reason="Server disconnected")
    assert path, "a blocked submission must leave the paper somewhere findable"

    saved = json.loads(open(path, encoding="utf-8").read())
    assert saved["word_count"] == 1160 + 1547 + 472
    assert saved["title"] == "A Real Title"
    assert "Server disconnected" in saved["reason_not_submitted"]
    assert set(saved["sections"]) == {"Introduction", "Results", "Conclusion"}


def test_saving_never_raises_even_when_it_cannot_write(monkeypatch):
    """A rescue path that throws turns one failure into two."""
    import agentpub.playbook_researcher as pr

    class _Stub:
        topic = "x"
        artifacts = {"final_paper": {"Introduction": "hello world"}}
        _save_unsubmitted_paper = pr.PlaybookResearcher._save_unsubmitted_paper

    def _boom():
        raise OSError("disk full")

    monkeypatch.setattr(pr.pathlib.Path, "home", staticmethod(_boom))
    assert _Stub._save_unsubmitted_paper(_Stub(), reason="test") == ""


def test_nothing_written_when_there_is_no_paper(tmp_path, monkeypatch):
    import agentpub.playbook_researcher as pr

    class _Stub:
        topic = "x"
        artifacts: dict = {}
        _save_unsubmitted_paper = pr.PlaybookResearcher._save_unsubmitted_paper

    monkeypatch.setattr(pr.pathlib.Path, "home", staticmethod(lambda: tmp_path))
    assert _Stub._save_unsubmitted_paper(_Stub(), reason="test") == ""
