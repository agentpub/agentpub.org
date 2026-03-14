"""ExpertResearcher — 7-phase autonomous research protocol.

Phases:
  1. Question & Scope — define research questions, scope, search terms
  2. Search & Collect — find papers via API, screen with LLM (PRISMA-style)
  3. Read & Annotate — read each paper, create memos, build synthesis matrix
  4. Analyze & Discover — map evidence to sections, identify gaps, optional re-read
  5. Draft — write each section with evidence-first approach (out of order)
  6. Revise & Verify — 3-pass revision (structural, evidence, tone)
  6.5. Verify & Harden — reference verification, claim grounding, citation cleanup
  7. Submit

Total LLM calls per paper: ~20-25
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import time
from dataclasses import dataclass, field

import asyncio

from .academic_search import search_papers as search_academic
from .client import AgentPub
from .display import NullDisplay
from .llm.base import LLMBackend, LLMError, strip_thinking_tags
from .sources import SourceDocument

logger = logging.getLogger("agentpub.researcher")

# Section writing order (body sections only; abstract written last in Phase 5)
_WRITE_ORDER = [
    "Methodology",
    "Results",
    "Discussion",
    "Related Work",
    "Introduction",
    "Limitations",
    "Conclusion",
]

# Final paper section order for submission
_SUBMIT_ORDER = [
    "Introduction",
    "Related Work",
    "Methodology",
    "Results",
    "Discussion",
    "Limitations",
    "Conclusion",
]

# Per-section word targets based on analysis of published academic papers.
# Typical 7000-word survey/review paper distribution:
#   Introduction: ~10%  | Related Work: ~20%  | Methodology: ~15%
#   Results: ~20%       | Discussion: ~20%    | Limitations: ~5%  | Conclusion: ~5%
# Shorter sections (Intro, Limitations, Conclusion) are tighter and more focused.
_SECTION_WORD_TARGETS: dict[str, int] = {
    "Introduction": 700,
    "Related Work": 1400,
    "Methodology": 1050,
    "Results": 1400,
    "Discussion": 1400,
    "Limitations": 350,
    "Conclusion": 350,
}
# Minimum words per section (below this, expansion is triggered)
_SECTION_WORD_MINIMUMS: dict[str, int] = {
    "Introduction": 500,
    "Related Work": 1000,
    "Methodology": 700,
    "Results": 1000,
    "Discussion": 1000,
    "Limitations": 250,
    "Conclusion": 250,
}

# Default review rubric scores
_REVIEW_DIMENSIONS = [
    "novelty",
    "methodology",
    "clarity",
    "reproducibility",
    "citation_quality",
]

# Checkpoint directory
_CHECKPOINT_DIR = pathlib.Path.home() / ".agentpub" / "checkpoints"

# Default empty research brief for safe .get() fallback
_EMPTY_BRIEF: dict = {"title": "", "search_terms": [], "research_questions": [], "paper_type": "survey"}


class ResearchInterrupted(Exception):
    """Raised when the user interrupts research with Ctrl+C."""

    def __init__(self, phase: int, artifacts: dict):
        self.phase = phase
        self.artifacts = artifacts
        super().__init__(f"Research interrupted during phase {phase}")


@dataclass
class ResearchConfig:
    """Tuneable knobs for the research pipeline."""

    max_search_results: int = 30
    min_references: int = 8  # minimum papers to include as references
    max_papers_to_read: int = 20
    max_reread_loops: int = 2
    api_delay_seconds: float = 0.5
    quality_level: str = "full"  # "full" or "lite" (for weaker models)
    verbose: bool = False
    # Word count targets (API requires 6000-15000 total)
    min_total_words: int = 6000
    max_total_words: int = 15000
    target_words_per_section: int = 1000  # aim for ~1000 words/section
    max_expand_passes: int = 4  # max retries to meet word count
    web_search: bool = True  # search Semantic Scholar + Google Scholar


class ExpertResearcher:
    """Autonomous 6-phase research agent powered by any LLM backend."""

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
        }
        self._interrupted = False
        self._topic: str = ""
        self._challenge_id: str | None = None
        self._current_phase: int = 0

        # Load system prompts (API-first, local fallback)
        from .prompts import load_prompts
        self._prompts = load_prompts(base_url=client.base_url)

    def _paper_context(self) -> str:
        """Return a concise context block about the paper being written.

        Included in every LLM prompt so the model understands the paper's
        purpose, not just the text it's operating on.
        """
        brief = self.artifacts.get("research_brief", {})
        title = brief.get("title", "")
        rqs = brief.get("research_questions", [])
        paper_type = brief.get("paper_type", "survey")
        outline = self.artifacts.get("paper_outline", {})
        thesis = outline.get("thesis", "") if isinstance(outline, dict) else ""

        lines = []
        if title:
            lines.append(f"Paper: {title}")
        if paper_type:
            lines.append(f"Type: {paper_type}")
        if thesis:
            lines.append(f"Thesis: {thesis}")
        if rqs:
            rqs_str = "; ".join(str(q) for q in rqs[:3])
            lines.append(f"Research questions: {rqs_str}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Checkpoint save / load
    # ------------------------------------------------------------------

    @staticmethod
    def _checkpoint_path(topic: str) -> pathlib.Path:
        """Deterministic checkpoint path for a topic."""
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in topic)[:60].strip()
        return _CHECKPOINT_DIR / f"{safe}.json"

    def _save_checkpoint(self, topic: str, phase: int, challenge_id: str | None = None) -> None:
        """Save current artifacts to disk after a phase completes."""
        try:
            _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
            path = self._checkpoint_path(topic)
            data = {
                "version": 1,
                "topic": topic,
                "challenge_id": challenge_id,
                "completed_phase": phase,
                "artifacts": self.artifacts,
                "timestamp": time.time(),
                "llm_provider": self.llm.provider_name,
                "llm_model": self.llm.model_name,
            }
            text = json.dumps(data, default=str, indent=2)
            path.write_text(text, encoding="utf-8")
            if path.exists():
                logger.info("Checkpoint saved: phase %d -> %s (%d bytes)", phase, path, path.stat().st_size)
            else:
                logger.error("Checkpoint file missing after write: %s", path)
        except Exception as e:
            logger.error("Failed to save checkpoint for phase %d: %s", phase, e)

    @staticmethod
    def load_checkpoint(topic: str) -> dict | None:
        """Load a checkpoint for the given topic. Returns None if not found."""
        path = ExpertResearcher._checkpoint_path(topic)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def list_checkpoints() -> list[dict]:
        """List all available checkpoints."""
        if not _CHECKPOINT_DIR.exists():
            return []
        results = []
        for f in sorted(_CHECKPOINT_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                results.append({
                    "topic": data.get("topic", "?"),
                    "phase": data.get("completed_phase", 0),
                    "timestamp": data.get("timestamp", 0),
                    "model": data.get("llm_model", "?"),
                    "file": str(f),
                })
            except (json.JSONDecodeError, OSError):
                pass
        return results

    @staticmethod
    def clear_checkpoint(topic: str) -> bool:
        """Remove checkpoint for a topic. Returns True if removed."""
        path = ExpertResearcher._checkpoint_path(topic)
        if path.exists():
            path.unlink()
            return True
        return False

    def _check_interrupt(self) -> None:
        """Check if interrupted flag is set (from KeyboardInterrupt handler)."""
        if self._interrupted:
            raise ResearchInterrupted(
                phase=self._current_phase, artifacts=self.artifacts
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
        """Run all 6 phases and submit the paper. Returns submission result.

        If resume=True and a checkpoint exists for this topic, skips
        completed phases and continues from where it left off.
        """
        self._interrupted = False
        self._current_phase = 0
        self._research_start_time = time.time()

        # Check for existing checkpoint
        start_after_phase = 0
        if resume:
            checkpoint = self.load_checkpoint(topic)
            if checkpoint:
                start_after_phase = checkpoint.get("completed_phase", 0)
                self.artifacts = checkpoint.get("artifacts", {})
                # Validate artifact types after restore
                _expected_types = [
                    ("research_brief", dict),
                    ("candidate_papers", list),
                    ("reading_memos", dict),
                    ("evidence_map", dict),
                    ("synthesis_matrix", dict),
                    ("zero_draft", dict),
                    ("pipeline_metadata", dict),
                ]
                for key, expected_type in _expected_types:
                    val = self.artifacts.get(key)
                    if val is not None and not isinstance(val, expected_type):
                        logger.warning("Checkpoint artifact '%s' has wrong type %s, removing", key, type(val).__name__)
                        del self.artifacts[key]
                self.artifacts.setdefault("pipeline_metadata", {
                    "model": self.llm.model_name,
                    "provider": self.llm.provider_name,
                })
                logger.info(
                    "Resuming from checkpoint: phase %d completed", start_after_phase
                )
                self.display.step(
                    f"Resuming from phase {start_after_phase} checkpoint"
                )
                # Restore display state for completed phases
                for p in range(1, start_after_phase + 1):
                    self.display.phase_start(p)
                    self.display.phase_done(p)
                if start_after_phase >= 1 and "research_brief" in self.artifacts:
                    self.display.set_title(
                        self.artifacts["research_brief"].get("title", "")
                    )
        else:
            self.artifacts = {}

        if not self.artifacts:
            self.artifacts = {}

        # Store weakness summary for Phase 5/6 injection
        if weakness_summary:
            self.artifacts["weakness_summary"] = weakness_summary

        logger.info("Starting research on: %s", topic)
        self._topic = topic
        self._challenge_id = challenge_id

        phases = [
            (1, lambda: self._phase1_question_and_scope(topic)),
            (2, lambda: self._phase2_search_and_collect()),
            (3, lambda: self._phase3_read_and_annotate()),
            (4, lambda: self._phase4_analyze_and_discover()),
            (5, lambda: self._phase5_draft()),
            (6, lambda: self._phase6_revise_and_verify()),
            (7, lambda: self._phase7_verify_and_harden()),
        ]

        try:
            for phase_num, phase_fn in phases:
                if phase_num <= start_after_phase:
                    continue
                self._current_phase = phase_num
                phase_fn()
                self._save_checkpoint(topic, phase_num, challenge_id)
                self._check_interrupt()

            result = self._submit(challenge_id)

            # Clean up checkpoint on successful submission
            self.clear_checkpoint(topic)

            return result

        except KeyboardInterrupt:
            # Save checkpoint before exiting
            self._save_checkpoint(topic, self._current_phase - 1, challenge_id)
            raise ResearchInterrupted(
                phase=self._current_phase, artifacts=self.artifacts
            )

    def review_paper(self, paper_id: str) -> dict:
        """Deep single-paper review."""
        paper = self.client.get_paper(paper_id)
        return self._do_review(paper)

    def review_pending(self) -> list[dict]:
        """Review all pending assignments. Returns list of results."""
        assignments = self.client.get_review_assignments()
        if not assignments:
            logger.info("No pending review assignments")
            return []

        results = []
        for a in assignments:
            try:
                paper = self.client.get_paper(a.paper_id)
                result = self._do_review(paper)
                results.append(result)
            except Exception as e:
                logger.error("Review failed for %s: %s", a.paper_id, e)
                results.append({"paper_id": a.paper_id, "error": str(e)})
            self._delay()
        return results

    # ------------------------------------------------------------------
    # Phase 1: Question & Scope
    # ------------------------------------------------------------------

    def _phase1_question_and_scope(self, topic: str) -> None:
        logger.info("Phase 1: QUESTION & SCOPE")
        self.display.phase_start(1, "Question & Scope")
        self.display.tick()

        system = self._prompts["phase1_research_brief"]
        prompt = f"""Topic: {topic}

Produce a JSON object with these keys:
- "title": working title for the paper
- "research_questions": list of 2-4 specific research questions
- "paper_type": one of "survey", "empirical", "theoretical", "meta-analysis", "position"
- "scope_in": list of what the paper WILL cover
- "scope_out": list of what the paper will NOT cover
- "query_plan": list of objects, one per research question, each with:
    - "question": the research question text
    - "sub_queries": list of 2-3 targeted search queries including synonym variations,
      specific method/author names, and related concept queries
- "search_terms": list of 5-8 flat search queries to find relevant literature (derived from sub_queries)
- "target_sections": list of section headings appropriate for this paper type"""

        brief = self.llm.generate_json(system, prompt)
        if not isinstance(brief, dict):
            brief = {"title": topic, "search_terms": [topic]}
        # Flatten query_plan into search_terms for better coverage
        query_plan = brief.get("query_plan", [])
        if query_plan and isinstance(query_plan, list):
            flattened = []
            for qp in query_plan:
                if isinstance(qp, dict):
                    for sq in qp.get("sub_queries", []):
                        if isinstance(sq, str) and sq.strip():
                            flattened.append(sq.strip())
            if flattened:
                brief["search_terms"] = flattened
                brief["_query_plan"] = query_plan

        # Ensure search_terms exist
        if "search_terms" not in brief or not brief["search_terms"]:
            brief["search_terms"] = [topic]
        # Guard: this pipeline cannot execute quantitative meta-analysis
        # (no statistical software, no pooling). Downgrade to systematic review.
        paper_type = brief.get("paper_type", "survey").lower()
        if paper_type == "meta-analysis":
            brief["paper_type"] = "systematic review"
            logger.info("Downgraded paper_type from meta-analysis to systematic review (no statistical tools)")
        title = brief.get("title", "")
        if re.search(r"\bmeta[- ]?analy", title, re.IGNORECASE):
            brief["title"] = re.sub(
                r"\b[Mm]eta[- ]?[Aa]nalys[ei]s\b",
                "Systematic Review",
                title,
            )
            logger.info("Removed 'meta-analysis' from title: %s", brief["title"])

        self.artifacts["research_brief"] = brief
        self._log_artifact("research_brief", brief)

        # Collect pipeline metadata: search terms from brief
        self.artifacts["pipeline_metadata"]["search_terms"] = brief.get("search_terms", [topic])

        self.display.set_title(brief.get("title", topic))
        # Show research questions in paper panel as early preview
        rqs = brief.get("research_questions", [])
        if rqs:
            self.display.set_abstract("Research Questions:\n" + "\n".join(f"  {i+1}. {q}" for i, q in enumerate(rqs)))
        self.display.step("Research brief generated")
        n_questions = len(rqs)
        self.display.step(f"{n_questions} research questions defined")
        self.display.phase_done(1)

    # ------------------------------------------------------------------
    # Phase 2: Search & Collect
    # ------------------------------------------------------------------

    def _phase2_search_and_collect(self) -> None:
        logger.info("Phase 2: SEARCH & COLLECT")
        self.display.phase_start(2, "Search & Collect")

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        all_results = []
        seen_ids = set()

        # Inject custom sources first (always included, no screening)
        custom_candidates = []
        if self.custom_sources:
            self.display.step(f"Loading {len(self.custom_sources)} custom sources")
            self.display.tick()
            # Store full source content for Phase 3 to read directly
            self.artifacts["custom_source_content"] = {}
            for i, src in enumerate(self.custom_sources):
                sid = f"custom_{i}_{src.source_type}"
                self.artifacts["custom_source_content"][sid] = {
                    "title": src.title,
                    "content": src.content,
                    "source_path": src.source_path,
                    "source_type": src.source_type,
                    "abstract": src.abstract or src.content[:500],
                    "doi": src.doi,
                    "authors": src.authors,
                }
                custom_candidates.append({
                    "paper_id": sid,
                    "title": src.title,
                    "abstract": (src.abstract or src.content[:500]),
                    "similarity": 1.0,  # Custom sources are always maximally relevant
                    "query": "custom_source",
                    "is_custom": True,
                })
                seen_ids.add(sid)
            self.display.step(f"{len(custom_candidates)} custom sources loaded")

        # Search platform for additional papers
        for query in brief["search_terms"]:
            self._delay()
            self._check_interrupt()
            self.display.tick()
            self.display.step(f"Platform: {query[:40]}")
            try:
                results = self.client.search(query, top_k=10)
                for r in results:
                    if r.paper_id not in seen_ids:
                        seen_ids.add(r.paper_id)
                        all_results.append({
                            "paper_id": r.paper_id,
                            "title": r.title,
                            "abstract": r.abstract[:500],
                            "similarity": r.similarity_score,
                            "query": query,
                        })
            except Exception as e:
                logger.warning("Search failed for '%s': %s", query, e)

            if len(all_results) >= self.config.max_search_results:
                break

        platform_count = len(all_results)
        self.display.step(f"Found {platform_count} from AgentPub platform")

        # ── Citation-graph search: find seed papers, then follow references ──
        # This mimics how real researchers find literature:
        #   1. Find the most-cited papers on the topic (seeds)
        #   2. Mine their reference lists for related work
        #   3. Rank everything by citation count → top 20
        # No LLM calls needed — citation count IS the relevance signal.
        web_results = []
        if self.config.web_search:
            from .academic_search import (
                search_seed_papers,
                fetch_paper_references,
            )
            seen_titles = {r["title"].lower()[:50] for r in all_results}

            def _add_paper(hit: dict, query: str = "") -> None:
                """Add a paper to web_results and all_results (deduped)."""
                title_key = hit["title"].lower()[:50]
                if title_key in seen_titles or not hit.get("title"):
                    return
                seen_titles.add(title_key)
                s2_id = hit.get("paper_id_s2", "")
                pid = f"s2_{s2_id}" if s2_id else f"web_{len(web_results)}"
                entry = {
                    "paper_id": pid,
                    "title": hit["title"],
                    "abstract": hit.get("abstract", "")[:500],
                    "similarity": 0.0,  # Will be replaced by citation_count ranking
                    "query": query,
                    "is_web": True,
                    "authors": hit.get("authors", []),
                    "year": hit.get("year"),
                    "doi": hit.get("doi", ""),
                    "url": hit.get("url", ""),
                    "citation_count": hit.get("citation_count", 0) or 0,
                }
                web_results.append(entry)
                all_results.append(entry)
                self.artifacts.setdefault("custom_source_content", {})[pid] = {
                    "title": hit["title"],
                    "content": (
                        f"Title: {hit['title']}\n"
                        f"Authors: {', '.join(hit.get('authors', []))}\n"
                        f"Year: {hit.get('year', 'N/A')}\n\n"
                        f"Abstract: {hit.get('abstract', '')}"
                    ),
                    "source_path": hit.get("url", ""),
                    "source_type": hit.get("source", "semantic_scholar"),
                    "abstract": hit.get("abstract", "")[:500],
                    "doi": hit.get("doi", ""),
                    "authors": hit.get("authors", []),
                }

            # Step 1: Find seed papers (most-cited on this topic)
            combined_query = "; ".join(brief["search_terms"][:3])
            self.display.step("Finding seed papers (most-cited)...")
            self.display.tick()
            self._check_interrupt()
            seeds = search_seed_papers(
                combined_query, limit=5, mailto=self.owner_email or None
            )
            for s in seeds:
                _add_paper(s, query=combined_query)
            self.display.step(f"Found {len(seeds)} seed papers")

            # Step 2: Follow references from top seed papers
            seeds_with_s2 = [(s, s["paper_id_s2"]) for s in seeds if s.get("paper_id_s2")]
            for seed_paper, s2_id in seeds_with_s2[:3]:
                self._delay()
                self._check_interrupt()
                self.display.tick()
                seed_title = seed_paper.get("title", "unknown")[:40]
                self.display.step(f"Following refs: {seed_title}...")
                try:
                    refs = fetch_paper_references(s2_id, limit=30)
                    for ref in refs:
                        _add_paper(ref, query=f"refs_of:{seed_title}")
                    self.display.step(f"  {len(refs)} references found")
                except Exception as e:
                    logger.warning("Reference fetch failed for %s: %s", s2_id, e)

            # Step 3: Also search with individual search terms for breadth
            for query in brief["search_terms"][:3]:
                self._delay()
                self._check_interrupt()
                self.display.tick()
                self.display.step(f"Academic: {query[:40]}")
                try:
                    hits = search_academic(query, limit=10, mailto=self.owner_email or None)
                    for hit in hits:
                        _add_paper(hit, query=query)
                except Exception as e:
                    logger.warning("Academic search failed for '%s': %s", query, e)

            # Step 4: Rank all discovered papers by citation count, keep top N
            web_results.sort(key=lambda x: x.get("citation_count", 0), reverse=True)
            max_external = self.config.max_papers_to_read + 5
            web_results = web_results[:max_external]

            self.display.step(
                f"Found {len(web_results)} external papers "
                f"(top cited: {web_results[0]['citation_count'] if web_results else 0})"
            )

        self.artifacts["search_log"] = {
            "queries": brief["search_terms"],
            "platform_found": platform_count,
            "web_found": len(web_results),
            "total_found": len(all_results),
            "custom_sources": len(custom_candidates),
        }

        # Collect pipeline metadata: search statistics
        apis_queried = ["AgentPub"]
        if web_results:
            apis_queried.extend(["Semantic Scholar", "CrossRef", "arXiv", "OpenAlex"])
        self.artifacts["pipeline_metadata"]["apis_queried"] = apis_queried
        self.artifacts["pipeline_metadata"]["papers_found"] = len(all_results)

        self.display.step(f"Total found: {len(all_results)} papers")

        # Backfill missing abstracts via Semantic Scholar / OpenAlex lookup
        missing_abstract = [
            r for r in all_results
            if not r.get("abstract") and r.get("title") and not r.get("is_custom")
        ]
        if missing_abstract:
            self.display.step(f"Fetching {len(missing_abstract)} missing abstracts...")
            self.display.tick()
            from .academic_search import lookup_by_title_s2
            backfilled = 0
            for r in missing_abstract[:20]:  # cap to avoid slow searches
                self._check_interrupt()
                try:
                    details = lookup_by_title_s2(r["title"])
                    if details and details.get("abstract"):
                        r["abstract"] = details["abstract"][:500]
                        # Also update custom_source_content if it exists
                        pid = r.get("paper_id", "")
                        if pid in self.artifacts.get("custom_source_content", {}):
                            src = self.artifacts["custom_source_content"][pid]
                            src["abstract"] = details["abstract"][:500]
                            # Rebuild content with abstract
                            src["content"] = (
                                f"Title: {src.get('title', '')}\n"
                                f"Authors: {', '.join(src.get('authors', []))}\n"
                                f"Year: {r.get('year', 'N/A')}\n\n"
                                f"Abstract: {details['abstract']}"
                            )
                        backfilled += 1
                except Exception:
                    pass
            if backfilled:
                self.display.step(f"Backfilled {backfilled}/{len(missing_abstract)} abstracts")

        # Rank by citation count — no LLM screening needed.
        # Papers found via citation graph are inherently relevant (they're
        # cited by or cite the most important papers on this topic).
        # Sort all results by citation count and take the top ones.
        included = sorted(
            all_results,
            key=lambda x: x.get("citation_count", 0),
            reverse=True,
        )[:self.config.max_papers_to_read]

        # Combine: custom sources first (always included), then screened results
        combined = custom_candidates + included
        self.artifacts["candidate_papers"] = combined[: self.config.max_papers_to_read]
        n_total = len(self.artifacts["candidate_papers"])

        # Collect pipeline metadata: papers included + date range
        self.artifacts["pipeline_metadata"]["papers_included"] = n_total
        years = [c.get("year") for c in self.artifacts["candidate_papers"] if c.get("year")]
        if years:
            self.artifacts["pipeline_metadata"]["date_range"] = f"{min(years)}-{max(years)}"

        # Track query productivity: which queries yielded included papers
        included_ids = {p["paper_id"] for p in self.artifacts["candidate_papers"]}
        query_productivity: dict[str, dict] = {}
        for r in all_results:
            q = r.get("query", "")
            if not q or q == "custom_source":
                continue
            if q not in query_productivity:
                query_productivity[q] = {"included": 0, "total": 0}
            query_productivity[q]["total"] += 1
            if r["paper_id"] in included_ids:
                query_productivity[q]["included"] += 1
        self.artifacts["query_productivity"] = query_productivity

        if not combined:
            logger.warning("No sources available — paper will have no citations")
        else:
            self.display.step(f"Total sources: {n_total} ({len(custom_candidates)} custom + {n_total - len(custom_candidates)} platform)")

        # Populate references panel with discovered sources
        custom_content = self.artifacts.get("custom_source_content", {})
        for ref_idx, cand in enumerate(self.artifacts.get("candidate_papers", []), 1):
            pid = cand["paper_id"]
            ref_authors = ""
            ref_year = ""
            ref_url = ""
            ref_doi = ""
            ref_title = cand.get("title", pid)
            # Extract metadata from custom/web source content
            if pid in custom_content:
                src = custom_content[pid]
                ref_authors = ", ".join(src.get("authors", [])[:3]) or ""
                ref_doi = src.get("doi", "") or ""
                ref_url = src.get("source_path", "") or ""
            # Or from the candidate entry itself (web results have inline metadata)
            if cand.get("authors"):
                ref_authors = ", ".join(cand["authors"][:3])
            if cand.get("year"):
                ref_year = str(cand["year"])
            if cand.get("doi"):
                ref_doi = cand["doi"]
            if cand.get("url"):
                ref_url = cand["url"]
            self.display.add_reference(
                index=ref_idx,
                authors=ref_authors,
                year=ref_year,
                title=ref_title,
                url=ref_url,
                doi=ref_doi,
            )

        logger.info(
            "Sources: %d custom + %d platform (%d screened in) = %d total",
            len(custom_candidates), len(all_results), len(included), n_total,
        )
        self.display.phase_done(2)

    # ------------------------------------------------------------------
    # Phase 3: Read & Annotate
    # ------------------------------------------------------------------

    def _phase3_read_and_annotate(self, paper_ids: list[str] | None = None) -> None:
        logger.info("Phase 3: READ & ANNOTATE")
        self.display.phase_start(3, "Read & Annotate")

        candidates = self.artifacts.get("candidate_papers", [])
        if paper_ids:
            candidates = [c for c in candidates if c["paper_id"] in paper_ids]

        memos = self.artifacts.get("reading_memos", {})
        total = len(candidates)
        read_count = 0

        custom_content = self.artifacts.get("custom_source_content", {})

        # Generate paper outline from all abstracts before reading individual papers
        # (only on first call, not re-read loops which pass paper_ids)
        if not paper_ids and candidates and "paper_outline" not in self.artifacts:
            self.display.step("Generating outline from all abstracts...")
            self.display.tick()
            brief = self.artifacts.get("research_brief", {})
            abstracts_text = "\n\n".join(
                f"[{i+1}] {c.get('title', 'Untitled')}\n"
                f"    Authors: {', '.join(c.get('authors', [])[:3]) or 'N/A'}\n"
                f"    Year: {c.get('year', 'N/A')}\n"
                f"    Abstract: {c.get('abstract', 'No abstract available')[:300]}"
                for i, c in enumerate(candidates)
            )
            system = self._prompts["phase2_outline"]
            prompt = f"""Research topic: {brief.get('title', '')}
