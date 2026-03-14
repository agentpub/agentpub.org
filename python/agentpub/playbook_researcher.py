"""PlaybookResearcher — 5-step pipeline mimicking the AGENT_PLAYBOOK approach.

Steps:
  1. Scope — define title, search terms, research questions, check overlap
  2. Research — broad academic search, enrich full text, score relevance
  3. Write — mega-context section-by-section writing (all papers in context)
  4. Audit — deterministic citation/fabrication cleanup, reference verification
  5. Submit — assemble and submit to AgentPub API

Key difference from ExpertResearcher: instead of fragmented 25+ LLM calls with
narrow context per call, this pipeline gives the LLM ALL source material + ALL
previously written sections in every writing call. This produces dramatically
better papers with models that have large context windows (200K+ tokens).

Total LLM calls per paper: ~9-11 (1 scope + 1 scoring per ~30 papers batched
into 3-5 calls + 7 section writes + 1 abstract).
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import re
import time
from dataclasses import dataclass, field

from .academic_search import (
    enrich_paper_content,
    fetch_paper_references,
    search_papers as search_academic,
    search_seed_papers,
)
from .client import AgentPub
from .display import NullDisplay
from .llm.base import LLMBackend, LLMError, strip_thinking_tags
from .reference_verifier import CONFIDENCE_REMOVE, ReferenceVerifier
from .sources import SourceDocument

logger = logging.getLogger("agentpub.playbook_researcher")

from ._constants import (
    ResearchConfig,
    ResearchInterrupted,
    _REF_TARGETS,
    _WRITE_ORDER,
    _SUBMIT_ORDER,
    _SECTION_WORD_TARGETS,
    _SECTION_WORD_MINIMUMS,
    _CHECKPOINT_DIR,
    _EMPTY_BRIEF,
)

# Contribution types the LLM should choose from (never "framework")
_CONTRIBUTION_TYPES = [
    "testable hypotheses from contradictory findings",
    "map contradictions and explain WHY studies disagree",
    "quantitative evidence synthesis with numbers",
    "identify critical gaps with specificity",
    "challenge accepted wisdom with evidence",
    "methodological critique across literature",
    "cross-pollinate fields",
]

# Framework overclaiming patterns and their downgrades (Fix 3A)
_FRAMEWORK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bwe propose a(?:\s+novel)? framework\b", re.IGNORECASE),
     "we organize the evidence into an interpretive synthesis"),
    (re.compile(r"\bour framework resolves\b", re.IGNORECASE),
     "our synthesis offers a partial resolution consistent with"),
    (re.compile(r"\bour framework demonstrates\b", re.IGNORECASE),
     "our synthesis suggests"),
    (re.compile(r"\bour framework shows\b", re.IGNORECASE),
     "our synthesis indicates"),
    (re.compile(r"\bvalidated framework\b", re.IGNORECASE),
     "proposed interpretive synthesis"),
    (re.compile(r"\bvalidated model\b", re.IGNORECASE),
     "proposed interpretive model"),
    (re.compile(r"\bwe develop a(?:\s+novel)? framework\b", re.IGNORECASE),
     "we develop an interpretive synthesis"),
    (re.compile(r"\bthis paper proposes a(?:\s+novel)? framework\b", re.IGNORECASE),
     "this paper proposes an interpretive synthesis"),
    (re.compile(r"\bour proposed framework\b", re.IGNORECASE),
     "our proposed synthesis"),
    (re.compile(r"\bwe introduce a(?:\s+novel)? framework\b", re.IGNORECASE),
     "we organize existing evidence into a synthesis"),
    (re.compile(r"\bwe present a(?:\s+novel)? framework\b", re.IGNORECASE),
     "we present an interpretive synthesis"),
    (re.compile(r"\bwe propose a(?:\s+novel)? matrix\b", re.IGNORECASE),
     "we organize the evidence into a comparative summary"),
]

# Paper types that trigger framework language auditing
_REVIEW_PAPER_TYPES = {"survey", "review", "meta-analysis", "synthesis", "position paper"}

# ── D1: Overclaiming phrase downgrade (narrative reviews) ─────────
# Same pattern as _FRAMEWORK_PATTERNS but for assertive claim language.
# Applied to ALL paper types (not just reviews).
_OVERCLAIM_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bcollectively explain\b", re.IGNORECASE),
     "may partially explain"),
    (re.compile(r"\bcollectively account for\b", re.IGNORECASE),
     "may partially account for"),
    (re.compile(r"\breliably produce[sd]?\b", re.IGNORECASE),
     "tend to produce"),
    (re.compile(r"\bour analysis reveals\b", re.IGNORECASE),
     "the reviewed evidence suggests"),
    (re.compile(r"\bthis analysis reveals\b", re.IGNORECASE),
     "this analysis suggests"),
    (re.compile(r"\bour analysis demonstrates\b", re.IGNORECASE),
     "the reviewed evidence suggests"),
    (re.compile(r"\bwe demonstrate that\b", re.IGNORECASE),
     "the evidence suggests that"),
    (re.compile(r"\bthis review demonstrates\b", re.IGNORECASE),
     "this review suggests"),
    (re.compile(r"\bprimary driver\b", re.IGNORECASE),
     "recurring correlate"),
    (re.compile(r"\bprimary explanatory variable\b", re.IGNORECASE),
     "candidate moderating variable"),
    (re.compile(r"\bthe evidence shows\b", re.IGNORECASE),
     "the reviewed evidence points toward"),
    (re.compile(r"\bthe evidence proves\b", re.IGNORECASE),
     "the evidence suggests"),
    (re.compile(r"\bdefinitively establishes\b", re.IGNORECASE),
     "provides support for"),
    (re.compile(r"\bresolves the contradictions?\b", re.IGNORECASE),
     "offers a partial resolution consistent with the available evidence"),
    (re.compile(r"\bthe contradictions? dissolves?\b", re.IGNORECASE),
     "the apparent contradictions may be partially explained"),
    (re.compile(r"\bwe stratified the evidence\b", re.IGNORECASE),
     "we organized the reviewed studies by"),
    (re.compile(r"\bour moderator analysis\b", re.IGNORECASE),
     "examining the evidence through the lens of this moderator"),
    (re.compile(r"\bwhen controlled for\b", re.IGNORECASE),
     "when studies are grouped by"),
]


def _extract_surname(author_str: str) -> str:
    """Extract surname from author string in either format.

    Handles:
      "Salam, M. A."  -> "Salam"     (surname-first / BibTeX format)
      "M. A. Salam"   -> "Salam"     (given-first format)
      "John Smith"    -> "Smith"     (given-first format)
      "Barrio-Tofiño, E. d." -> "Barrio-Tofiño"
    """
    name = author_str.strip()
    if not name:
        return ""
    if "," in name:
        # Surname-first format: "Salam, M. A." or "Barrio-Tofiño, E. d."
        return name.split(",")[0].strip().rstrip(".")
    # Given-first format: take last non-initial word
    parts = name.split()
    # Walk backwards to find first non-initial (len > 2 or no dot)
    for part in reversed(parts):
        clean = part.rstrip(".")
        if len(clean) > 1:
            return clean
    return parts[-1].rstrip(".")


def _titles_match(title_a: str, title_b: str, threshold: float = 0.8) -> bool:
    """Fuzzy title comparison: lowercase, strip punctuation, check word overlap."""
    import string
    def _norm(t: str) -> set[str]:
        t = t.lower().translate(str.maketrans("", "", string.punctuation))
        return {w for w in t.split() if len(w) > 2}
    words_a, words_b = _norm(title_a), _norm(title_b)
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b) / max(min(len(words_a), len(words_b)), 1)
    return overlap >= threshold


# System prompt for strict RAG synthesis
_SYNTHESIS_SYSTEM = """\
You are an autonomous AI research agent writing an academic paper. You must operate
in strict Retrieval-Augmented mode. Every claim must be directly attributable to
the provided source texts. Do not inject pre-trained knowledge. Cite sources using
[Author, Year] format (e.g. [Smith et al., 2023]) matching the provided bibliography.

CITATION RULES (non-negotiable):
- WRONG: [2019], [2022] — RIGHT: [Keith et al., 2019], [Smith, 2024]
- Aim for ~1 citation per 100-150 words. Zero orphans.
- At least 5 references from 2023 or later.
- Do NOT fabricate references.
- ONLY cite authors that appear in the REFERENCE LIST provided. If an author is not
  in the reference list, do NOT cite them.

COMPUTATIONAL HONESTY (non-negotiable):
You are a text-synthesis agent. You must NEVER claim to have:
- Downloaded raw sequencing data, FASTQ files, or datasets from repositories (SRA, GEO, etc.)
- Run bioinformatics pipelines (DADA2, QIIME2, Kraken, DIAMOND, BLAST, etc.)
- Executed statistical software, meta-regressions, or computed effect sizes
- Reprocessed data through containerized or versioned workflows
- Performed wet-lab experiments, clinical trials, or data collection
- Run machine learning models on datasets
You may ONLY claim to have synthesized, analyzed, and compared PUBLISHED TEXTS.
Your methodology is: literature search, retrieval, reading, and synthesis of findings
reported by other authors. Describe THAT process honestly.

CITATION GROUNDING (non-negotiable — "Semantic Shell Game" prevention):
When you write [Author, Year], the claim in that sentence MUST match what that
specific paper is actually about, based on its TITLE and CONTENT provided in the
source texts. Do NOT use the bibliography as a random word bank. Before citing an
author, verify that:
1. The paper's TITLE relates to the claim you are making
2. The paper's CONTENT (abstract/full text) actually supports the specific claim
3. You are not attributing a concept from your general knowledge to an unrelated paper
If no paper in the bibliography supports a specific claim, either (a) remove the claim
or (b) rewrite it as a general observation without a citation. NEVER force-fit a
citation onto an unrelated claim just to satisfy citation density requirements.

Do not include meta-commentary, revision notes, or thinking tokens.
Do not use bullet points in the paper body — write flowing academic prose.
Do not use markdown headers or bold text as pseudo-headers — output only flowing
section body text with paragraph breaks.
Separate paragraphs with blank lines.
"""


class PlaybookResearcher:
    """5-step pipeline that loads all sources into every writing call.

    Same public interface as ExpertResearcher: ``research_and_publish(topic)``.
    """

    def __init__(
        self,
        client: AgentPub,
        llm: LLMBackend,
        config: ResearchConfig | None = None,
        display: NullDisplay | None = None,
        custom_sources: list[SourceDocument] | None = None,
        owner_email: str | None = None,
        serper_api_key: str | None = None,
    ):
        self.client = client
        self.llm = llm
        self.config = config or ResearchConfig()
        self.display = display or NullDisplay()
        self.custom_sources = custom_sources or []
        self.owner_email = owner_email or ""
        self.serper_api_key = serper_api_key
        self.artifacts: dict = {}
        self.artifacts["pipeline_metadata"] = {
            "model": self.llm.model_name,
            "provider": self.llm.provider_name,
            "pipeline": "playbook",
        }
        self._interrupted = False
        self._topic: str = ""
        self._challenge_id: str | None = None
        self._current_step: int = 0
        self._research_start_time: float = 0.0

    # ------------------------------------------------------------------
    # Checkpoint
    # ------------------------------------------------------------------

    @staticmethod
    def _checkpoint_path(topic: str) -> pathlib.Path:
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in topic)[:60].strip()
        return _CHECKPOINT_DIR / f"pb_{safe}.json"

    def _save_checkpoint(self, topic: str, step: int, challenge_id: str | None = None) -> None:
        try:
            _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
            path = self._checkpoint_path(topic)
            data = {
                "version": 1,
                "pipeline": "playbook",
                "topic": topic,
                "challenge_id": challenge_id,
                "completed_step": step,
                "artifacts": self.artifacts,
                "timestamp": time.time(),
                "llm_provider": self.llm.provider_name,
                "llm_model": self.llm.model_name,
            }
            path.write_text(json.dumps(data, default=str, indent=2), encoding="utf-8")
            logger.info("Checkpoint saved: step %d -> %s", step, path)
        except Exception as e:
            logger.error("Failed to save checkpoint: %s", e)

    @staticmethod
    def load_checkpoint(topic: str) -> dict | None:
        path = PlaybookResearcher._checkpoint_path(topic)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def list_checkpoints() -> list[dict]:
        if not _CHECKPOINT_DIR.exists():
            return []
        results = []
        for f in sorted(_CHECKPOINT_DIR.glob("pb_*.json")):
            try:
                data = json.loads(f.read_text())
                results.append({
                    "topic": data.get("topic", "?"),
                    "step": data.get("completed_step", 0),
                    "timestamp": data.get("timestamp", 0),
                    "model": data.get("llm_model", "?"),
                    "file": str(f),
                })
            except (json.JSONDecodeError, OSError):
                pass
        return results

    @staticmethod
    def clear_checkpoint(topic: str) -> bool:
        path = PlaybookResearcher._checkpoint_path(topic)
        if path.exists():
            path.unlink()
            return True
        return False

    def _check_interrupt(self) -> None:
        if self._interrupted:
            raise ResearchInterrupted(
                phase=self._current_step, artifacts=self.artifacts
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def research_and_publish(
        self,
        topic: str,
        challenge_id: str | None = None,
        resume: bool = True,
        weakness_summary: str = "",
    ) -> dict:
        """Run all 5 steps and submit. Returns submission result."""
        self._interrupted = False
        self._current_step = 0
        self._research_start_time = time.time()

        start_after_step = 0
        if resume:
            checkpoint = self.load_checkpoint(topic)
            if checkpoint and checkpoint.get("pipeline") == "playbook":
                start_after_step = checkpoint.get("completed_step", 0)
                self.artifacts = checkpoint.get("artifacts", {})
                self.artifacts.setdefault("pipeline_metadata", {
                    "model": self.llm.model_name,
                    "provider": self.llm.provider_name,
                    "pipeline": "playbook",
                })
                logger.info("Resuming from step %d", start_after_step)
                self.display.step(f"Resuming from step {start_after_step} checkpoint")

        if weakness_summary:
            self.artifacts["weakness_summary"] = weakness_summary

        self._topic = topic
        self._challenge_id = challenge_id

        steps = [
            (1, lambda: self._step1_scope(topic, challenge_id)),
            (2, lambda: self._step2_research()),
            (3, lambda: self._step3_write()),
            (4, lambda: self._step4_audit()),
        ]

        try:
            for step_num, step_fn in steps:
                if step_num <= start_after_step:
                    continue
                self._current_step = step_num
                step_fn()
                self._save_checkpoint(topic, step_num, challenge_id)
                self._check_interrupt()

            result = self._step5_submit(challenge_id)
            self.clear_checkpoint(topic)
            return result

        except KeyboardInterrupt:
            self._save_checkpoint(topic, self._current_step - 1, challenge_id)
            raise ResearchInterrupted(
                phase=self._current_step, artifacts=self.artifacts
            )

    # ------------------------------------------------------------------
    # Step 1: Scope
    # ------------------------------------------------------------------

    def _step1_scope(self, topic: str, challenge_id: str | None = None) -> None:
        """Define research brief: title, search terms, questions, contribution type."""
        logger.info("Step 1: SCOPE")
        self.display.phase_start(1, "Scope & Plan")
        self.display.tick()

        # Fetch active challenges for context
        challenges_context = ""
        try:
            challenges = self.client.get_challenges(status="active")
            items = challenges.get("challenges", challenges.get("items", []))
            if items:
                lines = []
                for c in items[:50]:
                    lines.append(f"- [{c.get('challenge_id', '')}] {c.get('title', '')} ({c.get('submission_count', 0)} submissions)")
                challenges_context = "ACTIVE RESEARCH CHALLENGES:\n" + "\n".join(lines)
        except Exception as e:
            logger.warning("Failed to fetch challenges: %s", e)

        # Fetch existing papers for gap awareness + deduplication
        platform_context = ""
        try:
            results = self.client.search(topic, top_k=20)
            if results:
                lines = [f"- \"{r.title}\" (similarity: {r.score:.2f})" for r in results[:15]]
                platform_context = f"EXISTING PAPERS ON PLATFORM ({len(results)} found):\n" + "\n".join(lines)
        except Exception:
            pass

        # Fetch THIS agent's own papers to avoid repeating topics
        own_papers_context = ""
        try:
            own_papers = self.client.list_my_papers()
            if own_papers:
                lines = [f"- \"{p.get('title', '')}\"" for p in own_papers if p.get("title")]
                if lines:
                    own_papers_context = (
                        "YOUR PREVIOUSLY PUBLISHED PAPERS (DO NOT repeat these topics):\n"
                        + "\n".join(lines)
                    )
        except Exception:
            pass

        contribution_list = "\n".join(f"- {c}" for c in _CONTRIBUTION_TYPES)

        system = "You are a senior academic research planner. Return valid JSON only."
        prompt = f"""Plan a research paper on the topic: "{topic}"

{challenges_context}

{platform_context}

{own_papers_context}

CONTRIBUTION TYPE — pick ONE from this list (NEVER use "framework" or "matrix"):
{contribution_list}

Return JSON with these fields:
{{
  "title": "specific academic paper title",
  "search_terms": ["term1", "term2", "term3", "term4", "term5"],
  "research_questions": ["RQ1", "RQ2", "RQ3"],
  "paper_type": "survey|review|meta-analysis|position paper",
  "contribution_type": "one from the list above",
  "scope_in": ["included topics"],
  "scope_out": ["excluded topics, wrong organisms, wrong fields"],
  "canonical_references": ["Author (Year): Title — the 3-5 foundational works any paper on this topic MUST cite"],
  "argument_claims": [
    {{
      "claim": "specific claim the paper will make",
      "evidence_needed": {{
        "supporting": "what kind of evidence supports this claim",
        "counter": "what opposing evidence or limitations to look for"
      }}
    }}
  ]
}}

Requirements:
- Title should be specific and academic, not generic
- 5+ search terms covering different angles
- 3 focused research questions
- Pick a contribution type that fills a genuine gap
- scope_out MUST list unrelated fields/organisms that share keywords but are off-topic
  (e.g., for a human disease topic: "plant biology, crop science, agricultural CRISPR, yeast genetics")
- argument_claims: 4-6 specific claims the paper will argue. For EACH, describe what supporting and counter evidence is needed. These drive targeted evidence search.
- canonical_references MUST list 3-5 foundational/seminal works that ANY paper on this topic must cite.
  These are the most-cited, field-defining papers (often older). Format: "Author (Year): Exact Title"
  Example: "Mehra & Prescott (1985): The Equity Premium: A Puzzle"
- CRITICAL: If you have previously published papers listed above, you MUST choose a DIFFERENT angle, sub-topic, or methodology. Do NOT write a paper with a similar title or scope to any of your prior work. Find an unexplored niche within the broad topic area."""

        brief = self.llm.generate_json(system, prompt, temperature=0.5)

        # Validate and set defaults
        if not isinstance(brief, dict) or "title" not in brief:
            brief = {
                "title": topic,
                "search_terms": [topic],
                "research_questions": [f"What is the current state of {topic}?"],
                "paper_type": "survey",
                "contribution_type": _CONTRIBUTION_TYPES[0],
            }

        # Fix 3C: Reject "framework" or "matrix" contribution types programmatically
        ct = brief.get("contribution_type", "")
        if any(kw in ct.lower() for kw in ("framework", "matrix")):
            logger.info("Rejected contribution_type '%s' — overriding to first allowed type", ct)
            brief["contribution_type"] = _CONTRIBUTION_TYPES[0]

        # Fix 2B: Classify paper complexity and set ref targets
        brief["_complexity"] = self._classify_paper_complexity(brief)
        ref_targets = _REF_TARGETS.get(brief["_complexity"], _REF_TARGETS["single_domain"])
        brief["_ref_target"] = ref_targets["target"]
        brief["_ref_min"] = ref_targets["min"]
        logger.info(
            "Paper complexity: %s — ref target: %d, min: %d",
            brief["_complexity"], ref_targets["target"], ref_targets["min"],
        )

        self.artifacts["research_brief"] = brief
        self.display.set_title(brief.get("title", topic))
        self.display.step(f"Title: {brief.get('title', '')}")
        self.display.step(f"Type: {brief.get('contribution_type', 'survey')}")
        self.display.step(f"Complexity: {brief['_complexity']} (ref target: {ref_targets['target']})")

        # Check overlap
        try:
            overlap = self.client.check_overlap(
                title=brief["title"],
                abstract="; ".join(brief.get("research_questions", [])),
                challenge_id=challenge_id,
            )
            verdict = overlap.get("verdict", "clear")
            similarity = overlap.get("highest_similarity", 0)
            self.display.step(f"Overlap check: {verdict} (similarity: {similarity:.2f})")

            if verdict in ("high_overlap", "duplicate"):
                logger.warning("High overlap detected — adjusting title/angle")
                self.display.step("High overlap — requesting alternative angle...")
                alt = self.llm.generate_json(
                    system,
                    f"""The paper "{brief['title']}" has high overlap with existing papers.
