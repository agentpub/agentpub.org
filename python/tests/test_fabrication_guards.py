"""Guards against the fabrication paths a multi-agent review found in the pipeline.

Each test here corresponds to a confirmed finding. They are deliberately blunt:
they assert on the source text and the pure helpers, because the behaviours they
protect are properties of prompts and deterministic rewriters rather than of
anything reachable without a live LLM.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from agentpub import playbook_researcher as pr

SRC = pathlib.Path(pr.__file__).read_text(encoding="utf-8")
SDK_ROOT = pathlib.Path(pr.__file__).resolve().parents[1]

# Source with whole-line comments stripped. Several tests assert that a phrase no
# longer appears in any prompt; the comments explaining WHY a phrase was removed
# necessarily quote it, so they must not count as occurrences.
SRC_NO_COMMENTS = "\n".join(
    ln for ln in SRC.splitlines() if not ln.lstrip().startswith("#")
)


# ---------------------------------------------------------------------------
# The expansion prompts must not order the model to invent specifics
# ---------------------------------------------------------------------------


def test_no_prompt_asks_for_specific_findings_and_numbers():
    """The expand passes fire when the model has already shown it lacks grounded
    material. Asking for 'specific findings and numbers' from a title-only
    bibliography made fabrication the only way to comply."""
    assert "specific findings and numbers" not in SRC_NO_COMMENTS
    assert "with specific findings" not in SRC_NO_COMMENTS


def test_expand_guardrail_exists_and_permits_falling_short():
    """The word floor must be overridable by honesty, or the model has no legal
    way to comply when the sources are exhausted."""
    g = pr._EXPAND_GUARDRAIL
    assert "STOP" in g
    assert "under the word target" in g
    for phrase in ("from memory", "do not estimate", "must come from the source material"):
        assert phrase.lower() in g.lower(), phrase


def test_every_expand_prompt_carries_the_guardrail():
    """There are three expansion sites; all must be guarded, including the one
    in _step5_submit that runs after every integrity gate."""
    n_expand_prompts = SRC.count("expand_prompt = f\"\"\"")
    n_guarded = SRC.count("{_EXPAND_GUARDRAIL}")
    assert n_expand_prompts >= 3, f"expected >=3 expand prompts, found {n_expand_prompts}"
    assert n_guarded >= n_expand_prompts, (
        f"{n_expand_prompts} expand prompts but only {n_guarded} carry the guardrail"
    )


def test_no_expand_prompt_declares_the_word_floor_non_negotiable():
    """'NON-NEGOTIABLE' next to a word count is what made padding mandatory."""
    body = SRC.split("_EXPAND_GUARDRAIL = ", 1)[1]
    body = body.split("\n\n\nclass PlaybookResearcher", 1)[-1]
    assert "NON-NEGOTIABLE" not in body


# ---------------------------------------------------------------------------
# Count fixers must not falsify claims about cited works
# ---------------------------------------------------------------------------


def test_count_in_cited_sentence_is_protected():
    """'a meta-analysis drawing on 120 studies [Smith, 2020]' must not be
    rewritten to this paper's own corpus size — that fabricates a misquote."""
    text = "Earlier work is broad: a meta-analysis drawing on 120 studies [Smith, 2020] found no effect."
    start = text.index("120")
    assert pr._count_belongs_to_cited_work(text, start, start + 3) is True


def test_count_in_our_own_sentence_is_not_protected():
    text = "We reviewed 24 studies in total. The corpus was assembled in two passes."
    start = text.index("24")
    assert pr._count_belongs_to_cited_work(text, start, start + 2) is False


def test_citation_in_a_different_sentence_does_not_protect():
    """Sentence scoping must be tight, or the guard disables the fixer entirely."""
    text = "Prior work is extensive [Jones, 2019]. Our review covered 30 studies."
    start = text.index("30")
    assert pr._count_belongs_to_cited_work(text, start, start + 2) is False


@pytest.mark.parametrize("terminator", [". ", "\n", "! ", "? "])
def test_sentence_around_respects_boundaries(terminator):
    text = f"First sentence with [A, 2020]{terminator}Second has 42 studies here."
    start = text.index("42")
    got = pr._sentence_around(text, start, start + 2)
    assert "42" in got
    assert "[A, 2020]" not in got


def test_count_fixers_use_the_shared_guard():
    """Both deterministic count-rewrite passes must consult the guard."""
    assert SRC.count("_count_belongs_to_cited_work(") >= 3  # definition + 2 call sites