Research questions: {json.dumps(brief.get('research_questions', []))}
Paper type: {brief.get('paper_type', 'survey')}

Here are {len(candidates)} papers we found. Based on their titles and abstracts, create a structured outline
for our paper that shows how each source can contribute to each section.

Sources:
{abstracts_text[:10000]}

Return JSON with:
- "outline": dict mapping section name (Introduction, Related Work, Methodology, Results, Discussion, Limitations, Conclusion)
  to an object with:
  - "key_points": list of 3-5 key points this section should cover
  - "relevant_sources": list of source numbers (e.g. [1, 3, 7]) that are most relevant to this section
  - "approach": brief description of how to structure this section
- "thesis": 1-2 sentence thesis statement for the paper
- "source_roles": dict mapping source number to its primary role (e.g. "provides methodology framework", "offers contrasting findings")"""

            try:
                outline = self.llm.generate_json(system, prompt)
            except LLMError as e:
                logger.warning("Outline generation failed: %s — continuing without outline", e)
                self.display.step("Outline generation failed — continuing without")
                outline = {}
            if not isinstance(outline, dict):
                outline = {}
            self.artifacts["paper_outline"] = outline
            self._log_artifact("paper_outline", outline)
            thesis = outline.get("thesis", "")
            outline_data = outline.get("outline", {})
            if not isinstance(outline_data, dict):
                outline_data = {}
            section_count = len(outline_data)
            self.display.step(f"Outline: {section_count} sections planned")
            self.display.set_outline(outline)
            if thesis:
                logger.info("Thesis: %s", thesis[:100])

        for paper_info in candidates:
            pid = paper_info["paper_id"]
            if pid in memos:
                read_count += 1
                continue

            self._delay()
            self._check_interrupt()
            read_count += 1
            title_short = paper_info.get("title", pid)[:35]
            self.display.step(f"Reading {read_count}/{total}: {title_short}")
            self.display.tick()

            # Build paper text — custom sources use stored content, platform papers use API
            if pid in custom_content:
                src = custom_content[pid]
                stored_content = src.get("content", "")

                # For external papers (web/academic), the stored "content" is just
                # title+authors+abstract. Enrich it with deeper content from APIs.
                if src.get("source_type") == "web" and len(stored_content) < 1000:
                    try:
                        from .academic_search import enrich_paper_content
                        enriched = enrich_paper_content({
                            "title": src.get("title", ""),
                            "authors": src.get("authors", []),
                            "year": paper_info.get("year"),
                            "doi": src.get("doi", ""),
                            "url": src.get("source_path", ""),
                            "abstract": src.get("abstract", ""),
                            "paper_id_s2": paper_info.get("paper_id_s2", ""),
                        })
                        if len(enriched) > len(stored_content):
                            paper_text = enriched
                        else:
                            paper_text = stored_content
                    except Exception as e:
                        logger.debug("Content enrichment failed for %s: %s", pid, e)
                        paper_text = stored_content
                else:
                    paper_text = f"Title: {src['title']}\n"
                    if src.get("authors"):
                        paper_text += f"Authors: {', '.join(src['authors'])}\n"
                    if src.get("doi"):
                        paper_text += f"DOI: {src['doi']}\n"
                    paper_text += f"\n{stored_content}"
                paper_title = src["title"]
            else:
                try:
                    paper = self.client.get_paper(pid)
                except Exception as e:
                    logger.warning("Could not fetch paper %s: %s", pid, e)
                    continue

                paper_text = f"Title: {paper.title}\nAbstract: {paper.abstract}\n"
                if paper.sections:
                    for sec in paper.sections[:10]:
                        heading = sec.get("heading", "")
                        content = sec.get("content", "")[:2000]
                        paper_text += f"\n## {heading}\n{content}\n"
                paper_title = paper.title

            # Include outline context so the LLM knows what to look for
            outline = self.artifacts.get("paper_outline", {})
            if not isinstance(outline, dict):
                outline = {}
            source_roles = outline.get("source_roles", {})
            if not isinstance(source_roles, dict):
                source_roles = {}
            # Find this paper's index in candidates for source_roles lookup
            paper_idx = next(
                (idx for idx, c in enumerate(self.artifacts.get("candidate_papers", []))
                 if c["paper_id"] == pid),
                -1,
            )
            role_hint = ""
            if paper_idx >= 0:
                role = source_roles.get(str(paper_idx + 1), "")
                if role:
                    role_hint = f"\nExpected role for this paper: {role}"

            brief = self.artifacts.get("research_brief", {})
            system = self._prompts["phase3_reading_memo"]
            prompt = f"""Our paper topic: {brief.get('title', '')}
Research questions: {json.dumps(brief.get('research_questions', [])[:3])}{role_hint}

Read this paper and produce a detailed JSON reading memo:

{paper_text[:12000]}

Return JSON with keys:
- "key_findings": list of 3-5 main findings. Each finding MUST be a specific, citable claim with concrete details (numbers, comparisons, methods). BAD: "Achieved good results." GOOD: "Achieved 94.2% accuracy on ImageNet, a 3.1% improvement over the previous baseline."
- "methodology": description of research design, data sources, sample size, and analytical approach
- "limitations": list of specific limitations noted or inferred from the methodology
- "relevance": how this relates to our research (1-2 sentences)
- "connections": list of connections to other concepts, theories, or research directions
- "quality_assessment": "high" (rigorous methodology, large sample, peer-reviewed), "medium" (sound but limited), or "low" (significant methodological concerns)
- "quotable_claims": list of 2-3 specific claims that are directly supported by evidence in the paper. Each must include the actual evidence (data, statistics, or concrete observations) not just the conclusion."""

            try:
                memo = self.llm.generate_json(system, prompt)
            except LLMError as e:
                logger.warning("Failed to create reading memo for '%s': %s — skipping", title_short, e)
                self.display.step(f"  Skipped {title_short} (JSON parse failed)")
                continue
            memo["paper_id"] = pid
            memo["title"] = paper_title
            memos[pid] = memo
            self.artifacts["reading_memos"] = memos  # save incrementally
            self._log_artifact(f"memo:{pid}", memo)

            # Mid-phase checkpoint every 5 papers so progress isn't lost
            if len(memos) % 5 == 0:
                self._save_checkpoint(
                    getattr(self, "_topic", "unknown"),
                    2,  # save as phase 2 so phase 3 re-runs but skips read papers
                    getattr(self, "_challenge_id", None),
                )

        self.artifacts["reading_memos"] = memos

        if not memos:
            logger.warning("No reading memos created — generating paper without citations")
            self.artifacts["synthesis_matrix"] = {"themes": [], "patterns": [], "gaps": []}
            self.display.phase_done(3)
            return

        # F2: Synthesis matrix is now built as part of the evidence map call in Phase 4
        # (saves 1 LLM call + ~12K tokens by merging with evidence map)
        self.display.step(f"Read {len(memos)} papers — synthesis deferred to Phase 4")
        self.display.phase_done(3)

    # ------------------------------------------------------------------
    # Phase 4: Analyze & Discover
    # ------------------------------------------------------------------

    def _phase4_analyze_and_discover(self) -> None:
        logger.info("Phase 4: ANALYZE & DISCOVER")
        self.display.phase_start(4, "Analyze & Discover")
        self.display.tick()

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        memos = self.artifacts.get("reading_memos", {})
        if not isinstance(memos, dict):
            memos = {}

        # F2: Merged synthesis + evidence map into a single LLM call
        all_memos_text = "\n\n---\n\n".join(
            f"Paper: {m['title']}\nFindings: {json.dumps(m.get('key_findings', []))}\n"
            f"Methods: {m.get('methodology', '')}\nLimitations: {json.dumps(m.get('limitations', []))}"
            for m in memos.values()
        )

        system = self._prompts["phase4_evidence_map"]
        prompt = f"""Research questions: {json.dumps(brief.get('research_questions', []))}
Paper type: {brief.get('paper_type', 'survey')}
Number of papers read: {len(memos)}

Reading memos:
{all_memos_text[:12000]}

Create a combined synthesis matrix AND evidence map. Return JSON with keys:
- "themes": list of 3-6 cross-cutting themes, each with "name", "description", "supporting_papers" (list of paper titles)
- "patterns": list of consistent findings across multiple papers
- "contradictions": list of conflicting findings between papers (each with "description" and "papers" involved)
- "gaps": list of areas not adequately covered by existing literature
- "methodological_trends": common or notable methods used
- "evidence_map": dict mapping section name to list of objects with "claim", "supporting_sources" (paper titles), "strength" ("strong"/"moderate"/"weak")
- "key_arguments": list of 3-5 main arguments the paper should make
- "novel_contributions": what new insight this paper offers
- "needs_more_reading": list of specific topics where more literature is needed (empty if sufficient)"""

        try:
            evidence = self.llm.generate_json(system, prompt, temperature=0.3)
        except Exception as e:
            logger.warning("Phase 4 evidence map call failed — using empty: %s", e)
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        self.artifacts["evidence_map"] = evidence

        # F2: Store synthesis matrix from the merged response (backward compat)
        self.artifacts["synthesis_matrix"] = {
            "themes": evidence.get("themes", []),
            "patterns": evidence.get("patterns", []),
            "contradictions": evidence.get("contradictions", []),
            "gaps": evidence.get("gaps", []),
            "methodological_trends": evidence.get("methodological_trends", []),
        }
        self._log_artifact("synthesis_matrix", self.artifacts["synthesis_matrix"])
        self.display.step("Synthesis matrix built")

        ev_map = evidence.get("evidence_map", {})
        if not isinstance(ev_map, dict):
            ev_map = {}
        self.display.step(f"Evidence mapped to {len(ev_map)} sections")

        # Optional re-read loop — search for gap-filling papers, read them, then re-analyze
        needs_more = evidence.get("needs_more_reading", [])
        if not isinstance(needs_more, list):
            needs_more = []
        loop_count = 0
        seen_titles: set[str] = {
            c.get("title", "").lower()[:50]
            for c in self.artifacts.get("candidate_papers", [])
        }

        while needs_more and loop_count < self.config.max_reread_loops:
            loop_count += 1
            self._check_interrupt()
            self.display.step(f"Re-read loop {loop_count}: {len(needs_more)} gaps")
            self.display.tick()
            logger.info("Phase 4: Re-read loop %d for %d gaps", loop_count, len(needs_more))

            new_candidates = []

            # Prepend the paper's main topic to gap queries so searches
            # stay on-topic instead of returning generic methodology papers.
            main_topic = self.artifacts.get("research_brief", {}).get("title", "")
            if not main_topic:
                main_topic = self.artifacts.get("research_brief", {}).get("topic", "")

            for gap_topic in needs_more[:2]:
                gap_query = f"{main_topic}: {gap_topic}" if main_topic else str(gap_topic)
                self._delay()

                # Build keyword set from topic + gap for relevance filtering
                _stopwords = {
                    "the", "and", "for", "with", "from", "that", "this", "are",
                    "was", "has", "its", "not", "but", "how", "what", "when",
                    "can", "may", "will", "also", "than", "more", "new", "use",
                    "used", "using", "based", "study", "analysis", "research",
                    "review", "approach", "method", "model", "data", "results",
                    "effect", "effects", "evidence", "role", "impact", "paper",
                }
                _topic_words = set(
                    w.lower() for w in re.findall(r"[a-zA-Z]{3,}", f"{main_topic} {gap_topic}")
                ) - _stopwords

                def _is_gap_result_relevant(title: str) -> bool:
                    """Relevance check: at least 2 topic keywords in the title.

                    Single-keyword overlap is too loose and lets in off-topic
                    papers (e.g., particle physics matching on 'analysis').
                    """
                    title_words = set(w.lower() for w in re.findall(r"[a-zA-Z]{3,}", title))
                    overlap = title_words & _topic_words
                    return len(overlap) >= 2

                # Gap-filling uses only academic APIs (no LLM web search)
                # to avoid triggering dozens of extra web searches per article.

                # 1. Serper.dev Google Scholar (if key available)
                if self.serper_api_key and len(new_candidates) < 3:
                    try:
                        from .academic_search import search_serper_scholar
                        serper_hits = search_serper_scholar(gap_query, api_key=self.serper_api_key, limit=5)
                        for hit in serper_hits:
                            title_key = hit["title"].lower()[:50]
                            if title_key not in seen_titles and hit.get("title") and _is_gap_result_relevant(hit["title"]):
                                seen_titles.add(title_key)
                                pid = f"serper_{len(new_candidates)}_{loop_count}"
                                entry = {
                                    "paper_id": pid,
                                    "title": hit["title"],
                                    "abstract": hit.get("abstract", "")[:500],
                                    "similarity": 0.8,
                                    "is_web": True,
                                    "authors": hit.get("authors", []),
                                    "year": hit.get("year"),
                                    "doi": hit.get("doi", ""),
                                    "url": hit.get("url", ""),
                                }
                                new_candidates.append(entry)
                                self.artifacts.setdefault("custom_source_content", {})[pid] = {
                                    "title": hit["title"],
                                    "content": (
                                        f"Title: {hit['title']}\n"
                                        f"Authors: {', '.join(hit.get('authors', []))}\n"
                                        f"Year: {hit.get('year', 'N/A')}\n\n"
                                        f"Abstract: {hit.get('abstract', '')}"
                                    ),
                                    "source_path": hit.get("url", ""),
                                    "source_type": "web",
                                    "abstract": hit.get("abstract", "")[:500],
                                    "doi": hit.get("doi", ""),
                                    "authors": hit.get("authors", []),
                                }
                    except Exception as e:
                        logger.warning("Serper scholar gap search failed: %s", e)

                # 2b. Academic APIs fallback (Crossref/arXiv/Semantic Scholar)
                if len(new_candidates) < 3:
                    try:
                        academic_hits = search_academic(gap_query, limit=5, mailto=self.owner_email or None)
                        for hit in academic_hits:
                            title_key = hit["title"].lower()[:50]
                            if title_key not in seen_titles and hit.get("title") and _is_gap_result_relevant(hit["title"]):
                                seen_titles.add(title_key)
                                s2_id = hit.get("paper_id_s2", "")
                                arxiv_id = hit.get("arxiv_id", "")
                                if s2_id:
                                    pid = f"s2_{s2_id}"
                                elif arxiv_id:
                                    pid = f"arxiv_{arxiv_id}"
                                else:
                                    pid = f"web_{len(new_candidates)}_{loop_count}"
                                entry = {
                                    "paper_id": pid,
                                    "title": hit["title"],
                                    "abstract": hit.get("abstract", "")[:500],
                                    "similarity": 0.75,
                                    "is_web": True,
                                    "authors": hit.get("authors", []),
                                    "year": hit.get("year"),
                                    "doi": hit.get("doi", ""),
                                    "url": hit.get("url", ""),
                                }
                                new_candidates.append(entry)
                                self.artifacts.setdefault("custom_source_content", {})[pid] = {
                                    "title": hit["title"],
                                    "content": (
                                        f"Title: {hit['title']}\n"
                                        f"Authors: {', '.join(hit.get('authors', []))}\n"
                                        f"Year: {hit.get('year', 'N/A')}\n\n"
                                        f"Abstract: {hit.get('abstract', '')}"
                                    ),
                                    "source_path": hit.get("url", ""),
                                    "source_type": "web",
                                    "abstract": hit.get("abstract", "")[:500],
                                    "doi": hit.get("doi", ""),
                                    "authors": hit.get("authors", []),
                                }
                    except Exception as e:
                        logger.warning("Academic search for gap '%s' failed: %s", gap_query, e)

                # 3. Also check AgentPub platform
                try:
                    results = self.client.search(gap_query, top_k=5)
                    for r in results:
                        if r.paper_id not in memos:
                            title_key = r.title.lower()[:50]
                            if title_key not in seen_titles:
                                seen_titles.add(title_key)
                                new_candidates.append({
                                    "paper_id": r.paper_id,
                                    "title": r.title,
                                    "abstract": r.abstract[:500] if r.abstract else "",
                                })
                except Exception:
                    pass

            if not new_candidates:
                break

            self.display.step(f"Found {len(new_candidates)} gap-filling papers")

            # Add to candidates and re-read
            existing = {c["paper_id"] for c in self.artifacts.get("candidate_papers", [])}
            for nc in new_candidates:
                if nc["paper_id"] not in existing:
                    self.artifacts.setdefault("candidate_papers", []).append(nc)

            # Update references panel
            ref_start = len(self.artifacts.get("candidate_papers", [])) - len(new_candidates) + 1
            custom_content = self.artifacts.get("custom_source_content", {})
            for i, nc in enumerate(new_candidates):
                pid = nc["paper_id"]
                ref_authors = ", ".join(nc.get("authors", [])[:3]) if nc.get("authors") else ""
                ref_year = str(nc["year"]) if nc.get("year") else ""
                ref_url = nc.get("url", "")
                ref_doi = nc.get("doi", "")
                if pid in custom_content:
                    src = custom_content[pid]
                    ref_authors = ref_authors or ", ".join(src.get("authors", [])[:3])
                    ref_doi = ref_doi or src.get("doi", "")
                    ref_url = ref_url or src.get("source_path", "")
                self.display.add_reference(
                    index=ref_start + i,
                    authors=ref_authors,
                    year=ref_year,
                    title=nc.get("title", pid),
                    url=ref_url,
                    doi=ref_doi,
                )

            new_ids = [nc["paper_id"] for nc in new_candidates]
            self._phase3_read_and_annotate(paper_ids=new_ids)

            # Re-analyze
            prompt2 = f"""{self._paper_context()}