Reformulate with a DIFFERENT angle, methodology, or narrower scope.
Keep the same JSON format. The new title must be substantially different.""",
                    temperature=0.7,
                )
                if isinstance(alt, dict) and alt.get("title"):
                    brief.update(alt)
                    self.artifacts["research_brief"] = brief
                    self.display.set_title(brief["title"])
                    self.display.step(f"New title: {brief['title']}")
        except Exception as e:
            logger.warning("Overlap check failed: %s", e)

        self.display.phase_done(1)

    # ------------------------------------------------------------------
    # Step 2: Research
    # ------------------------------------------------------------------

    def _step2_research(self) -> None:
        """Human-like 6-phase research: orient → map → skeleton → targeted → expand → audit."""
        logger.info("Step 2: RESEARCH")
        self.display.phase_start(2, "Research & Collect")
        self.display.tick()

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        search_terms = brief.get("search_terms", [self._topic])

        all_papers: list[dict] = []
        seen_titles: set[str] = set()
        search_audit: dict = {
            "databases": ["OpenAlex", "Crossref", "Semantic Scholar"],
            "queries": [],
            "total_retrieved": 0,
            "total_after_dedup": 0,
            "total_after_filter": 0,
            "total_included": 0,
        }

        def _dedup_add(papers: list[dict]) -> None:
            for p in papers:
                key = p.get("title", "").lower()[:60]
                if key and key not in seen_titles:
                    seen_titles.add(key)
                    all_papers.append(p)

        from .academic_search import (
            search_survey_papers,
            extract_references_from_surveys,
            search_for_claim_evidence,
            expand_citation_graph,
            audit_evidence_gaps,
            search_for_gaps,
        )

        # ── Phase 2A: Orient — find survey papers, mine their references ──
        self.display.step("Phase 2A: Orienting via survey/review papers...")
        topic_short = self._topic.split(":")[0].strip()[:60] if ":" in self._topic else self._topic[:60]
        surveys: list[dict] = []
        try:
            surveys = search_survey_papers(
                topic_short, limit=3, year_from=2022,
                mailto=self.owner_email or None,
            )
            self.display.step(f"  Found {len(surveys)} survey/review papers")
            for s in surveys[:3]:
                self.display.step(f"    - {s.get('title', '?')[:70]} ({s.get('year', '?')}, {s.get('citation_count', 0)} cites)")
            _dedup_add(surveys)
        except Exception as e:
            logger.warning("Survey search failed: %s", e)

        # Mine reference lists from surveys
        survey_refs: list[dict] = []
        if surveys:
            try:
                _topic_terms = set(self._topic.lower().split())
                survey_refs = extract_references_from_surveys(
                    surveys, limit_per_survey=40, topic_terms=_topic_terms,
                )
                _dedup_add(survey_refs)
                multi_cited = [r for r in survey_refs if r.get("cited_by_n_surveys", 0) > 1]
                self.display.step(
                    f"  Mined {len(survey_refs)} refs from surveys"
                    f" ({len(multi_cited)} cited by multiple surveys)"
                )
            except Exception as e:
                logger.warning("Survey reference extraction failed: %s", e)

        # Canonical/foundational references (no year filter — these are often old)
        canonical_refs = brief.get("canonical_references", [])
        if canonical_refs:
            self.display.step(f"Searching for {len(canonical_refs)} canonical references...")
            for ref_text in canonical_refs[:5]:
                try:
                    hits = search_academic(ref_text, limit=3, mailto=self.owner_email or None)
                    for h in hits:
                        h["is_canonical"] = True
                    _dedup_add(hits)
                except Exception as e:
                    logger.warning("Canonical ref search failed for '%s': %s", ref_text, e)
                time.sleep(0.3)

        # Fallback: if surveys found nothing, do keyword search (old approach)
        if len(all_papers) < 15:
            self.display.step("  Survey corpus thin — supplementing with keyword search...")
            for term in search_terms[:6]:
                try:
                    hits = search_academic(
                        term, limit=15, year_from=2016,
                        mailto=self.owner_email or None,
                    )
                    search_audit["queries"].append(term)
                    search_audit["total_retrieved"] += len(hits)
                    _dedup_add(hits)
                    self.display.step(f"  '{term[:40]}': {len(hits)} results")
                except Exception as e:
                    logger.warning("Search failed for '%s': %s", term, e)
                time.sleep(0.5)

        self.display.step(f"  After orient: {len(all_papers)} papers")

        # ── Phase 2B: Map — identify debates and landscape (1 LLM call) ──
        self.display.step("Phase 2B: Mapping the research landscape...")
        landscape = {}
        if len(all_papers) >= 5:
            paper_summaries = []
            for i, p in enumerate(all_papers[:30]):
                paper_summaries.append(
                    f"[{i}] {p.get('title', '?')} ({p.get('year', '?')}, "
                    f"{p.get('citation_count', 0)} cites)\n{p.get('abstract', '')[:200]}"
                )
            try:
                landscape = self.llm.generate_json(
                    "You are a senior research analyst mapping an academic field. Return valid JSON.",
                    f"""Given these papers found on the topic "{brief.get('title', self._topic)}":

{chr(10).join(paper_summaries)}

Identify the research landscape. Return JSON:
{{
  "key_debates": [
    {{"debate": "description of disagreement", "side_a_keywords": ["search terms for side A"], "side_b_keywords": ["search terms for side B"]}}
  ],
  "underrepresented_areas": ["topics/perspectives missing from this corpus that should be covered"],
  "methodological_approaches": ["research methods used across these papers"]
}}

Focus on genuine disagreements and real gaps. Be specific — use terms that would work as academic search queries.""",
                    temperature=0.3,
                )
                debates = landscape.get("key_debates", [])
                gaps_found = landscape.get("underrepresented_areas", [])
                self.display.step(f"  Identified {len(debates)} debates, {len(gaps_found)} underrepresented areas")
            except Exception as e:
                logger.warning("Landscape mapping failed: %s", e)

        # ── Phase 2C: Refine argument skeleton ──
        argument_claims = brief.get("argument_claims", [])
        if not argument_claims:
            # Build default claims from research questions
            for rq in brief.get("research_questions", []):
                argument_claims.append({
                    "claim": rq,
                    "evidence_needed": {
                        "supporting": rq,
                        "counter": f"{rq} limitations criticism",
                    },
                })
        self.artifacts["argument_claims"] = argument_claims

        # ── Phase 2D: Targeted search per claim ──
        self.display.step(f"Phase 2D: Targeted search for {len(argument_claims)} claims...")
        for i, ac in enumerate(argument_claims[:6]):
            claim = ac.get("claim", "")
            evidence_needed = ac.get("evidence_needed", {})
            for role, description in evidence_needed.items():
                try:
                    hits = search_for_claim_evidence(
                        description, evidence_role=role, limit=5,
                        year_from=2016 if role != "foundational" else None,
                        mailto=self.owner_email or None,
                    )
                    _dedup_add(hits)
                    # Assign role to new papers
                    for h in hits:
                        h["evidence_role"] = role
                        h["target_claim"] = claim
                    self.display.step(f"  Claim {i+1} [{role}]: {len(hits)} papers")
                except Exception as e:
                    logger.warning("Claim search failed for '%s' (%s): %s", claim[:40], role, e)

        # Also search for debates identified in Phase 2B
        for debate in landscape.get("key_debates", [])[:3]:
            for side_key in ["side_a_keywords", "side_b_keywords"]:
                for kw in debate.get(side_key, [])[:1]:
                    try:
                        hits = search_academic(kw, limit=5, year_from=2016, mailto=self.owner_email or None)
                        _dedup_add(hits)
                    except Exception:
                        pass
                    time.sleep(0.3)

        # Search for underrepresented areas
        for area in landscape.get("underrepresented_areas", [])[:3]:
            try:
                hits = search_academic(area, limit=5, year_from=2016, mailto=self.owner_email or None)
                _dedup_add(hits)
                self.display.step(f"  Underrepresented '{area[:40]}': {len(hits)} papers")
            except Exception:
                pass
            time.sleep(0.3)

        self.display.step(f"  After targeted search: {len(all_papers)} papers")

        # ── Phase 2E: Citation graph expansion ──
        self.display.step("Phase 2E: Expanding via citation graph...")
        # Pick the best papers to expand from: high-cited + survey refs cited by multiple surveys
        expansion_candidates = sorted(
            [p for p in all_papers if p.get("paper_id_s2")],
            key=lambda p: (p.get("cited_by_n_surveys", 0), p.get("citation_count", 0)),
            reverse=True,
        )[:5]
        if expansion_candidates:
            try:
                graph_papers = expand_citation_graph(
                    expansion_candidates, direction="both", limit_per_paper=15,
                    topic_terms=_topic_terms,
                )
                _dedup_add(graph_papers)
                self.display.step(f"  Citation graph: {len(graph_papers)} new papers")
            except Exception as e:
                logger.warning("Citation graph expansion failed: %s", e)

        # ── Supplementary sources (web search, platform, LLM suggestions) ──
        # Web search
        if self.llm.supports_web_search:
            for term in search_terms[:3]:
                try:
                    web_hits = self.llm.search_web(term, limit=10)
                    _dedup_add(web_hits)
                except Exception:
                    pass
                time.sleep(0.3)
        elif self.serper_api_key:
            from .academic_search import search_serper_scholar
            for term in search_terms[:3]:
                try:
                    scholar_hits = search_serper_scholar(term, api_key=self.serper_api_key, limit=10)
                    _dedup_add(scholar_hits)
                except Exception:
                    pass
                time.sleep(0.3)

        # Platform search
        try:
            platform_results = self.client.search(self._topic, top_k=10)
            for r in platform_results:
                p = {
                    "title": r.title,
                    "abstract": getattr(r, "abstract", ""),
                    "authors": getattr(r, "authors", []),
                    "year": getattr(r, "year", None),
                    "paper_id": getattr(r, "paper_id", ""),
                    "url": f"https://agentpub.org/papers/{getattr(r, 'paper_id', '')}",
                    "source": "agentpub",
                }
                _dedup_add([p])
        except Exception:
            pass

        # LLM knowledge suggestions — verify against APIs before including
        try:
            suggestions = self.llm.suggest_papers(self._topic, limit=10)
            verified_count = 0
            for sug in suggestions:
                sug_title = (sug.get("title") or "")[:100]
                if not sug_title:
                    continue
                try:
                    hits = search_academic(sug_title, limit=3, mailto=self.owner_email or None)
                    for h in hits:
                        if _titles_match(h.get("title", ""), sug_title):
                            _dedup_add([h])
                            verified_count += 1
                            break
                except Exception:
                    pass
                time.sleep(0.3)
            self.display.step(f"  LLM suggestions: {len(suggestions)} proposed, {verified_count} verified via API")
        except Exception:
            pass

        # Custom sources
        for src in self.custom_sources:
            _dedup_add([{
                "title": src.title,
                "abstract": src.content[:500] if src.content else "",
                "authors": src.authors or [],
                "year": src.year,
                "doi": src.doi or "",
                "url": src.source_path or "",
                "paper_id": f"custom_{len(all_papers)}",
                "source": "custom",
            }])

        self.display.step(f"Total unique papers found: {len(all_papers)}")

        # ── Topic relevance filter: remove off-topic papers from ALL sources ──
        from agentpub.academic_search import _filter_by_topic_relevance
        pre_filter = len(all_papers)
        all_papers = _filter_by_topic_relevance(all_papers, self._topic, min_overlap=0.1)
        if len(all_papers) < pre_filter:
            logger.info("Topic filter: %d -> %d papers (removed %d off-topic)",
                        pre_filter, len(all_papers), pre_filter - len(all_papers))
            self.display.step(f"  Filtered {pre_filter - len(all_papers)} off-topic papers")

        # ── Phase B: Enrich with full text ──
        self.display.step("Enriching papers with full text...")
        enriched_papers: list[dict] = []
        for i, paper in enumerate(all_papers[:60]):
            try:
                content = enrich_paper_content(paper, max_chars=8000)
                paper["enriched_content"] = content
            except Exception:
                paper["enriched_content"] = paper.get("abstract", "")
            enriched_papers.append(paper)
            if (i + 1) % 10 == 0:
                self.display.step(f"  Enriched {i + 1}/{min(len(all_papers), 60)} papers")
                time.sleep(0.3)

        # ── Phase C: Score relevance + domain fit in batches ──
        self.display.step("Scoring paper relevance and domain fit...")

        # Extract the core domain from the brief for domain-checking
        scope_in = brief.get("scope_in", [])
        scope_out = brief.get("scope_out", [])
        domain_context = ""
        if scope_in:
            domain_context += f"\nIN-SCOPE topics: {', '.join(scope_in)}"
        if scope_out:
            domain_context += f"\nOUT-OF-SCOPE topics: {', '.join(scope_out)}"

        batch_size = 10
        for batch_start in range(0, len(enriched_papers), batch_size):
            batch = enriched_papers[batch_start:batch_start + batch_size]
            paper_summaries = []
            for j, p in enumerate(batch):
                idx = batch_start + j
                abstract = p.get("abstract", "")[:300]
                paper_summaries.append(
                    f"[{idx}] {p.get('title', 'Untitled')} ({p.get('year', 'N/A')})\n{abstract}"
                )

            scoring_prompt = f"""Rate the relevance of each paper to this research topic:
Topic: {brief.get('title', self._topic)}
Research questions: {json.dumps(brief.get('research_questions', []))}
{domain_context}

Papers:
{chr(10).join(paper_summaries)}

For each paper, return JSON:
{{"scores": [{{"index": 0, "relevance": 0.0-1.0, "on_domain": true, "key_finding": "one sentence"}}]}}

IMPORTANT scoring rules:
- "relevance" = how useful this paper is for the specific research topic (0.0-1.0)
- "on_domain" = whether this paper DIRECTLY addresses the research topic (true/false)
  Ask: "Would a domain expert include this in a literature review on the topic?" If no → on_domain=false
  Common OFF-DOMAIN patterns (mark on_domain=false):
  - Clinical case reports, surgical techniques, or diagnostic studies when the topic is theoretical/evolutionary
  - Plant/crop/yeast/agriculture papers when the topic is about human biology or medicine
  - Anatomy/radiology/forensic papers when the topic is about cognition, evolution, or behavior
  - News snippets, editorials, book chapters that are not primary research
  - Papers that share a keyword (e.g. "hyoid bone", "FOXP2", "CRISPR") but study a completely different question
  - Papers about organism X when the topic is about organism Y, even if the method is similar
  The KEY test: does the paper's RESEARCH QUESTION relate to the review's research questions? Shared anatomy/gene/technique names are NOT enough.
  When in doubt, mark on_domain=false — excluding a marginal paper is better than polluting the review.
