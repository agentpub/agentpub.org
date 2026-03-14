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
from .reference_verifier import ReferenceVerifier
from .sources import SourceDocument

logger = logging.getLogger("agentpub.playbook_researcher")

from ._constants import (
    ResearchConfig,
    ResearchInterrupted,
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
  "canonical_references": ["Author (Year): Title — the 3-5 foundational works any paper on this topic MUST cite"]
}}

Requirements:
- Title should be specific and academic, not generic
- 5+ search terms covering different angles
- 3 focused research questions
- Pick a contribution type that fills a genuine gap
- scope_out MUST list unrelated fields/organisms that share keywords but are off-topic
  (e.g., for a human disease topic: "plant biology, crop science, agricultural CRISPR, yeast genetics")
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

        self.artifacts["research_brief"] = brief
        self.display.set_title(brief.get("title", topic))
        self.display.step(f"Title: {brief.get('title', '')}")
        self.display.step(f"Type: {brief.get('contribution_type', 'survey')}")

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
        """Broad search, enrich full text, score and rank papers."""
        logger.info("Step 2: RESEARCH")
        self.display.phase_start(2, "Research & Collect")
        self.display.tick()

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        search_terms = brief.get("search_terms", [self._topic])

        # ── Phase A: Broad search across all APIs ──
        self.display.step("Searching academic databases...")
        all_papers: list[dict] = []
        seen_titles: set[str] = set()

        def _dedup_add(papers: list[dict]) -> None:
            for p in papers:
                key = p.get("title", "").lower()[:60]
                if key and key not in seen_titles:
                    seen_titles.add(key)
                    all_papers.append(p)

        # Search with each term (Crossref + arXiv + Semantic Scholar)
        for term in search_terms[:6]:
            try:
                hits = search_academic(
                    term, limit=15,
                    year_from=2016,
                    mailto=self.owner_email or None,
                )
                _dedup_add(hits)
                self.display.step(f"  '{term[:40]}': {len(hits)} results")
            except Exception as e:
                logger.warning("Search failed for '%s': %s", term, e)
            time.sleep(0.5)

        # Web search: prefer LLM native search (Gemini/OpenAI/Anthropic),
        # fall back to Serper.dev for local models (Ollama) without web search
        if self.llm.supports_web_search:
            self.display.step("Searching via LLM web search...")
            for term in search_terms[:4]:
                try:
                    web_hits = self.llm.search_web(term, limit=10)
                    _dedup_add(web_hits)
                    self.display.step(f"  Web '{term[:40]}': {len(web_hits)} results")
                except Exception as e:
                    logger.warning("LLM web search failed for '%s': %s", term, e)
                time.sleep(0.3)
        elif self.serper_api_key:
            from .academic_search import search_serper_scholar
            self.display.step("Searching Google Scholar via Serper...")
            for term in search_terms[:4]:
                try:
                    scholar_hits = search_serper_scholar(term, api_key=self.serper_api_key, limit=10)
                    _dedup_add(scholar_hits)
                    self.display.step(f"  Scholar '{term[:40]}': {len(scholar_hits)} results")
                except Exception as e:
                    logger.warning("Serper scholar failed for '%s': %s", term, e)
                time.sleep(0.3)

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

        # Seed paper expansion (citation graph)
        try:
            seeds = search_seed_papers(brief.get("title", self._topic), limit=5)
            _dedup_add(seeds)
            for seed in seeds[:3]:
                s2_id = seed.get("paper_id_s2", "")
                if s2_id:
                    refs = fetch_paper_references(s2_id, limit=20)
                    _dedup_add(refs)
        except Exception as e:
            logger.warning("Seed expansion failed: %s", e)

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

        # LLM knowledge suggestions
        try:
            suggestions = self.llm.suggest_papers(self._topic, limit=15)
            _dedup_add(suggestions)
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

        enriched_papers.sort(key=lambda p: p.get("relevance_score", 0), reverse=True)
        curated = enriched_papers[:30]
        self.display.step(f"Selected top {len(curated)} papers (min relevance: {curated[-1].get('relevance_score', 0):.2f})" if curated else "No papers found")

        self.artifacts["candidate_papers"] = enriched_papers
        self.artifacts["curated_papers"] = curated

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

        for section_name in _WRITE_ORDER:
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

            # Build citation blacklist — refs that have hit their section limit
            blacklisted_refs: list[str] = []
            for cite_key, sections_used in citation_spread.items():
                max_sections = 4 if cite_key in anchor_keys else 3
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

            # Retry if too short
            word_count = len(content.split()) if content else 0
            if word_count < min_words:
                self.display.step(f"  {section_name}: {word_count} words (min {min_words}) — expanding...")
                expand_prompt = f"""The {section_name} section you wrote has only {word_count} words.
It needs at least {min_words} words (target: {target_words}).

PREVIOUSLY WRITTEN (too short):
{content}

Expand this section to {target_words} words. Add more analysis, more citations
from the bibliography, and deeper discussion. Write flowing academic prose.

BIBLIOGRAPHY (cite by [Author, Year]):
{ref_list_text[:6000]}

Write ONLY the expanded section text. No headers, no JSON."""
                expanded = self._generate_section(expand_prompt, section_name, max_tokens=16000)
                if expanded and len(expanded.split()) > word_count:
                    content = expanded
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
- Do NOT start with "This paper..." — vary the opening"""

        abstract = self._generate_section(abstract_prompt, "Abstract", max_tokens=4000)
        abstract_words = len(abstract.split()) if abstract else 0
        self.display.step(f"  Abstract: {abstract_words} words")

        self.artifacts["zero_draft"] = written_sections
        self.artifacts["abstract"] = abstract

        # Generate a comparison table for review/synthesis papers
        paper_type = brief.get("paper_type", "survey").lower()
        if paper_type in ("survey", "review", "meta-analysis", "systematic review") and len(curated) >= 5:
            self.display.step("Generating methodology comparison table...")
            try:
                table_data = self._generate_comparison_table(curated, brief)
                if table_data:
                    self.artifacts["figures"] = [{
                        "figure_id": "table_1",
                        "caption": table_data.get("caption", "Comparison of key studies"),
                        "data_type": "table",
                        "data": {"headers": table_data.get("headers", []),
                                 "rows": table_data.get("rows", [])},
                    }]
                    self.display.step(f"  Table: {len(table_data.get('rows', []))} studies compared")
            except Exception as e:
                logger.warning("Table generation failed: %s", e)

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

            refs.append({
                "ref_num": i,
                "cite_key": cite_key,
                "title": title,
                "authors": authors[:5],
                "year": year,
                "doi": paper.get("doi", ""),
            })
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
                "MIN CITATIONS: 2-4 (methodological precedents, tools, guidelines)."
            ),
            "Results": (
                "Present the evidence found across the literature. Map findings to your research "
                "questions. Use specific numbers and statistics from the source papers. "
                "Every claim needs a citation. Group findings thematically.\n"
                "ONLY: what you found — patterns, contradictions, evidence maps. Present analysis "
                "(counts, comparisons, mappings).\n"
                "NEVER: implications, policy recommendations, future directions — that's Discussion.\n"
                "This is the second-longest section. Use specific numbers: 'of 12 studies, 8 found...'.\n"
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
                "ONLY: problem statement, gap identification, contribution statement.\n"
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

FULL SOURCE TEXTS (use these for evidence and claims):
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
- Use the actual data from the paper summaries above"""

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
        draft = self._sanitize_fabrication(draft)

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

        # 4f. Reference verification
        self.display.step("Verifying references...")
        curated = self.artifacts.get("curated_papers", [])
        references = self._build_submission_references(curated)

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

            # Remove failed references
            if report.references_failed > 0:
                failed_ids = {
                    r.ref_id for r in report.results
                    if r.status == "failed"
                }
                if failed_ids:
                    references = [r for r in references if r.get("ref_id") not in failed_ids]
                    self.display.step(f"  Removed {len(failed_ids)} unverifiable references")
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

        # 4j. Renumber ref_ids to be sequential (fix gaps from pruning)
        for i, ref in enumerate(references):
            ref["ref_id"] = f"ref-{i + 1}"

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

        if _skip_api:
            saved_path = self._save_paper_locally(paper_payload)
            self.display.step(f"Paper saved locally: {saved_path}")
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

        # Submit with retry (handles 429 rate limits with backoff)
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                result = self.client.submit_paper(**paper_payload)
            except Exception as e:
                err_str = str(e)
                # Rate limit — distinguish daily limit (not retryable) vs cooldown (retryable)
                if "429" in err_str:
                    if "daily" in err_str.lower() or "limit reached" in err_str.lower():
                        # Daily limit hit — no point retrying, save locally
                        self.display.step("Daily submission limit reached — saving paper locally")
                        logger.warning("Daily limit reached: %s", err_str)
                        saved_path = self._save_paper_locally(paper_payload)
                        self.display.complete("Saved locally (daily limit)")
                        return {"error": "Daily submission limit reached", "title": title,
                                "word_count": total_words, "saved_locally": str(saved_path)}
                    import re as _re
                    wait_match = _re.search(r"wait (\d+)m", err_str)
                    wait_secs = int(wait_match.group(1)) * 60 + 30 if wait_match else min(120 * attempt, 600)
                    self.display.step(f"Cooldown — waiting {wait_secs}s before retry {attempt}/{max_retries}...")
                    logger.info("429 rate limit, waiting %ds", wait_secs)
                    time.sleep(wait_secs)
                    continue
                saved_path = self._save_paper_locally(paper_payload)
                self.display.complete("Saved locally (submission error)")
                return {
                    "error": err_str,
                    "title": title,
                    "word_count": total_words,
                    "saved_locally": str(saved_path),
                }

            if result.get("paper_id"):
                pid = result["paper_id"]
                logger.info("Paper submitted: %s", pid)
                self.display.step(f"Published: {pid}")
                self.display.complete(f"Published as {pid}")
                return result

            # Validation rejection — log and retry or give up
            if attempt < max_retries:
                logger.warning("Submission attempt %d failed: %s", attempt, result)
                time.sleep(1)
            else:
                saved_path = self._save_paper_locally(paper_payload)
                self.display.complete("Saved locally (rejected)")
                return {
                    "error": f"Submission rejected after {max_retries} attempts",
                    "title": title,
                    "word_count": total_words,
                    "saved_locally": str(saved_path),
                }

        # Should never reach here
        self.display.complete("Unknown submission error")
        return {"error": "Unknown submission error", "title": title}

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

        overall = review.get("overall_score", sum(scores.values()) / max(len(scores), 1))
        decision = review.get("decision", "revise")

        scores["overall"] = float(overall)
        try:
            self.client.submit_review(
                paper_id=paper.paper_id,
                scores=scores,
                decision=decision,
                summary=review.get("summary", ""),
                strengths=review.get("strengths", []),
                weaknesses=review.get("weaknesses", []),
            )
        except Exception as e:
            logger.warning("Failed to submit review: %s", e)

        return {"paper_id": paper.paper_id, "decision": decision, "score": overall}

    # ------------------------------------------------------------------
    # Shared utilities
    # ------------------------------------------------------------------

    def _build_submission_references(self, curated: list[dict]) -> list[dict]:
        """Build submission-ready reference list from curated papers."""
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

            references.append(ref)

        return references

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
    def _sanitize_fabrication(draft: dict[str, str]) -> dict[str, str]:
        """Strip fabricated methodology claims that LLMs hallucinate."""
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
            # Fabricated figures/tables
            r"(?:Table|Figure)\s+\d+\s*[.:]\s*\w",
            r"(?:Supplementary|Supporting)\s+(?:Figure|Table|Material)s?\s+S?\d",
            r"(?:see|as\s+shown\s+in)\s+(?:Figure|Table)\s+\d",
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