def test_count_fixers_do_not_use_replace_by_value():
    """str.replace() on a match's text rewrites the first textual occurrence
    anywhere in the section, not necessarily the matched one."""
    for bad in (
        'text = text.replace(m.group(0), m.group(0).replace(str(claimed), str(canonical), 1))',
        'text = text.replace(m.group(0), m.group(0).replace(str(claimed), str(correct), 1))',
    ):
        assert bad not in SRC


# ---------------------------------------------------------------------------
# Ranking weights are a single source of truth for the published Methodology
# ---------------------------------------------------------------------------


def test_ranking_weights_sum_to_one():
    total = sum(s["weight"] for s in pr._RANKING_WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9, f"weights sum to {total}"


def test_composite_score_does_not_inline_weights():
    """If a weight is inlined, the Methodology text can drift from the scorer —
    which is how every published paper came to state 40/25/15/10/10 while the
    code used six different weights."""
    block = SRC.split("composite = (", 1)[1].split(")", 1)[0]
    assert '_RANKING_WEIGHTS' in block or '_w[' in block
    assert not re.search(r"0\.\d+\s*\*", block), f"inlined weight in: {block!r}"


def test_methodology_text_renders_the_real_weights_and_cap():
    """Render section 2.4 the way the builder does and assert the percentages and
    the author cap match the constants the code executes."""
    letters = "abcdefghijk"
    clauses = []
    for i, (_k, spec) in enumerate(pr._RANKING_WEIGHTS.items()):
        pct = spec["weight"] * 100
        pct_txt = f"{pct:.0f}%" if abs(pct - round(pct)) < 1e-9 else f"{pct:.1f}%"
        clauses.append(f"({letters[i]}) {spec['label']} ({pct_txt}), assessed via {spec['basis']}")
    rendered = "; ".join(clauses)

    assert "topical relevance (35%)" in rendered
    assert "evidence accessibility (20%)" in rendered
    assert "citation impact (15%)" in rendered
    # The pre-fix hardcoded text claimed these; they must not reappear.
    assert "topical relevance (40%)" not in rendered
    assert "citation impact (25%)" not in rendered
    assert pr._MAX_PAPERS_PER_FIRST_AUTHOR == 5


def test_methodology_no_longer_hardcodes_the_false_spec():
    """Three separate Methodology paragraphs hardcoded this sentence and all three
    had drifted. Any percentage next to a signal name means someone re-hardcoded
    it instead of calling the renderer."""
    for stale in (
        "topical relevance (40%)",
        "topical fit (40%)",
        "citation impact (25%)",
        "foundational status (15%)",
        "three papers per first author",
        "keyword overlap between the article's title/abstract",
    ):
        assert stale not in SRC_NO_COMMENTS, stale


def test_all_methodology_ranking_descriptions_use_the_renderer():
    """Every place describing the ranking must derive it, not restate it."""
    assert SRC_NO_COMMENTS.count("_render_ranking_signals(") >= 4  # def + 3 call sites
    assert SRC_NO_COMMENTS.count("_render_author_cap_clause(") >= 3  # def + 2 call sites


def test_methodology_discloses_the_open_access_bias():
    """The evidence-accessibility term biases the corpus toward open access and
    was never disclosed in the published Methodology."""
    assert "biases the corpus" in SRC


# ---------------------------------------------------------------------------
# Reference verification must actually run, and must fail closed
# ---------------------------------------------------------------------------


def test_reference_verification_runs_on_the_live_path():
    """_step4_audit is dead code; verification must be reachable from _step5_submit."""
    assert "def _verify_references_live" in SRC
    submit = SRC.split("def _step5_submit", 1)[1]
    assert "self._verify_references_live(" in submit


def test_verification_runs_before_the_gate_that_reads_it():
    submit = SRC.split("def _step5_submit", 1)[1]
    call = submit.index("self._verify_references_live(")
    gate = submit.index('unverifiable = self.artifacts.get("unverifiable_references")')
    assert call < gate, "verification must populate the artifact before the gate reads it"


def _bare_researcher():
    """A PlaybookResearcher with only what _verify_references_live touches."""
    import types
    obj = pr.PlaybookResearcher.__new__(pr.PlaybookResearcher)
    obj.artifacts = {}
    obj.display = types.SimpleNamespace(step=lambda *a, **k: None)
    return obj


def test_verification_records_a_sentinel_when_it_cannot_run(monkeypatch):
    """BEHAVIOURAL: a verifier that blows up must not leave an empty list.

    An empty artifacts['unverifiable_references'] reads to the submit gate as
    'every reference checked out'. That is how a dead check looked healthy.
    """
    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no network")

    monkeypatch.setattr(pr, "ReferenceVerifier", Boom)
    obj = _bare_researcher()
    obj._verify_references_live([{"ref_id": "r1", "title": "A paper"}])

    recorded = obj.artifacts["unverifiable_references"]
    assert recorded, "verification failure must not record an empty list"
    assert recorded[0]["ref_id"] == "_verification_incomplete"


def test_verification_records_failed_references(monkeypatch):
    """BEHAVIOURAL: unverifiable refs reach the artifact the gate reads."""
    import types

    class _VR:
        def __init__(self, ref_id, title, verified, confidence):
            self.ref_id, self.title = ref_id, title
            self.verified, self.confidence = verified, confidence
            self.issues = ["not found in Crossref"] if not verified else []

    report = types.SimpleNamespace(
        references_verified=1, references_failed=1, references_uncertain=0,
        results=[
            _VR("ok", "A real paper", True, 1.0),
            _VR("bad", "A paper that does not exist", False, 0.0),
        ],
    )

    class FakeVerifier:
        def __init__(self, *a, **k):
            pass

        async def verify_all(self, refs, topic_keywords=None):
            return report

    monkeypatch.setattr(pr, "ReferenceVerifier", FakeVerifier)
    obj = _bare_researcher()
    obj._verify_references_live([
        {"ref_id": "ok", "title": "A real paper"},
        {"ref_id": "bad", "title": "A paper that does not exist"},
    ])

    recorded = obj.artifacts["unverifiable_references"]
    assert [r["ref_id"] for r in recorded] == ["bad"]
    assert obj.artifacts["reference_verification"]["checked"] == 2


def test_verification_does_not_clobber_an_existing_result(monkeypatch):
    """The legacy audit path may already have populated this."""
    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("should not have been constructed")

    monkeypatch.setattr(pr, "ReferenceVerifier", Boom)
    obj = _bare_researcher()
    obj.artifacts["unverifiable_references"] = []
    obj._verify_references_live([{"ref_id": "r1", "title": "x"}])
    assert obj.artifacts["unverifiable_references"] == []


def test_verification_fails_closed():
    """A check that could not run must not read as 'all references fine'."""
    fn = SRC.split("def _verify_references_live", 1)[1].split("\n    def ", 1)[0]
    assert "_verification_incomplete" in fn
    assert "_sentinel(" in fn
    # Every failure path records the sentinel rather than an empty list.
    assert fn.count("_sentinel(") >= 4


# ---------------------------------------------------------------------------
# Playbook documents must not authorise unverified or concealed claims
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", ["AGENT_PLAYBOOK.md", "WRITING_RULES.md"])
def test_playbooks_do_not_authorise_fabrication(doc):
    text = (SDK_ROOT / doc).read_text(encoding="utf-8")
    banned = [
        "cite real papers you know exist",
        "Skip DOI verification",
        "NEVER admit to not reading sources",
        "The current date is March 2026",
    ]
    for phrase in banned:
        assert phrase not in text, f"{doc} still contains: {phrase!r}"


def test_playbook_requires_marking_unverified_references_offline():
    text = (SDK_ROOT / "AGENT_PLAYBOOK.md").read_text(encoding="utf-8")
    assert "_unverified" in text
    assert "Crossref" in text


def test_writing_rules_requires_disclosing_reading_depth():
    text = (SDK_ROOT / "WRITING_RULES.md").read_text(encoding="utf-8")
    assert "Do NOT conceal reading depth" in text
    assert "assessed from abstracts" in text


def test_playbook_does_not_hardcode_a_date():
    text = (SDK_ROOT / "AGENT_PLAYBOOK.md").read_text(encoding="utf-8")
    assert not re.search(r"current date is \w+ \d{4}", text)


@pytest.mark.parametrize(
    "doc", ["AGENT_PLAYBOOK.md", "WRITING_RULES.md", "POST_PROCESSING.md", "process.md"]
)
def test_no_doc_mandates_a_scoring_formula_the_code_does_not_run(doc):
    """Docs must not restate the ranking weights.

    Every copy of these numbers had drifted from _composite_score, and two docs
    actively ORDERED agents to publish the stale formula (playbook check 12,
    WRITING_RULES rule 25) while POST_PROCESSING check 15 warned when the false
    text was absent. Describe the procedure; let the code render the numbers.
    """
    text = (SDK_ROOT / doc).read_text(encoding="utf-8")
    # Drop lines that are explaining the history of this very bug.
    lines = [
        ln for ln in text.splitlines()
        if not re.search(r"previously|earlier version|never implemented|fabricat", ln, re.I)
    ]
    body = "\n".join(lines)
    stale = [
        "topical relevance (40%)",
        "topical fit (40%)",
        "citation impact (25%)",
        "max 3 per first author",
        "max 3 papers per first author",
        "three papers per first author",
    ]
    hits = [s for s in stale if s in body]
    assert not hits, f"{doc} restates stale ranking numbers: {hits}"


# ---------------------------------------------------------------------------
# Web-search hits must be verified before entering the corpus
# ---------------------------------------------------------------------------


def test_web_search_hits_are_bibliographically_verified():
    block = SRC.split("if self.llm.supports_web_search:", 1)[1].split("# Platform search", 1)[0]
    assert "verify_paper_bibliographic(" in block, "web hits enter the corpus unverified"
    assert "_dedup_add([_v])" in block, "must add the verified record, not the raw hit"


def test_model_written_summary_never_becomes_the_abstract():
    """A model-composed summary must not be evidence-extraction input, or a
    'verbatim quote' can be lifted from text the model wrote."""
    block = SRC.split("if self.llm.supports_web_search:", 1)[1].split("# Platform search", 1)[0]
    assert "llm_summary" in block
    assert '_v["abstract"] = _hit' not in block


# ---------------------------------------------------------------------------
# The enrichment cap must be principled and disclosed, not positional and hidden
# ---------------------------------------------------------------------------


def test_enrichment_cap_is_not_positional():
    """`all_papers[:60]` took papers by arrival order, which preferentially
    discarded claim-evidence, gap and forward-citation results because those are
    appended last — the most purpose-built sources in the corpus."""
    assert "all_papers[:60]" not in SRC_NO_COMMENTS
    assert "_pre_enrichment_rank" in SRC_NO_COMMENTS


def test_enrichment_cap_is_recorded_for_the_methodology():
    for key in (
        "pre_enrichment_candidates",
        "enrichment_cap",
        "capped_out_before_enrichment",
        "enrichment_cap_criterion",
    ):
        assert key in SRC_NO_COMMENTS, key


def test_methodology_no_longer_credits_relevance_scoring_for_a_positional_cut():
    """Papers dropped by the cap were never scored, so saying relevance screening
    reduced the corpus to N is a false PRISMA claim."""
    assert "Automated relevance scoring reduced this to" not in SRC_NO_COMMENTS


def test_methodology_discloses_unscreened_papers_when_the_cap_bites():
    """Section 2.5 must say the capped-out papers were never assessed, and must
    say nothing about a cap when it did not apply."""
    assert "were not assessed individually" in SRC_NO_COMMENTS
    assert "_capped_out and _cap" in SRC_NO_COMMENTS  # only claimed when true
    assert "All unique articles were retrieved and screened" in SRC_NO_COMMENTS


def test_flow_table_accounts_for_papers_that_were_never_screened():
    """A PRISMA-style table must account for every dropped record."""
    assert "Not individually assessed" in SRC_NO_COMMENTS
    assert "Retrieved for screening (rate limit)" in SRC_NO_COMMENTS


# ---------------------------------------------------------------------------
# Playbook: autonomy scoped to the task, no credential-in-prompt template
# ---------------------------------------------------------------------------


def test_playbook_does_not_tell_the_agent_to_self_approve():
    """The run must stay autonomous, but 'approve it yourself' / 'never ask
    permission to make HTTP calls' generalises to moments where asking is right."""
    text = (SDK_ROOT / "AGENT_PLAYBOOK.md").read_text(encoding="utf-8")
    for phrase in (
        "approve it yourself",
        "Never ask permission to make HTTP calls",
        "Never ask me to approve anything",
        "blanket permission for ALL tool calls",
    ):
        assert phrase not in text, f"still present: {phrase!r}"


def test_playbook_keeps_the_walk_away_property():
    """Scoping autonomy must not reintroduce mid-run stops — the whole point is
    that the human is involved at launch and after submission only."""
    text = (SDK_ROOT / "AGENT_PLAYBOOK.md").read_text(encoding="utf-8")
    assert "walk away and come back to find a submitted paper" in text
    assert "without asking for approval, confirmation, or feedback" in text
    # Out-of-scope work is skipped and reported, never blocked on a human.
    assert "do not stop and wait either" in text


def test_playbook_uses_an_api_key_not_a_literal_password():
    text = (SDK_ROOT / "AGENT_PLAYBOOK.md").read_text(encoding="utf-8")
    assert "mypassword" not in text
    assert "password is" not in text
    assert "AGENTPUB_API_KEY" in text


def test_research_guide_foundational_rule_is_relative_not_absolute():
    """A fixed '5 refs with 500+ citations, 3 pre-2015' is unsatisfiable for any
    subfield younger than ~3 years, and an unmeetable rule gets skipped."""
    text = (SDK_ROOT / "RESEARCH_GUIDE.md").read_text(encoding="utf-8")
    assert "top decile of citations-per-year" in text
    assert "Young field" in text and "Mature field" in text
    assert "state this in Limitations" in text


# ---------------------------------------------------------------------------
# Sourcing: scholarly indexes first, grey literature capped
# ---------------------------------------------------------------------------
#
# A real run produced 37 references of which only ~10 were peer-reviewed or
# preprint. The agent's own diagnosis: it never called the platform's scholarly
# search, told its sub-agents to prioritise McKinsey/Deloitte/Gartner/BCG/PwC/
# HBR, applied a hard 2024-2026 recency window (which favours whatever publishes
# fastest), and dropped paywalled journal articles on 403 while consulting
# reports survived because their findings are mirrored free everywhere.


def test_research_guide_points_at_the_scholarly_search_endpoint():
    """/v1/search/academic fans out over five free scholarly indexes. It existed
    and no document mentioned it, so agents used generic web search."""
    text = (SDK_ROOT / "RESEARCH_GUIDE.md").read_text(encoding="utf-8")
    assert "/v1/search/academic" in text
    assert "search_academic_papers" in text
    assert "General web search is not a literature search" in text


def test_playbook_points_at_scholarly_search_at_the_research_step():
    text = (SDK_ROOT / "AGENT_PLAYBOOK.md").read_text(encoding="utf-8")
    assert "/v1/search/academic" in text


@pytest.mark.parametrize("doc", ["RESEARCH_GUIDE.md", "AGENT_PLAYBOOK.md"])
def test_docs_forbid_prioritising_consulting_firms(doc):
    text = (SDK_ROOT / doc).read_text(encoding="utf-8")
    assert "Never instruct yourself or a sub-agent" in text or "Never tell yourself" in text


def test_research_guide_sets_a_source_type_mix():
    text = (SDK_ROOT / "RESEARCH_GUIDE.md").read_text(encoding="utf-8")
    assert "at least 60%" in text
    assert "at most 20%" in text


def test_research_guide_separates_verifying_from_reading():
    """Dropping a source because the publisher returned 403 deletes the
    peer-reviewed half of a corpus and keeps the marketing half — consulting
    findings are mirrored free, paywalled articles are not."""
    text = (SDK_ROOT / "RESEARCH_GUIDE.md").read_text(encoding="utf-8")
    assert "Verifying a reference is not the same as reading it" in text
    assert "403 from a publisher is not a failed" in text
    assert "stays in the corpus" in text


def test_recency_rule_does_not_mechanically_favour_grey_literature():
    """Peer review lags 6-18 months; press releases take weeks. A blanket
    recency preference selects for fast-to-publish, not for current."""
    text = (SDK_ROOT / "RESEARCH_GUIDE.md").read_text(encoding="utf-8")
    assert "quietly selects grey literature" in text
    assert "Do NOT go further and prefer recent sources generally" in text


def test_research_guide_does_not_order_deletion_of_recent_references():
    """It ordered agents to "REMOVE entirely" every current-year reference
    because "evaluators WILL flag as fabricated" — deleting real, resolvable
    primary sources to game a scoring model, while contradicting the recency
    requirement 400 lines earlier. Verified live: a paper citing 25 recent
    sources was flagged for "fabricated references" though every DOI and arXiv
    ID resolved with matching titles."""
    text = (SDK_ROOT / "RESEARCH_GUIDE.md").read_text(encoding="utf-8")
    assert "REMOVE entirely" not in text
    assert "Remove all 2026+ references" not in text
    assert "Do not delete recent references. Verify them." in text
    assert "not to remove it" in text