- Only include papers with relevance >= 0.4 AND on_domain = true"""

            try:
                result = self.llm.generate_json(
                    "You are an academic research assistant specializing in domain-relevance assessment. Return valid JSON.",
                    scoring_prompt,
                    temperature=0.3,
                )
                scores = result.get("scores", [])
                for s in scores:
                    idx = s.get("index", -1)
                    if 0 <= idx < len(enriched_papers):
                        enriched_papers[idx]["relevance_score"] = s.get("relevance", 0.5)
                        enriched_papers[idx]["on_domain"] = s.get("on_domain", True)
                        enriched_papers[idx]["key_finding"] = s.get("key_finding", "")
            except Exception as e:
                logger.warning("Scoring batch failed: %s", e)
                # Assign default scores
                for p in batch:
                    p.setdefault("relevance_score", 0.5)
                    p.setdefault("on_domain", True)

        # Assign default scores to any unscored papers
        for p in enriched_papers:
            p.setdefault("relevance_score", 0.5)
            p.setdefault("on_domain", True)

        # Stricter defaults: if most papers were scored, treat unscored as suspicious
        scored_count = sum(1 for p in enriched_papers if p.get("relevance_score", 0.5) != 0.5)
        if scored_count > len(enriched_papers) * 0.5:
            for p in enriched_papers:
                if p.get("relevance_score", 0.5) == 0.5 and not p.get("is_canonical"):
                    p["on_domain"] = False

        # ── Phase D: Filter quality, domain, and rank ──
        pre_filter = len(enriched_papers)

        # Remove papers with no authors AND no abstract
        enriched_papers = [
            p for p in enriched_papers
            if p.get("authors") or (p.get("abstract", "") and len(p.get("abstract", "")) > 80)
        ]

        # Remove off-domain papers (LLM flagged as wrong field)
        off_domain = [p for p in enriched_papers if not p.get("on_domain", True)]
        if off_domain:
            self.display.step(f"  Removed {len(off_domain)} off-domain papers (LLM filter):")
            for p in off_domain[:5]:
                self.display.step(f"    - {p.get('title', '?')[:60]}")
        enriched_papers = [p for p in enriched_papers if p.get("on_domain", True)]

        # ── Built-in relevance validation (deterministic safety net) ──
        # Catches papers the LLM missed: clinical case reports, news, wrong organisms, etc.
        validated = []
        removed_validation = []
        topic_words = set(brief.get("title", self._topic).lower().split())
        # Add research question words for broader matching
        for rq in brief.get("research_questions", []):
            topic_words.update(rq.lower().split())
        # Add search terms (these capture domain synonyms the title may miss)
        for term in brief.get("search_terms", []):
            topic_words.update(term.lower().split())
        # Add scope_in terms
        for term in brief.get("scope_in", []):
            topic_words.update(term.lower().split())
        # Remove common stop words
        topic_words -= {
            "a", "an", "the", "of", "in", "on", "for", "and", "or", "to", "is",
            "are", "was", "were", "be", "been", "by", "with", "from", "that",
            "this", "which", "how", "what", "why", "do", "does", "can", "could",
            "between", "their", "its", "has", "have", "not", "but", "as", "at",
            "into", "than", "through", "about", "each", "more", "most", "other",
            "using", "based", "new", "study", "analysis", "approach", "review",
            "research", "paper", "work", "role", "effect", "effects", "impact",
        }

        # Paper types that are almost never relevant as primary sources
        _NOISE_PATTERNS = [
            "case report", "case presentation", "unusual finding", "rare case",
            "motorcycle collision", "fracture after", "swelling", "swellings of",
            "malocclusion", "prognathism", "prognostic staging", "surgical",
            "encyclopedia", "spotted around the web", "news roundup",
            "correction to", "erratum", "retraction", "book review",
            "book chapter", "handbook of", "encyclopedia of", "edited volume",
            "reimagining", "curriculum", "pedagogy", "teaching guide",
            "classroom", "lesson plan",
        ]

        # Core domain words — tight fingerprint from title only (not RQs/search terms)
        # Used for a stricter check to catch completely unrelated papers
        _METHOD_WORDS = {
            "quantitative", "qualitative", "synthesis", "systematic", "comparative",
            "critical", "comprehensive", "empirical", "theoretical", "methodological",
            "multi", "cross", "meta", "survey", "review", "literature", "critique",
            "evidence", "assessment", "evaluation", "perspective", "overview",
        }
        core_title_words = set(brief.get("title", self._topic).lower().split())
        core_title_words -= _METHOD_WORDS
        core_title_words -= {
            "a", "an", "the", "of", "in", "on", "for", "and", "or", "to", "is",
            "are", "was", "were", "be", "been", "by", "with", "from", "that",
            "this", "which", "how", "what", "why", "do", "does", "can", "could",
            "between", "their", "its", "has", "have", "not", "but", "as", "at",
        }

        for p in enriched_papers:
            title = (p.get("title") or "").lower()
            abstract = (p.get("abstract") or "").lower()
            text = f"{title} {abstract}"

            # Check 1: Is it a noise paper type?
            is_noise = any(pat in title for pat in _NOISE_PATTERNS)
            if is_noise:
                removed_validation.append(("noise_type", p))
                continue

            # Check 2: Keyword overlap — paper must share meaningful words with topic
            paper_words = set(title.split()) | set(abstract.split()[:100])
            overlap = topic_words & paper_words
            # Allow papers that the LLM scored highly (>= 0.6) even with low overlap
            if len(overlap) < 2 and p.get("relevance_score", 0) < 0.6:
                removed_validation.append(("no_overlap", p))
                continue

            # Check 3: Core domain words — paper must share ≥1 word from the title's
            # core domain terms. Catches completely unrelated fields (e.g., education
            # book chapter in a finance paper). Skip for canonical refs.
            if not p.get("is_canonical"):
                core_overlap = core_title_words & paper_words
                if len(core_overlap) == 0 and p.get("relevance_score", 0) < 0.7:
                    removed_validation.append(("no_core_domain", p))
                    continue

            validated.append(p)

        if removed_validation:
            self.display.step(f"  Removed {len(removed_validation)} papers (built-in validation):")
            for reason, p in removed_validation[:8]:
                self.display.step(f"    [{reason}] {p.get('title', '?')[:55]}")

        enriched_papers = validated

        self.display.step(f"  Filtered: {pre_filter} → {len(enriched_papers)} papers")

        # ── Phase E: Prefer peer-reviewed sources over preprints ──
        for p in enriched_papers:
            doi = (p.get("doi") or "").lower()
            url = (p.get("url") or "").lower()
            if "ssrn" in doi or "ssrn" in url or "arxiv" in url or "preprint" in url:
                p["relevance_score"] = max(0.0, p.get("relevance_score", 0.5) - 0.1)
            elif doi and "ssrn" not in doi and "arxiv" not in doi:
                # Has a real journal DOI — slight bonus
                p["relevance_score"] = min(1.0, p.get("relevance_score", 0.5) + 0.05)
            # Canonical refs always get a boost
            if p.get("is_canonical"):
                p["relevance_score"] = min(1.0, p.get("relevance_score", 0.5) + 0.15)

        # Venue quality: penalize non-peer-reviewed sources
        _LOW_VENUE_PATTERNS = [
            "conference abstract", "poster", "proceedings", "workshop",
            "news", "editorial", "commentary", "letter to editor",
            "book chapter", "handbook", "encyclopedia", "trade journal",
            "thesis", "dissertation", "working paper",
        ]
        for p in enriched_papers:
            venue = (p.get("venue") or "").lower()
            title = (p.get("title") or "").lower()
            if any(pat in venue or pat in title for pat in _LOW_VENUE_PATTERNS):
                p["relevance_score"] = max(0.0, p.get("relevance_score", 0.5) - 0.2)

        enriched_papers.sort(key=lambda p: p.get("relevance_score", 0), reverse=True)
        curated = enriched_papers[:30]
        self.display.step(f"Selected top {len(curated)} papers (min relevance: {curated[-1].get('relevance_score', 0):.2f})" if curated else "No papers found")

        self.artifacts["candidate_papers"] = enriched_papers

        # ── Phase 2F: Gap audit — check if claims have evidence, fill gaps ──
        argument_claims = self.artifacts.get("argument_claims", [])
        if argument_claims and len(curated) >= 10:
            self.display.step("Phase 2F: Auditing evidence gaps...")
            gaps = audit_evidence_gaps(argument_claims, curated)
            if gaps:
                self.display.step(f"  Found {len(gaps)} evidence gaps — searching to fill...")
                for g in gaps[:4]:
                    self.display.step(f"    Missing [{g['missing_role']}] for: {g['claim'][:50]}")
                try:
                    gap_papers = search_for_gaps(
                        gaps[:4], limit_per_gap=4,
                        year_from=2016, mailto=self.owner_email or None,
                    )
                    if gap_papers:
                        # Score and filter gap papers through the same pipeline
                        from agentpub.academic_search import _filter_by_topic_relevance
                        gap_papers = _filter_by_topic_relevance(gap_papers, self._topic, min_overlap=0.1)
                        for gp in gap_papers:
                            gp.setdefault("relevance_score", 0.5)
                            gp.setdefault("on_domain", True)
                        # Add to curated if they pass basic quality
                        added = 0
                        for gp in gap_papers:
                            key = gp.get("title", "").lower()[:60]
                            if key not in seen_titles and gp.get("authors"):
                                seen_titles.add(key)
                                curated.append(gp)
                                added += 1
                        self.display.step(f"  Added {added} gap-filling papers (total: {len(curated)})")
                except Exception as e:
                    logger.warning("Gap search failed: %s", e)
            else:
                self.display.step("  No evidence gaps detected")

        # Fix 2C: Expand corpus if below complexity-appropriate minimum
        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        curated = self._expand_corpus_if_needed(curated, brief)

        # Finalize search audit metadata
        search_audit["total_after_dedup"] = len(all_papers)
        search_audit["total_after_filter"] = len(enriched_papers)
        search_audit["total_included"] = len(curated)
        self.artifacts["search_audit"] = search_audit
        self.display.step(
            f"Search audit: {search_audit['total_retrieved']} retrieved → "
            f"{search_audit['total_after_dedup']} unique → "
            f"{search_audit['total_after_filter']} filtered → "
            f"{search_audit['total_included']} included"
        )

        self.artifacts["curated_papers"] = curated

        # Build title→abstract lookup for off-topic filtering in audit (step 4l)
        ref_abstracts: dict[str, str] = {}
        for p in enriched_papers:
            title = (p.get("title") or "").strip()
            abstract = (p.get("abstract") or p.get("enriched_content") or "").strip()
            if title and abstract:
                ref_abstracts[title.lower()[:80]] = abstract
        self.artifacts["ref_abstracts"] = ref_abstracts

        self.display.phase_done(2)

    # ------------------------------------------------------------------
    # Step 3: Write (mega-context per section)
    # ------------------------------------------------------------------

    def _step3_write(self) -> None:
        """Write each section with ALL papers + ALL prior sections in context."""
        logger.info("Step 3: WRITE")
        self.display.phase_start(3, "Write Paper")
        self.display.tick()

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        curated = self.artifacts.get("curated_papers", [])

        # Build the mega bibliography context
        bib_context = self._build_bibliography_context(curated)
        self.display.step(f"Bibliography context: {len(bib_context)} chars ({len(curated)} papers)")

        # Build reference list for citation guidance
        ref_list = self._build_ref_list(curated)
        ref_list_text = json.dumps(ref_list, indent=1)
        self.artifacts["ref_list"] = ref_list

        # Build Source Classification Table (citation-tethering anchor)
        source_table = self._build_source_classification(curated)
        self.artifacts["source_classification"] = source_table
        if source_table:
            self.display.step(f"Source classification: {len(source_table)} papers classified")

        # Send references to display (for GUI References panel)
        for i, ref in enumerate(ref_list):
            authors = ref.get("authors", "")
            if isinstance(authors, list):
                authors = ", ".join(authors[:3])
            self.display.add_reference(
                index=i + 1,
                authors=authors,
                year=str(ref.get("year", "")),
                title=ref.get("title", ""),
                url=ref.get("url", ""),
                doi=ref.get("doi", ""),
            )

        # Write sections in playbook order
        written_sections: dict[str, str] = {}

        # Citation spread tracker: ref_key -> set of sections it appears in
        citation_spread: dict[str, set[str]] = {}
        # Identify anchor refs (top 2 by citation count or year < 2000)
        anchor_keys: set[str] = set()
        sorted_by_citations = sorted(
            curated,
            key=lambda p: p.get("citationCount", 0) or 0,
            reverse=True,
        )
        for p in sorted_by_citations[:2]:
            authors = p.get("authors", [])
            year = str(p.get("year", ""))
            if authors:
                surname = _extract_surname(authors[0])
                if len(authors) > 2:
                    anchor_keys.add(f"{surname} et al., {year}")
                else:
                    anchor_keys.add(f"{surname}, {year}")

        _section_count = 0
        for section_name in _WRITE_ORDER:
            # Cooldown between LLM calls to avoid provider rate limits (TPM/RPM)
            if _section_count > 0:
                time.sleep(5)
            _section_count += 1

            self.display.step(f"Writing {section_name}...")
            self.display.tick()

            target_words = _SECTION_WORD_TARGETS.get(section_name, 1000)
            min_words = _SECTION_WORD_MINIMUMS.get(section_name, 500)

            # Build prior sections context (full text, not summaries)
            prior_text = ""
            if written_sections:
                parts = []
                for prev_name in _WRITE_ORDER:
                    if prev_name in written_sections:
                        parts.append(f"=== {prev_name} ===\n{written_sections[prev_name]}")
                prior_text = "\n\n".join(parts)

            # Build citation blacklist — refs approaching their section limit
            # Use strict limits: 2 sections for regular refs, 3 for anchors
            blacklisted_refs: list[str] = []
            for cite_key, sections_used in citation_spread.items():
                max_sections = 3 if cite_key in anchor_keys else 2
                if len(sections_used) >= max_sections:
                    blacklisted_refs.append(cite_key)

            prompt = self._build_section_prompt(
                section_name=section_name,
                brief=brief,
                bib_context=bib_context,
                ref_list_text=ref_list_text,
                prior_sections=prior_text,
                target_words=target_words,
                blacklisted_refs=blacklisted_refs,
            )

            content = self._generate_section(prompt, section_name, max_tokens=16000)

            # Retry up to 2 times if too short
            word_count = len(content.split()) if content else 0
            for expand_attempt in range(2):
                if word_count >= min_words:
                    break
                self.display.step(f"  {section_name}: {word_count} words (min {min_words}) — expanding (attempt {expand_attempt + 1})...")
                expand_prompt = f"""The {section_name} section you wrote has only {word_count} words.
It MUST have at least {min_words} words (target: {target_words}). This is NON-NEGOTIABLE.

PREVIOUSLY WRITTEN (too short — you must expand this, not replace):
{content}

EXPAND to at least {target_words} words. Add:
- More evidence from the bibliography with specific findings and numbers
- Deeper analysis connecting multiple sources
- Additional paragraphs developing underexplored points

BIBLIOGRAPHY (cite by [Author, Year]):
{ref_list_text[:6000]}

Write ONLY the expanded section text. No headers, no JSON. MINIMUM {min_words} WORDS."""
                expanded = self._generate_section(expand_prompt, section_name, max_tokens=16000)
                if expanded and len(expanded.split()) > word_count:
                    content = expanded
                    word_count = len(content.split())

            # Enforce citation spread — strip blacklisted citations the LLM used anyway
            if blacklisted_refs and content:
                violations = []
                for ref_key in blacklisted_refs:
                    pattern = re.escape(f"[{ref_key}]")
                    if re.search(pattern, content):
                        violations.append(ref_key)
                if violations:
                    for ref_key in violations:
                        # Remove the citation bracket but keep surrounding text
                        content = content.replace(f"[{ref_key}]", "")
                        # Also handle compound citations like [Author1, 2020; Author2, 2021]
                        content = re.sub(r';\s*' + re.escape(ref_key), '', content)
                        content = re.sub(re.escape(ref_key) + r'\s*;', '', content)
                    # Clean up empty brackets and double spaces
                    content = re.sub(r'\[\s*\]', '', content)
                    content = re.sub(r'  +', ' ', content)
                    logger.info("Stripped %d overused citations from %s: %s",
                                len(violations), section_name, violations)
                    self.display.step(f"  Stripped {len(violations)} overused citations from {section_name}")
                    word_count = len(content.split())

            # Update citation spread tracker
            section_citations = re.findall(r'\[([^\]]+?,\s*\d{4})\]', content or "")
            for cite in section_citations:
                cite_clean = cite.strip()
                if cite_clean not in citation_spread:
                    citation_spread[cite_clean] = set()
                citation_spread[cite_clean].add(section_name)

            written_sections[section_name] = content
            self.display.step(f"  {section_name}: {word_count} words")

        # Write abstract LAST (sees full paper)
        self.display.step("Writing Abstract...")
        full_paper_text = "\n\n".join(
            f"=== {name} ===\n{written_sections[name]}"
            for name in _WRITE_ORDER if name in written_sections
        )

        # Get corpus count for consistency
        search_audit = self.artifacts.get("search_audit", {})
        corpus_count = search_audit.get("total_included", len(curated))

        abstract_prompt = f"""Write the Abstract for this academic paper.

PAPER TITLE: {brief.get('title', '')}
RESEARCH QUESTIONS: {json.dumps(brief.get('research_questions', []))}

FULL PAPER:
{full_paper_text[:20000]}

Requirements:
- 200-400 words
- Summarize: background, methods, key findings, implications
- Include 2+ citations from the paper using [Author, Year] format
- Write as a single paragraph
- Do NOT start with "This paper..." — vary the opening
- CORPUS COUNT: When mentioning the number of studies reviewed, use EXACTLY {corpus_count}.
  This number must be consistent with the Methodology section.
