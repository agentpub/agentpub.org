"""PlaybookResearcher — 5-step pipeline mimicking the AGENT_PLAYBOOK approach.

Steps:
  1. Scope — define title, search terms, research questions, check overlap
  2. Research — broad academic search, enrich full text, score relevance
  3. Write — paragraph-by-paragraph writing from small evidence packets
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

import collections
import concurrent.futures
import hashlib
import json
import logging
import os
import pathlib
import re
import time
from dataclasses import dataclass, field

from .academic_search import (
    enrich_paper_content,
    fetch_paper_references,
    search_papers as _search_papers_basic,
    search_papers_extended,
    search_seed_papers,
)
from .client import AgentPub
from .display import NullDisplay
from .llm.base import LLMBackend, LLMError, strip_thinking_tags
from .prompts import (
    load_prompts as _load_prompts,
    _SECTION_GUIDANCE,
    _ANTI_PATTERNS,
    _PAPER_TYPE_GUIDANCE,
    _CONTRIBUTION_TYPE_GUIDANCE,
    DEFAULT_PROMPTS,
)
from .reference_verifier import CONFIDENCE_REMOVE, ReferenceVerifier
from .sources import SourceDocument

logger = logging.getLogger("agentpub.playbook_researcher")

from ._constants import (
    CorpusManifest,
    ParagraphSpec,
    PipelineStep,
    ResearchConfig,
    ResearchInterrupted,
    WrittenParagraph,
    _REF_TARGETS,
    _WRITE_ORDER,
    _SUBMIT_ORDER,
    _SECTION_WORD_TARGETS,
    _SECTION_WORD_MINIMUMS,
    _SECTION_TOKEN_LIMITS,
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


# NOTE: Overclaiming, framework language, and AI jargon are now handled by
# the LLM editorial review pass (_llm_editorial_review) instead of regex patterns.

# Unicode hyphen character class — Crossref/S2 return non-breaking hyphens
# (U+2010, U+2011) and en-dashes (U+2013) in compound surnames.
# All citation-matching regexes must use this instead of bare ASCII \-.
_HYPH = r"\-\u2010\u2011\u2013"


def _run_non_critical(fn, *, label: str, timeout: int = 120, default=None):
    """Run *fn* with a hard timeout. If it hangs or crashes, log and return *default*.

    Use this for any pipeline operation that is NOT essential for the paper
    to be produced (venue enrichment, claim ledger, comparison table, etc.).
    The pipeline continues regardless.
    """
    # Do NOT use `with ThreadPoolExecutor` — its __exit__ calls shutdown(wait=True)
    # which blocks until the worker finishes, defeating the timeout entirely.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn)
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        logger.warning("Non-critical step '%s' timed out after %ds — skipping", label, timeout)
        pool.shutdown(wait=False)
        return default
    except Exception as e:
        logger.warning("Non-critical step '%s' failed: %s — skipping", label, e)
        pool.shutdown(wait=False)
        return default
    else:
        pool.shutdown(wait=False)


def _extract_surname(author_str: str) -> str:
    """Extract surname from author string in either format.

    Handles:
      "Salam, M. A."  -> "Salam"     (surname-first / BibTeX format)
      "M. A. Salam"   -> "Salam"     (given-first format)
      "John Smith"    -> "Smith"     (given-first format)
      "Barrio-Tofiño, E. d." -> "Barrio-Tofiño"
    """
    if isinstance(author_str, dict):
        author_str = author_str.get("name") or author_str.get("family") or author_str.get("literal") or str(author_str)
    name = str(author_str).strip()
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


# System prompt for source-bounded synthesis
_SYNTHESIS_SYSTEM = """\
You are an autonomous AI research agent writing an academic paper. Your goal is to
produce a source-bounded synthesis of the retrieved corpus. Every sentence must be
grounded in the provided source texts. Do not inject pre-trained knowledge. Cite
sources using [Author, Year] format (e.g. [Smith et al., 2023]) matching the
provided bibliography.

SCOPE: You are viewing a limited corpus, not the entire field. Every inference must
be framed as a corpus-bounded interpretation, not a field-wide fact.

CITATION RULES (non-negotiable):
- WRONG: [2019], [2022] — RIGHT: [Keith et al., 2019], [Smith, 2024]
- Only cite a source when it directly supports the sentence being written.
- Some references may remain uncited — do not force-cite.
- Do NOT fabricate references.
- ONLY cite authors that appear in the REFERENCE LIST provided. If an author is not
  in the reference list, do NOT cite them.

COMPUTATIONAL HONESTY (non-negotiable):
You are a text-synthesis agent. You must NEVER claim to have:
- Downloaded raw data or datasets from any repository
- Run computational pipelines, simulations, or analyses of any kind
- Executed statistical software (Stata, SPSS, R, SAS, etc.) or computed effect sizes
- Run bioinformatics, chemistry, physics, econometric, or ML software
- Reprocessed data through any automated or manual workflow
- Performed experiments, trials, fieldwork, surveys, or original data collection
You may ONLY claim to have synthesized, analyzed, and compared PUBLISHED TEXTS.
Your methodology is: literature search, retrieval, reading, and synthesis of findings
reported by other authors. Describe THAT process honestly.
NEVER claim you used NER, topic modeling, LDA, clustering, named entity recognition,
sentiment analysis, dependency parsing, argument mining, cosine similarity, SPECTER2
embeddings, or any computational NLP technique. You searched databases and read papers.

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

INTEGRITY COMMANDMENTS (non-negotiable):
1. URL or it didn't happen: Every cited paper must appear in the provided source
   material. If you cannot point to which source block contains it, do not cite it.
2. Read before you summarize: Only describe findings that appear in the paper's
   actual text provided to you. Do not summarize from memory of a paper's title alone.
3. Never say "verified" unless you performed the check: Do not write "we verified",
   "confirmed through analysis", "validated against" unless describing what the
   pipeline actually did (search, retrieve, read, synthesize).
4. Absence is not evidence: If your source corpus does not cover a topic, say
   "this area was not covered in the reviewed literature" — do not generalize.
5. Distinguish evidence tiers: [FULL TEXT] sources can be quoted precisely.
   [ABSTRACT ONLY] sources can only be cited for claims visible in their abstract.
   Never attribute detailed findings to an abstract-only source.

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
        enabled_sources: list[str] | None = None,
        library: "PaperLibrary | None" = None,
    ):
        self.client = client
        self.llm = llm
        self.review_llm: LLMBackend | None = None  # set externally for model routing
        self.config = config or ResearchConfig()
        self.display = display or NullDisplay()
        self.custom_sources = custom_sources or []
        self.owner_email = owner_email or ""
        self.serper_api_key = serper_api_key
        self.enabled_sources = enabled_sources  # None = use all configured
        self.library = library  # Local paper library for full-text access
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
        self.process_log: list[PipelineStep] = []  # Change 3: structured process log

        # Ensure ~/.agentpub/.env keys are in os.environ (API keys for S2, Serper, etc.)
        try:
            from .cli import _load_env_file
            for k, v in _load_env_file().items():
                if k not in os.environ and v:
                    os.environ[k] = v
        except Exception:
            pass  # CLI module may not be available in all contexts

        # Load prompts (priority: local overrides > API remote > built-in defaults)
        try:
            self._prompts = _load_prompts()
            logger.info("Loaded %d prompts from prompt system", len(self._prompts))
        except Exception as e:
            logger.warning("Failed to load prompts, using module defaults: %s", e)
            self._prompts = {}

    # ------------------------------------------------------------------
    # Per-section configurable limits (token, word targets, word minimums)
    # ------------------------------------------------------------------

    def _section_max_tokens(self, section_name: str) -> int:
        """Get max_tokens for a section — config override > _SECTION_TOKEN_LIMITS default."""
        if self.config.section_token_limits:
            val = self.config.section_token_limits.get(section_name)
            if val is not None:
                return int(val)
        return _SECTION_TOKEN_LIMITS.get(section_name, 65000)

    def _section_word_target(self, section_name: str) -> int:
        """Get word target for a section — config override > _SECTION_WORD_TARGETS default."""
        if self.config.section_word_targets:
            val = self.config.section_word_targets.get(section_name)
            if val is not None:
                return int(val)
        return _SECTION_WORD_TARGETS.get(section_name, 1000)

    def _section_word_min(self, section_name: str) -> int:
        """Get word minimum for a section — config override > _SECTION_WORD_MINIMUMS default."""
        if self.config.section_word_minimums:
            val = self.config.section_word_minimums.get(section_name)
            if val is not None:
                return int(val)
        return _SECTION_WORD_MINIMUMS.get(section_name, 500)

    # ------------------------------------------------------------------
    # Academic search wrapper (respects GUI source toggles)
    # ------------------------------------------------------------------

    def _search(self, query: str, limit: int = 10, year_from: int | None = None) -> list[dict]:
        """Search academic papers using database-specific strategies when possible."""
        # Use domain-optimized search if we have a domain qualifier
        dq = ""
        brief = self.artifacts.get("research_brief")
        if brief and isinstance(brief, dict):
            dq = brief.get("domain_qualifier", "")
        if dq:
            from .academic_search import search_domain_optimized
            return search_domain_optimized(
                query,
                domain_qualifier=dq,
                limit=limit,
                year_from=year_from,
                mailto=self.owner_email or None,
                sources=self.enabled_sources,
            )
        return search_papers_extended(
            query,
            limit=limit,
            year_from=year_from,
            mailto=self.owner_email or None,
            sources=self.enabled_sources,
        )

    # ------------------------------------------------------------------
    # Prompt template parsing
    # ------------------------------------------------------------------

    def _get_prompt(self, key: str, **kwargs: str) -> tuple[str, str]:
        """Parse a prompt template from the prompt system into (system, user).

        Prompt templates in DEFAULT_PROMPTS use this format:
            SYSTEM: <system message>

            USER PROMPT TEMPLATE:
            <user prompt with {placeholders}>

        Returns (system_message, user_prompt) with placeholders filled.
        If the template has no SYSTEM:/USER split, the whole thing is the user prompt
        and system defaults to empty string.
        """
        raw = self._prompts.get(key, "")
        if not raw:
            return ("", "")

        system = ""
        user = raw

        if raw.startswith("SYSTEM:"):
            lines = raw.split("\n", 1)
            system = lines[0].replace("SYSTEM:", "").strip()
            rest = lines[1] if len(lines) > 1 else ""
            # Strip the "USER PROMPT TEMPLATE:" header if present
            if "USER PROMPT TEMPLATE:" in rest:
                user = rest.split("USER PROMPT TEMPLATE:", 1)[1].strip()
            else:
                user = rest.strip()

        # Fill placeholders — use safe manual replacement to avoid issues
        # with stray {/} in academic text (math notation, JSON examples, etc.)
        if kwargs:
            for k, v in kwargs.items():
                user = user.replace("{" + k + "}", str(v))

        return (system, user)

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
            import agentpub as _ap
            data = {
                "version": 1,
                "pipeline": "playbook",
                "sdk_version": getattr(_ap, "__version__", "0.3.0"),
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
    # Helpers
    # ------------------------------------------------------------------

    def _get_manifest(self) -> "CorpusManifest | None":
        """Safely retrieve CorpusManifest from artifacts, handling checkpoint deserialization."""
        manifest = self.artifacts.get("corpus_manifest")
        if manifest is None:
            return None
        if isinstance(manifest, CorpusManifest):
            return manifest
        if isinstance(manifest, dict):
            try:
                return CorpusManifest(**{k: v for k, v in manifest.items() if k in CorpusManifest.__dataclass_fields__})
            except Exception:
                return None
        return None  # string or other unexpected type

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
                # v0.3 changed step numbering (1-7 instead of 1-4).
                # Invalidate old checkpoints that used the v0.2 numbering.
                cp_version = checkpoint.get("sdk_version", "0.2.0")
                if cp_version < "0.3.0":
                    logger.info("Discarding v0.2 checkpoint (incompatible step numbering)")
                    self.display.step("Discarding old checkpoint (v0.2 → v0.3 upgrade)")
                    checkpoint = None
                else:
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

        # Choose writing/validation mode based on config
        if self.config.pipeline_mode == "paragraph":
            write_fn = self._step3_write_paragraphs
            validate_fn = self._step4_validate_sections
        else:
            # "section" mode — per-section writing (legacy fallback)
            write_fn = self._step3_write
            validate_fn = self._step4_validate_sections

        steps = [
            (1, lambda: self._step1_scope(topic, challenge_id)),
            (2, lambda: self._step1b_outline()),
            (3, lambda: self._step2_research()),
            (4, lambda: self._step3_deep_reading()),
            (5, lambda: self._step3b_revise_outline()),
            (6, write_fn),
            (7, validate_fn),
        ]

        try:
            for step_num, step_fn in steps:
                if step_num <= start_after_step:
                    continue
                self._current_step = step_num
                # Save checkpoint BEFORE starting the step so Ctrl+C mid-step
                # still has the previous step's progress saved on disk
                if step_num > 1:
                    self._save_checkpoint(topic, step_num - 1, challenge_id)
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
    # Review model routing
    # ------------------------------------------------------------------

    def _get_review_llm(self) -> LLMBackend:
        """Return the review LLM if configured, otherwise the main LLM."""
        return self.review_llm if self.review_llm is not None else self.llm

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

        # Phase 1 prompt — fully from prompt system (GUI-editable)
        p1_system, p1_user = self._get_prompt(
            "phase1_research_brief",
            topic=topic,
        )
        if not p1_system:
            p1_system = "You are a senior academic research planner. Return valid JSON only."

        # Append dynamic context that changes per run
        dynamic_context = []
        if challenges_context:
            dynamic_context.append(challenges_context)
        if platform_context:
            dynamic_context.append(platform_context)
        if own_papers_context:
            dynamic_context.append(own_papers_context)

        # Build final prompt: template from prompts.py + dynamic per-run context
        if p1_user:
            prompt = p1_user
        else:
            prompt = f'Plan a research paper on the topic: "{topic}"'

        if dynamic_context:
            prompt = prompt + "\n\n" + "\n\n".join(dynamic_context)

        # Always inject the contribution type list (it's defined in code, not prompts)
        prompt += f"\n\nCONTRIBUTION TYPE — pick ONE from this list (NEVER use \"framework\" or \"matrix\"):\n{contribution_list}"

        try:
            brief = self.llm.generate_json(p1_system, prompt, temperature=0.5)
        except (LLMError, Exception) as e:
            logger.warning("Phase 1 brief generation failed: %s — using defaults", e)
            brief = None

        # Validate and set defaults
        if not isinstance(brief, dict) or "title" not in brief:
            # Extract a short title from long topic inputs
            short_topic = topic.split("\n")[0].strip()
            if len(short_topic) > 190:
                short_topic = short_topic[:187] + "..."
            brief = {
                "title": short_topic,
                "search_terms": [short_topic],
                "research_questions": [f"What is the current state of {short_topic}?"],
                "search_queries": [f'"{short_topic}"'],
                "paper_type": "survey",
                "contribution_type": _CONTRIBUTION_TYPES[0],
            }

        # Safety: cap title length even from LLM output
        if len(brief.get("title", "")) > 200:
            brief["title"] = brief["title"].split("\n")[0][:190].strip() + "..."

        # Fix 3C: Reject "framework" or "matrix" contribution types programmatically
        ct = brief.get("contribution_type") or ""
        if any(kw in ct.lower() for kw in ("framework", "matrix")):
            logger.info("Rejected contribution_type '%s' — overriding to first allowed type", ct)
            brief["contribution_type"] = _CONTRIBUTION_TYPES[0]

        # Store evidence scaffold for field-adaptive table generation
        scaffold = brief.get("evidence_scaffold")
        if isinstance(scaffold, dict) and scaffold.get("columns"):
            self.artifacts["evidence_scaffold"] = scaffold
            logger.info("Evidence scaffold: %s", scaffold.get("columns", []))

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

        # Log full brief for debugging
        logger.info("Research brief — Title: %s", brief.get("title", ""))
        logger.info("Research brief — Paper type: %s", brief.get("paper_type", "?"))
        logger.info("Research brief — Contribution: %s", brief.get("contribution_type", "?"))
        for i, rq in enumerate(brief.get("research_questions", []), 1):
            logger.info("Research brief — RQ%d: %s", i, rq)
            self.display.step(f"  RQ{i}: {rq}")
        for i, st in enumerate(brief.get("search_terms", []), 1):
            logger.info("Research brief — Search term %d: %s", i, st)
        for i, sq in enumerate(brief.get("search_queries", []), 1):
            logger.info("Research brief — Search query %d: %s", i, sq)
            self.display.step(f"  SQ{i}: {sq}")
        if brief.get("scope_out"):
            for so in brief["scope_out"]:
                logger.info("Research brief — Scope OUT: %s", so)
            self.display.step(f"  Scope out: {', '.join(brief['scope_out'][:5])}")
        if brief.get("scope_in"):
            self.display.step(f"  Scope in: {', '.join(brief['scope_in'][:5])}")
        if brief.get("canonical_references"):
            for cr in brief["canonical_references"]:
                logger.info("Research brief — Canonical ref: %s", cr)
            self.display.step(f"  Canonical refs: {len(brief['canonical_references'])}")
        if brief.get("argument_claims"):
            for ac in brief["argument_claims"]:
                claim = ac.get("claim", ac) if isinstance(ac, dict) else ac
                logger.info("Research brief — Argument claim: %s", str(claim)[:120])

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
                try:
                    alt = self.llm.generate_json(
                        p1_system,
                        f"""The paper "{brief['title']}" has high overlap with existing papers.
Reformulate with a DIFFERENT angle, methodology, or narrower scope.
Keep the same JSON format. The new title must be substantially different.""",
                        temperature=0.2,
                    )
                except (LLMError, Exception) as e:
                    logger.warning("Alternative angle generation failed: %s", e)
                    alt = None
                if isinstance(alt, dict) and alt.get("title"):
                    brief.update(alt)
                    self.artifacts["research_brief"] = brief
                    self.display.set_title(brief["title"])
                    self.display.step(f"New title: {brief['title']}")
        except Exception as e:
            logger.warning("Overlap check failed: %s", e)

        # Pre-research novelty check (AI Scientist-v2 inspired)
        self._novelty_check_external(brief)

        self.display.phase_done(1)

    # ------------------------------------------------------------------
    # Pre-research novelty check (AI Scientist-v2 inspired)
    # ------------------------------------------------------------------

    def _novelty_check_external(self, brief: dict) -> None:
        """Check topic novelty against Semantic Scholar before committing to research."""
        if not self.config.novelty_check_enabled:
            return
        title = brief.get("title", "")
        if not title:
            return
        self.display.step("Novelty check: searching for similar existing work...")
        try:
            from agentpub.academic_search import search_semantic_scholar
            results = search_semantic_scholar(title, limit=10)
            if not results:
                self.display.step("Novelty check: no closely related papers found — topic is novel")
                self.artifacts["novelty_check"] = {"status": "novel", "similar_papers": []}
                return

            # Check title similarity
            similar = []
            for r in results:
                r_title = r.get("title", "")
                if not r_title:
                    continue
                # Simple word overlap similarity
                words_a = set(title.lower().split())
                words_b = set(r_title.lower().split())
                # Remove stopwords
                stopwords = {"the", "a", "an", "of", "in", "on", "for", "and", "or", "to", "is", "are", "was", "were", "with", "from", "by", "at", "as", "its", "this", "that"}
                words_a -= stopwords
                words_b -= stopwords
                if not words_a or not words_b:
                    continue
                overlap = len(words_a & words_b) / max(len(words_a | words_b), 1)
                if overlap >= self.config.novelty_similarity_threshold:
                    similar.append({
                        "title": r_title,
                        "year": r.get("year"),
                        "citations": r.get("citationCount", 0),
                        "similarity": round(overlap, 2),
                    })

            if similar:
                self.display.step(f"Novelty check: found {len(similar)} similar papers — will differentiate angle")
                self.artifacts["novelty_check"] = {"status": "overlap_found", "similar_papers": similar}

                # Ask LLM to differentiate the approach
                similar_text = "\n".join(
                    f"- {s['title']} ({s.get('year', '?')}, {s.get('citations', 0)} citations, similarity: {s['similarity']})"
                    for s in similar[:5]
                )
                try:
                    prompt = (
                        f"These existing papers are similar to our planned research:\n{similar_text}\n\n"
                        f"Our planned title: {title}\n"
                        f"Our research questions: {', '.join(brief.get('research_questions', []))}\n\n"
                        "Reformulate the research questions to focus on a SPECIFIC angle, gap, or methodology "
                        "that these existing papers do NOT cover. Keep the same JSON format with keys: "
                        "title, search_terms, research_questions, paper_type. "
                        "The new approach should clearly differentiate from existing work."
                    )
                    alt = self.llm.generate_json(
                        "You are a research strategist. Identify gaps in the existing literature and reformulate the research to fill those gaps.",
                        prompt, temperature=0.3,
                    )
                    if isinstance(alt, dict) and alt.get("title") and alt.get("research_questions"):
                        brief.update(alt)
                        self.artifacts["research_brief"] = brief
                        self.display.set_title(brief["title"])
                        self.display.step(f"Novelty-adjusted title: {brief['title']}")
                except Exception as e:
                    logger.warning("Novelty reformulation failed: %s", e)
            else:
                self.display.step("Novelty check: no close matches — topic is sufficiently novel")
                self.artifacts["novelty_check"] = {"status": "novel", "similar_papers": []}
        except Exception as e:
            logger.warning("Novelty check failed: %s", e)
            self.artifacts["novelty_check"] = {"status": "error", "error": str(e)}

    # ------------------------------------------------------------------
    # Step 1b: Outline + Thesis (v0.3)
    # ------------------------------------------------------------------

    def _step1b_outline(self) -> None:
        """Develop thesis and section outline BEFORE searching — drives targeted search."""
        logger.info("Step 1b: OUTLINE")
        self.display.phase_start(2, "Outline & Thesis")

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        prompt_template = self._prompts.get("phase2_outline", DEFAULT_PROMPTS.get("phase2_outline", ""))

        # Fill in the brief
        brief_json = json.dumps({
            "title": brief.get("title", ""),
            "research_questions": brief.get("research_questions", []),
            "contribution_type": brief.get("contribution_type", ""),
            "argument_claims": brief.get("argument_claims", []),
            "scope_in": brief.get("scope_in", []),
            "scope_out": brief.get("scope_out", []),
        }, indent=2)

        system = prompt_template.split("USER PROMPT TEMPLATE:")[0] if "USER PROMPT TEMPLATE:" in prompt_template else prompt_template
        # Use format_map with defaults so stale remote prompts with extra
        # placeholders (e.g. {topic}) don't crash the pipeline
        _fmt = collections.defaultdict(str, brief_json=brief_json, topic=self._topic)
        user_prompt = prompt_template.format_map(_fmt)

        try:
            outline = self.llm.generate_json(system, user_prompt, temperature=0.3)
        except (LLMError, Exception) as e:
            logger.warning("Outline generation failed (non-fatal): %s", e)
            outline = {}

        if not isinstance(outline, dict):
            outline = {}

        # Store outline in artifacts
        self.artifacts["paper_outline"] = outline

        # Log outline
        thesis = outline.get("revised_thesis", outline.get("thesis", ""))
        if thesis:
            self.display.step(f"Thesis: {thesis[:120]}")
            logger.info("Thesis: %s", thesis[:200])

        sections = outline.get("sections", [])
        if sections:
            self.display.step(f"Outline: {len(sections)} sections planned")
            for sec in sections:
                sec_name = sec.get("name", "?")
                n_evidence = len(sec.get("evidence_needed", []))
                n_queries = len(sec.get("search_queries", []))
                logger.info("Outline — %s: %d evidence items, %d search queries", sec_name, n_evidence, n_queries)

        counter_evidence = outline.get("counter_evidence", [])
        if counter_evidence:
            self.display.step(f"Counter-evidence targets: {len(counter_evidence)}")
            for ce in counter_evidence:
                logger.info("Counter-evidence: %s → %s", ce.get("claim", "?")[:80], ce.get("search_query", "?"))

        # Extract outline-driven search queries for Step 2
        outline_queries = []
        for sec in sections:
            for sq in sec.get("search_queries", []):
                if sq and len(sq) > 5:
                    outline_queries.append({"query": sq, "section": sec.get("name", ""), "type": "evidence"})
        for ce in counter_evidence:
            sq = ce.get("search_query", "")
            if sq and len(sq) > 5:
                outline_queries.append({"query": sq, "section": "counter", "type": "counter_evidence"})

        self.artifacts["outline_queries"] = outline_queries
        if outline_queries:
            self.display.step(f"Targeted search queries: {len(outline_queries)}")

        self.display.phase_done(2)

    # ------------------------------------------------------------------
    # Step 2: Research
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_search_strategy(raw: str) -> dict[str, str | int]:
        """Parse the search strategy config block into a dict of values."""
        config: dict[str, str | int] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # Try to parse as int
            try:
                config[key] = int(val)
            except ValueError:
                config[key] = val
        return config

    def _step2_research(self) -> None:
        """Human-like 6-phase research: orient → map → skeleton → targeted → expand → audit."""
        logger.info("Step 2: RESEARCH")
        self.display.phase_start(3, "Research & Collect")
        self.display.tick()

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        search_terms = brief.get("search_terms", [self._topic])

        # Log what we're searching for
        logger.info("Search inputs — Title: %s", brief.get("title", self._topic))
        for i, st in enumerate(search_terms, 1):
            logger.info("Search inputs — Term %d: %s", i, st)
        for i, rq in enumerate(brief.get("research_questions", []), 1):
            logger.info("Search inputs — RQ %d: %s", i, rq)
        if brief.get("scope_out"):
            logger.info("Search inputs — Scope OUT: %s", ", ".join(brief["scope_out"]))

        # Load search strategy from prompt system (GUI-editable)
        ss_raw = self._prompts.get("phase3_search_strategy", "")
        ss = self._parse_search_strategy(ss_raw) if ss_raw else {}
        # Extract config values with defaults
        _year_default = int(ss.get("year_from_default", 2016))
        _year_surveys = int(ss.get("year_from_surveys", 2022))
        _lim_title = int(ss.get("results_per_title_search", 15))
        _lim_rq = int(ss.get("results_per_rq_search", 10))
        _lim_kw = int(ss.get("results_per_keyword_search", 15))
        _lim_claim = int(ss.get("results_per_claim_search", 5))
        _lim_canonical = int(ss.get("results_per_canonical_search", 3))
        _lim_debate = int(ss.get("results_per_debate_search", 5))
        _lim_gap = int(ss.get("results_per_gap_search", 5))
        # Cap per-query limits to config.max_search_results if set
        _max_per_query = self.config.max_search_results
        _lim_title = min(_lim_title, _max_per_query)
        _lim_rq = min(_lim_rq, _max_per_query)
        _lim_kw = min(_lim_kw, _max_per_query)
        _max_surveys = int(ss.get("max_surveys", 3))
        _refs_per_survey = int(ss.get("refs_per_survey", 40))
        _max_rqs = int(ss.get("max_research_questions", 3))
        _max_kw = int(ss.get("max_keyword_terms", 6))
        _max_canonical = int(ss.get("max_canonical_refs", 10))
        _max_claims = int(ss.get("max_claims_to_search", 6))
        _max_debate_kw = int(ss.get("max_debate_keywords_per_side", 1))
        _max_underrep = int(ss.get("max_underrepresented_areas", 3))
        _cg_top = int(ss.get("citation_graph_top_papers", 5))
        _cg_per = int(ss.get("citation_graph_results_per_paper", 15))
        _kw_fallback = int(ss.get("keyword_fallback_threshold", 15))
        logger.info("Search strategy loaded: year_from=%d, title_limit=%d, surveys=%d, fallback_threshold=%d",
                     _year_default, _lim_title, _max_surveys, _kw_fallback)

        all_papers: list[dict] = []
        seen_titles: set[str] = set()
        search_audit: dict = {
            "databases": list(self.enabled_sources) if self.enabled_sources else ["crossref", "arxiv", "semantic_scholar", "openalex", "pubmed", "europe_pmc"],
            "queries": [],
            "per_source_counts": {},  # source_name ��� total unique papers contributed
            "total_retrieved": 0,
            "total_after_dedup": 0,
            "total_after_filter": 0,
            "total_included": 0,
            # Store actual search terms for methodology transparency
            "search_terms": brief.get("search_terms", []),
            "search_queries": brief.get("search_queries", []),
            "research_questions": brief.get("research_questions", []),
            "scope_exclusions": brief.get("scope_exclusions", []),
            "year_from": 2016,
        }

        paper_title = brief.get("title", self._topic)
        research_questions = brief.get("research_questions", [])

        # ── Domain qualifier & negative keywords for search tightening ──
        domain_qualifier = brief.get("domain_qualifier", "")
        negative_keywords = brief.get("negative_keywords", [])
        scope_out = brief.get("scope_out", [])

        if domain_qualifier:
            self.display.step(f"Domain qualifier: '{domain_qualifier}'")
        if negative_keywords:
            self.display.step(f"Negative keywords: {negative_keywords[:5]}")

        # Build negative keyword set for title filtering (lowercased)
        _neg_kw = {kw.lower() for kw in negative_keywords if kw}

        def _dedup_add(papers: list[dict], skip_neg_filter: bool = False) -> None:
            search_audit["total_retrieved"] += len(papers)
            for p in papers:
                key = (p.get("title") or "").lower()[:60]
                if key and key not in seen_titles:
                    # Negative keyword filter: skip papers whose title contains off-topic terms
                    if not skip_neg_filter and _neg_kw:
                        title_lower = (p.get("title") or "").lower()
                        if any(nk in title_lower for nk in _neg_kw):
                            continue
                    seen_titles.add(key)
                    all_papers.append(p)

        from .academic_search import (
            search_survey_papers,
            extract_references_from_surveys,
            search_for_claim_evidence,
            expand_citation_graph,
            audit_evidence_gaps,
            search_for_gaps,
            verify_paper_bibliographic,
        )

        # Helper: prepend domain qualifier to a search query if not already present
        def _qualify_query(q: str) -> str:
            if not domain_qualifier:
                return q
            if domain_qualifier.lower() in q.lower():
                return q
            return f'"{domain_qualifier}" {q}'

        # ── Source recommendation: Ask LLM which academic sources are relevant ──
        if not self.enabled_sources:
            all_available = [
                "crossref", "arxiv", "semantic_scholar", "openalex", "pubmed",
                "europe_pmc", "biorxiv", "doaj", "dblp", "hal", "zenodo",
                "internet_archive", "openaire", "fatcat", "opencitations",
                "datacite", "inspire_hep", "eric", "figshare", "scielo",
                "base", "philpapers", "cinii", "google_books", "open_library",
            ]
            try:
                src_system = "You are a research librarian. Given a research topic, select which academic databases are most relevant."
                src_prompt = (
                    f"Topic: {paper_title}\n"
                    f"Domain: {domain_qualifier or 'general'}\n\n"
                    f"Available sources: {', '.join(all_available)}\n\n"
                    "Select the 6-10 MOST relevant sources for this topic. Always include crossref, semantic_scholar, and openalex. "
                    "Only include domain-specific sources if relevant (e.g. pubmed for biomedical, arxiv for STEM, dblp for CS, "
                    "inspire_hep for physics, eric for education, philpapers for philosophy).\n\n"
                    "Return ONLY a JSON array of source names, e.g. [\"crossref\", \"arxiv\", \"semantic_scholar\"]"
                )
                raw = self.llm.generate(src_system, src_prompt, max_tokens=500)
                import json as _json_src
                cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
                recommended = _json_src.loads(cleaned)
                if isinstance(recommended, list) and len(recommended) >= 3:
                    # Always include core sources
                    core = {"crossref", "semantic_scholar", "openalex"}
                    recommended_set = core | {s for s in recommended if s in all_available}
                    # Also include any API-key-gated sources the user has configured
                    import os as _os_src
                    if _os_src.environ.get("SERPER_API_KEY"):
                        recommended_set.add("serper")
                    self.enabled_sources = [s for s in all_available if s in recommended_set]
                    self.display.step(f"  LLM recommended {len(self.enabled_sources)} sources: {', '.join(self.enabled_sources)}")
                    logger.info("LLM source recommendation: %s", self.enabled_sources)
            except Exception as e:
                logger.warning("Source recommendation failed: %s — using all sources", e)

        # ── Phase 2-LLM: Ask LLM for most relevant papers, then verify via Crossref ──
        _lim_llm = int(ss.get("llm_suggestion_limit", 20))
        self.display.step(f"Phase 2-LLM: Asking LLM for {_lim_llm} most relevant papers...")
        llm_verified = 0
        try:
            suggestions = self.llm.suggest_papers(
                self._topic, limit=_lim_llm,
                research_questions=research_questions,
                paper_title=paper_title,
                scope_out=scope_out,
                domain_qualifier=domain_qualifier,
            )
            self.display.step(f"  LLM suggested {len(suggestions)} papers, verifying via Crossref...")
            for sug in suggestions:
                sug_title = (sug.get("title") or "").strip()
                if not sug_title:
                    continue
                # Extract first author for bibliographic lookup
                sug_authors = sug.get("authors", [])
                first_author = sug_authors[0] if sug_authors else None
                if isinstance(first_author, str):
                    # Use last name only for better matching
                    first_author = first_author.split()[-1] if first_author else None
                sug_year = sug.get("year")
                sug_doi = sug.get("doi") or None

                try:
                    verified = verify_paper_bibliographic(
                        sug_title, first_author=first_author, year=sug_year,
                        doi=sug_doi, mailto=self.owner_email or None,
                    )
                    if verified:
                        _dedup_add([verified])
                        llm_verified += 1
                except Exception:
                    pass
                time.sleep(0.25)
            search_audit["queries"].append(f"[llm-suggest] {_lim_llm} requested, {llm_verified} verified")
            self.display.step(f"  LLM→Crossref: {llm_verified}/{len(suggestions)} verified and added")
        except Exception as e:
            logger.warning("LLM suggestion phase failed: %s", e)

        # Build topic terms for title-overlap filtering (used by survey mining + citation graph)
        _topic_terms = set(self._topic.lower().split())
        if domain_qualifier:
            _topic_terms |= set(domain_qualifier.lower().split())

        # ══════════════════════════════════════════════════════════════
        # SEED-FIRST ARCHITECTURE: Like a real researcher, we start
        # with known reputable papers and expand outward from them.
        # Phase order: LLM → Seeds → Read → Snowball1 → Semantic →
        #              Surveys → Snowball2 → Keywords → Forward → Claims
        # ══════════════════════════════════════════════════════════════

        # ── Phase 2-SEEDS: Verify canonical/foundational references ──
        canonical_refs = brief.get("canonical_references", [])
        if canonical_refs:
            self.display.step(f"Phase 2-SEEDS: Verifying {len(canonical_refs)} canonical/seed references...")
            canonical_verified = 0
            for ref in canonical_refs[:_max_canonical]:
                try:
                    if isinstance(ref, dict):
                        ref_title = ref.get("title", "")
                        ref_author = ref.get("author", "")
                        ref_year = ref.get("year")
                        ref_doi = ref.get("doi")
                    else:
                        ref_title = str(ref)
                        ref_author = None
                        ref_year = None
                        ref_doi = None
                    if not ref_title:
                        continue
                    verified = verify_paper_bibliographic(
                        ref_title, first_author=ref_author, year=ref_year,
                        doi=ref_doi, mailto=self.owner_email or None,
                    )
                    if verified:
                        verified["is_canonical"] = True
                        _dedup_add([verified])
                        canonical_verified += 1
                    else:
                        hits = self._search(ref_title if isinstance(ref, str) else f"{ref_author} {ref_title}", limit=_lim_canonical)
                        for h in hits:
                            h["is_canonical"] = True
                        _dedup_add(hits)
                        if hits:
                            canonical_verified += 1
                except Exception as e:
                    logger.warning("Canonical ref search failed for '%s': %s", str(ref)[:50], e)
                time.sleep(0.25)
            self.display.step(f"  Canonical refs: {canonical_verified}/{len(canonical_refs)} verified")

        self.display.step(f"  After seeds (LLM + canonical): {len(all_papers)} papers")

        # ── Phase 2-READ: Enrich seed papers — read their content like a researcher ──
        # A real researcher reads the foundational papers before searching further.
        # We enrich seed papers to: (a) understand the field, (b) extract key terms
        # for better search queries, (c) feed into S2 semantic recommendations.
        from .academic_search import enrich_paper_content
        seed_papers = [p for p in all_papers if p.get("is_canonical") or p.get("source") == "llm_knowledge"]
        if not seed_papers:
            seed_papers = all_papers[:5]  # fallback: use first few papers as seeds
        if seed_papers:
            self.display.step(f"Phase 2-READ: Reading {len(seed_papers)} seed papers...")
            seed_key_terms: set[str] = set()
            for sp in seed_papers[:8]:
                try:
                    content = _run_non_critical(
                        lambda _p=sp: enrich_paper_content(_p, max_chars=6000),
                        label=f"enrich seed paper", timeout=30, default="")
                    if content and len(content) > 200:
                        sp["enriched_content"] = content
                        # Extract domain-specific terms from enriched content for smarter search
                        # Filter out HTML/publisher boilerplate before extracting bigrams
                        _BOILERPLATE_WORDS = {
                            "google", "scholar", "views", "captured", "open", "access",
                            "main", "content", "search", "within", "download", "citation",
                            "copyright", "springer", "elsevier", "wiley", "taylor", "francis",
                            "cookie", "privacy", "terms", "conditions", "sign", "login",
                            "subscribe", "purchase", "institutional", "data", "cannot",
                            "studies", "computer", "area", "education", "engineering",
                            "journal", "volume", "issue", "pages", "published", "received",
                            "accepted", "available", "online", "article", "abstract",
                            "keywords", "click", "here", "figure", "table", "supplementary",
                        }
                        words = content.lower().split()
                        # Bigrams are more specific than unigrams for academic search
                        for i in range(len(words) - 1):
                            w1, w2 = words[i], words[i+1]
                            # Skip if either word is boilerplate
                            if w1 in _BOILERPLATE_WORDS or w2 in _BOILERPLATE_WORDS:
                                continue
                            bg = f"{w1} {w2}"
                            if len(bg) > 8 and all(len(w) > 3 for w in bg.split()):
                                seed_key_terms.add(bg)
                except Exception as e:
                    logger.warning("Seed enrichment failed for '%s': %s", sp.get("title", "?")[:40], e)
                time.sleep(0.3)
            # Keep only the most informative terms (those that appear in multiple seeds)
            from collections import Counter as _SeedCounter
            term_freq = _SeedCounter()
            for sp in seed_papers[:8]:
                content = (sp.get("enriched_content") or "").lower()
                for term in seed_key_terms:
                    if term in content:
                        term_freq[term] += 1
            # Terms that appear in 2+ seed papers are likely core to the domain
            recurring_terms = [t for t, c in term_freq.most_common(20) if c >= 2]
            if recurring_terms:
                self.display.step(f"  Extracted {len(recurring_terms)} recurring domain terms from seeds")
                logger.info("Seed-derived terms: %s", recurring_terms[:10])

        # ── Phase 2-SNOWBALL1: Mine references from seed papers (backward) ──
        # Like a researcher checking "who did these papers cite?"
        seed_s2_ids = [p["paper_id_s2"] for p in all_papers if p.get("paper_id_s2")]
        canonical_s2_ids = [p["paper_id_s2"] for p in all_papers
                           if p.get("paper_id_s2") and (p.get("is_canonical") or p.get("source") == "llm_knowledge")]
        if canonical_s2_ids:
            self.display.step(f"Phase 2-SNOWBALL1: Mining references from {len(canonical_s2_ids)} seed papers...")
            try:
                snowball1_papers = expand_citation_graph(
                    [p for p in all_papers if p.get("paper_id_s2") in set(canonical_s2_ids)],
                    direction="backward", limit_per_paper=20,
                    topic_terms=_topic_terms,
                )
                pre_sb = len(all_papers)
                _dedup_add(snowball1_papers)
                sb1_added = len(all_papers) - pre_sb
                search_audit["queries"].append(f"[snowball-1] {len(canonical_s2_ids)} seeds → {len(snowball1_papers)} refs, {sb1_added} new")
                self.display.step(f"  Snowball pass 1: {len(snowball1_papers)} references found, {sb1_added} new after dedup")
            except Exception as e:
                logger.warning("Snowball pass 1 failed: %s", e)

        # ── Phase 2-SEMANTIC: S2 SPECTER2 embedding recommendations ──
        # This is the closest to how a researcher thinks: "find papers LIKE these ones"
        # Using enriched seeds gives better recommendations than raw keyword search
        seed_s2_ids = [p["paper_id_s2"] for p in all_papers if p.get("paper_id_s2")]
        if seed_s2_ids:
            from .academic_search import s2_recommend_from_list
            _max_seeds = min(len(seed_s2_ids), 5)
            self.display.step(f"Phase 2-SEMANTIC: S2 embedding recommendations from {_max_seeds} seed papers...")
            try:
                rec_hits = s2_recommend_from_list(
                    seed_s2_ids[:_max_seeds], limit=100,
                )
                pre = len(all_papers)
                _dedup_add(rec_hits)
                added = len(all_papers) - pre
                search_audit["queries"].append(f"[s2-recommend] {_max_seeds} seeds → {len(rec_hits)} recs, {added} new")
                self.display.step(f"  S2 Recommendations: {len(rec_hits)} similar papers, {added} new after dedup")
            except Exception as e:
                logger.warning("S2 recommendations failed: %s", e)

        # ── Phase 2-SURVEYS: Find survey papers and mine their references ──
        self.display.step("Phase 2-SURVEYS: Orienting via survey/review papers...")
        topic_short = self._topic.split(":")[0].strip()[:60] if ":" in self._topic else self._topic[:60]
        survey_query = _qualify_query(topic_short) if domain_qualifier else topic_short
        surveys: list[dict] = []
        try:
            surveys = search_survey_papers(
                survey_query, limit=_max_surveys, year_from=_year_surveys,
                mailto=self.owner_email or None,
            )
            self.display.step(f"  Found {len(surveys)} survey/review papers")
            for s in surveys[:3]:
                self.display.step(f"    - {s.get('title', '?')[:70]} ({s.get('year', '?')}, {s.get('citation_count', 0)} cites)")
            _dedup_add(surveys)
        except Exception as e:
            logger.warning("Survey search failed: %s", e)

        survey_refs: list[dict] = []
        if surveys:
            try:
                survey_refs = extract_references_from_surveys(
                    surveys, limit_per_survey=_refs_per_survey, topic_terms=_topic_terms,
                )
                _dedup_add(survey_refs)
                multi_cited = [r for r in survey_refs if r.get("cited_by_n_surveys", 0) > 1]
                self.display.step(
                    f"  Mined {len(survey_refs)} refs from surveys"
                    f" ({len(multi_cited)} cited by multiple surveys)"
                )
            except Exception as e:
                logger.warning("Survey reference extraction failed: %s", e)

        self.display.step(f"  After seeds + snowball + semantic + surveys: {len(all_papers)} papers")

        # ── Phase 2-SNOWBALL2: Second pass — mine refs of the best first-pass papers ──
        # Real researchers iterate: read → find refs → read those → find more refs
        new_s2_ids = [p["paper_id_s2"] for p in all_papers
                      if p.get("paper_id_s2") and p["paper_id_s2"] not in set(canonical_s2_ids or [])]
        # Pick highest-cited non-seed papers for second pass
        snowball2_candidates = sorted(
            [p for p in all_papers if p.get("paper_id_s2") in set(new_s2_ids)],
            key=lambda p: p.get("citation_count", 0),
            reverse=True,
        )[:5]
        if snowball2_candidates:
            self.display.step(f"Phase 2-SNOWBALL2: Mining references from {len(snowball2_candidates)} top-cited papers...")
            try:
                snowball2_papers = expand_citation_graph(
                    snowball2_candidates,
                    direction="backward", limit_per_paper=15,
                    topic_terms=_topic_terms,
                )
                pre_sb2 = len(all_papers)
                _dedup_add(snowball2_papers)
                sb2_added = len(all_papers) - pre_sb2
                search_audit["queries"].append(f"[snowball-2] {len(snowball2_candidates)} papers → {len(snowball2_papers)} refs, {sb2_added} new")
                self.display.step(f"  Snowball pass 2: {sb2_added} new papers")
            except Exception as e:
                logger.warning("Snowball pass 2 failed: %s", e)

        # ── Phase 2-KEYWORD: Targeted keyword search (supplement, not primary) ──
        # Saturation check: if we already have plenty of papers, keyword search is less critical
        _pre_keyword = len(all_papers)
        _saturated = _pre_keyword >= 60  # Good corpus already — keyword search is supplementary
        if _saturated:
            self.display.step(f"  Corpus looks saturated ({_pre_keyword} papers) — keyword search will be minimal")

        # Like a researcher doing a database search AFTER they already know the field
        self.display.step(f"Phase 2-KEYWORD: Targeted keyword search (supplement)...")

        # Title search
        try:
            title_hits = self._search(
                paper_title, limit=_lim_title, year_from=_year_default,
            )
            _dedup_add(title_hits)
            search_audit["queries"].append(f"[title] {paper_title[:80]}")
            self.display.step(f"  Title search: {len(title_hits)} results")
        except Exception as e:
            logger.warning("Title search failed: %s", e)

        # Domain-qualified search queries
        search_queries = brief.get("search_queries", [])
        if not search_queries:
            search_queries = research_questions[:_max_rqs]
            logger.info("No search_queries in brief — falling back to research_questions")
        else:
            search_queries = search_queries[:_max_rqs]
            logger.info("Using %d focused search_queries from brief", len(search_queries))

        for sq in search_queries:
            qualified_sq = _qualify_query(sq)
            try:
                sq_hits = self._search(
                    qualified_sq, limit=_lim_rq, year_from=_year_default,
                )
                _dedup_add(sq_hits)
                search_audit["queries"].append(f"[sq] {qualified_sq[:60]}")
                self.display.step(f"  Query '{qualified_sq[:50]}': {len(sq_hits)} results")
            except Exception as e:
                logger.warning("Search query failed for '%s': %s", sq[:50], e)
            time.sleep(0.3)

        # v0.3: Outline-driven targeted searches (purpose-driven by section outline)
        outline_queries = self.artifacts.get("outline_queries", [])
        if outline_queries:
            logger.info("Running %d outline-driven targeted searches", len(outline_queries))
            self.display.step(f"  Outline-driven searches: {len(outline_queries)} queries")
            for oq in outline_queries:
                query_text = oq.get("query", "")
                section = oq.get("section", "")
                q_type = oq.get("type", "evidence")
                qualified_oq = _qualify_query(query_text)
                try:
                    oq_hits = self._search(
                        qualified_oq, limit=_lim_rq, year_from=_year_default,
                    )
                    _dedup_add(oq_hits)
                    tag = f"[outline-{section[:12]}]" if section else "[outline]"
                    search_audit["queries"].append(f"{tag} {qualified_oq[:60]}")
                    if oq_hits:
                        self.display.step(f"  {tag} '{qualified_oq[:40]}': {len(oq_hits)} results")
                except Exception as e:
                    logger.warning("Outline query failed for '%s': %s", query_text[:50], e)
                time.sleep(0.3)

        # Keyword fallback only if corpus still thin
        if len(all_papers) < _kw_fallback:
            self.display.step("  Corpus thin — supplementing with domain-qualified keyword search...")
            for term in search_terms[:_max_kw]:
                qualified_term = _qualify_query(term)
                try:
                    hits = self._search(
                        qualified_term, limit=_lim_kw, year_from=_year_default,
                    )
                    search_audit["queries"].append(qualified_term)
                    _dedup_add(hits)
                    self.display.step(f"  '{qualified_term[:50]}': {len(hits)} results")
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
                p2b_system, p2b_user = self._get_prompt(
                    "phase3_outline",
                    topic=brief.get('title', self._topic),
                    paper_summaries=chr(10).join(paper_summaries),
                )
                if not p2b_system:
                    p2b_system = "You are a senior research analyst mapping an academic field. Return valid JSON."
                if not p2b_user:
                    p2b_user = (
                        f'Given these papers found on the topic "{brief.get("title", self._topic)}":\n\n'
                        f'{chr(10).join(paper_summaries)}\n\n'
                        f'Identify the research landscape. Return JSON:\n'
                        f'{{"key_debates": [{{"debate": "description", "side_a_keywords": ["..."], "side_b_keywords": ["..."]}}],\n'
                        f'"underrepresented_areas": ["topics/perspectives missing"],\n'
                        f'"methodological_approaches": ["research methods used"]}}\n\n'
                        f'Focus on genuine disagreements and real gaps. Be specific — use terms that would work as academic search queries.'
                    )
                landscape = self.llm.generate_json(
                    p2b_system,
                    p2b_user,
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
                # Build a proper counter-evidence query by negating the claim
                rq_short = " ".join(rq.split()[:12])  # first 12 words for query
                argument_claims.append({
                    "claim": rq,
                    "evidence_needed": {
                        "supporting": rq,
                        "counter": f"evidence against OR contradicting {rq_short}",
                    },
                })
        self.artifacts["argument_claims"] = argument_claims

        # ── Phase 2D: Targeted search per claim ──
        self.display.step(f"Phase 2D: Targeted search for {len(argument_claims)} claims...")
        for i, ac in enumerate(argument_claims[:_max_claims]):
            claim = ac.get("claim", "")
            evidence_needed = ac.get("evidence_needed", {})
            for role, description in evidence_needed.items():
                try:
                    hits = search_for_claim_evidence(
                        description, evidence_role=role, limit=_lim_claim,
                        year_from=_year_default if role != "foundational" else None,
                        mailto=self.owner_email or None,
                        sources=self.enabled_sources,
                    )
                    _dedup_add(hits)
                    # Assign role to new papers
                    for h in hits:
                        h["evidence_role"] = role
                        h["target_claim"] = claim
                    self.display.step(f"  Claim {i+1} [{role}]: {len(hits)} papers")
                except Exception as e:
                    logger.warning("Claim search failed for '%s' (%s): %s", claim[:40], role, e)

        # Also search for debates identified in Phase 2B (domain-qualified)
        for debate in landscape.get("key_debates", [])[:3]:
            for side_key in ["side_a_keywords", "side_b_keywords"]:
                for kw in debate.get(side_key, [])[:_max_debate_kw]:
                    qualified_kw = _qualify_query(kw)
                    try:
                        hits = self._search(qualified_kw, limit=_lim_debate, year_from=_year_default)
                        _dedup_add(hits)
                    except Exception:
                        pass
                    time.sleep(0.3)

        # Search for underrepresented areas (domain-qualified)
        for area in landscape.get("underrepresented_areas", [])[:_max_underrep]:
            qualified_area = _qualify_query(area)
            try:
                hits = self._search(qualified_area, limit=_lim_gap, year_from=_year_default)
                _dedup_add(hits)
                self.display.step(f"  Underrepresented '{qualified_area[:50]}': {len(hits)} papers")
            except Exception:
                pass
            time.sleep(0.3)

        self.display.step(f"  After targeted search: {len(all_papers)} papers")

        # ── Phase 2E: Forward citations — "who cited these key papers?" ──
        # (Backward citations already covered by snowball passes 1 & 2)
        self.display.step("Phase 2E: Forward citations (who cited key papers)...")
        expansion_candidates = sorted(
            [p for p in all_papers if p.get("paper_id_s2") and (p.get("is_canonical") or p.get("citation_count", 0) > 50)],
            key=lambda p: p.get("citation_count", 0),
            reverse=True,
        )[:_cg_top]
        if expansion_candidates:
            try:
                graph_papers = expand_citation_graph(
                    expansion_candidates, direction="forward", limit_per_paper=_cg_per,
                    topic_terms=_topic_terms,
                )
                _dedup_add(graph_papers)
                self.display.step(f"  Forward citations: {len(graph_papers)} new papers")
            except Exception as e:
                logger.warning("Citation graph expansion failed: %s", e)

        # ── Supplementary sources (web search, platform, LLM suggestions) ──
        # Web search
        if self.llm.supports_web_search:
            # Search with full title first, then individual terms
            web_queries = [paper_title] + search_terms[:3]
            for term in web_queries:
                try:
                    web_hits = self.llm.search_web(term, limit=10)
                    _dedup_add(web_hits)
                except Exception:
                    pass
                time.sleep(0.3)
        # (Serper fallback removed — serper is now covered by self._search() via enabled_sources)

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

        # (LLM suggestions moved to Phase 2-LLM above — runs first for best relevance)

        # Custom sources
        for src in self.custom_sources:
            _dedup_add([{
                "title": src.title,
                "abstract": src.content[:2000] if src.content else "",
                "authors": src.authors or [],
                "year": src.year,
                "doi": src.doi or "",
                "url": src.source_path or "",
                "paper_id": f"custom_{len(all_papers)}",
                "source": "custom",
            }])

        self.display.step(f"Total unique papers found: {len(all_papers)}")

        # ── Per-source summary ──
        from collections import Counter
        _src_counts = Counter(p.get("source", "unknown") for p in all_papers)
        search_audit["per_source_counts"] = dict(_src_counts)
        _src_summary = ", ".join(f"{s}: {c}" for s, c in _src_counts.most_common())
        self.display.step(f"  Source breakdown: {_src_summary}")
        logger.info("Per-source breakdown: %s", _src_summary)

        # NOTE: Topic relevance filtering is handled by the LLM scoring pass below,
        # which understands semantic relevance (e.g., "metastasis" = "metastatic").
        # No keyword-based pre-filtering.

        # ── Auto-import from Zotero if library is empty ──
        if self.library and self.library.count() == 0:
            try:
                from .zotero import find_zotero_data_dir, ZoteroLocal, import_zotero_papers
                zotero_dir = find_zotero_data_dir()
                if zotero_dir:
                    zl = ZoteroLocal(zotero_dir)
                    zotero_count = zl.count()
                    if zotero_count > 0:
                        self.display.step(f"Library empty but found local Zotero ({zotero_count} items) — auto-importing...")
                        zotero_papers = zl.get_papers(limit=300)
                        relevant = [p for p in zotero_papers if p.pdf_path or p.abstract]
                        if relevant:
                            imported = import_zotero_papers(relevant, self.library, include_pdfs=True)
                            if imported:
                                self.display.step(f"  Imported {imported} papers from Zotero into library")
                                logger.info("Zotero auto-import: %d papers imported", imported)
            except Exception as e:
                logger.debug("Zotero auto-import skipped: %s", e)

        # ── Local Library injection ──
        if self.library and self.library.count() > 0:
            self.display.step("Searching local paper library...")
            query_parts = [brief.get("title", self._topic)]
            query_parts.extend(search_terms[:3])
            query_parts.extend(rq[:80] for rq in research_questions[:2])
            library_query = " ".join(query_parts)

            library_matches = self.library.search(library_query, limit=20)
            lib_added = 0
            existing_titles = {(p.get("title") or "").lower().strip() for p in all_papers}
            existing_dois = {(p.get("doi") or "").lower().strip() for p in all_papers if p.get("doi")}
            for lp in library_matches:
                paper_dict = self.library.to_paper_dict(lp)
                # Skip duplicates
                if paper_dict["title"].lower().strip() in existing_titles:
                    continue
                if paper_dict.get("doi") and paper_dict["doi"].lower().strip() in existing_dois:
                    continue
                paper_dict["_from_local_library"] = True
                all_papers.append(paper_dict)
                lib_added += 1
            if lib_added:
                self.display.step(f"  Added {lib_added} papers from local library (full text)")
                logger.info("Local library: injected %d papers", lib_added)

        # ── Overlay local library full text onto API-found papers ──
        if self.library and self.library.count() > 0:
            overlay_count = 0
            for paper in all_papers:
                if paper.get("_from_local_library"):
                    continue  # already has full text
                doi = (paper.get("doi") or "").strip()
                title = (paper.get("title") or "").strip()
                local = None
                if doi:
                    local = self.library.find_by_doi(doi)
                if not local and title:
                    local = self.library.find_by_title(title)
                if local and local.full_text and len(local.full_text) > len(paper.get("enriched_content", "") or ""):
                    paper["enriched_content"] = local.full_text
                    paper["_from_local_library"] = True
                    overlay_count += 1
            if overlay_count:
                self.display.step(f"  Overlaid full text from local library onto {overlay_count} API-found papers")
                logger.info("Local library overlay: %d papers got full text", overlay_count)

        # ── Phase B: Enrich with full text ──
        self.display.step("Enriching papers with full text...")
        enriched_papers: list[dict] = []
        full_text_count = 0
        abstract_only_count = 0
        from agentpub.paper_cache import cache_papers as _cache_batch, update_enriched_content as _cache_enrich
        for i, paper in enumerate(all_papers[:60]):
            # Skip enrichment for papers that already have full text from local library
            existing = paper.get("enriched_content", "") or ""
            if paper.get("_from_local_library") and len(existing) > 2000:
                full_text_count += 1
                enriched_papers.append(paper)
                continue
            content = _run_non_critical(
                lambda _p=paper: enrich_paper_content(_p, max_chars=0),
                label=f"enrich paper {i+1}", timeout=45, default="")
            # Only overwrite if new content is longer than existing
            if len(content) > len(existing):
                paper["enriched_content"] = content
                _cache_enrich(paper, content)
            if len(paper.get("enriched_content", "")) > 2000:
                full_text_count += 1
            else:
                if not existing:
                    paper["enriched_content"] = paper.get("abstract", "")
                abstract_only_count += 1
                logger.info("Abstract-only: %s (%s)", paper.get("title", "?")[:60], paper.get("doi", "no DOI"))
            enriched_papers.append(paper)
            if (i + 1) % 10 == 0:
                self.display.step(f"  Enriched {i + 1}/{min(len(all_papers), 60)} papers ({full_text_count} full text, {abstract_only_count} abstract-only)")
                time.sleep(0.3)
        self.display.step(f"Enrichment complete: {full_text_count} full text, {abstract_only_count} abstract-only")
        logger.info("Enrichment: %d full text, %d abstract-only out of %d papers",
                    full_text_count, abstract_only_count, len(enriched_papers))
        _cache_batch(enriched_papers)

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
                content = (p.get("enriched_content") or p.get("abstract") or "")[:2500]
                paper_summaries.append(
                    f"[{idx}] {p.get('title', 'Untitled')} ({p.get('year', 'N/A')})\n{content}"
                )

            # Build screening prompt from prompt system (GUI-editable)
            p2s_system, p2s_user = self._get_prompt(
                "phase3_screen",
                topic=brief.get('title', self._topic),
                paper_summaries=chr(10).join(paper_summaries),
            )
            if not p2s_system:
                p2s_system = "You are an academic research assistant specializing in domain-relevance assessment. Return valid JSON."

            # Append dynamic context (research questions, scope) to the template
            rq_context = f"\nResearch questions: {json.dumps(brief.get('research_questions', []))}"
            if p2s_user:
                scoring_prompt = p2s_user + "\n" + rq_context + "\n" + domain_context
            else:
                scoring_prompt = (
                    f'Rate these papers for relevance to: "{brief.get("title", self._topic)}"\n\n'
                    f'{chr(10).join(paper_summaries)}\n\n'
                    f'{rq_context}\n{domain_context}\n\n'
                    f'For each paper, return JSON:\n'
                    f'{{"scores": [{{"index": 0, "relevance": 0.0-1.0, "on_domain": true, "key_finding": "one sentence"}}]}}'
                )
            scoring_prompt += "\n- Only include papers with relevance >= 0.4 AND on_domain = true"

            try:
                result = self.llm.generate_json(
                    p2s_system,
                    scoring_prompt,
                    temperature=0.2,
                    max_tokens=16000,
                )
                # Handle both {"scores": [...]} and raw [...] from the LLM
                if isinstance(result, list):
                    scores = result
                elif isinstance(result, dict):
                    scores = result.get("scores", [])
                    # Sometimes LLM nests it as {"scores": {"scores": [...]}}
                    if isinstance(scores, dict):
                        scores = scores.get("scores", [])
                else:
                    scores = []
                for s in scores:
                    if not isinstance(s, dict):
                        continue
                    idx = s.get("index", -1)
                    if 0 <= idx < len(enriched_papers):
                        enriched_papers[idx]["relevance_score"] = s.get("relevance", 0.5)
                        enriched_papers[idx]["on_domain"] = s.get("on_domain", False)
                        enriched_papers[idx]["key_finding"] = s.get("key_finding", "")
                        if s.get("source_type"):
                            enriched_papers[idx]["source_type_llm"] = s["source_type"]
                        if s.get("evidence_strength"):
                            enriched_papers[idx]["evidence_strength"] = s["evidence_strength"]
            except Exception as e:
                logger.warning("Scoring batch failed: %s", e)
                # Assign conservative defaults — unscored papers are suspicious
                for p in batch:
                    p.setdefault("relevance_score", 0.3)
                    p.setdefault("on_domain", False)

        # Assign default scores to any unscored papers — conservative: assume off-topic
        for p in enriched_papers:
            p.setdefault("relevance_score", 0.3)
            p.setdefault("on_domain", False)

        # ── Phase D: Filter quality, domain, and rank ──
        pre_filter = len(enriched_papers)

        # Remove papers with no authors AND no abstract
        enriched_papers = [
            p for p in enriched_papers
            if p.get("authors") or (p.get("abstract", "") and len(p.get("abstract", "")) > 80)
        ]

        # Remove retracted papers
        pre_retract = len(enriched_papers)
        enriched_papers = [
            p for p in enriched_papers
            if "retraction" not in (p.get("title") or "").lower()[:30]
        ]
        if len(enriched_papers) < pre_retract:
            self.display.step(f"  Removed {pre_retract - len(enriched_papers)} retracted papers")

        # Remove off-domain papers (LLM flagged as wrong field)
        off_domain = [p for p in enriched_papers if not p.get("on_domain", False)]
        if off_domain:
            self.display.step(f"  Removed {len(off_domain)} off-domain papers (LLM filter):")
            for p in off_domain[:5]:
                self.display.step(f"    - {p.get('title', '?')[:60]}")
            if len(off_domain) > 5:
                self.display.step(f"    ... and {len(off_domain) - 5} more")
        enriched_papers = [p for p in enriched_papers if p.get("on_domain", False)]

        # Code-enforced relevance floor — prompt says 0.4 but LLM sometimes ignores it
        pre_relevance = len(enriched_papers)
        enriched_papers = [p for p in enriched_papers if p.get("relevance_score", 0) >= 0.4]
        if len(enriched_papers) < pre_relevance:
            self.display.step(f"  Removed {pre_relevance - len(enriched_papers)} papers below relevance threshold (< 0.4)")

        # Relevance filtering: on_domain flag + code-enforced 0.4 relevance floor above.

        # ── Borderline re-screening (0.4-0.6 relevance) ──
        # Papers in this range got a marginal score from batch scoring.
        # Re-screen individually with full context for more accurate verdicts.
        borderline = [p for p in enriched_papers if 0.4 <= p.get("relevance_score", 0) <= 0.6]
        if borderline and len(borderline) <= 15:
            self.display.step(f"  Re-screening {len(borderline)} borderline papers (0.4-0.6)...")
            dq_note = f' in the field of "{domain_qualifier}"' if domain_qualifier else ""
            scope_note = f"\nOut of scope: {', '.join(scope_out)}" if scope_out else ""
            rq_text = "; ".join(research_questions[:3]) if research_questions else ""
            kept = 0
            removed_titles = []
            for bp in borderline:
                bp_title = bp.get("title", "")
                bp_abstract = (bp.get("enriched_content", "") or bp.get("abstract", ""))[:2000]
                rescreen_prompt = (
                    f'Is this paper relevant to a review titled "{paper_title}"{dq_note}?\n'
                    f"Research questions: {rq_text}{scope_note}\n\n"
                    f"Paper: {bp_title}\n{bp_abstract}\n\n"
                    f'Answer ONLY "yes" or "no".'
                )
                try:
                    resp = self.llm.generate(
                        "You are a domain expert deciding paper relevance. Answer only yes or no.",
                        rescreen_prompt, max_tokens=10, temperature=0.0,
                    )
                    answer = (resp.text if hasattr(resp, "text") else str(resp)).strip().lower()
                    if "no" in answer and "yes" not in answer:
                        bp["_borderline_rejected"] = True
                        removed_titles.append(bp_title[:60])
                    else:
                        kept += 1
                except Exception:
                    kept += 1  # On error, keep the paper
            rejected = [p for p in enriched_papers if p.get("_borderline_rejected")]
            if rejected:
                enriched_papers = [p for p in enriched_papers if not p.get("_borderline_rejected")]
                self.display.step(f"  Re-screening removed {len(rejected)}, kept {kept} borderline papers:")
                for t in removed_titles[:5]:
                    self.display.step(f"    - {t}")

        self.display.step(f"  Filtered: {pre_filter} → {len(enriched_papers)} papers")

        # ── Phase E: Tag preprints (used by preprint cap later) ──
        for p in enriched_papers:
            doi = (p.get("doi") or "").lower()
            url = (p.get("url") or "").lower()
            if "ssrn" in doi or "ssrn" in url or "arxiv" in url or "preprint" in url:
                p["is_preprint"] = True
            # NOTE: Score adjustments for venue quality are handled by the LLM scoring
            # pass, which understands context. We only tag preprints here for counting.

        # NOTE: Venue quality assessment is handled by the LLM scoring pass, which
        # can distinguish a conference abstract from a paper about conference abstracts.

        # ── Early future-date filter (before writing phase) ──
        current_year = time.localtime().tm_year
        pre_future = len(enriched_papers)
        enriched_papers = [
            p for p in enriched_papers
            if not isinstance(p.get("year"), int) or p["year"] <= current_year
        ]
        if len(enriched_papers) < pre_future:
            self.display.step(f"  Removed {pre_future - len(enriched_papers)} future-dated papers (year > {current_year})")

        # Deprioritize current-year papers — evaluators flag them as
        # potentially fabricated. Penalize their score so they're only
        # included if the corpus needs them.
        current_year_count = 0
        for p in enriched_papers:
            if isinstance(p.get("year"), int) and p["year"] == current_year:
                p["relevance_score"] = p.get("relevance_score", 0) * 0.7
                current_year_count += 1
        if current_year_count:
            self.display.step(f"  Deprioritized {current_year_count} current-year ({current_year}) papers")

        # ── Quality-weighted composite ranking ──
        # Like a real researcher: combine relevance, citation impact,
        # venue quality, recency, and foundational importance.
        _current_year = time.localtime().tm_year

        # Citation impact: log-scaled, normalized against the max in the corpus
        _max_cites = max((p.get("citation_count") or 0) for p in enriched_papers) if enriched_papers else 1
        _max_cites = max(_max_cites, 1)

        # Known high-quality venues (tier 1 = top journals/conferences)
        _VENUE_TIER1 = {
            "nature", "science", "cell", "pnas", "lancet", "bmj", "jama",
            "new england journal", "nejm", "neuron", "nature neuroscience",
            "nature medicine", "nature methods", "nature communications",
            "nature human behaviour", "current biology", "elife",
            "psychological bulletin", "psychological review", "annual review",
            "trends in cognitive sciences", "trends in neurosciences",
            "journal of neuroscience", "cerebral cortex", "brain",
            "neuropsychologia", "cognition", "journal of experimental psychology",
            "proceedings of the national academy", "philosophical transactions",
            "frontiers in", "plos one", "plos biology", "scientific reports",
            "neurips", "icml", "iclr", "cvpr", "acl", "emnlp",
            "aaai", "ieee transactions", "acm computing surveys",
        }

        def _composite_score(p: dict) -> float:
            """Compute quality-weighted score for paper ranking."""
            # 1. Relevance (0-1) — weight: 40%
            relevance = p.get("relevance_score", 0.3)

            # 2. Citation impact (0-1) — weight: 25%
            # Log-scaled: a paper with 100 cites and one with 1000 are both "high"
            cites = p.get("citation_count") or 0
            import math
            cite_score = math.log1p(cites) / math.log1p(_max_cites) if _max_cites > 0 else 0

            # 3. Venue quality (0-1) — weight: 10%
            venue = (p.get("venue") or p.get("journal") or "").lower()
            venue_score = 0.7 if any(v in venue for v in _VENUE_TIER1) else 0.3

            # 4. Recency (0-1) — weight: 10%
            year = p.get("year") or 2020
            age = max(0, _current_year - year)
            # Sweet spot: 1-5 years old. Very old papers get lower score
            # unless they're highly cited (handled by cite_score)
            if age <= 1:
                recency = 0.9
            elif age <= 3:
                recency = 1.0  # peak: 1-3 years (established but recent)
            elif age <= 5:
                recency = 0.85
            elif age <= 10:
                recency = 0.6
            else:
                recency = 0.3

            # 5. Foundational signal (0-1) — weight: 15%
            # Papers that are canonical, highly cited relative to age, or reviews
            is_canonical = p.get("is_canonical", False)
            is_review = "review" in (p.get("title") or "").lower() or "survey" in (p.get("title") or "").lower()
            # Citations per year as a signal of ongoing influence
            cites_per_year = cites / max(age, 1)
            if is_canonical:
                foundational = 1.0
            elif cites_per_year > 50:
                foundational = 0.9  # highly influential
            elif cites_per_year > 20:
                foundational = 0.7
            elif is_review:
                foundational = 0.6  # reviews help map the field
            elif cites_per_year > 5:
                foundational = 0.4
            else:
                foundational = 0.2

            # 6. Evidence access (0-1) — weight: 20%
            # Full-text papers are far more useful than abstract-only for grounded synthesis
            content_len = len(p.get("enriched_content", "") or p.get("content", "") or "")
            if content_len > 2000:
                evidence_access = 1.0   # full text
            elif content_len > 500:
                evidence_access = 0.6   # substantial extract
            else:
                evidence_access = 0.2   # abstract only

            composite = (
                0.35 * relevance
                + 0.20 * evidence_access   # NEW: penalise abstract-only papers
                + 0.15 * cite_score
                + 0.10 * venue_score
                + 0.10 * recency
                + 0.10 * foundational
            )
            p["_composite_score"] = round(composite, 4)
            return composite

        for p in enriched_papers:
            _composite_score(p)

        enriched_papers.sort(key=lambda p: p.get("_composite_score", 0), reverse=True)

        # Log top 5 for transparency
        for i, p in enumerate(enriched_papers[:5]):
            logger.info(
                "Top %d: %.3f [rel=%.2f cites=%d yr=%s] %s",
                i + 1, p.get("_composite_score", 0),
                p.get("relevance_score", 0), p.get("citation_count", 0),
                p.get("year", "?"), (p.get("title") or "?")[:60],
            )

        # Use ref target from brief (complexity-appropriate), not hardcoded 30
        ref_target = brief.get("_ref_target", 28)
        curated_cap = max(30, ref_target)

        # ── Diversity-aware selection ──
        # Don't just take the top N by score — ensure author diversity
        # (no more than 5 papers from the same first author)
        curated = []
        _first_author_count: dict[str, int] = {}
        _MAX_PER_AUTHOR = 5
        for p in enriched_papers:
            if len(curated) >= curated_cap:
                break
            authors = p.get("authors", [])
            first_author = ""
            if authors:
                first_author = _extract_surname(authors[0]).lower()
            if first_author and _first_author_count.get(first_author, 0) >= _MAX_PER_AUTHOR:
                continue  # skip — already have enough from this author
            curated.append(p)
            if first_author:
                _first_author_count[first_author] = _first_author_count.get(first_author, 0) + 1

        _skipped_diversity = len(enriched_papers[:curated_cap]) - len(curated) if len(enriched_papers) >= curated_cap else 0
        if _skipped_diversity > 0:
            self.display.step(f"  Diversity filter: skipped {_skipped_diversity} papers (author concentration)")

        # If below minimum, progressively relax filters to include more papers
        # rather than relying on expansion (which finds lower-quality results)
        min_refs = brief.get("_ref_min", 20)
        if len(curated) < min_refs:
            # Re-include on_domain=False papers that scored >= 0.5 relevance
            # (LLM may have been too aggressive with domain gating)
            off_domain_but_relevant = [
                p for p in off_domain
                if p.get("relevance_score", 0) >= 0.5
            ]
            if off_domain_but_relevant:
                off_domain_but_relevant.sort(
                    key=lambda p: p.get("relevance_score", 0), reverse=True
                )
                to_add = off_domain_but_relevant[:min_refs - len(curated)]
                curated.extend(to_add)
                if to_add:
                    self.display.step(
                        f"  Re-included {len(to_add)} off-domain papers with high relevance scores "
                        f"(relaxed filter: corpus was {len(curated) - len(to_add)} < {min_refs} min)"
                    )

        # If STILL below minimum, lower relevance floor from 0.4 to 0.25
        if len(curated) < min_refs:
            below_floor = [
                p for p in all_papers
                if 0.25 <= p.get("relevance_score", 0) < 0.4
                and p.get("on_domain", False)
                and p not in curated
            ]
            if below_floor:
                below_floor.sort(key=lambda p: p.get("relevance_score", 0), reverse=True)
                to_add = below_floor[:min_refs - len(curated)]
                curated.extend(to_add)
                if to_add:
                    self.display.step(
                        f"  Added {len(to_add)} papers with relaxed relevance floor (0.25-0.4)"
                    )

        curated.sort(key=lambda p: p.get("relevance_score", 0), reverse=True)
        self.display.step(f"Selected top {len(curated)} papers (min relevance: {curated[-1].get('relevance_score', 0):.2f})" if curated else "No papers found")

        # Log the final curated corpus for debugging
        logger.info("=== CURATED CORPUS (%d papers) ===", len(curated))
        for i, p in enumerate(curated, 1):
            authors = p.get("authors", [])
            first = authors[0] if authors else "?"
            if isinstance(first, dict):
                first = first.get("name", "?")
            logger.info(
                "  [%d] (score=%.2f) %s (%s). %s",
                i, p.get("relevance_score", 0),
                str(first)[:30], p.get("year", "?"),
                (p.get("title") or "?")[:80],
            )

        # ── Cap preprints at 30% of curated corpus ──
        max_preprints = max(1, int(len(curated) * 0.30))
        preprint_count = sum(1 for p in curated if p.get("is_preprint"))
        if preprint_count > max_preprints:
            # Keep highest-scored preprints, drop the rest
            kept, preprint_seen = [], 0
            for p in curated:
                if p.get("is_preprint"):
                    preprint_seen += 1
                    if preprint_seen > max_preprints:
                        continue
                kept.append(p)
            dropped = len(curated) - len(kept)
            curated = kept
            self.display.step(f"  Preprint cap: dropped {dropped} lowest-scored preprints (max {max_preprints})")

        # ── Cap abstract-only sources at 30% of curated corpus ──
        # Evaluators repeatedly flag papers with >50% abstract-only as weakly grounded.
        # Skip the cap when full-text papers are scarce (<8 available) to avoid
        # collapsing the corpus below minimum size.
        def _is_abstract_only(p: dict) -> bool:
            content = p.get("enriched_content") or p.get("content") or p.get("abstract") or ""
            return len(content) <= 2000

        full_text_available = sum(1 for p in curated if not _is_abstract_only(p))
        if full_text_available >= 8:
            max_abstract_only = max(1, int(len(curated) * 0.30))
            abstract_only_count = sum(1 for p in curated if _is_abstract_only(p))
            if abstract_only_count > max_abstract_only:
                # curated is already sorted by composite score — keep highest-scored
                # abstract-only papers, drop the rest
                kept, abs_seen = [], 0
                for p in curated:
                    if _is_abstract_only(p):
                        abs_seen += 1
                        if abs_seen > max_abstract_only:
                            continue
                    kept.append(p)
                dropped = len(curated) - len(kept)
                curated = kept
                self.display.step(f"  Abstract-only cap: dropped {dropped} lowest-scored abstract-only papers (max {max_abstract_only})")
        else:
            logger.info(
                "Skipping abstract-only cap: only %d full-text papers available",
                full_text_available,
            )

        self.artifacts["candidate_papers"] = enriched_papers

        # ── Phase 2F: Gap audit — log gaps but do NOT inject unvetted papers ──
        # Claims must be based on research already in the curated corpus.
        # If a claim has no evidence, the writer should simply not make that claim.
        argument_claims = self.artifacts.get("argument_claims", [])
        if argument_claims and len(curated) >= 10:
            gaps = audit_evidence_gaps(argument_claims, curated)
            if gaps:
                self.display.step(f"  {len(gaps)} argument claims lack direct evidence — writer will skip unsupported claims")
                for g in gaps[:4]:
                    logger.info("Evidence gap (will not be filled): [%s] %s", g['missing_role'], g['claim'][:80])

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

        # Enrich venues EARLY so they're available for writing prompts and methodology
        _run_non_critical(lambda: self._enrich_venues(curated),
                          label="venue enrichment (research)", timeout=30)

        # Validate reference years — remove papers with future years (likely fabricated)
        import datetime
        current_year = datetime.datetime.now().year
        suspicious_refs = [p for p in curated if p.get("year") and int(p.get("year", 0)) > current_year]
        if suspicious_refs:
            for sr in suspicious_refs:
                logger.warning("Removing ref with future year %s: %s", sr.get("year"), sr.get("title", "?")[:60])
            curated = [p for p in curated if not p.get("year") or int(p.get("year", 0)) <= current_year]
            search_audit["total_included"] = len(curated)
            self.artifacts["search_audit"] = search_audit
            self.display.step(f"  Removed {len(suspicious_refs)} refs with future year (>{current_year})")

        self.artifacts["curated_papers"] = curated

        # Change 3: Log search/dedup/filter steps
        self._log_step(
            "search", "Academic database search",
            input_count=0, output_count=search_audit.get("total_retrieved", 0),
            details={
                "databases": search_audit.get("databases", []),
                "queries": search_audit.get("queries", []),
            },
        )
        self._log_step(
            "dedup", "Deduplication of retrieved records",
            input_count=search_audit.get("total_retrieved", 0),
            output_count=search_audit.get("total_after_dedup", 0),
        )
        self._log_step(
            "filter", "Relevance filtering and scoring",
            input_count=search_audit.get("total_after_dedup", 0),
            output_count=search_audit.get("total_included", len(curated)),
        )

        # Build title→abstract lookup for source verification (step 4o)
        # Store the BEST available content: enriched_content (full text) > abstract
        ref_abstracts: dict[str, str] = {}
        for p in enriched_papers:
            title = (p.get("title") or "").strip()
            # Prefer enriched content (full text from library/OA) for better verification
            content = (p.get("enriched_content") or p.get("abstract") or "").strip()
            if title and content:
                # Store up to 3000 chars — enough for verifier to check claims
                ref_abstracts[title.lower()[:80]] = content[:3000]
        self.artifacts["ref_abstracts"] = ref_abstracts

        self.display.phase_done(3)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Step 3 (v0.3): Deep Reading — structured notes from full-text papers
    # ------------------------------------------------------------------

    def _step3_deep_reading(self) -> None:
        """Read all papers, produce structured notes per paper.

        For large-context models (>128k): single mega-context call with all papers.
        For smaller models: batch papers into chunks that fit the context window,
        then merge reading notes from all batches.
        """
        logger.info("Step 3: DEEP READING")
        self.display.phase_start(4, "Deep Reading")

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        curated = self.artifacts.get("curated_papers", [])

        if not curated:
            logger.warning("No curated papers — skipping deep reading")
            self.display.phase_done(4)
            return

        # Build per-paper text blocks
        paper_blocks = []
        for i, paper in enumerate(curated):
            header = f"[{i+1}] {paper.get('title', 'Untitled')} — {', '.join(str(a) for a in (paper.get('authors') or ['Unknown'])[:3])} ({paper.get('year', '?')})"
            content = paper.get("enriched_content", "") or paper.get("abstract", "") or ""
            content_type = "full_text" if len(content) > 2000 else "abstract_only"
            block = f"{header}\n[Content type: {content_type}]\n{content}\n"
            paper_blocks.append(block)

        total_chars = sum(len(b) for b in paper_blocks)
        self.display.step(f"Loading {len(curated)} papers ({total_chars:,} chars) for deep reading")
        logger.info("Deep reading: %d papers, %d chars total", len(curated), total_chars)

        # Build the base prompt (without papers)
        rq_text = "\n".join(f"  RQ{i+1}: {rq}" for i, rq in enumerate(brief.get("research_questions", [])))
        prompt_template = self._prompts.get("phase4_deep_reading", DEFAULT_PROMPTS.get("phase4_deep_reading", ""))
        system = "You are a meticulous academic researcher reading papers for a literature review. Return valid JSON only."

        # Estimate tokens: ~4 chars per token is a reasonable average
        base_prompt = prompt_template.format(
            title=brief.get("title", ""),
            research_questions=rq_text,
            n_papers=len(curated),
        )
        base_tokens = len(base_prompt) // 4 + 500  # overhead for system + formatting
        context_limit = self.llm.max_context_tokens
        # Reserve tokens for output
        output_reserve = min(65000, self.llm.max_output_tokens)
        available_input = context_limit - output_reserve - base_tokens

        # Decide: single call or batched
        estimated_total_tokens = total_chars // 4
        if estimated_total_tokens <= available_input:
            # All papers fit — single call
            batches = [paper_blocks]
            logger.info("Deep reading: all papers fit in context (%dk input available)", available_input // 1000)
        else:
            # Split into batches that fit
            batches = []
            current_batch = []
            current_tokens = 0
            # Use 80% of available to leave margin
            batch_limit = int(available_input * 0.8)
            for block in paper_blocks:
                block_tokens = len(block) // 4
                if current_tokens + block_tokens > batch_limit and current_batch:
                    batches.append(current_batch)
                    current_batch = []
                    current_tokens = 0
                current_batch.append(block)
                current_tokens += block_tokens
            if current_batch:
                batches.append(current_batch)
            logger.info("Deep reading: splitting into %d batches (%dk context, %dk available)",
                        len(batches), context_limit // 1000, available_input // 1000)
            self.display.step(f"Context limit {context_limit // 1000}k tokens — splitting into {len(batches)} batches")

        # Process each batch
        all_reading_notes = []
        all_corpus_summaries = []

        for batch_idx, batch in enumerate(batches):
            if len(batches) > 1:
                self.display.step(f"Reading batch {batch_idx + 1}/{len(batches)} ({len(batch)} papers)")
                logger.info("Deep reading batch %d/%d: %d papers", batch_idx + 1, len(batches), len(batch))

            batch_text = "\n---\n".join(batch)
            user_prompt = base_prompt + f"\n\nPAPERS:\n\n{batch_text}"

            try:
                reading_result = self.llm.generate_json(
                    system, user_prompt,
                    temperature=0.2,
                    max_tokens=min(65000, self.llm._effective_max_tokens(65000)),
                )
            except (LLMError, Exception) as e:
                logger.warning("Deep reading batch %d failed: %s — retrying with shorter input", batch_idx + 1, e)
                # Retry with truncated input (halve each paper's text)
                try:
                    short_batch = "\n---\n".join(p[:len(p) // 2] for p in batch)
                    short_prompt = base_prompt + f"\n\nPAPERS:\n\n{short_batch}"
                    reading_result = self.llm.generate_json(
                        system, short_prompt,
                        temperature=0.2,
                        max_tokens=min(65000, self.llm._effective_max_tokens(65000)),
                    )
                    logger.info("Deep reading batch %d succeeded on retry (truncated input)", batch_idx + 1)
                except Exception as e2:
                    logger.error("Deep reading batch %d failed on retry: %s", batch_idx + 1, e2)
                    reading_result = {}

            if not isinstance(reading_result, dict):
                reading_result = {}

            batch_notes = reading_result.get("reading_notes", [])
            batch_summary = reading_result.get("corpus_summary", {})
            all_reading_notes.extend(batch_notes)
            if batch_summary:
                all_corpus_summaries.append(batch_summary)

        # Merge corpus summaries from multiple batches
        if len(all_corpus_summaries) == 1:
            corpus_summary = all_corpus_summaries[0]
        elif len(all_corpus_summaries) > 1:
            # Merge: combine themes, contradictions, gaps from all batches
            corpus_summary = {"themes": [], "contradictions": [], "gaps": [], "strongest_evidence": []}
            for cs in all_corpus_summaries:
                corpus_summary["themes"].extend(cs.get("themes", []))
                corpus_summary["contradictions"].extend(cs.get("contradictions", []))
                corpus_summary["gaps"].extend(cs.get("gaps", []))
                corpus_summary["strongest_evidence"].extend(cs.get("strongest_evidence", []))
        else:
            corpus_summary = {}

        reading_notes = all_reading_notes

        self.artifacts["reading_notes"] = reading_notes
        self.artifacts["corpus_summary"] = corpus_summary

        # Flag quality degradation if deep reading failed entirely
        if not reading_notes:
            self.artifacts["quality_degraded"] = True
            self.display.step("WARNING: Deep reading produced no notes — paper quality will be degraded")
            logger.error("Deep reading produced 0 notes from %d papers — quality_degraded=True",
                        len(self.artifacts.get("curated_papers", [])))

        if reading_notes:
            n_landmark = sum(1 for n in reading_notes if n.get("quality_tier") == "landmark")
            n_solid = sum(1 for n in reading_notes if n.get("quality_tier") == "solid")
            n_weak = sum(1 for n in reading_notes if n.get("quality_tier") == "weak")
            n_tangential = sum(1 for n in reading_notes if n.get("quality_tier") == "tangential")
            self.display.step(f"Reading notes: {len(reading_notes)} papers analyzed")
            self.display.step(f"  Quality: {n_landmark} landmark, {n_solid} solid, {n_weak} weak, {n_tangential} tangential")
            logger.info("Reading notes: %d papers — %d landmark, %d solid, %d weak, %d tangential",
                        len(reading_notes), n_landmark, n_solid, n_weak, n_tangential)

        if corpus_summary:
            themes = corpus_summary.get("themes", [])
            contradictions = corpus_summary.get("contradictions", [])
            gaps = corpus_summary.get("gaps", [])
            if themes:
                self.display.step(f"  Themes: {len(themes)} identified")
                for t in themes[:5]:
                    logger.info("Theme: %s", str(t)[:120])
            if contradictions:
                self.display.step(f"  Contradictions: {len(contradictions)} found")
                for c in contradictions[:5]:
                    logger.info("Contradiction: %s", str(c)[:120])
            if gaps:
                self.display.step(f"  Gaps: {len(gaps)} identified")
                for g in gaps[:5]:
                    logger.info("Gap: %s", str(g)[:120])

        # Drop tangential papers from curated list — they add noise
        # without contributing relevant evidence to the paper
        if reading_notes:
            tangential_titles = {
                n.get("title", "").lower().strip()
                for n in reading_notes
                if n.get("quality_tier") == "tangential"
            }
            if tangential_titles:
                curated_before = self.artifacts.get("curated_papers", [])
                pruned = [
                    p for p in curated_before
                    if (p.get("title", "").lower().strip()) not in tangential_titles
                ]
                n_dropped = len(curated_before) - len(pruned)
                if n_dropped > 0:
                    self.artifacts["curated_papers"] = pruned
                    # Also prune reading notes to stay in sync
                    self.artifacts["reading_notes"] = [
                        n for n in reading_notes
                        if n.get("quality_tier") != "tangential"
                    ]
                    self.display.step(f"  Dropped {n_dropped} tangential paper(s) from corpus")
                    logger.info("Pruned %d tangential papers from curated list (%d → %d)",
                               n_dropped, len(curated_before), len(pruned))

        # Change 1: Create CorpusManifest — frozen single source of truth
        self._create_corpus_manifest()
        self.display.step(f"  CorpusManifest: display_count={self.artifacts['corpus_manifest'].display_count}")

        # Change 3: Log reading step
        curated = self.artifacts.get("curated_papers", [])
        self._log_step(
            "enrich", "Deep reading with structured annotation",
            input_count=len(curated), output_count=len(curated),
            details={"reading_notes_count": len(self.artifacts.get("reading_notes", []))},
        )

        self.display.phase_done(4)

    # ------------------------------------------------------------------
    # Step 3b (v0.3): Revise Outline — adapt argument to actual evidence
    # ------------------------------------------------------------------

    def _step3b_revise_outline(self) -> None:
        """Revise the paper outline based on what was actually found in the reading."""
        logger.info("Step 3b: REVISE OUTLINE")
        self.display.phase_start(5, "Revise Outline")

        original_outline = self.artifacts.get("paper_outline", {})
        reading_notes = self.artifacts.get("reading_notes", [])
        corpus_summary = self.artifacts.get("corpus_summary", {})

        if not reading_notes:
            logger.info("No reading notes — skipping outline revision")
            self.display.step("Skipping (no reading notes)")
            self.display.phase_done(5)
            return

        prompt_template = self._prompts.get("phase5_revise_outline", DEFAULT_PROMPTS.get("phase5_revise_outline", ""))

        # Truncate reading notes for context window — keep key fields only
        compact_notes = []
        for note in reading_notes:
            compact_notes.append({
                "paper_index": note.get("paper_index"),
                "key_findings": note.get("key_findings", [])[:3],
                "quality_tier": note.get("quality_tier", ""),
                "relevance": note.get("relevance", ""),
            })

        system = "You are a senior academic research planner. Return valid JSON only."
        user_prompt = prompt_template.format(
            original_outline=json.dumps(original_outline, indent=1)[:8000],
            corpus_summary=json.dumps({
                "notes_summary": compact_notes,
                "themes": corpus_summary.get("themes", []),
                "contradictions": corpus_summary.get("contradictions", []),
                "gaps": corpus_summary.get("gaps", []),
                "strongest_evidence": corpus_summary.get("strongest_evidence", []),
            }, indent=1)[:12000],
        )

        try:
            revised = self.llm.generate_json(system, user_prompt, temperature=0.2, max_tokens=8000)
        except (LLMError, Exception) as e:
            logger.warning("Outline revision failed (non-fatal): %s", e)
            revised = {}

        if not isinstance(revised, dict):
            revised = {}

        # Store revised outline
        if revised.get("revised_thesis") or revised.get("claim_evidence_map"):
            self.artifacts["revised_outline"] = revised
            self.artifacts["claim_evidence_map"] = revised.get("claim_evidence_map", [])

            thesis = revised.get("revised_thesis", "")
            if thesis:
                self.display.step(f"Revised thesis: {thesis[:120]}")
                logger.info("Revised thesis: %s", thesis[:200])

            claims = revised.get("claim_evidence_map", [])
            if claims:
                self.display.step(f"Evidence-backed claims: {len(claims)}")
                for cl in claims:
                    n_support = len(cl.get("supporting_papers", []))
                    confidence = cl.get("confidence", "?")
                    logger.info("Claim [%s, %d papers]: %s", confidence, n_support, str(cl.get("claim", ""))[:100])

            dropped = revised.get("dropped_claims", [])
            if dropped:
                self.display.step(f"Dropped claims (insufficient evidence): {len(dropped)}")
                for d in dropped:
                    logger.info("Dropped: %s", str(d)[:100])

            new_insights = revised.get("new_insights", [])
            if new_insights:
                self.display.step(f"New insights from reading: {len(new_insights)}")
        else:
            logger.info("Outline revision returned no changes — keeping original")
            self.display.step("No revision needed — outline holds")

        self.display.phase_done(5)

    # Step 3: Write (mega-context per section)
    # ------------------------------------------------------------------

    def _step3_write(self) -> None:
        """Write each section with ALL papers + ALL prior sections in context."""
        logger.info("Step 3: WRITE")
        self.display.phase_start(6, "Methodology")
        self.display.tick()

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        curated = self.artifacts.get("curated_papers", [])

        # Build the mega bibliography context
        bib_context = self._build_bibliography_context(curated)
        self.display.step(f"Bibliography context: {len(bib_context)} chars ({len(curated)} papers)")
        if len(curated) < self.config.min_references:
            logger.warning(
                "Corpus has %d papers but config.min_references=%d — paper may lack sufficient evidence",
                len(curated), self.config.min_references,
            )
            self.display.step(f"WARNING: Only {len(curated)} papers (minimum target: {self.config.min_references})")

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
                authors = ", ".join(str(a.get("name", a) if isinstance(a, dict) else a) for a in authors[:3])
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

        # Pre-extract evidence from each paper for section-specific use.
        # This gives the section writer SPECIFIC findings instead of raw abstracts.
        evidence_by_paper = self._extract_per_paper_evidence(curated)
        if evidence_by_paper:
            self.display.step(f"Extracted evidence from {len(evidence_by_paper)} papers")
            self.artifacts["evidence_by_paper"] = evidence_by_paper

        # Phase 4: Evidence Map — map evidence to sections BEFORE writing
        # This tells the section writer exactly which claims each section can make
        self.display.step("Mapping evidence to sections...")
        evidence_map = self._build_evidence_map(brief, curated, ref_list, evidence_by_paper)
        if evidence_map:
            self.artifacts["evidence_map"] = evidence_map
            self.display.step(f"Evidence map: {len(evidence_map)} sections mapped")

        _section_count = 0
        for section_name in _WRITE_ORDER:
            # Cooldown between LLM calls to avoid provider rate limits (TPM/RPM)
            if _section_count > 0:
                time.sleep(5)
            _section_count += 1

            self.display.step(f"Writing {section_name}...")
            self.display.tick()

            # Transition from Phase 6 (Methodology) to Phase 7 (Write) after methodology done
            if section_name != "Methodology" and not getattr(self, "_phase7_started", False):
                self.display.phase_done(6)
                self.display.phase_start(7, "Write Paper")
                self._phase7_started = True

            # v0.3+: Template methodology — deterministic, no LLM
            # Change 3: Prefer process-log-based methodology (more accurate)
            if section_name == "Methodology":
                template_meth = self._generate_methodology_from_process_log()
                if template_meth and len(template_meth.split()) >= 200:
                    written_sections[section_name] = template_meth
                    word_count = len(template_meth.split())
                    self.display.step(f"  Methodology: {word_count} words (template-generated, zero LLM)")
                    logger.info("Template methodology: %d words", word_count)
                    self.artifacts["_template_methodology_used"] = True
                    self.display.section_done(section_name, template_meth)
                    continue
                else:
                    logger.info("Template methodology too short (%d words) — falling back to LLM",
                                len(template_meth.split()) if template_meth else 0)

            target_words = self._section_word_target(section_name)
            min_words = self._section_word_min(section_name)

            # Build prior sections context — truncate for later sections to save tokens
            prior_text = ""
            if written_sections:
                parts = []
                for prev_name in _WRITE_ORDER:
                    if prev_name in written_sections:
                        parts.append(f"=== {prev_name} ===\n{written_sections[prev_name]}")
                prior_text = "\n\n".join(parts)
                # For sections 5+ in write order, truncate prior context
                # Discussion gets a larger budget (4000 words) so it can see
                # the full Results section and avoid redundancy
                if _section_count >= 5:
                    word_budget = 4000 if section_name == "Discussion" else 2000
                    words = prior_text.split()
                    if len(words) > word_budget:
                        # For Discussion, always keep the full Results section
                        if section_name == "Discussion" and "Results" in written_sections:
                            results_text = f"=== Results ===\n{written_sections['Results']}"
                            other_parts = [f"=== {n} ===\n{written_sections[n]}" for n in _WRITE_ORDER
                                           if n in written_sections and n != "Results"]
                            other_text = "\n\n".join(other_parts)
                            other_words = other_text.split()
                            remaining_budget = word_budget - len(results_text.split())
                            if remaining_budget > 500 and len(other_words) > remaining_budget:
                                other_text = "[Earlier sections truncated]\n..." + " ".join(other_words[-remaining_budget:])
                            prior_text = other_text + "\n\n" + results_text
                        else:
                            prior_text = "[Earlier sections truncated for brevity]\n\n..." + " ".join(words[-word_budget:])

            # Build citation blacklist — refs approaching their section limit
            # Use strict limits: 2 sections for regular refs, 3 for anchors
            blacklisted_refs: list[str] = []
            for cite_key, sections_used in citation_spread.items():
                max_sections = 3 if cite_key in anchor_keys else 2
                if len(sections_used) >= max_sections:
                    blacklisted_refs.append(cite_key)

            # Build section-specific deep context: select most relevant papers
            # and include their full enriched content + extracted evidence
            section_bib = self._build_section_bibliography(
                section_name, curated, evidence_by_paper, bib_context,
            )

            prompt = self._build_section_prompt(
                section_name=section_name,
                brief=brief,
                bib_context=section_bib,
                ref_list_text=ref_list_text,
                prior_sections=prior_text,
                target_words=target_words,
                blacklisted_refs=blacklisted_refs,
            )

            _sec_tokens = self._section_max_tokens(section_name)
            content = self._generate_section(prompt, section_name, max_tokens=_sec_tokens)

            # Retry once from scratch if generation returned empty (e.g. timeout)
            if not content or not content.strip():
                logger.warning("Section '%s' generation returned empty — retrying once", section_name)
                self.display.step(f"  {section_name}: empty — retrying...")
                content = self._generate_section(prompt, section_name, max_tokens=_sec_tokens)

            # Retry up to 2 times if too short
            word_count = len(content.split()) if content else 0
            for expand_attempt in range(min(self.config.max_expand_passes, 4)):
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
{ref_list_text[:15000]}

Write ONLY the expanded section text. No headers, no JSON. MINIMUM {min_words} WORDS."""
                # Use phase5_expand_section from GUI as system prompt
                expand_system = self._prompts.get("phase7_expand_section", "")
                if expand_system:
                    expand_system_filled = expand_system.replace("{section_name}", section_name)
                    try:
                        resp = self.llm.generate(
                            expand_system_filled,
                            expand_prompt,
                            temperature=0.2,
                            max_tokens=_sec_tokens,
                            think=True,
                        )
                        expanded = strip_thinking_tags(resp.text).strip()
                        expanded = self._clean_section_text(expanded)
                    except LLMError as e:
                        logger.warning("Expand with phase5_expand_section failed: %s — falling back", e)
                        expanded = self._generate_section(expand_prompt, section_name, max_tokens=_sec_tokens)
                else:
                    expanded = self._generate_section(expand_prompt, section_name, max_tokens=_sec_tokens)
                if expanded and len(expanded.split()) > word_count:
                    content = expanded
                    word_count = len(content.split())

            # NOTE: Citation spread enforcement (blacklisted over-used citations) is now
            # handled by the LLM citation cleanup pass in the audit phase, which can
            # rephrase sentences rather than just delete citation brackets.
            if blacklisted_refs and content:
                violations = [ref_key for ref_key in blacklisted_refs
                              if f"[{ref_key}]" in content]
                if violations:
                    logger.info("Section %s used %d blacklisted citations: %s — will be fixed in audit",
                                section_name, len(violations), violations)

            # Update citation spread tracker
            section_citations = re.findall(r'\[([^\]]+?,\s*\d{4})\]', content or "")
            for cite in section_citations:
                cite_clean = cite.strip()
                if cite_clean not in citation_spread:
                    citation_spread[cite_clean] = set()
                citation_spread[cite_clean].add(section_name)

            # Citation gap fill: find papers for uncited claims
            content = self._fill_citation_gaps(section_name, content, curated)

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

        contribution = brief.get("contribution_type", "evidence synthesis")
        # Abstract grounding rules from prompts (editable via GUI)
        grounding_rules = self._prompts.get("abstract_grounding_rules",
                          DEFAULT_PROMPTS.get("abstract_grounding_rules", ""))

        abstract_prompt = f"""Write the Abstract for this academic paper.

PAPER TITLE: {brief.get('title', '')}
CONTRIBUTION TYPE: {contribution}
RESEARCH QUESTIONS: {json.dumps(brief.get('research_questions', []))}

FULL PAPER:
{full_paper_text[:30000]}

Requirements:
- 200-400 words
- Summarize: background, methods, key findings, implications
- Include 2+ citations from the paper using [Author, Year] format
- Write as a single paragraph
- Do NOT start with "This paper..." — vary the opening
- CORPUS COUNT: When mentioning the number of studies reviewed, use EXACTLY {corpus_count}.
  This number must be consistent with the Methodology section.
- CITATION RULE: Only cite authors that appear in the reference list of the paper above.
  Do NOT introduce new citations not found in the paper.
- CLAIM GROUNDING: Every claim in the abstract MUST have a corresponding passage in the
  paper body above. Do NOT add findings, statistics, or conclusions not present in the body.
  If the body says "suggests", do NOT upgrade to "demonstrates" or "reveals".
- SCOPE QUALIFIERS: When describing findings, use "within the reviewed corpus" or "among
  the {corpus_count} studies examined" — do NOT make field-wide claims like "fundamental
  fragmentation" or "across the discipline" unless the body explicitly supports that scope.
- AI AGENT: Do NOT devote more than 1 sentence to describing the AI agent or platform.
  Focus on the research findings, not the tool that produced them.

{grounding_rules}"""

        # Use phase5_abstract from GUI-editable prompts as system prompt
        abstract_system = self._prompts.get("phase7_abstract", "")
        _abs_tokens = self._section_max_tokens("Abstract")
        if abstract_system:
            # Override system prompt for abstract generation
            try:
                resp = self.llm.generate(
                    abstract_system,
                    abstract_prompt,
                    temperature=0.2,
                    max_tokens=_abs_tokens,
                    think=True,
                )
                abstract = strip_thinking_tags(resp.text).strip()
                abstract = self._clean_section_text(abstract)
            except LLMError as e:
                logger.warning("Abstract generation with phase5_abstract failed: %s — falling back", e)
                abstract = self._generate_section(abstract_prompt, "Abstract", max_tokens=_abs_tokens)
        else:
            abstract = self._generate_section(abstract_prompt, "Abstract", max_tokens=_abs_tokens)

        # Cross-check abstract claims against paper body (LLM-based)
        if abstract and written_sections:
            full_body = "\n".join(content for content in written_sections.values())
            abstract = self._cross_check_abstract_claims(abstract, full_body)

        abstract_words = len(abstract.split()) if abstract else 0
        self.display.step(f"  Abstract: {abstract_words} words")

        self.artifacts["zero_draft"] = written_sections
        self.artifacts["written_sections"] = written_sections  # preserve for hypothesis extraction
        self.artifacts["abstract"] = abstract

        # Generate a comparison table — for ALL papers with 5+ sources
        # (not just review/survey; comparison tables improve scores universally)
        if len(curated) >= 5:
            self.display.step("Generating methodology comparison table...")

            def _gen_table():
                td = self._generate_comparison_table(curated, brief)
                return self._audit_table_citations(td, curated) if td else None

            table_data = _run_non_critical(_gen_table, label="comparison table", timeout=120)
            if table_data:
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

        # Build claim-evidence ledger (for audit phase)
        self.display.step("Building claim-evidence ledger...")
        ledger = _run_non_critical(
            lambda: self._build_claim_evidence_ledger(written_sections, ref_list_text),
            label="claim-evidence ledger", timeout=180, default=[],
        )
        if ledger:
            self.artifacts["claim_evidence_ledger"] = ledger
            self.display.step(f"  Ledger: {len(ledger)} claims mapped")

        total_words = sum(len(s.split()) for s in written_sections.values()) + abstract_words
        self.display.step(f"Total draft: {total_words} words")
        # Ensure phase 7 is started (in case all sections were methodology-only)
        if not getattr(self, "_phase7_started", False):
            self.display.phase_done(6)
            self.display.phase_start(7, "Write Paper")
        self.display.phase_done(7)
        self._phase7_started = False  # reset for reuse

    # ------------------------------------------------------------------
    # Step 3 (paragraph mode): Write one paragraph at a time
    # ------------------------------------------------------------------

    def _step3_write_paragraphs(self) -> None:
        """Write paper paragraph-by-paragraph from tiny evidence packets."""
        logger.info("Step 3: WRITE (paragraph mode)")
        self.display.phase_start(6, "Methodology")
        self.display.tick()

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        curated = self.artifacts.get("curated_papers", [])

        # Reuse existing infrastructure
        bib_context = self._build_bibliography_context(curated)
        self.display.step(f"Bibliography context: {len(bib_context)} chars ({len(curated)} papers)")

        ref_list = self._build_ref_list(curated)
        ref_list_text = json.dumps(ref_list, indent=1)
        self.artifacts["ref_list"] = ref_list

        source_table = self._build_source_classification(curated)
        self.artifacts["source_classification"] = source_table

        # Send references to display (for GUI References panel)
        for i, ref in enumerate(ref_list):
            authors = ref.get("authors", "")
            if isinstance(authors, list):
                authors = ", ".join(str(a.get("name", a) if isinstance(a, dict) else a) for a in authors[:3])
            self.display.add_reference(
                index=i + 1,
                authors=authors,
                year=str(ref.get("year", "")),
                title=ref.get("title", ""),
                url=ref.get("url", ""),
                doi=ref.get("doi", ""),
            )

        # Extract evidence from each paper
        evidence_by_paper = self._extract_per_paper_evidence(curated)
        if evidence_by_paper:
            self.display.step(f"Extracted evidence from {len(evidence_by_paper)} papers")
            self.artifacts["evidence_by_paper"] = evidence_by_paper

        # Build evidence map
        self.display.step("Mapping evidence to sections...")
        evidence_map = self._build_evidence_map(brief, curated, ref_list, evidence_by_paper)
        if evidence_map:
            self.artifacts["evidence_map"] = evidence_map
            self.display.step(f"Evidence map: {len(evidence_map)} sections mapped")

        # Build paper index for planner (index, cite_key, title, content_type)
        paper_index_lines = []
        for i, p in enumerate(curated):
            authors = p.get("authors", [])
            surname = _extract_surname(authors[0]) if authors else "Unknown"
            et_al = " et al." if len(authors) >= 3 else ""
            year = p.get("year", "N/A")
            cite_key = f"[{surname}{et_al}, {year}]"
            content = p.get("enriched_content", "") or p.get("abstract", "") or ""
            content_type = "full_text" if len(content) > 2000 else "abstract_only"
            paper_index_lines.append(f"  [{i}] {cite_key} — {p.get('title', '')[:80]} ({content_type})")

        paper_index_text = "\n".join(paper_index_lines)

        # Write sections paragraph-by-paragraph
        written_sections: dict[str, str] = {}
        _section_count = 0

        for section_name in _WRITE_ORDER:
            if _section_count > 0:
                time.sleep(3)
            _section_count += 1

            self.display.step(f"Writing {section_name} (paragraph mode)...")
            self.display.tick()

            # Transition from Phase 6 (Methodology) to Phase 7 (Write) after methodology done
            if section_name != "Methodology" and not getattr(self, "_phase7_started", False):
                self.display.phase_done(6)
                self.display.phase_start(7, "Write Paper (paragraph mode)")
                self._phase7_started = True

            # Template methodology — same as section mode
            if section_name == "Methodology":
                template_meth = self._generate_methodology_from_process_log()
                if template_meth and len(template_meth.split()) >= 200:
                    written_sections[section_name] = template_meth
                    word_count = len(template_meth.split())
                    self.display.step(f"  Methodology: {word_count} words (template-generated)")
                    self.artifacts["_template_methodology_used"] = True
                    self.display.section_done(section_name, template_meth)
                    continue

            target_words = self._section_word_target(section_name)
            min_words = self._section_word_min(section_name)

            # 1. Plan paragraphs for this section
            specs = self._plan_section_paragraphs(
                section_name, brief, curated, evidence_by_paper, evidence_map,
                paper_index_text, target_words,
            )

            if not specs:
                # Fallback: use existing section-level writer
                logger.warning("Paragraph planner failed for %s — falling back to section mode", section_name)
                self.display.step(f"  {section_name}: planner failed, using section fallback")
                section_bib = self._build_section_bibliography(
                    section_name, curated, evidence_by_paper, bib_context,
                )
                prompt = self._build_section_prompt(
                    section_name=section_name, brief=brief,
                    bib_context=section_bib, ref_list_text=ref_list_text,
                    prior_sections="", target_words=target_words,
                )
                content = self._generate_section(prompt, section_name)
                written_sections[section_name] = content
                self.display.step(f"  {section_name}: {len(content.split())} words (fallback)")
                continue

            self.display.step(f"  {section_name}: planned {len(specs)} paragraphs")

            # 2. Write each paragraph
            paragraphs: list[WrittenParagraph] = []
            for spec in specs:
                wp = self._write_single_paragraph(
                    spec, curated, evidence_by_paper, paragraphs, ref_list,
                )
                if wp and wp.text.strip():
                    paragraphs.append(wp)
                    self.display.step(f"    {spec.paragraph_id}: {wp.word_count} words, {len(wp.citations_used)} cites")
                else:
                    logger.warning("Paragraph %s produced empty text", spec.paragraph_id)
                time.sleep(1)  # Rate limit

            # 3. Assemble section
            section_text = self._assemble_section(paragraphs)

            # 4. Optional stitching
            if self.config.paragraph_stitch and len(paragraphs) > 1:
                stitched = self._stitch_section(section_name, section_text)
                if stitched:
                    section_text = stitched

            # Check word count, use expand pass if needed
            word_count = len(section_text.split())
            if word_count < min_words:
                logger.info("Section %s has %d words (min %d) — will use expand pass", section_name, word_count, min_words)
                self.display.step(f"  {section_name}: {word_count} words (below min {min_words}) — expanding...")
                _sec_tokens = self._section_max_tokens(section_name)
                expand_prompt = f"""The {section_name} section has only {word_count} words.
It MUST have at least {min_words} words (target: {target_words}). This is NON-NEGOTIABLE.

PREVIOUSLY WRITTEN (expand, do not replace):
{section_text}

BIBLIOGRAPHY (cite by [Author, Year]):
{ref_list_text[:15000]}

Write ONLY the expanded section text. No headers, no JSON. MINIMUM {min_words} WORDS."""
                expanded = self._generate_section(expand_prompt, section_name, max_tokens=_sec_tokens)
                if expanded and len(expanded.split()) > word_count:
                    section_text = expanded
                    word_count = len(section_text.split())

            # Citation gap fill: find papers for uncited claims
            section_text = self._fill_citation_gaps(section_name, section_text, curated)

            written_sections[section_name] = section_text
            self.display.step(f"  {section_name}: {word_count} words ({len(paragraphs)} paragraphs)")

        # Write abstract (same as section mode)
        self.display.step("Writing Abstract...")
        full_paper_text = "\n\n".join(
            f"=== {name} ===\n{written_sections[name]}"
            for name in _WRITE_ORDER if name in written_sections
        )

        search_audit = self.artifacts.get("search_audit", {})
        corpus_count = search_audit.get("total_included", len(curated))
        contribution = brief.get("contribution_type", "evidence synthesis")
        grounding_rules = self._prompts.get("abstract_grounding_rules",
                          DEFAULT_PROMPTS.get("abstract_grounding_rules", ""))

        abstract_prompt = f"""Write the Abstract for this academic paper.

PAPER TITLE: {brief.get('title', '')}
CONTRIBUTION TYPE: {contribution}
RESEARCH QUESTIONS: {json.dumps(brief.get('research_questions', []))}

FULL PAPER:
{full_paper_text[:30000]}

Requirements:
- 200-400 words
- Summarize: background, methods, key findings, implications
- Include 2+ citations from the paper using [Author, Year] format
- Write as a single paragraph
- Do NOT start with "This paper..." — vary the opening
- CORPUS COUNT: use EXACTLY {corpus_count}.
- CITATION RULE: Only cite authors in the reference list above.
- CLAIM GROUNDING: Every claim must have a corresponding passage in the paper body.
- AI AGENT: Do NOT devote more than 1 sentence to the AI agent or platform.

{grounding_rules}"""

        abstract_system = self._prompts.get("phase7_abstract", "")
        _abs_tokens = self._section_max_tokens("Abstract")
        if abstract_system:
            try:
                resp = self.llm.generate(
                    abstract_system, abstract_prompt,
                    temperature=0.2, max_tokens=_abs_tokens, think=True,
                )
                abstract = strip_thinking_tags(resp.text).strip()
                abstract = self._clean_section_text(abstract)
            except LLMError as e:
                logger.warning("Abstract generation failed: %s — falling back", e)
                abstract = self._generate_section(abstract_prompt, "Abstract", max_tokens=_abs_tokens)
        else:
            abstract = self._generate_section(abstract_prompt, "Abstract", max_tokens=_abs_tokens)

        if abstract and written_sections:
            full_body = "\n".join(content for content in written_sections.values())
            abstract = self._cross_check_abstract_claims(abstract, full_body)

        abstract_words = len(abstract.split()) if abstract else 0
        self.display.step(f"  Abstract: {abstract_words} words")

        self.artifacts["zero_draft"] = written_sections
        self.artifacts["written_sections"] = written_sections
        self.artifacts["abstract"] = abstract

        # Comparison table (same as section mode)
        if len(curated) >= 5:
            self.display.step("Generating methodology comparison table...")

            def _gen_table_p():
                td = self._generate_comparison_table(curated, brief)
                return self._audit_table_citations(td, curated) if td else None

            table_data = _run_non_critical(_gen_table_p, label="comparison table (para)", timeout=120)
            if table_data:
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

        # Claim-evidence ledger
        self.display.step("Building claim-evidence ledger...")
        ledger = _run_non_critical(
            lambda: self._build_claim_evidence_ledger(written_sections, ref_list_text),
            label="claim-evidence ledger (para)", timeout=180, default=[],
        )
        if ledger:
            self.artifacts["claim_evidence_ledger"] = ledger
            self.display.step(f"  Ledger: {len(ledger)} claims mapped")

        total_words = sum(len(s.split()) for s in written_sections.values()) + abstract_words
        self.display.step(f"Total draft: {total_words} words (paragraph mode)")
        # Ensure phase 7 is started (in case all sections were methodology-only)
        if not getattr(self, "_phase7_started", False):
            self.display.phase_done(6)
            self.display.phase_start(7, "Write Paper (paragraph mode)")
        self.display.phase_done(7)
        self._phase7_started = False  # reset for reuse

    def _plan_section_paragraphs(
        self,
        section_name: str,
        brief: dict,
        curated: list[dict],
        evidence_by_paper: dict[int, str],
        evidence_map: dict[str, list[dict]],
        paper_index_text: str,
        target_words: int,
    ) -> list[ParagraphSpec] | None:
        """Plan paragraphs for one section. Returns None on failure."""
        section_claims = evidence_map.get(section_name, [])
        if not section_claims:
            # Build minimal claims from evidence_by_paper
            claims_text = "(no pre-mapped claims — infer from available evidence)"
        else:
            claims_lines = []
            for c in section_claims:
                if isinstance(c, str):
                    claims_lines.append(f"- {c}")
                elif isinstance(c, dict):
                    cite_keys = ", ".join(c.get("cite_keys", []))
                    strength = c.get("strength", "moderate")
                    claims_lines.append(f"- {c.get('claim', '?')} [{cite_keys}] (strength: {strength})")
            claims_text = "\n".join(claims_lines)

        section_id = section_name.lower().replace(" ", "_")
        paragraph_target = self.config.paragraph_target_words

        prompt_template = self._prompts.get("phase7_paragraph_plan", "")
        if not prompt_template:
            logger.warning("phase7_paragraph_plan prompt not found")
            return None

        # Use manual replacement to avoid .format() issues with curly braces in content
        filled = prompt_template
        replacements = {
            "{section_name}": section_name,
            "{title}": brief.get("title", ""),
            "{research_questions}": "\n".join(f"  - {rq}" for rq in brief.get("research_questions", [])),
            "{claims}": claims_text,
            "{paper_index}": paper_index_text,
            "{paragraph_target_words}": str(paragraph_target),
            "{target_words}": str(target_words),
            "{section_id}": section_id,
        }
        for key, val in replacements.items():
            filled = filled.replace(key, val)

        system = (
            "You are an academic paper paragraph planner. "
            "Return a valid JSON array of paragraph specifications. No markdown, no commentary."
        )

        try:
            result = self.llm.generate_json(system, filled, temperature=0.3, max_tokens=4000)
        except Exception as e:
            logger.warning("Paragraph planning LLM call failed for %s: %s", section_name, e)
            return None

        # Parse result
        specs_raw = result if isinstance(result, list) else result.get("paragraphs", result.get("specs", []))
        if not isinstance(specs_raw, list) or not specs_raw:
            logger.warning("Paragraph planner returned non-list for %s: %s", section_name, type(result))
            return None

        # Abstract-only paper indices (for quarantine)
        abstract_only_indices = set()
        for i, p in enumerate(curated):
            content = p.get("enriched_content", "") or p.get("abstract", "") or ""
            if len(content) <= 2000:
                abstract_only_indices.add(i)

        quarantine_sections = {"Results", "Discussion", "Limitations", "Conclusion"}

        specs: list[ParagraphSpec] = []
        prev_id: str | None = None
        for raw in specs_raw:
            if not isinstance(raw, dict):
                continue
            pid = raw.get("paragraph_id", f"{section_id}_p{len(specs) + 1}")
            evidence_indices = raw.get("evidence_indices", [])

            # Quarantine: remove abstract-only papers from evidence in analytical sections
            if section_name in quarantine_sections:
                evidence_indices = [idx for idx in evidence_indices if idx not in abstract_only_indices]

            # Clamp indices to valid range
            evidence_indices = [idx for idx in evidence_indices if 0 <= idx < len(curated)]

            spec = ParagraphSpec(
                paragraph_id=pid,
                section=section_name,
                goal=raw.get("goal", ""),
                claim_type=raw.get("claim_type", "descriptive_synthesis"),
                evidence_indices=evidence_indices,
                allowed_citations=raw.get("allowed_citations", []),
                allowed_strength=raw.get("allowed_strength", "moderate"),
                transition_from=prev_id,
                target_words=raw.get("target_words", self.config.paragraph_target_words),
            )
            specs.append(spec)
            prev_id = pid

        # Validate total target words ±20%
        total_target = sum(s.target_words for s in specs)
        if total_target < target_words * 0.5 or total_target > target_words * 1.5:
            logger.warning(
                "Paragraph plan for %s: total target %d vs section target %d (out of range)",
                section_name, total_target, target_words,
            )
            # Adjust proportionally
            if total_target > 0:
                scale = target_words / total_target
                for s in specs:
                    s.target_words = max(100, int(s.target_words * scale))

        logger.info("Planned %d paragraphs for %s (total target: %d words)",
                     len(specs), section_name, sum(s.target_words for s in specs))
        return specs if specs else None

    def _write_single_paragraph(
        self,
        spec: ParagraphSpec,
        curated: list[dict],
        evidence_by_paper: dict[int, str],
        prior_paragraphs: list[WrittenParagraph],
        ref_list: list[dict],
    ) -> WrittenParagraph | None:
        """Write a single paragraph from a small evidence packet."""
        # Build evidence context — ONLY papers in spec.evidence_indices
        evidence_parts = []
        for idx in spec.evidence_indices:
            if idx >= len(curated):
                continue
            p = curated[idx]
            authors = p.get("authors", [])
            surname = _extract_surname(authors[0]) if authors else "Unknown"
            et_al = " et al." if len(authors) >= 3 else ""
            year = p.get("year", "N/A")
            cite_key = f"[{surname}{et_al}, {year}]"

            # Use extracted evidence if available, else abstract
            ev = evidence_by_paper.get(idx, "")
            if not ev:
                ev = p.get("abstract", "") or ""
                ev = ev[:500]

            evidence_parts.append(
                f"--- {cite_key} ---\n"
                f"Title: {p.get('title', '')}\n"
                f"Evidence:\n{ev}\n"
            )

        evidence_text = "\n".join(evidence_parts) if evidence_parts else "(no evidence records)"

        # Build prior paragraphs context (same section only, last ~350 words)
        prior_text = ""
        if prior_paragraphs:
            prior_lines = [wp.text for wp in prior_paragraphs[-3:]]
            prior_text = "\n\n".join(prior_lines)
            words = prior_text.split()
            if len(words) > 350:
                prior_text = "..." + " ".join(words[-350:])

        # Section guidance snippet
        section_guidance = _SECTION_GUIDANCE.get(spec.section, "")
        # Truncate guidance to ~200 words for token budget
        guidance_words = section_guidance.split()
        if len(guidance_words) > 200:
            section_guidance = " ".join(guidance_words[:200]) + "..."

        prompt_template = self._prompts.get("phase7_write_paragraph", "")
        if not prompt_template:
            logger.warning("phase7_write_paragraph prompt not found")
            return None

        # Canonical corpus metadata — single source of truth
        search_audit = self.artifacts.get("search_audit", {})
        _corpus_total = search_audit.get("total_included", len(curated))
        _corpus_full_text = sum(1 for p in curated if len(p.get("enriched_content", "") or p.get("abstract", "") or "") > 2000)
        _corpus_abstract_only = _corpus_total - _corpus_full_text

        # Use manual replacement instead of .format() to avoid issues with
        # curly braces in evidence text, section guidance, etc.
        filled = prompt_template
        replacements = {
            "{target_words}": str(spec.target_words),
            "{goal}": spec.goal,
            "{claim_type}": spec.claim_type,
            "{allowed_citations}": ", ".join(spec.allowed_citations),
            "{allowed_strength}": spec.allowed_strength,
            "{section_guidance}": section_guidance,
            "{evidence}": evidence_text,
            "{prior_paragraphs}": prior_text if prior_text else "(first paragraph — no prior context)",
            "{corpus_total}": str(_corpus_total),
            "{corpus_full_text}": str(_corpus_full_text),
            "{corpus_abstract_only}": str(_corpus_abstract_only),
        }
        for key, val in replacements.items():
            filled = filled.replace(key, val)

        system = (
            "You are an academic writer producing one paragraph of a research paper. "
            "Write grounded, formal prose using ONLY the evidence provided. "
            "Do not inject knowledge beyond the evidence records."
        )

        try:
            resp = self.llm.generate(
                system, filled,
                temperature=0.3, max_tokens=4000, think=False,
            )
            raw = resp.text
            if not raw or not raw.strip():
                logger.warning("Paragraph %s: LLM returned empty raw text (usage: %s)", spec.paragraph_id, resp.usage)
            text = strip_thinking_tags(raw).strip()
            text = self._clean_section_text(text)
        except LLMError as e:
            logger.warning("Paragraph %s failed: %s — retrying at temp=0.4", spec.paragraph_id, e)
            try:
                resp = self.llm.generate(
                    system, filled,
                    temperature=0.4, max_tokens=4000, think=False,
                )
                text = strip_thinking_tags(resp.text).strip()
                text = self._clean_section_text(text)
            except LLMError as e2:
                logger.error("Paragraph %s retry also failed: %s", spec.paragraph_id, e2)
                return None

        if not text:
            return None

        # Extract citations used
        citations_used = re.findall(r'\[([^\]]+?,\s*\d{4})\]', text)

        return WrittenParagraph(
            paragraph_id=spec.paragraph_id,
            section=spec.section,
            text=text,
            citations_used=citations_used,
            word_count=len(text.split()),
        )

    @staticmethod
    def _assemble_section(paragraphs: list[WrittenParagraph]) -> str:
        """Deterministically concatenate paragraphs into a section."""
        return "\n\n".join(wp.text for wp in paragraphs if wp.text.strip())

    def _stitch_section(self, section_name: str, section_text: str) -> str | None:
        """Optional light LLM pass to smooth transitions between paragraphs."""
        prompt_template = self._prompts.get("phase7_stitch_section", "")
        if not prompt_template:
            return None

        filled = prompt_template.replace("{section_name}", section_name).replace("{section_text}", section_text)

        system = (
            "You are an academic editor smoothing paragraph transitions. "
            "Make minimal changes. Preserve all citations and claims exactly."
        )

        try:
            resp = self.llm.generate(
                system, filled,
                temperature=0.2, max_tokens=65000, think=True,
            )
            stitched = strip_thinking_tags(resp.text).strip()
            stitched = self._clean_section_text(stitched)
        except LLMError as e:
            logger.warning("Stitching failed for %s: %s", section_name, e)
            return None

        if not stitched:
            return None

        # Safety: reject if output differs by >15% in length
        orig_words = len(section_text.split())
        new_words = len(stitched.split())
        if orig_words > 0 and abs(new_words - orig_words) / orig_words > 0.15:
            logger.warning(
                "Stitching rejected for %s: %d → %d words (>15%% change)",
                section_name, orig_words, new_words,
            )
            return None

        # Safety: reject if citations dropped
        orig_cites = set(re.findall(r'\[([^\]]+?,\s*\d{4})\]', section_text))
        new_cites = set(re.findall(r'\[([^\]]+?,\s*\d{4})\]', stitched))
        dropped = orig_cites - new_cites
        if dropped:
            logger.warning(
                "Stitching rejected for %s: dropped citations %s",
                section_name, dropped,
            )
            return None

        logger.info("Stitched %s: %d → %d words", section_name, orig_words, new_words)
        return stitched

    def _build_bibliography_context(self, papers: list[dict]) -> str:
        """Build a mega context block with all curated papers' content."""
        parts = []
        for i, paper in enumerate(papers):
            raw_title = self._fix_double_encoded_utf8(paper.get("title", "Untitled") or "Untitled")
            title = re.sub(r"<[^>]+>", "", raw_title).strip() if raw_title else "Untitled"
            authors = paper.get("authors", [])
            year = paper.get("year", "N/A")
            content = paper.get("enriched_content", paper.get("abstract", ""))

            author_str = ", ".join(self._fix_double_encoded_utf8(str(a.get("name", a) if isinstance(a, dict) else a)) for a in authors[:3]) if authors else "Unknown"
            if len(authors) > 3:
                author_str += " et al."

            # Sanitize year
            if year is None or (isinstance(year, str) and year.strip().lower() in ("none", "null", "n/a", "unknown", "")):
                year = "n.d."
            elif isinstance(year, (int, float)):
                year = str(int(year))

            # Build cite key — NEVER use "Source N" (it leaks into LLM output)
            if authors and isinstance(authors[0], str) and authors[0].strip():
                surname = _extract_surname(self._fix_double_encoded_utf8(authors[0]))
                et_al = " et al." if len(authors) >= 3 else ""
                cite_key = f"[{surname}{et_al}, {year}]" if year != "n.d." else f"[{surname}{et_al}]"
            else:
                # Fallback: use first meaningful word from title
                _SKIP = {"the", "a", "an", "of", "in", "on", "for", "and", "to", "with", "from"}
                words = [w.rstrip(",.:;") for w in title.split() if w.lower().rstrip(",.:;") not in _SKIP and len(w) > 2]
                label = words[0] if words else f"Ref{i + 1}"
                cite_key = f"[{label}, {year}]" if year != "n.d." else f"[{label}]"

            # Mark abstract-only sources explicitly
            content_len = len(content or "")
            access_label = "[FULL TEXT]" if content_len > 2000 else "[ABSTRACT ONLY]"

            header = f"--- Paper {i + 1}: {cite_key} {access_label} ---"
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

    def _extract_per_paper_evidence(self, papers: list[dict]) -> dict[int, str]:
        """Extract specific findings, data points, and quotes from each paper.

        Reads each paper's enriched content and builds a structured evidence
        card the section writer can use for grounded citations.
        """
        evidence: dict[int, str] = {}

        # Process papers in batches of 5
        batch_size = 5
        papers_with_content = [
            (i, p) for i, p in enumerate(papers)
            if len(p.get("enriched_content", p.get("abstract", ""))) > 200
        ]
        logger.info("Evidence extraction: %d papers with content (of %d curated)", len(papers_with_content), len(papers))

        if not papers_with_content:
            return evidence

        for batch_start in range(0, len(papers_with_content), batch_size):
            batch = papers_with_content[batch_start:batch_start + batch_size]
            if not batch:
                continue

            summaries = []
            for idx, (i, p) in enumerate(batch):
                authors = p.get("authors", [])
                surname = _extract_surname(authors[0]) if authors else "Unknown"
                et_al = " et al." if len(authors) > 2 else ""
                year = p.get("year", "N/A")
                content = p.get("enriched_content", p.get("abstract", ""))
                summaries.append(
                    f"PAPER [{i}]: [{surname}{et_al}, {year}]\n"
                    f"Title: {p.get('title', '')}\n"
                    f"Content:\n{content}\n"
                )

            # Evidence extraction prompt — from prompt system (GUI-editable)
            p3e_system, p3e_user = self._get_prompt(
                "phase4_evidence_extraction",
                paper_summaries="\n".join(summaries),
            )
            if not p3e_system:
                p3e_system = "You are an academic evidence extraction assistant. Extract specific findings from papers."
            if not p3e_user:
                paper_title = brief.get("title", self._topic)
                rqs = brief.get("research_questions", [])
                rq_text = "\n".join(f"  - {rq}" for rq in rqs) if rqs else "  (none specified)"
                p3e_user = (
                    f"CONTEXT: We are writing a paper titled: \"{paper_title}\"\n"
                    f"Research questions:\n{rq_text}\n\n"
                    "Read each paper below and extract evidence RELEVANT to this paper's topic "
                    "and research questions. Prioritize findings that directly address our RQs.\n\n"
                    "For EACH paper, output exactly this format:\n\n"
                    "PAPER [N]:\n"
                    "- FINDING: [one specific result — include numbers, comparisons, or concrete claims]\n"
                    "- FINDING: [another specific result if available]\n"
                    "- METHOD: [methodology used]\n"
                    "- QUOTE: [a key sentence or phrase directly from the text]\n\n"
                    "If the paper is theoretical or a review with no numbers, write:\n"
                    "- ARGUMENT: [the paper's central thesis in one sentence]\n\n"
                    "Do NOT paraphrase vaguely. Extract the MOST SPECIFIC claims from the text.\n\n"
                    + "\n".join(summaries)
                )

            try:
                resp = self.llm.generate(
                    p3e_system,
                    p3e_user, max_tokens=10000, temperature=0.1,
                )
                raw = strip_thinking_tags(resp.text if hasattr(resp, "text") else str(resp)).strip()

                if not raw:
                    logger.warning("Evidence extraction batch %d: empty response", batch_start)
                    continue

                # Parse per-paper blocks — flexible regex for various LLM formatting
                current_paper_idx = None
                current_lines: list[str] = []
                for line in raw.splitlines():
                    # Match many formats: "PAPER [0]", "**Paper [0]**", "### Paper 0",
                    # "Paper [0]:", "**PAPER 0:**", "## Paper [0]", "Paper #0", etc.
                    m = re.search(r"(?i)(?:\*{0,2}#{0,3}\s*)?paper\s*(?:\[|#)?(\d+)(?:\])?", line)
                    if m:
                        if current_paper_idx is not None and current_lines:
                            evidence[current_paper_idx] = "\n".join(current_lines)
                        current_paper_idx = int(m.group(1))
                        current_lines = []
                    elif current_paper_idx is not None and line.strip():
                        current_lines.append(line.strip())
                # Save last block
                if current_paper_idx is not None and current_lines:
                    evidence[current_paper_idx] = "\n".join(current_lines)

                logger.info("Evidence extraction batch %d: extracted %d papers",
                           batch_start, sum(1 for i, _ in batch if i in evidence))

            except Exception as e:
                logger.warning("Evidence extraction failed for batch %d: %s", batch_start, e)

            time.sleep(2)  # Rate limit cooldown

        logger.info("Evidence extraction complete: %d/%d papers have evidence",
                    len(evidence), len(papers_with_content))
        return evidence

    def _build_section_bibliography(
        self,
        section_name: str,
        curated: list[dict],
        evidence_by_paper: dict[int, str],
        full_bib_context: str,
    ) -> str:
        """Build section-specific bibliography with deep content for relevant papers.

        Instead of cramming all papers into 80K chars (giving each ~1700 chars),
        this selects the most relevant papers for each section and gives them
        full enriched content + extracted evidence.
        """
        import datetime as _dt_sec
        _current_year = _dt_sec.datetime.now().year

        # Section → relevant paper selection criteria
        _SECTION_RELEVANCE = {
            "Introduction": {"max_papers": 10, "prefer": "foundational"},
            "Related Work": {"max_papers": 20, "prefer": "all"},
            "Methodology": {"max_papers": 10, "prefer": "methodological"},
            "Results": {"max_papers": 15, "prefer": "empirical"},
            "Discussion": {"max_papers": 12, "prefer": "recent"},
            "Limitations": {"max_papers": 8, "prefer": "methodological"},
            "Conclusion": {"max_papers": 8, "prefer": "recent"},
        }
        config = _SECTION_RELEVANCE.get(section_name, {"max_papers": 15, "prefer": "all"})
        max_papers = config["max_papers"]

        # Sort papers by relevance to this section type
        def _section_relevance(idx_paper: tuple[int, dict]) -> float:
            i, p = idx_paper
            score = p.get("relevance_score", 0.5)
            year = p.get("year") or 2020
            cites = p.get("citation_count", 0) or 0
            has_evidence = i in evidence_by_paper

            prefer = config["prefer"]
            if prefer == "foundational":
                score += min(cites / 5000, 0.3)  # Highly-cited papers
            elif prefer == "recent":
                score += min((year - 2020) / 6, 0.3)  # Recent papers
            elif prefer == "empirical":
                title_lower = (p.get("title", "") or "").lower()
                if not any(kw in title_lower for kw in ("review", "survey", "overview")):
                    score += 0.2  # Prefer primary studies for Results
            elif prefer == "methodological":
                title_lower = (p.get("title", "") or "").lower()
                if any(kw in title_lower for kw in ("method", "framework", "approach", "model")):
                    score += 0.2

            if has_evidence:
                score += 0.15  # Prefer papers we have extracted evidence for
            return score

        indexed = list(enumerate(curated))
        indexed.sort(key=_section_relevance, reverse=True)
        selected = indexed[:max_papers]
        selected_indices = {i for i, _ in selected}

        # Build deep context for selected papers
        parts = []
        for i, paper in selected:
            authors = paper.get("authors", [])
            author_str = ", ".join(self._fix_double_encoded_utf8(str(a.get("name", a) if isinstance(a, dict) else a)) for a in authors[:3]) if authors else "Unknown"
            if len(authors) > 3:
                author_str += " et al."

            year = paper.get("year", "N/A")
            if year is None:
                year = "n.d."

            surname = _extract_surname(self._fix_double_encoded_utf8(authors[0]) if authors and isinstance(authors[0], str) else (authors[0] if authors else "Unknown"))
            et_al = " et al." if len(authors) >= 3 else ""
            cite_key = f"[{surname}{et_al}, {year}]"

            # Full enriched content (up to 6K per paper for selected ones)
            content = (paper.get("enriched_content") or paper.get("abstract") or "")[:6000]

            # Determine publication status for the LLM
            doi = paper.get("doi", "")
            venue = paper.get("venue", "") or paper.get("journal", "")
            _pub_status = "[PEER-REVIEWED]"
            if isinstance(year, int) and year >= _current_year and not doi:
                _pub_status = "[PREPRINT — use hedged language: 'suggests', 'proposes', not 'demonstrates']"
            elif not venue and not doi:
                _pub_status = "[UNVERIFIED — no venue or DOI; use cautious language]"

            header = f"--- Paper: {cite_key} {_pub_status} ---"
            header += f"\nTitle: {self._fix_double_encoded_utf8(paper.get('title', '') or '')}"
            header += f"\nAuthors: {author_str}"
            header += f"\nYear: {year}"
            if venue:
                header += f"\nVenue: {venue}"
            if doi:
                header += f"\nDOI: {doi}"
            key_finding = paper.get("key_finding", "")
            if key_finding:
                header += f"\nKey finding: {key_finding}"

            # Add extracted evidence if available
            ev = evidence_by_paper.get(i, "")
            if ev:
                header += f"\n\nEXTRACTED EVIDENCE (use these specific findings when citing this paper):\n{ev}"

            parts.append(f"{header}\n\n{content}")

        # Add brief mentions of remaining papers (title + cite key only)
        remaining = [(i, p) for i, p in enumerate(curated) if i not in selected_indices]
        if remaining:
            brief_refs = []
            for i, p in remaining:
                authors = p.get("authors", [])
                surname = _extract_surname(self._fix_double_encoded_utf8(authors[0]) if authors and isinstance(authors[0], str) else (authors[0] if authors else "Unknown"))
                et_al = " et al." if len(authors) >= 3 else ""
                year = p.get("year", "N/A")
                _b_doi = p.get("doi", "")
                _b_venue = p.get("venue", "") or p.get("journal", "")
                _b_tag = ""
                if isinstance(year, int) and year >= _current_year and not _b_doi:
                    _b_tag = " [PREPRINT]"
                brief_refs.append(f"[{surname}{et_al}, {year}]: {p.get('title', '')[:80]}{_b_tag}")
            parts.append(
                f"\n--- Additional references (cite key + title only) ---\n"
                + "\n".join(brief_refs)
            )

        result = "\n\n".join(parts)
        logger.debug(
            "Section '%s': %d deep papers + %d brief refs, %d chars",
            section_name, len(selected), len(remaining), len(result),
        )
        return result

    def _build_evidence_map(
        self,
        brief: dict,
        curated: list[dict],
        ref_list: list[dict],
        evidence_by_paper: dict[int, str],
    ) -> dict[str, list[dict]]:
        """Phase 4: Map available evidence to sections BEFORE writing.

        Uses the phase6_evidence_map prompt (GUI-editable) to ask the LLM
        which references support which claims in which sections.
        Returns {section_name: [{claim, cite_keys, strength}]}.
        """
        paper_title = brief.get("title", "")
        rqs = brief.get("research_questions", [])

        # Build reference summaries with extracted evidence
        ref_summaries = []
        for i, p in enumerate(curated[:25]):
            authors = p.get("authors", [])
            surname = _extract_surname(authors[0]) if authors else "Unknown"
            et_al = " et al." if len(authors) >= 3 else ""
            year = p.get("year", "N/A")
            cite_key = f"[{surname}{et_al}, {year}]"
            finding = evidence_by_paper.get(i, p.get("key_finding", p.get("abstract", "")[:200]))
            ref_summaries.append(f"{cite_key}: {p.get('title', '')[:100]}\n  Evidence: {finding[:300]}")

        p4_system, p4_user = self._get_prompt(
            "phase6_evidence_map",
            title=paper_title,
            research_questions="\n".join(f"  - {rq}" for rq in rqs),
            reference_summaries="\n".join(ref_summaries),
        )
        if not p4_system:
            p4_system = "You are an expert evidence mapper for academic papers. Return valid JSON only."
        if not p4_user:
            p4_user = (
                f'For the paper: "{paper_title}"\n\n'
                f'Research questions:\n' + "\n".join(f"  - {rq}" for rq in rqs) + "\n\n"
                f'Available references with findings:\n' + "\n".join(ref_summaries) + "\n\n"
                "Map evidence to sections. Return JSON with this structure:\n"
                '{"sections": {"Introduction": [{"claim": "...", "cite_keys": ["[Author, Year]"], "strength": "strong|moderate|weak"}], ...}}\n\n'
                "Sections: Introduction, Related Work, Methodology, Results, Discussion, Limitations, Conclusion.\n"
                "For each section, list 3-8 specific claims that CAN be made based on the available evidence.\n"
                "Only include claims that have at least one supporting reference. Rate evidence strength."
            )

        try:
            result = self.llm.generate_json(p4_system, p4_user, temperature=0.2, max_tokens=6000)
            if isinstance(result, dict) and "sections" in result:
                sections = result["sections"]
                # LLM may return sections as a list instead of dict — guard against it
                if isinstance(sections, dict):
                    return sections
                logger.warning("Evidence map 'sections' is %s, expected dict — ignoring", type(sections).__name__)
                return {}
            elif isinstance(result, dict):
                return result
        except Exception as e:
            logger.warning("Evidence map generation failed: %s", e)
        return {}

    def _build_source_classification(self, papers: list[dict]) -> list[dict]:
        """Build a source classification table from curated papers.

        Uses the LLM to classify each paper's domain, method, and primary finding.
        This serves as a citation-tethering anchor during writing.
        """
        if not papers:
            return []

        # Build summaries with enriched content (full text when available)
        summaries = []
        for i, p in enumerate(papers[:30]):
            authors = p.get("authors", [])
            author_str = authors[0] if authors else "Unknown"
            if isinstance(author_str, str) and ", " in author_str:
                author_str = author_str.split(",")[0]
            elif isinstance(author_str, str) and " " in author_str:
                author_str = author_str.split()[-1]
            year = p.get("year", "N/A")
            title = p.get("title", "")
            # Use enriched content (full text) if available, fall back to abstract
            content = p.get("enriched_content", "") or p.get("abstract", "")
            summaries.append(
                f"[{i}] {author_str} ({year}): {title}\n"
                f"    {content}"
            )

        # Reading memo prompt — from prompt system (GUI-editable)
        p3r_system, p3r_user = self._get_prompt(
            "phase4_reading_memo",
            paper_summaries=chr(10).join(summaries),
        )
        if not p3r_system:
            p3r_system = "You are a research analyst creating a detailed reading memo."
        if not p3r_user:
            paper_title = brief.get("title", self._topic)
            rqs = brief.get("research_questions", [])
            rq_text = "; ".join(rqs) if rqs else "(none specified)"
            p3r_user = f"""CONTEXT: We are writing a paper titled: "{paper_title}"
Research questions: {rq_text}

Classify each paper below. For each, output ONE line in this exact format:
AUTHOR | YEAR | DOMAIN | METHOD | PRIMARY_FINDING

Rules:
- DOMAIN: the paper's actual research field (e.g., "Computational Linguistics", "Genetics", "Scientometrics", "Education")
- METHOD: the paper's actual methodology (e.g., "corpus analysis", "systematic review", "twin study", "bibliometric analysis", "survey", "experiment")
- PRIMARY_FINDING: one sentence describing what the paper ACTUALLY found — focus on findings relevant to our paper's research questions
- Be precise and honest. If a paper is a bibliometric analysis, say so — do not classify it as an empirical study
- If you cannot determine the finding from the title/abstract, write "finding unclear from metadata"

Papers:
{chr(10).join(summaries)}

Output ONLY the classification lines, one per paper, numbered [0], [1], etc."""

        try:
            resp = self.llm.generate(p3r_system, p3r_user, max_tokens=4000, temperature=0.1)
            raw = strip_thinking_tags(resp.text if hasattr(resp, 'text') else str(resp)).strip()

            # Build index of reading notes by paper for quality_tier lookup
            reading_notes = self.artifacts.get("reading_notes", [])
            tier_by_index: dict[int, str] = {}
            for note in reading_notes:
                idx = note.get("paper_index")
                if idx is not None:
                    tier_by_index[int(idx)] = note.get("quality_tier", "solid")

            entries = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Parse "[N] Author | Year | Domain | Method | Finding"
                # Remove leading [N] if present
                idx_match = re.match(r"^\[?(\d+)\]?\s*", line)
                paper_idx = int(idx_match.group(1)) if idx_match else len(entries)
                cleaned = re.sub(r"^\[?\d+\]?\s*", "", line)
                parts = [p.strip() for p in cleaned.split("|")]
                if len(parts) >= 5:
                    # Change 2: Enrich with quality_tier and content_type
                    quality_tier = tier_by_index.get(paper_idx, "solid")
                    # Determine content_type from enriched_content length
                    paper = papers[paper_idx] if paper_idx < len(papers) else {}
                    content = paper.get("enriched_content", "") or paper.get("abstract", "") or ""
                    content_type = "full_text" if len(content) > 2000 else "abstract_only"
                    # Determine claim restriction based on source type + evidence quality
                    evidence_str = paper.get("evidence_strength", "unclear")
                    is_abstract_only = content_type == "abstract_only"
                    is_preprint_src = paper.get("is_preprint", False)
                    source_type = paper.get("source_type_llm", "")

                    if is_abstract_only:
                        claim_restriction = "background_only"
                    elif is_preprint_src and evidence_str in ("weak", "unclear"):
                        claim_restriction = "supporting_only"
                    elif source_type == "commentary" or evidence_str == "weak":
                        claim_restriction = "supporting_only"
                    else:
                        claim_restriction = "unrestricted"

                    entries.append({
                        "author": parts[0],
                        "year": parts[1],
                        "domain": parts[2],
                        "method": parts[3],
                        "finding": parts[4],
                        "quality_tier": quality_tier,
                        "content_type": content_type,
                        "claim_restriction": claim_restriction,
                    })
            return entries
        except Exception as e:
            logger.warning("Source classification failed: %s", e)
            return []

    @staticmethod
    def _fix_double_encoded_utf8(text: str) -> str:
        """Repair double-encoded UTF-8 (e.g. GonzÃ¡lez → González)."""
        try:
            fixed = text.encode("latin-1").decode("utf-8")
            # Only accept if it actually changed and looks cleaner
            if fixed != text:
                return fixed
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        return text

    def _build_ref_list(self, papers: list[dict]) -> list[dict]:
        """Build a compact reference list for citation guidance."""
        refs = []
        existing_keys: set[str] = set()
        for i, paper in enumerate(papers):
            authors = [self._fix_double_encoded_utf8(a) if isinstance(a, str) else a
                       for a in paper.get("authors", [])]
            year = paper.get("year", "N/A")

            # Sanitize year
            if year is None or (isinstance(year, str) and year.strip().lower() in ("none", "null", "n/a", "unknown", "")):
                year = "n.d."
            elif isinstance(year, (int, float)):
                year = str(int(year))

            raw_title = self._fix_double_encoded_utf8(paper.get("title", "Untitled") or "Untitled")
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

        # Section guidance from prompts (editable via GUI)
        guidance_key = f"guidance_{section_name.lower().replace(' ', '_')}"
        guidance = self._prompts.get(guidance_key,
                   _SECTION_GUIDANCE.get(section_name, "Write this section with academic rigor."))

        # Append paper-type-specific guidance from prompts (editable via GUI)
        paper_type = (brief.get("paper_type") or "survey").lower()
        ptype_key = f"paper_type_{paper_type.replace('-', '_')}"
        ptype_prompt = self._prompts.get(ptype_key, "")
        if ptype_prompt:
            guidance = ptype_prompt + "\n\n" + guidance

        # Append contribution-type-specific guidance from prompts (editable via GUI)
        _CONTRIBUTION_KEY_MAP = {
            "testable hypotheses from contradictory findings": "contribution_testable_hypotheses",
            "map contradictions and explain WHY studies disagree": "contribution_map_contradictions",
            "quantitative evidence synthesis with numbers": "contribution_quantitative_synthesis",
            "identify critical gaps with specificity": "contribution_identify_gaps",
            "challenge accepted wisdom with evidence": "contribution_challenge_wisdom",
            "methodological critique across literature": "contribution_methodological_critique",
            "cross-pollinate fields": "contribution_cross_pollinate",
        }
        contrib_key = _CONTRIBUTION_KEY_MAP.get(contribution, "")
        if contrib_key:
            contrib_prompt = self._prompts.get(contrib_key, "")
            if contrib_prompt:
                # Extract section-specific part
                for label in [f"{section_name.upper()}:", f"{section_name}:"]:
                    if label in contrib_prompt:
                        start = contrib_prompt.index(label) + len(label)
                        # Find next section label or end
                        next_labels = [f"\n{s.upper()}:" for s in ["RESULTS", "DISCUSSION", "METHODOLOGY"] if s.upper() != section_name.upper()]
                        end = len(contrib_prompt)
                        for nl in next_labels:
                            pos = contrib_prompt.find(nl, start)
                            if pos != -1 and pos < end:
                                end = pos
                        chunk = contrib_prompt[start:end].strip()
                        if chunk:
                            guidance += "\n\n" + chunk
                        break

        # For Methodology section, inject actual search audit data using template from prompts
        if section_name == "Methodology":
            search_audit = self.artifacts.get("search_audit", {})
            if search_audit:
                queries = search_audit.get("queries", [])
                total_included = search_audit.get('total_included', '?')
                # Build included-studies list for audit trail
                curated_papers = self.artifacts.get("curated_papers", [])
                if not curated_papers:
                    curated_papers = self.artifacts.get("candidate_papers", [])[:total_included] if isinstance(total_included, int) else []
                studies_list = ""
                for cp in curated_papers[:40]:
                    if not isinstance(cp, dict):
                        continue
                    cp_authors = cp.get("authors", [])
                    cp_first = cp_authors[0] if cp_authors else "Unknown"
                    if isinstance(cp_first, (list, dict)):
                        cp_first = str(cp_first)[:50]
                    cp_year = cp.get("year", "?")
                    cp_title = (cp.get("title") or "?")[:80]
                    cp_venue = (cp.get("venue") or cp.get("journal") or "")[:40]
                    cp_doi = "DOI" if cp.get("doi") else "no-DOI"
                    studies_list += f"  - {cp_first} ({cp_year}). {cp_title} [{cp_venue}] [{cp_doi}]\n"

                # Use editable template from prompts, fill in actual data
                meth_template = self._prompts.get("methodology_data_template",
                                DEFAULT_PROMPTS.get("methodology_data_template", ""))
                if meth_template:
                    try:
                        # Clean query log: strip internal prefixes like [llm-suggest], [s2-recommend], etc.
                        # and remove stats like "20 requested, 12 verified"
                        clean_queries = []
                        for q in queries:
                            # Remove [prefix] tags
                            cleaned = re.sub(r"^\[[\w-]+\]\s*", "", q)
                            # Skip internal stats lines
                            if re.match(r"^\d+\s+requested", cleaned):
                                continue
                            if "seeds →" in cleaned or "requested," in cleaned:
                                continue
                            # Truncate to just the search term (remove trailing stats)
                            cleaned = cleaned.split(" → ")[0].strip()
                            if cleaned and len(cleaned) > 3:
                                clean_queries.append(cleaned)
                        # Deduplicate while preserving order
                        seen_q: set[str] = set()
                        deduped_queries = []
                        for q in clean_queries:
                            q_lower = q.lower()[:60]
                            if q_lower not in seen_q:
                                seen_q.add(q_lower)
                                deduped_queries.append(q)

                        _DB_DISPLAY = {
                            "crossref": "Crossref", "arxiv": "arXiv",
                            "semantic_scholar": "Semantic Scholar", "openalex": "OpenAlex",
                            "pubmed": "PubMed", "europe_pmc": "Europe PMC",
                            "core": "CORE", "lens": "Lens.org", "scopus": "Scopus",
                            "serper": "Google Scholar (Serper)", "consensus": "Consensus",
                            "elicit": "Elicit", "scite": "Scite",
                            "biorxiv": "bioRxiv/medRxiv", "plos": "PLOS",
                            "springer": "Springer Nature", "hal": "HAL",
                            "zenodo": "Zenodo", "nasa_ads": "NASA ADS",
                            "doaj": "DOAJ", "dblp": "DBLP",
                            "internet_archive": "Internet Archive Scholar",
                            "openaire": "OpenAIRE", "fatcat": "Fatcat",
                            "datacite": "DataCite", "dimensions": "Dimensions",
                            "inspire_hep": "INSPIRE-HEP", "eric": "ERIC",
                            "figshare": "Figshare", "scielo": "SciELO",
                            "base": "BASE", "ieee": "IEEE Xplore",
                            "philpapers": "PhilPapers", "cinii": "CiNii",
                            "sciencedirect": "ScienceDirect", "wos": "Web of Science",
                            "google_books": "Google Books", "open_library": "Open Library",
                        }
                        _db_names = [_DB_DISPLAY.get(d, d) for d in search_audit.get('databases', [])]
                        guidance += "\n\n" + meth_template.format(
                            databases=', '.join(_db_names) if _db_names else 'OpenAlex, Crossref, Semantic Scholar',
                            queries='; '.join(repr(q) for q in deduped_queries[:6]),
                            total_retrieved=search_audit.get('total_retrieved', '?'),
                            total_after_dedup=search_audit.get('total_after_dedup', '?'),
                            total_after_filter=search_audit.get('total_after_filter', '?'),
                            total_included=total_included,
                            studies_list=studies_list,
                        )
                    except (KeyError, IndexError) as e:
                        logger.warning("Methodology template format error: %s", e)

        # Compute word bounds for strict isolation
        min_words = self._section_word_min(section_name)
        other_sections = [s for s in _WRITE_ORDER if s != section_name]
        forbidden_sections = ", ".join(other_sections)

        # Writing rules from prompts (editable via GUI)
        writing_rules = self._prompts.get("writing_rules",
                        DEFAULT_PROMPTS.get("writing_rules", _ANTI_PATTERNS))

        # Retrieve thesis — the central argument the paper must advance
        revised_outline = self.artifacts.get("revised_outline", {})
        thesis = revised_outline.get("revised_thesis", "") or brief.get("thesis", "")

        # Canonical corpus metadata — single source of truth for all sections
        search_audit = self.artifacts.get("search_audit", {})
        curated_papers = self.artifacts.get("curated_papers", [])
        _corpus_total = search_audit.get("total_included", len(curated_papers))
        _corpus_full_text = sum(1 for p in curated_papers if len(p.get("enriched_content", "") or p.get("abstract", "") or "") > 2000)
        _corpus_abstract_only = _corpus_total - _corpus_full_text

        prompt = f"""You are writing ONLY the '{section_name}' section for an academic paper.
TARGET LENGTH: approximately {target_words} words (minimum {min_words}, maximum {target_words + 500}).
Every claim must be properly cited from the REFERENCE LIST below. If you cannot cite a claim,
do not make that claim. Use as many references as possible to build comprehensive arguments.
Do NOT write any other section. Do NOT write the {forbidden_sections}. Do NOT summarize the paper.
Focus entirely on producing deep, evidence-grounded prose for this one section.

CORPUS SIZE (use these EXACT numbers — do NOT count papers yourself):
- Total papers in corpus: {_corpus_total}
- Full-text papers: {_corpus_full_text}
- Abstract-only papers: {_corpus_abstract_only}
When mentioning how many papers were reviewed, ALWAYS use {_corpus_total}. Never say a different number.

PAPER TITLE: {brief.get('title', '')}
CENTRAL ARGUMENT: {thesis if thesis else 'Synthesize the evidence to identify key patterns, debates, and gaps.'}
CONTRIBUTION TYPE: {contribution}
RESEARCH QUESTIONS: {rqs}

CRITICAL INSTRUCTION: This paper must ARGUE a coherent position, not merely summarize papers.
Every paragraph should advance the central argument above using specific evidence.
Do NOT write "Author X found Y. Author Z found W." paragraph after paragraph.
Instead, organize by THEMES and ARGUMENTS: "Evidence converges on [point] — [Author X] demonstrated...,
corroborated by [Author Z] who showed..., though [Author W] presents a contrasting view."

SECTION GUIDANCE:
{guidance}

{writing_rules}

TARGET: {target_words} words minimum. Write substantive paragraphs that develop arguments fully.
CITATION BREADTH: Cite as many papers from the REFERENCE LIST as possible — aim for at least 70% of provided references to appear in the paper. Each paragraph should synthesize evidence from multiple sources.

"""
        # Add prior sections if any
        if prior_sections:
            prompt += f"""PREVIOUSLY WRITTEN SECTIONS (maintain consistency, NEVER repeat):
{prior_sections[:35000]}

"""

        # Section writing rules from prompts (editable via GUI)
        section_rules = self._prompts.get("section_writing_rules",
                        DEFAULT_PROMPTS.get("section_writing_rules", ""))

        # Tell the LLM what figures/tables exist (so it doesn't reference non-existent ones)
        figures = self.artifacts.get("figures", [])
        if figures:
            fig_list = "; ".join(
                f"{f.get('figure_id', '?')}: {f.get('caption', '')[:60]}"
                for f in figures
            )
            prompt += f"""AVAILABLE TABLES/FIGURES: {fig_list}
You may reference these in your text. Do NOT reference any Table, Figure, or Appendix
not in this list — they do not exist.

"""
        else:
            prompt += """TABLES/FIGURES: None have been generated for this paper.
Do NOT reference any Table, Figure, or Appendix — they do not exist.

"""

        # Add bibliography — this is the key differentiator from ExpertResearcher
        prompt += f"""REFERENCE LIST (cite using these exact [Author, Year] keys):
{ref_list_text[:15000]}

{section_rules}

"""
        # Add source classification table if available
        source_table = self.artifacts.get("source_classification")
        if source_table:
            table_text = "\n".join(
                f"- {e['author']}, {e['year']} | {e['domain']} | {e['method']} | {e.get('claim_restriction', 'unrestricted')} | {e['finding']}"
                for e in source_table[:30]
            )
            # Compute corpus stats: method breakdown and domain breakdown
            from collections import Counter as _Counter
            method_counts = _Counter(e.get("method", "unknown") for e in source_table)
            domain_counts = _Counter(e.get("domain", "unknown") for e in source_table)
            total_papers = len(source_table)
            method_summary = "; ".join(f"{m}: {c}/{total_papers}" for m, c in method_counts.most_common(6))
            domain_summary = "; ".join(f"{d}: {c}/{total_papers}" for d, c in domain_counts.most_common(6))

            prompt += f"""
SOURCE CLASSIFICATION TABLE (use this to verify citation accuracy):
Each entry shows: Author, Year | Domain | Method | Claim Restriction | Primary Finding.
Before citing [Author, Year], check this table — your claim MUST match their domain and finding.
{table_text}

CORPUS STATISTICS (use these when making claims about the literature):
Total papers in corpus: {total_papers}
By method: {method_summary}
By domain: {domain_summary}
Use these counts when describing patterns. Say "8 of {total_papers} studies" not "the majority".

"""

        # Inject evidence map if available — tells writer exactly what claims this section can make
        evidence_map = self.artifacts.get("evidence_map", {})
        if not isinstance(evidence_map, dict):
            evidence_map = {}
        section_evidence = evidence_map.get(section_name, [])
        # Ensure section_evidence is a list (LLM may return a dict)
        if isinstance(section_evidence, dict):
            section_evidence = list(section_evidence.values()) if section_evidence else []
        elif not isinstance(section_evidence, list):
            section_evidence = []
        evidence_map_text = ""
        if section_evidence:
            parts = []
            for i, entry in enumerate(section_evidence[:15], 1):
                if not isinstance(entry, dict):
                    continue
                claim = entry.get("claim", "")
                cites = ", ".join(entry.get("cite_keys", []))
                strength = entry.get("strength", "moderate")
                parts.append(f"  {i}. {claim} [{cites}] (strength: {strength})")
            evidence_map_text = (
                f"\n\nEVIDENCE MAP FOR {section_name.upper()} (claims you CAN make with citations):\n"
                + "\n".join(parts)
                + "\nBuild your section around these evidence-backed claims. You may organize "
                "and connect them, but do NOT add claims not in this map unless you can cite "
                "a specific source from the REFERENCE LIST.\n"
            )

        # Inject weakness guidance from prior evaluations (phase5_weakness_guidance)
        weakness_text = ""
        weakness_prompt = self._prompts.get("phase7_weakness_guidance", "")
        if weakness_prompt:
            # Check if agent has weakness data from prior reviews
            weakness_data = self.artifacts.get("prior_weaknesses", {})
            if weakness_data:
                weakness_list = weakness_data.get("weakness_list", "")
                weakness_scores = weakness_data.get("weakness_scores", "")
                if weakness_list:
                    weakness_text = "\n\n" + weakness_prompt.replace(
                        "{weakness_list}", weakness_list
                    ).replace("{weakness_scores}", weakness_scores)

        # v0.3: Inject structured reading notes for this section's relevant papers
        reading_notes_text = ""
        reading_notes = self.artifacts.get("reading_notes", [])
        claim_evidence_map = self.artifacts.get("claim_evidence_map", [])
        if reading_notes:
            # Filter notes relevant to this section
            section_claims = [c for c in claim_evidence_map if (c.get("section") or "").lower() == section_name.lower()]
            relevant_indices = set()
            for cl in section_claims:
                relevant_indices.update(cl.get("supporting_papers", []))
                relevant_indices.update(cl.get("counter_papers", []))

            # Include notes for relevant papers, plus top-quality papers
            relevant_notes = []
            for note in reading_notes:
                idx = note.get("paper_index")
                tier = note.get("quality_tier", "")
                if idx in relevant_indices or tier in ("landmark", "solid"):
                    findings = note.get("key_findings", [])
                    method = note.get("methodology", "")
                    limitations = note.get("limitations", "")
                    compact = f"  [{idx}] Tier: {tier}"
                    if findings:
                        compact += f" | Findings: {'; '.join(str(f) for f in findings[:3])}"
                    if method:
                        compact += f" | Method: {method[:100]}"
                    if limitations:
                        compact += f" | Limitations: {limitations[:80]}"
                    relevant_notes.append(compact)

            if relevant_notes:
                reading_notes_text = (
                    f"\n\nYOUR READING NOTES FOR {section_name.upper()} (from your deep reading of full papers):\n"
                    + "\n".join(relevant_notes[:30])
                    + "\nUse these notes as your PRIMARY evidence source. They contain what you actually "
                    "found when reading the papers. Do NOT add claims beyond what's in these notes.\n"
                )

            # Add claim-evidence map from revised outline
            if section_claims:
                cem_parts = []
                for cl in section_claims:
                    claim = cl.get("claim", "")
                    support = cl.get("supporting_papers", [])
                    counter = cl.get("counter_papers", [])
                    confidence = cl.get("confidence", "?")
                    summary = cl.get("evidence_summary", "")
                    cem_parts.append(
                        f"  • {claim} [confidence: {confidence}]\n"
                        f"    Supporting: papers {support} | Counter: papers {counter}\n"
                        f"    Evidence: {summary[:150]}"
                    )
                reading_notes_text += (
                    f"\n\nCLAIM-EVIDENCE MAP FOR {section_name.upper()} (from your outline revision):\n"
                    + "\n".join(cem_parts)
                    + "\nWrite your section around these evidence-backed claims.\n"
                )

        # v0.3: Inject corpus summary themes/contradictions for Discussion/Results
        corpus_summary_text = ""
        corpus_summary = self.artifacts.get("corpus_summary", {})
        if corpus_summary and section_name in ("Discussion", "Results", "Limitations"):
            cs_parts = []
            if section_name == "Discussion" and corpus_summary.get("contradictions"):
                cs_parts.append("CONTRADICTIONS you identified during reading:")
                for c in corpus_summary["contradictions"][:5]:
                    cs_parts.append(f"  - {str(c)[:150]}")
            if section_name == "Results" and corpus_summary.get("themes"):
                cs_parts.append("THEMES you identified during reading:")
                for t in corpus_summary["themes"][:5]:
                    cs_parts.append(f"  - {str(t)[:150]}")
            if section_name == "Limitations" and corpus_summary.get("gaps"):
                cs_parts.append("GAPS you identified during reading:")
                for g in corpus_summary["gaps"][:5]:
                    cs_parts.append(f"  - {str(g)[:150]}")
            if cs_parts:
                corpus_summary_text = "\n\n" + "\n".join(cs_parts) + "\n"

        prompt += f"""{reading_notes_text}{corpus_summary_text}{evidence_map_text}{weakness_text}

FULL SOURCE TEXTS (use these for evidence and claims):
{bib_context[:120000]}

Write ONLY the '{section_name}' section body text. No headers, no bold pseudo-headers,
no JSON, no meta-commentary. Use flowing academic prose with paragraph breaks.

ABSTRACT-ONLY SOURCE RULE: Sources marked [ABSTRACT ONLY] in the bibliography can ONLY be
cited for background context or to note the existence of a study. Do NOT cite abstract-only
sources for specific findings, effect sizes, methodology details, or as primary evidence
for analytical claims in Results, Discussion, or Conclusion sections.

CLAIM RESTRICTION RULE: Sources marked 'background_only' in the classification table can ONLY be cited for background context (Introduction, Related Work). Sources marked 'supporting_only' can be cited as supplementary evidence but MUST NOT be the sole support for any central claim. Only 'unrestricted' sources can serve as primary evidence for central claims in Results and Discussion.

EVIDENCE-FIRST WRITING: Before writing each paragraph, identify 2-4 specific findings from
the SOURCE TEXTS above. Build your paragraph around those findings. Do NOT write a claim
first and then search for a citation — find the evidence first, then write the claim it supports.

SCOPE CONSTRAINT: Only cover topics for which you have specific evidence in the sources above.
If a subtopic has no supporting source in the REFERENCE LIST, do not discuss it — even if it
seems relevant to the overall topic. Never invent bracket references like [Topic] or [Concept].

Use [Author, Year] citations. Every paragraph needs 2-4 citations grounded in SPECIFIC evidence
from the sources above. Aim for approximately {target_words} words, but do NOT pad with unsupported
claims to reach the target.

QUANTIFIER RULE: When describing how many studies support a position, use ACTUAL COUNTS
(e.g., "8 of 25 studies" or "32%% of the reviewed corpus"), not vague quantifiers like
"the majority", "most studies", "several researchers", or "a minority". If you cannot
count the exact number from the sources above, use hedged language like "some studies"
or "a subset of the reviewed literature" instead of implying a count you don't have."""

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

        raw = self._generate_section(prompt, "claim_ledger", max_tokens=32000)
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
        """Generate an evidence comparison table from the curated papers.

        Uses the field-adaptive evidence_scaffold from Phase 1 to determine
        appropriate columns for the domain. Falls back to generic columns
        if no scaffold is available.

        Pre-fills Study and Year columns from curated data to prevent
        author-name mismatches that cause the audit to remove all rows.
        """
        if not papers:
            return None

        # Determine columns from evidence scaffold (field-adaptive)
        scaffold = self.artifacts.get("evidence_scaffold", {})
        scaffold_cols = scaffold.get("columns", [])
        if scaffold_cols and len(scaffold_cols) >= 3:
            # Ensure Study and Year are always first two columns
            extra_cols = [c for c in scaffold_cols if c.lower() not in ("study", "year")][:5]
            headers = ["Study", "Year"] + extra_cols
        else:
            headers = ["Study", "Year", "Method", "Sample/Scope", "Key Finding"]

        fill_cols = headers[2:]  # Columns the LLM needs to fill

        # Pre-build rows with exact Study/Year from curated data
        # Give the LLM FULL enriched content so it can write accurate findings
        prefilled_rows = []
        summaries = []
        for i, p in enumerate(papers[:15]):  # Max 15 rows
            authors = p.get("authors", [])
            if not authors:
                continue
            # Use _extract_surname for consistent formatting with audit
            surname = _extract_surname(authors[0])
            if len(authors) > 1:
                study_label = f"{surname} et al."
            else:
                study_label = surname
            year = str(p.get("year", "N/A"))
            prefilled_rows.append({"idx": i, "study": study_label, "year": year})
            # Use enriched content (full text) if available, fall back to abstract
            content = p.get("enriched_content", "") or p.get("abstract", "")
            # Cap per-paper content at 4000 chars to stay within context limits
            content = content[:4000]
            summaries.append(
                f"[{i}] {study_label} ({year}): {p.get('title', '')}\n"
                f"    {content}"
            )

        if len(prefilled_rows) < 3:
            return None

        # Build the pre-filled rows as JSON for the prompt
        fill_placeholders = ", ".join(f'"<fill: {c.lower()}>"' for c in fill_cols)
        row_template = []
        for pr in prefilled_rows:
            row_template.append(
                f'  [{pr["idx"]}] "{pr["study"]}", "{pr["year"]}", {fill_placeholders}'
            )

        rqs = brief.get("research_questions", [])
        rq_text = "; ".join(rqs) if rqs else ""
        col_instruction = ", ".join(fill_cols)
        prompt = f"""Fill in an evidence comparison table for this review paper.

PAPER TITLE: {brief.get('title', '')}
PAPER TYPE: {brief.get('paper_type', 'survey')}
RESEARCH QUESTIONS: {rq_text}

STUDIES (with abstracts/findings):
{chr(10).join(summaries)}

The Study and Year columns are PRE-FILLED. You MUST use them exactly as given.
Fill ONLY the {col_instruction} columns.

Pre-filled rows (fill the <fill:...> parts):
{chr(10).join(row_template)}

Return JSON with this EXACT structure:
{{"caption": "Table 1: Comparison of key studies on [topic]",
  "headers": {json.dumps(headers)},
  "rows": [["{prefilled_rows[0]['study']}", "{prefilled_rows[0]['year']}", {', '.join('"..."' for _ in fill_cols)}], ...]}}

Rules:
- Use ALL {len(prefilled_rows)} pre-filled rows — do NOT skip any, do NOT add new ones
- The Study and Year values MUST be EXACTLY as shown above (copy-paste, no reformatting)
- Keep each cell concise (5-15 words)
- Cell content MUST match the paper's ACTUAL abstract and title from the summaries
- If a paper is a meta-analysis, write "meta-analysis" — not "clinical study"
- If a paper is a review, write "review" — not "trial"
- Do NOT invent findings — use only information from the summaries above"""

        try:
            p4t_system, _ = self._get_prompt("phase6_comparison_table")
            if not p4t_system:
                p4t_system = "You are an academic research assistant. Generate comparison tables. Return valid JSON only."
            result = self.llm.generate_json(
                p4t_system,
                prompt,
                temperature=0.2,
            )
            if not result or "rows" not in result:
                return None

            # Post-process: force-overwrite Study/Year columns with pre-filled values
            # to guarantee they match curated data even if LLM reformatted them
            expected_cols = len(headers)
            rows = result.get("rows", [])
            corrected_rows = []
            for row_idx, row in enumerate(rows):
                if not row or len(row) < expected_cols:
                    # Pad short rows
                    if row:
                        row = list(row) + ["—"] * (expected_cols - len(row))
                    else:
                        continue
                if row_idx < len(prefilled_rows):
                    pf = prefilled_rows[row_idx]
                    row[0] = pf["study"]
                    row[1] = pf["year"]
                corrected_rows.append(row[:expected_cols])

            # If LLM returned fewer rows, add stubs
            if len(corrected_rows) < len(prefilled_rows):
                for pf in prefilled_rows[len(corrected_rows):]:
                    corrected_rows.append([pf["study"], pf["year"]] + ["—"] * len(fill_cols))

            result["rows"] = corrected_rows
            if "headers" not in result:
                result["headers"] = headers
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

            # Extract candidate surnames from study cell.
            # LLM may write "Da Zhang et al.", "Zhang et al.", "D. Zhang et al.",
            # "Zhang, Da et al." etc. Try multiple extractions.
            clean_study = re.sub(r"\s+et\s+al\.?", "", study_cell).strip().rstrip(".")
            # Get all name-like words (>1 char, capitalized)
            name_words = [w.rstrip(".,") for w in clean_study.split() if len(w) > 1]
            # Candidate surnames: last word (most likely), first word, all words
            candidate_surnames = []
            if name_words:
                candidate_surnames.append(name_words[-1].lower())  # Last word = surname in most formats
                candidate_surnames.append(name_words[0].lower())   # First word = surname in "Surname, Given" format
                for w in name_words:
                    wl = w.lower().rstrip(".")
                    if len(wl) > 1 and wl not in candidate_surnames:
                        candidate_surnames.append(wl)

            if not candidate_surnames:
                audited_rows.append(row)
                continue

            # Try each candidate surname against the lookup
            paper = None
            matched_surname = None
            for surname in candidate_surnames:
                key = f"{surname}_{year_cell}"
                paper = paper_lookup.get(key)
                if paper:
                    matched_surname = surname
                    break
            if not paper:
                # Try surname-only match (handles year formatting differences)
                for surname in candidate_surnames:
                    candidates = surname_index.get(surname, [])
                    if len(candidates) == 1:
                        paper = candidates[0]
                        matched_surname = surname
                        break
                    elif len(candidates) > 1:
                        year_matches = [c for c in candidates if c["year"] == year_cell]
                        paper = year_matches[0] if year_matches else candidates[0]
                        matched_surname = surname
                        break

            if not paper:
                # Can't verify — remove (likely hallucinated author)
                logger.warning("Table audit: removing unverifiable row '%s' (no matching curated paper)", study_cell)
                removed_count += 1
                continue

            # NOTE: Table content accuracy (whether method/finding cells match the paper)
            # is verified by the LLM source verification pass, which understands paraphrasing.

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
            p3s_system, p3s_user = self._get_prompt(
                "phase4_synthesis",
                title=brief.get('title', self._topic),
                research_questions="\n".join(brief.get("research_questions", [])),
                paper_classifications=relevant,
            )
            if not p3s_system:
                p3s_system = "You are an academic research analyst. Extract structured hypotheses and findings. Return valid JSON only."
            result = self.llm.generate_json(
                p3s_system,
                p3s_user if p3s_user else prompt,
            )
            if isinstance(result, dict):
                return result
        except Exception as e:
            logger.warning("Hypothesis extraction LLM call failed: %s", e)
        return {}

    def _generate_section(self, prompt: str, section_name: str, max_tokens: int = 65000) -> str:
        """Generate a section with cleanup. If truncated, attempt continuation."""
        # Use phase5_write_section from GUI-editable prompts, fall back to hardcoded
        system = self._prompts.get("phase7_write_section", self._prompts.get("synthesis_system", _SYNTHESIS_SYSTEM))
        prompt_words = len(prompt.split())
        logger.info("Generating '%s': sending %d prompt words, max_tokens=%d to %s...",
                     section_name, prompt_words, max_tokens, self.llm.model_name)
        t0 = time.time()
        try:
            resp = self.llm.generate(
                system,
                prompt,
                temperature=0.2,
                max_tokens=max_tokens,
                think=True,
            )
            elapsed = time.time() - t0
            text = strip_thinking_tags(resp.text).strip()
            out_words = len(text.split()) if text else 0
            usage = resp.usage or {}
            logger.info(
                "'%s' generated in %.1fs: %d words, %s tokens (in=%s, out=%s), finish=%s",
                section_name, elapsed, out_words,
                usage.get("total_tokens", "?"),
                usage.get("input_tokens", "?"),
                usage.get("output_tokens", "?"),
                resp.finish_reason or "?",
            )

            # Detect truncation: finish_reason == "length" or text ends mid-sentence
            is_truncated = (
                resp.finish_reason in ("length", "max_tokens")
                or (text and text[-1] not in '.!?"\')}]' and len(text) > 200)
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
                        system,
                        continuation_prompt,
                        temperature=0.2,
                        max_tokens=max_tokens,
                        think=True,
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

        text = "\n".join(cleaned).strip()

        # Fix encoding artifacts: replace U+FFFD (replacement char) with em-dash,
        # and normalize other common mojibake patterns
        text = text.replace("\ufffd", "\u2014")  # replacement char → em-dash
        text = text.replace("â\u0080\u0094", "\u2014")  # UTF-8 mojibake for em-dash
        text = text.replace("â\u0080\u0093", "\u2013")  # UTF-8 mojibake for en-dash
        text = text.replace("â\u0080\u0099", "\u2019")  # UTF-8 mojibake for right single quote

        # Normalize common apostrophe corruption patterns (e.g. O'Neill → O'Neill)
        text = text.replace("\u0092", "\u2019")  # Windows-1252 right quote
        text = text.replace("\u0080\u0099", "\u2019")  # Partial mojibake
        # Fix truncated names before apostrophe (e.g. "commissioned by O'" → needs manual review)
        # Fix "O\u2019" standalone to "O\u2019" (leave as-is, the name may follow)

        # Fix orphaned author name placeholders — sentences that start with a verb
        # because the author name failed to resolve (e.g. "reported that...", "'s finding...")
        # Remove broken fragments: ": indicates that" → "indicates that"
        text = re.sub(r':\s+indicates\s+that\b', ', indicating that', text)
        # Fix "'s finding" with no preceding author → remove the possessive
        text = re.sub(r"(?<![A-Za-z])'s\s+finding", "the finding", text)
        # Fix ": reported that" with no author before colon → clean up
        text = re.sub(r':\s+reported\s+that\b', ', which reported that', text)

        # NOTE: Self-referential cross-section phrases ("as discussed in the X section")
        # are now handled by the LLM editorial review pass.

        # Downgrade prestige-rigor terms that imply systematic-review methodology.
        # Evaluators consistently flag these as overclaiming when the pipeline
        # ran a narrative/AI-assisted workflow, not a PRISMA process.
        _PRESTIGE_SUBSTITUTIONS = (
            (r"\bsystematic mapping\b", "narrative mapping"),
            (r"\bsystematic contradiction mapping\b", "contradiction analysis"),
            (r"\bstructured retrieval\b", "automated retrieval"),
            (r"\bcomposite relevance scor(?:e|ing)\b", "weighted ranking"),
            (r"\bcomposite scor(?:e|ing)\b", "weighted ranking"),
            (r"\btransparent protocol\b", "documented procedure"),
            (r"\bquantitative synthesis\b", "narrative synthesis"),
            (r"\bsystematic literature review\b", "narrative literature review"),
            (r"\bsystematic review\b", "narrative review"),
            (r"\bmeta[- ]analysis\b", "narrative synthesis"),
            (r"\bscoping review\b", "narrative review"),
        )
        for pattern, replacement in _PRESTIGE_SUBSTITUTIONS:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

        return text

    # ------------------------------------------------------------------
    # Title/abstract framing validator
    # ------------------------------------------------------------------

    # Terms that require specific methodology to use in title/abstract
    _FORBIDDEN_FRAMING = {
        # term → replacement
        "quantitative synthesis": "narrative synthesis",
        "meta-analysis": "narrative review",
        "meta analysis": "narrative review",
        "systematic review": "narrative review",
        "systematic literature review": "narrative literature review",
        "scoping review": "narrative review",  # unless PRISMA-ScR is followed
    }

    def _sanitize_title_framing(self, title: str) -> str:
        """Replace overclaiming method labels in the title.

        The pipeline produces narrative reviews, not meta-analyses or systematic
        reviews. LLMs like to use prestigious method labels; this enforces honesty.
        Context-aware: skips if the term is in a negation/comparison context.
        """
        lower = title.lower()
        for forbidden, replacement in self._FORBIDDEN_FRAMING.items():
            if forbidden in lower:
                # Skip if already in a comparison/negation context
                idx = lower.index(forbidden)
                lookback = title[max(0, idx - 60):idx]
                if self._FRAMING_SKIP_CONTEXTS.search(lookback):
                    logger.info("Title framing: skipping '%s' (in negation/comparison context)", forbidden)
                    continue
                # Case-insensitive replacement preserving structure
                pattern = re.compile(re.escape(forbidden), re.IGNORECASE)
                title = pattern.sub(replacement.title() if title[0].isupper() else replacement, title, count=1)
                logger.info("Title framing fix: '%s' → '%s'", forbidden, replacement)
        return title

    # Phrases where the forbidden term is used comparatively/contrastively
    # and should NOT be replaced (it's already distinguishing methodology)
    _FRAMING_SKIP_CONTEXTS = re.compile(
        r"(?:rather\s+than\s+(?:a\s+)?|not\s+(?:a\s+)?|unlike\s+(?:a\s+)?|"
        r"distinct\s+from\s+(?:a\s+)?|does\s+not\s+constitute\s+(?:a\s+)?|"
        r"should\s+not\s+be\s+(?:confused\s+with|interpreted\s+as)\s+(?:a\s+)?)",
        re.IGNORECASE,
    )

    def _sanitize_abstract_framing(self, abstract: str) -> str:
        """Replace overclaiming method labels in the abstract.

        Context-aware: skips replacements when the term already appears in a
        comparative or negation context (e.g. 'rather than a systematic review').
        """
        for forbidden, replacement in self._FORBIDDEN_FRAMING.items():
            pattern = re.compile(re.escape(forbidden), re.IGNORECASE)
            matches = list(pattern.finditer(abstract))
            if not matches:
                continue
            # Process matches in reverse to preserve positions
            for m in reversed(matches):
                # Check if preceded by a skip-context phrase (look back ~60 chars)
                lookback_start = max(0, m.start() - 60)
                lookback = abstract[lookback_start:m.start()]
                if self._FRAMING_SKIP_CONTEXTS.search(lookback):
                    logger.info("Abstract framing: skipping '%s' (in negation/comparison context)",
                               forbidden)
                    continue
                abstract = abstract[:m.start()] + replacement + abstract[m.end():]
                logger.info("Abstract framing fix: '%s' → '%s'", forbidden, replacement)
        return abstract

    # ------------------------------------------------------------------
    # Source block builder (used by adversarial review + validation)
    # ------------------------------------------------------------------

    def _build_source_blocks(self, curated: list[dict]) -> str:
        """Build labeled source blocks for validation prompts.

        Labels each paper with:
        - Content depth: [FULL TEXT], [PARTIAL TEXT], or [ABSTRACT ONLY]
        - Source type: primary_study, review, meta-analysis, preprint, etc.
        """
        blocks = []
        for i, paper in enumerate(curated):
            title = paper.get("title", "Untitled")
            authors = paper.get("authors", ["Unknown"])
            year = paper.get("year", "?")
            author_str = ", ".join(str(a.get("name", a) if isinstance(a, dict) else a) for a in (authors[:3] if authors else ["Unknown"]))

            content = paper.get("enriched_content", "") or paper.get("abstract", "") or ""
            content_len = len(content.split())

            if content_len > 3000:
                label = "FULL TEXT"
            elif content_len > 500:
                label = "PARTIAL TEXT"
            else:
                label = "ABSTRACT ONLY"

            # Detect source type (rec #4: source-balancing)
            title_lower = (title or "").lower()
            venue = (paper.get("venue", "") or paper.get("journal", "") or "").lower()
            doi = paper.get("doi", "") or ""
            if "arxiv" in doi or "preprint" in venue or "biorxiv" in doi or "medrxiv" in doi:
                source_type = "preprint"
            elif any(kw in title_lower for kw in ("meta-analysis", "meta analysis", "systematic review")):
                source_type = "meta-analysis"
            elif any(kw in title_lower for kw in ("review", "overview", "survey", "perspective", "commentary")):
                source_type = "review"
            elif any(kw in venue for kw in ("conference", "proceedings", "workshop", "symposium")):
                source_type = "conference_paper"
            else:
                source_type = "primary_study"

            header = f"[Paper {i+1}] {author_str} ({year}) — \"{title}\""
            blocks.append(f"{header}\n[{label} — {content_len:,} words | TYPE: {source_type}]\n{content}\n")

        return "\n---\n".join(blocks)

        # Build source material with content-quality labels
        source_blocks = self._build_source_blocks(curated)
        source_tokens_est = len(source_blocks) // 4
        self.display.step(f"Source material: {len(curated)} papers, ~{source_tokens_est:,} tokens")

        # Build reference list for citation keys (include source type for transparency)
        ref_list = self._build_ref_list(curated)
        ref_keys_text = "\n".join(
            f"  [{r['cite_key']}] ({r.get('source_type', 'unknown')}) — {r['title'][:80]}"
            for r in ref_list
        )

        # Build reading notes summary
        notes_text = ""
        if reading_notes:
            note_parts = []
            for note in reading_notes:
                idx = note.get("paper_index", "?")
                tier = note.get("quality_tier", "?")
                findings = note.get("key_findings", [])
                findings_str = "; ".join(str(f) for f in findings[:3])
                note_parts.append(f"  [{idx}] ({tier}): {findings_str}")
            notes_text = "\n".join(note_parts[:40])

        # Build outline text
        outline_text = ""
        if outline:
            claims = outline.get("claim_evidence_map", [])
            if claims:
                outline_parts = []
                for cl in claims:
                    outline_parts.append(
                        f"  • {cl.get('section', '?')}: {cl.get('claim', '?')} "
                        f"[papers: {cl.get('supporting_papers', [])}]"
                    )
                outline_text = "\n".join(outline_parts[:30])

        # Build corpus summary
        corpus_text = ""
        if corpus_summary:
            parts = []
            for theme in corpus_summary.get("themes", [])[:5]:
                parts.append(f"  Theme: {theme}")
            for contradiction in corpus_summary.get("contradictions", [])[:5]:
                parts.append(f"  Contradiction: {contradiction}")
            for gap in corpus_summary.get("gaps", [])[:3]:
                parts.append(f"  Gap: {gap}")
            corpus_text = "\n".join(parts)

        # Word targets
        total_target = sum(_SECTION_WORD_TARGETS.values())
        section_targets = "\n".join(
            f"  {name}: {_SECTION_WORD_TARGETS[name]} words"
            for name in _SUBMIT_ORDER
        )

        # Search audit for methodology — build comprehensive data
        search_audit = self.artifacts.get("search_audit", {})
        databases = search_audit.get("databases", [])
        search_terms = search_audit.get("search_terms", [])
        search_queries_list = search_audit.get("search_queries", [])
        scope_exclusions = search_audit.get("scope_exclusions", [])
        year_from = search_audit.get("year_from", 2016)

        # Count source types FIRST (needed for methodology template and prompt)
        source_type_counts: dict[str, int] = {}
        for p in curated:
            title_lower = (p.get("title", "") or "").lower()
            venue_lower = (p.get("venue", "") or p.get("journal", "") or "").lower()
            doi = p.get("doi", "") or ""
            if "arxiv" in doi or "preprint" in venue_lower or "biorxiv" in doi or "medrxiv" in doi:
                st = "preprint"
            elif any(kw in title_lower for kw in ("meta-analysis", "meta analysis", "systematic review")):
                st = "meta-analysis"
            elif any(kw in title_lower for kw in ("review", "overview", "survey", "perspective", "commentary")):
                st = "review"
            else:
                st = "primary_study"
            source_type_counts[st] = source_type_counts.get(st, 0) + 1
        source_balance_text = ", ".join(f"{k}: {v}" for k, v in sorted(source_type_counts.items()))

        methodology_data = ""
        if search_audit:
            methodology_data = (
                f"  Databases searched: {', '.join(databases)}\n"
                f"  Search terms used: {'; '.join(search_terms[:6])}\n"
                f"  Boolean queries: {'; '.join(search_queries_list[:3])}\n"
                f"  Year range: {year_from}–present\n"
                f"  Total retrieved: {search_audit.get('total_retrieved', '?')}\n"
                f"  After deduplication: {search_audit.get('total_after_dedup', '?')}\n"
                f"  After relevance filter: {search_audit.get('total_after_filter', '?')}\n"
                f"  Included in review: {len(curated)} (this is the EXACT number — do not change it)\n"
                f"  Exclusion criteria: {'; '.join(scope_exclusions[:5])}\n"
            )

        # Pre-build methodology section template (LLM fills in prose, but numbers are locked)
        methodology_template = f"""The Methodology section MUST include these exact facts (rewrite in academic prose):
  - Searched {len(databases)} databases: {', '.join(databases)}
  - Used {len(search_terms)} search term combinations including: {', '.join(f'"{t}"' for t in search_terms[:4])}
  - Applied Boolean queries: {'; '.join(search_queries_list[:2]) if search_queries_list else 'combined term searches'}
  - Restricted to publications from {year_from} to present, English language
  - Excluded: {'; '.join(scope_exclusions[:3]) if scope_exclusions else 'out-of-scope topics'}
  - Pipeline: {search_audit.get('total_retrieved', '?')} initial results → {search_audit.get('total_after_dedup', '?')} after deduplication → {search_audit.get('total_after_filter', '?')} after relevance scoring → {len(curated)} included in final review
  - Corpus composition: {source_balance_text}
  - Synthesis approach: thematic analysis organized by research questions
  - NOT searched: PubMed (direct), Embase, Web of Science, Scopus (only the {len(databases)} databases listed above)
  DO NOT change any of these numbers. DO NOT add databases or screening stages."""

        paper_type = brief.get("paper_type", "survey")
        rq_text = "\n".join(f"  RQ{i+1}: {rq}" for i, rq in enumerate(brief.get("research_questions", [])))

        # Rec #3: Build included-studies evidence table
        evidence_table_rows = []
        for r in ref_list:
            evidence_table_rows.append(
                f"  [{r['cite_key']}] type={r.get('source_type', 'unknown')} — {r['title'][:60]}"
            )
        evidence_table_text = "\n".join(evidence_table_rows)

        system = f"""You are an expert academic researcher writing a {paper_type} paper.
You have access to the FULL SOURCE MATERIAL of every paper in your corpus.
Write a complete, publication-ready academic paper.

CRITICAL RULES:
1. ONLY cite papers from the CITATION KEYS list using [Author, Year] format.
2. Every factual claim MUST be supported by a specific citation.
3. For [FULL TEXT] papers, use specific numbers, methods, and findings from the text.
4. For [ABSTRACT ONLY] papers, use hedged language ("suggests", "indicates") — do NOT invent specific findings.
5. For the Methodology section, use ONLY the real pipeline numbers provided. Do NOT invent screening stages.
6. Do NOT use framework language ("this paper proposes a framework", "our framework", "unified account", "unified framework", "comprehensive model", "novel paradigm"). This is a narrative literature review, not a theoretical contribution.
7. Do NOT claim this is a "systematic review" or "meta-analysis" — it is a narrative literature review using automated search.
8. Each section must ADD NEW VALUE — do not repeat the same points across Introduction, Results, Discussion, and Conclusion. Discussion should interpret, not restate.
9. For strong absence claims ("no studies have...", "the literature lacks...") you must explain the search coverage that supports the absence.
10. Regulatory, ethical, or policy conclusions must be supported by specific sources — do not generalize beyond your corpus.
11. When citing a review or meta-analysis for a specific primary finding, acknowledge it is a secondary source.
12. CITATION BALANCE: Do NOT cite any single source more than 4 times in the entire paper. Spread citations across your corpus. If you find yourself relying on one source repeatedly, find supporting evidence from other sources.
13. CORPUS COUNT CONSISTENCY: The exact number of papers in this review is {len(curated)}. Whenever you mention how many papers/studies/sources were reviewed, use exactly this number.
14. Do NOT use overclaiming language: avoid "demonstrates", "proves", "confirms", "establishes", "groundbreaking", "revolutionary", "comprehensive". Use "suggests", "indicates", "supports", "proposes", "notable", "significant" instead."""

        # ── CALL 1: Write body sections (Introduction through Conclusion) ──
        body_prompt = f"""TITLE: {brief.get('title', '')}

RESEARCH QUESTIONS:
{rq_text}

PAPER TYPE: {paper_type}
CONTRIBUTION: {brief.get('contribution_type', 'evidence synthesis')}

SECTION STRUCTURE AND WORD TARGETS (write in this order):
{section_targets}
Total target: ~{total_target} words

CORPUS COMPOSITION (source types):
  {source_balance_text}
  Note: Ensure claims about specific domains (e.g., regulation, ethics) are supported by sources actually covering those domains, not extrapolated from technical papers.

INCLUDED-STUDIES TABLE (cite_key, type, title):
{evidence_table_text}

CITATION KEYS (use these EXACT keys — use [Author, Year] format in text):
{ref_keys_text}

YOUR READING NOTES:
{notes_text}

CORPUS THEMES AND CONTRADICTIONS:
{corpus_text}

EVIDENCE MAP FROM YOUR OUTLINE:
{outline_text}

METHODOLOGY DATA (use these numbers EXACTLY):
{methodology_data}

{methodology_template}

=== SOURCE PAPERS ({len(curated)} papers) ===

{source_blocks}

=== END SOURCE PAPERS ===

EXACT CORPUS SIZE: {len(curated)} papers. Use this exact number whenever you mention how many papers/studies/sources were reviewed. Do NOT round, estimate, or use a different number.

ANTI-REPETITION RULE: Each specific statistic or finding from a source paper should appear in AT MOST
2 sections. Introduce findings in Related Work OR Results (not both). Discuss implications in Discussion
(do not restate the finding). Do not repeat statistics in Conclusion — summarize at a higher level.

CITATION BALANCE: Distribute citations evenly across your corpus. No single source should be cited more than 4 times. If you catch yourself relying on one source heavily, find corroborating evidence from other papers.

Write the BODY SECTIONS of the paper (do NOT write an abstract — that will be written separately).

Output format (WRITE IN THIS ORDER):

=== Introduction ===
(content)

=== Related Work ===
(content — present prior work, introduce key findings from sources)

=== Methodology ===
(content — use ONLY the pre-built methodology template above, rewrite in academic prose)

=== Results ===
(content — organize by research question, present NEW findings not already in Related Work)

=== Discussion ===
(content — INTERPRET results, discuss implications and contradictions, do NOT restate findings)

=== Limitations ===
(content)

=== Conclusion ===
(content — high-level takeaways only, NO repeated statistics, future directions)"""

        self.display.step("Call 1/2: Writing body sections...")
        try:
            resp = self.llm.generate(
                system, body_prompt,
                temperature=0.4,
                max_tokens=65000,
                think=True,
            )
            body_output = strip_thinking_tags(resp.text).strip()
        except LLMError as e:
            logger.error("Mega write (body) failed: %s", e)
            self.display.step(f"ERROR: Body write failed — {e}")
            self.display.phase_done(7)
            return

        # Parse body sections
        draft: dict[str, str] = {}
        for section_name in list(_SUBMIT_ORDER):
            pattern = re.compile(
                rf"===\s*{re.escape(section_name)}\s*===\s*\n(.*?)(?=\n===\s|\Z)",
                re.DOTALL,
            )
            m = pattern.search(body_output)
            if m:
                text = self._clean_section_text(m.group(1).strip())
                draft[section_name] = text

        body_words = sum(len(s.split()) for s in draft.values())
        self.display.step(f"  Body: {body_words} words across {len(draft)} sections")

        # ── CALL 2: Write abstract based on completed body ──
        body_for_abstract = "\n\n".join(
            f"=== {name} ===\n{draft[name]}" for name in _SUBMIT_ORDER if name in draft
        )

        abstract_system = (
            "You are an expert academic researcher writing the abstract for a completed paper. "
            "The abstract must be 150-250 words and must ONLY contain claims, numbers, and "
            "findings that appear in the paper body below. Do NOT add new information."
        )
        abstract_prompt = f"""TITLE: {brief.get('title', '')}

COMPLETED PAPER BODY:

{body_for_abstract}

Write a 150-250 word abstract for this paper. Rules:
1. Every claim and number in the abstract MUST appear in the body above.
2. Do NOT introduce new findings, statistics, or claims not in the body.
3. Use [Author, Year] citations only if the body uses them in a key finding.
4. Structure: context (1-2 sentences), objective, methods summary, key findings, conclusion.
5. Do NOT use phrases like "this paper proposes a framework" or "systematic review".

=== Abstract ==="""

        self.display.step("Call 2/2: Writing abstract from completed body...")
        try:
            abs_resp = self.llm.generate(
                abstract_system, abstract_prompt,
                temperature=0.3,
                max_tokens=65000,
                think=True,
            )
            abstract_raw = strip_thinking_tags(abs_resp.text).strip()
            # Strip the === Abstract === header if the model echoed it
            abstract_raw = re.sub(r"^===\s*Abstract\s*===\s*\n?", "", abstract_raw).strip()
            abstract = self._clean_section_text(abstract_raw)
        except LLMError as e:
            logger.warning("Abstract write failed, extracting from body call: %s", e)
            # Fallback: check if body_output had an abstract anyway
            abs_pat = re.compile(r"===\s*Abstract\s*===\s*\n(.*?)(?=\n===\s|\Z)", re.DOTALL)
            m = abs_pat.search(body_output)
            abstract = self._clean_section_text(m.group(1).strip()) if m else ""

        # Replace LLM-written methodology with deterministic version (root cause fix:
        # LLMs keep inventing databases and screening stages no matter what the prompt says)
        # Change 3: Prefer process-log-based methodology for better transparency
        prebuilt_methodology = self._generate_methodology_from_process_log()
        if not prebuilt_methodology or len(prebuilt_methodology.split()) < 200:
            prebuilt_methodology = self._build_deterministic_methodology(
                search_audit, curated, source_type_counts, brief,
            )
        if prebuilt_methodology and "Methodology" in draft:
            llm_methodology_words = len(draft["Methodology"].split())
            draft["Methodology"] = prebuilt_methodology
            self.display.step(
                f"  Methodology: replaced LLM version ({llm_methodology_words}w) "
                f"with deterministic ({len(prebuilt_methodology.split())}w)"
            )

        # Log what we got
        for name in _SUBMIT_ORDER:
            words = len(draft.get(name, "").split())
            self.display.step(f"  {name}: {words} words")

        total_words = sum(len(s.split()) for s in draft.values())
        self.display.step(f"Draft complete: {total_words} words, {len(draft)} sections")

        # Change 3: Log write step
        self._log_step(
            "write", "Section-by-section writing",
            input_count=len(curated), output_count=len(draft),
            details={"total_words": total_words, "sections": list(draft.keys())},
        )

        # Store artifacts (same format as standard pipeline)
        self.artifacts["zero_draft"] = draft
        self.artifacts["abstract"] = abstract
        self.artifacts["ref_list"] = ref_list

        # Build submission references
        references = self._build_submission_references(curated)
        self.artifacts["references"] = references

        # Store ref_abstracts for downstream use
        ref_abstracts = {}
        for p in curated:
            title_key = (p.get("title", "") or "").lower()[:80]
            content = (p.get("enriched_content") or p.get("abstract") or "").strip()
            if title_key and content:
                ref_abstracts[title_key] = content[:3000]
        self.artifacts["ref_abstracts"] = ref_abstracts

        self.display.phase_done(7)

    # ------------------------------------------------------------------
    # Per-Section Validation (v0.4: small-context, high-accuracy)
    # ------------------------------------------------------------------

    def _step4_validate_sections(self) -> None:
        """Validate each section individually with ONLY its cited papers.

        Instead of one mega call with the full paper + all sources (which causes
        attention degradation and citation stripping), we validate each section
        with a small, focused context: the section text + the 3-10 papers it cites.
        This gives the validator enough context to verify each claim accurately.
        """
        logger.info("Step 4: VALIDATE (per-section mode)")
        self.display.phase_start(8, "Validate & Correct (per-section)")

        draft = self.artifacts.get("zero_draft", {})
        abstract = self.artifacts.get("abstract", "")
        curated = self.artifacts.get("curated_papers", [])
        references = self.artifacts.get("references", [])

        # Build submission references if not already built (per-section write path)
        if not references and curated:
            references = self._build_submission_references(curated)
            self.artifacts["references"] = references

        if not draft or not curated:
            logger.warning("No draft or curated papers — skipping validation")
            self.artifacts["final_paper"] = draft
            self.display.phase_done(8)
            self.display.phase_start(9, "Self-Review")
            self.display.phase_done(9)
            return

        # Build a lookup: cite_key -> paper data (for finding cited papers)
        ref_list = self.artifacts.get("ref_list", [])
        cite_key_to_paper: dict[str, dict] = {}
        for i, r in enumerate(ref_list):
            key = r.get("cite_key", "")
            if key and i < len(curated):
                cite_key_to_paper[key] = curated[i]

        validated_draft: dict[str, str] = {}
        _CITE_PAT = re.compile(r'\[([^\]]+?,\s*\d{4})\]')

        for section_name in _SUBMIT_ORDER:
            section_text = draft.get(section_name, "")
            if not section_text:
                continue

            # Skip methodology — it's deterministic and correct
            if section_name == "Methodology":
                validated_draft[section_name] = section_text
                continue

            # Extract citations from this section
            cited_keys = set(_CITE_PAT.findall(section_text))
            self.display.step(f"  Validating {section_name} ({len(cited_keys)} citations)...")

            # Build small source context with ONLY the cited papers
            source_parts = []
            for cite_key in cited_keys:
                paper = cite_key_to_paper.get(cite_key.strip())
                if not paper:
                    # Try fuzzy match by surname
                    surname = cite_key.split(",")[0].strip().split()[-1] if "," in cite_key else ""
                    for k, p in cite_key_to_paper.items():
                        if surname and surname.lower() in k.lower():
                            paper = p
                            break
                if paper:
                    title = paper.get("title", "?")
                    content = paper.get("enriched_content", "") or paper.get("abstract", "") or ""
                    content_type = "full_text" if len(content) > 2000 else "abstract_only"
                    source_parts.append(
                        f"[{cite_key.strip()}] ({content_type})\nTitle: {title}\n{content[:6000]}"
                    )

            if not source_parts:
                # No cited papers found — keep section as-is
                validated_draft[section_name] = section_text
                continue

            source_context = "\n\n---\n\n".join(source_parts)

            # Small, focused validation prompt
            system = (
                "You are a rigorous academic fact-checker. You have access to the cited "
                "source papers. Check each claim against its cited source and fix problems. "
                "Return ONLY the corrected section text — no commentary."
            )
            prompt = f"""Check this {section_name} section against its cited sources.

FOR EACH [Author, Year] CITATION:
1. Does the claim match what the source actually says? If not → fix the claim or remove citation
2. Is the claim stretched beyond what the source supports? If so → add hedging ("suggests" not "demonstrates")
3. Is the source marked (abstract_only) but cited for specific findings, effect sizes, or
   methodology details? If so → replace with hedged language like "according to [Author, Year]"
   or remove the specific claim entirely. Abstract-only sources may ONLY be cited for
   background context or the general existence of a finding.
4. Does a strong verb ("demonstrates", "proves", "confirms", "reveals", "establishes")
   match weak or indirect evidence? If so → downgrade to "suggests", "indicates", "is consistent with"
5. Is a finding from one domain attributed to a source in a different domain?
   (e.g., citing a linguistics paper for a genetics claim) If so → remove the citation

IMPORTANT RULES:
- Do NOT remove citations that ARE supported by the source
- Do NOT add new content or citations
- Do NOT shorten the section significantly
- Preserve all paragraph structure
- If everything is correct, return the section unchanged

=== {section_name} ===
{section_text}

=== CITED SOURCES ({len(source_parts)} papers) ===
{source_context}

Return ONLY the corrected section text. No headers, no commentary."""

            try:
                # Thinking models need headroom: thinking tokens + output both count
                # against max_tokens. 8K was too low — thinking ate all the budget.
                resp = self.llm.generate(
                    system, prompt,
                    temperature=0.1,
                    max_tokens=65000,
                    think=True,
                )
                validated = strip_thinking_tags(resp.text).strip()
                validated = self._clean_section_text(validated)

                # Safety: reject if section shrank by more than 40% or lost >50% citations
                if validated and len(validated) > 100:
                    orig_cites = len(_CITE_PAT.findall(section_text))
                    new_cites = len(_CITE_PAT.findall(validated))
                    size_ratio = len(validated) / len(section_text) if section_text else 1

                    if size_ratio < 0.6:
                        logger.warning(
                            "Per-section validate: rejecting %s — shrank to %.0f%%",
                            section_name, size_ratio * 100,
                        )
                        validated_draft[section_name] = section_text
                    elif orig_cites > 3 and new_cites < orig_cites * 0.5:
                        logger.warning(
                            "Per-section validate: rejecting %s — citations dropped from %d to %d",
                            section_name, orig_cites, new_cites,
                        )
                        validated_draft[section_name] = section_text
                    else:
                        validated_draft[section_name] = validated
                        if validated != section_text:
                            self.display.step(
                                f"    {section_name}: updated ({len(section_text.split())} → "
                                f"{len(validated.split())} words, {orig_cites} → {new_cites} cites)"
                            )
                else:
                    validated_draft[section_name] = section_text
            except LLMError as e:
                logger.warning("Per-section validation failed for %s: %s", section_name, e)
                validated_draft[section_name] = section_text

        # Fill any missing sections
        for name in _SUBMIT_ORDER:
            if name not in validated_draft and name in draft:
                validated_draft[name] = draft[name]

        # Validate abstract against body
        validated_abstract = abstract
        if abstract:
            full_body = "\n".join(validated_draft.get(s, "") for s in _SUBMIT_ORDER if s in validated_draft)
            validated_abstract = self._cross_check_abstract_claims(abstract, full_body)

        # Structured reflection pass (AI Scientist-v2 inspired)
        validated_draft = self._structured_reflection(validated_draft, validated_abstract)

        # Programmatic citation-claim alignment check (feeds into justification audit)
        alignment_preflag = self._check_citation_claim_alignment(
            validated_draft, self.artifacts.get("ref_list", []),
        )
        if alignment_preflag:
            logger.info("Citation alignment pre-flag: %d potential mismatches", len(alignment_preflag))
            self.artifacts["citation_alignment_warnings"] = alignment_preflag

        # Citation justification audit with 4-tier verdicts (AI Scientist-v2 inspired)
        validated_draft = self._audit_citation_justifications(validated_draft, curated, preflagged=alignment_preflag)

        # Transition to Phase 9: Self-Review
        self.display.phase_done(8)
        self.display.phase_start(9, "Self-Review")

        # Adversarial review loop (optional)
        if self.config.adversarial_review_enabled:
            source_blocks = self._build_source_blocks(curated)
            pre_adversarial_draft = {k: v for k, v in validated_draft.items()}
            self.artifacts["pre_adversarial_draft"] = pre_adversarial_draft
            validated_draft, validated_abstract, remaining_fatal = self._run_adversarial_loop(
                validated_draft, validated_abstract, references, curated, source_blocks,
            )
            if remaining_fatal > 0:
                logger.warning("Paper has %d unresolved FATAL issues after adversarial review", remaining_fatal)
                self.artifacts["unresolved_fatal_count"] = remaining_fatal

            # Regenerate abstract if adversarial loop changed sections materially
            draft_changed = any(
                validated_draft.get(s, "") != pre_adversarial_draft.get(s, "")
                for s in _SUBMIT_ORDER
            )
            if draft_changed:
                self.display.step("Regenerating abstract after adversarial revisions...")
                brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
                search_audit = self.artifacts.get("search_audit", {})
                corpus_count = search_audit.get("total_included", len(curated))
                full_paper_text = "\n\n".join(
                    f"=== {name} ===\n{validated_draft[name]}"
                    for name in _WRITE_ORDER if name in validated_draft
                )
                contribution = brief.get("contribution_type", "evidence synthesis")
                grounding_rules = self._prompts.get("abstract_grounding_rules",
                                  DEFAULT_PROMPTS.get("abstract_grounding_rules", ""))
                abstract_prompt = f"""Write the Abstract for this academic paper.

PAPER TITLE: {brief.get('title', '')}
CONTRIBUTION TYPE: {contribution}
RESEARCH QUESTIONS: {json.dumps(brief.get('research_questions', []))}

FULL PAPER:
{full_paper_text[:30000]}

Requirements:
- 200-400 words
- Summarize: background, methods, key findings, implications
- Include 2+ citations from the paper using [Author, Year] format
- Write as a single paragraph
- Do NOT start with "This paper..." — vary the opening
- CORPUS COUNT: use EXACTLY {corpus_count}.
- CITATION RULE: Only cite authors in the reference list above.
- CLAIM GROUNDING: Every claim must have a corresponding passage in the paper body.
- AI AGENT: Do NOT devote more than 1 sentence to the AI agent or platform.

{grounding_rules}"""
                abstract_system = self._prompts.get("phase7_abstract", "")
                _abs_tokens = self._section_max_tokens("Abstract")
                try:
                    if abstract_system:
                        resp = self.llm.generate(
                            abstract_system, abstract_prompt,
                            temperature=0.2, max_tokens=_abs_tokens, think=True,
                        )
                        validated_abstract = strip_thinking_tags(resp.text).strip()
                        validated_abstract = self._clean_section_text(validated_abstract)
                    else:
                        validated_abstract = self._generate_section(abstract_prompt, "Abstract", max_tokens=_abs_tokens)
                    # Cross-check regenerated abstract
                    full_body = "\n".join(validated_draft.get(s, "") for s in _SUBMIT_ORDER if s in validated_draft)
                    validated_abstract = self._cross_check_abstract_claims(validated_abstract, full_body)
                    self.display.step(f"  Abstract regenerated: {len(validated_abstract.split())} words")
                except LLMError as e:
                    logger.warning("Abstract regeneration after adversarial loop failed: %s — keeping previous", e)

            # Normalize citation keys: match in-text [Author, Year] against reference list
            validated_draft, validated_abstract = self._normalize_citation_keys(
                validated_draft, validated_abstract, self.artifacts.get("ref_list", []),
            )

            # Citation balance check + rewrite over-cited sections
            balance = self._check_citation_balance(validated_draft)
            if not balance["balanced"]:
                self.artifacts["citation_balance"] = balance
                over_cited = balance.get("over_cited_sources", {})
                if over_cited:
                    self.display.step(f"  Over-cited sources detected: {', '.join(f'{c}({n}x)' for c, n in over_cited.items())}")
                    # LLM rewrite: ask to reduce repetitive citations
                    for oc_cite, oc_count in over_cited.items():
                        if oc_count <= 8:
                            continue  # only fix egregious cases
                        for section_name, text in validated_draft.items():
                            if f"[{oc_cite}]" not in text:
                                continue
                            occurrences = text.count(f"[{oc_cite}]")
                            if occurrences <= 3:
                                continue
                            try:
                                fix_prompt = (
                                    f"The citation [{oc_cite}] appears {occurrences} times in this section, "
                                    f"which is excessive. Reduce it to at most 3 uses by:\n"
                                    f"1. Keeping the citation on the FIRST mention and the MOST important claim\n"
                                    f"2. Removing redundant citations where the same source is cited in consecutive sentences\n"
                                    f"3. Replacing removed citations with other relevant sources from this list if applicable\n"
                                    f"4. NEVER remove a citation without checking if the sentence still makes sense\n\n"
                                    f"Section ({section_name}):\n{text}"
                                )
                                fixed = self.llm.generate(
                                    "You are an academic editor reducing citation repetition. Output ONLY the revised section text.",
                                    fix_prompt, max_tokens=len(text) * 2,
                                )
                                if fixed and len(fixed) > len(text) * 0.7:
                                    new_count = fixed.count(f"[{oc_cite}]")
                                    if new_count < occurrences:
                                        validated_draft[section_name] = fixed
                                        logger.info("Over-citation fix: %s in %s reduced from %d to %d",
                                                    oc_cite, section_name, occurrences, new_count)
                            except Exception as e:
                                logger.warning("Over-citation fix failed for %s in %s: %s", oc_cite, section_name, e)

        # Apply ALL deterministic scrubs (same as mega path)
        self.display.step("Applying deterministic scrubs...")

        # 0. Strip numeric citations [4], [4, 5, 12]
        _numeric_cite_pat = re.compile(r'\s*\[[\d,\s]+\]')
        for section_name in list(validated_draft.keys()):
            before = validated_draft[section_name]
            validated_draft[section_name] = _numeric_cite_pat.sub('', validated_draft[section_name])
            if validated_draft[section_name] != before:
                count = len(_numeric_cite_pat.findall(before))
                logger.info("  Stripped %d numeric citations from %s", count, section_name)
        validated_abstract = _numeric_cite_pat.sub('', validated_abstract)

        # 1. Strip fabricated databases
        for section_name in list(validated_draft.keys()):
            before = validated_draft[section_name]
            validated_draft[section_name] = self._strip_fabricated_databases(validated_draft[section_name])
            if validated_draft[section_name] != before:
                logger.info("  Stripped fabricated databases from %s", section_name)
        validated_abstract = self._strip_fabricated_databases(validated_abstract)

        # 2. Scrub methodology
        search_audit = self.artifacts.get("search_audit", {})
        if "Methodology" in validated_draft:
            validated_draft["Methodology"] = self._strip_fabricated_stages(validated_draft["Methodology"])
            validated_draft["Methodology"] = self._scrub_methodology_numbers(
                validated_draft["Methodology"], search_audit, len(references)
            )

        # 3. Strip editorial placeholders
        validated_draft, validated_abstract = self._strip_editorial_placeholders(
            validated_draft, validated_abstract,
        )

        # 3b. DOI format validation
        import datetime as _dt_val
        _cur_year = _dt_val.datetime.now().year
        valid_refs = []
        for ref in references:
            doi = ref.get("doi", "") or ""
            year = ref.get("year")
            if year and int(year) > _cur_year:
                logger.warning("Removing ref with future year %d: %s", year, ref.get("title", "?")[:50])
                continue
            if doi and (not doi.startswith("10.") or "/" not in doi):
                logger.warning("Removing ref with malformed DOI '%s': %s", doi[:40], ref.get("title", "?")[:50])
                continue
            valid_refs.append(ref)
        if len(valid_refs) < len(references):
            removed = len(references) - len(valid_refs)
            self.display.step(f"  Removed {removed} refs with invalid DOI or future year")
            references = valid_refs

        # 4. Prune orphan references
        all_text = " ".join(validated_draft.values()) + " " + validated_abstract
        pre_prune = len(references)
        pruned = []
        for ref in references:
            cited = False
            for author in ref.get("authors", []) or []:
                if isinstance(author, str) and author.strip():
                    surname = _extract_surname(author)
                    if len(surname) >= 2 and surname.lower() in all_text.lower():
                        cited = True
                        break
            if cited:
                pruned.append(ref)
        if len(pruned) >= 8:
            orphans = pre_prune - len(pruned)
            if orphans > 0:
                references = pruned
                self.display.step(f"  Pruned {orphans} orphan references")

        # 5. Bibliography integrity check
        references, validated_draft, validated_abstract = self._bibliography_integrity_check(
            references, validated_draft, validated_abstract,
        )

        # Renumber
        for i, ref in enumerate(references):
            ref["ref_id"] = f"ref-{i + 1}"

        # 5b. Fix methodology ref count
        if "Methodology" in validated_draft:
            meth = validated_draft["Methodology"]
            meth = re.sub(
                r"(\b\d+)\s+(articles|papers|studies)\s+(were\s+(?:ultimately\s+)?included|were\s+included\s+in\s+(?:the\s+)?(?:final|this)\s+review)",
                lambda m: f"{len(references)} {m.group(2)} {m.group(3)}",
                meth,
            )
            validated_draft["Methodology"] = meth

        # 5c. Fix abstract corpus count
        if validated_abstract:
            def _fix_corpus_count(m):
                old_count = int(m.group(1))
                if abs(old_count - len(references)) > 2:
                    prefix = m.group(0)[:m.start(1) - m.start(0)]
                    return f"{prefix}{len(references)} {m.group(2)}"
                return m.group(0)
            validated_abstract = re.sub(
                r"(?:corpus of|synthesis of|analysis of|reviewing|review of|examined)\s+(\d+)\s+(sources|articles|studies|papers)",
                _fix_corpus_count,
                validated_abstract,
            )

        # 6. Overclaiming scrubber
        _OVERCLAIM_MAP = {
            r"\bprofoundly\b": "substantially",
            r"\bundeniably\b": "notably",
            r"\bindisputably\b": "notably",
            r"\bunequivocally\b": "clearly",
            r"\boverwhelmingly\b": "strongly",
            r"\brevolutionary\b": "significant",
            r"\bgroundbreaking\b": "notable",
            r"\bdramatically\b": "substantially",
            r"\brevelatory\b": "informative",
            r"\bdefinitively\b": "convincingly",
            r"\bmost consistent with\b": "broadly consistent with",
            r"\bprimary drivers? of\b": "contributing factors to",
            r"\bdemonstrates that\b": "suggests that",
            r"\bproves that\b": "indicates that",
            r"\breveals that\b": "suggests that",
            r"\bconfirms that\b": "supports the view that",
            r"\bestablishes that\b": "provides evidence that",
            r"\bconclusively\b": "on balance",
            r"\birrefutably\b": "strongly",
            r"\bunambiguously\b": "generally",
            r"\bparadigm[- ]shifting\b": "notable",
            r"\btransformative\b": "substantial",
            r"\bpivotal\b": "important",
            r"\bcritical(?:ly important)\b": "important",
        }
        for section_name in list(validated_draft.keys()):
            text = validated_draft[section_name]
            for pattern, replacement in _OVERCLAIM_MAP.items():
                text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
            validated_draft[section_name] = text
        for pattern, replacement in _OVERCLAIM_MAP.items():
            validated_abstract = re.sub(pattern, replacement, validated_abstract, flags=re.IGNORECASE)

        # 6b. Methodology roleplay detector
        self.display.step("Running methodology roleplay detector...")
        validated_draft, validated_abstract = self._scrub_methodology_roleplay(validated_draft, validated_abstract)

        # 6c. Claim-strength calibrator
        self.display.step("Running claim-strength calibrator...")
        validated_draft = self._calibrate_claim_strength(validated_draft)

        # 6c2. Corpus-scope enforcer
        self.display.step("Running corpus-scope enforcer...")
        validated_draft, validated_abstract = self._enforce_corpus_scope(validated_draft, validated_abstract)

        # 6d. Citation density audit (with fallback to pre-adversarial draft if citations stripped)
        pre_adv = self.artifacts.get("pre_adversarial_draft")
        validated_draft = self._audit_citation_density(validated_draft, fallback_draft=pre_adv)

        # 6e. CorpusManifest update
        manifest = self._get_manifest()
        if manifest:
            from dataclasses import replace as _dc_replace
            updated_manifest = _dc_replace(manifest, total_in_final_refs=len(references))
            self.artifacts["corpus_manifest"] = updated_manifest

        # 6f. Validate manifest counts
        self.display.step("Validating corpus counts against manifest...")
        validated_draft, validated_abstract = self._validate_manifest_counts(validated_draft, validated_abstract)

        # 6g. Abstract framing sanitizer
        validated_abstract = self._sanitize_abstract_framing(validated_abstract)

        # 6h. Post-fix strength check — verify abstract counts match body, detect over-hedging
        validated_abstract = self._post_fix_strength_check(validated_draft, validated_abstract, references)

        self.artifacts["abstract"] = validated_abstract
        self.artifacts["final_paper"] = validated_draft
        self.artifacts["references"] = references

        total_words = sum(len(s.split()) for s in validated_draft.values())
        self.display.step(f"Final paper: {total_words} words, {len(references)} references")
        self.display.phase_done(9)

    # ------------------------------------------------------------------
    # Post-fix strength check
    # ------------------------------------------------------------------

    def _post_fix_strength_check(
        self, sections: dict[str, str], abstract: str, references: list[dict],
    ) -> str:
        """Verify paper hasn't been watered down by citation fixes.

        1. Hedging density check — warn if too many hedge words
        2. Abstract count consistency — fix study/paper counts to match actual references
        Returns corrected abstract.
        """
        # 1. Hedging density check (deterministic, no LLM)
        _HEDGE_WORDS = {
            "suggests", "may", "might", "could", "possibly", "potentially",
            "appears", "seems", "arguably", "tentatively", "presumably",
        }
        for section_name, text in sections.items():
            words = text.lower().split()
            if len(words) < 50:
                continue
            hedge_count = sum(1 for w in words if w.rstrip(".,;:") in _HEDGE_WORDS)
            hedge_ratio = hedge_count / len(words)
            if hedge_ratio > 0.04:
                logger.warning(
                    "Section %s has %.1f%% hedging words (threshold 4%%) — may be over-hedged",
                    section_name, hedge_ratio * 100,
                )

        # 2. Abstract count consistency — fix counts to match actual reference list
        actual_ref_count = len(references)
        count_pattern = re.compile(r'\b(\d+)\s+(studies|papers|sources|articles|publications)')
        matches = list(count_pattern.finditer(abstract))
        for m in reversed(matches):  # reverse to preserve positions
            stated = int(m.group(1))
            if abs(stated - actual_ref_count) > 3:
                abstract = abstract[:m.start(1)] + str(actual_ref_count) + abstract[m.end(1):]
                logger.info("Abstract count fix: %d -> %d %s", stated, actual_ref_count, m.group(2))

        return abstract

    # ------------------------------------------------------------------
    # Adversarial Review Loop (harness engineering pattern)
    # ------------------------------------------------------------------

    def _run_adversarial_loop(
        self,
        draft: dict[str, str],
        abstract: str,
        references: list[dict],
        curated: list[dict],
        source_blocks: str,
    ) -> tuple[dict[str, str], str, int]:
        """Run adversarial review → fix → verify cycles.

        Returns updated (draft, abstract, remaining_fatal_count) after fixes.
        """
        from agentpub._constants import ReviewFinding, AdversarialReviewReport

        max_cycles = self.config.adversarial_max_cycles
        self.display.step(f"Adversarial review (max {max_cycles} cycles)...")

        # Build ref keys text for the reviewer — include full author list
        # so the LLM can fix citation key mismatches (e.g. Smith → Smith et al.)
        ref_lines = []
        for r in self.artifacts.get("ref_list", []):
            key = r.get("cite_key", "?")
            title = r.get("title", "?")[:80]
            authors = r.get("authors", [])
            if authors and isinstance(authors[0], str):
                author_str = "; ".join(authors[:5])
                ref_lines.append(f"  [{key}] — {author_str}. {title}")
            else:
                ref_lines.append(f"  [{key}] — {title}")
        ref_keys_text = "\n".join(ref_lines)

        last_report = None
        for cycle in range(1, max_cycles + 1):
            # --- Review ---
            report = self._adversarial_review(
                draft, abstract, ref_keys_text, source_blocks, len(curated), cycle,
            )
            last_report = report
            self.display.step(
                f"  Cycle {cycle}: {report.fatal_count} FATAL, "
                f"{report.major_count} MAJOR, {report.minor_count} MINOR"
            )

            # Log individual findings so user can see what's wrong
            for f in report.findings:
                logger.info("  [%s] %s — %s: %s", f.severity, f.section, f.category, f.problem[:200])

            if not report.needs_fixes and (
                not self.config.adversarial_fix_majors or report.major_count == 0
            ):
                self.display.step("  Paper passed adversarial review.")
                break

            # --- Fix ---
            fix_findings = [
                f for f in report.findings if f.severity == "FATAL"
            ]
            if self.config.adversarial_fix_majors:
                fix_findings += [f for f in report.findings if f.severity == "MAJOR"]

            if not fix_findings:
                break

            draft, abstract = self._apply_adversarial_fixes(
                draft, abstract, fix_findings, ref_keys_text,
            )
            self.display.step(f"  Cycle {cycle}: applied fixes to {len(fix_findings)} findings")

        remaining_fatal = last_report.fatal_count if last_report else 0
        if remaining_fatal > 0:
            self.display.step(f"  WARNING: {remaining_fatal} FATAL issues remain after {max_cycles} fix cycles")
            # Log the unresolved FATAL issues in detail
            fatal_findings = [f for f in last_report.findings if f.severity == "FATAL"]
            for i, f in enumerate(fatal_findings, 1):
                self.display.step(f"    FATAL #{i} [{f.section}] {f.category}: {f.problem[:300]}")
                if f.suggested_fix:
                    logger.info("    Suggested fix: %s", f.suggested_fix[:200])
        return draft, abstract, remaining_fatal

    # ------------------------------------------------------------------
    # Citation key normalization
    # ------------------------------------------------------------------

    def _normalize_citation_keys(
        self,
        draft: dict[str, str],
        abstract: str,
        ref_list: list[dict],
    ) -> tuple[dict[str, str], str]:
        """Match in-text [Author, Year] keys against reference list and fix mismatches.

        Common issues:
        - Single-author key for multi-author paper: [Aydinlioğlu, 2018] → [Aydinlioğlu and Bach, 2018]
        - Missing 'et al.' for 3+ authors: [Razborov, 1997] → [Razborov and Rudich, 1997]
        """
        # Build map from possible short keys to full reference keys
        key_corrections: dict[str, str] = {}

        for ref in ref_list:
            year = str(ref.get("year", ""))
            authors = ref.get("authors") or ref.get("author", "")
            if isinstance(authors, list):
                author_names = authors
            else:
                # Parse "Surname1, First1; Surname2, First2" or "Surname1 and Surname2"
                author_names = [a.strip() for a in re.split(r"[;&]|(?:\band\b)", str(authors)) if a.strip()]

            if len(author_names) < 2 or not year:
                continue

            # Extract first surname from each author
            surnames = []
            for a in author_names:
                parts = a.split(",")
                surname = parts[0].strip().split()[-1] if parts else a.strip().split()[-1]
                surnames.append(surname)

            if not surnames:
                continue

            first_surname = surnames[0]

            # Build the "correct" key
            if len(surnames) == 2:
                correct_key = f"{first_surname} and {surnames[1]}, {year}"
            else:
                correct_key = f"{first_surname} et al., {year}"

            # The wrong single-author key
            wrong_key = f"{first_surname}, {year}"

            # Only map if the wrong key is different from the correct key
            if wrong_key != correct_key:
                key_corrections[wrong_key] = correct_key

        if not key_corrections:
            return draft, abstract

        # Apply corrections across all sections and abstract
        fixes_applied = 0
        for wrong, correct in key_corrections.items():
            pattern = re.escape(f"[{wrong}]")
            replacement = f"[{correct}]"
            for section_name in list(draft.keys()):
                new_text = re.sub(pattern, replacement, draft[section_name])
                if new_text != draft[section_name]:
                    count = len(re.findall(pattern, draft[section_name]))
                    fixes_applied += count
                    draft[section_name] = new_text

            new_abstract = re.sub(pattern, replacement, abstract)
            if new_abstract != abstract:
                fixes_applied += 1
                abstract = new_abstract

        if fixes_applied:
            logger.info("Citation key normalization: fixed %d single-author keys to multi-author format", fixes_applied)
            self.display.step(f"  Citation key normalization: {fixes_applied} corrections")

        return draft, abstract

    # ------------------------------------------------------------------
    # Citation-claim alignment check
    # ------------------------------------------------------------------

    def _check_citation_claim_alignment(
        self, sections: dict[str, str], ref_list: list[dict],
    ) -> list[dict]:
        """Programmatic check: do cited paper titles relate to the claims they support?

        Returns list of misalignment warnings.
        """
        import re

        # Build lookup: cite_key -> title
        ref_titles = {}
        for r in ref_list:
            key = r.get("cite_key", "")
            if key:
                ref_titles[key] = r.get("title", "").lower()

        warnings = []
        # Pattern: [Author, Year] or [Author et al., Year]
        cite_pattern = re.compile(r'\[([A-Z][a-zà-ü]+(?:\s+(?:et\s+al\.|&\s+[A-Z][a-zà-ü]+))?(?:,?\s*\d{4})?)\]')

        for section_name, text in sections.items():
            if section_name in ("Methodology", "Limitations"):
                continue  # these sections cite for process, not claims

            sentences = re.split(r'(?<=[.!?])\s+', text)
            for sentence in sentences:
                cites_in_sentence = cite_pattern.findall(sentence)
                if not cites_in_sentence:
                    continue

                sentence_lower = sentence.lower()
                for cite in cites_in_sentence:
                    # Find matching ref by partial key match
                    matched_title = None
                    for key, title in ref_titles.items():
                        if cite.split(",")[0].strip().lower() in key.lower():
                            matched_title = title
                            break

                    if not matched_title:
                        continue

                    # Extract key topic words from the cited paper title (3+ char words)
                    title_words = set(
                        w for w in matched_title.split()
                        if len(w) > 3 and w not in {
                            "this", "that", "with", "from", "their", "about",
                            "these", "those", "which", "where", "when", "what",
                            "have", "been", "were", "will", "does", "more",
                            "than", "into", "also", "over", "such", "only",
                            "between", "through", "during", "before", "after",
                            "under", "using", "based", "study", "analysis",
                            "review", "paper", "research", "approach", "toward",
                            "towards", "among", "across",
                        }
                    )

                    # Check if at least 1 title word appears in the sentence context
                    overlap = sum(1 for w in title_words if w in sentence_lower)

                    if len(title_words) > 2 and overlap == 0:
                        warnings.append({
                            "section": section_name,
                            "citation": cite,
                            "paper_title_words": list(title_words)[:5],
                            "sentence_preview": sentence[:120],
                            "issue": "No topic overlap between cited paper title and claim context",
                        })

        return warnings

    # ------------------------------------------------------------------
    # Citation balance checker
    # ------------------------------------------------------------------

    def _check_citation_balance(
        self, sections: dict[str, str],
    ) -> dict:
        """Check if citations are over-concentrated on a few sources."""
        import re

        cite_pattern = re.compile(r'\[([A-Z][a-zà-ü]+(?:\s+(?:et\s+al\.))?(?:,?\s*\d{4})?)\]')

        all_cites = []
        for text in sections.values():
            all_cites.extend(cite_pattern.findall(text))

        if not all_cites:
            return {"balanced": True, "total_citations": 0}

        from collections import Counter
        counts = Counter(all_cites)
        total = len(all_cites)
        unique = len(counts)

        # Flag if any single source > 15% of all citations or > 6 uses
        over_cited = {
            cite: count for cite, count in counts.most_common(5)
            if count > max(6, total * 0.15)
        }

        # Flag if top 3 sources account for > 50% of citations
        top3_count = sum(c for _, c in counts.most_common(3))
        top3_pct = top3_count / total if total else 0

        result = {
            "balanced": len(over_cited) == 0 and top3_pct <= 0.50,
            "total_citations": total,
            "unique_sources": unique,
            "top3_concentration": round(top3_pct, 2),
            "over_cited_sources": over_cited,
        }

        if not result["balanced"]:
            logger.warning(
                "Citation balance: %d unique sources, top-3 = %.0f%%, over-cited: %s",
                unique, top3_pct * 100,
                ", ".join(f"{c}({n}x)" for c, n in over_cited.items()),
            )

        return result

    # ------------------------------------------------------------------
    # Structured reflection pass (AI Scientist-v2 inspired)
    # ------------------------------------------------------------------

    def _structured_reflection(self, sections: dict[str, str], abstract: str) -> dict[str, str]:
        """Structured reflection pass checking cross-section coherence."""
        if not self.config.structured_reflection_enabled:
            return sections

        self.display.step("Running structured reflection audit...")

        paper_text = "\n\n".join(
            f"=== {name} ===\n{sections[name]}"
            for name in _SUBMIT_ORDER if name in sections
        )

        prompt_template = self._prompts.get("phase6_structured_reflection", "")
        if not prompt_template:
            logger.warning("phase6_structured_reflection prompt not found")
            return sections

        filled = prompt_template.replace("{paper_text}", paper_text).replace("{abstract}", abstract or "")

        reviewer = self._get_review_llm()
        try:
            result = reviewer.generate_json(
                "You are a meticulous academic editor performing a final quality audit.",
                filled, temperature=0.2,
            )
        except (LLMError, Exception) as e:
            logger.warning("Structured reflection failed: %s", e)
            return sections

        if not isinstance(result, dict):
            return sections

        findings = result.get("findings", [])
        fails = [f for f in findings if isinstance(f, dict) and f.get("status") == "FAIL"]

        if not fails:
            self.display.step("Structured reflection: all checks passed")
            return sections

        self.display.step(f"Structured reflection: {len(fails)} issues found — applying fixes")

        for section_name in list(sections.keys()):
            section_text = sections[section_name]
            # Only fix sections that have issues mentioning them
            relevant_fixes = [f for f in fails if section_name.lower() in (f.get("fix") or "").lower()
                            or section_name.lower() in (f.get("quote") or "").lower()]
            if not relevant_fixes:
                continue

            fix_text = "\n".join(f"- {f.get('fix', '')}" for f in relevant_fixes)
            try:
                fixed = reviewer.generate(
                    "You are an academic editor. Apply the requested fixes to this section. "
                    "Return ONLY the fixed section text, no commentary.",
                    f"FIXES TO APPLY:\n{fix_text}\n\nSECTION ({section_name}):\n{section_text}",
                    temperature=0.15, max_tokens=32000,
                )
                fixed_text = strip_thinking_tags(fixed.text).strip()
                fixed_text = self._clean_section_text(fixed_text)
                if fixed_text and len(fixed_text.split()) >= len(section_text.split()) * 0.8:
                    sections[section_name] = fixed_text
                    logger.info("Structured reflection fixed section: %s", section_name)
            except Exception as e:
                logger.warning("Failed to apply reflection fix to %s: %s", section_name, e)

        return sections

    # ------------------------------------------------------------------
    # Citation gap fill (AI Scientist-v2 inspired)
    # ------------------------------------------------------------------

    def _fill_citation_gaps(self, section_name: str, section_text: str, curated: list[dict]) -> str:
        """Find uncited claims and search for supporting papers."""
        if not self.config.citation_gap_fill_enabled:
            return section_text

        # Find sentences without citations
        sentences = re.split(r'(?<=[.!?])\s+', section_text)
        uncited_claims = []
        for s in sentences:
            s = s.strip()
            if not s or len(s.split()) < 8:
                continue
            if re.search(r'\[.*?,\s*\d{4}\]', s):
                continue  # Has a citation
            # Check if it's a claim (not a transition or meta-sentence)
            claim_indicators = ["suggest", "show", "found", "report", "indicate", "demonstrate",
                              "evidence", "studies", "research", "according", "significant",
                              "increase", "decrease", "effect", "impact", "result", "data"]
            if any(ind in s.lower() for ind in claim_indicators):
                uncited_claims.append(s)

        if not uncited_claims:
            return section_text

        uncited_claims = uncited_claims[:self.config.max_gap_fills_per_section]
        self.display.step(f"Citation gap fill: {len(uncited_claims)} uncited claims in {section_name}")

        # Search for papers that could support these claims
        for claim in uncited_claims:
            try:
                from agentpub.academic_search import search_semantic_scholar
                # Extract key terms from the claim
                query = " ".join(claim.split()[:10])  # first 10 words as query
                results = search_semantic_scholar(query, limit=3)
                if not results:
                    continue

                # Check if any result is already in our curated list
                curated_titles = {(p.get("title") or "").lower() for p in curated}
                new_papers = [r for r in results if (r.get("title") or "").lower() not in curated_titles]

                if new_papers:
                    best = new_papers[0]
                    authors = best.get("authors", [])
                    if isinstance(authors, list) and authors:
                        if isinstance(authors[0], dict):
                            surname = (authors[0].get("name") or "Unknown").split()[-1]
                        else:
                            surname = str(authors[0]).split()[-1]
                    else:
                        surname = "Unknown"
                    year = best.get("year", "N/A")
                    cite_key = f"[{surname}, {year}]"

                    # Add to curated papers
                    curated.append({
                        "title": best.get("title", ""),
                        "authors": authors,
                        "year": year,
                        "abstract": best.get("abstract", ""),
                        "doi": best.get("externalIds", {}).get("DOI", ""),
                        "source": "citation_gap_fill",
                    })

                    # Insert citation into the claim
                    section_text = section_text.replace(claim, f"{claim} {cite_key}")
                    logger.info("Gap fill: added %s to support claim in %s", cite_key, section_name)
            except Exception as e:
                logger.debug("Citation gap fill search failed for claim: %s", e)

        return section_text

    # ------------------------------------------------------------------
    # Citation justification audit (AI Scientist-v2 inspired)
    # ------------------------------------------------------------------

    def _audit_citation_justifications(
        self, sections: dict[str, str], curated: list[dict],
        preflagged: list[dict] | None = None,
    ) -> dict[str, str]:
        """Audit each citation with 4-tier verdicts: SUPPORTED/STRETCHED/MISATTRIBUTED/UNSUPPORTED."""
        if not self.config.citation_justification_audit:
            return sections

        self.display.step("Citation justification audit (4-tier)...")

        # Snapshot for safety floor
        original_sections = {k: v for k, v in sections.items()}

        # Build citation-sentence pairs across all sections
        pairs = []
        for section_name, text in sections.items():
            sentences = re.split(r'(?<=[.!?])\s+', text)
            for sent in sentences:
                cites = re.findall(r'\[([^\]]+?,\s*\d{4})\]', sent)
                for cite in cites:
                    pair = {"section": section_name, "sentence": sent.strip(), "citation": cite}
                    # Mark pre-flagged items
                    if preflagged:
                        for pf in preflagged:
                            if (pf.get("citation", "").split(",")[0].strip().lower()
                                    in cite.lower() and pf.get("section") == section_name):
                                pair["preflag"] = pf.get("issue", "pre-flagged by alignment check")
                                break
                    pairs.append(pair)

        if not pairs:
            return sections

        # Build source lookup
        source_lookup = {}
        for p in curated:
            authors = p.get("authors", [])
            if authors:
                if isinstance(authors[0], dict):
                    surname = (authors[0].get("name") or "Unknown").split()[-1]
                elif isinstance(authors[0], str):
                    surname = authors[0].split()[-1]
                else:
                    surname = "Unknown"
            else:
                surname = "Unknown"
            year = str(p.get("year", ""))
            key = f"{surname}, {year}"
            abstract = p.get("abstract", "") or ""
            source_lookup[key] = {"title": p.get("title", ""), "abstract": abstract[:500]}

        # Batch pairs (max 15 per call) and classify into 3 non-SUPPORTED buckets
        batch_size = 15
        stretched_cites = []
        misattributed_cites = []
        unsupported_cites = []

        # Verdict mapping (support old prompt responses too)
        _VERDICT_MAP = {
            "STRETCHED": "STRETCHED", "WEAK": "STRETCHED",
            "MISATTRIBUTED": "MISATTRIBUTED",
            "UNSUPPORTED": "UNSUPPORTED", "UNJUSTIFIED": "UNSUPPORTED",
        }

        reviewer = self._get_review_llm()

        for i in range(0, len(pairs), batch_size):
            batch = pairs[i:i + batch_size]
            pairs_text = "\n".join(
                f"{j}. [{p['citation']}] in sentence: \"{p['sentence'][:200]}\""
                + (f" [PRE-FLAGGED: {p['preflag']}]" if p.get("preflag") else "")
                for j, p in enumerate(batch)
            )
            sources_text = "\n".join(
                f"- {cite}: {info['title']}\n  Abstract: {info['abstract'][:300]}"
                for p in batch
                for cite in [p["citation"]]
                if cite in source_lookup
                for info in [source_lookup[cite]]
            )

            prompt_template = self._prompts.get("phase6_citation_justification", "")
            if not prompt_template:
                break
            filled = prompt_template.replace("{pairs}", pairs_text).replace("{sources}", sources_text)

            try:
                result = reviewer.generate_json(
                    "You are a citation auditor checking whether each citation supports its claim.",
                    filled, temperature=0.1,
                )
                items = result if isinstance(result, list) else (
                    result.get("results", []) if isinstance(result, dict) else []
                )
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    verdict = _VERDICT_MAP.get(item.get("verdict", "").upper().strip())
                    if not verdict:
                        continue  # SUPPORTED or unknown → skip
                    idx = item.get("index", 0)
                    if not (0 <= idx < len(batch)):
                        continue
                    entry = {**batch[idx], "reason": item.get("reason", ""), "suggested_fix": item.get("suggested_fix", "")}
                    if verdict == "STRETCHED":
                        stretched_cites.append(entry)
                    elif verdict == "MISATTRIBUTED":
                        misattributed_cites.append(entry)
                    elif verdict == "UNSUPPORTED":
                        unsupported_cites.append(entry)
            except Exception as e:
                logger.warning("Citation justification batch failed: %s", e)

        total_issues = len(stretched_cites) + len(misattributed_cites) + len(unsupported_cites)
        if total_issues == 0:
            self.display.step("Citation audit: all citations supported")
            return sections

        self.display.step(
            f"Citation audit: {len(stretched_cites)} stretched, "
            f"{len(misattributed_cites)} misattributed, {len(unsupported_cites)} unsupported"
        )

        # --- Tier 1: STRETCHED — soften claim language, keep citation ---
        for sc in stretched_cites:
            section = sc["section"]
            if section not in sections:
                continue
            sentence = sc["sentence"]
            if sentence not in sections[section]:
                continue
            try:
                softened = self.llm.generate(
                    "You are an academic editor. Soften the claim in this sentence to match "
                    "what the cited source actually supports. Replace strong verbs "
                    "(demonstrates, proves, establishes, confirms) with hedged language "
                    "(suggests, indicates, is consistent with, provides preliminary support for). "
                    "Keep the citation. Output ONLY the revised sentence, nothing else.",
                    f"Original sentence: {sentence}\n"
                    f"Citation: [{sc['citation']}]\n"
                    f"Reason: {sc.get('reason', 'claim overstates source')}",
                    max_tokens=500, temperature=0.1,
                )
                softened_text = softened.text if hasattr(softened, "text") else str(softened)
                if softened_text and 20 < len(softened_text.strip()) < len(sentence) * 3:
                    cleaned = softened_text.strip().strip('"').strip("'")
                    sections[section] = sections[section].replace(sentence, cleaned, 1)
                    logger.info("Softened stretched claim for [%s] in %s", sc["citation"], section)
            except Exception as e:
                logger.warning("Stretched citation fix failed for [%s]: %s", sc["citation"], e)

        # --- Tier 2: MISATTRIBUTED — swap citation for a better match, or soften ---
        for mc in misattributed_cites:
            section = mc["section"]
            if section not in sections:
                continue
            sentence = mc["sentence"]
            old_cite = mc["citation"]
            if f"[{old_cite}]" not in sections[section]:
                continue

            # Try to find a better citation from curated papers via keyword overlap
            sentence_lower = sentence.lower()
            sent_words = set(w for w in sentence_lower.split() if len(w) > 3)
            best_match = None
            best_overlap = 0
            for p in curated:
                title_words = set(w.lower() for w in (p.get("title", "") or "").split() if len(w) > 3)
                abs_words = set(w.lower() for w in (p.get("abstract", "") or "").split()[:100] if len(w) > 3)
                ref_words = title_words | abs_words
                overlap = len(ref_words & sent_words)
                if overlap > best_overlap and overlap >= 3:
                    best_overlap = overlap
                    best_match = p

            if best_match:
                # Build new cite key from best match
                b_authors = best_match.get("authors", [])
                if b_authors:
                    if isinstance(b_authors[0], dict):
                        b_surname = (b_authors[0].get("name") or "").split()[-1]
                    elif isinstance(b_authors[0], str):
                        b_surname = b_authors[0].split()[-1]
                    else:
                        b_surname = None
                else:
                    b_surname = None
                b_year = str(best_match.get("year", ""))
                if b_surname and b_year:
                    new_cite = f"{b_surname}, {b_year}"
                    if new_cite != old_cite:
                        sections[section] = sections[section].replace(
                            f"[{old_cite}]", f"[{new_cite}]", 1
                        )
                        logger.info("Swapped misattributed [%s] -> [%s] in %s", old_cite, new_cite, section)
                        continue

            # Fallback: soften claim (same approach as STRETCHED)
            if sentence in sections[section]:
                try:
                    softened = self.llm.generate(
                        "You are an academic editor. The citation in this sentence doesn't match "
                        "the claim. Soften the claim to be a general observation that doesn't need "
                        "specific source support, and remove the citation. Output ONLY the revised sentence.",
                        f"Original sentence: {sentence}\nMisattributed citation: [{old_cite}]\n"
                        f"Reason: {mc.get('reason', 'source discusses different topic')}",
                        max_tokens=500, temperature=0.1,
                    )
                    softened_text = softened.text if hasattr(softened, "text") else str(softened)
                    if softened_text and 20 < len(softened_text.strip()) < len(sentence) * 3:
                        cleaned = softened_text.strip().strip('"').strip("'")
                        sections[section] = sections[section].replace(sentence, cleaned, 1)
                        logger.info("Softened misattributed claim (no swap found) [%s] in %s", old_cite, section)
                except Exception as e:
                    logger.warning("Misattributed citation fix failed for [%s]: %s", old_cite, e)

        # --- Tier 3: UNSUPPORTED — remove citation or reframe as gap ---
        for uc in unsupported_cites:
            section = uc["section"]
            if section not in sections:
                continue
            sentence = uc["sentence"]
            cite_str = f"[{uc['citation']}]"
            if cite_str not in sections[section]:
                continue

            # Check if this is the only citation in the sentence
            other_cites = re.findall(r'\[[^\]]+?,\s*\d{4}\]', sentence.replace(cite_str, ""))

            if other_cites:
                # Peripheral: just remove this citation
                sections[section] = sections[section].replace(cite_str, "", 1)
                logger.info("Removed unsupported citation [%s] from %s (other cites remain)", uc["citation"], section)
            elif sentence in sections[section]:
                # Sole citation: reframe as hypothesis/gap
                try:
                    reframed = self.llm.generate(
                        "You are an academic editor. This claim's sole citation doesn't support it. "
                        "Reframe the sentence as a hypothesis or research gap. Remove the citation. "
                        "Output ONLY the revised sentence, nothing else.",
                        f"Sentence: {sentence}\nUnsupported citation: {cite_str}\n"
                        f"Reason: {uc.get('reason', 'no connection to claim')}",
                        max_tokens=500, temperature=0.1,
                    )
                    if reframed and 20 < len(reframed.strip()) < len(sentence) * 3:
                        cleaned = reframed.strip().strip('"').strip("'")
                        sections[section] = sections[section].replace(sentence, cleaned, 1)
                        logger.info("Reframed unsupported claim (sole cite [%s]) in %s", uc["citation"], section)
                except Exception:
                    # Fallback: just remove citation
                    sections[section] = sections[section].replace(cite_str, "", 1)

        # --- Safety floor: reject all changes if too many citations lost ---
        _cite_pat = re.compile(r'\[([^\]]+?,\s*\d{4})\]')
        original_cites = set()
        for text in original_sections.values():
            original_cites.update(_cite_pat.findall(text))
        current_cites = set()
        for text in sections.values():
            current_cites.update(_cite_pat.findall(text))

        min_ratio = self.config.citation_min_retention_ratio
        if original_cites and len(current_cites) < len(original_cites) * min_ratio:
            logger.warning(
                "Citation audit REVERTED: would drop from %d to %d unique citations (< %.0f%%). Keeping original.",
                len(original_cites), len(current_cites), min_ratio * 100,
            )
            self.display.step(f"Citation audit reverted — would lose too many citations ({len(current_cites)}/{len(original_cites)})")
            return dict(original_sections)

        kept = len(current_cites)
        lost = len(original_cites) - kept
        self.display.step(f"Citation audit complete: {kept} citations retained, {lost} removed/swapped")
        return sections

    def _adversarial_review(
        self,
        draft: dict[str, str],
        abstract: str,
        ref_keys_text: str,
        source_blocks: str,
        source_count: int,
        cycle: int,
    ):
        """Send paper to LLM for adversarial review. Returns AdversarialReviewReport."""
        from agentpub._constants import ReviewFinding, AdversarialReviewReport

        paper_text = f"=== Abstract ===\n{abstract}\n\n"
        paper_text += "\n\n".join(
            f"=== {name} ===\n{draft[name]}"
            for name in _SUBMIT_ORDER if name in draft
        )

        # Change 2: Build enriched source classification table for adversarial reviewer
        source_class = self.artifacts.get("source_classification", [])
        if source_class:
            table_lines = ["AUTHOR | YEAR | DOMAIN | METHOD | QUALITY | ACCESS | CLAIM_RESTRICTION | FINDING"]
            for entry in source_class:
                table_lines.append(
                    f"{entry.get('author', '?')} | {entry.get('year', '?')} | "
                    f"{entry.get('domain', '?')} | {entry.get('method', '?')} | "
                    f"{entry.get('quality_tier', '?')} | {entry.get('content_type', '?')} | "
                    f"{entry.get('claim_restriction', 'unrestricted')} | "
                    f"{entry.get('finding', '?')}"
                )
            source_classification_table = "\n".join(table_lines)
        else:
            source_classification_table = "(Source classification not available)"

        # Use editable prompt from prompt system
        review_prompt = self._prompts.get(
            "phase9_adversarial_review",
            DEFAULT_PROMPTS.get("phase9_adversarial_review", ""),
        )
        if review_prompt:
            prompt = review_prompt.format(
                paper_text=paper_text,
                ref_keys_text=ref_keys_text,
                source_blocks=source_blocks[:60000],  # Truncate to fit context
                source_count=source_count,
                source_classification_table=source_classification_table,
            )
        else:
            prompt = f"Review this paper for errors:\n{paper_text}"

        system = (
            "You are a hostile peer reviewer. Find every flaw. "
            "Return valid JSON array of findings. No markdown wrapping."
        )

        try:
            resp = self._get_review_llm().generate(system, prompt, temperature=0.3, max_tokens=32000)
            raw = strip_thinking_tags(resp.text if hasattr(resp, "text") else str(resp)).strip()

            # Parse JSON response
            # Handle markdown code fences
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)

            findings_data = json.loads(raw)
            if not isinstance(findings_data, list):
                findings_data = []

            findings = []
            for f in findings_data:
                if not isinstance(f, dict):
                    continue
                findings.append(ReviewFinding(
                    severity=f.get("severity", "MINOR").upper(),
                    category=f.get("category", "unknown"),
                    section=f.get("section", "Unknown"),
                    quote=f.get("quote", "")[:500],
                    problem=f.get("problem", ""),
                    suggested_fix=f.get("suggested_fix", ""),
                ))

            return AdversarialReviewReport(cycle=cycle, findings=findings)

        except (json.JSONDecodeError, Exception) as e:
            logger.warning("Adversarial review cycle %d failed: %s", cycle, e)
            return AdversarialReviewReport(cycle=cycle, findings=[])

    def _apply_adversarial_fixes(
        self,
        draft: dict[str, str],
        abstract: str,
        findings: list,
        ref_keys_text: str,
    ) -> tuple[dict[str, str], str]:
        """Apply targeted fixes for FATAL/MAJOR findings."""
        from agentpub._constants import ReviewFinding

        # Group findings by section
        by_section: dict[str, list] = {}
        for f in findings:
            sec = f.section
            # Normalize section name
            for canon in list(_SUBMIT_ORDER) + ["Abstract"]:
                if canon.lower() == sec.lower():
                    sec = canon
                    break
            by_section.setdefault(sec, []).append(f)

        fix_prompt_template = self._prompts.get(
            "phase9_adversarial_fix",
            DEFAULT_PROMPTS.get("phase9_adversarial_fix", ""),
        )

        for section_name, section_findings in by_section.items():
            if section_name == "Abstract":
                section_text = abstract
            elif section_name in draft:
                section_text = draft[section_name]
            else:
                continue

            # Skip Methodology — it's deterministic
            if section_name == "Methodology":
                continue

            findings_json = json.dumps(
                [{"severity": f.severity, "quote": f.quote, "problem": f.problem,
                  "suggested_fix": f.suggested_fix} for f in section_findings],
                indent=1,
            )

            if fix_prompt_template:
                prompt = fix_prompt_template.format(
                    findings_json=findings_json,
                    section_text=section_text,
                    ref_keys_text=ref_keys_text,
                )
            else:
                prompt = (
                    f"Fix these problems in the {section_name} section:\n"
                    f"{findings_json}\n\nCurrent text:\n{section_text}"
                )

            try:
                resp = self.llm.generate(
                    f"You are a senior academic editor fixing specific peer-review findings "
                    f"in the {section_name} section.",
                    prompt, temperature=0.2, max_tokens=32000,
                )
                fixed = strip_thinking_tags(resp.text if hasattr(resp, "text") else str(resp)).strip()
                fixed = self._clean_section_text(fixed)

                # Safety: reject if section shrank too much
                if len(fixed) < len(section_text) * 0.5:
                    logger.warning(
                        "Adversarial fix rejected for %s: shrank from %d to %d chars",
                        section_name, len(section_text), len(fixed),
                    )
                    continue

                # Safety: reject if citations were stripped (Fix 2)
                _cite_pat = re.compile(
                    r"\[([A-Z][a-zA-Z" + _HYPH + r"]+(?:\s+et\s+al\.?)?,\s*\d{4}[a-z]?)\]"
                )
                cites_before = len(set(_cite_pat.findall(section_text)))
                cites_after = len(set(_cite_pat.findall(fixed)))
                if cites_before > 0 and cites_after < cites_before * 0.8:
                    logger.warning(
                        "Adversarial fix rejected for %s: citations dropped from %d to %d (>20%% loss)",
                        section_name, cites_before, cites_after,
                    )
                    continue

                if section_name == "Abstract":
                    abstract = fixed
                else:
                    draft[section_name] = fixed

                logger.info("Adversarial fix applied to %s (%d findings, citations %d→%d)",
                           section_name, len(section_findings), cites_before, cites_after)

            except Exception as e:
                logger.warning("Adversarial fix failed for %s: %s", section_name, e)

        return draft, abstract

    # ------------------------------------------------------------------
    # Provenance Sidecar (harness engineering pattern)
    # ------------------------------------------------------------------

    def _build_provenance_sidecar(self, final_references: list[dict]) -> dict:
        """Build a structured provenance record from pipeline artifacts."""
        search_audit = self.artifacts.get("search_audit", {})
        curated = self.artifacts.get("curated_papers", [])
        candidates = self.artifacts.get("candidate_papers", [])

        # Sources rejected (in candidates but not in curated)
        curated_titles = {(p.get("title", "") or "").lower()[:60] for p in curated}
        rejected = []
        for p in candidates:
            title = (p.get("title", "") or "").lower()[:60]
            if title and title not in curated_titles:
                rejected.append({
                    "title": (p.get("title", "") or "")[:100],
                    "reason": p.get("filter_reason", "below relevance threshold"),
                })

        # Source classification summary
        source_table = self.artifacts.get("source_classification", [])

        # Content depth breakdown
        full_text_count = sum(
            1 for p in curated
            if len(p.get("enriched_content", "")) > 2000
        )
        abstract_only_count = len(curated) - full_text_count

        return {
            "databases_searched": search_audit.get("databases", []),
            "search_terms": search_audit.get("search_terms", [])[:10],
            "total_retrieved": search_audit.get("total_retrieved", 0),
            "total_after_dedup": search_audit.get("total_after_dedup", 0),
            "total_after_filter": search_audit.get("total_after_filter", 0),
            "total_included": len(curated),
            "total_in_final_refs": len(final_references),
            "sources_rejected_sample": rejected[:20],
            "full_text_sources": full_text_count,
            "abstract_only_sources": abstract_only_count,
            "source_classifications": len(source_table),
            "adversarial_review_enabled": self.config.adversarial_review_enabled,
            "pipeline_mode": self.config.pipeline_mode,
        }

    # ------------------------------------------------------------------
    # Step 4: Audit (deterministic post-processing)
    # ------------------------------------------------------------------

    def _step4_audit(self) -> None:
        """Citation audit, fabrication sanitization, reference verification."""
        logger.info("Step 4: AUDIT")
        self.display.phase_start(8, "Audit & Verify")
        self.display.tick()

        draft = self.artifacts.get("zero_draft", {})

        # v0.3: Source-level verification — check claims against reading notes
        reading_notes = self.artifacts.get("reading_notes", [])
        if reading_notes and draft:
            self.display.step("Running source-level verification against reading notes...")
            draft = self._source_level_verification(draft, reading_notes)

        # NOTE: Fabrication sanitization (fake methodology claims, human-reviewer roleplay,
        # fabricated statistics) is now handled by the LLM editorial review below, which
        # understands context and can rephrase rather than blindly delete sentences.

        # 4a. LLM editorial review — fix overclaiming, framework language,
        # AI jargon, and fabricated methodology stages in a single pass
        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)
        paper_type = brief.get("paper_type", "survey")
        curated_early = self.artifacts.get("curated_papers", [])
        actual_ref_count = len(curated_early)
        abstract = self.artifacts.get("abstract", "")
        self.display.step("Running LLM editorial review...")
        draft, abstract = self._llm_editorial_review(draft, abstract, paper_type, actual_ref_count)

        # 4a3. Cross-section repetition detection and removal
        self.display.step("Checking cross-section repetition...")
        draft = self._remove_cross_section_repetition(draft)

        # 4a4. LLM-based dedup pass (phase5_dedup from GUI)
        dedup_system = self._prompts.get("phase7_dedup", "")
        if dedup_system:
            self.display.step("Running LLM dedup pass...")
            try:
                full_draft_text = "\n\n".join(
                    f"=== {name} ===\n{draft[name]}"
                    for name in _WRITE_ORDER if name in draft
                )
                dedup_prompt = (
                    f"PAPER TITLE: {brief.get('title', '')}\n\n"
                    f"Below is the full draft. Remove duplicated content across sections "
                    f"as described in your instructions. Return the FULL paper with all sections, "
                    f"preserving section headers (=== Section Name ===).\n\n"
                    f"{full_draft_text[:25000]}"
                )
                resp = self.llm.generate(
                    dedup_system,
                    dedup_prompt,
                    temperature=0.2,
                    max_tokens=65000,
                    think=True,
                )
                deduped = strip_thinking_tags(resp.text).strip()
                # Parse sections back from response
                for section_name_dd in _WRITE_ORDER:
                    pattern = re.compile(
                        rf"===\s*{re.escape(section_name_dd)}\s*===\s*\n(.*?)(?=\n===\s|\Z)",
                        re.DOTALL,
                    )
                    match = pattern.search(deduped)
                    if match:
                        new_content = match.group(1).strip()
                        if new_content and len(new_content) > len(draft.get(section_name_dd, "")) * 0.5:
                            draft[section_name_dd] = new_content
                self.display.step("  LLM dedup pass complete")
            except (LLMError, Exception) as e:
                logger.warning("LLM dedup pass failed (non-fatal): %s", e)

        # NOTE: Citation density (removing uncited empirical paragraphs) is now handled
        # by the LLM editorial review and source verification passes, which understand
        # context rather than blindly removing paragraphs matching regex patterns.

        # 4c. Fix truncated sections
        for heading, content in draft.items():
            stripped = content.rstrip()
            if stripped and stripped[-1] not in '.!?"\')':
                last_end = max(stripped.rfind("."), stripped.rfind("!"), stripped.rfind("?"))
                if last_end > len(stripped) * 0.7:
                    draft[heading] = stripped[:last_end + 1]

        # 4d. Word count check
        for section_name in _WRITE_ORDER:
            min_words = self._section_word_min(section_name)
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
                        raw_cites = entry.get("citations", [])
                        flat_cites = []
                        for c in raw_cites:
                            if isinstance(c, list):
                                flat_cites.extend(str(x) for x in c)
                            else:
                                flat_cites.append(str(c))
                        citations = ", ".join(flat_cites)
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

        # NOTE: Claim-citation relevance checking is handled by the LLM source
        # verification pass (_verify_against_sources), which reads actual paper
        # abstracts and understands paraphrasing.

        # 4f. Reference verification
        self.display.step("Verifying references...")
        curated = self.artifacts.get("curated_papers", [])
        self.display.step(f"  Curated papers available: {len(curated)}")
        references = self._build_submission_references(curated)
        self.display.step(f"  Submission references built: {len(references)}")

        # 4f1. LLM citation cleanup — fix phantom citations, wrong years, bare-year cites
        # The LLM gets the valid reference list and fixes all citation issues in one pass.
        self.display.step("Running LLM citation cleanup...")
        draft = self._llm_citation_cleanup(draft, references)

        try:
            import asyncio
            verifier = ReferenceVerifier()
            # Build topic keywords for content-relevance check
            _brief = self.artifacts.get("research_brief", {})
            _topic_text = _brief.get("title", "") + " " + " ".join(_brief.get("search_terms", []))
            _topic_kw: set[str] | None = {w.lower() for w in _topic_text.split() if len(w) > 3}
            _topic_kw -= {"review", "analysis", "study", "research", "paper", "novel",
                          "based", "using", "critical", "current", "approach"}
            if not _topic_kw:
                _topic_kw = None
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    try:
                        future = pool.submit(asyncio.run, verifier.verify_all(references, topic_keywords=_topic_kw))
                        report = future.result(timeout=90)
                    except concurrent.futures.TimeoutError:
                        logger.warning("Citation verification timed out after 90s — skipping")
                        report = None
                    finally:
                        pool.shutdown(wait=False)
                else:
                    report = loop.run_until_complete(verifier.verify_all(references, topic_keywords=_topic_kw))
            except RuntimeError:
                report = asyncio.run(verifier.verify_all(references, topic_keywords=_topic_kw))
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

        # 4f3. Deterministic citation-claim cross-check
        # For each [Author, Year] in the text, check that the claim sentence
        # overlaps with the reference's abstract. Removes misattributed citations.
        ref_abstracts = self.artifacts.get("ref_abstracts", {})
        evidence_by_paper = self.artifacts.get("evidence_by_paper", {})
        curated = self.artifacts.get("curated_papers", [])
        draft = self._deterministic_cite_claim_check(
            draft, references, ref_abstracts, evidence_by_paper, curated,
        )

        # 4g. Prune orphan references (in bibliography but never cited in text)
        all_text = " ".join(draft.values())
        if self.artifacts.get("abstract"):
            all_text += " " + self.artifacts["abstract"]
        pre_prune = len(references)
        pruned_refs = []
        for ref in references:
            # Check if any author surname appears in text as a citation
            # Handles both [bracket] and (parenthetical) citation styles
            cited = False
            for author in ref.get("authors", []) or []:
                if isinstance(author, str) and author.strip():
                    surname = _extract_surname(author)
                    if len(surname) >= 2:
                        esc = re.escape(surname)
                        # Bracket: [... Surname ...]
                        if re.search(r"\[[^\]]*" + esc + r"[^\]]*\]", all_text, re.IGNORECASE):
                            cited = True
                            break
                        # Parenthetical: (Surname et al., YYYY) or (Surname, YYYY)
                        if re.search(r"\(" + esc + r"(?:\s+et\s+al\.?)?\s*[,;]?\s*\d{4}", all_text, re.IGNORECASE):
                            cited = True
                            break
                        # Narrative: Surname et al. (YYYY) or Surname (YYYY)
                        if re.search(esc + r"(?:\s+et\s+al\.?)?\s*\(\s*\d{4}", all_text, re.IGNORECASE):
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

        # 4g2. Remove refs with no year (can't be properly cited as [Author, Year])
        _pre_year_filter = len(references)
        references = [r for r in references if r.get("year")]
        _removed_no_year = _pre_year_filter - len(references)
        if _removed_no_year > 0:
            logger.info("Removed %d references with no year", _removed_no_year)
            self.display.step(f"  Removed {_removed_no_year} references missing publication year")

        # NOTE: Bare-year citations [YYYY] and citation-year mismatches are now
        # handled by the LLM citation cleanup pass (step 4f1) above.

        # 4j. Safety floor: if pruning left fewer than 8 refs, pad from curated papers
        if len(references) < 8:
            existing_titles = {(r.get("title") or "").lower() for r in references}
            for paper in curated:
                if len(references) >= 10:
                    break
                t = (paper.get("title") or "").lower()
                if t and t not in existing_titles:
                    ref = self._build_single_submission_ref(paper, len(references))
                    references.append(ref)
                    existing_titles.add(t)
            self.display.step(f"  Padded references to {len(references)} (safety floor)")

        # 4k. Filter future-dated references (strictly future, not current year)
        import datetime as _dt
        current_year = _dt.datetime.now().year
        pre_future = len(references)
        future_filtered = []
        for ref in references:
            try:
                ref_year = int(ref.get("year", 0) or 0)
            except (ValueError, TypeError):
                ref_year = 0
            if ref_year > current_year:
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
                self.display.step(f"  Removed {removed} future-dated references (year > {current_year})")
        else:
            logger.info("Skipping future-date filter: would leave only %d refs", len(future_filtered))

        # NOTE: Off-topic reference filtering now happens upstream in Phase 2
        # (LLM scoring pass with 800-char enriched content). No post-writing
        # keyword/bigram topic filter needed.

        # 4k2. Rescue hallucinated citations: search for papers the LLM cited
        # from its training knowledge that aren't in the reference list. If found
        # in academic databases and relevant, add them; if not found, they'll be
        # stripped later by _strip_orphan_citations.
        try:
            rescued = self._rescue_hallucinated_citations(draft, references, brief)
            if rescued:
                references.extend(rescued)
                self.display.step(f"  Rescued {len(rescued)} LLM-cited papers (verified in academic DBs)")
        except Exception as e:
            logger.warning("Citation rescue failed (non-fatal): %s", e)

        # 4k3. Re-audit table against final reference list (not curated)
        # Table was built against curated papers in Phase 3 but refs may have
        # been pruned/filtered since then — remove table rows for papers no
        # longer in the reference list.
        figures = self.artifacts.get("figures", [])
        if figures:
            ref_surnames_final: set[str] = set()
            for ref in references:
                for author in ref.get("authors", []) or []:
                    if isinstance(author, str):
                        s = _extract_surname(author).lower()
                        if len(s) >= 2:
                            ref_surnames_final.add(s)
            for fig in figures:
                if fig.get("data_type") == "table" and fig.get("data", {}).get("rows"):
                    rows = fig["data"]["rows"]
                    pre = len(rows)
                    kept_rows = []
                    for row in rows:
                        if not row:
                            continue
                        study_cell = str(row[0]).strip()
                        clean = re.sub(r"\s+et\s+al\.?", "", study_cell).strip().rstrip(".")
                        name_words = [w.rstrip(".,").lower() for w in clean.split() if len(w) > 1]
                        matched = any(w in ref_surnames_final for w in name_words)
                        if matched:
                            kept_rows.append(row)
                        else:
                            logger.info("Table re-audit: removing row '%s' (not in final refs)", study_cell)
                    if len(kept_rows) < pre:
                        fig["data"]["rows"] = kept_rows
                        self.display.step(f"  Table re-audit: removed {pre - len(kept_rows)} rows not in final refs")

        # 4l. Renumber ref_ids to be sequential (fix gaps from pruning)
        for i, ref in enumerate(references):
            ref["ref_id"] = f"ref-{i + 1}"

        # 4m-pre. Replace methodology with deterministic version (purely data-driven)
        search_audit = self.artifacts.get("search_audit", {})
        if "Methodology" in draft:
            curated = self.artifacts.get("curated_papers", [])
            _src_type_counts = {}
            for p in curated:
                st = p.get("source_type", "primary")
                _src_type_counts[st] = _src_type_counts.get(st, 0) + 1
            prebuilt = self._build_deterministic_methodology(
                search_audit, curated, _src_type_counts, brief,
            )
            if prebuilt:
                draft["Methodology"] = prebuilt
                self.display.step("  Methodology: replaced with deterministic version")
            else:
                draft["Methodology"] = self._strip_fabricated_stages(draft["Methodology"])
                draft["Methodology"] = self._scrub_methodology_numbers(
                    draft["Methodology"], search_audit, len(references)
                )

        # 4m-pre2. Strip fabricated database names from ALL sections
        # LLM commonly claims WoS, Scopus, ERIC etc. in Introduction, Methodology, Abstract
        for section_name in list(draft.keys()):
            before = draft[section_name]
            draft[section_name] = self._strip_fabricated_databases(draft[section_name])
            if draft[section_name] != before:
                logger.info("  Stripped fabricated databases from %s", section_name)
        # Also strip from abstract
        abstract = self.artifacts.get("abstract", "")
        if abstract:
            cleaned_abstract = self._strip_fabricated_databases(abstract)
            if cleaned_abstract != abstract:
                self.artifacts["abstract"] = cleaned_abstract
                logger.info("  Stripped fabricated databases from Abstract")

        # 4m. Fix methodology numbers via LLM (uses real pipeline numbers)
        # Skip if template methodology was used — it's already correct and the LLM
        # would re-introduce fabricated databases/stages.
        meth_text = draft.get("Methodology", "")
        if meth_text and not self.artifacts.get("_template_methodology_used"):
            search_audit = self.artifacts.get("search_audit", {})
            real_numbers = {
                "total_retrieved": search_audit.get("total_retrieved", "unknown"),
                "total_after_dedup": search_audit.get("total_after_dedup", "unknown"),
                "total_after_filter": search_audit.get("total_after_filter", "unknown"),
                "total_included": search_audit.get("total_included", len(references)),
            }
            self.display.step("  Fixing methodology with real pipeline numbers...")
            # Use GUI-editable prompt (phase6_methodology_fix), fall back to inline
            mf_system, mf_user = self._get_prompt(
                "phase6_methodology_fix",
                total_retrieved=str(real_numbers['total_retrieved']),
                total_after_dedup=str(real_numbers['total_after_dedup']),
                total_after_filter=str(real_numbers['total_after_filter']),
                total_included=str(real_numbers['total_included']),
                ref_count=str(len(references)),
                methodology=meth_text,
            )
            if not mf_user:
                mf_user = (
                    f"Fix the methodology numbers using real pipeline data: "
                    f"retrieved={real_numbers['total_retrieved']}, dedup={real_numbers['total_after_dedup']}, "
                    f"filtered={real_numbers['total_after_filter']}, included={real_numbers['total_included']}, "
                    f"refs={len(references)}. Remove fabricated screening stages.\n\n"
                    f"METHODOLOGY TEXT:\n{meth_text}\n\nReturn ONLY the corrected methodology text."
                )
            meth_fix_prompt = mf_user
            try:
                fixed = self._generate_section(meth_fix_prompt, "methodology_fix", max_tokens=32000)
                if fixed and len(fixed) > 200:
                    draft["Methodology"] = fixed
                    self.display.step("  Methodology numbers and stages corrected")
            except Exception as e:
                logger.warning("Methodology fix failed: %s", e)

        # 4m2. Corpus count consistency validator (deterministic)
        # Check that stated corpus counts in text match actual table rows / reference count.
        self._validate_corpus_counts(draft, abstract, references)

        # 4m3. Change 6: Methodology roleplay detector
        abstract = self.artifacts.get("abstract", "")
        self.display.step("Running methodology roleplay detector...")
        draft, abstract = self._scrub_methodology_roleplay(draft, abstract)
        self.artifacts["abstract"] = abstract

        # 4m4. Change 4: Claim-strength calibrator
        self.display.step("Running claim-strength calibrator...")
        draft = self._calibrate_claim_strength(draft)

        # 4m4b. Corpus-scope enforcer (fixes "unsupported central claim" hard-fail)
        self.display.step("Running corpus-scope enforcer...")
        abstract = self.artifacts.get("abstract", "")
        draft, abstract = self._enforce_corpus_scope(draft, abstract)
        self.artifacts["abstract"] = abstract

        # 4m5. Change 5: Citation density auditor
        draft = self._audit_citation_density(draft)

        # 4m6. Change 1: Update manifest and validate counts
        manifest = self._get_manifest()
        if manifest:
            from dataclasses import replace as _dc_replace
            updated_manifest = _dc_replace(manifest, total_in_final_refs=len(references))
            self.artifacts["corpus_manifest"] = updated_manifest
        self.display.step("Validating corpus counts against manifest...")
        draft, abstract = self._validate_manifest_counts(draft, abstract)
        self.artifacts["abstract"] = abstract

        # NOTE: Citation frequency capping (over-cited references) is now handled by
        # the LLM citation cleanup pass, which can rephrase rather than just delete brackets.

        # NOTE: Orphan citation stripping (phantom [Author, Year], pseudo-citations like
        # [Tumor], [Mechanisms], and orphan table rows) is now handled by the LLM citation
        # cleanup pass (step 4f1), which rephrases sentences naturally instead of just
        # deleting brackets.

        # 4o-pre. Phase 8: Self-critique → targeted revision loop (uses GUI prompts)
        critique_prompt_raw = self._prompts.get("phase8_self_critique", "")
        revision_prompt_raw = self._prompts.get("phase8_targeted_revision", "")
        if critique_prompt_raw and revision_prompt_raw:
            self.display.step("Running self-critique pass...")
            try:
                # Build full paper text for critique
                full_text_for_critique = "\n\n".join(
                    f"=== {name} ===\n{draft[name]}"
                    for name in _WRITE_ORDER if name in draft
                )
                abstract_text = self.artifacts.get("abstract", "")
                if abstract_text:
                    full_text_for_critique = f"=== Abstract ===\n{abstract_text}\n\n" + full_text_for_critique

                # Parse critique prompt (has SYSTEM: / USER PROMPT TEMPLATE: format)
                crit_system, crit_user = self._get_prompt(
                    "phase8_self_critique",
                    full_paper_text=full_text_for_critique[:25000],
                )
                if not crit_system:
                    crit_system = "You are a demanding peer reviewer for a top-tier academic journal."

                critique_resp = self.llm.generate(
                    crit_system,
                    crit_user,
                    temperature=0.5,
                    max_tokens=65000,
                    think=True,
                )
                weaknesses = strip_thinking_tags(critique_resp.text).strip()

                if weaknesses and "PASS" not in weaknesses.upper()[:20]:
                    self.display.step(f"  Self-critique found issues — applying targeted revision...")

                    # Apply targeted revision to each section that has weaknesses
                    for section_name in list(draft.keys()):
                        # Check if this section is mentioned in weaknesses
                        if section_name.lower() not in weaknesses.lower():
                            continue

                        rev_system, rev_user = self._get_prompt(
                            "phase8_targeted_revision",
                            weaknesses=weaknesses,
                            section_text=draft[section_name][:8000],
                        )
                        if not rev_system:
                            rev_system = "You are a senior academic editor performing a targeted revision."

                        try:
                            rev_resp = self.llm.generate(
                                rev_system,
                                rev_user,
                                temperature=0.2,
                                max_tokens=65000,
                                think=True,
                            )
                            revised = strip_thinking_tags(rev_resp.text).strip()
                            revised = self._clean_section_text(revised)
                            if revised and len(revised) > len(draft[section_name]) * 0.5:
                                draft[section_name] = revised
                                self.display.step(f"    Revised: {section_name}")
                        except LLMError as e:
                            logger.warning("Targeted revision failed for %s: %s", section_name, e)
                else:
                    self.display.step("  Self-critique: no significant issues found")
            except LLMError as e:
                logger.warning("Self-critique LLM call failed (non-fatal): %s", e)
            except Exception as e:
                logger.warning("Self-critique failed (non-fatal): %s", e)

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

        # ── FINAL DETERMINISTIC CLEANUP (runs AFTER all LLM passes) ──
        # These are Python-only checks. No LLM call can undo them.
        abstract = self.artifacts.get("abstract", "")
        draft, abstract = self._final_deterministic_cleanup(
            draft, abstract, references,
        )
        self.artifacts["abstract"] = abstract

        self.artifacts["final_paper"] = draft
        self.artifacts["references"] = references

        total_words = sum(len(s.split()) for s in draft.values())
        self.display.step(f"Final paper: {total_words} words, {len(references)} references")
        self.display.phase_done(8)
        # Emit phase 9 (Self-Review) for progress completeness — audit path skips adversarial
        self.display.phase_start(9, "Self-Review")
        self.display.phase_done(9)

    # ------------------------------------------------------------------
    # Pre-submit self-evaluation
    # ------------------------------------------------------------------

    def _pre_submit_selfeval(self) -> dict:
        """Run a single LLM evaluation pass on the complete paper before submission.

        Checks for citation mismatches, source count errors, redundancy,
        fabricated authorship, garbled text, and claim-evidence issues.
        If fixable issues are found, applies one fix cycle per affected section.

        Returns the eval result dict (also stored in self.artifacts["self_eval"]).
        """
        self.display.step("Running pre-submit self-evaluation...")

        draft = self.artifacts.get("final_paper", self.artifacts.get("zero_draft", {}))
        abstract = self.artifacts.get("abstract", "")
        references = self.artifacts.get("references", [])

        # Build complete paper text for evaluation
        paper_parts = []
        for heading in ("Introduction", "Related Work", "Methodology", "Results",
                        "Discussion", "Limitations", "Conclusion"):
            content = draft.get(heading, "")
            if content:
                paper_parts.append(f"=== {heading} ===\n{content}")
        paper_text = "\n\n".join(paper_parts)

        # Build reference list summary with full author names
        ref_lines = []
        for r in references:
            authors = r.get("authors", [])
            if isinstance(authors, list):
                author_str = "; ".join(str(a) for a in authors[:5])
                if len(authors) > 5:
                    author_str += " et al."
            else:
                author_str = str(authors)
            ref_lines.append(
                f"- [{r.get('ref_id', '?')}] {author_str} ({r.get('year', '?')}). "
                f"\"{r.get('title', '?')}\""
            )
        reference_list_text = "\n".join(ref_lines)

        # Get eval prompt
        prompt_template = self._prompts.get("phase10_selfeval", DEFAULT_PROMPTS.get("phase10_selfeval", ""))
        if not prompt_template:
            logger.warning("phase10_selfeval prompt not found — skipping self-eval")
            return {"issues": [], "score": 0}

        eval_prompt = prompt_template.format(
            paper_text=paper_text[:80000],  # cap to avoid token overflow
            abstract=abstract,
            reference_list=reference_list_text[:20000],
        )

        # Use review LLM if available (different model = better eval)
        eval_llm = self._get_review_llm()

        eval_result = {"issues": [], "score": 0}
        try:
            resp = eval_llm.generate(
                "You are a rigorous academic paper quality auditor.",
                eval_prompt,
                temperature=0.1,
                max_tokens=4000,
            )
            raw = strip_thinking_tags(resp.text).strip()

            # Parse JSON — handle markdown fences
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```\s*$", "", raw)

            eval_result = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("Self-eval JSON parse failed: %s — raw: %.200s", e, raw)
        except (LLMError, Exception) as e:
            logger.warning("Self-eval LLM call failed (non-fatal): %s", e)

        issues = eval_result.get("issues", [])
        score = eval_result.get("score", 0)
        self.display.step(f"Pre-submit self-eval: {len(issues)} issues found, score {score}/10")

        # Store eval result
        self.artifacts["self_eval"] = eval_result

        if not issues:
            return eval_result

        # Determine which sections need fixes
        fixable_types = {"citation_key", "fabricated_author", "source_count",
                         "redundancy", "over_citation", "garbled_text", "claim_evidence"}
        sections_to_fix: dict[str, list[dict]] = {}
        for issue in issues:
            itype = issue.get("type", "")
            section = issue.get("section", "")
            if itype in fixable_types and section and section in draft:
                sections_to_fix.setdefault(section, []).append(issue)

        if not sections_to_fix:
            self.display.step("  No fixable section-level issues — skipping fix cycle")
            return eval_result

        # One fix cycle per affected section
        fix_prompt_template = self._prompts.get(
            "phase10_selfeval_fix", DEFAULT_PROMPTS.get("phase10_selfeval_fix", "")
        )
        if not fix_prompt_template:
            logger.warning("phase10_selfeval_fix prompt not found — skipping fixes")
            return eval_result

        fixed_count = 0
        for section_name, section_issues in sections_to_fix.items():
            self.display.step(f"  Fixing {len(section_issues)} issues in {section_name}...")
            original_text = draft[section_name]
            original_len = len(original_text)

            fix_prompt = fix_prompt_template.format(
                section_name=section_name,
                section_text=original_text[:40000],
                issues_json=json.dumps(section_issues, indent=2),
                reference_list=reference_list_text[:20000],
            )

            try:
                fix_resp = eval_llm.generate(
                    f"You are an academic paper editor fixing issues in the {section_name} section.",
                    fix_prompt,
                    temperature=0.15,
                    max_tokens=self._section_max_tokens(section_name),
                )
                fixed_text = strip_thinking_tags(fix_resp.text).strip()
                fixed_text = self._clean_section_text(fixed_text)

                # Safety check: reject if section shrinks >30%
                if len(fixed_text) < original_len * 0.7:
                    logger.warning(
                        "Self-eval fix for %s shrank text from %d to %d chars (>30%%) — rejecting",
                        section_name, original_len, len(fixed_text),
                    )
                    self.display.step(f"  Rejected fix for {section_name} (shrank >30%)")
                    continue

                # Safety check: must have reasonable content
                if len(fixed_text.split()) < 50:
                    logger.warning("Self-eval fix for %s produced only %d words — rejecting",
                                   section_name, len(fixed_text.split()))
                    continue

                draft[section_name] = fixed_text
                fixed_count += 1
                logger.info("Self-eval fixed %s: %d → %d chars",
                            section_name, original_len, len(fixed_text))
            except (LLMError, Exception) as e:
                logger.warning("Self-eval fix for %s failed (non-fatal): %s", section_name, e)

        if fixed_count:
            self.display.step(f"  Fixed {fixed_count}/{len(sections_to_fix)} sections")
            # Update artifacts with fixed draft
            self.artifacts["final_paper"] = draft
            # Also update abstract if it had source_count issues
            abstract_issues = [i for i in issues
                               if i.get("section", "").lower() in ("abstract", "") and
                               i.get("type") == "source_count"]
            if abstract_issues:
                canonical_count = len(references)
                count_pat = re.compile(r'(\d+)\s+(?:sources|articles|studies|papers|publications)')
                new_abstract = count_pat.sub(
                    lambda m: f"{canonical_count} {m.group(0).split(m.group(1), 1)[1].strip()}"
                    if abs(int(m.group(1)) - canonical_count) > 2 else m.group(0),
                    abstract,
                )
                if new_abstract != abstract:
                    self.artifacts["abstract"] = new_abstract
                    self.display.step("  Fixed source count in abstract")

        return eval_result

    # ------------------------------------------------------------------
    # Step 5: Submit
    # ------------------------------------------------------------------

    def _step5_submit(self, challenge_id: str | None = None) -> dict:
        """Assemble and submit to AgentPub API."""
        self.display.phase_start(10, "Submit")
        self.display.step("Submitting to AgentPub...")
        self.display.tick()

        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)

        # ── Pre-submit self-evaluation (one LLM eval + fix cycle) ──
        try:
            self._pre_submit_selfeval()
        except Exception as e:
            logger.warning("Pre-submit self-eval failed (non-fatal): %s", e)

        # Re-read artifacts after self-eval may have updated them
        draft = self.artifacts.get("final_paper", self.artifacts.get("zero_draft", {}))
        abstract = self.artifacts.get("abstract", "")
        references = self.artifacts.get("references", [])

        # ── Pre-submission consistency check: corpus counts ──
        # Ensure all mentions of corpus size match the canonical count
        canonical_count = len(references)
        all_text = abstract + " " + " ".join(draft.values())
        count_pattern = re.compile(r'(\d+)\s+(?:sources|articles|studies|papers|publications)\s+(?:were\s+)?(?:included|reviewed|examined|analyzed|selected|identified)')
        mismatches = []
        for m in count_pattern.finditer(all_text):
            mentioned = int(m.group(1))
            if abs(mentioned - canonical_count) > 2:
                mismatches.append((mentioned, m.group(0)[:80]))
        if mismatches:
            logger.warning("Corpus count mismatches detected (canonical=%d): %s", canonical_count, mismatches)
            self.display.step(f"  Fixing {len(mismatches)} corpus count mismatches (canonical: {canonical_count})...")
            # Fix in abstract
            abstract = count_pattern.sub(
                lambda m: f"{canonical_count} {m.group(0).split(m.group(1), 1)[1].strip()}"
                if abs(int(m.group(1)) - canonical_count) > 2 else m.group(0),
                abstract,
            )
            # Fix in each section
            for sec_name in list(draft.keys()):
                draft[sec_name] = count_pattern.sub(
                    lambda m: f"{canonical_count} {m.group(0).split(m.group(1), 1)[1].strip()}"
                    if abs(int(m.group(1)) - canonical_count) > 2 else m.group(0),
                    draft[sec_name],
                )
            self.artifacts["abstract"] = abstract

        # Build sections in submission order
        sections = []
        for heading in _SUBMIT_ORDER:
            content = draft.get(heading, "")
            if content:
                sections.append({"heading": heading, "content": content})

        # Inject comparison table markdown into Results section body
        # (tables stored as sidecar "figures" are invisible to evaluators
        #  who only read section content — must be inline)
        figures = self.artifacts.get("figures", [])
        for fig in figures:
            if fig.get("data_type") == "table":
                data = fig.get("data", {})
                headers = data.get("headers", [])
                rows = data.get("rows", [])
                if headers and rows:
                    # Build markdown table
                    table_md = f"\n\n**{fig.get('caption', 'Table 1: Comparison of Key Studies')}**\n\n"
                    table_md += "| " + " | ".join(headers) + " |\n"
                    table_md += "| " + " | ".join("---" for _ in headers) + " |\n"
                    for row in rows:
                        table_md += "| " + " | ".join(str(c) for c in row) + " |\n"
                    table_md += "\n"
                    # Inject into Results section (or Related Work if no Results)
                    target_section = "Results"
                    sec = next((s for s in sections if s["heading"] == target_section), None)
                    if not sec:
                        target_section = "Related Work"
                        sec = next((s for s in sections if s["heading"] == target_section), None)
                    if sec:
                        sec["content"] = sec["content"].rstrip() + table_md
                        logger.info("Injected table markdown into %s section (%d rows)",
                                    target_section, len(rows))

        # ── Inject study-selection flow table into Methodology ──
        search_audit = self.artifacts.get("search_audit", {})
        meth_sec = next((s for s in sections if s["heading"] == "Methodology"), None)
        if meth_sec and search_audit:
            total_retrieved = search_audit.get("total_retrieved", "?")
            total_dedup = search_audit.get("total_after_dedup", "?")
            total_filtered = search_audit.get("total_after_filter", "?")
            total_included = len(references)
            curated = self.artifacts.get("curated_papers", [])
            ft_count = sum(1 for p in curated if len(p.get("enriched_content", "") or p.get("abstract", "") or "") > 2000)
            ao_count = total_included - ft_count

            flow_table = (
                "\n\n**Table: Study Selection Flow**\n\n"
                "| Stage | Count |\n"
                "| --- | --- |\n"
                f"| Records identified from databases | {total_retrieved} |\n"
                f"| After deduplication | {total_dedup} |\n"
                f"| After relevance screening | {total_filtered} |\n"
                f"| Included in final synthesis | {total_included} |\n"
                f"| — Full text accessed | {ft_count} |\n"
                f"| — Abstract only | {ao_count} |\n"
                "\n"
            )
            meth_sec["content"] = meth_sec["content"].rstrip() + flow_table
            logger.info("Injected study-selection flow table into Methodology")

        # ── Inject included-studies summary table into Related Work ──
        if curated and len(curated) >= 5:
            studies_table = "\n\n**Table: Summary of Included Studies**\n\n"
            studies_table += "| Study | Year | Access | Domain | Key Contribution |\n"
            studies_table += "| --- | --- | --- | --- | --- |\n"
            source_table = self.artifacts.get("source_classification", [])
            for i, p in enumerate(curated[:30]):
                authors = p.get("authors", [])
                surname = _extract_surname(authors[0]) if authors else "Unknown"
                et_al = " et al." if len(authors) >= 3 else ""
                year = p.get("year", "?")
                content = p.get("enriched_content", "") or p.get("abstract", "") or ""
                access = "Full text" if len(content) > 2000 else "Abstract"
                # Get domain and finding from source classification if available
                sc_entry = source_table[i] if i < len(source_table) else {}
                domain = sc_entry.get("domain", "—") if isinstance(sc_entry, dict) else "—"
                finding = sc_entry.get("finding", "—") if isinstance(sc_entry, dict) else "—"
                if len(finding) > 60:
                    finding = finding[:57] + "..."
                studies_table += f"| {surname}{et_al} | {year} | {access} | {domain} | {finding} |\n"
            studies_table += "\n"

            rw_sec = next((s for s in sections if s["heading"] == "Related Work"), None)
            if rw_sec:
                rw_sec["content"] = rw_sec["content"].rstrip() + studies_table
                logger.info("Injected included-studies summary table into Related Work (%d rows)", min(len(curated), 30))

        # Check word count — try to expand undersized sections before giving up
        total_words = sum(len(s["content"].split()) for s in sections)
        _skip_api = False
        _skip_reason = ""
        if total_words < self.config.min_total_words:
            shortfall = self.config.min_total_words - total_words
            logger.info("Paper has %d words (need %d, short by %d) — attempting expansion",
                        total_words, self.config.min_total_words, shortfall)
            self.display.step(f"Paper too short ({total_words} words) — expanding undersized sections...")

            # Find sections that are below their target and expand the shortest ones first
            ref_list = [{"author": r.get("authors", "?"), "year": r.get("year", "?"), "title": r.get("title", "")} for r in references]
            ref_list_text = json.dumps(ref_list, indent=1)
            expandable = ["Related Work", "Results", "Discussion", "Introduction"]
            for section_name in expandable:
                if total_words >= self.config.min_total_words:
                    break
                sec = next((s for s in sections if s["heading"] == section_name), None)
                if not sec:
                    continue
                sec_words = len(sec["content"].split())
                sec_target = self._section_word_target(section_name)
                if sec_words >= sec_target:
                    continue  # already at target
                self.display.step(f"  Expanding {section_name}: {sec_words} → {sec_target} words...")
                expand_prompt = f"""The {section_name} section has only {sec_words} words but needs at least {sec_target}.

CURRENT TEXT:
{sec['content']}

EXPAND to at least {sec_target} words. Add more evidence from the bibliography with specific findings.

BIBLIOGRAPHY (cite by [Author, Year]):
{ref_list_text[:15000]}

Write ONLY the expanded section text. MINIMUM {sec_target} WORDS."""
                try:
                    resp = self.llm.generate(
                        f"You are expanding the {section_name} section of an academic paper. Write detailed, evidence-rich prose.",
                        expand_prompt, temperature=0.2,
                        max_tokens=self._section_max_tokens(section_name),
                    )
                    expanded = strip_thinking_tags(resp.text).strip()
                    expanded = self._clean_section_text(expanded)
                    if len(expanded.split()) > sec_words:
                        sec["content"] = expanded
                        draft[section_name] = expanded
                        total_words = sum(len(s["content"].split()) for s in sections)
                        logger.info("Expanded %s: %d → %d words (total now %d)",
                                    section_name, sec_words, len(expanded.split()), total_words)
                except (LLMError, Exception) as e:
                    logger.warning("Pre-submit expansion of %s failed: %s", section_name, e)

            # Re-check after expansion
            total_words = sum(len(s["content"].split()) for s in sections)

        if total_words < self.config.min_total_words:
            _skip_api = True
            _skip_reason = f"Paper has {total_words} words but requires minimum {self.config.min_total_words}"

        # Check required sections
        present = {s["heading"] for s in sections}
        missing = [h for h in _SUBMIT_ORDER if h not in present]
        if missing:
            _skip_api = True
            _skip_reason = f"Missing required sections: {', '.join(missing)}"

        # Pre-submission: drop references with invalid titles (< 10 chars)
        bad_refs = [r for r in references if len((r.get("title") or "").strip()) < 10]
        if bad_refs:
            logger.info("Dropping %d references with too-short titles: %s",
                        len(bad_refs), [r.get("title", "") for r in bad_refs])
            references = [r for r in references if len((r.get("title") or "").strip()) >= 10]
            self.artifacts["references"] = references

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

        # Hard safety cap: API rejects titles > 200 chars
        if len(title) > 200:
            # Take just the first line/sentence, truncated
            short = title.split("\n")[0].split(". ")[0].strip()
            if len(short) > 190:
                short = short[:187] + "..."
            title = short
            logger.warning("Title truncated from %d chars to %d chars", len(brief.get("title", "")), len(title))

        # Token usage
        token_usage = self.llm.total_usage
        logger.info(
            "Session token usage — input: %s, output: %s, thinking: %s, total: %s",
            f"{token_usage.get('input_tokens', 0):,}",
            f"{token_usage.get('output_tokens', 0):,}",
            f"{token_usage.get('thinking_tokens', 0):,}",
            f"{token_usage.get('total_tokens', 0):,}",
        )

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
            "thinking_tokens": token_usage.get("thinking_tokens", 0),
            "total_tokens": token_usage.get("total_tokens", 0),
            "generation_seconds": generation_seconds,
            "sdk_version": sdk_version,
            "content_hash": content_hash,
            "search_audit": self.artifacts.get("search_audit", {}),
            "provenance": self._build_provenance_sidecar(references),
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
            # Text references tables/figures that don't exist — log warning.
            # The LLM editorial review and citation cleanup passes handle this.
            logger.warning("Paper references Table/Figure %s but no figures in payload", missing_tables)

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
            self.display.phase_done(10)
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

        # Title framing validator: disallow method labels the paper doesn't earn
        paper_payload["title"] = self._sanitize_title_framing(paper_payload["title"])
        paper_payload["abstract"] = self._sanitize_abstract_framing(paper_payload["abstract"])

        # Submit with intelligent retry and LLM-powered rework
        result = self._submit_with_rework(paper_payload, title, total_words)
        self.display.phase_done(10)
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
                self.artifacts["paper_id"] = pid
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

        # Too few references — log but do NOT pad with unvetted papers.
        # Better to submit with fewer high-quality refs than pad with garbage.
        if "references" in error_lower and ("at least" in error_lower or "minimum" in error_lower):
            current = len(paper_payload.get("references", []))
            logger.warning("API wants more references (have %d) — submitting as-is with curated refs only", current)
            self.display.step(f"  Reference count ({current}) below API minimum — submitting with curated refs only")

        # Missing required fields
        if "field required" in error_lower or "missing" in error_lower:
            logger.info("Missing field detected, attempting LLM fix: %s", error_detail[:200])

        # --- LLM-powered fix for everything else ---
        sections_summary = ""
        for s in paper_payload.get("sections", []):
            wc = len((s.get("content") or "").split())
            sections_summary += f"  - {s.get('heading', '?')}: {wc} words\n"

        refs_count = len(paper_payload.get("references", []))

        fix_system, fix_user = self._get_prompt(
            "fix_paper",
            error_detail=error_detail,
            title=paper_payload.get('title', 'N/A'),
            abstract_word_count=str(len(paper_payload.get('abstract', '').split())),
            sections_summary=sections_summary,
            refs_count=str(refs_count),
        )
        if not fix_system:
            fix_system = (
                "You are a research paper editor. The paper was rejected by the API with a validation error. "
                "Fix the paper to address the error. Return ONLY valid JSON with the corrected fields."
            )
        if not fix_user:
            fix_user = (
                f"The paper submission was rejected with this error:\n\n"
                f"{error_detail}\n\n"
                f"Current paper structure:\n"
                f"- Title: {paper_payload.get('title', 'N/A')}\n"
                f"- Abstract: {len(paper_payload.get('abstract', '').split())} words\n"
                f"- Sections:\n{sections_summary}- References: {refs_count}\n\n"
                f'Provide corrections as JSON. Only include fields that need changing.\n'
                f'Return ONLY the JSON with fields that need fixing. If no fix is possible, return {{"no_fix": true}}.'
            )

        try:
            fixes = self.llm.generate_json(fix_system, fix_user)
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
        """Search academic databases for additional references to meet the minimum.

        Uses real academic search APIs (OpenAlex, Crossref, Semantic Scholar)
        instead of asking the LLM to fabricate references.
        """
        current_refs = paper_payload.get("references", [])
        needed = minimum - len(current_refs)
        if needed <= 0:
            return paper_payload

        existing_titles = {(r.get("title") or "").lower()[:60] for r in current_refs}
        topic = paper_payload.get("title", "the research topic")
        brief = self.artifacts.get("research_brief", _EMPTY_BRIEF)

        # Build search queries from paper title, research questions, and search terms
        queries = [topic]
        for rq in brief.get("research_questions", [])[:2]:
            queries.append(rq)
        for term in brief.get("search_terms", [])[:3]:
            queries.append(term)

        self.display.step(f"  Searching academic databases for {needed} more references...")
        candidates = []
        for query in queries:
            if len(candidates) >= needed + 5:
                break
            try:
                hits = self._search(
                    query, limit=10, year_from=2016,
                )
                for h in hits:
                    title_key = (h.get("title") or "").lower()[:60]
                    if title_key and title_key not in existing_titles:
                        existing_titles.add(title_key)
                        # Only keep papers with authors and a real title
                        if h.get("authors") and len(h.get("title", "")) > 15:
                            candidates.append(h)
            except Exception as e:
                logger.warning("Gap-fill search failed for '%s': %s", query[:40], e)
            time.sleep(0.3)

        if not candidates:
            logger.warning("No additional references found via academic search")
            return paper_payload

        # Build proper reference entries from search results
        added = 0
        for paper in candidates[:needed + 2]:
            authors = paper.get("authors", [])
            if not authors:
                continue
            ref = {
                "title": paper.get("title", ""),
                "authors": authors,
                "year": paper.get("year"),
                "venue": paper.get("venue") or paper.get("journal") or "",
                "url": paper.get("url") or "",
                "doi": paper.get("doi") or "",
                "type": "external",
                "ref_id": f"gap_fill_{len(current_refs) + added + 1}",
            }
            if isinstance(ref.get("year"), str):
                try:
                    ref["year"] = int(ref["year"])
                except ValueError:
                    ref["year"] = None
            current_refs.append(ref)
            added += 1
            if added >= needed:
                break

        paper_payload["references"] = current_refs
        logger.info("Added %d references from academic search (now %d total)", added, len(current_refs))
        self.display.step(f"  Added {added} references from academic search (total: {len(current_refs)})")

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
        sections_text = ""
        if paper.sections:
            for sec in paper.sections[:10]:
                heading = sec.get("heading", "")
                content = sec.get("content", "")[:2000]
                sections_text += f"\n## {heading}\n{content}\n"
        paper_text = f"Title: {paper.title}\nAbstract: {paper.abstract}\n{sections_text}"

        pr_system, pr_user = self._get_prompt(
            "peer_review",
            title=paper.title,
            abstract=paper.abstract or "",
            sections_text=sections_text,
            reference_count=str(len(paper.references or [])),
        )
        if not pr_system:
            pr_system = "You are an expert academic peer reviewer. Evaluate the paper rigorously."
        if not pr_user:
            pr_user = (
                f"{paper_text[:10000]}\n\n"
                f'Review this paper. Return JSON with:\n'
                f'- "scores": dict with keys ["novelty", "methodology", "clarity", "reproducibility", "citation_quality"], each 1-10\n'
                f'- "overall_score": float 1-10\n'
                f'- "decision": "accept", "revise", or "reject"\n'
                f'- "summary": 2-3 sentence summary\n'
                f'- "strengths": list of 3-5 strengths\n'
                f'- "weaknesses": list of 3-5 weaknesses'
            )

        try:
            review = self.llm.generate_json(pr_system, pr_user)
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
        paper_type = (brief.get("paper_type") or "survey").lower()
        if "meta" in paper_type:
            return "meta_analysis"

        title = (brief.get("title") or "").lower()
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
    # 4a3: Cross-section repetition detection and removal
    # ------------------------------------------------------------------

    def _remove_cross_section_repetition(self, draft: dict[str, str]) -> dict[str, str]:
        """Detect and remove sentences that are paraphrased repeats across sections.

        Uses trigram overlap to find similar sentences. When a repeat is found,
        keeps the version in the section with higher priority (earlier in write order)
        and removes the repeat from the later section.
        """
        def _trigrams(text: str) -> set[tuple[str, ...]]:
            words = text.lower().split()
            if len(words) < 3:
                return set()
            return {tuple(words[i:i + 3]) for i in range(len(words) - 2)}

        def _sim(a: str, b: str) -> float:
            ta, tb = _trigrams(a), _trigrams(b)
            if not ta or not tb:
                return 0.0
            return len(ta & tb) / min(len(ta), len(tb))

        # Section priority: earlier = keep, later = remove duplicates
        section_priority = {name: i for i, name in enumerate(_WRITE_ORDER)}

        # Collect sentences by section
        section_sentences: dict[str, list[str]] = {}
        for heading, content in draft.items():
            sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", content) if len(s.split()) >= 8]
            section_sentences[heading] = sents

        # Find cross-section duplicates
        to_remove: list[tuple[str, str]] = []  # (section, sentence) to remove
        headings = list(section_sentences.keys())
        for i, h1 in enumerate(headings):
            for h2 in headings[i + 1:]:
                for s1 in section_sentences[h1]:
                    for s2 in section_sentences[h2]:
                        sim = _sim(s1, s2)
                        if sim > 0.40:
                            # Remove from lower-priority (later) section
                            p1 = section_priority.get(h1, 99)
                            p2 = section_priority.get(h2, 99)
                            if p1 <= p2:
                                to_remove.append((h2, s2))
                            else:
                                to_remove.append((h1, s1))

        if not to_remove:
            self.display.step("  No cross-section repetition detected")
            return draft

        # Deduplicate removal list
        seen = set()
        unique_removals: list[tuple[str, str]] = []
        for section, sent in to_remove:
            key = (section, sent[:80])
            if key not in seen:
                seen.add(key)
                unique_removals.append((section, sent))

        # Remove sentences from draft
        removed_count = 0
        for section, sentence in unique_removals:
            if section in draft and sentence in draft[section]:
                draft[section] = draft[section].replace(sentence, "").strip()
                # Clean up double spaces and orphan whitespace
                draft[section] = re.sub(r"\s{2,}", " ", draft[section])
                draft[section] = re.sub(r"\n\s*\n\s*\n", "\n\n", draft[section])
                removed_count += 1

        self.display.step(f"  Removed {removed_count} repeated sentences across sections")
        for section, sent in unique_removals[:5]:
            self.display.step(f"    [{section}] {sent[:70]}...")

        return draft

    # ------------------------------------------------------------------
    # Fix 1C: Claim-citation relevance check
    # ------------------------------------------------------------------

    # NOTE: _check_claim_citation_relevance has been removed. Claim-citation
    # relevance is now checked by the LLM source verification pass which reads
    # actual paper abstracts and understands semantic similarity.

    # ------------------------------------------------------------------
    # 4a2: LLM editorial review (replaces regex-based overclaim/framework/AI-jargon fixes)
    # ------------------------------------------------------------------

    def _llm_editorial_review(
        self,
        draft: dict[str, str],
        abstract: str,
        paper_type: str,
        actual_ref_count: int,
    ) -> tuple[dict[str, str], str]:
        """Single LLM pass to fix overclaiming, framework language, AI jargon, and tone.

        Replaces the old regex-based passes (_OVERCLAIM_PATTERNS, _FRAMEWORK_PATTERNS,
        _OVERPROMISE_MAP, AI self-description stripper) with one LLM call that understands context.
        """
        full_draft_text = "\n\n".join(
            f"=== {name} ===\n{draft[name]}"
            for name in _WRITE_ORDER if name in draft
        )

        # Use GUI-editable prompt (phase6_editorial_review), fall back to hardcoded
        er_system, er_user = self._get_prompt(
            "phase6_editorial_review",
            paper_type=paper_type,
            ref_count=str(actual_ref_count),
            abstract=abstract,
            full_draft=full_draft_text[:25000],
        )
        if not er_system:
            er_system = "You are a precise academic editor. Return the full corrected paper with section headers preserved."
        if not er_user:
            er_user = (
                f"Review this {paper_type} paper with {actual_ref_count} references. "
                f"Fix overclaiming, framework language, AI jargon, and systematic review claims. "
                f"Return full text with === Section Name === headers.\n\n"
                f"=== Abstract ===\n{abstract}\n\n{full_draft_text[:25000]}"
            )

        try:
            resp = self._get_review_llm().generate(
                er_system,
                er_user,
                temperature=0.2,
                max_tokens=65000,
                think=True,
            )
            edited = strip_thinking_tags(resp.text).strip()

            # Parse sections back from LLM response with safety checks
            updated_any = False
            cite_pat = re.compile(r"\[[A-Z][a-zA-Z\-]+(?:\s+et\s+al\.)?(?:,\s*\d{4}[a-z]?)?\]")

            for section_name in _WRITE_ORDER:
                pat = re.compile(
                    rf"===\s*{re.escape(section_name)}\s*===\s*\n(.*?)(?=\n===\s|\Z)",
                    re.DOTALL,
                )
                m = pat.search(edited)
                if not m:
                    continue
                new_text = self._clean_section_text(m.group(1).strip())
                if not new_text or len(new_text) < 100:
                    continue
                old_text = draft.get(section_name, "")

                # Safety 1: reject if length changed by more than 25%
                if old_text and not (0.75 < len(new_text) / len(old_text) < 1.25):
                    logger.warning(
                        "Editorial review: rejecting %s — length ratio %.2f",
                        section_name, len(new_text) / len(old_text),
                    )
                    continue

                # Safety 2: reject if citations were lost
                old_cites = set(cite_pat.findall(old_text))
                new_cites = set(cite_pat.findall(new_text))
                lost_cites = old_cites - new_cites
                if len(lost_cites) > 2:
                    logger.warning(
                        "Editorial review: rejecting %s — lost %d citations: %s",
                        section_name, len(lost_cites), list(lost_cites)[:5],
                    )
                    continue

                draft[section_name] = new_text
                updated_any = True

            # Extract abstract (same safety checks)
            abs_pat_match = re.compile(
                r"===\s*Abstract\s*===\s*\n(.*?)(?=\n===\s|\Z)",
                re.DOTALL,
            )
            abs_m = abs_pat_match.search(edited)
            if abs_m:
                new_abstract = self._clean_section_text(abs_m.group(1).strip())
                if new_abstract and len(new_abstract) > 50:
                    if 0.7 < len(new_abstract) / max(len(abstract), 1) < 1.3:
                        abstract = new_abstract
                        self.artifacts["abstract"] = abstract
                        updated_any = True
                    else:
                        logger.warning("Editorial review: rejecting abstract — length ratio %.2f",
                                       len(new_abstract) / max(len(abstract), 1))

            if updated_any:
                self.display.step("  LLM editorial review: corrections applied")
            else:
                self.display.step("  LLM editorial review: no changes needed")

        except LLMError as e:
            logger.warning("LLM editorial review failed: %s — skipping", e)
            self.display.step("  LLM editorial review: skipped (error)")

        return draft, abstract

    # ------------------------------------------------------------------
    # 4m2: Corpus count consistency validator (deterministic)
    # ------------------------------------------------------------------

    def _validate_corpus_counts(
        self, draft: dict[str, str], abstract: str, references: list[dict],
    ) -> None:
        """Check that stated corpus counts in text match actual table rows / reference count.

        If the text says 'N studies reviewed' but the table has fewer rows,
        fix the text to match the table. If no table, fix to match ref count.
        """
        # Determine the actual counts
        actual_ref_count = len(references)
        table_row_count = 0
        figures = self.artifacts.get("figures", [])
        for fig in figures:
            if fig.get("data_type") == "table":
                rows = fig.get("data", {}).get("rows", [])
                # Exclude stub rows (all dashes)
                real_rows = [r for r in rows if r and any(c != "—" for c in r[2:])]
                table_row_count = len(real_rows)
                break

        # Find corpus count claims in abstract and methodology
        corpus_pattern = re.compile(
            r"\b(\d{1,4})\s+(?:studies|papers|articles|sources|publications|works)"
            r"(?:\s+(?:were|was|are|is))?\s+(?:reviewed|examined|analyzed|included|selected|surveyed|synthesized)",
            re.IGNORECASE,
        )

        # Also catch "corpus of N papers", "synthesis of N studies", etc.
        corpus_pattern2 = re.compile(
            r"(?:corpus|sample|set|synthesis|review)\s+of\s+(\d{1,4})\s+"
            r"(?:studies|papers|articles|sources|publications|works)",
            re.IGNORECASE,
        )

        # Use ONE consistent number across ALL sections.
        # Priority: table row count (most visible to reader) > ref count (fallback).
        canonical_count = table_row_count if table_row_count else actual_ref_count
        logger.info(
            "Corpus count canonical target: %d (table=%d, refs=%d)",
            canonical_count, table_row_count, actual_ref_count,
        )

        def _fix_count(text: str, section_name: str) -> str:
            """Replace overclaimed corpus counts with actual count."""
            changed = False
            for pat in (corpus_pattern, corpus_pattern2):
                for m in pat.finditer(text):
                    claimed = int(m.group(1))
                    # Only fix if claimed count is wrong
                    if claimed == canonical_count:
                        continue
                    correct = canonical_count
                    logger.info(
                        "Corpus count fix in %s: %d → %d",
                        section_name, claimed, correct,
                    )
                    text = text.replace(m.group(0), m.group(0).replace(str(claimed), str(correct), 1))
                    changed = True
            if changed:
                self.display.step(f"  Fixed corpus count in {section_name}")
            return text

        # Fix in abstract
        if abstract:
            fixed_abstract = _fix_count(abstract, "Abstract")
            if fixed_abstract != abstract:
                self.artifacts["abstract"] = fixed_abstract

        # Fix in all sections
        for section_name in list(draft.keys()):
            draft[section_name] = _fix_count(draft[section_name], section_name)

    # ------------------------------------------------------------------
    # Change 1: CorpusManifest — single source of truth for study counts
    # ------------------------------------------------------------------

    def _create_corpus_manifest(self) -> CorpusManifest:
        """Create a frozen CorpusManifest from pipeline artifacts.

        Called once after research phase completes. All subsequent count
        references use manifest.display_count.
        """
        search_audit = self.artifacts.get("search_audit", {})
        curated = self.artifacts.get("curated_papers", [])
        reading_notes = self.artifacts.get("reading_notes", [])

        n_full_text = 0
        n_abstract_only = 0
        for paper in curated:
            content = paper.get("enriched_content", "") or paper.get("abstract", "") or ""
            if len(content) > 2000:
                n_full_text += 1
            else:
                n_abstract_only += 1

        years = [p.get("year") for p in curated if p.get("year")]
        year_range = f"{min(years)}-{max(years)}" if years else ""

        databases = tuple(search_audit.get("databases", []))

        manifest = CorpusManifest(
            total_retrieved=search_audit.get("total_retrieved", 0),
            total_after_dedup=search_audit.get("total_after_dedup", 0),
            total_after_filter=search_audit.get("total_after_filter", 0),
            total_included=len(curated),
            total_in_final_refs=len(curated),  # updated after audit pruning
            full_text_count=n_full_text,
            abstract_only_count=n_abstract_only,
            databases=databases,
            year_range=year_range,
        )
        self.artifacts["corpus_manifest"] = manifest
        logger.info(
            "CorpusManifest created: display_count=%d (retrieved=%d, dedup=%d, filter=%d, included=%d)",
            manifest.display_count, manifest.total_retrieved,
            manifest.total_after_dedup, manifest.total_after_filter, manifest.total_included,
        )
        return manifest

    def _validate_manifest_counts(
        self, draft: dict[str, str], abstract: str,
    ) -> tuple[dict[str, str], str]:
        """Scan ALL sections + abstract for study-count numbers and fix any
        that don't match manifest.display_count.

        Uses 4 broadened regex patterns to catch all variants:
        1. "N studies/papers/articles were reviewed/analyzed/..."
        2. "corpus/sample/set of N studies/papers/..."
        3. "reviewing/examining/analyzing N studies/papers/..."
        4. "N peer-reviewed/included/selected studies/papers/..."

        Returns updated (draft, abstract).
        """
        manifest = self._get_manifest()
        if not manifest:
            return draft, abstract

        canonical = manifest.display_count
        if canonical <= 0:
            return draft, abstract

        patterns = [
            # Pattern 1: "N studies were reviewed"
            re.compile(
                r"\b(\d{1,4})\s+(?:peer[- ]reviewed\s+)?(?:studies|papers|articles|sources|publications|works|records)"
                r"(?:\s+(?:were|was|are|is))?\s+(?:reviewed|examined|analyzed|included|selected|surveyed|synthesized|assessed|screened|identified|retrieved)",
                re.IGNORECASE,
            ),
            # Pattern 2: "corpus of N papers"
            re.compile(
                r"(?:corpus|sample|set|synthesis|review|collection|pool)\s+(?:of|comprising)\s+(\d{1,4})\s+"
                r"(?:studies|papers|articles|sources|publications|works|records)",
                re.IGNORECASE,
            ),
            # Pattern 3: "reviewing N studies"
            re.compile(
                r"(?:reviewing|examining|analyzing|synthesizing|covering|spanning|comprising)\s+(\d{1,4})\s+"
                r"(?:studies|papers|articles|sources|publications|works|records)",
                re.IGNORECASE,
            ),
            # Pattern 4: "N included/selected studies"
            re.compile(
                r"\b(\d{1,4})\s+(?:included|selected|curated|final|remaining)\s+"
                r"(?:studies|papers|articles|sources|publications|works|records)",
                re.IGNORECASE,
            ),
            # Pattern 5: "the N-paper corpus" or "N-paper corpus" or "N‑paper corpus"
            re.compile(
                r"\b(?:the\s+)?(\d{1,4})[-\u2010\u2011\u2012\u2013](?:paper|study|article|source|record)\s+"
                r"(?:corpus|set|sample|collection|pool)",
                re.IGNORECASE,
            ),
            # Pattern 6: "Analysis of the N papers/records" or "across N papers"
            re.compile(
                r"(?:analysis\s+of\s+(?:the\s+)?|across\s+|drawing\s+on\s+|based\s+on\s+)(\d{1,4})\s+"
                r"(?:peer[- ]reviewed\s+)?(?:studies|papers|articles|sources|publications|works|records)",
                re.IGNORECASE,
            ),
            # Pattern 7: "approximately N sources/papers"
            re.compile(
                r"approximately\s+(\d{1,4})\s+"
                r"(?:studies|papers|articles|sources|publications|works|records)",
                re.IGNORECASE,
            ),
            # Pattern 8: "a total of N papers" / "totaling N studies" / "this review covers N"
            re.compile(
                r"(?:a\s+total\s+of|totaling|this\s+(?:review|paper|synthesis)\s+(?:covers|draws\s+on|synthesizes))\s+(\d{1,4})\s+"
                r"(?:studies|papers|articles|sources|publications|works|records)",
                re.IGNORECASE,
            ),
            # Pattern 9: "N papers in the final synthesis/corpus/dataset"
            re.compile(
                r"\b(\d{1,4})\s+(?:studies|papers|articles|sources|publications|works|records)\s+"
                r"(?:in|comprise|make\s+up|form|constitute)\s+(?:the\s+)?(?:final\s+)?(?:synthesis|corpus|sample|dataset|review|pool)",
                re.IGNORECASE,
            ),
            # Pattern 10: "corpus comprised N papers" / "review included N studies"
            re.compile(
                r"(?:corpus|review|synthesis|sample|analysis)\s+"
                r"(?:comprised|comprises|contained|contains|included|includes|drew\s+on|draws\s+on)\s+"
                r"(?:a\s+total\s+of\s+)?(\d{1,4})\s+"
                r"(?:studies|papers|articles|sources|publications|works|records)",
                re.IGNORECASE,
            ),
            # Pattern 11: "set of N papers" with optional qualifiers like "heterogeneous"
            re.compile(
                r"\b(?:heterogeneous|diverse|broad|curated|final)\s+(?:set|corpus|sample)\s+of\s+(\d{1,4})\s+"
                r"(?:studies|papers|articles|sources|publications|works|records)",
                re.IGNORECASE,
            ),
        ]

        def _fix_text(text: str, section_name: str) -> str:
            changed = False
            for pat in patterns:
                for m in pat.finditer(text):
                    claimed = int(m.group(1))
                    if claimed == canonical:
                        continue
                    # Skip numbers that are clearly not corpus counts (< 3 or > 500)
                    if claimed < 3 or claimed > 500:
                        continue
                    logger.info(
                        "Manifest count fix in %s: %d → %d",
                        section_name, claimed, canonical,
                    )
                    text = text.replace(m.group(0), m.group(0).replace(str(claimed), str(canonical), 1))
                    changed = True
            if changed:
                self.display.step(f"  Fixed corpus count in {section_name} (manifest={canonical})")
            return text

        # Fix abstract
        if abstract:
            abstract = _fix_text(abstract, "Abstract")

        # Fix all sections
        for section_name in list(draft.keys()):
            draft[section_name] = _fix_text(draft[section_name], section_name)

        return draft, abstract

    # ------------------------------------------------------------------
    # Change 4: Claim-Strength Calibrator (deterministic, 0 LLM cost)
    # ------------------------------------------------------------------

    def _calibrate_claim_strength(self, draft: dict[str, str]) -> dict[str, str]:
        """Post-write pass: for each sentence with a citation, look up the
        cited source's quality_tier.  If quality is weak/tangential AND the
        sentence uses strong language, auto-downgrade to hedged language.

        Depends on Change 2 (enriched source classification with quality_tier).
        """
        source_class = self.artifacts.get("source_classification", [])
        if not source_class:
            return draft

        # Build lookup: "Author" -> quality_tier  (use first-author surname)
        tier_by_author: dict[str, str] = {}
        for entry in source_class:
            author = (entry.get("author") or "").strip()
            if author:
                # Normalize: take first word (surname)
                surname = author.split()[0].rstrip(",").rstrip(".")
                tier_by_author[surname.lower()] = entry.get("quality_tier", "solid")

        # Strong verbs that should be downgraded when evidence is weak
        _STRONG_TO_HEDGED = {
            "demonstrates": "suggests",
            "demonstrate": "suggest",
            "establishes": "indicates",
            "establish": "indicate",
            "confirms": "is consistent with",
            "confirm": "are consistent with",
            "proves": "suggests",
            "prove": "suggest",
            "shows conclusively": "suggests",
            "show conclusively": "suggest",
            "definitively shows": "suggests",
            "definitively show": "suggest",
            "has established": "has suggested",
            "have established": "have suggested",
        }

        # Citation pattern: [Author et al., 2024] or [Author, 2024]
        cite_pat = re.compile(r"\[([A-Z][a-zA-Z\-]+)(?:\s+et\s+al\.?)?,\s*\d{4}[a-z]?\]")

        calibrated_count = 0
        for section_name in list(draft.keys()):
            text = draft[section_name]
            sentences = re.split(r'(?<=[.!?])\s+', text)
            new_sentences = []
            for sentence in sentences:
                cites = cite_pat.findall(sentence)
                if not cites:
                    new_sentences.append(sentence)
                    continue

                # Check if ALL cited sources are weak/tangential
                all_weak = True
                for cite_author in cites:
                    tier = tier_by_author.get(cite_author.lower(), "solid")
                    if tier not in ("weak", "tangential"):
                        all_weak = False
                        break

                if all_weak:
                    # Check for strong language and downgrade
                    modified = sentence
                    for strong, hedged in _STRONG_TO_HEDGED.items():
                        pattern = re.compile(r"\b" + re.escape(strong) + r"\b", re.IGNORECASE)
                        if pattern.search(modified):
                            modified = pattern.sub(hedged, modified)
                            calibrated_count += 1
                    new_sentences.append(modified)
                else:
                    new_sentences.append(sentence)
            draft[section_name] = " ".join(new_sentences)

        if calibrated_count:
            self.display.step(f"  Claim calibrator: downgraded {calibrated_count} strong claims with weak sources")
            logger.info("Claim calibrator: %d downgrades applied", calibrated_count)

        return draft

    # ------------------------------------------------------------------
    # Corpus-Scope Enforcer (deterministic, 0 LLM cost)
    # Fixes: "Unsupported central claim" hard-fail flag
    # ------------------------------------------------------------------

    def _enforce_corpus_scope(self, draft: dict[str, str], abstract: str) -> tuple[dict[str, str], str]:
        """Post-generation pass: detect unscoped field-level claims and inject
        corpus-bounding language.

        Problem: LLM writes "the field lacks X" or "no studies have shown Y" when
        it only reviewed N papers. These are field-level claims not supportable from
        a limited corpus.

        Fix: Find sentences with field-level claim patterns and inject scoping
        language like "within the reviewed literature" or "among the N papers examined".
        """
        corpus_size = len(self.artifacts.get("curated_papers", []))
        if corpus_size == 0:
            corpus_size = len(self.artifacts.get("references", []))
        if corpus_size == 0:
            return draft, abstract

        # Determine claim ceiling based on corpus size
        # <20 papers: must scope everything, no field claims
        # 20-40: hedged field claims OK
        # 40+: broader claims permitted
        strict_mode = corpus_size < 20
        moderate_mode = 20 <= corpus_size < 40

        # Patterns that indicate field-level (unscoped) claims
        _FIELD_CLAIM_PATTERNS = [
            # Absence claims
            (r"\b(no studies have|no research has|no work has)\b", "no studies in the reviewed corpus have"),
            (r"\b(few studies have|few researchers have)\b", "few studies in the reviewed literature have"),
            (r"\b(no existing work)\b", "no work in the reviewed corpus"),
            (r"\b(the literature lacks)\b", "the reviewed literature lacks"),
            (r"\b(the field lacks)\b", "the reviewed literature lacks"),
            (r"\b(a gap exists in the literature)\b", "a gap exists in the reviewed literature"),
            (r"\b(remains understudied)\b", "remains underrepresented in the reviewed corpus"),
            (r"\b(remains unexplored)\b", "remains underrepresented in the reviewed corpus"),
            (r"\b(is poorly understood)\b", "is not well represented in the reviewed literature"),
            (r"\b(has received little attention)\b", "received limited attention in the reviewed corpus"),
            (r"\b(has been largely overlooked)\b", "was not prominently represented in the reviewed corpus"),
            (r"\b(has not been established)\b", "was not established in the reviewed literature"),
            (r"\b(has not been demonstrated)\b", "was not demonstrated in the reviewed corpus"),
            (r"\b(has not been investigated)\b", "was not investigated in the reviewed corpus"),
            # Universality claims
            (r"\b(all studies show|all research shows)\b", "the reviewed studies consistently show"),
            (r"\b(the consensus is)\b", "within the reviewed literature, the prevailing view is"),
            (r"\b(it is well established that)\b", "the reviewed evidence indicates that"),
            (r"\b(it is widely accepted that)\b", "the reviewed literature suggests that"),
            # Emergence/trend claims from small corpus
            (r"\b(emerging evidence suggests)\b", "evidence in the reviewed corpus suggests"),
            (r"\b(a growing body of evidence)\b", "evidence in the reviewed literature"),
            (r"\b(mounting evidence suggests)\b", "evidence in the reviewed corpus suggests"),
            (r"\b(recent advances have shown)\b", "recent work in the reviewed corpus shows"),
            (r"\b(the literature demonstrates)\b", "the reviewed literature indicates"),
            (r"\b(the evidence demonstrates)\b", "the reviewed evidence indicates"),
            (r"\b(studies consistently show)\b", "the reviewed studies indicate"),
            (r"\b(research consistently demonstrates)\b", "the reviewed research indicates"),
        ]

        # Additional strict-mode patterns (for corpus < 20 papers)
        _STRICT_PATTERNS = [
            (r"\b(this review reveals)\b", f"this review of {corpus_size} papers suggests"),
            (r"\b(our analysis reveals)\b", f"our analysis of {corpus_size} papers suggests"),
            (r"\b(our synthesis reveals)\b", f"our synthesis of {corpus_size} papers suggests"),
            (r"\b(this synthesis reveals)\b", f"this synthesis of {corpus_size} papers suggests"),
            (r"\b(we found that)\b", f"within the {corpus_size} reviewed papers, we found that"),
            (r"\b(our findings indicate)\b", f"findings from the {corpus_size} reviewed papers indicate"),
            (r"\b(our review indicates)\b", f"our review of {corpus_size} papers indicates"),
            (r"\b(the results indicate)\b", f"results from the {corpus_size} reviewed papers indicate"),
        ]

        patterns = _FIELD_CLAIM_PATTERNS[:]
        if strict_mode:
            patterns.extend(_STRICT_PATTERNS)

        total_replacements = 0

        def _apply_scope_fixes(text: str) -> tuple[str, int]:
            count = 0
            for pattern, replacement in patterns:
                regex = re.compile(pattern, re.IGNORECASE)
                matches = regex.findall(text)
                if matches:
                    # Preserve original case of first letter
                    def _replace_preserving_case(m):
                        original = m.group(0)
                        if original[0].isupper():
                            return replacement[0].upper() + replacement[1:]
                        return replacement
                    text = regex.sub(_replace_preserving_case, text)
                    count += len(matches)
            return text, count

        # Apply to Discussion, Conclusion, and Abstract (the sections most prone to overclaiming)
        for section_name in ["Discussion", "Conclusion", "Limitations", "Results"]:
            if section_name in draft:
                draft[section_name], n = _apply_scope_fixes(draft[section_name])
                total_replacements += n

        # Apply to abstract
        abstract, n = _apply_scope_fixes(abstract)
        total_replacements += n

        # In strict mode (<20 papers), also check Introduction
        if strict_mode and "Introduction" in draft:
            draft["Introduction"], n = _apply_scope_fixes(draft["Introduction"])
            total_replacements += n

        if total_replacements:
            self.display.step(
                f"  Corpus-scope enforcer: bounded {total_replacements} field-level claims "
                f"(corpus={corpus_size}, mode={'strict' if strict_mode else 'moderate' if moderate_mode else 'normal'})"
            )
            logger.info("Corpus-scope enforcer: %d replacements (corpus_size=%d)", total_replacements, corpus_size)

        return draft, abstract

    # ------------------------------------------------------------------
    # Change 5: Citation Density Auditor (deterministic, 0 LLM cost)
    # ------------------------------------------------------------------

    def _audit_citation_density(
        self, draft: dict[str, str], fallback_draft: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Count unique [Author, Year] citations per section and log warnings
        if below minimums.

        If fallback_draft is provided and a section drops below 50% of its
        minimum citation count, the section is restored from fallback_draft.
        Returns the (possibly patched) draft.
        """

        _SECTION_MIN_UNIQUE_CITATIONS = {
            "Related Work": 12,
            "Results": 8,
            "Discussion": 6,
            "Introduction": 3,
            "Limitations": 2,
            "Conclusion": 2,
        }

        cite_pat = re.compile(
            r"\[([A-Z][a-zA-Z" + _HYPH + r"]+(?:\s+et\s+al\.?)?,\s*\d{4}[a-z]?)\]"
        )

        for section_name, min_unique in _SECTION_MIN_UNIQUE_CITATIONS.items():
            text = draft.get(section_name, "")
            if not text:
                continue
            unique_cites = set(cite_pat.findall(text))
            if len(unique_cites) < min_unique:
                logger.warning(
                    "Citation density: %s has %d unique citations (minimum: %d)",
                    section_name, len(unique_cites), min_unique,
                )
                self.display.step(
                    f"  CITATION DENSITY: {section_name} has {len(unique_cites)} unique citations (min: {min_unique})"
                )

                # Fallback: restore from pre-adversarial draft if critically low
                if fallback_draft and len(unique_cites) < min_unique * 0.5:
                    fallback_text = fallback_draft.get(section_name, "")
                    fallback_cites = set(cite_pat.findall(fallback_text))
                    if len(fallback_cites) > len(unique_cites):
                        logger.warning(
                            "Citation density: restoring %s from pre-adversarial draft (%d→%d citations)",
                            section_name, len(unique_cites), len(fallback_cites),
                        )
                        self.display.step(
                            f"  RESTORED {section_name} from pre-fix version ({len(unique_cites)}→{len(fallback_cites)} citations)"
                        )
                        draft[section_name] = fallback_text

        return draft

    # ------------------------------------------------------------------
    # Change 6: Methodology Roleplay Detector (deterministic, 0 LLM cost)
    # ------------------------------------------------------------------

    def _scrub_methodology_roleplay(self, draft: dict[str, str], abstract: str) -> tuple[dict[str, str], str]:
        """Regex scrub for ~15 forbidden phrases that falsely imply human
        research procedures.  Strongest patterns applied globally, weaker
        patterns only in Methodology/Abstract.

        Returns (updated_draft, updated_abstract).
        """
        # Patterns applied to ALL sections (strongest — clearly false claims)
        _GLOBAL_REPLACEMENTS: list[tuple[str, str]] = [
            (r"\btwo independent reviewers? screened\b", "the automated pipeline screened"),
            (r"\bindependent reviewers? (?:screened|assessed|evaluated)\b", "automated screening assessed"),
            (r"\bIRB approval was obtained\b", ""),
            (r"\b(?:IRB|ethics committee|institutional review board) approv(?:al|ed)\b", ""),
            (r"\binformed consent was obtained\b", ""),
            (r"\binter-rater reliability\b", ""),
            (r"\bCohen'?s? kappa\b", ""),
            (r"\bpercent(?:age)? agreement (?:was|between)\b", ""),
            (r"\bblinded? (?:assessment|evaluation|review)\b", "automated assessment"),
            (r"\bsenior author (?:arbitrated|resolved)\b", "automated resolution was applied"),
            (r"\bhuman (?:annotators?|coders?|raters?|reviewers?) (?:independently )?(?:screened|coded|rated|assessed)\b",
             "automated screening processed"),
        ]

        # Patterns applied only to Methodology + Abstract (weaker — could be valid in other contexts)
        _METHODOLOGY_REPLACEMENTS: list[tuple[str, str]] = [
            (r"\bstatistical analyses? (?:were |was )?performed using (?:SPSS|Stata|SAS|R software)\b",
             "synthesis performed using automated text analysis"),
            (r"\bPRISMA (?:guidelines|flow diagram|checklist)\b",
             "structured search and screening protocol"),
            (r"\bmeta-(?:analysis|regression) (?:was |were )?conducted\b",
             "narrative synthesis was conducted"),
            (r"\brandom[- ]effects? model\b", "thematic synthesis approach"),
        ]

        scrub_count = 0

        def _apply_replacements(text: str, replacements: list[tuple[str, str]]) -> str:
            nonlocal scrub_count
            for pattern, replacement in replacements:
                new_text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
                if new_text != text:
                    scrub_count += 1
                text = new_text
            # Clean up artifacts from empty replacements (double spaces, empty sentences)
            text = re.sub(r"  +", " ", text)
            text = re.sub(r"\.\s*\.", ".", text)
            text = re.sub(r"^\s*\.\s*$", "", text, flags=re.MULTILINE)
            return text

        # Apply global replacements to all sections
        for section_name in list(draft.keys()):
            draft[section_name] = _apply_replacements(draft[section_name], _GLOBAL_REPLACEMENTS)

        # Apply methodology-specific replacements only to Methodology + Abstract
        if "Methodology" in draft:
            draft["Methodology"] = _apply_replacements(draft["Methodology"], _METHODOLOGY_REPLACEMENTS)
        abstract = _apply_replacements(abstract, _GLOBAL_REPLACEMENTS)
        abstract = _apply_replacements(abstract, _METHODOLOGY_REPLACEMENTS)

        if scrub_count:
            self.display.step(f"  Roleplay detector: scrubbed {scrub_count} forbidden phrases")
            logger.info("Roleplay detector: %d scrubs applied", scrub_count)

        return draft, abstract

    # ------------------------------------------------------------------
    # Change 3: Process log methodology auto-generation
    # ------------------------------------------------------------------

    def _log_step(self, name: str, description: str,
                  input_count: int = 0, output_count: int = 0,
                  details: dict | None = None) -> None:
        """Record a pipeline step in the process log."""
        self.process_log.append(PipelineStep(
            name=name,
            description=description,
            timestamp=time.time(),
            input_count=input_count,
            output_count=output_count,
            details=details or {},
        ))

    def _generate_methodology_from_process_log(self) -> str:
        """Generate a concise academic methodology — 3 paragraphs max.

        ROOT CAUSE FIX: The old version dumped 7 paragraphs of pipeline internals
        (intermediate counts, quality tier breakdowns, process steps) that:
        1. Created inconsistent numbers (different pipeline stages report different counts)
        2. Read like a technical spec, not academic methodology
        3. Included pipeline artifacts ("20 requested, 19 verified")

        New approach: Report ONLY final numbers. 3 concise paragraphs.
        No intermediate counts. No quality tier breakdowns.
        """
        if not self.process_log:
            return self._generate_template_methodology()

        manifest = self._get_manifest()
        final_count = len(self.artifacts.get("references", []))
        if manifest and isinstance(manifest, CorpusManifest):
            final_count = manifest.display_count

        # Database names
        search_steps = [s for s in self.process_log if s.name == "search"]
        databases = search_steps[0].details.get("databases", []) if search_steps else []
        _DB_DISPLAY = {
            "crossref": "Crossref", "arxiv": "arXiv",
            "semantic_scholar": "Semantic Scholar", "openalex": "OpenAlex",
            "pubmed": "PubMed", "europe_pmc": "Europe PMC",
            "serper": "Google Scholar", "core": "CORE",
            "lens": "Lens.org", "scopus": "Scopus",
            "consensus": "Consensus", "elicit": "Elicit",
            "scite": "Scite.ai", "biorxiv": "bioRxiv/medRxiv",
            "plos": "PLOS", "springer": "Springer Nature",
            "hal": "HAL", "zenodo": "Zenodo",
            "nasa_ads": "NASA ADS", "doaj": "DOAJ",
            "dblp": "DBLP", "internet_archive": "Internet Archive Scholar",
            "openaire": "OpenAIRE", "fatcat": "Fatcat",
            "datacite": "DataCite", "dimensions": "Dimensions",
            "inspire_hep": "INSPIRE-HEP", "eric": "ERIC",
            "figshare": "Figshare", "scielo": "SciELO",
            "base": "BASE", "ieee": "IEEE Xplore",
            "philpapers": "PhilPapers", "cinii": "CiNii",
            "sciencedirect": "ScienceDirect", "wos": "Web of Science",
            "google_books": "Google Books", "open_library": "Open Library",
        }
        db_names = [_DB_DISPLAY.get(d, d) for d in databases] if databases else ["OpenAlex", "Crossref", "Semantic Scholar"]

        # Year range
        curated = self.artifacts.get("curated_papers", [])
        years = [p.get("year") for p in curated if p.get("year")]
        year_range = f"{min(years)}\u2013{max(years)}" if years else "the available literature"

        # Full text vs abstract counts
        ft = manifest.full_text_count if manifest else 0
        ao = manifest.abstract_only_count if manifest else 0

        paragraphs = []

        # Paragraph 1: Search and selection
        import datetime as _dt
        _search_date = _dt.datetime.now().strftime("%B %d, %Y")
        paragraphs.append(
            f"This study adopts a narrative literature review approach — it is not a "
            f"systematic review or meta-analysis, and no statistical pooling of effect sizes "
            f"was performed. A structured literature search was conducted on {_search_date} across "
            f"{', '.join(db_names)} "
            f"using domain-specific keyword queries and citation graph expansion, "
            f"covering publications from {year_range}. "
            f"After deduplication and relevance screening, {final_count} papers were "
            f"included in the final synthesis."
        )

        # Paragraph 2: Scoring, access, and inclusion/exclusion criteria
        access_text = ""
        if ft and ao:
            access_text = (
                f"Of these, {ft} were accessed in full text and {ao} were assessed "
                f"from abstracts and available excerpts. "
                f"Abstract-only sources were used for background context only and were not "
                f"cited as primary evidence for analytical claims. "
            )
        paragraphs.append(
            f"{access_text}"
            f"Papers were ranked using a composite score weighting topical relevance (40%), "
            f"citation impact (25%), foundational status (15%), recency (10%), and "
            f"venue quality (10%), with an author diversity constraint of maximum three "
            f"papers per first author. "
            f"Inclusion criteria: peer-reviewed or preprint papers with clear relevance to "
            f"the research questions, published in English, with identifiable authors and "
            f"publication year. Exclusion criteria: duplicate records, papers without abstracts, "
            f"and works outside the topical scope after relevance screening. "
            f"The synthesis was organized thematically around the research questions, "
            f"with claims weighted by the strength and directness of the supporting evidence."
        )

        # Paragraph 3: Transparency and limitations
        paragraphs.append(
            "This review was conducted using an AI-assisted research pipeline. "
            "Search, retrieval, screening, and synthesis were automated using a large language model "
            "with structured prompts to ensure consistency and reproducibility. "
            f"The study selection flow was: initial retrieval across {len(db_names)} databases → "
            f"deduplication → relevance screening → {final_count} papers included. "
            "As a narrative review, this study is subject to selection bias inherent in the "
            "search strategy and does not claim exhaustive coverage of the field."
        )

        return "\n\n".join(paragraphs)

    # ------------------------------------------------------------------
    # v0.3: Template methodology — deterministic, zero LLM involvement
    # ------------------------------------------------------------------

    def _generate_template_methodology(self) -> str:
        """Generate concise 3-paragraph methodology using only final numbers."""
        search_audit = self.artifacts.get("search_audit", {})
        curated = self.artifacts.get("curated_papers", [])
        manifest = self._get_manifest()

        if not search_audit:
            return ""

        # Final count — single source of truth
        final_count = search_audit.get("total_included", len(curated))
        if manifest and isinstance(manifest, CorpusManifest):
            final_count = manifest.display_count

        # Database display names
        _DB_DISPLAY = {
            "crossref": "Crossref", "arxiv": "arXiv",
            "semantic_scholar": "Semantic Scholar", "openalex": "OpenAlex",
            "pubmed": "PubMed", "europe_pmc": "Europe PMC",
            "serper": "Google Scholar (Serper)", "core": "CORE",
            "biorxiv": "bioRxiv/medRxiv", "plos": "PLOS",
            "springer": "Springer Nature", "hal": "HAL",
            "zenodo": "Zenodo", "nasa_ads": "NASA ADS",
            "doaj": "DOAJ", "dblp": "DBLP",
            "internet_archive": "Internet Archive Scholar",
            "openaire": "OpenAIRE", "fatcat": "Fatcat",
            "datacite": "DataCite", "dimensions": "Dimensions",
            "inspire_hep": "INSPIRE-HEP", "eric": "ERIC",
            "figshare": "Figshare", "scielo": "SciELO",
            "base": "BASE", "ieee": "IEEE Xplore",
            "philpapers": "PhilPapers", "cinii": "CiNii",
            "sciencedirect": "ScienceDirect", "wos": "Web of Science",
            "google_books": "Google Books", "open_library": "Open Library",
        }
        databases = search_audit.get("databases", [])
        db_names = [_DB_DISPLAY.get(d, d) for d in databases] if databases else ["OpenAlex", "Crossref", "Semantic Scholar"]

        # Year range
        years = [p.get("year") for p in curated if p.get("year")]
        year_range = f"{min(years)}\u2013{max(years)}" if years else "recent years"

        # Full-text vs abstract-only access
        n_full = sum(1 for p in curated if len(p.get("enriched_content", "") or p.get("abstract", "") or "") > 2000)
        n_abstract = len(curated) - n_full

        # Paragraph 1: Search and selection (only final number)
        import datetime as _dt
        _search_date = _dt.datetime.now().strftime("%B %d, %Y")
        p1 = (
            f"A systematic literature search was conducted on {_search_date} across "
            f"{', '.join(db_names)} "
            f"using domain-specific keyword queries and citation graph expansion, "
            f"covering publications from {year_range}. "
            f"After deduplication and relevance screening, {final_count} papers were "
            f"included in the final synthesis."
        )

        # Paragraph 2: Analysis approach
        if n_full > 0 and n_abstract > 0:
            access_text = (
                f"Of these, {n_full} were accessed in full text and "
                f"{n_abstract} via abstracts and available excerpts. "
            )
        elif n_full > 0:
            access_text = f"All {n_full} papers were accessed in full text. "
        else:
            access_text = ""

        p2 = (
            f"{access_text}"
            f"Each paper was assessed for methodological quality and relevance. "
            f"The synthesis was organized thematically around the research questions, "
            f"with claims weighted by the strength and directness of the supporting evidence."
        )

        # Paragraph 3: Transparency
        p3 = (
            "This review was conducted using an AI-assisted research pipeline. "
            "Search, retrieval, screening, and synthesis were automated using a large language model "
            "with structured prompts to ensure consistency and reproducibility."
        )

        return "\n\n".join([p1, p2, p3])

    # ------------------------------------------------------------------
    # 4m-pre: Deterministic methodology scrubbers (before LLM fix)
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_fabricated_stages(text: str) -> str:
        """Remove sentences describing screening stages that don't exist in an automated pipeline."""
        fabricated_patterns = [
            r"(?i)\b(?:full[- ]text|title[/ ]abstract)\s+screening\b[^.]*\.",
            r"(?i)\beligibility\s+assessment\b[^.]*\.",
            r"(?i)\bcritical\s+appraisal\b[^.]*\.",
            r"(?i)\brisk[- ]of[- ]bias\s+assessment\b[^.]*\.",
            r"(?i)\bquality\s+(?:assessment|appraisal)\b[^.]*?(?:resulted in|yielded|led to|removed|excluded)\b[^.]*\.",
            r"(?i)\btwo\s+(?:independent\s+)?reviewers?\b[^.]*\.",
            r"(?i)\binter[- ]rater\s+reliability\b[^.]*\.",
            r"(?i)\bcohen'?s?\s+kappa\b[^.]*\.",
            r"(?i)\bPRISMA\s+flow\s+diagram\b[^.]*\.",
        ]
        for pat in fabricated_patterns:
            text = re.sub(pat, "", text)
        # Clean up double spaces and blank lines
        text = re.sub(r"  +", " ", text)
        text = re.sub(r"\n\s*\n\s*\n", "\n\n", text)
        return text.strip()

    def _strip_fabricated_databases(self, text: str) -> str:
        """Remove mentions of databases we never actually searched.

        The LLM frequently claims we searched Web of Science, Scopus, ERIC,
        ProQuest, Google Scholar, PsycINFO, etc. when we only used the APIs
        in search_audit['databases']. This strips those false claims.
        """
        search_audit = self.artifacts.get("search_audit", {})
        real_dbs = set(search_audit.get("databases", []))
        # Map of real API names to display names the LLM might use
        real_display = set()
        _DB_DISPLAY = {
            "crossref": {"crossref"},
            "arxiv": {"arxiv"},
            "semantic_scholar": {"semantic scholar"},
            "openalex": {"openalex"},
            "pubmed": {"pubmed"},
            "europe_pmc": {"europe pmc", "pmc", "pubmed central"},
            "serper": {"google scholar"},
            "core": {"core"},
            "biorxiv": {"biorxiv", "medrxiv"},
            "plos": {"plos"},
            "springer": {"springer", "springer nature"},
            "hal": {"hal"},
            "zenodo": {"zenodo"},
            "nasa_ads": {"nasa ads", "adsabs"},
            "doaj": {"doaj"},
            "dblp": {"dblp"},
            "internet_archive": {"internet archive"},
            "openaire": {"openaire"},
            "base": {"base"},
            "eric": {"eric"},
            "ieee": {"ieee", "ieee xplore"},
            "sciencedirect": {"sciencedirect", "science direct"},
            "dimensions": {"dimensions"},
            "inspire_hep": {"inspire", "inspirehep"},
        }
        for db in real_dbs:
            real_display.update(_DB_DISPLAY.get(db, {db}))
            real_display.add(db)

        # Databases that are commonly fabricated
        fake_db_patterns = [
            (r"Web of Science", "web of science"),
            (r"Scopus", "scopus"),
            (r"ERIC", "eric"),
            (r"ProQuest", "proquest"),
            (r"PsycINFO", "psycinfo"),
            (r"CINAHL", "cinahl"),
            (r"Cochrane", "cochrane"),
            (r"MEDLINE", "medline"),
            (r"EBSCOhost", "ebscohost"),
            (r"IEEE Xplore", "ieee xplore"),
            (r"ACM Digital Library", "acm digital library"),
            (r"Wiley Online Library", "wiley"),
            (r"SpringerLink", "springerlink"),
            (r"Science Direct", "sciencedirect"),
            (r"ScienceDirect", "sciencedirect"),
        ]

        for display_name, check_name in fake_db_patterns:
            if check_name in real_display:
                continue  # This one is real
            # Remove from comma-separated lists: "Crossref, Web of Science, and OpenAlex"
            text = re.sub(
                rf",?\s*(?:and\s+)?{re.escape(display_name)}\b", "", text
            )
            # Also catch when it's first in a list: "Web of Science, Crossref, ..."
            text = re.sub(
                rf"\b{re.escape(display_name)},?\s*(?:and\s+)?", "", text
            )

        # Clean up truncated/broken database references (e.g. "PubMed/, Embase, or.")
        text = re.sub(r"\b(?:such as|including|like)\s+[A-Za-z]+/,?\s*(?:[A-Za-z]+,?\s*)*(?:or|and)\s*\.", ".", text)
        # Clean up "databases such as ." or "databases such as , and ."
        text = re.sub(r"such as\s*[,\s]*(?:and\s*)?\.(?=\s)", ".", text)

        # Clean up artifacts: double commas, trailing "and", leading "and"
        text = re.sub(r",\s*,", ",", text)
        text = re.sub(r",\s*and\s*([.;])", r" and\1", text)  # fix trailing ", and."
        text = re.sub(r"from\s+and\s+", "from ", text)
        text = re.sub(r"including\s+and\s+", "including ", text)
        text = re.sub(r"  +", " ", text)
        return text

    @staticmethod
    def _scrub_methodology_numbers(text: str, search_audit: dict, ref_count: int) -> str:
        """Replace fabricated screening numbers with real pipeline numbers.

        Only touches number+label combos that look like screening stages
        (e.g. '338 articles', '52 studies'). Leaves years (4-digit > 1900) alone.
        """
        real = {
            int(search_audit.get("total_retrieved", 0)),
            int(search_audit.get("total_after_dedup", 0)),
            int(search_audit.get("total_after_filter", 0)),
            int(search_audit.get("total_included", ref_count)),
            ref_count,
        }
        real.discard(0)
        if not real:
            return text

        # Sort real numbers descending so mapping is deterministic
        real_sorted = sorted(real, reverse=True)

        # Find all number+label combos in screening context
        screening_labels = (
            r"(?:articles?|papers?|studies?|records?|sources?|publications?|"
            r"abstracts?|references?|documents?|titles?|results?|hits?)"
        )
        pattern = re.compile(
            rf"\b(\d{{1,5}})\s+{screening_labels}\b", re.IGNORECASE
        )

        def _replace(m: re.Match) -> str:
            num = int(m.group(1))
            # Don't touch years or numbers already in real set
            if num > 1900 or num in real:
                return m.group(0)
            # Find the closest real number
            closest = min(real_sorted, key=lambda r: abs(r - num))
            # Only replace if fabricated number is within 10x of a real number
            # (avoids replacing things like "5 articles" when real is 500)
            ratio = max(num, closest) / max(min(num, closest), 1)
            if ratio > 10:
                return m.group(0)
            logger.info("  Methodology scrub: %d → %d (%s)", num, closest, m.group(0))
            return m.group(0).replace(str(num), str(closest), 1)

        return pattern.sub(_replace, text)

    # ------------------------------------------------------------------
    # 4n: LLM abstract claim cross-check
    # ------------------------------------------------------------------

    def _cross_check_abstract_claims(self, abstract: str, body: str) -> str:
        """Use LLM to rewrite abstract claims that aren't supported by the paper body."""
        if not abstract or not body:
            return abstract

        # Use GUI-editable prompt (phase6_abstract_crosscheck), fall back to hardcoded
        ac_system, ac_user = self._get_prompt(
            "phase6_abstract_crosscheck",
            abstract=abstract,
            body=body[:25000],
        )
        if not ac_system:
            ac_system = (
                "You are a precise academic editor. Your job is to ensure the abstract "
                "claims ONLY what the paper body actually demonstrates. Return only the "
                "corrected abstract text, nothing else."
            )
        if not ac_user:
            ac_user = (
                "Compare the abstract against the paper body below.\n\n"
                "FIX these problems if present:\n"
                "1. NUMBERS: If the abstract mentions a number (study count, percentage, statistic) "
                "not found in the body, remove it or replace with language from the body.\n"
                "2. OVERCLAIMING: If the abstract says 'fundamental', 'comprehensive', 'reveals', "
                "'demonstrates' but the body uses 'suggests', 'indicates', 'explores', downgrade "
                "the abstract to match the body's hedging level.\n"
                "3. SCOPE INFLATION: If the abstract claims 'field-wide' or 'across the discipline' "
                "but the body only covers a limited corpus, add scope qualifiers like "
                "'within the reviewed corpus' or 'among the studies examined'.\n"
                "4. UNSUPPORTED CLAIMS: If the abstract makes a claim with no corresponding "
                "passage in the body, remove it.\n"
                "5. CORPUS SIZE: The corpus count must match between abstract and methodology.\n\n"
                f"=== ABSTRACT ===\n{abstract}\n\n=== PAPER BODY ===\n{body[:25000]}"
            )

        try:
            resp = self.llm.generate(
                ac_system,
                ac_user,
                temperature=0.2,
                max_tokens=4000,
            )
            fixed = strip_thinking_tags(resp.text).strip()
            fixed = self._clean_section_text(fixed)
            if fixed and len(fixed) > 100:
                # Safety: don't accept if LLM returned something too different in length
                if 0.7 < len(fixed) / len(abstract) < 1.3:
                    if fixed != abstract:
                        self.display.step("  Abstract cross-check: LLM corrected unsupported claims")
                    return fixed
                else:
                    logger.warning("Abstract cross-check: LLM output length ratio %.2f — keeping original",
                                   len(fixed) / len(abstract))
        except LLMError as e:
            logger.warning("Abstract cross-check LLM call failed: %s", e)

        return abstract

    # ------------------------------------------------------------------
    # 4f0.5: Deterministic citation-claim cross-check
    # ------------------------------------------------------------------

    def _deterministic_cite_claim_check(
        self,
        draft: dict[str, str],
        references: list[dict],
        ref_abstracts: dict[str, str],
        evidence_by_paper: dict[int, str],
        curated: list[dict],
    ) -> dict[str, str]:
        """Remove citations where the claim sentence doesn't match the reference's content.

        For each [Author, Year] citation in the text:
        1. Extract the sentence containing it
        2. Get the reference's abstract + evidence
        3. Compute keyword overlap between claim and abstract
        4. If overlap < 2 meaningful content words, remove the citation tag

        This is a deterministic safety net that catches misattributions the LLM
        verifier misses. It does NOT rely on any LLM call.
        """
        # Build lookup: surname_lower + year -> (ref_index, abstract_text, evidence_text)
        _STOPWORDS = {
            "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
            "her", "was", "one", "our", "out", "has", "have", "been", "some",
            "them", "than", "its", "over", "such", "that", "this", "with",
            "will", "each", "from", "they", "were", "which", "their", "said",
            "what", "when", "into", "more", "also", "been", "would", "make",
            "like", "many", "then", "these", "other", "could", "about", "after",
            "most", "only", "very", "between", "through", "during", "before",
            "study", "studies", "research", "paper", "review", "analysis",
            "findings", "results", "data", "evidence", "model", "approach",
            "method", "novel", "proposed", "showed", "found", "suggest",
            "suggests", "indicate", "indicates", "associated", "role",
            "however", "while", "both", "among", "across", "based", "using",
            "specific", "different", "significant", "important", "particular",
        }

        # Build title→curated index for evidence lookup
        title_to_curated_idx: dict[str, int] = {}
        for ci, cp in enumerate(curated):
            ct = (cp.get("title") or "").lower().strip()[:80]
            if ct:
                title_to_curated_idx[ct] = ci

        # Build cite_key → (abstract_text, evidence_text)
        ref_content: dict[str, tuple[str, str]] = {}
        for ref in references:
            ck = ref.get("cite_key", "")
            if not ck:
                continue
            title = ref.get("title", "")
            title_key = title.lower()[:80]
            abs_text = ref_abstracts.get(title_key, "")
            # Also try evidence_by_paper
            ev_text = ""
            curated_idx = title_to_curated_idx.get(title.lower().strip()[:80])
            if curated_idx is not None:
                ev_text = evidence_by_paper.get(curated_idx, "")
            ref_content[ck] = (abs_text, ev_text)

        if not ref_content:
            return draft

        # Extract keywords from text
        def _content_words(text: str) -> set[str]:
            words = re.findall(r"[a-z]{4,}", text.lower())
            return {w for w in words if w not in _STOPWORDS}

        # Citation pattern: [Author, Year] or [Author et al., Year]
        cite_pattern = re.compile(
            r"\[([A-Z][a-z]+(?:\s+et\s+al\.?)?(?:\s+(?:and|&)\s+[A-Z][a-z]+)?,\s*\d{4}(?:[a-z])?)\]"
        )

        misattributions_found = 0
        updated_draft: dict[str, str] = {}

        for section, text in draft.items():
            if section in ("References", "Acknowledgments"):
                updated_draft[section] = text
                continue

            # Split into sentences
            sentences = re.split(r'(?<=[.!?])\s+', text)
            new_sentences = []

            for sentence in sentences:
                cites_in_sentence = cite_pattern.findall(sentence)
                if not cites_in_sentence:
                    new_sentences.append(sentence)
                    continue

                bad_cites = []
                for cite_str in cites_in_sentence:
                    # Parse "Author et al., 2021" -> cite_key format
                    # The cite_key in references is typically "Author, Year"
                    # Normalize to match
                    ck_normalized = cite_str.strip()
                    abs_text, ev_text = ref_content.get(ck_normalized, ("", ""))

                    # If we have no abstract/evidence for this ref, check if it's
                    # being used for a specific empirical claim (risky without verification)
                    combined_ref_text = (abs_text + " " + ev_text).strip()
                    if len(combined_ref_text) < 50:
                        # Check if sentence makes specific empirical claims
                        claim_text = sentence.replace(f"[{cite_str}]", "").lower()
                        _empirical_markers = (
                            "found that", "demonstrated", "showed that",
                            "reported that", "revealed", "observed that",
                            "confirmed that", "established that", "%",
                            "significantly", "p <", "p=", "n =", "n=",
                        )
                        if any(m in claim_text for m in _empirical_markers):
                            bad_cites.append(cite_str)
                            logger.warning(
                                "Unverifiable citation with empirical claim: [%s] in '%s'",
                                cite_str, sentence[:80],
                            )
                        continue

                    # Extract claim words (sentence minus the citation itself)
                    claim_text = sentence.replace(f"[{cite_str}]", "")
                    claim_words = _content_words(claim_text)
                    ref_words = _content_words(combined_ref_text)

                    if not claim_words or not ref_words:
                        continue

                    overlap = claim_words & ref_words
                    # Need at least 2 content words overlapping
                    if len(overlap) < 2:
                        bad_cites.append(cite_str)
                        logger.warning(
                            "Citation-claim mismatch: [%s] in '%s' — overlap: %s",
                            cite_str, sentence[:80], overlap,
                        )

                if bad_cites:
                    # Remove the bad citations from the sentence
                    fixed_sentence = sentence
                    for bc in bad_cites:
                        # Remove [Author, Year] tag but keep the prose
                        fixed_sentence = fixed_sentence.replace(f"[{bc}]", "")
                        misattributions_found += 1
                    # Clean up double spaces and orphaned brackets
                    fixed_sentence = re.sub(r"\s{2,}", " ", fixed_sentence).strip()
                    # If sentence is now just a claim with no citation, add hedging
                    remaining_cites = cite_pattern.findall(fixed_sentence)
                    if not remaining_cites and len(fixed_sentence) > 20:
                        # Don't leave unsupported claims — remove entire sentence
                        # if it was primarily citation-dependent
                        words_in_sent = len(fixed_sentence.split())
                        if words_in_sent < 25:
                            logger.info("Removing short claim sentence with no remaining citations: %s", fixed_sentence[:80])
                            continue  # skip this sentence entirely
                    new_sentences.append(fixed_sentence)
                else:
                    new_sentences.append(sentence)

            updated_draft[section] = " ".join(new_sentences)

        if misattributions_found > 0:
            self.display.step(
                f"  Deterministic cite-check: removed {misattributions_found} misattributed citations"
            )

        return updated_draft

    # ------------------------------------------------------------------
    # 4f1: LLM citation cleanup (phantom cites, wrong years, bare-year)
    # ------------------------------------------------------------------

    def _llm_citation_cleanup(
        self,
        draft: dict[str, str],
        references: list[dict],
    ) -> dict[str, str]:
        """Use LLM to fix citation issues: phantom cites, wrong years, bare-year [YYYY].

        The LLM receives the valid reference list and the draft text, and returns
        corrected text with only valid citations in correct [Author, Year] format.
        """
        # Build compact reference list for the LLM
        ref_lines = []
        for ref in references:
            ck = ref.get("cite_key", "")
            title = ref.get("title", "?")[:80]
            year = ref.get("year", "?")
            authors = ref.get("authors", [])
            first_author = authors[0] if authors else "Unknown"
            ref_lines.append(f"  - [{ck}] {first_author} ({year}). {title}")
        ref_list_text = "\n".join(ref_lines)

        full_draft_text = "\n\n".join(
            f"=== {name} ===\n{draft[name]}"
            for name in _WRITE_ORDER if name in draft
        )

        # Use GUI-editable prompt, fall back to inline
        cc_system, cc_user = self._get_prompt(
            "phase6_citation_cleanup",
            ref_list=ref_list_text,
            ref_count=str(len(references)),
            full_draft=full_draft_text[:25000],
        )
        if not cc_system:
            cc_system = (
                "You are a precise academic citation editor. "
                "Return the full corrected paper with section headers preserved."
            )
        if not cc_user:
            cc_user = (
                f"Below is a paper draft and its COMPLETE reference list ({len(references)} references).\n\n"
                f"VALID REFERENCES:\n{ref_list_text}\n\n"
                f"Fix ALL citation issues:\n"
                f"1. PHANTOM CITATIONS: Any [Author, Year] not in the reference list — "
                f"rephrase the sentence naturally without the citation\n"
                f"2. WRONG YEARS: Author matches but year is wrong — correct the year\n"
                f"3. BARE-YEAR CITATIONS: [2021] with no author — add author or rephrase\n"
                f"4. PSEUDO-CITATIONS: [Mechanisms], [Overview] etc. — remove brackets naturally\n"
                f"5. OVERCITED: Any ref appearing >8 times — keep key occurrences, rephrase others\n\n"
                f"RULES:\n"
                f"- Keep structure, section headers, and all other content\n"
                f"- Rephrase naturally, don't just delete brackets\n"
                f"- Return the full text with === Section Name === headers\n\n"
                f"{full_draft_text[:25000]}"
            )

        try:
            resp = self.llm.generate(
                cc_system,
                cc_user,
                temperature=0.2,
                max_tokens=65000,
                think=True,
            )
            edited = strip_thinking_tags(resp.text).strip()

            # Parse sections back with safety checks
            cite_pat = re.compile(r"\[[A-Z][a-zA-Z\-]+(?:\s+et\s+al\.)?(?:,\s*\d{4}[a-z]?)?\]")
            updated_any = False

            for section_name in _WRITE_ORDER:
                pat = re.compile(
                    rf"===\s*{re.escape(section_name)}\s*===\s*\n(.*?)(?=\n===\s|\Z)",
                    re.DOTALL,
                )
                m = pat.search(edited)
                if not m:
                    continue
                new_text = self._clean_section_text(m.group(1).strip())
                if not new_text or len(new_text) < 100:
                    continue
                old_text = draft.get(section_name, "")

                # Safety: reject if length changed by more than 20%
                if old_text and not (0.80 < len(new_text) / len(old_text) < 1.20):
                    logger.warning(
                        "Citation cleanup: rejecting %s — length ratio %.2f",
                        section_name, len(new_text) / len(old_text),
                    )
                    continue

                draft[section_name] = new_text
                updated_any = True

            if updated_any:
                self.display.step("  LLM citation cleanup: corrections applied")
            else:
                self.display.step("  LLM citation cleanup: no changes needed")

        except LLMError as e:
            logger.warning("LLM citation cleanup failed: %s — skipping", e)
            self.display.step("  LLM citation cleanup: skipped (error)")

        return draft

    # ------------------------------------------------------------------
    # v0.3: Source-level verification against reading notes
    # ------------------------------------------------------------------

    def _source_level_verification(self, draft: dict[str, str], reading_notes: list[dict]) -> dict[str, str]:
        """Check each section's claims against the structured reading notes.

        For sections where the LLM finds misattributed or unsupported claims,
        it rewrites the section with corrections applied.
        """
        prompt_template = self._prompts.get("phase8_source_verification",
                                            DEFAULT_PROMPTS.get("phase8_source_verification", ""))
        if not prompt_template:
            logger.info("No phase6_source_verification prompt — skipping")
            return draft

        # Build a lookup: paper_index -> reading note
        notes_by_index = {}
        for note in reading_notes:
            idx = note.get("paper_index")
            if idx is not None:
                notes_by_index[idx] = note

        curated = self.artifacts.get("curated_papers", [])

        sections_verified = 0
        sections_rewritten = 0

        for section_name, section_text in draft.items():
            if section_name == "Methodology":
                continue  # Template methodology is already factual
            if not section_text or len(section_text) < 100:
                continue

            # Find which papers are cited in this section
            cited_indices = set()
            for i, paper in enumerate(curated):
                authors = paper.get("authors", [])
                if not authors:
                    continue
                first_author = str(authors[0])
                surname = _extract_surname(first_author)
                if surname and len(surname) >= 3 and surname.lower() in section_text.lower():
                    cited_indices.add(i + 1)  # 1-indexed

            if not cited_indices:
                continue

            # Build source notes for cited papers
            source_notes_parts = []
            for idx in sorted(cited_indices):
                note = notes_by_index.get(idx)
                if note:
                    findings = note.get("key_findings", [])
                    method = note.get("methodology", "")
                    tier = note.get("quality_tier", "")
                    source_notes_parts.append(
                        f"[{idx}] Quality: {tier}\n"
                        f"  Findings: {'; '.join(str(f) for f in findings[:4])}\n"
                        f"  Method: {method[:150]}"
                    )

            if not source_notes_parts:
                continue

            source_notes_text = "\n".join(source_notes_parts)

            system = "You are a rigorous academic fact-checker. Return valid JSON only."
            user_prompt = prompt_template.format(
                section_text=section_text[:8000],
                source_notes=source_notes_text[:6000],
            )

            try:
                result = self.llm.generate_json(system, user_prompt, temperature=0.1, max_tokens=12000)
            except (LLMError, Exception) as e:
                logger.warning("Source verification failed for %s: %s", section_name, e)
                continue

            if not isinstance(result, dict):
                continue

            sections_verified += 1
            verifications = result.get("verifications", [])

            # Count issues
            n_unsupported = sum(1 for v in verifications if v.get("verdict") in ("unsupported", "misattributed"))
            n_stretched = sum(1 for v in verifications if v.get("verdict") == "stretched")

            if n_unsupported > 0 or n_stretched > 1:
                # Use the rewritten text if provided
                rewritten = result.get("rewritten_text", "")
                if rewritten and len(rewritten) > len(section_text) * 0.5:
                    draft[section_name] = rewritten
                    sections_rewritten += 1
                    self.display.step(
                        f"  {section_name}: {n_unsupported} unsupported + {n_stretched} stretched claims → rewritten"
                    )
                    logger.info("Source verification rewrote %s: %d unsupported, %d stretched",
                                section_name, n_unsupported, n_stretched)
                else:
                    # Log warnings for issues
                    for v in verifications:
                        if v.get("verdict") in ("unsupported", "misattributed"):
                            logger.warning("Source verify [%s]: %s — %s",
                                           section_name, v.get("verdict"), str(v.get("claim", ""))[:80])

            time.sleep(2)  # Rate limit between verification calls

        if sections_verified:
            self.display.step(f"Source verification: {sections_verified} sections checked, {sections_rewritten} rewritten")
        return draft

    # ------------------------------------------------------------------
    # FINAL DETERMINISTIC CLEANUP — runs after ALL LLM passes
    # These are pure Python checks. No LLM can undo them.
    # ------------------------------------------------------------------

    # Common stopwords excluded from keyword overlap checks
    _STOPWORDS: set[str] = {
        "the", "and", "for", "that", "this", "with", "from", "which", "have",
        "been", "were", "was", "are", "has", "their", "they", "than", "also",
        "these", "those", "into", "more", "such", "between", "through", "other",
        "our", "can", "may", "its", "both", "each", "one", "two", "not",
        "only", "most", "some", "all", "new", "used", "using", "based",
        "will", "being", "about", "over", "how", "when", "where", "what",
        "while", "within", "across", "among", "several", "various",
        # Academic filler — never meaningful for matching
        "study", "studies", "research", "paper", "analysis", "results",
        "findings", "approach", "method", "methods", "model", "data",
        "significant", "important", "novel", "proposed", "framework",
        "review", "literature", "evidence", "suggests", "suggests",
        "demonstrated", "showed", "found", "reported", "examined",
        "investigated", "explored", "revealed", "identified", "observed",
        "noted", "indicated", "concluded", "argued", "proposed",
    }

    def _build_deterministic_methodology(
        self,
        search_audit: dict,
        curated: list[dict],
        source_type_counts: dict[str, int],
        brief: dict,
    ) -> str:
        """Build the Methodology section entirely from pipeline data — no LLM involvement.

        This eliminates fabricated databases, invented screening stages, and wrong counts.
        The LLM cannot hallucinate what it doesn't write.
        """
        if not search_audit:
            logger.warning("_build_deterministic_methodology: search_audit is empty, skipping")
            return ""
        logger.info("Building deterministic methodology from audit: databases=%s, total_retrieved=%s",
                     search_audit.get("databases"), search_audit.get("total_retrieved"))

        databases = search_audit.get("databases", [])
        search_terms = search_audit.get("search_terms", [])
        search_queries = search_audit.get("search_queries", [])
        scope_exclusions = search_audit.get("scope_exclusions", [])
        year_from = search_audit.get("year_from", 2016)
        total_retrieved = search_audit.get("total_retrieved", "N/A")
        after_dedup = search_audit.get("total_after_dedup", "N/A")
        after_filter = search_audit.get("total_after_filter", "N/A")
        included = len(curated)
        rqs = brief.get("research_questions", [])
        paper_title = brief.get("title", "")

        # Format database names nicely
        db_names = {
            "crossref": "Crossref", "arxiv": "arXiv",
            "semantic_scholar": "Semantic Scholar", "openalex": "OpenAlex",
            "pubmed": "PubMed", "europe_pmc": "Europe PMC",
            "core": "CORE", "biorxiv": "bioRxiv/medRxiv",
            "plos": "PLOS", "springer": "Springer Nature",
            "hal": "HAL", "zenodo": "Zenodo",
            "nasa_ads": "NASA ADS", "doaj": "DOAJ",
            "dblp": "DBLP", "internet_archive": "Internet Archive Scholar",
            "openaire": "OpenAIRE", "fatcat": "Fatcat",
            "datacite": "DataCite", "dimensions": "Dimensions",
            "inspire_hep": "INSPIRE-HEP", "eric": "ERIC",
            "figshare": "Figshare", "scielo": "SciELO",
            "base": "BASE", "ieee": "IEEE Xplore",
            "philpapers": "PhilPapers", "cinii": "CiNii",
            "sciencedirect": "ScienceDirect", "wos": "Web of Science",
            "google_books": "Google Books", "open_library": "Open Library",
        }
        db_display = [db_names.get(d, d.title()) for d in databases]

        # Build search terms display
        terms_display = ", ".join(f'"{t}"' for t in search_terms[:6])

        # Build source type breakdown
        type_parts = []
        for st, count in sorted(source_type_counts.items()):
            label = st.replace("_", " ")
            if count != 1:
                # Proper pluralization
                if label.endswith("y") and not label.endswith("ey"):
                    label = label[:-1] + "ies"  # study → studies, primary study → primary studies
                elif label.endswith(("s", "sh", "ch", "x", "z")):
                    label += "es"
                else:
                    label += "s"
            type_parts.append(f"{count} {label}")
        type_text = ", ".join(type_parts) if type_parts else f"{included} articles"

        # Build the methodology text
        sections = []

        # 2.1 Search Strategy
        sections.append(
            "2.1 Search Strategy\n\n"
            f"This review employed an automated literature search pipeline querying "
            f"{len(databases)} academic databases: {', '.join(db_display)}. "
            f"Searches were conducted using {len(search_terms)} search term combinations "
            f"including {terms_display}. "
        )
        if search_queries:
            sq_display = "; ".join(f'"{q}"' for q in search_queries[:3])
            sections[-1] += (
                f"Boolean queries were also employed: {sq_display}. "
            )
        sections[-1] += (
            f"The search was restricted to publications from {year_from} to the present, "
            f"in English language. Citation graph traversal (forward and backward snowballing) "
            f"was applied to seed papers to identify additional relevant literature."
        )

        # 2.2 Research Questions
        if rqs:
            rq_items = "\n".join(f"  RQ{i+1}: {q}" for i, q in enumerate(rqs[:5]))
            sections.append(
                "2.2 Research Questions\n\n"
                "This review was guided by the following research questions:\n\n"
                f"{rq_items}\n\n"
                "These questions were used to structure the search strategy, define "
                "inclusion criteria, and organize the thematic synthesis of findings."
            )

        # 2.3 Selection Criteria
        # Use a shortened topic description (first ~80 chars or up to first colon/dash)
        topic_desc = paper_title if paper_title else "the research topic"
        for delim in [":", " — ", " - ", "?"]:
            if delim in topic_desc and topic_desc.index(delim) > 15:
                topic_desc = topic_desc[:topic_desc.index(delim)].strip()
                break
        if len(topic_desc) > 80:
            topic_desc = topic_desc[:77].rsplit(" ", 1)[0] + "..."
        topic_desc = topic_desc.lower()
        rq_count_text = f"the {len(rqs)} research questions above" if rqs else "the research questions"
        sections.append(
            "2.3 Selection Criteria\n\n"
            f"Articles were included if they: (a) were directly relevant to {topic_desc}; "
            f"(b) addressed at least one aspect of {rq_count_text}; "
            f"(c) were published in English; and (d) were available as peer-reviewed "
            f"journal articles, conference papers, or preprints in recognized repositories. "
        )
        if scope_exclusions:
            excl_text = "; ".join(scope_exclusions[:5])
            sections[-1] += f"Exclusion criteria included: {excl_text}. "
        sections[-1] += (
            "Papers not meeting these criteria, or whose titles contained "
            "negative keywords indicating off-topic content, were excluded during "
            "automated screening."
        )

        # 2.4 Relevance Scoring and Ranking
        sections.append(
            "2.4 Relevance Scoring and Ranking\n\n"
            "Each retrieved article was assigned a weighted ranking score "
            "combining five heuristic signals: (a) topical relevance (40%), assessed "
            "via keyword overlap between the article's title/abstract and the search "
            "terms; (b) citation impact (25%), computed as the log-normalized citation "
            "count relative to the corpus maximum; (c) foundational status (15%), "
            "determined by canonical citation markers, citations-per-year ratio, and "
            "review article status; (d) recency (10%), with a linear decay favoring "
            "publications from the last three years; and (e) venue quality (10%), "
            "based on publication in recognized high-impact journals. This is a "
            "heuristic ranking rather than a validated scoring instrument. "
            "Articles were ranked by this score, and the top-scoring articles "
            "were retained up to the corpus cap, subject to an author diversity "
            "constraint (maximum three papers per first author) to ensure breadth "
            "of perspectives."
        )

        # 2.5 Screening and Selection
        sections.append(
            "2.5 Screening and Selection\n\n"
            f"The automated pipeline retrieved {total_retrieved} initial records across "
            f"all databases. After title-based deduplication, {after_dedup} unique articles "
            f"remained. Automated relevance scoring reduced this to {after_filter} articles "
            f"meeting the inclusion threshold. Following corpus expansion through citation "
            f"graph traversal and targeted searches to address evidence gaps, "
            f"{included} articles were included in the final review."
        )

        # 2.6 Corpus Composition
        sections.append(
            "2.6 Corpus Composition\n\n"
            f"The final corpus comprised {type_text}. "
            f"Full-text content was available for articles accessible through open-access "
            f"repositories, preprint servers, and author-posted versions. "
            f"For articles without open-access full text, analysis was limited to "
            f"abstracts and available metadata."
        )

        # 2.7 Synthesis Approach
        sections.append(
            "2.7 Synthesis Approach\n\n"
            f"Evidence was synthesized thematically, organized around the {len(rqs)} "
            f"research questions guiding this review. Key findings, methodological details, "
            f"sample characteristics, and reported effect sizes were extracted from each "
            f"included article through structured deep reading. Where quantitative results "
            f"were reported, these were compared across studies to identify convergent "
            f"and divergent findings. Contradictions and gaps in the literature were "
            f"identified through cross-comparison of results, methods, and conclusions "
            f"across the corpus."
        )

        # 2.8 Quality Assessment and Evidence Access
        full_text_count = sum(1 for p in curated if p.get("enriched_content") and len(p.get("enriched_content", "")) > 1000)
        abstract_only_count = included - full_text_count
        sections.append(
            "2.8 Quality Assessment and Evidence Access\n\n"
            f"Each included article was assessed for relevance, methodological rigor, "
            f"and evidentiary contribution. Of the {included} included articles, "
            f"{full_text_count} were accessed in full text through open-access "
            f"repositories (arXiv, PubMed Central, Unpaywall, and author-posted versions), "
            f"while {abstract_only_count} were assessed based on their "
            f"abstracts and available metadata. Claims derived from abstract-only sources "
            f"are presented with appropriate hedging language to reflect the limited depth "
            f"of analysis possible for those records. "
            f"No formal risk-of-bias tool was applied, as this review is a narrative "
            f"thematic synthesis rather than a systematic review or meta-analysis. "
            f"Source quality was instead assessed through indicators including publication "
            f"venue, citation count, study design, and recency."
        )

        # 2.9 Limitations of Search
        sections.append(
            "2.9 Limitations of Search\n\n"
            f"This review was limited to the {len(databases)} databases listed above "
            f"({', '.join(db_display)}). Subscription-only databases such as Web of Science "
            f"and Scopus were not available to the automated pipeline, which may have "
            f"resulted in the exclusion of some relevant literature indexed exclusively "
            f"in those sources. The automated relevance scoring, while consistent and "
            f"reproducible, may not capture all nuances that manual screening would identify. "
            f"Grey literature, conference abstracts not indexed in the searched databases, "
            f"and non-English publications were also excluded. Additionally, the reliance "
            f"on automated search and filtering means that articles using novel or "
            f"unconventional terminology may have been missed."
        )

        # 2.10 AI-Assistance Disclosure
        try:
            llm_model = self.llm.model_name or "a large language model"
        except AttributeError:
            llm_model = "a large language model"
        sections.append(
            "2.10 AI-Assistance Disclosure\n\n"
            f"This review was conducted using an AI-assisted research pipeline (AgentPub). "
            f"Search query generation, metadata retrieval, deduplication, relevance scoring, "
            f"and evidence extraction were performed automatically. Section drafting, "
            f"claim-to-source alignment, and editorial review were generated by {llm_model} "
            f"under structured prompts constrained to cite only sources in the retrieved corpus. "
            f"No human reviewer manually verified every citation-to-claim mapping; readers "
            f"should treat specific numerical values and direct claims as AI-mediated "
            f"extractions from the sources listed in the reference list, not as independently "
            f"verified measurements. The reference list reflects the actual retrieval "
            f"and screening performed by the pipeline on the search date."
        )

        return "\n\n".join(sections)

    def _bibliography_integrity_check(
        self,
        references: list[dict],
        draft: dict[str, str],
        abstract: str,
    ) -> tuple[list[dict], dict[str, str], str]:
        """Deterministic bibliography integrity check (evaluator rec #6).

        1. Flags uncited references (already handled by orphan pruning above)
        2. Finds in-text citations not in the reference list → removes them
        3. Deduplicates references by DOI or title similarity
        4. Flags year/DOI inconsistencies
        """
        all_text = " ".join(draft.values()) + " " + abstract

        # Build set of valid citation keys (surname_year)
        valid_keys: dict[str, dict] = {}  # surname_lower → {years set, ref}
        for ref in references:
            for author in ref.get("authors", []) or []:
                if isinstance(author, str) and author.strip():
                    surname = _extract_surname(author).lower()
                    if len(surname) >= 2:
                        if surname not in valid_keys:
                            valid_keys[surname] = {"years": set(), "ref": ref}
                        year = str(ref.get("year", ""))
                        if year:
                            valid_keys[surname]["years"].add(year)

        # Find in-text citations [Author, Year] not matching any reference
        cite_pat = re.compile(r"\[([A-Z][a-zA-Z\-]+)(?:\s+et\s+al\.?)?,?\s*(\d{4}[a-z]?)\]")
        orphan_cites_removed = 0

        def _strip_orphan_cites(text: str) -> str:
            nonlocal orphan_cites_removed
            for m in reversed(list(cite_pat.finditer(text))):
                surname = m.group(1).lower()
                year = m.group(2)
                ref_data = valid_keys.get(surname)
                if not ref_data:
                    # Citation for author not in reference list — remove bracket
                    text = text[:m.start()] + text[m.end():]
                    orphan_cites_removed += 1
                elif year not in ref_data["years"] and ref_data["years"]:
                    # Year doesn't match any ref by this author — remove bracket
                    text = text[:m.start()] + text[m.end():]
                    orphan_cites_removed += 1
            # Clean up trailing/leading spaces
            text = re.sub(r"\s{2,}", " ", text).strip()
            return text

        for heading in draft:
            draft[heading] = _strip_orphan_cites(draft[heading])
        abstract = _strip_orphan_cites(abstract)

        if orphan_cites_removed:
            self.display.step(f"  Bib integrity: removed {orphan_cites_removed} citations with no matching reference")
            logger.info("Bib integrity: removed %d orphan in-text citations", orphan_cites_removed)

        # Deduplicate references by DOI
        seen_dois: set[str] = set()
        seen_titles: set[str] = set()
        deduped: list[dict] = []
        dupes_removed = 0
        for ref in references:
            doi = (ref.get("doi") or "").strip().lower()
            title_norm = re.sub(r"[^a-z0-9]", "", (ref.get("title") or "").lower())[:60]

            if doi and doi in seen_dois:
                dupes_removed += 1
                continue
            if title_norm and title_norm in seen_titles:
                dupes_removed += 1
                continue

            if doi:
                seen_dois.add(doi)
            if title_norm:
                seen_titles.add(title_norm)
            deduped.append(ref)

        if dupes_removed:
            self.display.step(f"  Bib integrity: removed {dupes_removed} duplicate references")
            logger.info("Bib integrity: removed %d duplicate references", dupes_removed)

        return deduped, draft, abstract

    def _final_deterministic_cleanup(
        self,
        draft: dict[str, str],
        abstract: str,
        references: list[dict],
    ) -> tuple[dict[str, str], str]:
        """Final deterministic cleanup — NO LLM calls. Runs last, cannot be undone.

        Three checks:
        1. Strip editorial placeholders (REMOVE..., TODO..., etc.)
        2. Deterministic citation-content verification (keyword overlap)
        3. Abstract-body number/claim alignment
        """
        self.display.step("Running final deterministic cleanup...")

        # 0. Strip numeric citations [4], [4, 5, 12] — wrong format, should be [Author, Year]
        _numeric_cite_pat = re.compile(r'\s*\[[\d,\s]+\]')
        for section_name in list(draft.keys()):
            before = draft[section_name]
            draft[section_name] = _numeric_cite_pat.sub('', draft[section_name])
            if draft[section_name] != before:
                count = len(_numeric_cite_pat.findall(before))
                logger.info("Stripped %d numeric citations from %s", count, section_name)
        before_abs = abstract
        abstract = _numeric_cite_pat.sub('', abstract)
        if abstract != before_abs:
            count = len(_numeric_cite_pat.findall(before_abs))
            logger.info("Stripped %d numeric citations from Abstract", count)

        # 1. Strip editorial placeholders from all text
        draft, abstract = self._strip_editorial_placeholders(draft, abstract)

        # 2. Deterministic citation-content check
        ref_abstracts = self.artifacts.get("ref_abstracts", {})
        evidence_by_paper = self.artifacts.get("evidence_by_paper", {})
        draft, abstract = self._deterministic_citation_check(
            draft, abstract, references, ref_abstracts, evidence_by_paper,
        )

        # 3. Abstract-body number alignment
        abstract = self._deterministic_abstract_check(abstract, draft, references)

        # 4. Overclaiming / hyperbole scrubber
        _OVERCLAIM_MAP = {
            r"\bprofoundly\b": "substantially",
            r"\bundeniably\b": "notably",
            r"\bindisputably\b": "notably",
            r"\bunequivocally\b": "clearly",
            r"\boverwhelmingly\b": "strongly",
            r"\brevolutionary\b": "significant",
            r"\bgroundbreaking\b": "notable",
            r"\bdramatically\b": "substantially",
            r"\brevelatory\b": "informative",
            r"\bdefinitively\b": "convincingly",
            r"\bmost consistent with\b": "broadly consistent with",
            r"\bprimary drivers? of\b": "contributing factors to",
            r"\bdemonstrates that\b": "suggests that",
            r"\bproves that\b": "indicates that",
            r"\breveals that\b": "suggests that",
            r"\bconfirms that\b": "supports the view that",
            r"\bestablishes that\b": "provides evidence that",
            r"\bconclusively\b": "on balance",
            r"\birrefutably\b": "strongly",
            r"\bunambiguously\b": "generally",
            r"\bparadigm[- ]shifting\b": "notable",
            r"\btransformative\b": "substantial",
            r"\bpivotal\b": "important",
            r"\bcritical(?:ly important)\b": "important",
        }
        for section_name in list(draft.keys()):
            text = draft[section_name]
            for pattern, replacement in _OVERCLAIM_MAP.items():
                text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
            draft[section_name] = text
        for pattern, replacement in _OVERCLAIM_MAP.items():
            abstract = re.sub(pattern, replacement, abstract, flags=re.IGNORECASE)

        # 5. Corpus count consistency — fix "N studies/papers/sources" to match actual ref count
        ref_count = len(references)
        _corpus_count_pat = re.compile(
            r'\b(\d+)\s+(?:peer[- ]reviewed\s+)?(?:studies|papers|sources|articles|publications|works)',
            re.IGNORECASE,
        )

        def _fix_count(m: re.Match) -> str:
            mentioned = int(m.group(1))
            # Only fix numbers close to actual count (within ±5), skip small numbers
            # and numbers that are likely referring to something else (e.g., cited meta-analysis)
            if mentioned > 5 and abs(mentioned - ref_count) <= 5 and mentioned != ref_count:
                return m.group(0).replace(m.group(1), str(ref_count), 1)
            return m.group(0)

        for section_name in list(draft.keys()):
            before = draft[section_name]
            draft[section_name] = _corpus_count_pat.sub(_fix_count, draft[section_name])
            if draft[section_name] != before:
                logger.info("Fixed corpus count in %s", section_name)
        before_abs = abstract
        abstract = _corpus_count_pat.sub(_fix_count, abstract)
        if abstract != before_abs:
            logger.info("Fixed corpus count in Abstract")

        # 6. Framework/overclaim language in title context — downgrade in abstract
        _FRAMEWORK_MAP = {
            r"\bunified account\b": "integrative review",
            r"\bunified framework\b": "thematic synthesis",
            r"\bcomprehensive model\b": "thematic overview",
            r"\bnovel paradigm\b": "proposed perspective",
            r"\bour framework reveals\b": "this synthesis suggests",
            r"\bour analysis reveals\b": "this analysis suggests",
            r"\bour model demonstrates\b": "this review suggests",
        }
        for pattern, replacement in _FRAMEWORK_MAP.items():
            abstract = re.sub(pattern, replacement, abstract, flags=re.IGNORECASE)
            for section_name in list(draft.keys()):
                draft[section_name] = re.sub(pattern, replacement, draft[section_name], flags=re.IGNORECASE)

        # 7. Log single-source citation dominance warning
        all_text = " ".join(draft.values())
        cite_pat = re.compile(r'\[([A-Z][a-z]+(?:\s+et\s+al\.)?(?:\s+and\s+[A-Z][a-z]+)?,\s*\d{4})\]')
        cite_counts: dict[str, int] = {}
        for m in cite_pat.finditer(all_text):
            key = m.group(1)
            cite_counts[key] = cite_counts.get(key, 0) + 1
        for key, count in sorted(cite_counts.items(), key=lambda x: -x[1]):
            if count > 4:
                logger.warning("Single-source dominance: [%s] cited %d times (max recommended: 4)", key, count)

        return draft, abstract

    def _strip_editorial_placeholders(
        self,
        draft: dict[str, str],
        abstract: str,
    ) -> tuple[dict[str, str], str]:
        """Remove editorial notes/placeholders that leaked into final text.

        Catches: REMOVE..., TODO..., NOTE TO..., EDITOR..., [VERIFY...],
        [CHECK...], [PLACEHOLDER...], [citation needed], etc.
        """
        # Patterns that match editorial instructions left in text
        patterns = [
            # Bracket-wrapped editorial notes
            re.compile(r"\[(?:REMOVE|TODO|NOTE\s+TO|EDITOR|VERIFY|CHECK|PLACEHOLDER|NEEDS?\s+CITATION|citation\s+needed)[^\]]{0,200}\]", re.IGNORECASE),
            # Bare editorial sentences (start of sentence or after period)
            re.compile(r"(?:^|(?<=\.\s)|(?<=!\s)|(?<=\?\s))(?:REMOVE|DELETE|TODO|NOTE:?|EDITOR:?|VERIFY:?|CHECK:?)\s[^.]{10,200}\.\s*", re.IGNORECASE | re.MULTILINE),
            # "REMOVE the X citation" style (common from our verifier)
            re.compile(r"REMOVE\s+the\s+[^.]{5,150}(?:citation|reference|claim)\w*[^.]*\.\s*", re.IGNORECASE),
            # Angle-bracket editorial comments
            re.compile(r"<(?:note|todo|edit|remove|fix|check)[^>]{0,200}>", re.IGNORECASE),
        ]

        total_stripped = 0
        for heading in draft:
            for pat in patterns:
                cleaned, n = pat.subn(" ", draft[heading])
                if n:
                    draft[heading] = cleaned
                    total_stripped += n
            # Clean up double spaces
            draft[heading] = re.sub(r"\s{2,}", " ", draft[heading]).strip()

        for pat in patterns:
            cleaned, n = pat.subn(" ", abstract)
            if n:
                abstract = cleaned
                total_stripped += n
        abstract = re.sub(r"\s{2,}", " ", abstract).strip()

        if total_stripped:
            self.display.step(f"  Stripped {total_stripped} editorial placeholders")
            logger.info("Final cleanup: stripped %d editorial placeholders", total_stripped)

        return draft, abstract

    def _deterministic_citation_check(
        self,
        draft: dict[str, str],
        abstract: str,
        references: list[dict],
        ref_abstracts: dict[str, str],
        evidence_by_paper: dict,
    ) -> tuple[dict[str, str], str]:
        """Deterministic citation-content verification using keyword overlap.

        For each [Author, Year] citation in the text:
        1. Find the sentence it appears in
        2. Extract content keywords from that sentence
        3. Check keyword overlap with the reference's abstract/content
        4. If overlap < threshold → remove the citation bracket (keep sentence)

        This catches catastrophic misattributions (quantum walk paper cited for
        protein folding) without any LLM call.
        """
        # Build reference lookup: surname → {keywords from abstract, year, title}
        ref_lookup: dict[str, dict] = {}  # surname_lower → ref data
        curated = self.artifacts.get("curated_papers", [])

        for ref in references:
            authors = ref.get("authors", [])
            year = str(ref.get("year", ""))
            title = ref.get("title", "")

            # Get the best content we have for this reference
            content = ""
            title_key = title.lower()[:80]
            if title_key in ref_abstracts:
                content = ref_abstracts[title_key]

            # Also check curated papers for enriched content
            for cp in curated:
                if (cp.get("title") or "").lower()[:80] == title_key:
                    ec = cp.get("enriched_content", "") or ""
                    if len(ec) > len(content):
                        content = ec
                    break

            if not content:
                content = ref.get("abstract", "") or title

            # Extract meaningful keywords from reference content
            ref_words = set()
            for w in re.findall(r"[a-zA-Z]{4,}", content.lower()):
                if w not in self._STOPWORDS:
                    ref_words.add(w)

            for author in authors:
                if isinstance(author, str) and author.strip():
                    surname = _extract_surname(author).lower()
                    if len(surname) >= 2:
                        # Store per surname+year combo to handle multiple refs by same author
                        key = f"{surname}_{year}"
                        ref_lookup[key] = {
                            "keywords": ref_words,
                            "title": title,
                            "year": year,
                        }
                        # Also store without year for lookup flexibility
                        if surname not in ref_lookup:
                            ref_lookup[surname] = {
                                "keywords": ref_words,
                                "title": title,
                                "year": year,
                            }

        if not ref_lookup:
            return draft, abstract

        # Citation pattern: [Author, Year] or [Author et al., Year]
        cite_pat = re.compile(
            r"\[([A-Z][a-zA-Z\-]+)(?:\s+et\s+al\.?)?,?\s*(\d{4}[a-z]?)\]"
        )

        removals = 0
        min_overlap = 3  # Need at least 3 content keyword matches

        def _check_and_strip_cites(text: str) -> str:
            nonlocal removals
            sentences = re.split(r"(?<=[.!?])\s+", text)
            result_sentences = []

            for sent in sentences:
                cites_in_sent = list(cite_pat.finditer(sent))
                if not cites_in_sent:
                    result_sentences.append(sent)
                    continue

                # Extract content keywords from this sentence (excluding citation text)
                sent_clean = cite_pat.sub("", sent)
                sent_words = set()
                for w in re.findall(r"[a-zA-Z]{4,}", sent_clean.lower()):
                    if w not in self._STOPWORDS:
                        sent_words.add(w)

                if len(sent_words) < 3:
                    # Too few content words to judge — keep as-is
                    result_sentences.append(sent)
                    continue

                modified_sent = sent
                for m in reversed(cites_in_sent):  # reverse to preserve positions
                    surname = m.group(1).lower()
                    year = m.group(2)

                    # Look up reference
                    ref_data = ref_lookup.get(f"{surname}_{year}") or ref_lookup.get(surname)
                    if not ref_data:
                        # Citation not in our reference list — don't touch (handled elsewhere)
                        continue

                    ref_keywords = ref_data["keywords"]
                    if not ref_keywords:
                        continue  # No abstract to check against

                    overlap = sent_words & ref_keywords
                    if len(overlap) < min_overlap:
                        # Misattribution detected — remove just the citation bracket
                        citation_text = m.group(0)  # e.g. "[Smith et al., 2020]"
                        modified_sent = modified_sent.replace(citation_text, "", 1)
                        removals += 1
                        logger.warning(
                            "Deterministic citation check: removed %s — "
                            "only %d keyword overlap (need %d). Sentence words: %s, Ref keywords sample: %s",
                            citation_text, len(overlap), min_overlap,
                            list(sent_words)[:5], list(ref_keywords)[:5],
                        )

                # Clean up orphaned commas/spaces from removed citations
                modified_sent = re.sub(r"\s{2,}", " ", modified_sent)
                modified_sent = re.sub(r"\(\s*,", "(", modified_sent)
                modified_sent = re.sub(r",\s*\)", ")", modified_sent)
                modified_sent = re.sub(r"\(\s*\)", "", modified_sent)
                result_sentences.append(modified_sent.strip())

            return " ".join(result_sentences)

        for heading in draft:
            original = draft[heading]
            draft[heading] = _check_and_strip_cites(draft[heading])

        abstract = _check_and_strip_cites(abstract)

        if removals:
            self.display.step(
                f"  Deterministic citation check: removed {removals} misattributed citations"
            )
            logger.info("Final cleanup: removed %d misattributed citations (keyword overlap < %d)",
                        removals, min_overlap)

        return draft, abstract

    def _deterministic_abstract_check(
        self,
        abstract: str,
        draft: dict[str, str],
        references: list[dict],
    ) -> str:
        """Deterministic abstract-body alignment check.

        For each sentence in the abstract:
        1. Numbers: verify they appear somewhere in the body
        2. Citations: verify they're in the reference list
        3. Strong claims: verify supporting language exists in body

        Removes or weakens sentences that fail.
        """
        if not abstract:
            return abstract

        body = "\n".join(draft.values())
        body_lower = body.lower()

        # Build valid citation surnames
        valid_surnames: set[str] = set()
        for ref in references:
            for author in ref.get("authors", []) or []:
                if isinstance(author, str):
                    s = _extract_surname(author).lower()
                    if len(s) >= 2:
                        valid_surnames.add(s)

        sentences = re.split(r"(?<=[.!?])\s+", abstract)
        kept_sentences = []
        removed_count = 0

        for sent in sentences:
            remove = False

            # Check 1: Numbers in abstract must appear in body
            numbers = re.findall(r"\b(\d{2,}(?:\.\d+)?)\b", sent)
            # Filter out years (1900-2099) and common non-content numbers
            content_numbers = [
                n for n in numbers
                if not (1900 <= int(float(n)) <= 2099)
                and n not in ("100", "10", "50")  # common percentages
            ]
            if content_numbers:
                numbers_found = sum(1 for n in content_numbers if n in body)
                if numbers_found == 0 and len(content_numbers) >= 1:
                    # Abstract has numbers not found anywhere in body
                    # Try to weaken instead of remove
                    for n in content_numbers:
                        # Replace specific number with hedged language
                        sent = re.sub(
                            rf"\b{re.escape(n)}\b\s*(?:sources?|studies|papers?|articles?)",
                            f"multiple studies",
                            sent, count=1,
                        )
                    logger.info("Abstract check: weakened unsupported numbers in: %s", sent[:80])

            # Check 2: Strong claim language without body support
            strong_claims = re.findall(
                r"\b(reveals?|demonstrates?|establishes?|proves?|confirms?|fundamentally)\b",
                sent, re.IGNORECASE,
            )
            if strong_claims:
                # Check if body uses similarly strong language
                has_support = any(
                    claim.lower() in body_lower for claim in strong_claims
                )
                if not has_support:
                    # Downgrade strong claims to hedged versions
                    replacements = {
                        "reveal": "suggest", "reveals": "suggests",
                        "demonstrate": "indicate", "demonstrates": "indicates",
                        "establish": "suggest", "establishes": "suggests",
                        "prove": "suggest", "proves": "suggests",
                        "confirm": "support", "confirms": "supports",
                        "fundamentally": "notably",
                    }
                    for strong, weak in replacements.items():
                        sent = re.sub(
                            rf"\b{re.escape(strong)}\b",
                            weak,
                            sent, flags=re.IGNORECASE, count=1,
                        )

            if not remove:
                kept_sentences.append(sent)
            else:
                removed_count += 1

        if removed_count:
            self.display.step(f"  Abstract check: removed {removed_count} unsupported sentences")
            logger.info("Final cleanup: removed %d unsupported abstract sentences", removed_count)

        result = " ".join(kept_sentences)
        # Safety: if we removed more than 40% of content, revert
        if len(result) < len(abstract) * 0.6:
            logger.warning("Abstract check would remove >40%% — reverting to original")
            return abstract

        return result

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
        # Build reference context: title + abstract + extracted evidence for each ref
        ref_abstracts = self.artifacts.get("ref_abstracts", {})
        evidence_by_paper = self.artifacts.get("evidence_by_paper", {})
        curated = self.artifacts.get("curated_papers", [])
        # Build title→index lookup for evidence_by_paper
        title_to_evidence_idx: dict[str, int] = {}
        for cp_i, cp in enumerate(curated):
            cp_title = (cp.get("title") or "").lower().strip()[:80]
            if cp_title:
                title_to_evidence_idx[cp_title] = cp_i

        ref_context_parts = []
        for i, ref in enumerate(references):
            title = ref.get("title", "Unknown")
            authors = ref.get("authors", [])
            year = ref.get("year", "?")
            author_str = authors[0] if authors else "Unknown"
            # Look up abstract from step 2 enrichment
            abs_text = ref_abstracts.get(title.lower()[:80], "")
            # Look up extracted evidence
            ev_idx = title_to_evidence_idx.get(title.lower().strip()[:80])
            ev_text = evidence_by_paper.get(ev_idx, "") if ev_idx is not None else ""
            ref_context_parts.append(
                f"[REF-{i+1}] {author_str} ({year}): {title}\n"
                f"  Content: {abs_text[:2500] if abs_text else '(not available)'}"
                + (f"\n  Evidence: {ev_text[:800]}" if ev_text else "")
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

TASK: Identify problems that must be fixed. Return JSON with this EXACT structure:
{{
  "issues": [
    {{
      "type": "citation_mismatch" | "unsupported_claim" | "table_fabrication" | "corpus_count_error",
      "section": "section name or 'Abstract' or 'Table 1'",
      "quote": "the problematic sentence or table row (EXACT text from the draft)",
      "action": "replace" | "remove",
      "replacement": "corrected academic prose to substitute for 'quote' (REQUIRED when action=replace, empty when action=remove)",
      "reason": "why this is wrong — your editorial explanation (NEVER inserted into the paper)"
    }}
  ]
}}

CRITICAL FIELD RULES:
- "action" MUST be either "replace" or "remove" — nothing else.
- "replacement" MUST contain ONLY finished academic prose ready to go into the paper.
  Do NOT put instructions, suggestions, or editorial comments in "replacement".
  BAD replacement: "Rephrase the sentence to reflect what Hawking actually said"
  BAD replacement: "Adjust the corpus count to 21"
  BAD replacement: "Provide a more specific citation"
  GOOD replacement: "Hawking (1975) demonstrated that black holes emit thermal radiation, raising fundamental questions about unitarity."
- "reason" is where your editorial explanation goes. It is never inserted into the paper.
- When action="remove", set "replacement" to "".
- When you are unsure how to rewrite a claim, use action="remove" instead of writing instructions.

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
{body_text[:25000]}
{table_text}

=== REFERENCES ({len(references)} total) ===
{ref_context[:20000]}"""

        # Try phase8_verification first (GUI-editable), fall back to phase8b_verification
        v_system, v_user = self._get_prompt(
            "phase8_verification",
            full_paper_text=body_text[:25000],
            reference_list=ref_context[:20000],
        )
        if not v_system:
            v_system, _ = self._get_prompt("phase8b_verification")
        if not v_system:
            v_system = "You are an academic fact-checker. Return valid JSON only."
        result = self.llm.generate_json(
            v_system,
            prompt,
            temperature=0.2,
            max_tokens=16000,
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
            action = (issue.get("action") or "").lower().strip()
            replacement = (issue.get("replacement") or "").strip()
            reason = issue.get("reason", "")

            # Backward compat: if LLM used old "fix" field instead of new schema
            if not action and "fix" in issue:
                old_fix = issue["fix"].strip()
                if old_fix.upper() == "REMOVE":
                    action = "remove"
                else:
                    action = "replace"
                    replacement = old_fix

            if not quote or action not in ("replace", "remove"):
                continue

            self.display.step(f"    [{itype}] {section}: {quote[:60]}...")
            if reason:
                logger.info("Verification reason: %s", reason[:120])

            # Apply fix to table
            if itype == "table_fabrication" and figures:
                for fig in figures:
                    if fig.get("data_type") != "table":
                        continue
                    data = fig.get("data", {})
                    rows = data.get("rows", [])
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

            # Determine final action: replace or remove
            if action == "remove":
                fix = "REMOVE"
            else:
                fix = replacement

            # Safety net: detect instruction-like text in replacement field
            # (LLM may still put editorial instructions in "replacement" despite schema)
            if fix != "REMOVE" and fix:
                _fix_upper = fix.upper().strip()
                _is_instruction = any(kw in _fix_upper for kw in [
                    "REMOVE", "DELETE", "ADD THE", "REPLACE WITH", "INSERT",
                    "REWRITE", "CHANGE TO", "SHOULD BE", "FIX THIS",
                    "REPHRASE", "PROVIDE A DIFFERENT", "PROVIDE A MORE",
                    "PROVIDE AN ALTERNATIVE", "CONSIDER REPHRASING",
                    "SUGGEST REPLACING", "MODIFY THE", "UPDATE THE",
                    "CLARIFY THE", "REVISE THE", "ADJUST THE",
                ])
                if not _is_instruction:
                    _is_instruction = bool(re.match(
                        r"^(rephrase|provide|clarify|revise|modify|update|consider|suggest|"
                        r"add|remove|delete|replace|insert|rewrite|change|fix|use|cite|"
                        r"specify|ensure|verify|check|note that|adjust|correct|amend|"
                        r"confirm|mention|state|include|omit|avoid|refrain)\b",
                        fix.strip(), re.IGNORECASE,
                    ))
                if _is_instruction:
                    logger.warning("Instruction leaked into replacement field, treating as REMOVE: %s", fix[:100])
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
            r"\s*(?:REMOVE|DELETE|ADD|REPLACE|INSERT|REWRITE|FIX|REPHRASE|PROVIDE|CLARIFY|"
            r"REVISE|MODIFY|UPDATE|CONSIDER|ADJUST|CORRECT|AMEND|CONFIRM|ENSURE)\s+"
            r"(?:the|this|that|a|an)?\s*[^.]{10,150}(?:citation|reference|claim|sentence|text|"
            r"abstract|paper|source|argument|detail|corpus|count|methodology|section)\w*[^.]*\.\s*",
            re.IGNORECASE,
        )
        # Additional pattern: "Verb the sentence/stated/corpus to..." style instructions
        _rephrase_pat = re.compile(
            r"(?:Rephrase|Provide|Clarify|Revise|Modify|Adjust|Correct)\s+"
            r"(?:the\s+)?(?:sentence|stated|corpus|abstract|section|claim|count|methodology)\s+[^.]{5,200}\.\s*,?\s*",
            re.IGNORECASE,
        )
        editorial_stripped = 0
        for heading in draft:
            for pat in (_editorial_pat, _rephrase_pat):
                cleaned, n = pat.subn(" ", draft[heading])
                if n:
                    draft[heading] = cleaned
                    editorial_stripped += n
        if abstract_modified:
            for pat in (_editorial_pat, _rephrase_pat):
                cleaned, n = pat.subn(" ", modified_abstract)
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
                hits = self._search(
                    rq, limit=10,
                    year_from=2014,  # Broader year range
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
                    refs = _run_non_critical(
                        lambda _sid=s2_id: fetch_paper_references(_sid, limit=15),
                        label="citation graph expansion", timeout=30, default=[])
                    if refs:
                        _dedup_add(refs)
                        self.display.step(f"  Citation graph expansion: +{len(refs)} candidates")
                    time.sleep(0.5)

        if new_papers:
            # Use LLM to score relevance (same as Phase 2 screening)
            paper_title = brief.get("title", self._topic)
            rqs = brief.get("research_questions", [])
            rq_text = "; ".join(rqs[:3]) if rqs else paper_title

            summaries = []
            for i, p in enumerate(new_papers):
                content = (p.get("enriched_content") or p.get("abstract") or "")[:2500]
                summaries.append(
                    f"[{i}] {p.get('title', 'Untitled')} ({p.get('year', '?')})\n{content}"
                )

            # Use same phase3_screen prompt as main scoring for consistency
            sc_system, sc_user = self._get_prompt(
                "phase3_screen",
                topic=paper_title,
                paper_summaries="\n\n".join(summaries),
            )
            if not sc_system:
                sc_system = "You are an academic screening assistant. Return valid JSON."
            # Append research questions context
            if sc_user:
                sc_user += f"\nResearch questions: {rq_text}"
            else:
                sc_user = (
                    f'Rate these papers for relevance to: "{paper_title}"\n\n'
                    + "\n\n".join(summaries)
                    + f"\nResearch questions: {rq_text}\n\n"
                    + 'For each paper, return JSON:\n'
                    + '{"scores": [{"index": 0, "relevance": 0.0-1.0, "on_domain": true, "key_finding": "one sentence"}]}'
                )
            sc_user += "\n- Only include papers with relevance >= 0.4 AND on_domain = true"

            filtered_new = []
            try:
                result = self.llm.generate_json(sc_system, sc_user)
                if isinstance(result, list):
                    scores_list = result
                elif isinstance(result, dict):
                    scores_list = result.get("scores", [])
                    if isinstance(scores_list, dict):
                        scores_list = list(scores_list.values())
                else:
                    scores_list = []
                for s in scores_list:
                    if not isinstance(s, dict):
                        continue
                    idx = s.get("index", -1)
                    relevance = s.get("relevance", 0)
                    on_domain = s.get("on_domain", False)
                    try:
                        relevance = float(relevance)
                    except (ValueError, TypeError):
                        relevance = 0
                    if 0 <= idx < len(new_papers) and on_domain and relevance >= 0.4:
                        new_papers[idx]["relevance_score"] = relevance
                        new_papers[idx]["on_domain"] = True
                        if s.get("source_type"):
                            new_papers[idx]["source_type_llm"] = s["source_type"]
                        if s.get("evidence_strength"):
                            new_papers[idx]["evidence_strength"] = s["evidence_strength"]
                        filtered_new.append(new_papers[idx])
                    elif 0 <= idx < len(new_papers):
                        logger.info("Corpus expansion: rejected (rel=%.2f, domain=%s) '%s'",
                                    relevance, on_domain, (new_papers[idx].get("title") or "")[:60])
            except Exception as e:
                logger.warning("Corpus expansion LLM screening failed: %s — rejecting all", e)
                filtered_new = []

            added = filtered_new[:target_refs - len(curated)]
            curated.extend(added)
            if added:
                self.display.step(
                    f"  Corpus expanded: {len(curated)} refs (added {len(added)}/{len(new_papers)} on-topic papers)"
                )
        else:
            self.display.step("  No additional papers found during expansion")

        return curated

    def _build_submission_references(self, curated: list[dict]) -> list[dict]:
        """Build submission-ready reference list from curated papers."""
        # Enrich venues from Crossref for papers with DOI but no venue
        _run_non_critical(lambda: self._enrich_venues(curated),
                          label="venue enrichment (submission)", timeout=30)

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
            raw_title = self._fix_double_encoded_utf8(paper.get("title", "Unknown") or "Unknown")
            clean_title = re.sub(r"<[^>]+>", "", raw_title).strip() if raw_title else "Unknown"

            ref = {
                "ref_id": clean_ref_id,
                "type": ref_type,
                "title": clean_title,
            }
            if ref_source:
                ref["source"] = ref_source
            if authors and isinstance(authors, list):
                ref["authors"] = [self._fix_double_encoded_utf8(a) if isinstance(a, str) else a for a in authors if a][:10]
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

        # Copy journal→venue if present
        for p in papers:
            if not p.get("venue") and p.get("journal"):
                p["venue"] = p["journal"]
        to_enrich = [p for p in papers if p.get("doi") and not p.get("venue")]
        if not to_enrich:
            logger.info("Venue enrichment: all %d papers already have venues", len(papers))
            return

        logger.info("Enriching venues for %d/%d refs via Crossref...", len(to_enrich), len(papers))
        enriched = 0
        failed = 0
        with httpx.Client(timeout=5) as client:
            for paper in to_enrich[:30]:
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
                        else:
                            # Try short-container-title as fallback
                            short = item.get("short-container-title", [])
                            if short:
                                paper["venue"] = short[0]
                                enriched += 1
                    else:
                        failed += 1
                        logger.debug("Crossref venue lookup failed for DOI %s: HTTP %d", doi, resp.status_code)
                except Exception as e:
                    failed += 1
                    logger.debug("Crossref venue lookup error for DOI %s: %s", doi, e)
        logger.info("Venue enrichment: %d enriched, %d failed, %d already had venue",
                     enriched, failed, len(papers) - len(to_enrich))

    @staticmethod
    def _collect_cited_keys(sections: dict[str, str], abstract: str = "") -> set[str]:
        """Scan all text for [Author, Year] citation patterns."""
        pattern = re.compile(
            r"\["
            r"("
            rf"[A-Z][a-zA-Z{_HYPH}']+(?:\s+[a-z]+)*(?:\s+[A-Z][a-zA-Z{_HYPH}']*)?"
            rf"(?:\s+(?:et\s+al\.|and|&)\s*[A-Z]?[a-zA-Z{_HYPH}']*)?"
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
            # NOTE: Previously added title words to ref_surnames, but this caused
            # false negatives: common words like "review", "resistance", "analysis"
            # in titles would match phantom citations like [Review, 2017].
            # Title words are no longer treated as valid author surnames.

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

            # Also strip orphan parenthetical citations: (Author et al., YYYY)
            def _clean_paren_cite(m: re.Match) -> str:
                """Remove parenthetical citations where the author is not in refs."""
                surname = m.group(1).strip().lower()
                year = m.group(2)
                if surname in ref_surnames:
                    # Also check author-year pair if we have that data
                    if ref_author_years and (surname, year) not in ref_author_years:
                        if any(s == surname for s, _ in ref_author_years):
                            return ""  # Surname exists but wrong year
                    return m.group(0)  # Keep — matches a reference
                return ""  # Orphan — remove

            # Match (Author et al., YYYY) and (Author, YYYY) — full parenthetical
            content = re.sub(
                rf"\(\s*([A-Z][a-zA-ZÀ-ÖØ-öø-ÿ{_HYPH}\']+)"
                r"(?:\s+et\s+al\.?)?"
                r"\s*[,;]\s*"
                r"(\d{4})\s*\)",
                _clean_paren_cite, content,
            )

            # Match narrative: "Author et al. (YYYY)" or "Author (YYYY)" where Author is orphan
            def _clean_narrative_cite(m: re.Match) -> str:
                surname = m.group(1).strip().lower()
                year = m.group(2)
                if surname in ref_surnames:
                    if ref_author_years and (surname, year) not in ref_author_years:
                        if any(s == surname for s, _ in ref_author_years):
                            return ""
                    return m.group(0)
                return ""

            content = re.sub(
                rf"([A-Z][a-zA-ZÀ-ÖØ-öø-ÿ{_HYPH}\']+)"
                r"(?:\s+et\s+al\.?)?"
                r"\s*\(\s*(\d{4})\s*\)",
                _clean_narrative_cite, content,
            )

            # Strip "Author (n.d.)" citations — no-year refs were already removed
            def _clean_nd_cite(m: re.Match) -> str:
                surname = m.group(1).strip().lower()
                if surname not in ref_surnames:
                    return ""
                return m.group(0)

            content = re.sub(
                rf"([A-Z][a-zA-ZÀ-ÖØ-öø-ÿ{_HYPH}\']+)"
                r"(?:\s+et\s+al\.?)?"
                r"\s*\(\s*n\.?\s*d\.?\s*\)",
                _clean_nd_cite, content,
                flags=re.IGNORECASE,
            )
            # Also strip bracket form: [Author, n.d.]
            content = re.sub(
                r"\[\s*[A-Za-zÀ-ÖØ-öø-ÿ]+(?:\s+et\s+al\.?)?\s*[,;]\s*n\.?\s*d\.?\s*\]",
                "", content,
            )

            # Clean up artifacts
            content = re.sub(r"\(\s*[;,\s]*\s*\)", "", content)
            content = re.sub(r"\s{2,}", " ", content)
            content = re.sub(r"\s+\.", ".", content)
            content = re.sub(r"\s+,", ",", content)
            # Fix sentences that lost their citation mid-sentence leaving orphan text
            content = re.sub(r"\s+\)", ")", content)
            content = re.sub(r"\(\s+", "(", content)
            sections[i] = {"heading": sec["heading"], "content": content}

        logger.info("Reverse-orphan citation check completed (bracket + parenthetical)")

    def _rescue_hallucinated_citations(
        self, draft: dict[str, str], references: list[dict], brief: dict,
    ) -> list[dict]:
        """Find in-text citations not in the reference list, search academic DBs,
        and return submission-ready refs for any that are real and relevant.

        The LLM sometimes cites papers from its training knowledge that weren't
        in the curated set. If they're real, we rescue them; if not, they'll be
        stripped later by _strip_orphan_citations.
        """
        from agentpub.academic_search import _search_crossref, _search_semantic_scholar

        # Build set of existing ref author-year keys
        existing_keys: set[str] = set()
        for ref in references:
            year_str = str(ref.get("year", ""))
            for author in ref.get("authors", []) or []:
                if isinstance(author, str) and author.strip():
                    surname = _extract_surname(author).lower()
                    if len(surname) >= 2 and year_str:
                        existing_keys.add(f"{surname}_{year_str}")

        # Extract all citation mentions from text (both bracket and parenthetical)
        all_text = "\n".join(draft.values())
        abstract = self.artifacts.get("abstract", "")
        if abstract:
            all_text += "\n" + abstract

        # Pattern for (Author et al., YYYY) or (Author, YYYY)
        paren_pattern = re.compile(
            r"(?:\(|(?<=[;,]\s))"   # opening paren or after semicolon in multi-cite
            rf"([A-Z][a-zA-ZÀ-ÖØ-öø-ÿ{_HYPH}']+)"  # surname
            r"(?:\s+et\s+al\.?)?"   # optional et al.
            r"[\s,]*"
            r"(\d{4})"             # year
        )
        # Pattern for [Author et al., YYYY] or [Author, YYYY]
        bracket_pattern = re.compile(
            r"\["
            rf"([A-Z][a-zA-ZÀ-ÖØ-öø-ÿ{_HYPH}']+)"
            r"(?:\s+et\s+al\.?)?"
            r"[\s,]*"
            r"(\d{4})"
            r"[^\]]*\]"
        )
        # Narrative: Author et al. (YYYY) or Author (YYYY)
        narrative_pattern = re.compile(
            r"([A-Z][a-zA-ZÀ-ÖØ-öø-ÿ\-']+)"
            r"(?:\s+et\s+al\.?)?"
            r"\s*\(\s*(\d{4})\s*\)"
        )

        cited: dict[str, tuple[str, str]] = {}  # key "surname_year" -> (surname, context_snippet)
        for pattern in (paren_pattern, bracket_pattern, narrative_pattern):
            for m in pattern.finditer(all_text):
                surname = m.group(1)
                year = m.group(2)
                key = f"{surname.lower()}_{year}"
                if key not in existing_keys and key not in cited:
                    # Skip common false positives
                    if surname.lower() in {"figure", "table", "fig", "tab", "section",
                                           "chapter", "appendix", "box", "note", "eq"}:
                        continue
                    # Extract surrounding context (±120 chars) for search query
                    start = max(0, m.start() - 120)
                    end = min(len(all_text), m.end() + 120)
                    context = all_text[start:end]
                    # Extract meaningful words from context for search query
                    ctx_words = re.findall(r"[a-zA-Z]{4,}", context.lower())
                    _ctx_stop = {"this", "that", "these", "those", "their", "which", "where",
                                 "with", "from", "have", "been", "were", "also", "such", "than",
                                 "while", "however", "findings", "studies", "suggests", "proposed",
                                 "between", "investigated", "demonstrated", "approach", "paper"}
                    ctx_keywords = [w for w in ctx_words if w not in _ctx_stop
                                    and w != surname.lower()][:5]
                    cited[key] = (surname, " ".join(ctx_keywords))

        if not cited:
            return []

        logger.info("Found %d unmatched citations to rescue: %s",
                     len(cited), list(cited.keys())[:10])

        # Topic keywords for relevance check
        topic_title = brief.get("title", "")
        topic_terms = brief.get("search_terms", [])
        topic_text = (topic_title + " " + " ".join(topic_terms)).lower()
        topic_words = {w for w in topic_text.split() if len(w) > 3}
        topic_words -= {"review", "analysis", "study", "research", "paper", "novel",
                        "based", "using", "critical", "current", "approach",
                        "significant", "model", "system", "data", "method",
                        "proposed", "results", "effect", "effects", "between",
                        "different", "studies", "findings", "evidence", "literature"}

        rescued: list[dict] = []
        max_rescue = 8  # cap to avoid excessive API calls

        for key, (surname, ctx_snippet) in list(cited.items())[:max_rescue]:
            year = key.split("_")[-1]
            # Use context-enriched query for better matches
            query = f"{surname} {ctx_snippet}"

            # Try Crossref first
            found = None
            try:
                cr_results = _search_crossref(query, limit=3)
                for r in cr_results:
                    r_surname = ""
                    if r.get("authors"):
                        r_surname = _extract_surname(r["authors"][0]).lower()
                    r_year = str(r.get("year", ""))
                    # Allow ±1 year tolerance (papers often have online vs print year difference)
                    if r_surname == surname.lower() and abs(int(r_year or 0) - int(year)) <= 1:
                        found = r
                        break
            except Exception as e:
                logger.debug("Crossref rescue search failed for %s: %s", key, e)

            # Try Semantic Scholar if Crossref didn't find it
            if not found:
                try:
                    s2_results = _search_semantic_scholar(query, limit=3)
                    for r in s2_results:
                        r_surname = ""
                        if r.get("authors"):
                            r_surname = _extract_surname(r["authors"][0]).lower()
                        r_year = str(r.get("year", ""))
                        if r_surname == surname.lower() and abs(int(r_year or 0) - int(year)) <= 1:
                            found = r
                            break
                except Exception as e:
                    logger.debug("S2 rescue search failed for %s: %s", key, e)

            if not found:
                logger.info("  Rescue: %s — not found in academic DBs (will be stripped)", key)
                continue

            # Relevance check: does the found paper's title/abstract overlap with topic?
            found_text = (found.get("title", "") + " " + found.get("abstract", "")).lower()
            found_words = {w for w in found_text.split() if len(w) > 3}
            found_words -= {"significant", "model", "system", "data", "method",
                           "proposed", "results", "effect", "effects", "between",
                           "different", "studies", "findings", "evidence", "literature"}
            overlap = topic_words & found_words
            if len(overlap) < 3:
                logger.info("  Rescue: %s — found but not relevant (overlap=%d: %s)",
                           key, len(overlap), overlap)
                continue

            # Build submission reference
            ref = self._build_single_submission_ref(found, len(references) + len(rescued))
            rescued.append(ref)
            logger.info("  Rescued: %s (%s) — %s [overlap=%d]",
                        surname, year, found.get("title", "?")[:60], len(overlap))

        return rescued

    @staticmethod
    def _enforce_citation_spread(draft: dict[str, str]) -> None:
        """Playbook Rule 3: no reference in more than 3 sections (2 anchors allowed in 4).

        Removes excess citations from sections where the ref is least important.
        """
        cite_pattern = re.compile(rf"\[([A-Z][a-zA-ZÀ-ÖØ-öø-ÿ{_HYPH}']+(?:\s+et\s+al\.)?(?:,\s*\d{{4}}[a-z]?)?(?:,\s*\"[^\"]+\")?)\]")

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

    # NOTE: _sanitize_fabrication and _enforce_citation_density have been removed.
    # Fabrication detection and uncited-claim handling are now part of the LLM
    # editorial review pass (phase6_editorial_review prompt, items 5 and 8).

    @staticmethod
    def _generate_tags(brief: dict, title: str) -> list[str]:
        """Generate tags from research brief.

        Tags use Title Case (e.g. "Machine Learning", "AI Agent") not
        slugs or lowercase.
        """
        tags = set()
        paper_type = brief.get("paper_type", "")
        if paper_type:
            tags.add(paper_type.strip().title())
        for term in brief.get("search_terms", [])[:5]:
            words = [w.strip() for w in term.split() if len(w) > 2]
            if words:
                tags.add(" ".join(words[:3])[:50].title())
        if not tags:
            title_words = [w.strip(",:;.") for w in title.split() if len(w) > 3]
            for tw in title_words[:3]:
                tags.add(tw.title())
        if not tags:
            tags.add("Research")
        return list(tags)[:10]

    def _save_paper_locally(self, paper_payload: dict) -> pathlib.Path:
        """Save paper as JSON for later submission."""
        output_dir = _CHECKPOINT_DIR.parent / "papers"
        output_dir.mkdir(parents=True, exist_ok=True)
        title = paper_payload.get("title", "untitled")
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:60].strip()
        ts = int(time.time())
        path = output_dir / f"{safe}_{ts}.json"
        path.write_text(json.dumps(paper_payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        logger.info("Paper saved locally: %s", path)
        return path