Updated reading memos (now {len(self.artifacts.get('reading_memos', {}))} papers).
Previous gaps were: {json.dumps(needs_more)}

Re-evaluate with the paper's research questions in mind. Return JSON with same keys as before:
- "evidence_map", "key_arguments", "novel_contributions", "needs_more_reading" """

            try:
                evidence = self.llm.generate_json(system, prompt2)
            except Exception as e:
                logger.warning("Phase 4 re-analysis call failed — keeping previous: %s", e)
                break
            if not isinstance(evidence, dict):
                evidence = {}
            self.artifacts["evidence_map"] = evidence
            needs_more = evidence.get("needs_more_reading", [])
            if not isinstance(needs_more, list):
                needs_more = []

        self._log_artifact("evidence_map", evidence)
        self.display.phase_done(4)

    # ------------------------------------------------------------------
    # Phase 5: Draft
    # ------------------------------------------------------------------

    def _phase5_draft(self) -> None:
        logger.info("Phase 5: DRAFT")
        self.display.phase_start(5, "Draft")

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        evidence = self.artifacts.get("evidence_map", {})
        if not isinstance(evidence, dict):
            evidence = {}
        ev_map = evidence.get("evidence_map", {})
        if not isinstance(ev_map, dict):
            # Small models sometimes return a list instead of a dict — normalize
            ev_map = {}
        memos = self.artifacts.get("reading_memos", {})
        if not isinstance(memos, dict):
            memos = {}
        candidates = self.artifacts.get("candidate_papers", [])
        cand_by_id = {c["paper_id"]: c for c in candidates}

        # Build a numbered reference list with citation keys the LLM must use
        custom_content = self.artifacts.get("custom_source_content", {})
        ref_list = []
        for i, (pid, memo) in enumerate(memos.items(), 1):
            cand = cand_by_id.get(pid, {})
            src = custom_content.get(pid, {})
            # Pull authors from best available source
            authors = cand.get("authors", []) or src.get("authors", [])
            year = cand.get("year") or src.get("year") or "n.d."

            # Build a meaningful cite_key — never use "Unknown"
            title = memo.get("title", "")
            first_author = ""
            if authors:
                # Use the last name of the first author
                first_author = authors[0].split()[-1] if authors[0] else ""
            if not first_author and title:
                # Fallback: use first meaningful word from title (skip articles/prepositions)
                _SKIP_WORDS = {"the", "a", "an", "of", "in", "on", "for", "and", "to", "with", "from", "by", "is", "are", "was", "were", "at", "its"}
                title_words = [w.rstrip(",.:;") for w in title.split() if w.lower().rstrip(",.:;") not in _SKIP_WORDS and len(w) > 2]
                first_author = title_words[0] if title_words else f"Ref{i}"
            if not first_author:
                first_author = f"Ref{i}"

            cite_key = f"[{first_author}, {year}]" if year and year != "n.d." else f"[{first_author}]"

            # Disambiguate cite_key collisions (e.g., two "Smith, 2020" papers)
            existing_keys = {r["cite_key"] for r in ref_list}
            if cite_key in existing_keys:
                # Add suffix: [Smith, 2020a], [Smith, 2020b], etc.
                for suffix_ord in range(ord("a"), ord("z") + 1):
                    candidate_key = cite_key.rstrip("]") + chr(suffix_ord) + "]"
                    if candidate_key not in existing_keys:
                        cite_key = candidate_key
                        break

            ref_entry = {
                "ref_num": i,
                "cite_key": cite_key,
                "title": title,
                "authors": authors[:3],
                "year": year,
                "key_findings": memo.get("key_findings", []),
                "quotable_claims": memo.get("quotable_claims", []),
            }
            ref_list.append(ref_entry)

        ref_list_text = json.dumps(ref_list, indent=2)
        self.artifacts["ref_list_text"] = ref_list_text

        # F1: Compact ref list (cite_key + title + authors + year only) for non-writing calls
        ref_list_compact = [
            {
                "ref_num": r["ref_num"],
                "cite_key": r["cite_key"],
                "title": r["title"],
                "authors": r["authors"][:2],
                "year": r["year"],
            }
            for r in ref_list
        ]
        ref_list_compact_text = json.dumps(ref_list_compact, indent=2)
        self.artifacts["ref_list_compact_text"] = ref_list_compact_text

        # E1: Build title -> cite_key lookup for evidence-cite_key binding
        title_to_cite_key: dict[str, str] = {}
        for r in ref_list:
            title_lower = r["title"].lower()[:50].strip()
            if title_lower:
                title_to_cite_key[title_lower] = r["cite_key"]

        # E1: Post-process evidence_map — resolve supporting_sources titles to cite_keys
        for section_name_ev, entries in ev_map.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                sources = entry.get("supporting_sources", [])
                resolved = []
                for src_title in sources:
                    src_lower = str(src_title).lower()[:50].strip()
                    ck = title_to_cite_key.get(src_lower)
                    if not ck:
                        # Fuzzy: check if any ref title starts with same prefix
                        for t_key, t_ck in title_to_cite_key.items():
                            if t_key[:30] == src_lower[:30]:
                                ck = t_ck
                                break
                    resolved.append(ck or str(src_title))
                entry["resolved_cite_keys"] = resolved
        self.artifacts["title_to_cite_key"] = title_to_cite_key

        sections_written: dict[str, str] = {}

        write_order = _WRITE_ORDER
        if self.config.quality_level == "lite":
            write_order = ["Introduction", "Methodology", "Results", "Discussion", "Conclusion"]

        outline = self.artifacts.get("paper_outline", {})
        if not isinstance(outline, dict):
            outline = {}
        outline_sections = outline.get("outline", {})
        if not isinstance(outline_sections, dict):
            outline_sections = {}
        thesis = outline.get("thesis", "")

        for section_name in write_order:
            self._delay()
            self._check_interrupt()
            self.display.section_start(section_name)
            self.display.tick()

            section_evidence = ev_map.get(section_name, [])
            context_sections = dict(sections_written)

            # Pull outline guidance for this section
            section_outline = outline_sections.get(section_name, {})
            outline_hint = ""
            if section_outline:
                key_points = section_outline.get("key_points", [])
                approach = section_outline.get("approach", "")
                if key_points:
                    outline_hint = f"\nOutline key points for this section:\n"
                    outline_hint += "\n".join(f"- {p}" for p in key_points)
                if approach:
                    outline_hint += f"\nApproach: {approach}"

            # E3: Inject relevant synthesis insights for this section
            synthesis_hint = self._get_relevant_synthesis(section_name, ev_map)

            # Build evidence-first findings block from evidence_map + reading memos
            findings_block = self._build_evidence_findings(
                section_name, section_evidence, memos, cand_by_id,
            )

            # Build weakness guidance block from prior review feedback
            weakness_hint = self.artifacts.get("weakness_summary", "")
            weakness_block = ""
            if weakness_hint:
                weakness_block = (
                    f"\nQUALITY IMPROVEMENT GUIDANCE (from prior review feedback):\n"
                    f"{self._prompts.get('phase5_weakness_guidance', '')}\n"
                    f"{weakness_hint}\n"
                )

            # Inject section-specific structural guidance + paper-type guidance
            from agentpub.prompts import _SECTION_GUIDANCE, _PAPER_TYPE_GUIDANCE
            sec_guidance = _SECTION_GUIDANCE.get(section_name, "")
            paper_type = brief.get("paper_type", "survey")
            type_guidance = _PAPER_TYPE_GUIDANCE.get(paper_type, {})
            if type_guidance:
                global_hint = type_guidance.get("global", "")
                section_hint = type_guidance.get(section_name, "")
                if global_hint or section_hint:
                    sec_guidance += "\n\nPAPER TYPE GUIDANCE:\n"
                    if global_hint:
                        sec_guidance += global_hint + "\n"
                    if section_hint:
                        sec_guidance += section_hint
            system = self._prompts["phase5_write_section"].format(
                section_name=section_name,
                section_guidance=sec_guidance,
            )

            # Build context summary — brief summaries of written sections, not full JSON dumps
            context_summary = ""
            if context_sections:
                context_parts = []
                for sname, scontent in context_sections.items():
                    # First 200 chars is enough for coherence
                    context_parts.append(f"- {sname}: {scontent[:200]}...")
                context_summary = "Previously written sections (for coherence):\n" + "\n".join(context_parts)

            # Inject real pipeline metadata for Methodology section
            pipeline_block = ""
            if section_name == "Methodology":
                pm = self.artifacts.get("pipeline_metadata", {})
                model_name = pm.get('model', 'unknown')
                provider_name = pm.get('provider', 'unknown')
                pipeline_block = (
                    "\nPIPELINE METADATA (use these EXACT values in your prose — DO NOT substitute other model names):\n"
                    f"- Model: {model_name} (provider: {provider_name})\n"
                    f"- APIs queried: {', '.join(pm.get('apis_queried', ['unknown']))}\n"
                    f"- Search terms: {json.dumps(pm.get('search_terms', [])[:8])}\n"
                    f"- Papers found: {pm.get('papers_found', '?')} → "
                    f"{pm.get('papers_included', '?')} included after relevance screening\n"
                    f"- Date range of included papers: {pm.get('date_range', 'not available')}\n"
                    f"\nCRITICAL: The model used is {model_name} — NOT gpt-5, NOT gpt-4, NOT any other model. "
                    f"Write '{model_name}' exactly as shown when describing the AI model used.\n"
                    "Write the methodology around these REAL values. Do NOT invent "
                    "different numbers or describe human procedures that did not occur.\n"
                    "This is a qualitative systematic review — do NOT claim to perform meta-analysis, "
                    "statistical pooling, effect sizes, or quantitative aggregation.\n"
                )

            prompt = f"""Paper title: {brief.get('title', '')}
Research questions: {json.dumps(brief.get('research_questions', []))}
Paper type: {brief.get('paper_type', 'survey')}
{f'Thesis: {thesis}' if thesis else ''}
{outline_hint}
{weakness_block}
{synthesis_hint}
{pipeline_block}
VERIFIED FINDINGS FROM THE LITERATURE:
Each finding below is pre-bound to a specific cite_key. You MUST use that exact cite_key when discussing that finding. Do NOT reassign findings to different cite_keys.
{findings_block if findings_block else 'No specific findings mapped — use the reference list below and cite conservatively.'}

REFERENCE LIST (use ONLY these — cite by cite_key):
{ref_list_text[:12000]}

{context_summary}

Write the '{section_name}' section. Synthesize the findings into flowing academic prose.

CITATION WHITELIST RULE (strictly enforced — paragraphs violating this will be auto-deleted):
- Every paragraph that makes a factual, empirical, or attributive claim MUST contain at least one [Author, Year] citation from the reference list above.
- Paragraphs without citations may ONLY contain: topic sentences, transitions between ideas, or your own interpretive framing.
- If you cannot cite a claim, do NOT write it. Omit the claim entirely rather than stating it without a citation.
- NEVER write findings, statistics, comparisons, or attributions without an inline citation.
CITATION DIVERSITY: Distribute citations across many references — do NOT over-rely on 2-3 papers for all claims. Each paragraph should cite different references.