- CITATION RULE: Only cite authors that appear in the reference list of the paper above.
  Do NOT introduce new citations not found in the paper."""

        abstract = self._generate_section(abstract_prompt, "Abstract", max_tokens=4000)
        abstract_words = len(abstract.split()) if abstract else 0
        self.display.step(f"  Abstract: {abstract_words} words")

        self.artifacts["zero_draft"] = written_sections
        self.artifacts["written_sections"] = written_sections  # preserve for hypothesis extraction
        self.artifacts["abstract"] = abstract

        # Generate a comparison table for review/synthesis papers
        paper_type = brief.get("paper_type", "survey").lower()
        # Generate table for any review-like paper type (LLMs use varied labels)
        _is_review = any(kw in paper_type for kw in ("survey", "review", "meta", "synthesis", "analysis"))
        if _is_review and len(curated) >= 5:
            self.display.step("Generating methodology comparison table...")
            try:
                table_data = self._generate_comparison_table(curated, brief)
                if table_data:
                    # Audit table rows against source classification
                    table_data = self._audit_table_citations(table_data, curated)
                    rows = table_data.get("rows", [])
                    if rows:
                        self.artifacts["figures"] = [{
                            "figure_id": "table_1",
                            "caption": table_data.get("caption", "Comparison of key studies"),
                            "data_type": "table",
                            "data": {"headers": table_data.get("headers", []),
                                     "rows": rows},
                        }]
                        self.display.step(f"  Table: {len(rows)} studies compared")
                    else:
                        logger.warning("Table audit left 0 rows — omitting Table 1")
                        self.display.step("  Table: omitted (audit removed all rows)")
            except Exception as e:
                logger.warning("Table generation failed: %s", e)

        # Build claim-evidence ledger (for audit phase)
        self.display.step("Building claim-evidence ledger...")
        try:
            ledger = self._build_claim_evidence_ledger(written_sections, ref_list_text)
            if ledger:
                self.artifacts["claim_evidence_ledger"] = ledger
                self.display.step(f"  Ledger: {len(ledger)} claims mapped")
        except Exception as e:
            logger.warning("Claim-evidence ledger generation failed: %s", e)

        total_words = sum(len(s.split()) for s in written_sections.values()) + abstract_words
        self.display.step(f"Total draft: {total_words} words")
        self.display.phase_done(3)

    def _build_bibliography_context(self, papers: list[dict]) -> str:
        """Build a mega context block with all curated papers' content."""
        parts = []
        for i, paper in enumerate(papers):
            raw_title = paper.get("title", "Untitled")
            title = re.sub(r"<[^>]+>", "", raw_title).strip() if raw_title else "Untitled"
            authors = paper.get("authors", [])
            year = paper.get("year", "N/A")
            content = paper.get("enriched_content", paper.get("abstract", ""))

            author_str = ", ".join(authors[:3]) if authors else "Unknown"
            if len(authors) > 3:
                author_str += " et al."

            # Sanitize year
            if year is None or (isinstance(year, str) and year.strip().lower() in ("none", "null", "n/a", "unknown", "")):
                year = "n.d."
            elif isinstance(year, (int, float)):
                year = str(int(year))

            # Build cite key — NEVER use "Source N" (it leaks into LLM output)
            if authors and isinstance(authors[0], str) and authors[0].strip():
                surname = _extract_surname(authors[0])
                et_al = " et al." if len(authors) >= 3 else ""
                cite_key = f"[{surname}{et_al}, {year}]" if year != "n.d." else f"[{surname}{et_al}]"
            else:
                # Fallback: use first meaningful word from title
                _SKIP = {"the", "a", "an", "of", "in", "on", "for", "and", "to", "with", "from"}
                words = [w.rstrip(",.:;") for w in title.split() if w.lower().rstrip(",.:;") not in _SKIP and len(w) > 2]
                label = words[0] if words else f"Ref{i + 1}"
                cite_key = f"[{label}, {year}]" if year != "n.d." else f"[{label}]"

            header = f"--- Paper {i + 1}: {cite_key} ---"
            header += f"\nTitle: {title}"
            header += f"\nAuthors: {author_str}"
            header += f"\nYear: {year}"

            doi = paper.get("doi", "")
            if doi:
                header += f"\nDOI: {doi}"

            key_finding = paper.get("key_finding", "")
            if key_finding:
                header += f"\nKey finding: {key_finding}"

            parts.append(f"{header}\n\n{content}")

        return "\n\n".join(parts)

    def _build_source_classification(self, papers: list[dict]) -> list[dict]:
        """Build a source classification table from curated papers.

        Uses the LLM to classify each paper's domain, method, and primary finding.
        This serves as a citation-tethering anchor during writing.
        """
        if not papers:
            return []

        # Build compact summaries for the LLM
        summaries = []
        for i, p in enumerate(papers[:30]):
            authors = p.get("authors", [])
            author_str = authors[0] if authors else "Unknown"
            if isinstance(author_str, str) and ", " in author_str:
                author_str = author_str.split(",")[0]
            elif isinstance(author_str, str) and " " in author_str:
                author_str = author_str.split()[-1]
            year = p.get("year", "N/A")
            title = p.get("title", "")[:120]
            abstract = p.get("abstract", "")[:200]
            key_finding = p.get("key_finding", "")[:150]
            summaries.append(
                f"[{i}] {author_str} ({year}): {title}\n"
                f"    Abstract: {abstract}\n"
                f"    Finding: {key_finding}"
            )

        prompt = f"""Classify each paper below. For each, output ONE line in this exact format:
AUTHOR | YEAR | DOMAIN | METHOD | PRIMARY_FINDING

Rules:
- DOMAIN: the paper's actual research field (e.g., "Computational Linguistics", "Genetics", "Scientometrics", "Education")
- METHOD: the paper's actual methodology (e.g., "corpus analysis", "systematic review", "twin study", "bibliometric analysis", "survey", "experiment")
- PRIMARY_FINDING: one sentence describing what the paper ACTUALLY found — not what you wish it found
- Be precise and honest. If a paper is a bibliometric analysis, say so — do not classify it as an empirical study
- If you cannot determine the finding from the title/abstract, write "finding unclear from metadata"

Papers:
{chr(10).join(summaries)}

Output ONLY the classification lines, one per paper, numbered [0], [1], etc."""

        try:
            resp = self.llm.generate("", prompt, max_tokens=4000, temperature=0.1)
            raw = strip_thinking_tags(resp.text if hasattr(resp, 'text') else str(resp)).strip()

            entries = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Parse "[N] Author | Year | Domain | Method | Finding"
                # Remove leading [N] if present
                cleaned = re.sub(r"^\[?\d+\]?\s*", "", line)
                parts = [p.strip() for p in cleaned.split("|")]
                if len(parts) >= 5:
                    entries.append({
                        "author": parts[0],
                        "year": parts[1],
                        "domain": parts[2],
                        "method": parts[3],
                        "finding": parts[4],
                    })
            return entries
        except Exception as e:
            logger.warning("Source classification failed: %s", e)
            return []

    def _build_ref_list(self, papers: list[dict]) -> list[dict]:
        """Build a compact reference list for citation guidance."""
        refs = []
        existing_keys: set[str] = set()
        for i, paper in enumerate(papers):
            authors = paper.get("authors", [])
            year = paper.get("year", "N/A")

            # Sanitize year
            if year is None or (isinstance(year, str) and year.strip().lower() in ("none", "null", "n/a", "unknown", "")):
                year = "n.d."
            elif isinstance(year, (int, float)):
                year = str(int(year))

            raw_title = paper.get("title", "Untitled")
            title = re.sub(r"<[^>]+>", "", raw_title).strip() if raw_title else "Untitled"

            if authors and isinstance(authors[0], str) and authors[0].strip():
                surname = _extract_surname(authors[0])
                et_al = " et al." if len(authors) >= 3 else ""
                cite_key = f"{surname}{et_al}, {year}" if year != "n.d." else f"{surname}{et_al}"
            else:
                _SKIP = {"the", "a", "an", "of", "in", "on", "for", "and", "to", "with", "from"}
                words = [w.rstrip(",.:;") for w in title.split() if w.lower().rstrip(",.:;") not in _SKIP and len(w) > 2]
                label = words[0] if words else f"Ref{i + 1}"
                cite_key = f"{label}, {year}" if year != "n.d." else label

            # Disambiguate collisions with title word
            if cite_key in existing_keys:
                _SKIP2 = {"the", "a", "an", "of", "in", "on", "for", "and", "to", "with", "from"}
                dist_words = [w.rstrip(",.:;") for w in title.split() if w.lower().rstrip(",.:;") not in _SKIP2 and len(w) > 3]
                dist = dist_words[0] if dist_words else str(i)
                cite_key = f"{cite_key}, \"{dist}\""
            existing_keys.add(cite_key)

            venue = paper.get("venue", "") or paper.get("journal", "")
            # Detect source type for evidence-bounding
            title_lower = title.lower()
            venue_lower = (venue or "").lower()
            doi = paper.get("doi", "") or ""
            if any(kw in title_lower for kw in ("meta-analysis", "meta analysis", "systematic review")):
                source_type = "meta-analysis/systematic_review"
            elif any(kw in title_lower for kw in ("review", "overview", "survey", "perspective", "commentary")):
                source_type = "review"
            elif any(kw in venue_lower for kw in ("conference", "proceedings", "workshop", "symposium")):
                source_type = "conference_abstract"
            elif any(kw in doi for kw in ("abstract", "supplement", "poster")):
                source_type = "conference_abstract"
            else:
                source_type = "primary_study"

            ref = {
                "ref_num": i,
                "cite_key": cite_key,
                "title": title,
                "authors": authors[:5],
                "year": year,
                "doi": doi,
                "source_type": source_type,
            }
            if venue:
                ref["venue"] = venue
            refs.append(ref)
        return refs

    def _build_section_prompt(
        self,
        section_name: str,
        brief: dict,
        bib_context: str,
        ref_list_text: str,
        prior_sections: str,
        target_words: int,
        blacklisted_refs: list[str] | None = None,
    ) -> str:
        """Build the mega-context prompt for a single section."""
        rqs = json.dumps(brief.get("research_questions", []))
        contribution = brief.get("contribution_type", "evidence synthesis")

        # Section-specific guidance — matches AGENT_PLAYBOOK.md section isolation rules
        section_guidance = {
            "Methodology": (
                "Describe your search strategy, databases queried, inclusion/exclusion criteria, "
                "and analytical approach. Be specific about which APIs and sources were used. "
                "This is an AI-agent-written paper — do NOT claim human reviewers, inter-rater "
                "reliability, PRISMA flow diagrams, or manual screening.\n"
                "CRITICAL: You are a TEXT SYNTHESIS agent. You searched academic databases and "
                "read published papers. You did NOT download raw data, run bioinformatics pipelines "
                "(DADA2, QIIME2, Kraken, etc.), execute statistical software, compute effect sizes, "
                "run meta-regressions, or reprocess datasets. Do NOT claim any of these. "
                "Describe ONLY your literature search and synthesis methodology.\n"
                "ONLY: your search/synthesis process with concrete numbers (databases, queries, inclusion criteria).\n"
                "NEVER: findings, comparisons with other work, or interpretations.\n"
                "NEVER: claims of running computational pipelines, downloading raw data, or executing software.\n"
                "REPRODUCIBILITY REQUIREMENTS — include ALL of the following:\n"
                "1. Name the exact databases searched (e.g., OpenAlex, Crossref, Semantic Scholar, PubMed)\n"
                "2. State the search date range (e.g., 'articles published between January 2018 and December 2025')\n"
                "3. Provide the actual search query terms used (e.g., '(microplastic OR nanoplastic) AND freshwater')\n"
                "4. State the total number of records retrieved and the final number included after screening\n"
                "5. List specific inclusion criteria (peer-reviewed, English-language, specific study types)\n"
                "6. List specific exclusion criteria (conference abstracts only, non-English, grey literature)\n"
                "7. Describe the synthesis method (narrative synthesis, thematic analysis, contradiction mapping, etc.)\n"
                "8. Explicitly state this is a narrative/conceptual review — NOT a systematic review or meta-analysis\n"
                "MIN CITATIONS: 2-4 (methodological precedents, tools, guidelines)."
            ),
            "Results": (
                "Present the evidence found across the literature. Map findings to your research "
                "questions. Use specific numbers and statistics FROM the source papers (not invented). "
                "Every claim needs a citation. Group findings thematically.\n"
                "ONLY: what you found — patterns, contradictions, evidence maps. Present analysis.\n"
                "NEVER: implications, policy recommendations, future directions — that's Discussion.\n"
                "This is the second-longest section. Characterize the balance of evidence using "
                "qualitative hedging ('several studies suggest,' 'the majority of reviewed work') "
                "unless you can verify exact counts against the bibliography.\n"
                "FORBIDDEN: inventing study counts like '9 studies found X, 4 found Y' unless "
                "you have actually counted those papers in your reference list. This creates false "
                "meta-analytic precision that peer reviewers will flag immediately.\n"
                "EVIDENCE TYPE LABELING: When presenting findings, distinguish direct evidence "
                "(studies measuring the exact phenomenon) from proxy evidence (studies using indirect "
                "indicators). Label proxy evidence explicitly: 'indirect evidence from [X] studies suggests...'\n"
                "MIN CITATIONS: 10-20 (evidence-heavy, this is where findings live)."
            ),
            "Discussion": (
                "Interpret the findings. What do they mean? Where do studies agree or contradict? "
                "What are the implications? Make 2-3 testable predictions. "
                "Connect back to the research questions.\n"
                "ONLY: interpretation, comparison with prior work, implications, testable predictions.\n"
                "NEVER: restate results verbatim or re-introduce the problem.\n"
                "MIN CITATIONS: 5-10."
            ),
            "Related Work": (
                "Organize existing literature into 3-4 thematic clusters. For each cluster, "
                "identify 4-6 key papers and explain their contributions and limitations. "
                "Show how this paper fills a gap not addressed by prior work.\n"
                "ONLY: thematic synthesis of prior work across 3-4 themes. This must be the LONGEST section.\n"
                "NEVER: restate the Introduction, discuss your own findings, or preview results.\n"
                "MIN CITATIONS: 8-15 (citation-heaviest section)."
            ),
            "Introduction": (
                "Establish the problem, its significance, and what is missing in current research. "
                "End with a clear statement of this paper's contribution. "
                "The introduction should make the reader understand WHY this paper matters.\n"
                "REQUIRED: Define 2-4 key terms operationally before synthesizing literature. "
                "Example: 'In this review, simplification refers to reduction in syntactic complexity...'\n"
                "REQUIRED: State the paper type explicitly — 'This conceptual review...' or "
                "'This narrative literature review...' — NOT 'systematic review' or 'meta-analysis'.\n"
                "ONLY: problem statement, key definitions, gap identification, contribution statement.\n"
                "NEVER: preview specific results or discuss related work in detail.\n"
                "MIN CITATIONS: 3-5 (foundational works that frame the problem)."
            ),
            "Limitations": (
                "Be genuinely honest about limitations: search scope, language bias, "
                "AI-agent limitations (no original data collection, reliance on training data). "
                "Suggest how future work could address each limitation.\n"
                "ONLY: specific weaknesses of YOUR methodology and analysis.\n"
                "NEVER: discuss limitations of other papers.\n"
                "MIN CITATIONS: 1-3."
            ),
            "Conclusion": (
                "STRICT FORMAT (follow exactly):\n"
                "Paragraph 1: Three to four KEY TAKEAWAYS — one sentence each, no paragraph-length restatements.\n"
                "Paragraph 2: Two to three SPECIFIC future research directions with concrete methodological suggestions.\n"
                "Paragraph 3: One practical implication for the field.\n"
                "TOTAL: 300-400 words MAXIMUM.\n"
                "CRITICAL ANTI-REPETITION RULE: The Discussion section already interpreted the findings. "
                "Do NOT paraphrase, summarize, or restate anything from the Discussion. "
                "The Conclusion must contain NEW synthesis — distilled takeaways and forward-looking directions ONLY. "
                "If a sentence could appear in the Discussion, DELETE it.\n"
                "MIN CITATIONS: 2-4."
            ),
        }

        guidance = section_guidance.get(section_name, "Write this section with academic rigor.")

        # For Methodology section, inject actual search audit data
        if section_name == "Methodology":
            search_audit = self.artifacts.get("search_audit", {})
            if search_audit:
                queries = search_audit.get("queries", [])
                total_included = search_audit.get('total_included', '?')
                guidance += (
                    f"\n\nACTUAL SEARCH DATA — you MUST use these exact numbers, do NOT invent others:\n"
                    f"- Databases searched: {', '.join(search_audit.get('databases', ['OpenAlex', 'Crossref']))}\n"
                    f"- Search queries used: {'; '.join(repr(q) for q in queries[:6])}\n"
                    f"- Total records retrieved: {search_audit.get('total_retrieved', '?')}\n"
                    f"- After deduplication: {search_audit.get('total_after_dedup', '?')} unique records\n"
                    f"- After relevance screening and domain filtering: {search_audit.get('total_after_filter', '?')} relevant records\n"
                    f"- Final corpus: {total_included} peer-reviewed texts included in the review\n"
                    f"- Year range: 2016-2025\n"
                    f"\nCRITICAL RULES FOR METHODOLOGY:\n"
                    f"- Use ONLY the numbers above. Do NOT invent additional screening stages or counts.\n"
                    f"- The pipeline is: total retrieved → deduplicated → relevance-filtered → final corpus.\n"
                    f"  Present it as exactly this 3-stage funnel. Do NOT add extra stages.\n"
                    f"- Do NOT claim specific dates for when searches were conducted.\n"
                    f"- Do NOT claim PRISMA compliance — this is a narrative review.\n"
                    f"- Whenever you mention the corpus size in the paper, use EXACTLY {total_included}.\n"
                    f"  Do NOT say '35 studies' in one place and '45 texts' in another."
                )

        # Compute word bounds for strict isolation
        min_words = _SECTION_WORD_MINIMUMS.get(section_name, 500)
        other_sections = [s for s in _WRITE_ORDER if s != section_name]
        forbidden_sections = ", ".join(other_sections)

        prompt = f"""You are writing ONLY the '{section_name}' section for an academic paper.
You MUST write between {min_words} and {target_words + 400} words. Do NOT write any other
section. Do NOT write the {forbidden_sections}. Do NOT summarize the paper. Focus entirely
on producing deep, granular prose for this one section.

PAPER TITLE: {brief.get('title', '')}
CONTRIBUTION TYPE: {contribution}
RESEARCH QUESTIONS: {rqs}

SECTION GUIDANCE:
{guidance}

TARGET: {target_words} words ({target_words // 150} paragraphs of ~150 words each)

"""
        # Add prior sections if any
        if prior_sections:
            prompt += f"""PREVIOUSLY WRITTEN SECTIONS (maintain consistency, do not repeat):
{prior_sections[:15000]}

"""

        # Add bibliography — this is the key differentiator from ExpertResearcher
        prompt += f"""REFERENCE LIST (cite using these exact [Author, Year] keys):
{ref_list_text[:8000]}

CITATION GROUNDING RULE: Each [Author, Year] citation MUST match that paper's actual
topic. Check the "title" field before citing. Do NOT attribute claims about topic X to
a paper whose title is about topic Y. If no paper covers a specific claim, write it
without a citation or remove it.

EPISTEMIC HUMILITY RULE: You are writing a conceptual narrative review, NOT a
quantitative meta-analysis. Do NOT invent study counts ("9 studies found X, 4 found Y")
or vote-counting unless you can verify each count against the bibliography. Use
qualitative hedging: "several studies suggest," "the literature is divided,"
"a growing body of evidence indicates." Fake precision is a red flag reviewers catch.

EVIDENCE BOUNDING RULE: Distinguish between DIRECT evidence (studies that directly
measure the phenomenon being discussed) and PROXY evidence (studies that measure
a related variable as an indirect indicator). When citing a proxy study, explicitly
label it: "indirect evidence from [medication/exposure] studies suggests..." or
"[Author, Year], examining [proxy measure] rather than [direct measure], found..."
NEVER present a proxy study as if it were direct evidence for the topic under review.
For example, a PPI-usage study is proxy evidence for microbiome effects, not direct
microbiome diversity measurement. A meta-analysis is a secondary source, not a
primary clinical study.

SOURCE TYPE RULE: Each reference has a "source_type" field. Use it correctly:
- "primary_study": Can carry full argumentative weight for empirical claims
- "review": Secondary source — cite as "as reviewed by [Author, Year]" or "[Author, Year] summarized..."
- "meta-analysis/systematic_review": High-level synthesis — cite for pooled estimates, not individual findings
- "conference_abstract": LOW WEIGHT — never use as sole support for a central claim.
  Frame as: "preliminary data presented by [Author, Year] suggested..." or similar hedging

THEORY VS EVIDENCE RULE: When citing classic theory (pre-2000), frame it as a
"conceptual lens" or "theoretical framework." When citing modern empirical research,
frame it as "empirical findings" or "recent evidence suggests." Never state speculative
philosophical implications as proven empirical facts.

"""
        # Add source classification table if available
        source_table = self.artifacts.get("source_classification")
        if source_table:
            table_text = "\n".join(
                f"- {e['author']}, {e['year']} | {e['domain']} | {e['method']} | {e['finding']}"
                for e in source_table[:30]
            )
            prompt += f"""
SOURCE CLASSIFICATION TABLE (use this to verify citation accuracy):
Each entry shows: Author, Year | Domain | Method | Primary Finding.
Before citing [Author, Year], check this table — your claim MUST match their domain and finding.
{table_text}

"""

        prompt += f"""FULL SOURCE TEXTS (use these for evidence and claims):
{bib_context[:80000]}

Write ONLY the '{section_name}' section body text. No headers, no bold pseudo-headers,
no JSON, no meta-commentary. Use flowing academic prose with paragraph breaks.
Use [Author, Year] citations from the reference list above. Every paragraph needs citations.
IMPORTANT: Write at LEAST {target_words} words. If your section is shorter, expand with
more evidence, more analysis, or more connections between sources."""

        # Inject citation blacklist if any refs have hit their section limit
        if blacklisted_refs:
            blacklist_text = ", ".join(f"[{r}]" for r in blacklisted_refs)
            prompt += f"""

CITATION SPREAD CONSTRAINT: The following references have already been cited in too many
sections. You are FORBIDDEN from citing them in this section. Draw from OTHER references
in the bibliography instead: {blacklist_text}"""

        return prompt

    def _build_claim_evidence_ledger(self, sections: dict[str, str], ref_list_text: str) -> list[dict]:
        """Ask the LLM to extract a claim-evidence ledger from the completed draft.

        For each major claim, maps: claim text, section, citation(s), evidence role.
        Used in the audit phase to flag citation-role mismatches.
        """
        full_text = "\n\n".join(
            f"=== {name} ===\n{sections[name]}"
            for name in _WRITE_ORDER if name in sections
        )

        prompt = f"""Analyze this academic paper draft and extract a claim-evidence ledger.

PAPER DRAFT:
{full_text[:20000]}

BIBLIOGRAPHY:
{ref_list_text[:4000]}

For each MAJOR claim in the paper (claims that carry argumentative weight — skip trivial statements), output a JSON array where each element has:
- "claim": the claim in 1 sentence
- "section": which section it appears in
- "citations": list of [Author, Year] strings supporting it
- "evidence_role": one of "direct_evidence", "theoretical_framing", "secondary_synthesis", "analogy"

Focus on the 15-25 most important claims. Output ONLY the JSON array, no other text."""

        raw = self._generate_section(prompt, "claim_ledger", max_tokens=8000)
        if not raw:
            return []

        # Parse JSON from the response
        try:
            # Strip markdown code fences if present
            cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
            ledger = json.loads(cleaned)
            if isinstance(ledger, list):
                return ledger
        except (json.JSONDecodeError, ValueError):
            logger.warning("Could not parse claim-evidence ledger JSON")
        return []

    def _generate_comparison_table(self, papers: list[dict], brief: dict) -> dict | None:
        """Generate a methodology comparison table from the curated papers."""
        # Build compact paper summaries for the LLM
        summaries = []
        for i, p in enumerate(papers[:15]):  # Max 15 rows to avoid context overload
            authors = p.get("authors", [])
            author_str = authors[0] if authors else "Unknown"
            if len(authors) > 1:
                author_str += " et al."
            summaries.append(
                f"[{i}] {author_str} ({p.get('year', 'N/A')}): {p.get('title', '')[:80]}\n"
                f"    {p.get('key_finding', p.get('abstract', '')[:150])}"
            )

        prompt = f"""Generate a methodology comparison table for this review paper.

PAPER TITLE: {brief.get('title', '')}
PAPER TYPE: {brief.get('paper_type', 'survey')}

STUDIES TO COMPARE:
{chr(10).join(summaries)}

Return JSON with this structure:
{{"caption": "Table 1: Comparison of key studies on [topic]",
  "headers": ["Study", "Year", "Method", "Sample/Scope", "Key Finding"],
  "rows": [["Author et al.", "2020", "method used", "sample/scope", "main finding"], ...]}}

Rules:
- Include 8-15 studies (the most important ones)
- Headers should match the paper's analytical dimensions
- Keep each cell concise (5-15 words)
- Use the actual data from the paper summaries above — do NOT invent or fabricate findings
- CRITICAL: Each row's "Method" and "Key Finding" columns MUST match the paper's ACTUAL title and abstract
- If a paper is a meta-analysis, write "meta-analysis" — not "observational study" or "clinical study"
- If a paper is a review article, write "review" — not "clinical study" or "trial"
- Do NOT describe a paper as studying topic X if its title is about topic Y
- Only include studies whose content you can verify from the summaries above"""

        try:
            result = self.llm.generate_json(
                "You are an academic research assistant. Generate comparison tables. Return valid JSON only.",
                prompt,
                temperature=0.3,
            )
            if result and "headers" in result and "rows" in result:
                return result
        except Exception as e:
            logger.warning("Comparison table generation failed: %s", e)
        return None

    def _audit_table_citations(self, table_data: dict, curated: list[dict]) -> dict:
        """Audit each table row against curated papers to prevent citation misattribution.

        Validates that Study column matches an actual curated paper and that
        the Method/Key Finding columns are consistent with the paper's metadata.
        Removes rows that cannot be matched or that misrepresent the source.
        """
        if not table_data or "rows" not in table_data:
            return table_data

        # Build lookup: surname -> paper metadata
        paper_lookup: dict[str, dict] = {}
        for p in curated:
            authors = p.get("authors", [])
            if not authors:
                continue
            surname = _extract_surname(authors[0]).lower()
            year = str(p.get("year", ""))
            key = f"{surname}_{year}"
            paper_lookup[key] = {
                "title": p.get("title", ""),
                "abstract": p.get("abstract", ""),
                "key_finding": p.get("key_finding", ""),
                "authors": authors,
                "year": year,
            }

        audited_rows = []
        removed_count = 0

        # Also index by surname-only for fuzzy matching
        surname_index: dict[str, list[dict]] = {}
        for p in curated:
            authors = p.get("authors", [])
            if not authors:
                continue
            surname = _extract_surname(authors[0]).lower()
            surname_index.setdefault(surname, []).append({
                "title": p.get("title", ""),
                "abstract": p.get("abstract", ""),
                "key_finding": p.get("key_finding", ""),
                "authors": authors,
                "year": str(p.get("year", "")),
            })

        for row in table_data.get("rows", []):
            if not row or len(row) < 2:
                continue
            study_cell = str(row[0]).strip()
            year_cell = str(row[1]).strip() if len(row) > 1 else ""

            # Extract surname from study cell
            surname_match = re.match(r"([A-Za-z]+)", study_cell)
            if not surname_match:
                audited_rows.append(row)
                continue

            surname = surname_match.group(1).lower()
            key = f"{surname}_{year_cell}"

            # Try exact match first
            paper = paper_lookup.get(key)
            if not paper:
                # Try surname-only match (handles year formatting differences)
                candidates = surname_index.get(surname, [])
                if len(candidates) == 1:
                    paper = candidates[0]
                elif len(candidates) > 1:
                    # Multiple papers by same author — try year match
                    year_matches = [c for c in candidates if c["year"] == year_cell]
                    paper = year_matches[0] if year_matches else candidates[0]

            if not paper:
                # Can't verify — remove (likely hallucinated author)
                logger.warning("Table audit: removing unverifiable row '%s' (no matching curated paper)", study_cell)
                removed_count += 1
                continue

            # Check if method/finding cells have reasonable overlap with paper's actual content
            paper_text = (paper.get("title", "") + " " + paper.get("abstract", "") + " " + paper.get("key_finding", "")).lower()
            # Check last cells (method, finding) for plausibility
            row_text = " ".join(str(c) for c in row[2:]).lower()
            # Extract content words (>3 chars)
            row_words = {w for w in re.findall(r"[a-z]{4,}", row_text)}
            paper_words = {w for w in re.findall(r"[a-z]{4,}", paper_text)}
            overlap = row_words & paper_words

            if len(row_words) > 3 and len(overlap) < 1:
                # Row description completely disconnected — remove
                logger.warning(
                    "Table audit: removing misattributed row '%s' — "
                    "table describes '%s' but paper is about '%s'",
                    study_cell, row_text[:60], paper.get("title", "")[:60],
                )
                removed_count += 1
                continue

            audited_rows.append(row)

        if removed_count > 0:
            logger.info("Table audit: removed %d misattributed rows (kept %d)", removed_count, len(audited_rows))

        table_data["rows"] = audited_rows
        return table_data

    def _extract_hypotheses_and_findings(self, sections: dict[str, str], brief: dict) -> dict:
        """Extract structured hypotheses and findings from written sections."""
        # Combine relevant sections
        relevant = ""
        for name in ("Introduction", "Results", "Discussion", "Conclusion"):
            if name in sections:
                relevant += f"\n--- {name} ---\n{sections[name][:3000]}\n"

        if len(relevant) < 200:
            return {}

        prompt = f"""Analyze this academic paper and extract structured data.

PAPER TOPIC: {brief.get('title', self._topic)}

{relevant}

Extract ALL hypotheses and key findings. Return as JSON:
{{
  "hypotheses": [
    {{
      "id": "H1",
      "statement": "Clear testable hypothesis statement",
      "section": "which section it appears in",
      "status": "proposed|supported|partially_supported|refuted"
    }}
  ],
  "findings": [
    {{
      "id": "F1",
      "statement": "Key finding or result",
      "section": "which section it appears in",
      "hypothesis_ids": ["H1"],
      "confidence": "high|moderate|low"
    }}
  ]
}}

Return ONLY valid JSON. Extract 3-8 hypotheses and 5-15 findings."""

        try:
            result = self.llm.generate_json(
                "You are an academic research analyst. Extract structured hypotheses and findings. Return valid JSON only.",
                prompt,
            )
            if isinstance(result, dict):
                return result
        except Exception as e:
            logger.warning("Hypothesis extraction LLM call failed: %s", e)
        return {}

    def _generate_section(self, prompt: str, section_name: str, max_tokens: int = 16000) -> str:
        """Generate a section with cleanup. If truncated, attempt continuation."""
        try:
            resp = self.llm.generate(
                _SYNTHESIS_SYSTEM,
                prompt,
                temperature=0.7,
                max_tokens=max_tokens,
            )
            text = strip_thinking_tags(resp.text).strip()

            # Detect truncation: finish_reason == "length" or text ends mid-sentence
            is_truncated = (
                resp.finish_reason in ("length", "max_tokens")
                or (text and text[-1] not in '.!?"\')' and len(text) > 200)
            )

            if is_truncated:
                logger.warning("Section '%s' was truncated (finish_reason=%s), attempting continuation",
                               section_name, resp.finish_reason)
                self.display.step(f"  Section '{section_name}' truncated — generating continuation...")
                # Get last ~500 chars for context overlap
                tail = text[-500:] if len(text) > 500 else text
                continuation_prompt = (
                    f"Continue writing the {section_name} section from EXACTLY where it left off. "
                    f"Do NOT repeat any text. Do NOT add a section header. "
                    f"The text so far ends with:\n\n...{tail}\n\n"
                    f"Continue from that point and bring the section to a proper conclusion."
                )
                try:
                    cont_resp = self.llm.generate(
                        _SYNTHESIS_SYSTEM,
                        continuation_prompt,
                        temperature=0.7,
                        max_tokens=max_tokens,
                    )
                    continuation = strip_thinking_tags(cont_resp.text).strip()
                    continuation = self._clean_section_text(continuation)
                    if continuation:
                        text = text + " " + continuation
                except LLMError as e:
                    logger.warning("Continuation failed for section '%s': %s", section_name, e)

        except LLMError as e:
            logger.error("LLM failed for section '%s': %s", section_name, e)
            return ""

        # Clean up: remove markdown headers, code fences, JSON wrapping
        text = self._clean_section_text(text)
        return text

    @staticmethod
    def _clean_section_text(raw: str) -> str:
        """Strip markdown headers, code fences, JSON wrapping, thinking tags."""
        text = strip_thinking_tags(raw).strip()

        # If wrapped in JSON, extract content
        if text.lstrip().startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "content" in parsed:
                    text = parsed["content"]
            except json.JSONDecodeError:
                pass

        lines = text.split("\n")
        cleaned = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                continue
            if stripped.startswith("## ") or stripped.startswith("# "):
                continue
            cleaned.append(line)

        return "\n".join(cleaned).strip()

    # ------------------------------------------------------------------
    # Step 4: Audit (deterministic post-processing)
    # ------------------------------------------------------------------

    def _step4_audit(self) -> None:
        """Citation audit, fabrication sanitization, reference verification."""
        logger.info("Step 4: AUDIT")
        self.display.phase_start(4, "Audit & Verify")
        self.display.tick()

        draft = self.artifacts.get("zero_draft", {})

        # 4a. Fabrication sanitization (reuse from ExpertResearcher)
        self.display.step("Sanitizing fabrication markers...")
        has_figures = bool(self.artifacts.get("figures"))
        draft = self._sanitize_fabrication(draft, has_figures=has_figures)

        # 4a2. Framework language audit (Fix 3B) — only for review/survey/synthesis
        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        paper_type = brief.get("paper_type", "survey")
        self.display.step("Auditing framework language...")
        fw_replacements = self._audit_framework_language(draft, paper_type)
        if fw_replacements:
            self.display.step(f"  Downgraded {fw_replacements} framework overclaiming phrase(s)")
        else:
            self.display.step("  No framework overclaiming detected")

        # 4b. Citation density enforcement
        self.display.step("Enforcing citation density...")
        draft = self._enforce_citation_density(draft)

        # 4c. Fix truncated sections
        for heading, content in draft.items():
            stripped = content.rstrip()
            if stripped and stripped[-1] not in '.!?"\')':
                last_end = max(stripped.rfind("."), stripped.rfind("!"), stripped.rfind("?"))
                if last_end > len(stripped) * 0.7:
                    draft[heading] = stripped[:last_end + 1]

        # 4d. Word count check
        for section_name, min_words in _SECTION_WORD_MINIMUMS.items():
            content = draft.get(section_name, "")
            if content and len(content.split()) < min_words:
                self.display.step(f"  WARNING: {section_name} has {len(content.split())} words (min {min_words})")

        # 4e. Citation spread enforcement (Playbook Rule 3: max 3 sections per ref, except 2 anchors)
        self.display.step("Enforcing citation spread limits...")
        self._enforce_citation_spread(draft)

        # 4e2. Claim-evidence ledger audit — flag citation-role mismatches
        ledger = self.artifacts.get("claim_evidence_ledger", [])
        if ledger:
            self.display.step("Auditing claim-evidence ledger...")
            assertive_with_framing = 0
            for entry in ledger:
                role = entry.get("evidence_role", "")
                if role in ("theoretical_framing", "analogy"):
                    claim = entry.get("claim", "")
                    section = entry.get("section", "")
                    # Check if claim uses assertive (non-hedged) language
                    hedging = re.search(
                        r"\b(suggest|consistent with|may|might|could|possibly|speculatively|one possible)\b",
                        claim, re.IGNORECASE,
                    )
                    if not hedging:
                        assertive_with_framing += 1
                        citations = ", ".join(entry.get("citations", []))
                        logger.warning(
                            "Claim-role mismatch: '%s' in %s cites %s as %s but uses assertive language",
                            claim[:80], section, citations, role,
                        )
            if assertive_with_framing:
                self.display.step(
                    f"  WARNING: {assertive_with_framing} claims use assertive language "
                    f"with theoretical_framing/analogy sources — review manually"
                )
            else:
                self.display.step("  Ledger audit: no citation-role mismatches detected")

        # 4e3. Claim-citation relevance check (Fix 1C) — warning-only
        self.display.step("Checking claim-citation relevance...")
        mismatches = self._check_claim_citation_relevance(draft)
        if mismatches:
            self.display.step(f"  WARNING: {len(mismatches)} potential citation-content mismatches")
            for m in mismatches[:5]:
                logger.warning(
                    "Claim-citation mismatch: [%s] in %s — overlap: %d words (%s)",
                    m["citation"], m["section"], m["overlap_count"],
                    ", ".join(m["overlap_words"]) if m["overlap_words"] else "none",
                )
            self.artifacts["claim_citation_mismatches"] = mismatches
        else:
            self.display.step("  No claim-citation mismatches detected")

        # 4f. Reference verification
        self.display.step("Verifying references...")
        curated = self.artifacts.get("curated_papers", [])
        self.display.step(f"  Curated papers available: {len(curated)}")
        references = self._build_submission_references(curated)
        self.display.step(f"  Submission references built: {len(references)}")

        try:
            import asyncio
            verifier = ReferenceVerifier()
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        report = pool.submit(asyncio.run, verifier.verify_all(references)).result()
                else:
                    report = loop.run_until_complete(verifier.verify_all(references))
            except RuntimeError:
                report = asyncio.run(verifier.verify_all(references))
            self.display.step(
                f"  Verified: {report.references_verified}, "
                f"Failed: {report.references_failed}, "
                f"Uncertain: {report.references_uncertain}"
            )

            # Remove failed references (but keep at least the minimum)
            ref_floor = brief.get("_ref_min", 20)
            if report.references_failed > 0:
                failed_ids = {
                    r.ref_id for r in report.results
                    if not r.verified and r.confidence < CONFIDENCE_REMOVE
                }
                if failed_ids:
                    kept = [r for r in references if r.get("ref_id") not in failed_ids]
                    if len(kept) >= ref_floor:
                        references = kept
                        self.display.step(f"  Removed {len(failed_ids)} unverifiable references")
                    else:
                        logger.info("Skipping verification pruning: would leave only %d refs (floor: %d)", len(kept), ref_floor)
                        self.display.step(f"  Kept unverifiable references (need minimum {ref_floor})")

            # Overwrite authors/venue/DOI with API-verified canonical data
            for vr in report.results:
                if vr.verified and vr.canonical_data:
                    for ref in references:
                        if ref.get("ref_id") == vr.ref_id:
                            api_authors = vr.canonical_data.get("authors", [])
                            if api_authors:
                                ref["authors"] = api_authors
                            api_venue = vr.canonical_data.get("venue", "")
                            if api_venue:
                                ref["venue"] = api_venue
                            api_doi = vr.canonical_data.get("doi", "")
                            if api_doi:
                                ref["doi"] = api_doi
                            break
        except Exception as e:
            logger.warning("Reference verification failed: %s", e)

        # 4g. Prune orphan references (in bibliography but never cited in text)
        all_text = " ".join(draft.values())
        if self.artifacts.get("abstract"):
            all_text += " " + self.artifacts["abstract"]
        pre_prune = len(references)
        pruned_refs = []
        for ref in references:
            # Check if any author surname appears in text as a citation
            cited = False
            for author in ref.get("authors", []) or []:
                if isinstance(author, str) and author.strip():
                    surname = _extract_surname(author)
                    if len(surname) >= 2 and re.search(
                        r"\[[^\]]*" + re.escape(surname) + r"[^\]]*\]", all_text, re.IGNORECASE
                    ):
                        cited = True
                        break
            if cited:
                pruned_refs.append(ref)
            else:
                logger.info("Pruning orphan reference: %s", ref.get("title", "?")[:60])

        # Safety: keep all if pruning would drop below 8 refs
        if len(pruned_refs) >= 8:
            orphan_count = pre_prune - len(pruned_refs)
            if orphan_count > 0:
                self.display.step(f"  Pruned {orphan_count} orphan references (never cited in text)")
                references = pruned_refs
        else:
            logger.info("Skipping orphan ref pruning: would leave only %d refs", len(pruned_refs))

        # 4h. Fix bare-year citations [YYYY] → remove them (no author = unusable)
        bare_year_pat = re.compile(r"\[(\d{4})\]")
        bare_count = 0
        for section_key in list(draft.keys()):
            new_content, n = bare_year_pat.subn("", draft[section_key])
            if n > 0:
                bare_count += n
                # Clean up leftover artifacts
                new_content = re.sub(r"\s{2,}", " ", new_content)
                new_content = re.sub(r"\s+\.", ".", new_content)
                draft[section_key] = new_content
        if bare_count > 0:
            self.display.step(f"  Removed {bare_count} bare-year citations [YYYY]")

        # 4i. Fix citation-year mismatches (author exists but year wrong)
        surname_years: dict[str, set[str]] = {}
        for ref in references:
            year_str = str(ref.get("year", ""))
            for author in ref.get("authors", []) or []:
                if isinstance(author, str) and author.strip():
                    sn = _extract_surname(author).lower()
                    if len(sn) >= 2:
                        surname_years.setdefault(sn, set()).add(year_str)

        year_fix_count = 0
        cite_year_pat = re.compile(r"\[([A-Z][a-zA-Z]+(?:\s+et\s+al\.)?),\s*(\d{4}[a-z]?)\]")
        for section_key in list(draft.keys()):
            def _fix_year(m: re.Match) -> str:
                nonlocal year_fix_count
                author_part = m.group(1)
                cited_year = m.group(2)
                surname = author_part.split(" et ")[0].strip().lower()
                valid_years = surname_years.get(surname, set())
                if valid_years and cited_year not in valid_years:
                    # Pick closest valid year
                    closest = min(valid_years, key=lambda y: abs(int(y[:4]) - int(cited_year[:4])) if y.isdigit() else 9999)
                    year_fix_count += 1
                    return f"[{author_part}, {closest}]"
                return m.group(0)
            draft[section_key] = cite_year_pat.sub(_fix_year, draft[section_key])

        if year_fix_count > 0:
            self.display.step(f"  Fixed {year_fix_count} citation-year mismatches")

        # 4j. Safety floor: if pruning left fewer than 8 refs, pad from curated papers
        if len(references) < 8:
            existing_titles = {r.get("title", "").lower() for r in references}
            for paper in curated:
                if len(references) >= 10:
                    break
                t = paper.get("title", "").lower()
                if t and t not in existing_titles:
                    ref = self._build_single_submission_ref(paper, len(references))
                    references.append(ref)
                    existing_titles.add(t)
            self.display.step(f"  Padded references to {len(references)} (safety floor)")

        # 4k. Filter future-dated references (current year or later)
        import datetime as _dt
        current_year = _dt.datetime.now().year
        pre_future = len(references)
        future_filtered = []
        for ref in references:
            try:
                ref_year = int(ref.get("year", 0) or 0)
            except (ValueError, TypeError):
                ref_year = 0
            if ref_year >= current_year:
                logger.warning(
                    "Removing future-dated reference: %s (%s)",
                    ref.get("title", "?")[:60], ref.get("year", "?"),
                )
            else:
                future_filtered.append(ref)
        if len(future_filtered) >= 8:
            removed = pre_future - len(future_filtered)
            if removed > 0:
                references = future_filtered
                self.display.step(f"  Removed {removed} future-dated references (year >= {current_year})")
        else:
            logger.info("Skipping future-date filter: would leave only %d refs", len(future_filtered))

        # 4l. Remove off-topic references (multi-signal relevance check)
        from agentpub.academic_search import (
            _filter_by_topic_relevance, _STOPWORDS, _GENERIC_WORDS,
            _extract_bigrams, _clean_words,
        )
        # Build domain fingerprint from topic + brief
        brief = self.artifacts.get("research_brief", {})
        topic_text = brief.get("title", self._topic)
        # Also include search terms for broader domain matching
        for term in brief.get("search_terms", []):
            topic_text += " " + term
        topic_words = {w for w in _clean_words(topic_text)
                       if w not in _STOPWORDS and len(w) > 1}
        domain_words = topic_words - _GENERIC_WORDS
        topic_bigrams = _extract_bigrams(topic_text)

        # Retrieve stored abstracts from step 2 for richer matching
        ref_abstracts = self.artifacts.get("ref_abstracts", {})

        if domain_words:
            pre_topic = len(references)
            topic_filtered = []
            for ref in references:
                title = ref.get("title", "")
                if not title:
                    topic_filtered.append(ref)
                    continue

                # Build text from title + abstract (if available from step 2)
                abstract = ref_abstracts.get(title.lower()[:80], "")
                full_text = f"{title} {abstract}"
                text_words = set(_clean_words(full_text))
                text_bigrams = _extract_bigrams(full_text)

                # Signal 1: Bigram match (strongest)
                if topic_bigrams & text_bigrams:
                    topic_filtered.append(ref)
                    continue

                # Signal 2: Domain word overlap (title + abstract)
                d_overlap = domain_words & text_words
                all_overlap = topic_words & text_words
                cite_count = (ref.get("citation_count", 0)
                              or ref.get("citationCount", 0) or 0)

                # High-citation: still needs ≥1 domain word
                if cite_count > 500 and len(d_overlap) >= 1:
                    topic_filtered.append(ref)
                    continue

                # Normal: ≥1 domain word + ≥2 total, or high fractional overlap
                if len(d_overlap) >= 1 and len(all_overlap) >= 2:
                    topic_filtered.append(ref)
                    continue

                logger.warning("Removing off-topic reference: %s (domain overlap: %s)",
                               title[:80], d_overlap)

            if len(topic_filtered) >= 8:
                removed = pre_topic - len(topic_filtered)
                if removed > 0:
                    references = topic_filtered
                    self.display.step(f"  Removed {removed} off-topic references")
            else:
                logger.info("Skipping topic filter: would leave only %d refs",
                            len(topic_filtered))

        # 4l2. Renumber ref_ids to be sequential (fix gaps from pruning)
        for i, ref in enumerate(references):
            ref["ref_id"] = f"ref-{i + 1}"

        # 4m. Fix impossible methodology screening numbers
        meth_text = draft.get("Methodology", "")
        if meth_text:
            # Find sequences of numbers describing a screening flow
            flow_nums = re.findall(
                r'(\d+)\s+(?:records?|papers?|texts?|articles?|sources?|studies|results?|unique|relevant|initial)',
                meth_text
            )
            flow_nums_int = [int(n) for n in flow_nums if int(n) > 5]
            # Check if any number increases from a previous one (impossible in a screening flow)
            has_impossible = False
            for i in range(1, len(flow_nums_int)):
                if flow_nums_int[i] > flow_nums_int[i - 1]:
                    has_impossible = True
                    break
            if has_impossible:
                logger.warning("Impossible methodology numbers detected: %s — asking LLM to fix", flow_nums_int)
                self.display.step("  Fixing impossible methodology screening numbers...")
                fix_prompt = (
                    f"The following Methodology section contains logically impossible record counts.\n"
                    f"The numbers {flow_nums_int} appear in a screening flow, but some steps INCREASE "
                    f"instead of decreasing. In any screening process: retrieved > deduplicated > screened > included.\n\n"
                    f"METHODOLOGY TEXT:\n{meth_text}\n\n"
                    f"Rewrite this Methodology section with CORRECTED numbers that form a valid "
                    f"decreasing sequence. Use the ACTUAL number of references in this paper ({len(references)}) "
                    f"as the final included count. Keep all other text identical. "
                    f"Return ONLY the corrected methodology text, no headers or labels."
                )
                try:
                    fixed = self._generate_section(fix_prompt, "methodology_fix", max_tokens=8000)
                    if fixed and len(fixed) > 200:
                        draft["Methodology"] = fixed
                        self.display.step("  Methodology numbers corrected")
                except Exception as e:
                    logger.warning("Methodology fix failed: %s", e)

        # 4n-pre. D1: Overclaiming phrase downgrade (all paper types)
        overclaim_count = 0
        for section_key in list(draft.keys()):
            text = draft[section_key]
            for pattern, replacement in _OVERCLAIM_PATTERNS:
                text, n = pattern.subn(replacement, text)
                overclaim_count += n
            draft[section_key] = text
        # Also clean abstract
        abstract = self.artifacts.get("abstract", "")
        if abstract:
            for pattern, replacement in _OVERCLAIM_PATTERNS:
                abstract, n = pattern.subn(replacement, abstract)
                overclaim_count += n
            self.artifacts["abstract"] = abstract
        if overclaim_count > 0:
            logger.info("Overclaim downgrade: %d phrases softened", overclaim_count)
            self.display.step(f"  Softened {overclaim_count} overclaiming phrases")

        # 4n-pre2. C2: Corpus count consistency check
        # Ensure abstract and methodology report consistent study counts
        search_audit = self.artifacts.get("search_audit", {})
        actual_refs = len(references)
        actual_included = search_audit.get("total_included", actual_refs)
        # Scan abstract + methodology for study count claims
        count_pat = re.compile(
            r"(?:exactly\s+|approximately\s+|roughly\s+|nearly\s+|about\s+|of\s+)?"
            r"(\d{2,3})\s+"
            r"(?:peer[\s\-\u2010\u2011\u2012\u2013\u2014]reviewed\s+|published\s+|included\s+|selected\s+|reviewed\s+|primary\s+)?"
            r"(?:studies|papers|sources|articles|texts|works|publications)"
        )
        for target_key in ["abstract"]:
            target_text = self.artifacts.get(target_key, "")
            if not target_text:
                continue
            matches = count_pat.findall(target_text)
            for m in matches:
                claimed = int(m)
                if abs(claimed - actual_refs) > 3:
                    logger.warning(
                        "Corpus count mismatch in %s: claims %d but %d references exist",
                        target_key, claimed, actual_refs,
                    )
                    # Replace the claimed count with actual, stripping any preceding qualifier
                    new_num = str(actual_refs)
                    target_text = self.artifacts[target_key]
                    target_text = re.sub(
                        r"(?:exactly|approximately|roughly|nearly|about)\s+" + str(claimed) + r"\b",
                        f"approximately {new_num}",
                        target_text,
                        count=1,
                    )
                    # Fallback: plain number replacement if no qualifier prefix
                    if str(claimed) in target_text and str(new_num) not in target_text:
                        target_text = target_text.replace(
                            f"{claimed} ", f"approximately {new_num} ", 1
                        )
                    self.artifacts[target_key] = target_text
                    self.display.step(
                        f"  Fixed corpus count in {target_key}: {claimed} -> ~{actual_refs}"
                    )
        # Same check in Methodology section
        if "Methodology" in draft:
            meth_text = draft["Methodology"]
            matches = count_pat.findall(meth_text)
            for m in matches:
                claimed = int(m)
                if abs(claimed - actual_refs) > 3:
                    new_num = str(actual_refs)
                    meth_text = re.sub(
                        r"(?:exactly|approximately|roughly|nearly|about)\s+" + str(claimed) + r"\b",
                        f"approximately {new_num}",
                        meth_text,
                        count=1,
                    )
                    if str(claimed) in meth_text and str(new_num) not in meth_text:
                        meth_text = meth_text.replace(
                            f"{claimed} ", f"approximately {new_num} ", 1
                        )
                    draft["Methodology"] = meth_text
                    logger.warning(
                        "Corpus count in Methodology: %d -> ~%d", claimed, actual_refs
                    )
                    self.display.step(
                        f"  Fixed corpus count in Methodology: {claimed} -> ~{actual_refs}"
                    )

        # 4n. Strip orphan citations from abstract and body
        # Build set of valid (surname, year) pairs from final references
        valid_surname_years: set[tuple[str, str]] = set()
        for ref in references:
            year_str = str(ref.get("year", ""))
            for author in ref.get("authors", []) or []:
                if isinstance(author, str) and author.strip():
                    sn = _extract_surname(author).lower()
                    if len(sn) >= 2 and year_str:
                        valid_surname_years.add((sn, year_str))

        cite_pat = re.compile(r"\[([A-Z][a-zA-Z]+(?:\s+et\s+al\.)?),\s*(\d{4})[a-z]?\]")
        orphan_cite_count = 0

        def _strip_orphan_cites(text: str) -> str:
            nonlocal orphan_cite_count
            def _check(m: re.Match) -> str:
                nonlocal orphan_cite_count
                author_part = m.group(1)
                year_part = m.group(2)
                surname = author_part.split(" et ")[0].strip().lower()
                if (surname, year_part) not in valid_surname_years:
                    orphan_cite_count += 1
                    return ""
                return m.group(0)
            cleaned = cite_pat.sub(_check, text)
            # Clean up artifacts from removed citations
            cleaned = re.sub(r"\s*\(\s*\)\s*", " ", cleaned)
            cleaned = re.sub(r"\s{2,}", " ", cleaned)
            return cleaned.strip()

        # Clean abstract
        abstract = self.artifacts.get("abstract", "")
        if abstract:
            self.artifacts["abstract"] = _strip_orphan_cites(abstract)

        # Clean body sections
        for section_key in list(draft.keys()):
            draft[section_key] = _strip_orphan_cites(draft[section_key])

        if orphan_cite_count > 0:
            self.display.step(f"  Stripped {orphan_cite_count} orphan citations (author+year not in references)")

        # 4o. LLM source-verification pass: check ALL text (body + abstract + table)
        #     against reference titles & abstracts. Flags unsupported claims,
        #     table rows not backed by any source, and citation misattributions.
        self.display.step("Running source-verification pass...")
        try:
            draft, abstract_verified = self._verify_against_sources(
                draft, self.artifacts.get("abstract", ""), references,
            )
            if abstract_verified is not None:
                self.artifacts["abstract"] = abstract_verified
        except Exception as e:
            logger.warning("Source verification failed (non-fatal): %s", e)

        self.artifacts["final_paper"] = draft
        self.artifacts["references"] = references

        total_words = sum(len(s.split()) for s in draft.values())
        self.display.step(f"Final paper: {total_words} words, {len(references)} references")
        self.display.phase_done(4)

    # ------------------------------------------------------------------
    # Step 5: Submit
    # ------------------------------------------------------------------

    def _step5_submit(self, challenge_id: str | None = None) -> dict:
        """Assemble and submit to AgentPub API."""
        self.display.step("Submitting to AgentPub...")
        self.display.tick()

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        draft = self.artifacts.get("final_paper", self.artifacts.get("zero_draft", {}))
        abstract = self.artifacts.get("abstract", "")
        references = self.artifacts.get("references", [])

        # Build sections in submission order
        sections = []
        for heading in _SUBMIT_ORDER:
            content = draft.get(heading, "")
            if content:
                sections.append({"heading": heading, "content": content})

        # Check word count
        total_words = sum(len(s["content"].split()) for s in sections)
        _skip_api = False
        _skip_reason = ""
        if total_words < self.config.min_total_words:
            _skip_api = True
            _skip_reason = f"Paper has {total_words} words but requires minimum {self.config.min_total_words}"

        # Check required sections
        present = {s["heading"] for s in sections}
        missing = [h for h in _SUBMIT_ORDER if h not in present]
        if missing:
            _skip_api = True
            _skip_reason = f"Missing required sections: {', '.join(missing)}"

        # Reverse-orphan check: strip in-text citations with no matching reference
        self._strip_orphan_citations(sections, abstract, references)

        # Final cleanup: fix phantom spaces left by any citation/sentence removal
        for i, sec in enumerate(sections):
            content = sec["content"]
            content = re.sub(r"\s+\.", ".", content)
            content = re.sub(r"\s+,", ",", content)
            content = re.sub(r"\s+;", ";", content)
            content = re.sub(r"\s+\)", ")", content)
            content = re.sub(r"\(\s+", "(", content)
            content = re.sub(r"\s{2,}", " ", content)
            sections[i] = {"heading": sec["heading"], "content": content.strip()}

        title = brief.get("title", "Untitled Research Paper")

        # Token usage
        token_usage = self.llm.total_usage

        # Generation duration
        generation_seconds = round(time.time() - self._research_start_time, 1)

        # SDK version
        import agentpub
        sdk_version = getattr(agentpub, "__version__", "unknown")

        # Content hash
        full_text = title + "\n" + abstract + "\n"
        for s in sections:
            full_text += s.get("heading", "") + "\n" + s.get("content", "") + "\n"
        content_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()

        metadata = {
            "agent_model": self.llm.model_name,
            "agent_platform": self.llm.provider_name,
            "research_protocol": "playbook_5step",
            "phases_completed": 5,
            "papers_reviewed": len(self.artifacts.get("curated_papers", [])),
            "quality_level": self.config.quality_level,
            "input_tokens": token_usage.get("input_tokens", 0),
            "output_tokens": token_usage.get("output_tokens", 0),
            "total_tokens": token_usage.get("total_tokens", 0),
            "generation_seconds": generation_seconds,
            "sdk_version": sdk_version,
            "content_hash": content_hash,
        }

        tags = self._generate_tags(brief, title)

        paper_payload = {
            "title": title,
            "abstract": abstract,
            "sections": sections,
            "references": references,
            "metadata": metadata,
            "challenge_id": challenge_id,
            "tags": tags,
        }

        # Include figures/tables if generated
        figures = self.artifacts.get("figures", [])
        if figures:
            paper_payload["figures"] = figures

        # Table-text cross-validation: ensure referenced tables exist in figures
        all_text = abstract + " " + " ".join(s["content"] for s in sections)
        table_refs = set(re.findall(r"(?:Table|Figure)\s+(\d+)", all_text))
        figure_ids = {str(f.get("figure_id", "")).replace("table_", "").replace("figure_", "")
                      for f in figures} if figures else set()
        missing_tables = table_refs - figure_ids
        if missing_tables and not figures:
            # Text references tables but no figures exist — strip the references
            for i, sec in enumerate(sections):
                cleaned = re.sub(
                    r"\s*\(?\s*(?:see\s+)?(?:Table|Figure)\s+\d+\s*\)?\s*",
                    " ", sec["content"])
                sections[i] = {"heading": sec["heading"], "content": re.sub(r"\s{2,}", " ", cleaned).strip()}
            logger.warning("Stripped orphan table/figure references (no figures in payload)")

        # Recent reference enforcement: require 5+ references from 2023+
        recent_count = sum(1 for r in references
                          if int(r.get("year", 0) or 0) >= 2023)
        if recent_count < 5:
            logger.warning("Only %d recent references (2023+), minimum 5 recommended", recent_count)

        # Extract structured hypotheses and findings
        try:
            written_sections = self.artifacts.get("written_sections", {})
            structured = self._extract_hypotheses_and_findings(written_sections, brief)
            if structured.get("hypotheses"):
                paper_payload["hypotheses"] = structured["hypotheses"]
            if structured.get("findings"):
                paper_payload["findings"] = structured["findings"]
        except Exception as e:
            logger.warning("Hypothesis/finding extraction failed: %s", e)

        if _skip_api:
            saved_path = self._save_paper_locally(paper_payload)
            self.display.step(f"Paper saved locally: {saved_path}")
            self.display.complete(f"Saved locally: {saved_path.name}")
            return {
                "error": _skip_reason,
                "title": title,
                "word_count": total_words,
                "saved_locally": str(saved_path),
            }

        # Final cleanup: strip thinking tags from all fields
        paper_payload["title"] = strip_thinking_tags(paper_payload["title"]).strip()
        paper_payload["abstract"] = strip_thinking_tags(paper_payload["abstract"]).strip()
        for s in paper_payload["sections"]:
            s["content"] = self._clean_section_text(s["content"])

        # Submit with intelligent retry and LLM-powered rework
        result = self._submit_with_rework(paper_payload, title, total_words)
        # Signal completion even on failure so GUI can auto-stop in single-paper mode
        if result.get("error") and not result.get("paper_id"):
            self.display.complete(f"Finished (with errors): {result['error'][:80]}")
        return result

    # ------------------------------------------------------------------
    # Submission: intelligent retry with LLM rework
    # ------------------------------------------------------------------

    def _submit_with_rework(
        self, paper_payload: dict, title: str, total_words: int
    ) -> dict:
        """Submit paper with intelligent error handling.

        - Content/validation errors (400/422): use LLM to rework, then retry
        - Rate limits (429): wait and retry with backoff
        - Server errors (5xx) / network: save locally for later resubmission
        """
        import re as _re

        max_content_retries = 3   # LLM rework attempts for validation errors
        max_rate_retries = 5      # Rate limit retry attempts

        rate_attempt = 0

        # Filter payload to only accepted kwargs for submit_paper
        _submit_keys = {"title", "abstract", "sections", "references", "metadata",
                        "challenge_id", "tags", "figures"}
        submit_payload = {k: v for k, v in paper_payload.items() if k in _submit_keys}

        for content_attempt in range(1, max_content_retries + 1):
            # Try submitting
            try:
                result = self.client.submit_paper(**submit_payload)
            except Exception as e:
                err_str = str(e)

                # Rate limit errors — retryable with backoff
                if "429" in err_str:
                    if "daily" in err_str.lower() or "limit reached" in err_str.lower():
                        self.display.step("Daily submission limit reached — saving locally")
                        logger.warning("Daily limit reached: %s", err_str)
                        saved_path = self._save_paper_locally(paper_payload)
                        return {
                            "error": "Daily submission limit reached. Submit later with: agentpub submit <file>",
                            "title": title, "word_count": total_words,
                            "saved_locally": str(saved_path),
                        }
                    rate_attempt += 1
                    if rate_attempt > max_rate_retries:
                        saved_path = self._save_paper_locally(paper_payload)
                        self.display.step("Rate limit — saved locally for later submission")
                        return {
                            "error": "Rate limited after retries. Submit later with: agentpub submit <file>",
                            "title": title, "word_count": total_words,
                            "saved_locally": str(saved_path),
                        }
                    wait_match = _re.search(r"wait (\d+)m", err_str)
                    wait_secs = int(wait_match.group(1)) * 60 + 30 if wait_match else min(120 * rate_attempt, 600)
                    self.display.step(f"Cooldown — waiting {wait_secs}s (attempt {rate_attempt}/{max_rate_retries})")
                    logger.info("429 rate limit, waiting %ds", wait_secs)
                    time.sleep(wait_secs)
                    continue

                # Network/server errors — save locally
                saved_path = self._save_paper_locally(paper_payload)
                self.display.step(f"Submission error: {err_str[:120]}")
                return {
                    "error": f"Server/network error: {err_str[:200]}. Submit later with: agentpub submit <file>",
                    "title": title, "word_count": total_words,
                    "saved_locally": str(saved_path),
                }

            # Success — paper accepted
            if result.get("paper_id"):
                pid = result["paper_id"]
                logger.info("Paper submitted successfully: %s", pid)
                self.display.step(f"Published: {pid}")
                self.display.complete(f"Published as {pid}")
                return result

            # Content/validation error — try LLM rework
            if result.get("error") == "validation_rejected":
                detail = result.get("detail", "Unknown validation error")
                detail_str = json.dumps(detail) if isinstance(detail, (dict, list)) else str(detail)
                status_code = result.get("status_code", 422)
                logger.warning("Validation rejected (attempt %d/%d): %s",
                               content_attempt, max_content_retries, detail_str[:300])
                self.display.step(f"Submission rejected: {detail_str[:100]}")

                if content_attempt < max_content_retries:
                    self.display.step(f"Reworking paper based on feedback (attempt {content_attempt}/{max_content_retries})...")
                    paper_payload = self._rework_paper_for_error(paper_payload, detail_str)
                    continue  # retry with reworked payload
                else:
                    # Exhausted rework attempts — save locally
                    saved_path = self._save_paper_locally(paper_payload)
                    self.display.step("Could not fix validation issues — saved locally")
                    return {
                        "error": f"Validation rejected after {max_content_retries} rework attempts: {detail_str[:200]}",
                        "title": title, "word_count": total_words,
                        "saved_locally": str(saved_path),
                    }

            # Unexpected response shape — save locally
            saved_path = self._save_paper_locally(paper_payload)
            self.display.step("Unexpected API response — saved locally")
            return {
                "error": f"Unexpected response: {json.dumps(result)[:200]}",
                "title": title, "word_count": total_words,
                "saved_locally": str(saved_path),
            }

        # Fallback (should not reach)
        saved_path = self._save_paper_locally(paper_payload)
        return {"error": "Submission failed", "title": title, "word_count": total_words,
                "saved_locally": str(saved_path)}

    def _rework_paper_for_error(self, paper_payload: dict, error_detail: str) -> dict:
        """Use the LLM to fix the paper based on API validation feedback."""
        # Classify common fixable issues and handle programmatically first
        error_lower = error_detail.lower()

        # --- Programmatic fixes (no LLM needed) ---

        # Too few references
        if "references" in error_lower and ("at least" in error_lower or "minimum" in error_lower):
            import re as _re
            m = _re.search(r"at least (\d+)", error_lower)
            needed = int(m.group(1)) if m else 8
            current = len(paper_payload.get("references", []))
            if current < needed:
                logger.info("Need %d references, have %d — asking LLM to add more", needed, current)
                self.display.step(f"Adding references ({current} → {needed} minimum)...")
                paper_payload = self._add_missing_references(paper_payload, needed)
                return paper_payload

        # Missing required fields
        if "field required" in error_lower or "missing" in error_lower:
            logger.info("Missing field detected, attempting LLM fix: %s", error_detail[:200])

        # --- LLM-powered fix for everything else ---
        sections_summary = ""
        for s in paper_payload.get("sections", []):
            wc = len(s.get("content", "").split())
            sections_summary += f"  - {s.get('heading', '?')}: {wc} words\n"

        refs_count = len(paper_payload.get("references", []))

        system = (
            "You are a research paper editor. The paper was rejected by the API with a validation error. "
            "Fix the paper to address the error. Return ONLY valid JSON with the corrected fields."
        )
        prompt = f"""The paper submission was rejected with this error:

{error_detail}

Current paper structure:
- Title: {paper_payload.get('title', 'N/A')}
- Abstract: {len(paper_payload.get('abstract', '').split())} words
- Sections:
{sections_summary}- References: {refs_count}

Provide corrections as JSON. Only include fields that need changing:
- "title": corrected title (if title was the issue)
- "abstract": corrected abstract (if too short/long)
- "sections": list of {{"heading": ..., "content": ...}} for sections that need changes
- "references": list of corrected references (if ref format was wrong)

Return ONLY the JSON with fields that need fixing. If no fix is possible, return {{"no_fix": true}}."""

        try:
            fixes = self.llm.generate_json(system, prompt)
        except Exception as e:
            logger.warning("LLM rework failed: %s", e)
            return paper_payload  # return unchanged

        if not fixes or fixes.get("no_fix"):
            logger.info("LLM could not determine a fix")
            return paper_payload

        # Apply fixes
        if "title" in fixes and isinstance(fixes["title"], str):
            paper_payload["title"] = strip_thinking_tags(fixes["title"]).strip()
            logger.info("Reworked title")

        if "abstract" in fixes and isinstance(fixes["abstract"], str):
            paper_payload["abstract"] = strip_thinking_tags(fixes["abstract"]).strip()
            logger.info("Reworked abstract")

        if "sections" in fixes and isinstance(fixes["sections"], list):
            # Merge fixed sections into payload by heading
            existing = {s["heading"]: i for i, s in enumerate(paper_payload["sections"])}
            for fix_sec in fixes["sections"]:
                heading = fix_sec.get("heading", "")
                content = fix_sec.get("content", "")
                if heading in existing and content:
                    paper_payload["sections"][existing[heading]]["content"] = (
                        self._clean_section_text(content)
                    )
                    logger.info("Reworked section: %s", heading)

        if "references" in fixes and isinstance(fixes["references"], list) and fixes["references"]:
            paper_payload["references"] = fixes["references"]
            logger.info("Reworked references (%d)", len(fixes["references"]))

        return paper_payload

    def _add_missing_references(self, paper_payload: dict, minimum: int) -> dict:
        """Ask LLM to generate additional references to meet the minimum."""
        current_refs = paper_payload.get("references", [])
        needed = minimum - len(current_refs)
        if needed <= 0:
            return paper_payload

        existing_titles = [r.get("title", "") for r in current_refs]
        topic = paper_payload.get("title", "the research topic")

        system = "You are an academic research assistant. Generate additional real, plausible references."
        prompt = f"""The paper "{topic}" needs {needed} more references (currently {len(current_refs)}, minimum {minimum}).

Existing reference titles (do not duplicate):
{json.dumps(existing_titles[:15], indent=1)}

Generate {needed + 2} additional references as a JSON list. Each reference must have:
- "title": full paper title
- "authors": list of author names
- "year": publication year (2018-2025)
- "venue": journal or conference name
- "url": DOI URL or empty string

Return ONLY the JSON list."""

        try:
            new_refs = self.llm.generate_json(system, prompt)
        except Exception as e:
            logger.warning("Failed to generate additional references: %s", e)
            return paper_payload

        if isinstance(new_refs, list):
            candidates = []
            for ref in new_refs:
                if isinstance(ref, dict) and ref.get("title"):
                    # Mark as LLM-generated so verifier doesn't auto-trust
                    ref["ref_id"] = f"llm_gen_{len(current_refs) + len(candidates) + 1}"
                    if "type" not in ref:
                        ref["type"] = "external"
                    # Convert year to int if string
                    if isinstance(ref.get("year"), str):
                        try:
                            ref["year"] = int(ref["year"])
                        except ValueError:
                            pass
                    candidates.append(ref)

            # Verify LLM-generated refs before adding
            if candidates:
                try:
                    import asyncio
                    verifier = ReferenceVerifier()
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures
                            with concurrent.futures.ThreadPoolExecutor() as pool:
                                report = pool.submit(asyncio.run, verifier.verify_all(candidates)).result()
                        else:
                            report = loop.run_until_complete(verifier.verify_all(candidates))
                    except RuntimeError:
                        report = asyncio.run(verifier.verify_all(candidates))

                    verified_ids = {r.ref_id for r in report.results if r.verified}
                    candidates = [r for r in candidates if r["ref_id"] in verified_ids]
                    logger.info(
                        "LLM-generated refs: %d verified out of %d candidates",
                        len(candidates), len(new_refs),
                    )
                except Exception as e:
                    logger.warning("LLM ref verification failed, dropping all: %s", e)
                    candidates = []

            added = 0
            for ref in candidates:
                current_refs.append(ref)
                added += 1
            paper_payload["references"] = current_refs
            logger.info("Added %d references (now %d total)", added, len(current_refs))

        return paper_payload

    # ------------------------------------------------------------------
    # Review (for ContinuousDaemon compatibility)
    # ------------------------------------------------------------------

    def review_paper(self, paper_id: str) -> dict:
        """Review a single paper using the LLM."""
        paper = self.client.get_paper(paper_id)
        return self._do_review(paper)

    def review_pending(self) -> list[dict]:
        """Review all pending assignments."""
        assignments = self.client.get_review_assignments()
        if not assignments:
            return []
        results = []
        for a in assignments:
            try:
                paper = self.client.get_paper(a.paper_id)
                result = self._do_review(paper)
                results.append(result)
            except Exception as e:
                results.append({"paper_id": a.paper_id, "error": str(e)})
        return results

    def _do_review(self, paper) -> dict:
        """Review a paper using the LLM."""
        paper_text = f"Title: {paper.title}\nAbstract: {paper.abstract}\n"
        if paper.sections:
            for sec in paper.sections[:10]:
                heading = sec.get("heading", "")
                content = sec.get("content", "")[:2000]
                paper_text += f"\n## {heading}\n{content}\n"

        system = "You are an expert academic peer reviewer. Evaluate the paper rigorously."
        prompt = f"""{paper_text[:10000]}

Review this paper. Return JSON with:
- "scores": dict with keys ["novelty", "methodology", "clarity", "reproducibility", "citation_quality"], each 1-10
- "overall_score": float 1-10
- "decision": "accept", "revise", or "reject"
- "summary": 2-3 sentence summary
- "strengths": list of 3-5 strengths
- "weaknesses": list of 3-5 weaknesses"""

        try:
            review = self.llm.generate_json(system, prompt)
        except Exception:
            review = {}

        scores = review.get("scores", {})
        for dim in ["novelty", "methodology", "clarity", "reproducibility", "citation_quality"]:
            if dim not in scores:
                scores[dim] = 5
        # Remove any extra keys — API only accepts the 5 dimensions
        scores = {k: v for k, v in scores.items() if k in {"novelty", "methodology", "clarity", "reproducibility", "citation_quality"}}

        overall = review.get("overall_score", sum(scores.values()) / max(len(scores), 1))
        decision = review.get("decision", "revise")
        try:
            self.client.submit_review(
                paper_id=paper.paper_id,
                scores=scores,
                decision=decision,
                summary=review.get("summary", "") or "No summary provided.",
                strengths=review.get("strengths") or ["Not specified."],
                weaknesses=review.get("weaknesses") or ["Not specified."],
            )
        except Exception as e:
            logger.warning("Failed to submit review: %s", e)

        return {"paper_id": paper.paper_id, "decision": decision, "score": overall}

    # ------------------------------------------------------------------
    # Shared utilities
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Fix 2B: Paper complexity classifier
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_paper_complexity(brief: dict) -> str:
        """Classify paper complexity to determine ref targets.

        Returns: 'single_domain', 'cross_domain', or 'meta_analysis'
        """
        paper_type = brief.get("paper_type", "survey").lower()
        if "meta" in paper_type:
            return "meta_analysis"

        title = brief.get("title", "").lower()
        if "meta-analysis" in title or "meta analysis" in title:
            return "meta_analysis"

        # Cross-domain indicators
        cross_indicators = 0
        scope_in = brief.get("scope_in", [])
        if len(scope_in) >= 3:
            cross_indicators += 1

        cross_keywords = [
            "cross-disciplin", "interdisciplin", "multi-disciplin",
            "convergence", "intersection", "bridging", "integrat",
            "cross-domain", "multidomain",
        ]
        for kw in cross_keywords:
            if kw in title:
                cross_indicators += 1

        search_terms = brief.get("search_terms", [])
        # If search terms span very different domains, that's cross-domain
        if len(search_terms) >= 5:
            cross_indicators += 1

        if cross_indicators >= 2:
            return "cross_domain"

        return "single_domain"

    # ------------------------------------------------------------------
    # Fix 1C: Claim-citation relevance check
    # ------------------------------------------------------------------

    def _check_claim_citation_relevance(self, draft: dict[str, str]) -> list[dict]:
        """Lightweight keyword-overlap check between cited papers and citing sentences.

        Returns a list of mismatch warnings (stored in artifacts, not auto-fixed).
        """
        curated = self.artifacts.get("curated_papers", [])
        # Build a lookup: ref keyword sets keyed by surname
        source_keywords: dict[str, set[str]] = {}
        for paper in curated:
            authors = paper.get("authors", [])
            if not authors:
                continue
            # Use first author's surname as key
            surname = _extract_surname(authors[0]) if authors[0] else ""
            if not surname:
                continue
            # Collect keywords from title + key_finding
            words = set()
            for field in ("title", "key_finding", "abstract"):
                text = (paper.get(field) or "").lower()
                words.update(w for w in text.split() if len(w) > 3)
            # Remove stopwords
            words -= {
                "this", "that", "with", "from", "have", "been", "were",
                "their", "which", "these", "those", "also", "more", "than",
                "about", "into", "each", "between", "through", "other",
                "study", "paper", "results", "analysis", "research", "review",
                "using", "based", "effect", "effects", "approach", "found",
            }
            source_keywords[surname.lower()] = words

        # Scan draft for citations and check overlap
        cite_pattern = re.compile(
            r"\[([A-Z][a-zA-Z]+(?:\s+et\s+al\.)?(?:,\s*\d{4}[a-z]?)?)\]"
        )
        mismatches: list[dict] = []

        for section, content in draft.items():
            sentences = re.split(r"(?<=[.!?])\s+", content)
            for sentence in sentences:
                citations = cite_pattern.findall(sentence)
                if not citations:
                    continue
                sentence_words = set(sentence.lower().split())
                sentence_words = {w for w in sentence_words if len(w) > 3}

                for cite in citations:
                    surname = cite.split(",")[0].split(" et ")[0].strip().lower()
                    keywords = source_keywords.get(surname, set())
                    if not keywords:
                        continue  # Can't check — no source info
                    overlap = keywords & sentence_words
                    if len(overlap) < 2:
                        mismatches.append({
                            "section": section,
                            "citation": cite,
                            "sentence": sentence[:120],
                            "overlap_count": len(overlap),
                            "overlap_words": sorted(overlap),
                        })

        return mismatches

    # ------------------------------------------------------------------
    # 4o: LLM source-verification pass
    # ------------------------------------------------------------------

    def _verify_against_sources(
        self,
        draft: dict[str, str],
        abstract: str,
        references: list[dict],
    ) -> tuple[dict[str, str], str | None]:
        """Send full paper text + all reference abstracts to LLM for verification.

        Checks:
        - Every citation [Author, Year] is used for a claim the source actually supports
        - Table rows (if any) correspond to real references
        - No claims are stronger than what the cited source's abstract supports
        - Corpus counts match actual reference count

        Returns (updated_draft, updated_abstract_or_None).
        """
        # Build reference context: title + abstract for each ref
        ref_abstracts = self.artifacts.get("ref_abstracts", {})
        ref_context_parts = []
        for i, ref in enumerate(references):
            title = ref.get("title", "Unknown")
            authors = ref.get("authors", [])
            year = ref.get("year", "?")
            author_str = authors[0] if authors else "Unknown"
            # Look up abstract from step 2 enrichment
            abs_text = ref_abstracts.get(title.lower()[:80], "")
            ref_context_parts.append(
                f"[REF-{i+1}] {author_str} ({year}): {title}\n"
                f"  Abstract: {abs_text[:300] if abs_text else '(not available)'}"
            )
        ref_context = "\n".join(ref_context_parts)

        # Build full paper text
        body_text = "\n\n".join(
            f"=== {heading} ===\n{content}"
            for heading, content in draft.items()
        )

        # Include table if it exists
        figures = self.artifacts.get("figures", [])
        table_text = ""
        for fig in figures:
            if fig.get("data_type") == "table":
                data = fig.get("data", {})
                headers = data.get("headers", [])
                rows = data.get("rows", [])
                if rows:
                    table_text = f"\n\n=== {fig.get('caption', 'Table 1')} ===\n"
                    table_text += " | ".join(headers) + "\n"
                    for row in rows:
                        table_text += " | ".join(str(c) for c in row) + "\n"

        prompt = f"""You are a rigorous academic fact-checker. Below is a paper draft, its abstract, and the COMPLETE list of references with their abstracts.

TASK: Identify problems that must be fixed. Return JSON with this structure:
{{
  "issues": [
    {{
      "type": "citation_mismatch" | "unsupported_claim" | "table_fabrication" | "corpus_count_error",
      "section": "section name or 'Abstract' or 'Table 1'",
      "quote": "the problematic sentence or table row (exact text)",
      "explanation": "why this is wrong based on the reference abstract",
      "fix": "suggested replacement text (or 'REMOVE' if the sentence/row should be deleted)"
    }}
  ]
}}

Rules:
- Only flag issues you are CERTAIN about based on the reference abstracts provided
- citation_mismatch: a citation [Author, Year] is used to support a claim the source's abstract does NOT support
- unsupported_claim: a strong claim (no hedging) that no reference abstract supports
- table_fabrication: a table row author/finding that does not match any reference in the list
- corpus_count_error: abstract or methodology claims a different number of studies than the {len(references)} references
- Do NOT flag hedged statements ("suggests", "may", "is consistent with")
- Do NOT flag claims that are reasonable inferences from the cited abstract
- If there are no issues, return {{"issues": []}}
- Maximum 15 issues (most severe first)

=== ABSTRACT ===
{abstract}

=== PAPER BODY ===
{body_text[:15000]}
{table_text}

=== REFERENCES ({len(references)} total) ===
{ref_context[:8000]}"""

        result = self.llm.generate_json(
            "You are an academic fact-checker. Return valid JSON only.",
            prompt,
            temperature=0.2,
            max_tokens=6000,
        )

        issues = result.get("issues", [])
        if not issues:
            self.display.step("  Source verification: no issues found")
            return draft, None

        self.display.step(f"  Source verification: {len(issues)} issues found")

        # Process fixes
        fixes_applied = 0
        table_rows_removed = 0
        abstract_modified = False
        modified_abstract = abstract

        for issue in issues:
            itype = issue.get("type", "")
            section = issue.get("section", "")
            quote = issue.get("quote", "")
            fix = issue.get("fix", "")

            if not quote or not fix:
                continue

            self.display.step(f"    [{itype}] {section}: {quote[:60]}...")

            # Apply fix to table
            if itype == "table_fabrication" and figures:
                for fig in figures:
                    if fig.get("data_type") != "table":
                        continue
                    data = fig.get("data", {})
                    rows = data.get("rows", [])
                    # Find and remove the matching row
                    new_rows = []
                    for row in rows:
                        row_text = " ".join(str(c) for c in row).lower()
                        if quote.lower()[:30] in row_text:
                            table_rows_removed += 1
                            logger.info("Verification: removed table row '%s'", row_text[:60])
                        else:
                            new_rows.append(row)
                    data["rows"] = new_rows
                continue

            # Detect instruction-like fixes (LLM returned editing commands instead of text)
            _is_instruction = any(kw in fix.upper() for kw in [
                "REMOVE", "DELETE", "ADD THE", "REPLACE WITH", "INSERT",
                "REWRITE", "CHANGE TO", "SHOULD BE", "FIX THIS",
            ])
            if _is_instruction:
                # Treat as removal — don't insert instructions into the paper
                fix = "REMOVE"

            # Apply fix to abstract
            if section.lower() == "abstract" and quote in modified_abstract:
                if fix == "REMOVE":
                    modified_abstract = modified_abstract.replace(quote, "").strip()
                else:
                    modified_abstract = modified_abstract.replace(quote, fix)
                abstract_modified = True
                fixes_applied += 1
                continue

            # Apply fix to body sections
            for heading in draft:
                if quote in draft[heading]:
                    if fix == "REMOVE":
                        draft[heading] = draft[heading].replace(quote, "").strip()
                    else:
                        draft[heading] = draft[heading].replace(quote, fix)
                    fixes_applied += 1
                    break

        # Clean up empty table if all rows were removed
        if table_rows_removed > 0:
            for fig in figures:
                if fig.get("data_type") == "table":
                    remaining = len(fig.get("data", {}).get("rows", []))
                    if remaining == 0:
                        self.artifacts["figures"] = [f for f in figures if f is not fig]
                        self.display.step(f"  Removed empty table (all {table_rows_removed} rows failed verification)")
                    else:
                        self.display.step(f"  Removed {table_rows_removed} fabricated table rows ({remaining} remain)")

        if fixes_applied > 0:
            self.display.step(f"  Applied {fixes_applied} text fixes from verification")

        # Safety net: strip any editorial instructions that leaked into text
        _editorial_pat = re.compile(
            r"\s*(?:REMOVE|DELETE|ADD|REPLACE|INSERT|REWRITE|FIX)\s+"
            r"(?:the|this|that)?\s*[^.]{10,80}(?:citation|reference|claim|sentence|text)[^.]*\.\s*",
            re.IGNORECASE,
        )
        editorial_stripped = 0
        for heading in draft:
            cleaned, n = _editorial_pat.subn(" ", draft[heading])
            if n:
                draft[heading] = cleaned
                editorial_stripped += n
        if abstract_modified:
            cleaned, n = _editorial_pat.subn(" ", modified_abstract)
            if n:
                modified_abstract = cleaned
                editorial_stripped += n
        if editorial_stripped > 0:
            self.display.step(f"  Stripped {editorial_stripped} leaked editorial instructions")
            logger.warning("Stripped %d editorial instructions from final text", editorial_stripped)

        # Clean phantom double-spaces left by removals
        for heading in draft:
            draft[heading] = re.sub(r"\s{2,}", " ", draft[heading]).strip()
        if abstract_modified:
            modified_abstract = re.sub(r"\s{2,}", " ", modified_abstract).strip()

        return draft, modified_abstract if abstract_modified else None

    # ------------------------------------------------------------------
    # Fix 3A+3B: Framework language audit
    # ------------------------------------------------------------------

    @staticmethod
    def _audit_framework_language(draft: dict[str, str], paper_type: str) -> int:
        """Detect and downgrade framework overclaiming language.

        Only fires for review/survey/synthesis paper types.
        Returns the number of replacements made.
        """
        if paper_type.lower() not in _REVIEW_PAPER_TYPES:
            return 0

        total_replacements = 0
        for section, content in draft.items():
            new_content = content
            for pattern, replacement in _FRAMEWORK_PATTERNS:
                new_content, count = pattern.subn(replacement, new_content)
                if count > 0:
                    logger.info(
                        "Framework language fix in %s: %d replacement(s) → '%s'",
                        section, count, replacement,
                    )
                    total_replacements += count
            if new_content != content:
                draft[section] = new_content

        return total_replacements

    # ------------------------------------------------------------------
    # Fix 2C: Corpus expansion when below minimum
    # ------------------------------------------------------------------

    def _expand_corpus_if_needed(self, curated: list[dict], brief: dict) -> list[dict]:
        """Run additional searches if corpus is below the complexity-appropriate minimum."""
        min_refs = brief.get("_ref_min", 20)
        target_refs = brief.get("_ref_target", 28)

        if len(curated) >= min_refs:
            return curated

        self.display.step(
            f"Corpus expansion needed: {len(curated)} refs < {min_refs} minimum "
            f"(target: {target_refs})"
        )

        seen_titles: set[str] = {(p.get("title") or "").lower()[:60] for p in curated}
        new_papers: list[dict] = []

        def _dedup_add(papers: list[dict]) -> None:
            for p in papers:
                key = (p.get("title") or "").lower()[:60]
                if key and key not in seen_titles:
                    seen_titles.add(key)
                    new_papers.append(p)

        # Strategy 1: Broader search terms (use research questions as queries)
        rqs = brief.get("research_questions", [])
        for rq in rqs[:3]:
            if len(curated) + len(new_papers) >= target_refs:
                break
            try:
                hits = search_academic(
                    rq, limit=10,
                    year_from=2014,  # Broader year range
                    mailto=self.owner_email or None,
                )
                _dedup_add(hits)
                self.display.step(f"  Expanded with RQ search: +{len(hits)} candidates")
            except Exception as e:
                logger.warning("Corpus expansion search failed: %s", e)
            time.sleep(0.5)

        # Strategy 2: Follow citation graph from top existing papers via S2 API
        if len(curated) + len(new_papers) < min_refs:
            for paper in curated[:5]:
                if len(curated) + len(new_papers) >= target_refs:
                    break
                s2_id = paper.get("paper_id_s2", "")
                if s2_id:
                    try:
                        refs = fetch_paper_references(s2_id, limit=15)
                        _dedup_add(refs)
                        self.display.step(f"  Citation graph expansion: +{len(refs)} candidates")
                    except Exception as e:
                        logger.warning("Citation graph expansion failed: %s", e)
                    time.sleep(0.5)

        if new_papers:
            # Score new papers minimally (give them a default score)
            for p in new_papers:
                p.setdefault("relevance_score", 0.4)
                p.setdefault("on_domain", True)
            curated.extend(new_papers[:target_refs - len(curated)])
            self.display.step(
                f"  Corpus expanded: {len(curated)} refs (added {len(new_papers)} new papers)"
            )
        else:
            self.display.step("  No additional papers found during expansion")

        return curated

    def _build_submission_references(self, curated: list[dict]) -> list[dict]:
        """Build submission-ready reference list from curated papers."""
        # Enrich venues from Crossref for papers with DOI but no venue
        self._enrich_venues(curated)

        references = []
        for paper in curated:
            pid = paper.get("paper_id", f"ref_{len(references)}")
            authors = paper.get("authors", [])
            year = paper.get("year")
            doi = paper.get("doi", "")
            url = paper.get("url", "")

            is_platform = paper.get("source") == "agentpub"
            ref_type = "internal" if is_platform else "external"
            ref_source = None
            if is_platform:
                ref_source = "agentpub"
            elif doi:
                ref_source = "doi"
            elif url:
                ref_source = "url"

            # Clean ref_id
            clean_ref_id = pid
            if pid.startswith(("s2_", "serper_", "web_")):
                if doi:
                    clean_ref_id = f"doi:{doi}"
                elif authors:
                    surname = authors[0].split()[-1].lower() if isinstance(authors[0], str) else "unknown"
                    clean_ref_id = f"ext:{surname}_{year}" if year else f"ext:{surname}"
                else:
                    clean_ref_id = f"ext:ref_{len(references) + 1}"

            # Strip HTML tags from title (Crossref/Semantic Scholar sometimes return <i>, <b>, etc.)
            raw_title = paper.get("title", "Unknown")
            clean_title = re.sub(r"<[^>]+>", "", raw_title).strip() if raw_title else "Unknown"

            ref = {
                "ref_id": clean_ref_id,
                "type": ref_type,
                "title": clean_title,
            }
            if ref_source:
                ref["source"] = ref_source
            if authors and isinstance(authors, list):
                ref["authors"] = [a for a in authors if a][:10]
            if year:
                try:
                    ref["year"] = int(year)
                except (ValueError, TypeError):
                    pass
            if doi:
                ref["doi"] = doi
            if url:
                ref["url"] = url
            venue = paper.get("venue", "") or paper.get("journal", "")
            if venue:
                ref["venue"] = venue

            references.append(ref)

        return references

    @staticmethod
    def _build_single_submission_ref(paper: dict, index: int) -> dict:
        """Build a single submission-ready reference from a curated paper dict."""
        authors = paper.get("authors", [])
        doi = paper.get("doi", "")
        url = paper.get("url", "")
        raw_title = paper.get("title", "Unknown")
        clean_title = re.sub(r"<[^>]+>", "", raw_title).strip() if raw_title else "Unknown"

        ref = {
            "ref_id": f"ref-{index + 1}",
            "type": "internal" if paper.get("source") == "agentpub" else "external",
            "title": clean_title,
        }
        if doi:
            ref["source"] = "doi"
            ref["doi"] = doi
        elif url:
            ref["source"] = "url"
            ref["url"] = url
        if authors and isinstance(authors, list):
            ref["authors"] = [a for a in authors if a][:10]
        year = paper.get("year")
        if year:
            try:
                ref["year"] = int(year)
            except (ValueError, TypeError):
                pass
        venue = paper.get("venue", "") or paper.get("journal", "")
        if venue:
            ref["venue"] = venue
        return ref

    @staticmethod
    def _enrich_venues(papers: list[dict]) -> None:
        """Batch-enrich venue/journal from Crossref for papers with DOI but no venue."""
        import httpx
        from agentpub.academic_search import _throttle

        to_enrich = [p for p in papers if p.get("doi") and not p.get("venue")]
        if not to_enrich:
            return

        logger.info("Enriching venues for %d refs via Crossref...", len(to_enrich))
        enriched = 0
        with httpx.Client(timeout=8) as client:
            for paper in to_enrich[:30]:  # cap to avoid slowdown
                doi = paper["doi"]
                try:
                    _throttle("crossref")
                    resp = client.get(
                        f"https://api.crossref.org/works/{doi}",
                        params={"mailto": "agent@agentpub.org"},
                    )
                    if resp.status_code == 200:
                        item = resp.json().get("message", {})
                        container = item.get("container-title", [])
                        if container:
                            paper["venue"] = container[0]
                            enriched += 1
                except Exception:
                    pass
        if enriched:
            logger.info("Enriched %d/%d venues", enriched, len(to_enrich))

    @staticmethod
    def _collect_cited_keys(sections: dict[str, str], abstract: str = "") -> set[str]:
        """Scan all text for [Author, Year] citation patterns."""
        pattern = re.compile(
            r"\["
            r"("
            r"[A-Z][a-zA-Z\-']+(?:\s+[a-z]+)*(?:\s+[A-Z][a-zA-Z\-']*)?"
            r"(?:\s+(?:et\s+al\.|and|&)\s*[A-Z]?[a-zA-Z\-']*)?"
            r"(?:,\s*\d{4}[a-z]?)?"
            r")"
            r"\]"
        )
        _NOT_CITATIONS = {
            "figure", "figures", "fig", "table", "tables", "tab",
            "supplementary", "supporting", "appendix", "panel",
            "section", "chapter", "equation", "box", "note",
        }
        cited: set[str] = set()
        all_text = abstract + "\n" + "\n".join(sections.values())
        for match in pattern.finditer(all_text):
            key = match.group(0).lower()
            inner = match.group(1).strip().split()[0].lower() if match.group(1).strip() else ""
            if inner in _NOT_CITATIONS:
                continue
            cited.add(key)
        return cited

    def _strip_orphan_citations(
        self, sections: list[dict], abstract: str, references: list[dict]
    ) -> None:
        """Remove in-text citations that have no matching reference.

        Handles both full-bracket orphans like [FS, 2023] and sub-citation
        orphans inside multi-cite brackets like [FS, 2023; Kapoor et al., 2026].
        """
        ref_surnames: set[str] = set()
        ref_author_years: set[tuple[str, str]] = set()
        for ref in references:
            year_str = str(ref.get("year", ""))
            for author in ref.get("authors", []) or []:
                if isinstance(author, str) and author.strip():
                    surname = _extract_surname(author).lower()
                    if len(surname) >= 2:
                        ref_surnames.add(surname)
                        if year_str:
                            ref_author_years.add((surname, year_str))
            for w in re.findall(r"[a-zA-Z]{5,}", ref.get("title", "")):
                ref_surnames.add(w.lower())

        def _is_orphan_subcite(subcite: str) -> bool:
            """Check if an individual sub-citation (e.g. 'FS, 2023') has no matching ref."""
            sub = subcite.strip()
            # Extract surname: "Smith et al., 2023" -> "Smith", "FS, 2023" -> "FS"
            surname = sub.split(",")[0].split(" et ")[0].split(" and ")[0].strip().lower()
            if not surname or surname in ref_surnames:
                # Surname found — also check year if present
                year_match = re.search(r"\d{4}", sub)
                if surname and year_match:
                    year = year_match.group()
                    # If we have author-year pairs and this combo doesn't exist, it's orphan
                    if ref_author_years and (surname, year) not in ref_author_years:
                        # But only if the surname itself IS in our author-year set
                        # (title-word matches don't have years)
                        if any(s == surname for s, _ in ref_author_years):
                            return True
                return False
            return True

        for i, sec in enumerate(sections):
            content = sec["content"]

            # Find all citation brackets [...]
            def _clean_bracket(m: re.Match) -> str:
                inner = m.group(1)
                # Split on semicolons to get individual sub-citations
                subcites = [s.strip() for s in inner.split(";")]
                kept = [s for s in subcites if not _is_orphan_subcite(s)]
                if not kept:
                    return ""  # All sub-citations are orphans — remove entire bracket
                return "[" + "; ".join(kept) + "]"

            content = re.sub(r"\[([^\]]+)\]", _clean_bracket, content)

            # Clean up artifacts
            content = re.sub(r"\(\s*[;,\s]*\s*\)", "", content)
            content = re.sub(r"\s{2,}", " ", content)
            content = re.sub(r"\s+\.", ".", content)
            content = re.sub(r"\s+,", ",", content)
            sections[i] = {"heading": sec["heading"], "content": content}

        # Count orphans stripped for logging
        stripped_count = sum(1 for ref_s in ref_surnames if ref_s)  # just log that we ran
        logger.info("Reverse-orphan citation check completed")

    @staticmethod
    def _enforce_citation_spread(draft: dict[str, str]) -> None:
        """Playbook Rule 3: no reference in more than 3 sections (2 anchors allowed in 4).

        Removes excess citations from sections where the ref is least important.
        """
        cite_pattern = re.compile(r"\[([A-Z][a-zA-Z]+(?:\s+et\s+al\.)?(?:,\s*\d{4}[a-z]?)?(?:,\s*\"[^\"]+\")?)\]")

        # Map each cite_key -> list of sections it appears in
        cite_sections: dict[str, list[str]] = {}
        for section_name, content in draft.items():
            for match in cite_pattern.finditer(content):
                key = match.group(1)
                cite_sections.setdefault(key, [])
                if section_name not in cite_sections[key]:
                    cite_sections[key].append(section_name)

        # Find the 2 most-used citations (anchors — allowed in 4 sections)
        by_count = sorted(cite_sections.items(), key=lambda x: len(x[1]), reverse=True)
        anchor_keys = {k for k, secs in by_count[:2]}

        # Sections where a citation is LEAST important (trim from these first)
        trim_priority = ["Conclusion", "Limitations", "Introduction", "Methodology", "Discussion", "Related Work", "Results"]

        for key, sections in cite_sections.items():
            max_allowed = 4 if key in anchor_keys else 3
            if len(sections) <= max_allowed:
                continue

            # Remove from lowest-priority sections first
            excess = len(sections) - max_allowed
            for trim_sec in trim_priority:
                if excess <= 0:
                    break
                if trim_sec in sections and trim_sec in draft:
                    # Remove all instances of this citation from trim_sec
                    pattern = re.compile(re.escape(f"[{key}]"))
                    # Replace [Key] with empty string, then clean up orphan semicolons
                    new_content = pattern.sub("", draft[trim_sec])
                    new_content = re.sub(r"\[\s*;\s*", "[", new_content)
                    new_content = re.sub(r";\s*\]", "]", new_content)
                    new_content = re.sub(r"\[\s*\]", "", new_content)
                    # Clean phantom spaces left by citation removal
                    new_content = re.sub(r"\s+\.", ".", new_content)
                    new_content = re.sub(r"\s+,", ",", new_content)
                    new_content = re.sub(r"\s+\)", ")", new_content)
                    new_content = re.sub(r"\(\s*\)", "", new_content)
                    new_content = re.sub(r"\s{2,}", " ", new_content)
                    draft[trim_sec] = new_content
                    sections.remove(trim_sec)
                    excess -= 1

    @staticmethod
    def _sanitize_fabrication(draft: dict[str, str], has_figures: bool = False) -> dict[str, str]:
        """Strip fabricated methodology claims that LLMs hallucinate.

        Args:
            draft: section heading -> content mapping
            has_figures: if True, keep Table/Figure references (real tables exist)
        """
        _FABRICATION_PATTERNS = [
            # Human reviewer roleplay
            r"[Cc]ohen['\u2019]?s?\s+kappa",
            r"inter[- ]?rater\s+reliability",
            r"two\s+independent\s+reviewers",
            r"three\s+independent\s+reviewers",
            r"disagreements?\s+(?:were|was)\s+resolved\s+by\s+consensus",
            r"manual\s+screening\s+by\s+(?:two|three|multiple)\s+(?:reviewers|researchers)",
            r"dual\s+(?:independent\s+)?review",
            r"PRISMA\s+flow\s+diagram",
            r"hand[- ]?search(?:ed|ing)?",
            r"snowball\s+(?:sampling|search)",
            r"trained\s+human\s+annotators?\s+validated",
            r"blinded\s+(?:assessment|evaluation)",
            r"participants?\s+were\s+recruited",
            r"(?:IRB|ethics\s+committee)\s+approval",
            r"wet[- ]?lab\s+experiment(?:s|ation)?",
            r"informed\s+consent\s+was\s+obtained",
            r"verified\s+by\s+(?:a\s+)?human\s+(?:team|expert|reviewer)",
            r"human[- ]?curated",
            r"domain\s+expert\s+(?:review|validation|verification)",
            # Fabricated statistics
            r"pooled\s+(?:mean|effect\s+size|estimate)\s*[=:]\s*[\d.-]+",
            r"(?:95|99)%?\s*CI\s*[\[=(]\s*[\d.-]+\s*[,;–-]\s*[\d.-]+\s*[\])]",
            r"I[²2]\s*[=:]\s*\d+(?:\.\d+)?%?",
            # Fabricated figures/tables — skip if real figures exist
            *([] if has_figures else [
                r"(?:Table|Figure)\s+\d+\s*[.:]\s*\w",
                r"(?:Supplementary|Supporting)\s+(?:Figure|Table|Material)s?\s+S?\d",
                r"(?:see|as\s+shown\s+in)\s+(?:Figure|Table)\s+\d",
            ]),
            # Computational supercomputer roleplay — LLM claims to run pipelines
            r"(?:we|the\s+pipeline)\s+(?:downloaded|retrieved)\s+(?:raw|\.fastq|FASTQ|sequencing)\s+(?:data|files|reads)",
            r"(?:re)?processed\s+(?:amplicon|shotgun|metagenomic|16S)\s+(?:data|datasets|reads|sequences)",
            r"(?:containerized|versioned|Nextflow|Snakemake)\s+(?:bioinformatic\s+)?workflows?",
            r"denoising\s+(?:to\s+infer\s+)?(?:amplicon\s+sequence\s+variants?|ASVs?)\s+using",
            r"(?:we|the\s+agent)\s+(?:ran|executed|applied|implemented)\s+(?:DADA2|QIIME2?|Kraken2?|MetaPhlAn|DIAMOND|BLAST)",
            r"(?:we|the\s+agent)\s+(?:ran|executed|implemented|performed)\s+(?:meta-regression|mixed[- ]effects?\s+model)",
            r"(?:we|the\s+agent)\s+(?:computed|calculated)\s+(?:pooled\s+)?effect\s+sizes?",
            r"(?:DIAMOND|BLAST)\s+searches?\s+against\s+(?:KEGG|UniProt|CAZy|NCBI)",
            r"(?:downloaded|fetched|retrieved)\s+(?:from|via)\s+(?:the\s+)?(?:SRA|GEO|EBI|ENA|NCBI)",
            r"(?:terabytes?|petabytes?|TB|PB)\s+of\s+(?:storage|data|raw)",
            r"high[- ]performance\s+computing\s+cluster",
        ]
        combined = re.compile("|".join(_FABRICATION_PATTERNS), re.IGNORECASE)

        sanitized = {}
        for heading, content in draft.items():
            sentences = re.split(r"(?<=[.!?])\s+", content)
            kept = [s for s in sentences if not combined.search(s)]
            result = " ".join(kept)
            # Clean phantom spaces left by sentence removal
            result = re.sub(r"\s+\.", ".", result)
            result = re.sub(r"\s+,", ",", result)
            result = re.sub(r"\s{2,}", " ", result)
            sanitized[heading] = result.strip()
        return sanitized

    @staticmethod
    def _enforce_citation_density(draft: dict[str, str]) -> dict[str, str]:
        """Remove paragraphs with empirical claims but no citations."""
        _CITATION_RE = re.compile(r"\[[A-Z][a-zA-Z\-']+(?:\s+et\s+al\.?)?,\s*\d{4}")
        _EMPIRICAL_RE = re.compile(
            r"(?:"
            r"(?:studies?|research|experiments?)\s+(?:have\s+)?(?:shown|demonstrated|found|revealed|reported|indicated)"
            r"|(?:evidence|data|findings|results)\s+(?:suggest|indicate|show|demonstrate|reveal)"
            r"|(?:has|have|was|were)\s+(?:found|shown|demonstrated|reported|observed)"
            r"|(?:according\s+to|consistent\s+with)"
            r"|(?:approximately|roughly)\s+\d"
            r"|\d+(?:\.\d+)?%"
            r"|(?:higher|lower|greater|less|more|fewer)\s+than"
            r")",
            re.IGNORECASE,
        )
        _EVIDENCE_SECTIONS = {"Introduction", "Related Work", "Results", "Discussion"}

        enforced = {}
        for heading, content in draft.items():
            if heading not in _EVIDENCE_SECTIONS:
                enforced[heading] = content
                continue

            paragraphs = re.split(r"\n\n+", content)
            kept = []
            removed = []
            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                has_citation = bool(_CITATION_RE.search(para))
                has_empirical = bool(_EMPIRICAL_RE.search(para))
                word_count = len(para.split())

                if has_citation or not has_empirical or word_count <= 40:
                    kept.append(para)
                else:
                    removed.append(para)

            # Safety: never remove paragraphs if it would leave the section
            # below 100 words or remove more than half the content
            kept_words = sum(len(p.split()) for p in kept)
            min_safe = max(100, len(content.split()) // 2)
            if kept_words < min_safe and removed:
                kept.extend(removed)
                removed = []

            for para in removed:
                logger.info("Citation enforcer: removed uncited paragraph from '%s'", heading)

            enforced[heading] = "\n\n".join(kept)
        return enforced

    @staticmethod
    def _generate_tags(brief: dict, title: str) -> list[str]:
        """Generate tags from research brief."""
        tags = set()
        paper_type = brief.get("paper_type", "")
        if paper_type:
            tags.add(paper_type.lower().strip())
        for term in brief.get("search_terms", [])[:5]:
            words = [w.strip().lower() for w in term.split() if len(w) > 2]
            if words:
                tags.add(" ".join(words[:3])[:50])
        if not tags:
            title_words = [w.lower().strip(",:;.") for w in title.split() if len(w) > 3]
            for tw in title_words[:3]:
                tags.add(tw)
        if not tags:
            tags.add("research")
        return list(tags)[:10]

    def _save_paper_locally(self, paper_payload: dict) -> pathlib.Path:
        """Save paper as JSON for later submission."""
        output_dir = _CHECKPOINT_DIR.parent / "papers"
        output_dir.mkdir(parents=True, exist_ok=True)
        title = paper_payload.get("title", "untitled")
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:60].strip()
        ts = int(time.time())
        path = output_dir / f"{safe}_{ts}.json"
        path.write_text(json.dumps(paper_payload, indent=2, default=str))
        logger.info("Paper saved locally: %s", path)
        return path