Target length: {_SECTION_WORD_TARGETS.get(section_name, 1000)} words ({_SECTION_WORD_TARGETS.get(section_name, 1000) // 150}-{_SECTION_WORD_TARGETS.get(section_name, 1000) // 120} paragraphs of 150-200 words each).

FORMATTING RULES:
- Separate each paragraph with a blank line (two newlines).
- Use **bold** for key terms when first introduced.
- Use bullet lists (- item) for enumerations of 3+ items, comparisons, or lists of findings.
- Keep flowing academic prose for narrative passages — do not over-use bullets.
- Do NOT use markdown headers (# or ##) — the section heading is added separately.
- For mathematical expressions, equations, scoring functions, or formulas, use LaTeX notation:
  - Inline math: $expression$ (e.g., $w_M = 0.35$)
  - Block/display math: $$expression$$ on its own line (e.g., $$S(r) = \\sum_{{i}} w_i \\cdot f_i(r)$$)
  - Use proper LaTeX symbols: \\sum, \\prod, \\frac{{}}{{}}, \\log, \\sqrt{{}}, \\alpha, \\beta, etc.
  - ALWAYS prefer LaTeX math over plain-text formulas whenever an equation, score, metric, or mathematical relationship is described.

Write the section content directly as academic text with the formatting above. Do NOT wrap in JSON.
Do NOT include a section heading — just write the body paragraphs."""

            try:
                result = self.llm.generate(system, prompt, temperature=0.5, max_tokens=16000)
            except Exception as e:
                logger.warning("Phase 5 section '%s' generation failed — skipping: %s", section_name, e)
                self.display.step(f"Section {section_name} failed — skipping")
                continue
            content = self._extract_section_text(result.text)
            sections_written[section_name] = content
            self.display.section_done(section_name, content)
            self._log_artifact(f"draft:{section_name}", content[:200])

        # Write abstract last (has full context)
        self._delay()
        self._check_interrupt()
        self.display.step("Writing abstract...")
        self.display.tick()
        system = self._prompts["phase5_abstract"]
        # Use longer summaries for better abstract quality
        section_summaries = []
        for k, v in sections_written.items():
            # Give Introduction, Results, and Conclusion more space
            limit = 600 if k in ("Introduction", "Results", "Discussion", "Conclusion") else 400
            section_summaries.append(f"{k}: {v[:limit]}")
        prompt = f"""Paper title: {brief.get('title', '')}
Research questions: {json.dumps(brief.get('research_questions', []))}

Full paper sections:
{chr(10).join(section_summaries)}

Write a structured abstract (200-300 words) as a single paragraph covering:
context, objective, method, key results, and conclusion.

Return JSON: {{"abstract": "the abstract text"}}"""

        try:
            abstract_result = self.llm.generate_json(system, prompt)
        except Exception as e:
            logger.warning("Phase 5 abstract generation failed — using empty: %s", e)
            abstract_result = {"abstract": ""}
        self.artifacts["abstract"] = abstract_result.get("abstract", "")
        self.display.set_abstract(self.artifacts["abstract"])

        self.artifacts["zero_draft"] = sections_written

        # Word count enforcement: expand short sections until we hit the minimum
        total_words = sum(len(s.split()) for s in sections_written.values())
        self.display.step(f"Draft: {total_words} words across {len(sections_written)} sections")

        # E2: Evidence-bounded expansion — only expand with uncovered evidence
        expand_pass = 0
        stall_count = 0
        while total_words < self.config.min_total_words and expand_pass < self.config.max_expand_passes:
            expand_pass += 1

            # E2: Build uncovered evidence per section
            cited_keys_in_text = self._collect_cited_keys(sections_written)
            ref_list_compact_text = self.artifacts.get("ref_list_compact_text", "[]")
            any_uncovered = False

            self.display.step(f"Expanding: {total_words}/{self.config.min_total_words} words (pass {expand_pass}/{self.config.max_expand_passes})")
            self.display.tick()

            # Expand sections below their per-section minimum
            section_lengths = {k: len(v.split()) for k, v in sections_written.items()}
            to_expand = [
                name for name, wc in section_lengths.items()
                if wc < _SECTION_WORD_MINIMUMS.get(name, 800)
            ]
            if not to_expand:
                # All sections meet minimums but total is still low — expand the shortest major sections
                major_sections = [(n, wc) for n, wc in section_lengths.items()
                                  if n not in ("Limitations", "Conclusion")]
                major_sections.sort(key=lambda x: x[1])
                to_expand = [name for name, _ in major_sections[:3]]

            prev_total = total_words
            for section_name in to_expand:
                self._delay()
                self._check_interrupt()
                current_content = sections_written[section_name]
                current_words = len(current_content.split())

                # E2: Find uncovered evidence for this section
                section_ev = ev_map.get(section_name, [])
                uncovered_lines = []
                for ev_entry in section_ev:
                    if not isinstance(ev_entry, dict):
                        continue
                    resolved = ev_entry.get("resolved_cite_keys", [])
                    claim = ev_entry.get("claim", "")
                    strength = ev_entry.get("strength", "moderate")
                    for ck in resolved:
                        if ck.lower() not in cited_keys_in_text:
                            uncovered_lines.append(f"- {ck}: '{claim}' (strength: {strength})")

                if not uncovered_lines:
                    # No uncovered evidence — skip this section
                    continue

                any_uncovered = True
                uncovered_text = "\n".join(uncovered_lines[:15])
                section_min = _SECTION_WORD_MINIMUMS.get(section_name, 800)
                words_needed = max(section_min - current_words, 300)
                n_paragraphs = max(2, min(len(uncovered_lines), words_needed // 150))

                system = self._prompts["phase5_expand_section"].format(section_name=section_name)
                prompt = f"""{self._paper_context()}

The '{section_name}' section has uncovered evidence that should be discussed.

Current section (for context only — do NOT repeat or paraphrase this content):
{current_content[:3000]}

UNCOVERED FINDINGS to discuss (each is pre-bound to a cite_key — use that exact cite_key):
{uncovered_text}

REFERENCE LIST (use ONLY these — cite by cite_key):
{ref_list_compact_text[:6000]}

Write {n_paragraphs} NEW paragraphs (150-200 words each) discussing the uncovered findings above.
Each finding is pre-bound to a specific cite_key — use that exact cite_key.

Write the new paragraphs as academic text that matches the tone and style of the existing section.
Maintain a consistent scholarly voice — avoid promotional language, monotonous transitions
(Furthermore/Moreover/Additionally), and repetition of phrases already used in the existing content.
Separate paragraphs with blank lines. Use **bold** for key terms and bullet lists (- item) for enumerations where appropriate.
Do NOT repeat existing content. Do NOT wrap in JSON. Do NOT add section headers."""

                try:
                    result = self.llm.generate(system, prompt, temperature=0.5, max_tokens=16000)
                except Exception as e:
                    logger.warning("Phase 5 expansion of '%s' failed — skipping: %s", section_name, e)
                    continue
                new_text = self._extract_section_text(result.text)
                if new_text and len(new_text.split()) > 30:
                    sections_written[section_name] = current_content + "\n\n" + new_text
                    self.display.section_done(section_name, sections_written[section_name])
                    new_wc = len(sections_written[section_name].split())
                    added = len(new_text.split())
                    self.display.step(f"  {section_name}: {current_words} -> {new_wc} words (+{added})")

            total_words = sum(len(s.split()) for s in sections_written.values())
            self.display.step(f"After expansion: {total_words} words")

            # E2: Stop if all evidence is covered
            if not any_uncovered:
                logger.info("All evidence covered at %d words — skipping further expansion", total_words)
                self.display.step(f"All evidence covered at {total_words} words — stopping expansion")
                break

            # Stall detection
            if total_words <= prev_total + 50:
                stall_count += 1
                if stall_count >= 2:
                    logger.warning("Expansion stalled at %d words after %d passes", total_words, expand_pass)
                    break
            else:
                stall_count = 0

        # F5: Skip dedup when no expansion was done (each section written once with prior-section context)
        if expand_pass == 0:
            logger.info("No expansion passes — skipping dedup (low duplication risk)")
            self.display.step("No expansion — skipping dedup pass")
            sections_written = self._inject_citations(sections_written)
            self.artifacts["zero_draft"] = sections_written
            total_words = sum(len(s.split()) for s in sections_written.values())
            self.display.step(f"Final draft: {total_words} words, {len(sections_written)} sections + abstract")
            logger.info("Draft complete: %d words, %d sections + abstract", total_words, len(sections_written))
            self.display.phase_done(5)
            return

        # Cross-section dedup pass: remove repeated discussions of same references/findings
        self._delay()
        self._check_interrupt()
        self.display.step("Deduplicating across sections...")
        self.display.tick()
        ref_list_text = self.artifacts.get("ref_list_text", "[]")
        full_draft_text = "\n\n".join(
            f"## {heading}\n{content}" for heading, content in sections_written.items()
        )
        # F1: Use compact ref list for dedup
        ref_list_compact_text = self.artifacts.get("ref_list_compact_text", ref_list_text)
        system = self._prompts.get("phase5_dedup", "Remove duplicated content across sections.")
        dedup_prompt = f"""{self._paper_context()}

REFERENCE LIST:
{ref_list_compact_text[:6000]}

Full draft:
{full_draft_text[:14000]}

Remove cross-section duplication while preserving the paper's coherent narrative.
Each section has a distinct purpose — Introduction frames the problem, Related Work surveys the field,
Methodology describes the approach, Results presents findings, Discussion interprets them,
Limitations acknowledges constraints, and Conclusion synthesizes the contribution.
When the same finding or reference appears in multiple sections, keep the most detailed treatment
and replace others with brief cross-references or remove them.
Preserve consistent terminology and tone throughout — do not introduce new phrasing or vocabulary.

Return JSON with:
- "deduped_sections": dict of section_name -> revised_content (only include sections that changed)
- "duplicates_found": list of strings describing what was duplicated and where"""

        try:
            dedup_result = self.llm.generate_json(system, dedup_prompt, temperature=0.2)
            deduped = dedup_result.get("deduped_sections", {})
            dupes_found = dedup_result.get("duplicates_found", [])
            for section, new_content in deduped.items():
                if section in sections_written and new_content:
                    original_words = len(sections_written[section].split())
                    new_words = len(new_content.split())
                    # 75% word-count guard: reject dedup that removes >25% of a section
                    if original_words > 0 and new_words < original_words * 0.75:
                        logger.warning(
                            "Dedup of '%s' dropped from %d to %d words — keeping original",
                            section, original_words, new_words,
                        )
                        continue
                    sections_written[section] = new_content
                    self.display.section_done(section, new_content)
            if dupes_found:
                logger.info("Dedup removed %d duplications", len(dupes_found))
                self.display.step(f"Removed {len(dupes_found)} cross-section duplications")
            total_words = sum(len(s.split()) for s in sections_written.values())
        except Exception as e:
            logger.warning("Dedup pass failed (non-fatal): %s", e)

        sections_written = self._inject_citations(sections_written)
        self.artifacts["zero_draft"] = sections_written
        total_words = sum(len(s.split()) for s in sections_written.values())
        self.display.step(f"Final draft: {total_words} words, {len(sections_written)} sections + abstract")
        logger.info("Draft complete: %d words, %d sections + abstract", total_words, len(sections_written))
        self.display.phase_done(5)

    def _inject_citations(self, sections: dict[str, str]) -> dict[str, str]:
        """Inject [Author, Year] citation markers into sections that lack them.

        The LLM often writes prose without bracket citations despite being
        instructed to use cite_keys. This method uses two strategies:
        1. Mechanical: find bare author surname mentions and append cite_keys
        2. Focused LLM pass: for sections still lacking citations, ask the LLM
           specifically to insert [Author, Year] markers without rewriting.
        """
        ref_list: list[dict] = []
        try:
            ref_list = json.loads(self.artifacts.get("ref_list_text", "[]"))
        except (json.JSONDecodeError, TypeError):
            return sections
        if not ref_list:
            return sections

        # Check current citation density
        existing_cites = self._collect_cited_keys(sections)
        if len(existing_cites) >= len(ref_list) * 0.4:
            logger.info("Citation injection: %d citations already present — skipping", len(existing_cites))
            return sections

        self.display.step(f"Low citation density ({len(existing_cites)} markers) — injecting citations...")

        # ── Strategy 1: Mechanical surname matching ──
        # Build surname → cite_key map
        surname_to_citekey: dict[str, str] = {}
        for r in ref_list:
            ck = r.get("cite_key", "")
            authors = r.get("authors", [])
            if ck and authors and isinstance(authors[0], str) and authors[0].strip():
                surname = authors[0].split()[-1]
                # Require >= 4 chars to avoid short surnames matching inside words
                if len(surname) >= 4:
                    surname_to_citekey[surname.lower()] = ck

        mechanical_injected = 0
        for section_name, content in sections.items():
            for surname_lower, cite_key in surname_to_citekey.items():
                surname_cap = surname_lower.capitalize()
                # Match "Surname et al." or "Surname and Coauthor" or "Surname (YYYY)"
                # that are NOT already followed by a bracket citation.
                # Use \b on both sides to avoid matching inside words.
                bare_re = re.compile(
                    r"(\b" + re.escape(surname_cap) + r"\b)"
                    r"(\s+(?:et\s+al\.?|and\s+[A-Z][a-z]+))?"
                    r"(\s*\(\d{4}[a-z]?\))?"
                    r"(?!\s*\[)",  # not already followed by bracket citation
                )
                # Only inject once per surname per section to avoid clutter
                match = bare_re.search(content)
                if match:
                    # Skip if match is inside an existing bracket citation [...]
                    start = match.start()
                    preceding = content[:start]
                    open_brackets = preceding.count("[") - preceding.count("]")
                    if open_brackets > 0:
                        continue  # surname is inside [...], skip
                    # Check that this cite_key isn't already present in the section
                    if cite_key not in content:
                        # Insert cite_key after the matched mention
                        insert_pos = match.end()
                        content = content[:insert_pos] + " " + cite_key + content[insert_pos:]
                        mechanical_injected += 1
            sections[section_name] = content

        if mechanical_injected > 0:
            self.display.step(f"  Mechanical injection: {mechanical_injected} citations added")
            logger.info("Citation injection: %d mechanical citations added", mechanical_injected)

        # Re-check density after mechanical pass
        post_mechanical = self._collect_cited_keys(sections)
        if len(post_mechanical) >= len(ref_list) * 0.4:
            logger.info("Citation injection: %d citations after mechanical pass — sufficient", len(post_mechanical))
            return sections

        # ── Strategy 2: Focused LLM citation pass ──
        # Per-section: ask the LLM to insert [Author, Year] markers
        ref_list_compact_text = self.artifacts.get("ref_list_compact_text", "[]")
        ev_map = self.artifacts.get("evidence_map", {})
        llm_injected = 0

        for section_name, content in sections.items():
            # Check per-section citation count
            section_cites = self._collect_cited_keys({section_name: content})
            # Sections under 200 words (e.g. Conclusion) need fewer citations
            min_cites = 2 if len(content.split()) < 300 else 4
            if len(section_cites) >= min_cites:
                continue  # this section has enough citations

            self._delay()
            self._check_interrupt()
            self.display.tick()

            # Get evidence for this section to guide citation placement
            section_evidence = ev_map.get(section_name, [])
            evidence_hint = ""
            if section_evidence:
                ev_lines = []
                for ev in section_evidence[:10]:
                    if isinstance(ev, dict):
                        claim = ev.get("claim", "")
                        keys = ev.get("resolved_cite_keys", [])
                        if claim and keys:
                            ev_lines.append(f"- Claim: \"{claim}\" → cite: {', '.join(keys)}")
                if ev_lines:
                    evidence_hint = "\n\nEVIDENCE-CITATION MAPPING (insert these citations near the matching claims):\n" + "\n".join(ev_lines)

            ctx = self._paper_context()
            system = (
                "You are a citation formatting assistant. Your ONLY job is to insert "
                "[Author, Year] citation markers into academic text. Do NOT rewrite, "
                "rephrase, or reorganize the text. Do NOT add new content. ONLY insert "
                "citation markers from the provided reference list."
            )
            prompt = f"""{ctx}

Section: {section_name}

REFERENCE LIST (insert these cite_keys where claims are supported):
{ref_list_compact_text[:6000]}
{evidence_hint}

TEXT TO ADD CITATIONS TO:
{content[:6000]}

RULES:
1. Insert [Author, Year] markers (from the cite_key column) after claims that are supported by the reference
2. Do NOT change any wording — only INSERT citation markers
3. Place citations at the end of the relevant sentence, before the period
4. Each paragraph should cite at least 1-2 different references
5. Return ONLY the section text with citations inserted — no JSON, no headers"""

            try:
                result = self.llm.generate(system, prompt, temperature=0.1, max_tokens=16000)
            except Exception as e:
                logger.warning("Citation injection LLM for '%s' failed — keeping as-is: %s", section_name, e)
                continue
            injected_text = self._extract_section_text(result.text)

            if injected_text:
                # Validate: text should be roughly same length (just added citations)
                orig_words = len(content.split())
                new_words = len(injected_text.split())
                new_cites = self._collect_cited_keys({section_name: injected_text})

                # Accept if: similar length (+/- 30%) AND more citations
                if (new_words >= orig_words * 0.7
                        and new_words <= orig_words * 1.3
                        and len(new_cites) > len(section_cites)):
                    sections[section_name] = injected_text
                    added = len(new_cites) - len(section_cites)
                    llm_injected += added
                    logger.info(
                        "Citation injection LLM: %s gained %d citations (%d → %d)",
                        section_name, added, len(section_cites), len(new_cites),
                    )
                else:
                    logger.info(
                        "Citation injection LLM: %s rejected (words %d→%d, cites %d→%d)",
                        section_name, orig_words, new_words,
                        len(section_cites), len(new_cites),
                    )

        if llm_injected > 0:
            self.display.step(f"  LLM citation pass: {llm_injected} citations added")
            logger.info("Citation injection: %d LLM citations added", llm_injected)

        total_cites = len(self._collect_cited_keys(sections))
        self.display.step(f"  Citation injection complete: {total_cites} total citations")
        return sections

    # ------------------------------------------------------------------
    # Phase 6: Revise & Verify
    # ------------------------------------------------------------------

    def _sanitize_fabrication(self, draft: dict[str, str]) -> dict[str, str]:
        """Strip fabricated methodology claims that LLMs hallucinate.

        Runs after Phase 5 draft, before Phase 6 revision. Removes entire
        sentences containing known fabrication markers from methodology
        sections. This is more reliable than prompt-only prevention.
        """
        _FABRICATION_PATTERNS = [
            # Original patterns
            r"[Cc]ohen['\u2019]?s?\s+kappa",
            r"inter[- ]?rater\s+reliability",
            r"two\s+independent\s+reviewers",
            r"three\s+independent\s+reviewers",
            r"disagreements?\s+(?:were|was)\s+resolved\s+by\s+consensus",
            r"manual\s+screening\s+by\s+(?:two|three|multiple)\s+(?:reviewers|researchers)",
            r"dual\s+(?:independent\s+)?review",
            r"hybrid[,\s]+human[- ]in[- ]the[- ]loop",
            r"PRISMA\s+flow\s+diagram",
            r"hand[- ]?search(?:ed|ing)?",
            r"snowball\s+(?:sampling|search)",
            # Broader patterns — LLM uses synonyms to evade the above
            r"\bkappa\s*[=:]\s*0\.\d",  # bare "kappa = 0.81" without "Cohen's"
            r"(?:two|three|multiple)\s+(?:biomedical\s+)?(?:annotators|coders|raters)",
            r"adjudicat(?:ed|ion)\s+by\s+(?:a\s+)?(?:third|senior|additional)\s+(?:expert|reviewer|annotator)",
            r"(?:precision|recall|F1)\s*[=:]\s*0\.\d{2}",  # fabricated P/R/F1 scores
            r"\bgold\s+standard\s*=\s*\d+\s+(?:randomly\s+)?sampled",  # fabricated gold standards
            r"independently\s+annotated\s+by\s+(?:two|three|multiple)",
            r"hybrid[,\s]+human[- ]in[- ]the[- ]loop\s+synthesis",
            r"reconciled?\s+disagreements?\s+through\s+discussion",
            r"until\s+consensus\s+was\s+reached",
            r"(?:ROC|AUC|sensitivity)\s+(?:curve|analysis|plot)s?\s+(?:are|were|is)\s+provided",
            r"(?:Supplementary|Supporting)\s+(?:Figure|Table|Material)\s+S\d",  # fabricated supplements
            r"\bbootstrap\s+CI\s*=",  # fabricated confidence intervals
            r"Records?\s+identified\s*(?:through|via|from)?\s*(?:automated)?\s*searches?\s*:\s*[\d,]+",  # fabricated PRISMA counts
            r"(?:de-?duplication|screening)\s+removed\s+[\d,]+\s+records?",  # fabricated screening counts
            r"(?:Gold|gold)\s+standard\s*(?:=|of|:)\s*\d+\s+(?:randomly|manually)",  # fabricated gold standard
            r"archived\s+in\s+the\s+project\s+repository\s+on\s+Zenodo",  # fabricated data deposit
            r"reviewer-only\s+access\s+snapshot",  # fabricated reviewer access
            r"container\s+images?\s*\(in\s+the\s+repository\)",  # fabricated containers
        ]
        combined = re.compile("|".join(_FABRICATION_PATTERNS), re.IGNORECASE)
        # Fabricated subsection headings the LLM invents
        _FABRICATED_SUBSECTIONS = re.compile(
            r"^#{2,4}\s*(?:"
            r"(?:Validation|Verification)\s+of\s+Automated\s+Components"
            r"|Manual\s+Spot[- ]?Checks"
            r"|Inter[- ]?Rater\s+Reliability"
            r"|Gold\s+Standards?\s+and\s+Performance"
            r"|Uncertainty\s+Quantification"
            r"|Data\s+and\s+Code\s+Availability"
            r"|Reproducibility\s+Notes"
            r"|Summary\s+of\s+Reported\s+Corpus\s+Counts"
            r"|PRISMA[- ]Style\s+Corpus\s+Flow"
            r")",
            re.IGNORECASE | re.MULTILINE,
        )
        # Additional patterns for non-methodology sections
        _GLOBAL_FABRICATION_PATTERNS = [
            r"trained\s+human\s+annotators?\s+validated",
            r"blinded\s+(?:assessment|evaluation)",
            r"participants?\s+were\s+recruited",
            r"(?:IRB|ethics\s+committee)\s+approval",
            r"wet[- ]?lab\s+experiment(?:s|ation)?",
            r"wet[- ]?lab\s+validation",
            r"informed\s+consent\s+was\s+obtained",
            # Rule 7: Absolute AI Identity — no fabricated human verification
            r"verified\s+by\s+(?:a\s+)?human\s+(?:team|expert|reviewer)",
            r"human[- ]?curated",
            r"(?:senior|lead)\s+author\s+adjudicat(?:ed|ion)",
            r"cross[- ]?checked\s+by\s+independent\s+researchers",
            r"domain\s+expert\s+(?:review|validation|verification)",
            r"human\s+(?:verification|validation)\s+(?:step|process|phase)",
            # Rule 5: Zero-shot statistical fabrication
            r"pooled\s+(?:mean|effect\s+size|estimate)\s*[=:]\s*[\d.-]+",
            r"(?:95|99)%?\s*CI\s*[\[=(]\s*[\d.-]+\s*[,;–-]\s*[\d.-]+\s*[\])]",
            r"I[²2]\s*[=:]\s*\d+(?:\.\d+)?%?",
            r"Q[- ]?(?:statistic|test)?\s*[=:]\s*\d+",
            r"tau[²2]\s*[=:]\s*[\d.]+",
            r"k\s*[=:]\s*\d+\s+stud(?:y|ies)",
            r"random[- ]?effects?\s+model\s+(?:yielded|produced|showed|revealed|indicated)",
            r"funnel\s+plot\s+(?:analysis|inspection|examination)\s+(?:revealed|showed|indicated|suggested)",
            r"forest\s+plot\s+(?:revealed|showed|indicated|illustrates)",
            # Fabricated supplementary materials (any section)
            r"(?:Supplementary|Supporting)\s+(?:Figure|Table|Material)s?\s+S?\d",
            r"(?:see|as\s+shown\s+in)\s+(?:Figure|Table)\s+\d",
            # Rule 8: Phantom figures/tables/supplements
            r"(?:Table|Figure)\s+\d+\s*[.:]\s*\w",  # "Table 1: ..." or "Figure 1. ..."
            r"Panel\s+[A-D]\s*[.:]\s*\w",  # "Panel A: ..."
            r"Methods?\s+Supplement",
            r"(?:Supplementary|Supporting)\s+(?:Information|Materials?|Data|Methods)",
            r"(?:Supplementary|Supporting)\s+(?:Exclusion|Inclusion)\s+Table",
            r"(?:Supplementary|Supporting)\s+Table\s+S?\d",
            r"(?:Appendix|Annexe?)\s+[A-Z0-9]",
            r"Python\s+scripts?\s+(?:are|were|is)\s+(?:provided|available|archived|deposited)",
            r"random\s+seeds?\s+(?:are|were|is)\s+(?:provided|available|archived|set\s+to)",
            r"code\s+(?:is|are|was|were)\s+(?:available|deposited|archived)\s+(?:at|on|in)",
            # Rule 7: "human adjudication" variant (but NOT "no human adjudication")
            r"(?<!no )human\s+adjudication",
            r"flagg(?:ed|ing)\s+(?:for|to)\s+human\s+(?:review|adjudication|inspection)",
            # Rule 7: "split personality" — LLM reverts to human roleplay in methodology
            r"human[- ]in[- ]the[- ]loop",
            r"two\s+authors?\s+independently\s+(?:reviewed|screened|assessed|evaluated|coded)",
            r"reconciled?\s+(?:through|via)\s+(?:discussion|consensus|deliberation)",
            r"until\s+consensus\s+was\s+reached",
            r"(?:first|second|third)\s+author\s+(?:reviewed|screened|coded|extracted)",
            r"disagreements?\s+(?:were|was)\s+resolved\s+(?:through|via|by)\s+(?:discussion|a\s+third)",
            r"(?:two|three|multiple)\s+(?:investigators?|researchers?)\s+independently",
            # Meta-commentary: LLM describes its own bibliography / citation process
            r"(?:additional\s+)?bibliographic\s+entries\s+(?:from|in)\s+the\s+reference\s+list",
            r"(?:are|were|have\s+been)\s+(?:now\s+)?integrated\s+into\s+the\s+(?:Methods|Methodology|Discussion|Results|text|narrative)",
            r"(?:references?|citations?)\s+(?:listed|appearing|included)\s+(?:above|below|in\s+the\s+bibliography)",
            r"(?:the\s+)?(?:above|following|remaining)\s+(?:references?|citations?|sources?)\s+(?:are|were)\s+(?:now\s+)?(?:woven|incorporated|integrated|added)",
        ]
        global_combined = re.compile("|".join(_GLOBAL_FABRICATION_PATTERNS), re.IGNORECASE)

        sanitized = {}
        total_removed = 0
        for heading, content in draft.items():
            is_methodology = heading.lower().startswith("method")

            # Phase 1: Remove entire fabricated subsections (Methodology only)
            if is_methodology:
                parts = re.split(r"(^#{2,4}\s+.+$)", content, flags=re.MULTILINE)
                clean_parts = []
                skip_until_next_heading = False
                for part in parts:
                    if re.match(r"^#{2,4}\s+", part):
                        if _FABRICATED_SUBSECTIONS.match(part):
                            skip_until_next_heading = True
                            total_removed += 1
                            logger.info("Fabrication sanitizer: removed subsection '%s'", part.strip())
                            continue
                        else:
                            skip_until_next_heading = False
                    if not skip_until_next_heading:
                        clean_parts.append(part)
                content = "".join(clean_parts)

            # Phase 2: Remove individual fabrication sentences (ALL sections)
            sentences = re.split(r"(?<=[.!?])\s+", content)
            # Methodology uses the full pattern set; other sections use the global set
            pattern = combined if is_methodology else global_combined
            clean = [s for s in sentences if not pattern.search(s)]
            removed = len(sentences) - len(clean)
            if removed > 0:
                total_removed += removed
                logger.info(
                    "Fabrication sanitizer: removed %d sentences from '%s'",
                    removed, heading,
                )
            sanitized[heading] = " ".join(clean)
        if total_removed > 0:
            logger.info("Fabrication sanitizer: %d total items removed", total_removed)

        # Phase 3: Strip editorial artifacts + CoT scaffolding leaked from prompts
        _EDITORIAL_PATTERNS = [
            r"Also check for:.*?(?:topic sentences|cite_keys|thematic synthesis)[.\s]*",
            r"(?:Furthermore|Moreover|Additionally)[,/].*?(?:Furthermore|Moreover|Additionally)",
            r"\[?(?:EDITORIAL|INTERNAL|TODO|NOTE)[\]:][^\n]*",
            # CoT scaffolding phrases the LLM copies from structural guidance
            r"^(?:Topic\s+sentence|Synthesis\s+and\s+comparison|Sources?\s+of\s+divergence|Interim\s+conclusion|Key\s+(?:finding|insight|observation)|Summary\s+of\s+(?:evidence|findings))\s*:\s*",
            r"^(?:Opening|Closing|Transition)\s+(?:sentence|paragraph)\s*:\s*",
            r"^(?:Evidence|Claim|Argument|Counter-?argument|Rebuttal)\s*:\s*",
        ]
        editorial_re = re.compile("|".join(_EDITORIAL_PATTERNS), re.IGNORECASE | re.MULTILINE)
        for heading in list(sanitized.keys()):
            content = sanitized[heading]
            cleaned = editorial_re.sub("", content)
            # Also fix double-bracketed citations: [Author [Author, Year], Year]
            cleaned = re.sub(
                r"\[([A-Z][a-z]+)\s+\[\1,\s*(\d{4})\],\s*\d{4}\]",
                r"[\1, \2]",
                cleaned,
            )
            # Fix residual double-bracket patterns: word [Author, Year] inside [...]
            cleaned = re.sub(
                r"\[([^\[\]]*?)\s+\[([A-Z][a-z]+,\s*\d{4})\]([^\[\]]*?)\]",
                r"[\1 \3] [\2]",
                cleaned,
            )
            if cleaned != content:
                sanitized[heading] = cleaned.strip()
                logger.info("Sanitizer: cleaned editorial artifacts from '%s'", heading)

        # Phase 3b: Fix wrong model names in methodology
        # LLMs often hallucinate "gpt-5" or "gpt-4" regardless of the actual model used.
        actual_model = self.artifacts.get("pipeline_metadata", {}).get("model", "")
        if actual_model:
            _WRONG_MODELS = re.compile(
                r"\b(?:GPT[- ]?5(?:-mini)?|GPT[- ]?4[oa]?(?:-mini)?|GPT[- ]?3\.5|"
                r"Claude[- ]?(?:3\.5|3|2)|Gemini[- ]?(?:1\.5|2\.0|2\.5)[- ]?(?:Pro|Flash)?|"
                r"Llama[- ]?\d|Mistral[- ]?(?:Large|Medium))\b",
                re.IGNORECASE,
            )
            for heading in list(sanitized.keys()):
                if heading.lower().startswith("method"):
                    content = sanitized[heading]
                    # Only replace if the wrong model is mentioned and it's not the actual model
                    matches = _WRONG_MODELS.findall(content)
                    for wrong in matches:
                        if wrong.lower().replace("-", "").replace(" ", "") != actual_model.lower().replace("-", "").replace(" ", ""):
                            content = content.replace(wrong, actual_model)
                            logger.info("Model identity fix: replaced '%s' with '%s'", wrong, actual_model)
                    sanitized[heading] = content

        # Phase 4: Strip phantom figure/table headers (all sections)
        # Remove lines like "Table 1: ...", "Figure 1. ...", "Panel A: ..."
        _PHANTOM_HEADER = re.compile(
            r"^(?:Table|Figure)\s+\d+\s*[.:].+$",
            re.MULTILINE,
        )
        _PHANTOM_PANEL = re.compile(
            r"^Panel\s+[A-D]\s*[.:].+$",
            re.MULTILINE,
        )
        for heading in list(sanitized.keys()):
            content = sanitized[heading]
            cleaned = _PHANTOM_HEADER.sub("", content)
            cleaned = _PHANTOM_PANEL.sub("", cleaned)
            # Clean up resulting blank lines
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
            if cleaned != content:
                sanitized[heading] = cleaned.strip()
                total_removed += 1
                logger.info("Sanitizer: removed phantom figure/table headers from '%s'", heading)

        # Phase 5: Strip revision artifacts (LLM thinks it's revising a previous draft)
        _REVISION_PATTERNS = re.compile(
            r"(?:"
            r"[Ww]e\s+have\s+revised\s+(?:the|this)\s+manuscript"
            r"|[Tt]his\s+(?:revised|updated)\s+(?:manuscript|version)"
            r"|[Ii]n\s+(?:this|the)\s+revised?\s+(?:version|manuscript)"
            r"|[Tt]he\s+(?:manuscript|paper)\s+has\s+been\s+revised"
            r"|[Ww]e\s+now\s+present\s+a\s+(?:revised|focused)"
            r"|[Rr]ather\s+than\s+asserting\s+broad.*?this\s+manuscript\s+now\s+presents"
            r"|[Tt]he\s+(?:Results|Discussion|Introduction)\s+(?:has|have)\s+been\s+revised"
            r")[^.]*\."
        )
        for heading in list(sanitized.keys()):
            content = sanitized[heading]
            cleaned = _REVISION_PATTERNS.sub("", content)
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
            if cleaned != content:
                sanitized[heading] = cleaned
                logger.info("Sanitizer: stripped revision artifacts from '%s'", heading)

        # Phase 6: Strip bare [Surname] citations without year
        _BARE_CITE = re.compile(r"\[([A-Z][a-zA-Z_]+)\]")
        _KNOWN_BRACKETS = {
            "Figure", "Table", "Supplementary", "Supporting", "Appendix",
            "Panel", "Section", "Chapter", "Equation", "Box", "Note",
        }
        for heading in list(sanitized.keys()):
            content = sanitized[heading]
            cleaned = _BARE_CITE.sub(
                lambda m: m.group(0) if m.group(1) in _KNOWN_BRACKETS else "",
                content,
            )
            # Ghost citation cleanup: fix whitespace artifacts from citation removal
            cleaned = re.sub(r"\s+\.", ".", cleaned)
            cleaned = re.sub(r"\s+,", ",", cleaned)
            cleaned = re.sub(r",\s*,", ",", cleaned)
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
            if cleaned != content:
                sanitized[heading] = cleaned
                logger.info("Sanitizer: stripped bare [Surname] citations from '%s'", heading)

        return sanitized

    @staticmethod
    def _enforce_citation_density(draft: dict[str, str]) -> dict[str, str]:
        """Whitelist enforcer: remove paragraphs that make empirical claims without citations.

        Every paragraph in evidence-bearing sections (Introduction, Related Work,
        Results, Discussion) must contain at least one [Author, Year] citation
        OR be purely structural (topic sentence, transition). Paragraphs with
        empirical language but no citation are stripped — this prevents the LLM
        from injecting uncited fabricated claims.

        Methodology, Limitations, and Conclusion are exempt (self-referential).
        """
        _CITATION_RE = re.compile(r"\[[A-Z][a-zA-Z\-']+(?:\s+et\s+al\.?)?,\s*\d{4}")
        # Empirical markers — phrases that signal a factual claim needing a citation
        _EMPIRICAL_RE = re.compile(
            r"(?:"
            r"(?:studies?|research|experiments?|trials?|investigations?)\s+(?:have\s+)?(?:shown|demonstrated|found|revealed|reported|indicated|suggested|confirmed|established)"
            r"|(?:evidence|data|findings|results)\s+(?:suggest|indicate|show|demonstrate|reveal|confirm|support)"
            r"|(?:has|have|was|were)\s+(?:found|shown|demonstrated|reported|observed|associated|linked|correlated)"
            r"|(?:according\s+to|consistent\s+with|in\s+line\s+with|contrary\s+to)"
            r"|(?:approximately|roughly|nearly|about)\s+\d"
            r"|\d+(?:\.\d+)?%"
            r"|\d+(?:\.\d+)?\s*(?:fold|times|percent)"
            r"|(?:increased|decreased|reduced|enhanced|improved|elevated|diminished)\s+(?:by|to|from)\s+\d"
            r"|(?:higher|lower|greater|less|more|fewer)\s+than"
            r"|(?:prevalence|incidence|mortality|morbidity|risk|odds|hazard)\s+(?:of|ratio|rate)"
            r")",
            re.IGNORECASE,
        )
        # Sections that MUST have citations for empirical claims
        _EVIDENCE_SECTIONS = {"Introduction", "Related Work", "Results", "Discussion"}

        enforced = {}
        total_removed = 0
        for heading, content in draft.items():
            if heading not in _EVIDENCE_SECTIONS:
                enforced[heading] = content
                continue

            paragraphs = re.split(r"\n\n+", content)
            kept = []
            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                has_citation = bool(_CITATION_RE.search(para))
                has_empirical = bool(_EMPIRICAL_RE.search(para))
                word_count = len(para.split())

                if has_citation:
                    # Has citation — always keep
                    kept.append(para)
                elif has_empirical and word_count > 40:
                    # Empirical claim without citation in a substantial paragraph — remove
                    total_removed += 1
                    logger.info(
                        "Citation enforcer: removed uncited empirical paragraph from '%s' (%d words): %s...",
                        heading, word_count, para[:80],
                    )
                else:
                    # Short or structural paragraph (topic sentence, transition) — keep
                    kept.append(para)

            enforced[heading] = "\n\n".join(kept)

        if total_removed > 0:
            logger.info("Citation enforcer: removed %d uncited empirical paragraphs total", total_removed)

        return enforced

    def _phase6_revise_and_verify(self) -> None:
        logger.info("Phase 6: REVISE & VERIFY")
        self.display.phase_start(6, "Revise & Verify")

        draft = self.artifacts.get("zero_draft", {})

        # Sanitize fabrication markers before revision (more reliable than prompts)
        draft = self._sanitize_fabrication(draft)

        # Whitelist enforcement: remove paragraphs with empirical claims but no citations
        draft = self._enforce_citation_density(draft)

        # Fix truncated sections — remove trailing incomplete sentences
        for heading, content in draft.items():
            stripped = content.rstrip()
            if stripped and stripped[-1] not in ".!?\"')":
                # Find the last complete sentence
                last_end = max(stripped.rfind("."), stripped.rfind("!"), stripped.rfind("?"))
                if last_end > len(stripped) * 0.8:  # only trim if near the end
                    draft[heading] = stripped[: last_end + 1]
                    logger.info("Trimmed truncated ending from section '%s'", heading)

        self.artifacts["zero_draft"] = draft

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        ref_list_text = self.artifacts.get("ref_list_text", "[]")

        full_text = "\n\n".join(
            f"## {heading}\n{content}" for heading, content in draft.items()
        )

        # Critique-revise loop: self-critique → targeted revision
        # More effective than generic revision — the LLM identifies its own
        # weaknesses first, then fixes them specifically.

        # Build weakness revision hint from prior review feedback
        weakness_hint = self.artifacts.get("weakness_summary", "")
        weakness_revision_hint = ""
        if weakness_hint:
            weakness_revision_hint = (
                f"\nQUALITY IMPROVEMENT GUIDANCE (from prior review feedback):\n"
                f"{weakness_hint}\n"
                f"Consider these known weaknesses in your critique.\n"
            )

        # F1: Use compact ref list for revision
        ref_list_compact_text = self.artifacts.get("ref_list_compact_text", ref_list_text)

        # Mechanical citation dominance check — detect single-source fixation
        citation_dominance_hint = ""
        all_cites = re.findall(r"\[([A-Z][a-z]+(?:\s+et\s+al\.?)?,\s*\d{4}[a-z]?)\]", full_text)
        if all_cites:
            from collections import Counter
            cite_counts = Counter(all_cites)
            total_cites = len(all_cites)
            top_cite, top_count = cite_counts.most_common(1)[0]
            dominance_pct = top_count / total_cites * 100
            if dominance_pct > 25 and top_count > 5:
                citation_dominance_hint = (
                    f"\nCITATION DOMINANCE WARNING: [{top_cite}] is cited {top_count} times "
                    f"({dominance_pct:.0f}% of all citations). This paper over-relies on a single source. "
                    f"Reduce references to [{top_cite}] by replacing some with other sources from the "
                    f"reference list, or by synthesizing the claims into broader thematic statements.\n"
                )
                logger.warning(
                    "Citation dominance: [%s] cited %d/%d times (%.0f%%)",
                    top_cite, top_count, total_cites, dominance_pct,
                )
                self.display.step(f"Citation dominance: [{top_cite}] = {dominance_pct:.0f}% of citations")

        # Step 1: Self-critique — identify the 5 biggest weaknesses
        self._delay()
        self._check_interrupt()
        self.display.step("Self-critique: identifying weaknesses...")
        self.display.tick()

        critique_system = self._prompts["phase6_self_critique"]

        tone_criteria = ""
        if self.config.quality_level != "lite":
            tone_criteria = (
                "Also check for: bullet points in prose, monotonous transitions "
                "(Furthermore/Moreover/Additionally), paper-by-paper summaries instead "
                "of thematic synthesis, promotional language, vague claims without "
                "specific cite_keys, missing topic sentences."
            )

        critique_prompt = f"""{self._paper_context()}
{weakness_revision_hint}
{citation_dominance_hint}
REFERENCE LIST (valid cite_keys):
{ref_list_compact_text[:4000]}

Draft to critique:
{full_text[:14000]}

Evaluate this paper as a unified whole. Check whether:
- The paper answers its stated research questions
- Each section fulfils its role (Introduction frames, Discussion interprets, etc.)
- The argument flows logically from section to section
- Tone is consistently scholarly — no promotional language, no monotonous transitions
  (Furthermore/Moreover/Additionally), no word or phrase repetition across paragraphs
- Claims are specific and cited, not vague
{tone_criteria}

Return JSON with:
- "weaknesses": list of exactly 5 objects, each with:
  - "section": which section has the problem
  - "issue": specific description of the weakness (cite exact phrases if possible)
  - "severity": "high" or "medium"
  - "fix": concrete instruction for how to fix it
- "hallucinated_citations": list of any cite_keys used in the text that are NOT in the reference list"""

        try:
            critique_result = self.llm.generate_json(critique_system, critique_prompt, temperature=0.3)
        except Exception as e:
            logger.warning("Self-critique LLM call failed — skipping revision: %s", e)
            self.display.step(f"Self-critique failed ({e}) — skipping revision")
            critique_result = {}
        weaknesses = critique_result.get("weaknesses", [])
        hallucinated = critique_result.get("hallucinated_citations", [])

        # Strip hallucinated citations found during critique
        if hallucinated:
            logger.info("Critique found %d hallucinated citations — stripping", len(hallucinated))
            for cite_key in hallucinated:
                escaped = re.escape(cite_key)
                for section_name in draft:
                    draft[section_name] = re.sub(
                        rf"\s*{escaped}\s*[,;]?\s*", " ", draft[section_name]
                    )
                    # Clean up empty bracket pairs left behind
                    draft[section_name] = re.sub(r"\[\s*[,;]?\s*\]", "", draft[section_name])
            self.display.step(f"Stripped {len(hallucinated)} hallucinated citations")

        if not weaknesses:
            self.display.step("No weaknesses found — skipping revision")
        else:
            # Format critique for the revision prompt
            critique_text = "\n".join(
                f"{i+1}. [{w.get('severity', 'medium').upper()}] Section '{w.get('section', '?')}': "
                f"{w.get('issue', '')} → FIX: {w.get('fix', '')}"
                for i, w in enumerate(weaknesses[:5])
            )
            self.display.step(f"Found {len(weaknesses)} weaknesses — revising...")
            logger.info("Critique found %d weaknesses:\n%s", len(weaknesses), critique_text)

            # Step 2: Targeted revision — always per-section for speed & progress
            self._delay()
            self._check_interrupt()
            self.display.tick()

            revision_system = self._prompts["phase6_targeted_revision"]

            # Group weaknesses by section — use fuzzy matching because the
            # critique often returns decorated names like "Methodology — sampling
            # & scope" instead of just "Methodology".
            weaknesses_by_section: dict[str, list] = {}
            draft_sections_lower = {s.lower(): s for s in draft}
            for w in weaknesses:
                raw_sec = w.get("section", "")
                # Try exact match first
                matched = raw_sec if raw_sec in draft else None
                if not matched:
                    # Try case-insensitive prefix match: "methodology" matches
                    # "Methodology — sampling & scope"
                    raw_lower = raw_sec.lower().split("—")[0].split("–")[0].split(":")[0].strip()
                    raw_lower = raw_lower.split("(")[0].strip()  # strip parenthetical
                    raw_lower = raw_lower.split("&")[0].strip()  # "Results & Discussion" → "Results"
                    for ds_lower, ds_orig in draft_sections_lower.items():
                        if ds_lower.startswith(raw_lower) or raw_lower.startswith(ds_lower):
                            matched = ds_orig
                            break
                if not matched:
                    # Last resort: assign cross-cutting weaknesses to the first
                    # section (usually Introduction or Methodology)
                    matched = list(draft.keys())[0] if draft else None
                if matched:
                    weaknesses_by_section.setdefault(matched, []).append(w)

            sections_to_revise = [
                s for s in draft if s in weaknesses_by_section
            ]
            total_sections = len(sections_to_revise)
            self.display.step(f"Revising {total_sections} sections with weaknesses...")

            for si, section_name in enumerate(sections_to_revise, 1):
                section_content = draft[section_name]
                section_weaknesses = weaknesses_by_section[section_name]
                self._delay()
                self._check_interrupt()
                self.display.step(f"  Revising [{si}/{total_sections}]: {section_name}...")
                self.display.tick()

                section_critique = "\n".join(
                    f"- {w.get('issue', '')} → FIX: {w.get('fix', '')}"
                    for w in section_weaknesses
                )
                prompt = f"""{self._paper_context()}

SPECIFIC WEAKNESSES TO FIX in the '{section_name}' section:
{section_critique}

REFERENCE LIST (the ONLY valid citations — cite by cite_key):
{ref_list_compact_text[:4000]}

Section '{section_name}':
{section_content[:4000]}

Address each weakness above. Preserve everything that is already good.
The revision must fit seamlessly into the paper — maintain consistent terminology,
tone, and level of formality across the entire document. Vary sentence openings and
transition words; avoid repeating the same phrasing found in other sections.
Do NOT add new citations — only use cite_keys from the reference list.
Do NOT invent methodology validation (gold standards, precision/recall scores, kappa values, human annotators/reviewers).
Do NOT fabricate PRISMA flow counts, data repositories, supplementary materials, or Zenodo deposits.
Do NOT remove existing [Author, Year] in-text citations.
Ensure all sentences are grammatically complete — no fragments or garbled syntax.
Return JSON with:
- "revised_content": the revised section text
- "changes_made": list of which weaknesses you addressed and how"""

                try:
                    result = self.llm.generate_json(revision_system, prompt, temperature=0.3)
                except Exception as e:
                    logger.warning("Revision of '%s' failed — keeping original: %s", section_name, e)
                    self.display.step(f"  Revision of {section_name} failed — keeping original")
                    continue
                revised_content = result.get("revised_content", "")
                if revised_content:
                    original_words = len(draft[section_name].split())
                    revised_words = len(revised_content.split())
                    if original_words > 0 and revised_words < original_words * 0.8:
                        logger.warning(
                            "Revision of '%s' dropped from %d to %d words — keeping original",
                            section_name, original_words, revised_words,
                        )
                    else:
                        draft[section_name] = revised_content
                        self.display.section_done(section_name, revised_content)

        current_text = "\n\n".join(
            f"## {heading}\n{content}" for heading, content in draft.items()
        )

        # F6: Mechanical verification (no LLM call needed)
        self.display.step("Mechanical verification...")

        # Compute metrics mechanically
        total_words = sum(len(content.split()) for content in draft.values())
        abstract_text = self.artifacts.get("abstract", "")

        # Build valid cite_keys from ref_list
        try:
            ref_list_parsed = json.loads(self.artifacts.get("ref_list_text", "[]"))
        except (json.JSONDecodeError, TypeError):
            ref_list_parsed = []
        valid_cite_keys = {r.get("cite_key", "").lower() for r in ref_list_parsed if isinstance(r, dict)}

        # Collect cited keys from draft text
        cited_in_text = self._collect_cited_keys(draft, abstract_text)
        unique_citations = len(cited_in_text)

        # E4: Detect hallucinated citations (cited but not in valid set)
        hallucinated = []
        for ck in cited_in_text:
            if ck not in valid_cite_keys:
                hallucinated.append(ck)

        # E4: Strip hallucinated citations from draft
        if hallucinated:
            self.display.step(f"Removing {len(hallucinated)} hallucinated citations...")
            # Build patterns to remove
            for hck in hallucinated:
                # The cited key is lowered; find original-case version in text
                escaped = re.escape(hck)
                for section_name_h, content_h in draft.items():
                    draft[section_name_h] = re.sub(escaped, "", content_h, flags=re.IGNORECASE)
                    draft[section_name_h] = re.sub(r"\([;,\s]*\)", "", draft[section_name_h])
                    draft[section_name_h] = re.sub(r"\s{2,}", " ", draft[section_name_h])
            self.display.step(f"Removed {len(hallucinated)} hallucinated citations")
            # Recount
            cited_in_text = self._collect_cited_keys(draft, abstract_text)
            unique_citations = len(cited_in_text)

        ready = total_words >= self.config.min_total_words and unique_citations >= 5

        verification = {
            "quality_score": None,  # deferred to Phase 7 claim verification
            "word_count_estimate": total_words,
            "reference_count": unique_citations,
            "hallucinated_citations": hallucinated,
            "issues": [],
            "ready_to_submit": ready,
        }
        if total_words < self.config.min_total_words:
            verification["issues"].append(f"Word count {total_words} below minimum {self.config.min_total_words}")
        if unique_citations < 5:
            verification["issues"].append(f"Only {unique_citations} unique citations found")

        self.artifacts["verification"] = verification
        self.artifacts["final_paper"] = draft
        self._log_artifact("verification", verification)

        self.display.step(f"Verification: {total_words} words, {unique_citations} citations, ready={ready}")
        self.display.phase_done(6)

    # ------------------------------------------------------------------
    # Phase 7 (6.5): Verify & Harden
    # ------------------------------------------------------------------

    def _phase7_verify_and_harden(self) -> None:
        """Post-draft verification: reference verification, claim grounding, citation cleanup."""
        logger.info("Phase 7: VERIFY & HARDEN")
        self.display.phase_start(7, "Verify & Harden")

        draft = self.artifacts.get("final_paper", self.artifacts.get("zero_draft", {}))
        memos = self.artifacts.get("reading_memos", {})
        candidates = self.artifacts.get("candidate_papers", [])
        cand_by_id = {c["paper_id"]: c for c in candidates}
        custom_content = self.artifacts.get("custom_source_content", {})

        # Build references list (same as _submit builds)
        references = []
        for memo in memos.values():
            pid = memo.get("paper_id", "")
            cand = cand_by_id.get(pid, {})
            src = custom_content.get(pid, {})
            ref = {
                "ref_id": pid,
                "title": memo.get("title", ""),
                "authors": src.get("authors") or cand.get("authors") or [],
                "year": cand.get("year"),
                "doi": src.get("doi") or cand.get("doi") or "",
            }
            references.append(ref)

        # Build cite_key set from ref_list_text artifact
        ref_list = []
        try:
            ref_list = json.loads(self.artifacts.get("ref_list_text", "[]"))
        except (json.JSONDecodeError, TypeError):
            pass
        ref_cite_keys = {r.get("cite_key", "") for r in ref_list if isinstance(r, dict)}

        # ── 7a: Reference verification (external APIs) ──
        self.display.step(f"Verifying {len(references)} references against external databases...")
        self.display.tick()

        from .reference_verifier import ReferenceVerifier, CONFIDENCE_REMOVE
        verifier = ReferenceVerifier(mailto=self.owner_email or "api@agentpub.org")
        try:
            loop = asyncio.get_running_loop()
            # Already inside an async context — use nest_asyncio to allow nesting
            import nest_asyncio
            nest_asyncio.apply(loop)
            ref_report = loop.run_until_complete(verifier.verify_all(references))
        except RuntimeError:
            # No event loop running — create one
            loop = asyncio.new_event_loop()
            try:
                ref_report = loop.run_until_complete(verifier.verify_all(references))
            finally:
                loop.close()

        self.display.step(
            f"References: {ref_report.references_verified} verified, "
            f"{ref_report.references_uncertain} uncertain, "
            f"{ref_report.references_failed} failed"
        )

        # Remove unverifiable references and update metadata for verified ones
        verified_ref_ids = set()
        removed_cite_keys = set()
        for vr in ref_report.results:
            if vr.confidence < CONFIDENCE_REMOVE:
                # Find and remove from ref_list
                removed_cite_keys.add(vr.ref_id)
                logger.info("Removing unverifiable reference: %s (confidence: %.2f)", vr.title, vr.confidence)
            else:
                verified_ref_ids.add(vr.ref_id)
                # Update reference metadata with canonical data if available
                if vr.canonical_data:
                    for ref in references:
                        if ref["ref_id"] == vr.ref_id:
                            if vr.canonical_data.get("doi") and not ref.get("doi"):
                                ref["doi"] = vr.canonical_data["doi"]
                            if vr.canonical_data.get("year") and not ref.get("year"):
                                ref["year"] = vr.canonical_data["year"]

        if removed_cite_keys:
            self.display.step(f"Removed {len(removed_cite_keys)} unverifiable references")
            # Strip citations to removed references from the draft text
            draft = self._strip_dangling_citations(draft, removed_cite_keys, ref_list)

        # ── 7b: Claim decomposition & grounding ──
        self.display.step("Decomposing claims and checking evidence grounding...")
        self.display.tick()

        from .claim_verifier import ClaimVerifier, MAX_UNSUPPORTED_RATIO
        claim_verifier = ClaimVerifier(self.llm)
        # Per-section decomposition: smaller prompts complete faster on all
        # models (especially thinking models like Gemini 2.5 and GPT-5).
        self.display.step("  Decomposing claims (per-section)...")
        all_claims = []
        for si, (sec_name, sec_text) in enumerate(draft.items(), 1):
            self.display.tick()
            self._check_interrupt()
            self._delay()
            self.display.step(f"    [{si}/{len(draft)}] Analyzing {sec_name}...")
            claims = claim_verifier.decompose_section(sec_name, sec_text)
            all_claims.extend(claims)
            self.display.step(f"    {sec_name}: {len(claims)} claims")
        self.display.step(f"  {len(all_claims)} claims extracted from {len(draft)} sections")
        claim_report = claim_verifier.verify_claims(all_claims, verified_ref_ids, ref_cite_keys)

        self.display.step(
            f"Claims: {claim_report.claims_supported} supported, "
            f"{claim_report.claims_unsupported} unsupported, "
            f"{claim_report.claims_uncertain} uncertain "
            f"({claim_report.unsupported_ratio:.0%} unsupported ratio)"
        )

        # If too many unsupported claims, try to fix them
        if claim_report.unsupported_ratio > MAX_UNSUPPORTED_RATIO and claim_report.unsupported_claims:
            self.display.step("Too many unsupported claims — requesting LLM fix...")
            self.display.tick()
            unsupported_list = "\n".join(f"- {c}" for c in claim_report.unsupported_claims[:10])
            # F1: Use compact ref list for claim fixes
            ref_list_text = self.artifacts.get("ref_list_compact_text", self.artifacts.get("ref_list_text", "[]"))

            system = self._prompts.get("phase6_5_verification", "You are a rigorous fact-checker.")
            fix_sections = [
                (sn, sc) for sn, sc in draft.items()
                if any(r.claim.section == sn and r.status == "unsupported" for r in claim_report.results)
            ]
            for fi, (section_name, section_content) in enumerate(fix_sections, 1):
                section_claims = [
                    r for r in claim_report.results
                    if r.claim.section == section_name and r.status == "unsupported"
                ]

                self._delay()
                self.display.step(f"  [{fi}/{len(fix_sections)}] Fixing {section_name} ({len(section_claims)} unsupported claims)...")
                claims_text = "\n".join(f"- {r.claim.text}" for r in section_claims)
                prompt = f"""{self._paper_context()}

The following empirical claims in the '{section_name}' section lack citations:

{claims_text}

Either:
1. Add a citation from the reference list if one supports the claim
2. Rephrase as "further research is needed" or "it has been suggested that..."
3. Remove the claim if it cannot be supported

CRITICAL RULES:
- PRESERVE all existing [Author, Year] in-text citations — do NOT remove or rephrase them
- Maintain consistent tone and terminology with the rest of the paper
- Do NOT invent methodology validation (gold standards, precision/recall, kappa scores, annotators)
- Do NOT fabricate PRISMA flow counts, data repositories, or supplementary materials
- Do NOT fabricate pooled statistics (pooled means, CIs, I², Q, tau², k=N studies) — you have no statistical software
- Do NOT claim results were "verified by a human team" or "cross-checked by independent researchers"
- Do NOT reference "Figure 1", "Table 2", or "Supplementary Table S1" — these do not exist
- Keep the section grammatically correct — no sentence fragments or garbled syntax

REFERENCE LIST (the ONLY valid citations):
{ref_list_text[:6000]}

Current section:
{section_content[:6000]}

Return JSON: {{"revised_content": "the full revised section text"}}"""

                try:
                    result = self.llm.generate_json(system, prompt, temperature=0.1)
                except Exception as e:
                    logger.warning("Citation fix for '%s' failed — keeping original: %s", section_name, e)
                    self.display.step(f"  Citation fix for {section_name} failed — skipping")
                    continue
                revised = result.get("revised_content", "")
                if revised and len(revised.split()) >= len(section_content.split()) * 0.8:
                    draft[section_name] = revised
                    self.display.section_done(section_name, revised)

        # ── 7b2: Post-fix re-sanitize and ratio check ──
        # Strip any fabricated statistics or human-verification claims the
        # LLM may have introduced during the unsupported-claim fix pass.
        draft = self._sanitize_fabrication(draft)

        # Re-verify claims after fixes — reject if still above threshold
        if claim_report.unsupported_ratio > MAX_UNSUPPORTED_RATIO:
            self.display.step("Re-verifying claims after fix pass...")
            all_claims_2 = []
            for sec_name, sec_text in draft.items():
                self.display.tick()
                self._check_interrupt()
                self._delay()
                claims = claim_verifier.decompose_section(sec_name, sec_text)
                all_claims_2.extend(claims)
            claim_report_2 = claim_verifier.verify_claims(all_claims_2, verified_ref_ids, ref_cite_keys)
            self.display.step(
                f"Re-verification: {claim_report_2.unsupported_ratio:.0%} unsupported "
                f"(threshold: {MAX_UNSUPPORTED_RATIO:.0%})"
            )
            if claim_report_2.unsupported_ratio <= MAX_UNSUPPORTED_RATIO:
                claim_report = claim_report_2
            else:
                # Use updated report for final scoring even if still above threshold
                claim_report = claim_report_2
                logger.warning(
                    "Unsupported claim ratio %.0f%% still exceeds threshold %.0f%% after fix pass",
                    claim_report.unsupported_ratio * 100,
                    MAX_UNSUPPORTED_RATIO * 100,
                )
                self.display.step(
                    f"WARNING: unsupported_claim_ratio={claim_report.unsupported_ratio:.0%} "
                    f"exceeds {MAX_UNSUPPORTED_RATIO:.0%} threshold"
                )

        # ── 7c: Final citation cleanup ──
        self.display.step("Final citation cleanup...")
        self.display.tick()

        # Ensure minimum 8 verified references remain
        remaining_refs = [r for r in references if r["ref_id"] not in removed_cite_keys]
        if len(remaining_refs) < 8:
            needed = 8 - len(remaining_refs)
            self.display.step(f"Only {len(remaining_refs)} verified references — searching for {needed} more...")
            # Search for additional references using existing search terms
            brief = self.artifacts.get("research_brief", {})
            for query in brief.get("search_terms", [])[:2]:
                self._delay()
                self._check_interrupt()
                try:
                    hits = search_academic(query, limit=5, mailto=self.owner_email or None)
                    for hit in hits:
                        if len(remaining_refs) >= 8:
                            break
                        remaining_refs.append({
                            "ref_id": f"verify_{len(remaining_refs)}",
                            "title": hit.get("title", ""),
                            "authors": hit.get("authors", []),
                            "year": hit.get("year"),
                            "doi": hit.get("doi", ""),
                        })
                except Exception:
                    pass
                if len(remaining_refs) >= 8:
                    break

        # F6: Compute quality score heuristic from claim verification
        grounded_ratio = 1.0 - claim_report.unsupported_ratio
        quality_score = round(
            min(10, max(1, grounded_ratio * 8 + ref_report.verification_score * 2)),
            1,
        )
        # Update Phase 6 verification with the quality score
        if "verification" in self.artifacts:
            self.artifacts["verification"]["quality_score"] = quality_score

        # Store verification report in artifacts
        self.artifacts["verification_report"] = {
            "references_verified": ref_report.references_verified,
            "references_failed": ref_report.references_failed,
            "references_uncertain": ref_report.references_uncertain,
            "total_claims": claim_report.total_claims,
            "claims_supported": claim_report.claims_supported,
            "claims_unsupported": claim_report.claims_unsupported,
            "claims_uncertain": claim_report.claims_uncertain,
            "verification_score": round(ref_report.verification_score, 2),
            "unsupported_claim_ratio": round(claim_report.unsupported_ratio, 2),
            "apis_consulted": ref_report.apis_consulted,
        }

        # Final cleanup: fix double-bracket citations and editorial artifacts
        # that Phase 6 revision or Phase 7 hardening may have introduced
        draft = self._final_citation_cleanup(draft)

        self.artifacts["final_paper"] = draft
        self.display.step(
            f"Verification score: {ref_report.verification_score:.0%} references, "
            f"{1 - claim_report.unsupported_ratio:.0%} claims grounded"
        )
        self.display.phase_done(7)

    @staticmethod
    def _final_citation_cleanup(draft: dict[str, str]) -> dict[str, str]:
        """Fix double-bracket citations and editorial artifacts in final draft."""
        cleaned = {}
        fixes = 0
        for heading, content in draft.items():
            original = content
            # Fix [Author [Author, Year], Year] -> [Author, Year]
            content = re.sub(
                r"\[([A-Z][a-z]+)\s+\[\1,\s*(\d{4})\],\s*\d{4}\]",
                r"[\1, \2]",
                content,
            )
            # Fix [... Surname [Surname, Year] ...] -> [... ...] [Surname, Year]
            content = re.sub(
                r"\[([^\[\]]*?)\s+\[([A-Z][a-z]+,\s*\d{4})\]([^\[\]]*?)\]",
                r"[\1 \3] [\2]",
                content,
            )
            # Fix remaining nested brackets: [text [Author, Year] -> [Author, Year]
            # Catch patterns like "[Duplessis, 2020; Fenton [Fenton, 2011]"
            content = re.sub(
                r"\[[^\]]*?\[([A-Z][a-z]+,\s*\d{4})\]",
                r"[\1]",
                content,
            )
            # Strip malformed citation keys that don't match [Author, Year] format
            # Catches: [The; Nielsen, 2009], [Sleep], [Comparative, 2005], [REM; Nielsen, 2009]
            # Valid format: [AuthorName, YYYY] or [AuthorName, YYYYa] or [AuthorName et al., YYYY]
            # Remove entries within brackets that are just common words (not author names)
            _COMMON_WORDS = r"(?:The|A|An|In|On|For|And|To|Of|With|From|By|Is|Are|Was|Were|Its|This|That|Sleep|REM|Investigation|Comparative|Study|Review|Analysis|Research|Brain|Neural|Human)"
            # Fix "[Word; Author, Year]" → "[Author, Year]"
            content = re.sub(
                rf"\[{_COMMON_WORDS};\s*([A-Z][a-z]+(?:\s+et\s+al\.?)?,\s*\d{{4}}[a-z]?)\]",
                r"[\1]",
                content,
            )
            # Fix "[Author, Year; Word]" → "[Author, Year]"
            content = re.sub(
                rf"\[([A-Z][a-z]+(?:\s+et\s+al\.?)?,\s*\d{{4}}[a-z]?);\s*{_COMMON_WORDS}\]",
                r"[\1]",
                content,
            )
            # Remove bare word-only citations: [Sleep], [The], [Investigation]
            content = re.sub(
                rf"\[{_COMMON_WORDS}\]",
                "",
                content,
            )

            # Strip editorial checklist artifacts
            content = re.sub(
                r"Also check for:.*?(?:topic sentences|cite_keys|thematic synthesis)[.\s]*",
                "",
                content,
                flags=re.IGNORECASE,
            )
            # Strip meta-commentary where LLM brags about its own references/citations
            # e.g. "Additional bibliographic entries from the reference list ... are now integrated"
            content = re.sub(
                r"[^.]*(?:(?:bibliographic|additional)\s+(?:entries|references|citations)\s+"
                r"(?:from|in)\s+the\s+reference\s+list"
                r"|(?:are|were|have\s+been)\s+(?:now\s+)?integrated\s+into\s+the\s+"
                r"(?:Methods|Methodology|Discussion|Results|Introduction|text|narrative)"
                r"|(?:references?\s+(?:listed?|appearing|included)\s+(?:above|below|in\s+the\s+"
                r"(?:bibliography|reference\s+(?:list|section))))"
                r"|(?:the\s+(?:above|following|remaining)\s+(?:references?|citations?|sources?)\s+"
                r"(?:are|were|have\s+been)\s+(?:now\s+)?(?:woven|incorporated|integrated|added|included))"
                r")[^.]*\.",
                "",
                content,
                flags=re.IGNORECASE,
            )
            # Strip sentences where LLM lists cite_keys as examples of what it did
            # e.g. "for example, [Al_Rabeah], [Blaschke], [Chen] ... are now cited"
            content = re.sub(
                r"[^.]*for\s+example,?\s*(?:\[[^\]]+\],?\s*){2,}[^.]*\.",
                "",
                content,
                flags=re.IGNORECASE,
            )
            # Clean up leftover whitespace artifacts from removals
            content = re.sub(r"\s+\.", ".", content)
            content = re.sub(r"\s+,", ",", content)
            content = re.sub(r"\s{2,}", " ", content)
            content = content.strip()
            if content != original:
                fixes += 1
            cleaned[heading] = content
        if fixes > 0:
            logger.info("Final citation cleanup: fixed %d sections", fixes)
        return cleaned

    def _strip_dangling_citations(
        self,
        draft: dict[str, str],
        removed_ref_ids: set[str],
        ref_list: list[dict],
    ) -> dict[str, str]:
        """Remove in-text citations that reference removed/unverifiable refs."""
        # Build cite_keys for removed references
        removed_keys = set()
        for rl in ref_list:
            if not isinstance(rl, dict):
                continue
            if any(rid in rl.get("title", "") or rid == str(rl.get("ref_num", ""))
                   for rid in removed_ref_ids):
                ck = rl.get("cite_key", "")
                if ck:
                    removed_keys.add(ck)

        if not removed_keys:
            return draft

        # Build regex pattern to match any removed cite_key
        escaped = [re.escape(k) for k in removed_keys]
        pattern = re.compile("|".join(escaped))

        cleaned = {}
        for section_name, content in draft.items():
            content = pattern.sub("", content)
            # Clean up artifacts left by citation removal
            content = re.sub(r"\(\s*[;,\s]*\s*\)", "", content)   # empty parens "(  ; )"
            content = re.sub(r"\s*;\s*\]", "]", content)          # orphaned semicolons in brackets
            content = re.sub(r"\[\s*;\s*", "[", content)           # leading semicolons in brackets
            content = re.sub(r",\s*,", ",", content)               # stuttered commas ", ,"
            content = re.sub(r",\s*\)", ")", content)              # ", )" → ")"
            content = re.sub(r"\(\s*,", "(", content)              # "(," → "("
            content = re.sub(r"\s+\.", ".", content)               # "word ." → "word."
            content = re.sub(r"\s+,", ",", content)                # "word ," → "word,"
            content = re.sub(r"\s+\)", ")", content)               # "word )" → "word)"
            content = re.sub(r"\(\s*\)", "", content)              # empty "()"
            content = re.sub(r"\s{2,}", " ", content)              # collapse multiple spaces
            cleaned[section_name] = content

        return cleaned

    @staticmethod
    def _collect_cited_keys(sections: dict[str, str], abstract: str = "") -> set[str]:
        """Scan all section content + abstract for cite_key patterns.

        Recognises:
          [Author, YYYY]          [Author et al., YYYY]
          [Author]                [Author and Author, YYYY]
          [Author & Author]       [Author, YYYYa]  (disambiguated)
        Returns lowercased cite_keys for case-insensitive matching.
        """
        # Match brackets containing at least one capitalized word, optional
        # "et al." / "and Author" / "&", optional ", YYYY" suffix.
        # Also handles multi-word surnames like "De Groot", "Van der Berg".
        pattern = re.compile(
            r"\["
            r"("
            r"[A-Z][a-zA-Z\-']+(?:\s+[a-z]+)*(?:\s+[A-Z][a-zA-Z\-']*)?"  # Surname (possibly multi-word)
            r"(?:\s+(?:et\s+al\.|and|&)\s*[A-Z]?[a-zA-Z\-']*)?"  # optional co-author/et al.
            r"(?:,\s*\d{4}[a-z]?)?"                   # optional year + disambig suffix
            r")"
            r"\]"
        )
        # Non-citation bracket words that the regex would wrongly match
        _NOT_CITATIONS = {
            "figure", "figures", "fig", "table", "tables", "tab",
            "supplementary", "supporting", "appendix", "panel",
            "section", "chapter", "equation", "box", "note",
            "emphasis", "original", "ibid", "sic", "deleted",
            "decision", "editorial", "erratum", "corrigendum",
            "review", "response", "comment", "letter",
        }
        cited: set[str] = set()
        all_text = abstract + "\n" + "\n".join(sections.values())
        for match in pattern.finditer(all_text):
            key = match.group(0).lower()
            # Extract the first word inside brackets to check against exclusion list
            inner = match.group(1).strip().split()[0].lower() if match.group(1).strip() else ""
            if inner in _NOT_CITATIONS:
                continue
            cited.add(key)
        return cited

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def _submit(self, challenge_id: str | None = None) -> dict:
        """Assemble final paper and submit to AgentPub API."""
        self.display.step("Submitting to AgentPub...")
        self.display.tick()

        brief = self.artifacts.get("research_brief", {})
        draft = self.artifacts.get("final_paper", self.artifacts.get("zero_draft", {}))
        memos = self.artifacts.get("reading_memos", {})

        # Final fabrication sanitizer pass — catches markers reintroduced by Phase 7
        draft = self._sanitize_fabrication(draft)

        # Final citation density enforcement — strip uncited empirical paragraphs
        draft = self._enforce_citation_density(draft)

        # Final truncation fix — trim garbled/incomplete sentence endings
        for heading in list(draft.keys()):
            content = draft[heading].rstrip()
            if content and content[-1] not in '.!?"\')':
                last_end = max(content.rfind("."), content.rfind("!"), content.rfind("?"))
                if last_end > len(content) * 0.7:
                    draft[heading] = content[: last_end + 1]
                    logger.info("Submission truncation fix: trimmed '%s'", heading)

        # Build sections in submission order
        # Note: API requires standard heading names, so we keep "Methodology"
        # The AI-native framing is enforced in the section *content* via prompts
        sections = []
        for heading in _SUBMIT_ORDER:
            content = draft.get(heading, "")
            if content:
                sections.append({"heading": heading, "content": content})

        # Pre-submission validation: check word count locally
        total_words = sum(len(s["content"].split()) for s in sections)
        _skip_api = False
        _skip_reason = ""
        if total_words < self.config.min_total_words:
            _skip_api = True
            _skip_reason = (
                f"Paper has {total_words} words but API requires minimum {self.config.min_total_words}. "
                "You can edit the saved JSON and resubmit with: agentpub submit"
            )
            logger.warning("Paper has only %d words (minimum %d)", total_words, self.config.min_total_words)
            self.display.step(_skip_reason)

        # Check required sections
        present = {s["heading"] for s in sections}
        missing = [h for h in _SUBMIT_ORDER if h not in present]
        if missing:
            _skip_api = True
            _skip_reason = f"Missing required sections: {', '.join(missing)}"
            logger.warning(_skip_reason)
            self.display.step(_skip_reason)

        # Build references with full metadata from candidates + custom sources
        candidates = self.artifacts.get("candidate_papers", [])
        custom_content = self.artifacts.get("custom_source_content", {})
        # Index candidates by paper_id for fast lookup
        cand_by_id = {c["paper_id"]: c for c in candidates}

        references = []
        for memo in memos.values():
            pid = memo.get("paper_id", "")
            cand = cand_by_id.get(pid, {})
            src = custom_content.get(pid, {})

            # Determine type and source
            is_platform = not (pid.startswith("s2_") or pid.startswith("web_") or pid.startswith("custom_"))
            ref_type = "internal" if is_platform else "external"
            ref_source = None
            if is_platform:
                ref_source = "agentpub"
            elif pid.startswith("s2_"):
                ref_source = "scholar"
            elif src.get("doi") or cand.get("doi"):
                ref_source = "doi"
            elif src.get("source_path") or cand.get("url"):
                ref_source = "url"

            # Collect authors from best available source
            authors = (
                src.get("authors")
                or cand.get("authors")
                or None
            )
            if authors and isinstance(authors, list):
                authors = [a for a in authors if a]  # filter blanks

            # Year
            year = cand.get("year") or None
            if year:
                try:
                    year = int(year)
                except (ValueError, TypeError):
                    year = None

            # Clean up internal tracking IDs — don't expose s2_xxx, serper_xxx, web_xxx
            # to the submitted paper. Use DOI or generate a clean ref ID.
            clean_ref_id = pid
            if pid.startswith(("s2_", "serper_", "web_")):
                _doi = src.get("doi") or cand.get("doi") or ""
                if _doi:
                    clean_ref_id = f"doi:{_doi}"
                else:
                    # Generate from first author + year
                    _ref_authors = src.get("authors") or cand.get("authors") or []
                    _ref_year = cand.get("year") or ""
                    if _ref_authors:
                        _surname = _ref_authors[0].split()[-1].lower()
                        clean_ref_id = f"ext:{_surname}_{_ref_year}" if _ref_year else f"ext:{_surname}"
                    else:
                        clean_ref_id = f"ext:ref_{len(references) + 1}"

            ref = {
                "ref_id": clean_ref_id,
                "type": ref_type,
                "title": memo.get("title", "Unknown"),
            }
            if ref_source:
                ref["source"] = ref_source
            if authors:
                ref["authors"] = authors
            if year:
                ref["year"] = year
            doi = src.get("doi") or cand.get("doi") or None
            if doi:
                ref["doi"] = doi
            url = src.get("source_path") or cand.get("url") or None
            if url:
                ref["url"] = url

            references.append(ref)

        # Junk reference filtering — remove metadata artifacts
        # CrossRef and other APIs sometimes return peer-review letters,
        # decision letters, figure DOIs, and other non-paper artifacts.
        _JUNK_TITLE_PATTERNS = re.compile(
            r"^(?:Review\s+for|Decision\s+(?:letter\s+)?(?:for|on)|Figure\s+\d|"
            r"Reviewer\s+comment|Editor(?:ial)?\s+(?:decision|comment|note)|"
            r"Author\s+response|Supplementary|Erratum|Corrigendum|"
            r"Faculty\s+Opinions?\s+recommendation|"
            r"Faculty\s+of\s+1000\s+recommendation|"
            r"Peer\s+review\s+report|Referee\s+report)",
            re.IGNORECASE,
        )
        pre_junk = len(references)
        references = [
            r for r in references
            if not _JUNK_TITLE_PATTERNS.match(r.get("title", ""))
        ]
        junk_removed = pre_junk - len(references)
        if junk_removed > 0:
            logger.info("Junk reference filter: removed %d metadata artifacts", junk_removed)

        # Self-citation: append own papers if injected by ContinuousDaemon
        self_citation_refs = getattr(self, "_self_citation_refs", [])
        if self_citation_refs:
            existing_ids = {r["ref_id"] for r in references}
            for self_ref in self_citation_refs:
                if self_ref["ref_id"] not in existing_ids:
                    references.append(self_ref)
            self._self_citation_refs = []

        # Reference reconciliation: remove uncited references
        abstract = self.artifacts.get("abstract", "")
        cited_keys = self._collect_cited_keys(
            {s["heading"]: s["content"] for s in sections}, abstract
        )
        if cited_keys:
            # Build cite_key -> ref_id mapping using THREE strategies:
            # 1. Title match: ref_list title → reference title (most reliable)
            # 2. Direct cite_key from ref_list via positional memo mapping
            # 3. Author-surname match: extract surname from cite_key, match refs
            cite_key_to_refid: dict[str, str] = {}

            try:
                ref_list = json.loads(self.artifacts.get("ref_list_text", "[]"))
            except (json.JSONDecodeError, Exception):
                ref_list = []

            # Strategy 1: Title-based mapping (cite_key → ref_list title → reference title)
            # Build reference title index
            ref_title_to_id: dict[str, str] = {}
            for ref in references:
                t = (ref.get("title") or "").lower().strip()[:60]
                if t:
                    ref_title_to_id[t] = ref["ref_id"]
            for rl in ref_list:
                ck = rl.get("cite_key", "")
                rl_title = (rl.get("title") or "").lower().strip()[:60]
                if ck and rl_title:
                    rid = ref_title_to_id.get(rl_title)
                    if rid:
                        cite_key_to_refid[ck.lower()] = rid

            # Strategy 2: Positional memo-key mapping (fallback)
            memo_keys = list(memos.keys())
            for rl in ref_list:
                ck = rl.get("cite_key", "")
                if ck and ck.lower() not in cite_key_to_refid:
                    ref_num = rl.get("ref_num", 0)
                    if ref_num < len(memo_keys):
                        candidate_id = memo_keys[ref_num]
                        # Only use if this memo_key is actually in references
                        if any(r["ref_id"] == candidate_id for r in references):
                            cite_key_to_refid[ck.lower()] = candidate_id

            # Strategy 3: Author-surname fuzzy match
            author_re = re.compile(r"\[([A-Za-z][A-Za-z\-']+)")
            ref_by_author: dict[str, list[str]] = {}
            for ref in references:
                for author in ref.get("authors", []) or []:
                    if isinstance(author, str) and author.strip():
                        surname = author.split()[-1].lower()
                        ref_by_author.setdefault(surname, []).append(ref["ref_id"])

            for ck in cited_keys:
                if ck in cite_key_to_refid:
                    continue
                m = author_re.search(ck)
                if m:
                    surname = m.group(1).lower()
                    matching_refs = ref_by_author.get(surname, [])
                    if matching_refs:
                        cite_key_to_refid[ck] = matching_refs[0]

            logger.info(
                "Cite key mapping: %d/%d keys mapped to ref_ids",
                len(cite_key_to_refid), len(cited_keys),
            )

            # Identify which ref_ids are actually cited
            cited_ref_ids = set()
            for ck in cited_keys:
                rid = cite_key_to_refid.get(ck)
                if rid:
                    cited_ref_ids.add(rid)

            # Remove uncited references, but only if the cite_key mapping
            # covers a reasonable fraction of refs. If most refs appear
            # "uncited", the mapping is broken (e.g. cite format mismatch),
            # not the references themselves.
            if cited_ref_ids:
                cited_refs = [r for r in references if r["ref_id"] in cited_ref_ids]
                coverage = len(cited_ref_ids) / max(len(references), 1)
                if coverage >= 0.25 and len(cited_refs) >= 8:
                    removed = len(references) - len(cited_refs)
                    if removed > 0:
                        logger.info(
                            "Reference reconciliation: removed %d uncited references (%d remain, %.0f%% coverage)",
                            removed, len(cited_refs), coverage * 100,
                        )
                        self.display.step(f"Removed {removed} uncited references")
                    references = cited_refs
                else:
                    logger.info(
                        "Skipping reference reconciliation: only %d/%d refs matched (%.0f%% coverage) — keeping all",
                        len(cited_ref_ids), len(references), coverage * 100,
                    )

        # Orphan reference removal: even without full cite_key mapping,
        # remove references whose author surname never appears in the paper text.
        # This catches obvious orphans (papers listed but never mentioned).
        all_text_lower = (abstract + " " + " ".join(s["content"] for s in sections)).lower()
        non_orphan_refs = []
        orphan_count = 0
        # Common English words that are also surnames — these produce false
        # positive matches against body text so we exclude them from surname
        # matching and rely on the title-word fallback instead.
        _SURNAME_STOPWORDS = {
            "best", "current", "clinical", "trial", "impact", "comments",
            "expanding", "breakthroughs", "personalized", "care", "control",
            "strategy", "global", "rapid", "test", "review", "new", "use",
            "general", "long", "low", "van", "lee", "park", "kim", "li",
            "young", "white", "green", "brown", "grant", "cross", "field",
            "price", "bell", "rose", "wells", "fox", "may", "rich", "case",
        }
        for ref in references:
            # Check if any author surname appears in the text (word-boundary)
            authors = ref.get("authors", []) or []
            ref_title_words = set(
                w.lower() for w in re.findall(r"[a-zA-Z]{4,}", ref.get("title", ""))
            )
            found = False
            for author in authors:
                if isinstance(author, str) and author.strip():
                    surname = author.split()[-1].lower()
                    if len(surname) < 3 or surname in _SURNAME_STOPWORDS:
                        continue
                    # Use word-boundary matching to avoid "lau" matching "clause"
                    if re.search(r"\b" + re.escape(surname) + r"\b", all_text_lower):
                        found = True
                        break
            if not found:
                # Fallback: check if 3+ distinctive title words appear in text
                title_matches = sum(1 for w in ref_title_words if w in all_text_lower)
                if title_matches >= 3:
                    found = True
            if found:
                non_orphan_refs.append(ref)
            else:
                orphan_count += 1
        if orphan_count > 0 and len(non_orphan_refs) >= 8:
            logger.info(
                "Orphan reference removal: dropped %d refs never mentioned in text (%d remain)",
                orphan_count, len(non_orphan_refs),
            )
            self.display.step(f"Removed {orphan_count} orphan references")
            references = non_orphan_refs

        # Pad to minimum 8 references if needed, but only with candidates
        # that were actually read (have reading memos — meaning the LLM
        # processed them and found them relevant enough to keep).
        if len(references) < 8:
            existing_ids = {r["ref_id"] for r in references}
            memo_ids = set(memos.keys())
            for cand in candidates:
                if len(references) >= 8:
                    break
                pid = cand["paper_id"]
                if pid in existing_ids:
                    continue
                # Only pad with papers we actually read (have memos)
                if pid not in memo_ids:
                    continue
                src = custom_content.get(pid, {})
                is_platform = not (pid.startswith("s2_") or pid.startswith("web_") or pid.startswith("custom_"))
                ref = {
                    "ref_id": pid,
                    "type": "internal" if is_platform else "external",
                    "title": cand.get("title", "Unknown"),
                }
                authors = src.get("authors") or cand.get("authors") or None
                if authors and isinstance(authors, list):
                    ref["authors"] = [a for a in authors if a]
                year = cand.get("year")
                if year:
                    try:
                        ref["year"] = int(year)
                    except (ValueError, TypeError):
                        pass
                doi = src.get("doi") or cand.get("doi")
                if doi:
                    ref["doi"] = doi
                url = src.get("source_path") or cand.get("url")
                if url:
                    ref["url"] = url
                references.append(ref)

        # ── Reverse-orphan check: strip in-text citations with no matching ref ──
        # Build set of all author surnames from the final reference list
        _ref_surnames: set[str] = set()
        for ref in references:
            for author in ref.get("authors", []) or []:
                if isinstance(author, str) and author.strip():
                    surname = author.split()[-1].lower()
                    if len(surname) >= 3:
                        _ref_surnames.add(surname)
            # Also add first distinctive word from title as fallback
            for w in re.findall(r"[a-zA-Z]{5,}", ref.get("title", "")):
                _ref_surnames.add(w.lower())

        # Find cite_keys in text that don't match any reference
        all_section_text = {s["heading"]: s["content"] for s in sections}
        all_cited = self._collect_cited_keys(all_section_text, self.artifacts.get("abstract", ""))
        reverse_orphans: set[str] = set()
        for ck in all_cited:
            # Extract author surname from cite_key like "[götz, 2018]" → "götz"
            ck_inner = ck.strip("[]")
            ck_surname = ck_inner.split(",")[0].split(" et ")[0].split(" and ")[0].strip().lower()
            if ck_surname and ck_surname not in _ref_surnames:
                reverse_orphans.add(ck)

        if reverse_orphans:
            logger.info(
                "Reverse-orphan cleanup: %d in-text citations have no matching reference: %s",
                len(reverse_orphans), list(reverse_orphans)[:10],
            )
            self.display.step(f"Stripping {len(reverse_orphans)} reverse-orphan citations")
            # Build regex to remove these citations from all sections
            for i, sec in enumerate(sections):
                content = sec["content"]
                for orphan_ck in reverse_orphans:
                    inner = orphan_ck.strip("[]")
                    # Try exact match first
                    escaped = re.escape(inner)
                    content = re.sub(
                        r"\[" + escaped + r"\]",
                        "",
                        content,
                        flags=re.IGNORECASE,
                    )
                    # Also try flexible match: [Surname ... Year] with any spacing/punctuation
                    parts = inner.split(",")
                    if len(parts) >= 2:
                        surname = re.escape(parts[0].strip())
                        year = parts[-1].strip()
                        if re.match(r"\d{4}", year):
                            content = re.sub(
                                r"\[\s*" + surname + r"[^]]*?" + re.escape(year) + r"\s*\]",
                                "",
                                content,
                                flags=re.IGNORECASE,
                            )
                # Clean up artifacts: empty parens, double spaces, orphaned semicolons,
                # ghost citation whitespace ("word ." → "word.")
                content = re.sub(r"\(\s*[;,\s]*\s*\)", "", content)
                content = re.sub(r"\s*;\s*\]", "]", content)
                content = re.sub(r"\[\s*;\s*", "[", content)
                content = re.sub(r"\s{2,}", " ", content)
                content = re.sub(r"\s+\.", ".", content)  # "word ." → "word."
                content = re.sub(r"\s+,", ",", content)  # "word ," → "word,"
                content = re.sub(r",\s*,+", ",", content)  # ", ," or ",,," → ","
                content = re.sub(r"\(\s*\)", "", content)  # empty "()"
                content = re.sub(r",\s*\)", ")", content)  # ", )" → ")"
                content = re.sub(r"\(\s*,", "(", content)  # "(," → "("
                content = re.sub(r"\s+\)", ")", content)  # "word )" → "word)"
                content = re.sub(r"\s{2,}", " ", content)  # re-collapse after all fixes
                sections[i] = {"heading": sec["heading"], "content": content}

        title = brief.get("title", "Untitled Research Paper")
        abstract = self.artifacts.get("abstract", "")

        # Token usage across all LLM calls
        token_usage = self.llm.total_usage

        # Generation duration
        generation_seconds = round(time.time() - getattr(self, "_research_start_time", time.time()), 1)

        # SDK version
        import agentpub
        sdk_version = getattr(agentpub, "__version__", "unknown")

        # Content hash for similarity comparisons
        import hashlib
        full_text = title + "\n" + abstract + "\n"
        for s in sections:
            full_text += s.get("heading", "") + "\n" + s.get("content", "") + "\n"
        content_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()

        metadata = {
            "agent_model": self.llm.model_name,
            "agent_platform": self.llm.provider_name,
            "research_protocol": "expert_7phase",
            "phases_completed": 7,
            "papers_reviewed": len(memos),
            "quality_level": self.config.quality_level,
            "input_tokens": token_usage.get("input_tokens", 0),
            "output_tokens": token_usage.get("output_tokens", 0),
            "total_tokens": token_usage.get("total_tokens", 0),
            "generation_seconds": generation_seconds,
            "sdk_version": sdk_version,
            "content_hash": content_hash,
        }

        verification = self.artifacts.get("verification", {})
        if verification:
            metadata["self_quality_score"] = verification.get("quality_score", 0)

        # Attach Phase 7 verification report if available
        vr = self.artifacts.get("verification_report")
        if vr:
            metadata["verification_report"] = vr

        # Generate tags from research brief (API requires at least 1)
        tags = self._generate_tags(brief, title)

        # Build the paper payload for both submission and local save
        paper_payload = {
            "title": title,
            "abstract": abstract,
            "sections": sections,
            "references": references,
            "metadata": metadata,
            "challenge_id": challenge_id,
            "tags": tags,
        }

        # If word count is too low, try LLM expansion before giving up
        ref_list_text = self.artifacts.get("ref_list_text", "[]")
        if _skip_api and "word" in _skip_reason.lower():
            self.display.step(f"Word count too low ({total_words}), attempting last-resort expansion...")
            paper_payload = self._fix_paper_from_feedback(
                paper_payload,
                f"Paper has {total_words} words but requires minimum {self.config.min_total_words}",
                ref_list_text,
            )
            # Recheck
            total_words = sum(len(s["content"].split()) for s in paper_payload["sections"])
            self.display.step(f"After fix: {total_words} words")
            if total_words >= self.config.min_total_words:
                _skip_api = False
                _skip_reason = ""

        if _skip_api:
            saved_path = self._save_paper_locally(paper_payload)
            self.display.step(f"Paper saved locally: {saved_path}")
            return {
                "error": _skip_reason,
                "title": title,
                "word_count": total_words,
                "saved_locally": str(saved_path),
                "retry_hint": f"Submit later with: agentpub submit \"{saved_path}\"",
            }

        # Final quality pass: strip any remaining thinking tokens from all fields
        paper_payload["title"] = strip_thinking_tags(paper_payload["title"]).strip()
        paper_payload["abstract"] = strip_thinking_tags(paper_payload["abstract"]).strip()
        for s in paper_payload["sections"]:
            s["content"] = self._extract_section_text(s["content"])

        # Verify no thinking artifacts remain (catch edge cases like orphaned </think>)
        import re as _re
        _artifact_re = _re.compile(r"</?(?:think|thinking|reasoning|internal|reflection)>", _re.IGNORECASE)
        for s in paper_payload["sections"]:
            if _artifact_re.search(s["content"]):
                logger.warning("Thinking artifact in '%s' after cleanup — force-stripping", s.get("heading"))
                s["content"] = _artifact_re.sub("", s["content"]).strip()
        if _artifact_re.search(paper_payload["abstract"]):
            paper_payload["abstract"] = _artifact_re.sub("", paper_payload["abstract"]).strip()
        if _artifact_re.search(paper_payload["title"]):
            paper_payload["title"] = _artifact_re.sub("", paper_payload["title"]).strip()

        # Submit with self-correction loop: if the API rejects with 400,
        # feed the rejection reason back to the LLM for fixes and retry.
        max_retries = 3

        for attempt in range(1, max_retries + 1):
            try:
                result = self.client.submit_paper(**paper_payload)
            except Exception as e:
                # Network / server error — save locally
                saved_path = self._save_paper_locally(paper_payload)
                self.display.step(f"Paper saved locally: {saved_path}")
                error_msg = str(e)
                logger.error("Submission failed: %s", error_msg)
                return {
                    "error": error_msg,
                    "title": title,
                    "word_count": total_words,
                    "saved_locally": str(saved_path),
                    "retry_hint": f"Submit later with: agentpub submit \"{saved_path}\"",
                }

            # Success
            if result.get("paper_id"):
                pid = result["paper_id"]
                logger.info("Paper submitted: %s", pid)
                self.display.step(f"Published: {pid}")

                # Post-submission quality review (automated, no LLM)
                self._post_submission_quality_review(paper_payload, pid)

                self.display.complete(f"Published as {pid}")
                return result

            # 400/422 validation rejection — self-correct
            if result.get("status_code") in (400, 422) and attempt < max_retries:
                feedback = result.get("detail", "Unknown validation error")
                # Pydantic 422 errors come as a list of dicts — flatten to string
                if isinstance(feedback, list):
                    feedback = "; ".join(
                        item.get("msg", str(item)) if isinstance(item, dict) else str(item)
                        for item in feedback
                    )
                logger.warning("Submission rejected (attempt %d/%d): %s", attempt, max_retries, feedback)
                self.display.step(f"API rejected (attempt {attempt}): {str(feedback)[:80]}")
                self.display.tick()

                paper_payload = self._fix_paper_from_feedback(
                    paper_payload, feedback, ref_list_text
                )
                # Update word count
                total_words = sum(len(s["content"].split()) for s in paper_payload["sections"])
                self.display.step(f"Fixed paper: {total_words} words — resubmitting...")
                self._delay()
                continue

            # Final attempt also rejected, or non-400 error
            error_msg = result.get("detail", result.get("error", "Unknown error"))
            break

        # All retries exhausted — save locally
        saved_path = self._save_paper_locally(paper_payload)
        self.display.step(f"Paper saved locally: {saved_path}")
        logger.error("Submission failed after %d attempts: %s", max_retries, error_msg)
        return {
            "error": str(error_msg),
            "title": title,
            "word_count": total_words,
            "saved_locally": str(saved_path),
            "retry_hint": f"Submit later with: agentpub submit \"{saved_path}\"",
        }

    def _fix_paper_from_feedback(
        self, paper_payload: dict, feedback: str, ref_list_text: str
    ) -> dict:
        """Use the LLM to fix a paper based on API rejection feedback."""
        sections = paper_payload["sections"]
        brief = self.artifacts.get("research_brief", {})

        # Quick fix: missing tags (no LLM needed)
        feedback_lower = str(feedback).lower()
        if "tag" in feedback_lower and ("required" in feedback_lower or "at least" in feedback_lower):
            if not paper_payload.get("tags"):
                paper_payload["tags"] = self._generate_tags(
                    brief, paper_payload.get("title", "")
                )
                self.display.step(f"Auto-fixed: added {len(paper_payload['tags'])} tags")
                return paper_payload

        # Build a summary of the current paper state
        section_summary = "\n".join(
            f"  {s['heading']}: {len(s['content'].split())} words"
            for s in sections
        )
        total_words = sum(len(s["content"].split()) for s in sections)

        system = self._prompts["fix_paper"]

        # Determine what kind of fix is needed based on the feedback
        feedback_str = str(feedback)
        feedback_lower = feedback_str.lower()

        # Word count issue — expand ALL sections below target
        if "word" in feedback_lower and ("short" in feedback_lower or "minimum" in feedback_lower or "count" in feedback_lower):
            self.display.step("Fixing: expanding sections to meet word count...")
            sorted_sections = sorted(sections, key=lambda s: len(s["content"].split()))
            for s in sorted_sections:
                current_words = len(s["content"].split())
                section_min = _SECTION_WORD_MINIMUMS.get(s["heading"], 800)
                if current_words >= section_min and total_words >= self.config.min_total_words:
                    continue  # already long enough and total is met
                self._delay()
                self._check_interrupt()
                target = max(current_words + 500, section_min)
                words_needed = target - current_words
                n_paragraphs = max(3, words_needed // 150)
                prompt = f"""Paper title: {brief.get('title', '')}

The '{s['heading']}' section needs {words_needed} more words to meet the {self.config.min_total_words}-word minimum.

Current section (for context — do NOT repeat this content):
{s['content'][:3000]}

REFERENCE LIST (cite ONLY these):
{ref_list_text[:6000]}

Write {n_paragraphs} NEW paragraphs (150-200 words each) to ADD to this section.
Include detailed analysis, reference discussion, comparisons, and implications.

Write the new paragraphs as academic text. Separate paragraphs with blank lines. Use **bold** for key terms and bullet lists (- item) for enumerations where appropriate.
Do NOT wrap in JSON. Do NOT repeat existing content."""

                result = self.llm.generate(system, prompt, max_tokens=16000)
                new_text = self._extract_section_text(result.text)
                if new_text and len(new_text.split()) > 30:
                    s["content"] = s["content"] + "\n\n" + new_text
                    total_words = sum(len(x["content"].split()) for x in sections)
                    added = len(new_text.split())
                    self.display.step(f"  {s['heading']}: {current_words} -> {len(s['content'].split())} words (+{added})")

            return paper_payload

        # Content safety issue — clean up flagged content
        if "content safety" in feedback_lower or "inappropriate" in feedback_lower:
            self.display.step("Fixing: addressing content safety issues...")
            for s in sections:
                self._delay()
                self._check_interrupt()
                prompt = f"""Paper title: {brief.get('title', '')}
API rejection feedback: {feedback_str}

Revise the '{s['heading']}' section to address the content safety issues.
Remove or rephrase any flagged content while preserving the academic substance.

Current content:
{s['content'][:6000]}

REFERENCE LIST (cite ONLY these):
{ref_list_text[:4000]}

Return JSON: {{"content": "the revised section"}}"""

                result = self.llm.generate_json(system, prompt)
                revised = result.get("content", "")
                if revised:
                    s["content"] = revised

            return paper_payload

        # Generic fix — send the whole feedback to the LLM
        self.display.step("Fixing: applying general corrections...")
        self._delay()
        prompt = f"""Paper title: {brief.get('title', '')}
API rejection feedback: {feedback_str}

Current paper structure:
{section_summary}
Total words: {total_words}
References: {len(paper_payload.get('references', []))}

Fix the issues described in the feedback. Return JSON with:
- "sections": list of objects with "heading" and "content" for any sections that need changes
- "abstract": revised abstract (only if the abstract needs changes, otherwise omit)
- "title": revised title (only if the title needs changes, otherwise omit)

Only include sections that actually need changes."""

        result = self.llm.generate_json(system, prompt)

        # Apply fixes
        if result.get("title"):
            paper_payload["title"] = result["title"]
        if result.get("abstract"):
            paper_payload["abstract"] = result["abstract"]

        revised_sections = result.get("sections", [])
        if revised_sections:
            existing = {s["heading"]: s for s in sections}
            for rs in revised_sections:
                heading = rs.get("heading", "")
                content = rs.get("content", "")
                if heading in existing and content:
                    existing[heading]["content"] = content

        return paper_payload

    def _post_submission_quality_review(self, paper_payload: dict, paper_id: str) -> None:
        """Automated post-submission quality check — no LLM call needed.

        Checks for common issues flagged by expert reviewers:
        - Orphan references (listed but never cited)
        - Citation concentration (over-reliance on few sources)
        - Truncated/unfinished sentences
        - Methodology fabrication markers
        """
        issues: list[str] = []

        sections = paper_payload.get("sections", [])
        references = paper_payload.get("references", [])
        all_text = " ".join(
            s.get("content", "") if isinstance(s, dict) else str(s)
            for s in (sections if isinstance(sections, list) else [])
        )
        all_text_lower = all_text.lower()

        # 1. Orphan references — refs whose author surname never appears in text
        _SURNAME_STOPWORDS = {
            "best", "current", "clinical", "trial", "impact", "comments",
            "expanding", "breakthroughs", "personalized", "care", "control",
            "strategy", "global", "rapid", "test", "review", "new", "use",
            "general", "long", "low", "van", "lee", "park", "kim", "li",
            "young", "white", "green", "brown", "grant", "cross", "field",
            "price", "bell", "rose", "wells", "fox", "may", "rich", "case",
        }
        orphan_refs = []
        for ref in references:
            authors = ref.get("authors", []) or []
            found = False
            for author in authors:
                if isinstance(author, str) and author.strip():
                    surname = author.split()[-1].lower()
                    if len(surname) < 3 or surname in _SURNAME_STOPWORDS:
                        continue
                    if re.search(r"\b" + re.escape(surname) + r"\b", all_text_lower):
                        found = True
                        break
            if not found:
                # Fallback: check if 3+ distinctive title words appear
                ref_title_words = set(
                    w.lower() for w in re.findall(r"[a-zA-Z]{4,}", ref.get("title", ""))
                )
                title_matches = sum(1 for w in ref_title_words if w in all_text_lower)
                if title_matches >= 3:
                    found = True
            if not found:
                orphan_refs.append(ref.get("title", "?")[:60])
        if orphan_refs:
            issues.append(f"Orphan references ({len(orphan_refs)} listed but never cited): {', '.join(orphan_refs[:5])}")

        # 2. Citation concentration — count cite_key frequency
        cite_pattern = re.compile(r"\[([A-Z][a-zA-Z\-']+(?:[^]]{0,30})?)\]")
        cite_counts: dict[str, int] = {}
        for m in cite_pattern.finditer(all_text):
            key = m.group(0).lower()
            cite_counts[key] = cite_counts.get(key, 0) + 1
        if cite_counts:
            total_cites = sum(cite_counts.values())
            top_cite = max(cite_counts, key=cite_counts.get)  # type: ignore
            top_count = cite_counts[top_cite]
            if top_count > total_cites * 0.3 and total_cites > 10:
                issues.append(
                    f"Citation concentration: {top_cite} cited {top_count}/{total_cites} times "
                    f"({top_count * 100 // total_cites}% of all citations)"
                )
            unique_cited = len(cite_counts)
            if unique_cited < len(references) * 0.5 and len(references) > 10:
                issues.append(
                    f"Low citation coverage: only {unique_cited}/{len(references)} "
                    f"references actually cited in text"
                )

        # 3. Truncated sentences — text ending mid-word or single letter
        for sec in sections:
            content = sec.get("content", "") if isinstance(sec, dict) else ""
            heading = sec.get("heading", "?") if isinstance(sec, dict) else "?"
            # Check for abrupt endings
            stripped = content.rstrip()
            if stripped and not stripped[-1] in ".!?\"')":
                last_20 = stripped[-20:]
                issues.append(f"Possible truncation in {heading}: '...{last_20}'")

        # 4. Fabrication markers — scanned across ALL sections, not just Methodology
        fabrication_patterns = [
            (r"cohen['\u2019]?s?\s+kappa", "Fabricated Cohen's kappa"),
            (r"\bkappa\s*[=:]\s*0\.\d", "Fabricated kappa score"),
            (r"inter[- ]?rater\s+reliability", "Fabricated inter-rater reliability"),
            (r"(?:two|three|multiple)\s+(?:independent\s+)?(?:reviewers|annotators|coders|raters)",
             "Fabricated human reviewers/annotators"),
            (r"adjudicat(?:ed|ion)\s+by\s+(?:a\s+)?(?:third|senior|additional)",
             "Fabricated adjudication"),
            (r"(?:precision|recall|F1)\s*[=:]\s*0\.\d{2}", "Fabricated P/R/F1 scores"),
            (r"gold\s+standard\s*=\s*\d+", "Fabricated gold standard"),
            (r"independently\s+annotated\s+by", "Fabricated annotation"),
            (r"(?:Supplementary|Supporting)\s+(?:Figure|Table)\s+S\d", "Fabricated supplements"),
            (r"bootstrap\s+CI\s*=", "Fabricated bootstrap CI"),
            (r"disagreements?\s+(?:were|was)\s+resolved\s+by\s+consensus",
             "Fabricated consensus resolution"),
            (r"trained\s+human\s+annotators?\s+validated", "Fabricated human validation"),
            (r"blinded\s+(?:assessment|evaluation)", "Fabricated blinded assessment"),
            (r"participants?\s+were\s+recruited", "Fabricated participant recruitment"),
            (r"(?:IRB|ethics\s+committee)\s+approval", "Fabricated IRB/ethics approval"),
            (r"informed\s+consent\s+was\s+obtained", "Fabricated informed consent"),
            # Rule 7: Fabricated human-in-the-loop
            (r"verified\s+by\s+(?:a\s+)?human\s+(?:team|expert|reviewer)", "Fabricated human verification"),
            (r"(?:senior|lead)\s+author\s+adjudicat(?:ed|ion)", "Fabricated author adjudication"),
            (r"cross[- ]?checked\s+by\s+independent\s+researchers", "Fabricated independent cross-check"),
            # Rule 5: Fabricated aggregate statistics
            (r"pooled\s+(?:mean|effect\s+size|estimate)\s*[=:]\s*[\d.-]+", "Fabricated pooled statistic"),
            (r"I[²2]\s*[=:]\s*\d+(?:\.\d+)?%?", "Fabricated heterogeneity metric I²"),
            (r"Q[- ]?(?:statistic|test)?\s*[=:]\s*\d+", "Fabricated Q-statistic"),
            (r"tau[²2]\s*[=:]\s*[\d.]+", "Fabricated tau² estimate"),
            (r"k\s*[=:]\s*\d+\s+stud(?:y|ies)", "Fabricated k-count"),
            # Rule 8: Phantom figures/tables/supplements
            (r"(?:Table|Figure)\s+\d+\s*[.:]\s*\w", "Phantom table/figure reference"),
            (r"Methods?\s+Supplement", "Fabricated Methods Supplement"),
            (r"(?:Supplementary|Supporting)\s+(?:Information|Materials?|Data|Methods)", "Fabricated supplementary materials"),
            (r"Python\s+scripts?\s+(?:are|were|is)\s+(?:provided|available)", "Fabricated code availability"),
            (r"random\s+seeds?\s+(?:are|were|is)\s+(?:provided|available)", "Fabricated random seeds"),
            (r"(?<!no )human\s+adjudication", "Fabricated human adjudication"),
            # Split personality — LLM reverts to human roleplay
            (r"human[- ]in[- ]the[- ]loop", "Fabricated human-in-the-loop claim"),
            (r"two\s+authors?\s+independently\s+(?:reviewed|screened|coded)", "Fabricated dual-author review"),
            (r"reconciled?\s+(?:through|via)\s+(?:discussion|consensus)", "Fabricated consensus process"),
            # CoT scaffolding leaked into text
            (r"(?:^|\.\s+)(?:Topic\s+sentence|Synthesis\s+and\s+comparison|Sources?\s+of\s+divergence|Interim\s+conclusion)\s*:",
             "Leaked CoT scaffolding in text"),
            # Revision artifacts
            (r"[Ww]e\s+have\s+revised\s+(?:the|this)\s+manuscript",
             "Revision artifact - LLM thinks it is revising"),
            # Supplementary table/exclusion table
            (r"(?:Supplementary|Supporting)\s+(?:Exclusion|Inclusion)\s+Table",
             "Fabricated supplementary exclusion table"),
            (r"(?:Supplementary|Supporting)\s+Table\s+S?\d",
             "Fabricated supplementary table reference"),
        ]
        for pattern, desc in fabrication_patterns:
            if re.search(pattern, all_text, re.IGNORECASE):
                # Find which section contains it
                for sec in sections:
                    if isinstance(sec, dict) and re.search(pattern, sec.get("content", ""), re.IGNORECASE):
                        sec_name = sec.get("heading", "?")
                        issues.append(f"Fabrication in {sec_name}: {desc}")
                        break
                else:
                    issues.append(f"Fabrication detected: {desc}")

        # 5. Junk references — metadata artifacts that aren't real papers
        _JUNK_TITLE_RE = re.compile(
            r"^(?:Review\s+for|Decision\s+letter|Figure\s+\d|"
            r"Reviewer\s+comment|Editor(?:ial)?\s+decision|"
            r"Faculty\s+Opinions?\s+recommendation|"
            r"Faculty\s+of\s+1000\s+recommendation)",
            re.IGNORECASE,
        )
        junk_refs = [r.get("title", "?")[:60] for r in references
                     if _JUNK_TITLE_RE.match(r.get("title", ""))]
        if junk_refs:
            issues.append(f"Junk references ({len(junk_refs)}): {', '.join(junk_refs[:3])}")

        # 6. Missing in-text citations — check that references are actually
        # cited inline with [Author, Year] or (Author, Year) markers
        cite_marker_re = re.compile(r"\[([A-Z][a-zA-Z\-']+(?:\s+et\s+al\.?)?,\s*\d{4}[a-z]?)\]")
        inline_citations = cite_marker_re.findall(all_text)
        if len(references) > 5 and len(inline_citations) < 3:
            issues.append(
                f"Missing in-text citations: {len(references)} references listed but "
                f"only {len(inline_citations)} [Author, Year] markers found in text"
            )

        # 7. Garbled sentences — detect broken syntax patterns
        garbled_patterns = re.compile(
            r"(?:(?:for|with|and|the|of|in|to|by)\s*[.!?]"  # preposition ending sentence
            r"|\b\w+\s+\w+\s+\w+ed\s+\w+\s+with\s*[.!?]$)",  # word salad ending
            re.IGNORECASE,
        )
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            content = sec.get("content", "")
            heading = sec.get("heading", "?")
            sentences = re.split(r"(?<=[.!?])\s+", content)
            garbled_count = 0
            for sent in sentences:
                stripped = sent.rstrip()
                # Sentence ending with a preposition/article before punctuation
                if stripped and re.search(r"\b(?:for|with|and|the|of|in|to|by|or)\s*$",
                                          stripped.rstrip(".!?")):
                    garbled_count += 1
            if garbled_count >= 2:
                issues.append(f"Garbled sentences in {heading}: {garbled_count} sentences end with prepositions/articles")

        # Log results
        if issues:
            logger.warning(
                "Post-submission quality review for %s found %d issues:\n  %s",
                paper_id, len(issues), "\n  ".join(issues),
            )
            self.display.step(f"Quality review: {len(issues)} issues found")
            for issue in issues:
                self.display.step(f"  ⚠ {issue}")
        else:
            logger.info("Post-submission quality review for %s: no issues found", paper_id)
            self.display.step("Quality review: passed")

    def _save_paper_locally(self, paper_payload: dict) -> pathlib.Path:
        """Save the finished paper as JSON so it can be submitted later."""
        output_dir = _CHECKPOINT_DIR.parent / "papers"
        output_dir.mkdir(parents=True, exist_ok=True)
        title = paper_payload.get("title", "untitled")
        safe_title = "".join(
            c if c.isalnum() or c in " -_" else "_" for c in title
        )[:60].strip()
        ts = int(time.time())
        path = output_dir / f"{safe_title}_{ts}.json"
        path.write_text(json.dumps(paper_payload, indent=2, default=str))
        logger.info("Paper saved locally: %s", path)
        return path

    # ------------------------------------------------------------------
    # Review helper
    # ------------------------------------------------------------------

    def _do_review(self, paper) -> dict:
        """Review a single paper using the LLM."""
        paper_text = f"Title: {paper.title}\nAbstract: {paper.abstract}\n"
        if paper.sections:
            for sec in paper.sections[:10]:
                heading = sec.get("heading", "")
                content = sec.get("content", "")[:2000]
                paper_text += f"\n## {heading}\n{content}\n"

        system = self._prompts["peer_review"]
        prompt = f"""{paper_text[:10000]}

Review this paper. Return JSON with:
- "scores": dict with keys {_REVIEW_DIMENSIONS}, each 1-10
- "overall_score": float 1-10
- "decision": "accept", "revise", or "reject"
- "summary": 2-3 sentence summary of the review
- "strengths": list of 3-5 strengths
- "weaknesses": list of 3-5 weaknesses
- "questions_for_authors": list of 1-3 questions"""

        review = self.llm.generate_json(system, prompt)

        # Ensure required fields
        scores = review.get("scores", {})
        for dim in _REVIEW_DIMENSIONS:
            if dim not in scores:
                scores[dim] = 5

        decision = review.get("decision", "revise")
        if decision not in ("accept", "revise", "reject"):
            decision = "revise"

        try:
            result = self.client.submit_review(
                paper_id=paper.paper_id,
                scores=scores,
                decision=decision,
                summary=review.get("summary", "Review completed."),
                strengths=review.get("strengths", ["No specific strengths noted."]),
                weaknesses=review.get("weaknesses", ["No specific weaknesses noted."]),
                questions_for_authors=review.get("questions_for_authors"),
            )
            logger.info("Review submitted for %s: %s", paper.paper_id, decision)
            return result
        except Exception as e:
            logger.error("Review submission failed for %s: %s", paper.paper_id, e)
            return {"paper_id": paper.paper_id, "error": str(e)}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # Inline reasoning patterns — lines that are LLM "thinking out loud"
    _INLINE_REASONING_RE = re.compile(
        r"^\s*("
        r"I (?:need to|have to|must|should|will|would|can|shall)\b"
        r"|Let me\b"
        r"|Wait,|Actually,|Hmm,|Ok,|Okay,"
        r"|First, I|Next, I|Then, I|Now, I|Finally, I"
        r"|The paragraphs? should\b"
        r"|I must not\b|I have to include\b"
        r"|I (?:need|have) to (?:mention|include|add|write|produce|create|ensure|make sure)\b"
        r"|Let me (?:produce|write|create|think|consider)\b"
        r")",
        re.IGNORECASE,
    )

    def _extract_section_text(self, raw: str) -> str:
        """Extract clean section text from raw LLM output.

        Handles thinking tags, inline reasoning, accidental JSON wrapping,
        code fences, markdown headers, and \\boxed{} math wrappers.
        """
        # Strip all thinking/reasoning tags first
        text = strip_thinking_tags(raw).strip()

        # If model output JSON despite being asked for plain text, extract content
        if text.lstrip().startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "content" in parsed:
                    text = parsed["content"]
            except json.JSONDecodeError:
                start = text.find("{")
                end = text.rfind("}") + 1
                if start >= 0 and end > start:
                    try:
                        parsed = json.loads(text[start:end])
                        if isinstance(parsed, dict) and "content" in parsed:
                            text = parsed["content"]
                    except json.JSONDecodeError:
                        pass

        # Strip code fences, markdown headers, boxed wrappers, inline reasoning
        lines = text.split("\n")
        cleaned = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                continue
            if stripped.startswith("## ") or stripped.startswith("# "):
                continue
            if stripped.startswith("\\boxed{") and stripped.endswith("}"):
                stripped = stripped[7:-1]
                cleaned.append(stripped)
                continue
            # Drop lines that are inline LLM reasoning
            if self._INLINE_REASONING_RE.match(stripped):
                continue
            cleaned.append(line)

        return "\n".join(cleaned).strip()

    @staticmethod
    def _generate_tags(brief: dict, title: str) -> list[str]:
        """Generate tags from research brief for the API (requires >= 1)."""
        tags = set()

        # Extract from paper_type
        paper_type = brief.get("paper_type", "")
        if paper_type:
            tags.add(paper_type.lower().strip())

        # Extract from search_terms (first few words of each)
        for term in brief.get("search_terms", [])[:5]:
            # Take meaningful keywords (2-3 words max)
            words = [w.strip().lower() for w in term.split() if len(w) > 2]
            if words:
                tag = " ".join(words[:3])[:50]
                tags.add(tag)

        # Extract from scope_in
        for scope in brief.get("scope_in", [])[:3]:
            words = [w.strip().lower() for w in scope.split() if len(w) > 2]
            if words:
                tag = " ".join(words[:3])[:50]
                tags.add(tag)

        # Fallback: use title words
        if not tags:
            title_words = [w.lower().strip(",:;.") for w in title.split() if len(w) > 3]
            for tw in title_words[:3]:
                tags.add(tw)

        # Ensure at least 1 tag
        if not tags:
            tags.add("research")

        return list(tags)[:10]  # API max is 10

    def _get_relevant_synthesis(self, section_name: str, ev_map: dict) -> str:
        """E3: Filter synthesis matrix insights relevant to this section."""
        matrix = self.artifacts.get("synthesis_matrix", {})
        if not matrix:
            return ""

        # Collect paper titles associated with this section's evidence
        section_papers = set()
        for ev in ev_map.get(section_name, []):
            if isinstance(ev, dict):
                for s in ev.get("supporting_sources", []):
                    section_papers.add(str(s).lower()[:50])

        lines = []

        # Contradictions relevant to this section
        for c in matrix.get("contradictions", []):
            if isinstance(c, dict):
                desc = c.get("description", str(c))
                papers = [str(p).lower()[:50] for p in c.get("papers", [])]
                if section_papers & set(papers) or not section_papers:
                    lines.append(f"  Contradiction: {desc[:150]}")
            elif isinstance(c, str):
                lines.append(f"  Contradiction: {c[:150]}")

        # Gaps
        for g in matrix.get("gaps", []):
            gap_str = str(g)[:150]
            lines.append(f"  Gap: {gap_str}")

        # Themes with overlapping papers
        for t in matrix.get("themes", []):
            if isinstance(t, dict):
                t_papers = [str(p).lower()[:50] for p in t.get("supporting_papers", [])]
                if section_papers & set(t_papers) or not section_papers:
                    lines.append(f"  Theme: {t.get('name', '')}: {t.get('description', '')[:100]}")

        if not lines:
            return ""

        return (
            "SYNTHESIS INSIGHTS for this section:\n"
            + "\n".join(lines[:8])
            + "\nAddress contradictions and gaps where relevant.\n"
        )

    def _build_evidence_findings(
        self,
        section_name: str,
        section_evidence: list,
        memos: dict,
        cand_by_id: dict,
    ) -> str:
        """Build an evidence-first findings block for a section.

        E1: Uses resolved cite_keys from evidence map so each finding is
        pre-bound to the correct citation key the LLM must use.
        """
        if not section_evidence and not memos:
            return ""

        title_to_cite_key = self.artifacts.get("title_to_cite_key", {})
        lines = []

        # Extract concrete findings from evidence map entries with bound cite_keys
        if section_evidence:
            for ev in section_evidence:
                if not isinstance(ev, dict):
                    continue
                claim = ev.get("claim", "")
                strength = ev.get("strength", "moderate")
                if not claim:
                    continue
                # E1: Use resolved cite_keys if available, fall back to titles
                resolved = ev.get("resolved_cite_keys", [])
                if resolved:
                    for ck in resolved:
                        lines.append(f"- {ck}: '{claim}' (strength: {strength})")
                else:
                    sources = ev.get("supporting_sources", [])
                    source_str = ", ".join(str(s) for s in sources) if sources else "general"
                    lines.append(f"- [{source_str}]: '{claim}' (strength: {strength})")

        # Collect source titles relevant to this section from evidence map
        section_source_titles = set()
        if section_evidence:
            for ev in section_evidence:
                if isinstance(ev, dict):
                    for s in ev.get("supporting_sources", []):
                        section_source_titles.add(str(s).lower().strip()[:50])

        # Supplement with quotable claims from reading memos for this section
        if memos:
            for pid, memo in memos.items():
                if not isinstance(memo, dict):
                    continue
                title = memo.get("title", "")
                if section_source_titles and title.lower().strip()[:50] not in section_source_titles:
                    continue

                quotable = memo.get("quotable_claims", [])
                if isinstance(quotable, dict):
                    quotable = list(quotable.values())
                elif not isinstance(quotable, list):
                    quotable = []
                # E1: Use title_to_cite_key lookup first
                title_lower = title.lower()[:50].strip()
                cite_key = title_to_cite_key.get(title_lower)
                if not cite_key:
                    cand = cand_by_id.get(pid, {})
                    authors = cand.get("authors", [])
                    year = cand.get("year", "n.d.")
                    first_author = ""
                    if authors:
                        first_author = authors[0].split()[-1] if authors[0] else ""
                    if not first_author:
                        first_author = memo.get("title", "Unknown").split()[0].rstrip(",.:;")
                    cite_key = f"[{first_author}, {year}]" if year and year != "n.d." else f"[{first_author}]"

                for qc in quotable[:2]:
                    if isinstance(qc, str) and qc.strip():
                        lines.append(f"- {cite_key}: '{qc}'")

        return "\n".join(lines[:30])

    def _delay(self) -> None:
        if self.config.api_delay_seconds > 0:
            time.sleep(self.config.api_delay_seconds)

    def _log_artifact(self, name: str, data) -> None:
        if self.config.verbose:
            preview = json.dumps(data, indent=2, default=str)[:500] if isinstance(data, (dict, list)) else str(data)[:500]
            logger.info("  [%s] %s", name, preview)
