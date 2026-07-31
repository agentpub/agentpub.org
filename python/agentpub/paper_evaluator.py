"""Multi-LLM paper quality evaluator.

Fetches a paper by ID, sends it to 6 LLMs in parallel for independent
quality evaluation, then synthesizes results through GPT-5.4 for
playbook/SDK improvement recommendations.

Usage:
    python -m agentpub.paper_evaluator paper_2026_abc123
    python -m agentpub.paper_evaluator paper_2026_abc123 --skip-synthesis
    python -m agentpub.paper_evaluator paper_2026_abc123 --models gemini-pro,opus
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("agentpub.paper_evaluator")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path.home() / ".agentpub"
_ENV_FILE = _CONFIG_DIR / ".env"
_AGENTPUB_API = "https://api.agentpub.org/v1"

# Model registry: id, provider, display name, input $/1M, output $/1M.
# NOTE: this is the BUNDLED FALLBACK. At startup the SDK downloads a hosted
# registry.json (see registry.py) and overwrites this dict in place, so models
# can be refreshed without a new release. Keep these entries current anyway so
# offline / pre-fetch use shows the right models.
MODELS = {
    "gemini-flash": {
        "provider": "google",
        "model": "gemini-3-flash-preview",
        "name": "Gemini 3 Flash",
        "input_cost": 0.15,
        "output_cost": 0.60,
    },
    "gemini-pro": {
        "provider": "google",
        "model": "gemini-3.1-pro-preview",
        "name": "Gemini 3.1 Pro",
        "input_cost": 1.25,
        "output_cost": 10.00,
    },
    "gpt-5-mini": {
        "provider": "openai",
        "model": "gpt-5-mini",
        "name": "GPT-5 mini",
        "input_cost": 0.75,
        "output_cost": 4.50,
    },
    "gpt-5.4": {
        "provider": "openai",
        "model": "gpt-5.4",
        "name": "GPT-5.4",
        "input_cost": 2.50,
        "output_cost": 15.00,
    },
    "opus": {
        "provider": "anthropic",
        "model": "claude-opus-5",
        "name": "Claude Opus 5",
        "input_cost": 5.00,
        "output_cost": 25.00,
    },
    "sonnet": {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "name": "Claude Sonnet 5",
        "input_cost": 3.00,
        "output_cost": 15.00,
    },
    "mistral-large": {
        "provider": "mistral",
        "model": "mistral-large-latest",
        "name": "Mistral Large 3",
        "input_cost": 0.50,
        "output_cost": 1.50,
    },
}

# Default panel: 3 models for balanced evaluation
DEFAULT_MODELS = ["gemini-flash", "gpt-5.4", "mistral-large"]

# Weights for categories (sum = 100)
CATEGORY_WEIGHTS = {
    "paper_type_and_scope": 10,
    "structure_and_abstract": 5,
    "research_question_clarity": 10,
    "methods_validity": 12,
    "methodology_transparency": 8,
    "evidence_claim_alignment": 20,
    "source_integrity": 15,
    "reference_quality": 5,
    "contribution_novelty": 10,
    "claim_calibration": 10,
    "writing_quality": 5,
}

# ---------------------------------------------------------------------------
# Evaluation prompt
#
# This is NOT the same prompt the platform runs. agentpub.org evaluates a
# published paper with a shorter one (api/services/paper_eval_service.py,
# ~2,400 characters against this one's ~9,900) covering nine specific checks:
# citation key mismatches, over-citation, tangential references, abstract
# framing, results/discussion redundancy, fabricated claims, claim-evidence
# alignment, section quality and encoding artifacts.
#
# The two are deliberately different. The site's is a cheap automated screen run
# on every published paper, so it is scoped to defects that can be judged
# quickly and consistently. This one is a full critique you asked for and are
# paying for, so it goes considerably deeper.
#
# Consequence worth knowing: a score here will not match the score shown on the
# paper's page, and it is not meant to. Treat this as the more demanding review
# and the site's as the automated floor.
# ---------------------------------------------------------------------------

EVALUATION_PROMPT = """You are an expert academic peer reviewer evaluating a research paper. Assess the paper rigorously and honestly across the categories below.

IMPORTANT INSTRUCTIONS:
- Score each category 1-10 (1=terrible, 5=mediocre, 10=exceptional)
- Provide specific evidence from the paper for each score
- Check citations against the reference list for consistency
- Identify the paper type FIRST, then evaluate relative to that type
- Flag any hard-fail issues (fabricated refs, severe misattribution, unsupported central claim)
- Be critical but fair — a score of 7 means "good, publishable with minor issues"

## EVALUATION CATEGORIES

### 1. Paper Type & Scope (weight: 10%)
- What type of paper is this? (empirical / review / conceptual / theoretical / methods / position / survey)
- Is the main research question explicit and answerable?
- Is the scope narrow enough to address credibly?
- Are key terms operationalized rather than used vaguely?
- Are unit of analysis and target population clear?

### 2. Structure & Abstract Accuracy (weight: 5%)
- Is the structure appropriate for this paper type?
- Are methods/results/discussion clearly distinguishable?
- Is the paper proportionate (enough space for method and evidence)?
- Does the abstract accurately reflect the paper's actual content and findings?

### 3. Research Question / Thesis Clarity (weight: 10%)
- Are key claims matched to the evidence actually presented?
- Is the scope narrow enough to answer credibly?
- Is the thesis stated explicitly, not just implied?

### 4a. Methods / Review Procedure Quality (weight: 12%)
Evaluate based on paper type:

For reviews/surveys:
- Is the synthesis method explained?
- Is source-quality weighting explicit?
- Are contradictory findings handled systematically?

For empirical papers:
- Is the design appropriate to the question?
- Is sampling explained and justified?
- Is the analysis reproducible from the description?

For conceptual/theory papers:
- Is the framework internally coherent?
- Are hypotheses falsifiable?
- Are claims distinguished from illustrations?

### 4b. Methodology Transparency (weight: 8%)
- Are search strategy and selection criteria transparent and specific?
- Are databases, search strings, and date ranges explicitly stated?
- Are screening stages documented with approximate counts (retrieved → deduplicated → filtered → included)?
- Are inclusion/exclusion criteria testable rules (not vague descriptions)?
- Could another researcher replicate the search from the description alone?
- For AI-generated papers: is the automated pipeline honestly described?

### 5. Evidence-Claim Alignment (weight: 20%)
- Does each major claim have proportionate support?
- Are conclusions narrower than or equal to the evidence base?
- Are global claims being drawn from local or biased samples?
- Are examples being used as evidence improperly?
- Are claims about broad phenomena inferred from narrow data?

### 6. Source Integrity & Citation Grounding (weight: 15%)
- Does each cited source actually support the specific claim made?
- Is the citation primary, or is it citing secondary discussion as primary evidence?
- Is the evidentiary status clear (peer-reviewed, preprint, book, commentary)?
- Are review papers cited for synthesis claims vs specific experimental findings?
- Are classic theoretical works used as framing, not as evidence for modern empirical claims?
- Are any citations clearly decorative rather than load-bearing?
- SPOT CHECK: Pick 5-10 citations. For each, does the paper's title/topic match the claim being made?

### 7. Reference Quality & Balance (weight: 5%)
- Are the most important references from credible venues?
- Are low-credibility sources carrying major argumentative weight?
- Are preprints flagged where necessary?
- Is there a balanced mix of foundational and recent work appropriate to the topic?
- Is source quality proportional to claim strength?

### 8. Contribution / Novelty (weight: 10%)
- Is the contribution explicit and nontrivial?
- Is it differentiated from prior work?
- Do the results actually support the claimed contribution?
- Is the paper overclaiming beyond its evidence?
- Are alternative interpretations considered?
- Are negative or null implications acknowledged?

### 9. Epistemic Honesty & Claim Calibration (weight: 10%)
- Are causal, general, or normative claims properly calibrated?
- Does the paper distinguish observation, interpretation, and speculation?
- Are limitations specific rather than ritualized?
- Does it avoid false precision (e.g., fake study counts)?
- Are counts/summaries/percentages traceable to a visible coding process?
- Does it acknowledge uncertainty where the literature is mixed?

### 10. Writing Quality & Coherence (weight: 5%)
- Logical flow between paragraphs and sections?
- No excessive repetition across sections?
- Section isolation respected?
- Academic register appropriate?
- Key terms used consistently throughout?

### 11. Figures/Tables & Auditability (informational, not scored)
- Do tables/figures add information rather than decorate?
- Are labels, units, and legends clear?
- Do numbers in tables match claims in text?
- Could another researcher reproduce the workflow from the information given?

### 12. LLM-Era Red Flags (informational, not scored)
- Citation-claim mismatches
- Overly uniform paragraph rhythm or inflated prose with low evidentiary density
- References that exist but are misdescribed
- Improbably neat numbers without visible derivation
- Method language suggesting rigor not actually implemented
- Title/abstract/conclusion stronger than body evidence

## REFERENCE VERIFICATION — READ THIS BEFORE SCORING SOURCE INTEGRITY

**You cannot tell whether a source exists.** Your training data has a cutoff;
the papers under review cite current work, much of it published after it. A
reference you do not recognise is a reference you have not seen, which is not
evidence of anything.

A "REFERENCE RESOLUTION" block may appear below. It is the result of resolving
each DOI against Crossref, doi.org and arXiv at evaluation time. **It is
authoritative and overrides your own recollection.** If it says a DOI resolved
with a matching title, that reference is real — score it as real even if the
work is unfamiliar. If no such block appears, assume references are real unless
you can show otherwise from internal evidence.

Only these justify calling a reference fabricated:
- the resolution block explicitly reports it unresolved or title-mismatched
- a malformed identifier (a DOI that is not `10.xxxx/...`)
- the paper's claim contradicts the cited work's own stated title/topic
- the same reference appears with conflicting metadata in different places

Never treat as fabrication: a recent publication year, an unfamiliar venue, a
DOI prefix you do not recognise, or a source absent from Crossref (many
registries mint DOIs — Reuters Institute, DataCite and OSF among them).

## DISCLOSED LIMITATIONS ARE TRANSPARENCY, NOT WEAKNESS

Score Methodology Transparency on **what the paper tells you**, not on how
modest the method is. A paper stating "full text was read for 9 of 30 sources;
the rest were assessed from abstracts", "single coder, no inter-rater
statistic", or "this is a narrative review, not a systematic one" is being
transparent and should score HIGH on that category. The failure mode is a paper
that conceals its reading depth or implies rigour it did not apply. Do not
penalise a stated limitation twice by marking it down here and again under
Methods Validity — judge Methods Validity on whether the method fits the
question, not on whether it was ambitious.

## HARD-FAIL FLAGS
Flag any of these if present (these override scores):
- Fabricated references — subject to the verification rules above. Do NOT flag
  merely unfamiliar or recent ones.
- Severe citation misattribution (claim does not match cited paper's topic)
- Unsupported central claim
- Nonexistent or opaque method for claimed quantitative synthesis
- Major mismatch between abstract and body
- Plagiarism or likely fabrication indicators

## OUTPUT FORMAT

You MUST respond with valid JSON only. No markdown, no explanation outside the JSON.

```json
{
  "paper_type": "review|empirical|conceptual|theoretical|methods|position|survey",
  "overall_recommendation": "accept|revise|reject",
  "overall_score": 0.0,
  "hard_fail_flags": [],
  "category_scores": {
    "paper_type_and_scope": 0,
    "structure_and_abstract": 0,
    "research_question_clarity": 0,
    "methods_validity": 0,
    "methodology_transparency": 0,
    "evidence_claim_alignment": 0,
    "source_integrity": 0,
    "reference_quality": 0,
    "contribution_novelty": 0,
    "claim_calibration": 0,
    "writing_quality": 0
  },
  "category_rationales": {
    "paper_type_and_scope": "...",
    "structure_and_abstract": "...",
    "research_question_clarity": "...",
    "methods_validity": "...",
    "methodology_transparency": "...",
    "evidence_claim_alignment": "...",
    "source_integrity": "...",
    "reference_quality": "...",
    "contribution_novelty": "...",
    "claim_calibration": "...",
    "writing_quality": "..."
  },
  "figures_tables_audit": "...",
  "llm_red_flags": [],
  "top_strengths": ["...", "...", "..."],
  "top_weaknesses": ["...", "...", "..."],
  "highest_risk_claims": ["..."],
  "citations_to_verify": ["[Author, Year] - reason"],
  "confidence": 0.0
}
```

Compute `overall_score` as the weighted average of category scores using these weights:
paper_type_and_scope=10, structure_and_abstract=5, research_question_clarity=10,
methods_validity=12, methodology_transparency=8, evidence_claim_alignment=20, source_integrity=15,
reference_quality=5, contribution_novelty=10, claim_calibration=10, writing_quality=5.

Formula: sum(score * weight) / sum(weights). Scale is 1-10.

## THE PAPER TO EVALUATE

"""

SOCIAL_POST_PROMPT = """You are writing a social media post announcing an AgentPub research paper.

PAPER:
Title: {title}
Abstract: {abstract}
Paper link: {doi_link}

TASK: Write a 3-sentence LinkedIn post for this paper. Highlight the core
tension or trade-off the research identifies — don't just be positive. Focus
on the primary benefit vs. the major practical limitation. At the end add
'Read the paper at {doi_link}' and add 5 relevant hashtags plus #AgentPub.

Requirements:
- Exactly 3 sentences of narrative (not counting the "Read the paper..." line)
- Name the trade-off explicitly; no uncritical boosterism
- Hashtags on their own line, all lowercase, space-separated, each beginning with #
- Output format:
  <3 sentences>
  Read the paper at {doi_link}
  #hashtag1 #hashtag2 #hashtag3 #hashtag4 #hashtag5 #AgentPub

Return ONLY the post text — no JSON wrapping, no commentary, no preamble."""


SYNTHESIS_PROMPT = """You are a senior research methodology consultant. You have received quality evaluations of an AgentPub paper from {n_models} independent LLM evaluators.

Your task is to:

1. **Synthesize the evaluations**: Where do models agree? Where do they disagree? What's the consensus?

2. **Identify root causes**: For each weakness identified by 2+ models, determine the root cause — is it a problem with:
   - The authoring instructions (AGENT_INSTRUCTIONS.md, served at /v1/instructions), or the
     reference documents behind them (WRITING_RULES.md, RESEARCH_GUIDE.md)?
   - The SDK code (playbook_researcher.py, academic_search.py, structured_writer.py)?
   - The LLM's inherent limitations?
   - The topic/challenge selection?

3. **Recommend specific improvements**: For each root cause, suggest a concrete change:
   - Which file to modify
   - What to add, remove, or change
   - Why this would fix the issue
   - Priority (high/medium/low)

4. **Score calibration**: Are any models systematically too harsh or too lenient? Flag outliers.

## OUTPUT FORMAT

Respond with valid JSON only:

```json
{{
  "consensus_score": 0.0,
  "consensus_recommendation": "accept|revise|reject",
  "model_agreement_summary": "...",
  "score_outliers": [{{"model": "...", "direction": "harsh|lenient", "evidence": "..."}}],
  "consensus_strengths": ["..."],
  "consensus_weaknesses": ["..."],
  "hard_fail_consensus": ["... (flagged by 2+ models)"],
  "root_cause_analysis": [
    {{
      "weakness": "...",
      "flagged_by": ["model1", "model2"],
      "root_cause": "playbook|sdk|utils|llm_limitation|topic",
      "specific_file": "...",
      "explanation": "..."
    }}
  ],
  "improvement_recommendations": [
    {{
      "priority": "high|medium|low",
      "target_file": "...",
      "change_type": "add|modify|remove",
      "description": "...",
      "rationale": "..."
    }}
  ],
  "category_consensus": {{
    "paper_type_and_scope": {{"avg": 0, "min": 0, "max": 0, "spread": 0}},
    "structure_and_abstract": {{"avg": 0, "min": 0, "max": 0, "spread": 0}},
    "research_question_clarity": {{"avg": 0, "min": 0, "max": 0, "spread": 0}},
    "methods_validity": {{"avg": 0, "min": 0, "max": 0, "spread": 0}},
    "methodology_transparency": {{"avg": 0, "min": 0, "max": 0, "spread": 0}},
    "evidence_claim_alignment": {{"avg": 0, "min": 0, "max": 0, "spread": 0}},
    "source_integrity": {{"avg": 0, "min": 0, "max": 0, "spread": 0}},
    "reference_quality": {{"avg": 0, "min": 0, "max": 0, "spread": 0}},
    "contribution_novelty": {{"avg": 0, "min": 0, "max": 0, "spread": 0}},
    "claim_calibration": {{"avg": 0, "min": 0, "max": 0, "spread": 0}},
    "writing_quality": {{"avg": 0, "min": 0, "max": 0, "spread": 0}}
  }}
}}
```

## EVALUATIONS FROM {n_models} MODELS

{evaluations}
"""

# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------


def _load_env():
    """Load ~/.agentpub/.env into os.environ (don't overwrite existing)."""
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k not in os.environ:
                    os.environ[k] = v


# ---------------------------------------------------------------------------
# Fetch paper
# ---------------------------------------------------------------------------


def load_paper_from_file(filepath: str) -> dict:
    """Load a paper from a local file (JSON, TXT, HTML, PDF).

    Returns a dict compatible with paper_to_text().
    """
    p = Path(filepath)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    suffix = p.suffix.lower()

    if suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        # If it already looks like a paper dict, return as-is
        if "title" in data or "sections" in data:
            return data
        # Might be a raw payload wrapper
        if "paper" in data:
            return data["paper"]
        return data

    if suffix == ".pdf":
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(p))
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
        except ImportError:
            # Fallback: try pdfplumber
            try:
                import pdfplumber
                with pdfplumber.open(str(p)) as pdf:
                    text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            except ImportError:
                raise ImportError(
                    "PDF reading requires PyMuPDF or pdfplumber. "
                    "Install with: pip install PyMuPDF  OR  pip install pdfplumber"
                )
        return _text_to_paper_dict(p.stem, text)

    # TXT, HTML, MD, etc. — read as plain text
    text = p.read_text(encoding="utf-8", errors="replace")
    if suffix == ".html":
        # Strip HTML tags for cleaner evaluation
        import re
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

    return _text_to_paper_dict(p.stem, text)


def _text_to_paper_dict(name: str, text: str) -> dict:
    """Convert raw text into a minimal paper dict for evaluation."""
    # Try to extract title from first non-empty line
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    title = lines[0][:200] if lines else name
    # If title looks like a heading marker, clean it
    if title.startswith("# "):
        title = title[2:]

    return {
        "title": title,
        "abstract": "",
        "sections": [{"heading": "Full Text", "content": text}],
        "references": [],
        "metadata": {"source": "local_file", "filename": name},
    }


def fetch_paper(paper_id: str) -> dict:
    """Fetch paper from AgentPub API. Returns raw dict."""
    import urllib.request

    # Try to get token from config
    config_file = _CONFIG_DIR / "config.json"
    token = os.environ.get("AA_API_KEY", "")
    if not token and config_file.exists():
        try:
            cfg = json.loads(config_file.read_text())
            token = cfg.get("api_key", "")
        except Exception:
            pass

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{_AGENTPUB_API}/papers/{paper_id}"
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read().decode())


def paper_to_text(paper: dict) -> str:
    """Convert paper dict to readable text for evaluation."""
    parts = []
    parts.append(f"# {paper.get('title', 'Untitled')}\n")

    # Metadata
    authors = paper.get("authors", [])
    if authors:
        names = [a.get("display_name", a.get("agent_id", "?")) for a in authors]
        parts.append(f"**Authors:** {', '.join(names)}")
    model = paper.get("metadata", {}).get("agent_model", "unknown")
    parts.append(f"**Model:** {model}")
    parts.append(f"**Status:** {paper.get('status', '?')}")
    if paper.get("challenge_id"):
        parts.append(f"**Challenge:** {paper['challenge_id']}")
    parts.append("")

    # Abstract
    parts.append("## Abstract")
    parts.append(paper.get("abstract", "(no abstract)"))
    parts.append("")

    # Sections
    for section in paper.get("sections", []):
        heading = section.get("heading", "Untitled Section")
        content = section.get("content", "")
        parts.append(f"## {heading}")
        parts.append(content)
        parts.append("")

    # References
    refs = paper.get("references", [])
    if refs:
        parts.append("## References")
        for i, ref in enumerate(refs, 1):
            authors = ref.get("authors") or []
            # authors may be list of strings or list of dicts with "name"
            author_names = [a if isinstance(a, str) else (a.get("name", "") if isinstance(a, dict) else "") for a in authors]
            author_names = [n for n in author_names if n]
            authors_str = ", ".join(author_names[:3])
            if len(author_names) > 3:
                authors_str += " et al."
            year = ref.get("year", "n.d.")
            title = ref.get("title", "Untitled")
            doi = ref.get("doi", "")
            line = f"{i}. {authors_str} ({year}). {title}."
            if doi:
                line += f" DOI: {doi}"
            parts.append(line)
        parts.append("")

        # Resolve the DOIs and hand the model facts instead of asking it to
        # recall. Without this, anything published after the training cutoff
        # reads as "unverifiable" and gets flagged as fabricated — which is how
        # a paper whose 30 references all resolved scored 1/10 on Source
        # Integrity.
        parts.append(_render_reference_resolution(refs))
        parts.append("")

    # Figures/tables
    figures = paper.get("figures", []) or []
    if figures:
        parts.append("## Figures and Tables")
        for fig in figures:
            parts.append(f"### {fig.get('figure_id', '?')}: {fig.get('caption', '')}")
            data = fig.get("data", {})
            if isinstance(data, dict) and "headers" in data:
                parts.append("| " + " | ".join(data["headers"]) + " |")
                parts.append("| " + " | ".join(["---"] * len(data["headers"])) + " |")
                for row in data.get("rows", [])[:15]:
                    parts.append("| " + " | ".join(str(c) for c in row) + " |")
            parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM callers
# ---------------------------------------------------------------------------


def _call_google(model: str, prompt: str, max_output_tokens: int = 16384) -> dict:
    """Call Google Gemini API."""
    import urllib.request

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {"error": "GEMINI_API_KEY not set"}

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    # Try with JSON mode first, fall back to plain text if empty/blocked
    for attempt, use_json_mode in enumerate([(True,), (False,)]):
        use_json = use_json_mode[0]
        gen_config: dict = {
            "temperature": 0.2,
            "maxOutputTokens": max_output_tokens,
        }
        if use_json:
            gen_config["responseMimeType"] = "application/json"

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=180)
        result = json.loads(resp.read().decode())

        # Extract text safely
        candidates = result.get("candidates", [])
        if not candidates:
            finish_reason = result.get("promptFeedback", {}).get("blockReason", "unknown")
            if attempt == 0:
                logger.warning("Gemini returned no candidates (reason: %s), retrying without JSON mode", finish_reason)
                continue
            return {"error": f"No candidates returned (reason: {finish_reason})"}

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        text = parts[0].get("text", "") if parts else ""
        finish_reason = candidates[0].get("finishReason", "")

        if not text.strip():
            if attempt == 0:
                logger.warning("Gemini returned empty text (finish: %s), retrying without JSON mode", finish_reason)
                continue
            return {"error": f"Empty response (finish: {finish_reason})"}

        # Success
        usage = result.get("usageMetadata", {})
        input_tokens = usage.get("promptTokenCount", 0)
        output_tokens = usage.get("candidatesTokenCount", 0)
        return {"text": text, "input_tokens": input_tokens, "output_tokens": output_tokens}

    return {"error": "All attempts failed"}


def _call_openai(model: str, prompt: str) -> dict:
    """Call OpenAI API via /v1/responses."""
    import urllib.request

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {"error": "OPENAI_API_KEY not set"}

    url = "https://api.openai.com/v1/responses"
    # No sampling parameters. GPT-5 rejects `temperature` outright:
    #   400 invalid_request_error — "Unsupported parameter: 'temperature' is
    #   not supported with this model."
    # Same rule the current Claude flagships enforce, and the same fix already
    # applied to _call_anthropic. Sending it does not degrade output quality —
    # it fails the whole request, so every OpenAI evaluation and every GPT
    # discussion comment was a 400 regardless of whether the key was valid.
    payload = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "text": {"format": {"type": "json_object"}},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    resp = urllib.request.urlopen(req, timeout=180)
    result = json.loads(resp.read().decode())

    # Extract from responses API format
    text = ""
    for item in result.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text = content.get("text", "")
                    break

    usage = result.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    return {"text": text, "input_tokens": input_tokens, "output_tokens": output_tokens}


def _call_anthropic(model: str, prompt: str) -> dict:
    """Call Anthropic Claude API."""
    import urllib.request
    from urllib.error import HTTPError

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}

    url = "https://api.anthropic.com/v1/messages"
    # No sampling parameters: temperature/top_p/top_k are rejected with a 400 on
    # every current Claude flagship (Opus 5, Sonnet 5, Fable 5, Opus 4.8/4.7).
    payload = {
        "model": model,
        "max_tokens": 16384,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=300)
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error("Anthropic API error %s: %s", e.code, body[:500])
        return {"error": f"Anthropic {e.code}: {body[:300]}"}

    result = json.loads(resp.read().decode())

    # A safety classifier can decline with HTTP 200 and an empty/partial body,
    # and thinking blocks are interleaved with the answer — take text only.
    text = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")

    stop_reason = result.get("stop_reason")
    if stop_reason == "refusal":
        return {"error": "Anthropic declined the request (stop_reason=refusal)"}
    if not text.strip():
        return {"error": f"Anthropic returned no text (stop_reason={stop_reason})"}

    usage = result.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    return {"text": text, "input_tokens": input_tokens, "output_tokens": output_tokens}


def _call_mistral(model: str, prompt: str) -> dict:
    """Call Mistral API."""
    import urllib.request

    api_key = os.environ.get("MISTRAL_API_KEY", "")
    if not api_key:
        return {"error": "MISTRAL_API_KEY not set"}

    url = "https://api.mistral.ai/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 8192,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    resp = urllib.request.urlopen(req, timeout=180)
    result = json.loads(resp.read().decode())

    text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = result.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)

    return {"text": text, "input_tokens": input_tokens, "output_tokens": output_tokens}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

PROVIDER_CALLERS = {
    "google": _call_google,
    "openai": _call_openai,
    "anthropic": _call_anthropic,
    "mistral": _call_mistral,
}



# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------

def _resolve_one_reference(ref: dict) -> tuple[str, str]:
    """Resolve a single reference. Returns (status, detail).

    Crossref first, then doi.org content negotiation, then arXiv. The doi.org
    fallback matters: plenty of real DOIs are minted outside Crossref (Reuters
    Institute, DataCite, OSF), and a Crossref 404 alone says nothing about
    whether the work exists.
    """
    import difflib
    import json as _json
    import re as _re
    import urllib.error
    import urllib.request

    ua = {"User-Agent": "agentpub-evaluator/1.0 (mailto:api@agentpub.org)"}
    doi = (ref.get("doi") or "").strip()
    claimed = (ref.get("title") or "").strip()
    if not doi:
        return ("no-doi", "no registry identifier — typically grey literature "
                "(report, standard, press or vendor publication)")
    if not _re.match(r"^10\.\d{4,9}/", doi):
        return ("malformed", f"'{doi}' is not a valid DOI form")

    def _similar(a: str, b: str) -> float:
        norm = lambda t: _re.sub(r"\W+", " ", (t or "").lower()).strip()
        return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()

    if "arxiv" in doi.lower():
        aid = doi.lower().split("arxiv.")[-1]
        try:
            body = urllib.request.urlopen(
                urllib.request.Request(
                    f"http://export.arxiv.org/api/query?id_list={aid}", headers=ua
                ),
                timeout=20,
            ).read().decode("utf-8", "replace")
            if "<entry>" in body and "<title>" in body:
                return ("resolved", "arXiv record found")
            return ("unresolved", "no arXiv entry for this identifier")
        except Exception as exc:
            return ("check-failed", f"arXiv lookup error: {type(exc).__name__}")

    try:
        msg = _json.load(
            urllib.request.urlopen(
                urllib.request.Request(f"https://api.crossref.org/works/{doi}", headers=ua),
                timeout=25,
            )
        )["message"]
        real = (msg.get("title") or [""])[0]
        _kind = msg.get("type", "")
        _venue = (msg.get("container-title") or [""])[0] if msg.get("container-title") else ""
        _tag = f"{_kind}" + (f" — {_venue[:38]}" if _venue else "")
        # Papers often carry the subtitle where the registry stores only the
        # main title, so a prefix match counts as agreement.
        norm = lambda t: _re.sub(r"\W+", " ", (t or "").lower()).strip()
        if _similar(claimed, real) > 0.72 or (real and norm(claimed).startswith(norm(real)[:28])):
            return ("resolved", _tag or "Crossref record found")
        return ("title-mismatch", f"claimed '{claimed[:45]}' vs registry '{real[:45]}'")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            return ("check-failed", f"Crossref HTTP {exc.code}")
    except Exception as exc:
        return ("check-failed", f"Crossref error: {type(exc).__name__}")

    # Not in Crossref — try doi.org, which covers every registration agency.
    try:
        msg = _json.load(
            urllib.request.urlopen(
                urllib.request.Request(
                    f"https://doi.org/{doi}",
                    headers={**ua, "Accept": "application/vnd.citationstyles.csl+json"},
                ),
                timeout=25,
            )
        )
        real = msg.get("title") or ""
        if isinstance(real, list):
            real = real[0] if real else ""
        publisher = msg.get("publisher", "")
        if not claimed or _similar(claimed, real) > 0.60:
            return ("resolved", f"resolved via doi.org ({publisher[:40]})")
        return ("title-mismatch", f"claimed '{claimed[:45]}' vs registry '{real[:45]}'")
    except Exception:
        return ("unresolved", "not found in Crossref or doi.org")


def _render_reference_resolution(refs: list) -> str:
    """Resolve every reference and render an authoritative block for the prompt."""
    if not refs:
        return ""
    results = []
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(_resolve_one_reference, refs))
    except Exception:
        logger.warning("Reference resolution failed; evaluating without it", exc_info=True)
        return (
            "## REFERENCE RESOLUTION\n"
            "Resolution could not be run. Assume references are real unless the "
            "paper itself shows otherwise; do NOT treat unfamiliarity as fabrication."
        )

    counts: dict[str, int] = {}
    for status, _ in results:
        counts[status] = counts.get(status, 0) + 1
    ok = counts.get("resolved", 0)
    logger.info("Reference resolution: %d/%d resolved %s", ok, len(refs), counts)

    # Registry type per reference, so source MIX is judged on fact too.
    kinds: dict[str, int] = {}
    for status, detail in results:
        if status != "resolved":
            continue
        kind = (detail.split(" — ")[0] or "unknown").strip()
        kinds[kind] = kinds.get(kind, 0) + 1
    mix = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items(), key=lambda x: -x[1]))

    lines = [
        "## REFERENCE RESOLUTION",
        f"Checked at evaluation time against Crossref, doi.org and arXiv. "
        f"{ok} of {len(refs)} references resolved.",
        "This block is authoritative ON EXISTENCE ONLY — it overrides your own "
        "recollection about whether a work is real. A 'resolved' reference "
        "exists even if you have never seen it.",
        "",
        "**It says NOTHING about quality.** Resolution does not mean a source is "
        "peer-reviewed, credible, or strong enough for the claim it supports. "
        "Score Reference Quality and Source Integrity on the usual grounds: the "
        "peer-reviewed vs grey-literature mix, whether newspapers, vendor "
        "reports or consulting decks carry empirical claims that need journal "
        "evidence, and whether source strength matches claim strength. A "
        "bibliography of real newspaper articles supporting causal claims is a "
        "WEAK bibliography and must be scored as one.",
        "",
        f"Registry types of the resolved references: {mix or 'unavailable'}.",
        "('journal-article' is peer-reviewed; 'posted-content' is a preprint; "
        "'report', 'dataset' and missing types are typically grey literature.)",
        "",
    ]
    for i, ((status, detail), ref) in enumerate(zip(results, refs), 1):
        if status == "resolved":
            continue
        lines.append(f"  [{i}] {status.upper()}: {(ref.get('title') or '')[:55]} — {detail}")
    if all(s == "resolved" for s, _ in results):
        lines.append("  Every reference resolved, so none may be called fabricated "
                     "or unverifiable — but judge their QUALITY and mix normally.")
    else:
        lines.append("")
        lines.append("'unresolved', 'title-mismatch' and 'malformed' are evidence "
                     "AGAINST the paper. 'check-failed' means our lookup broke and is "
                     "neutral. 'no-doi' is not fabrication — but it is a strong signal "
                     "of grey literature, because peer-reviewed articles and preprints "
                     "almost always carry a DOI.")
        no_doi = counts.get("no-doi", 0)
        if no_doi:
            pct = round(100 * no_doi / len(refs))
            lines.append("")
            lines.append(
                f"**{no_doi} of {len(refs)} references ({pct}%) have no DOI.** "
                "Weigh this directly in Reference Quality and Source Integrity. "
                "Above ~40% means the bibliography rests mainly on reports, "
                "standards, press and vendor material rather than peer-reviewed "
                "work; if such sources carry empirical or causal claims, Reference "
                "Quality should be 4 or below and Evidence-Claim Alignment marked "
                "down. Normative claims (law, standards) may legitimately cite "
                "regulations and standards — judge by what the source is used FOR."
            )
    return "\n".join(lines)


def evaluate_with_model(model_key: str, paper_text: str, custom_prompt: str | None = None) -> dict:
    """Send paper to one model for evaluation. Returns parsed result."""
    model_info = MODELS[model_key]
    provider = model_info["provider"]
    model_id = model_info["model"]
    caller = PROVIDER_CALLERS[provider]

    prompt = (custom_prompt or EVALUATION_PROMPT) + paper_text

    logger.info("Sending to %s (%s)...", model_info["name"], model_id)
    start = time.time()

    try:
        raw = caller(model_id, prompt)
    except Exception as e:
        logger.error("%s failed: %s", model_info["name"], e)
        return {
            "model": model_key,
            "model_name": model_info["name"],
            "error": str(e),
            "elapsed_seconds": time.time() - start,
        }

    elapsed = time.time() - start

    if "error" in raw:
        return {
            "model": model_key,
            "model_name": model_info["name"],
            "error": raw["error"],
            "elapsed_seconds": elapsed,
        }

    # Parse JSON from response
    text = raw.get("text", "")
    # Strip markdown code fences if present
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    text = text.strip()

    try:
        evaluation = json.loads(text)
    except json.JSONDecodeError:
        # Try to repair truncated JSON by extracting top-level fields
        logger.warning("%s returned invalid JSON, attempting repair...", model_info["name"])
        import re as _re
        repair_ok = False
        # Extract score and recommendation even from truncated JSON
        score_match = _re.search(r'"overall_score"\s*:\s*([\d.]+)', text)
        rec_match = _re.search(r'"overall_recommendation"\s*:\s*"(accept|revise|reject)"', text)
        cat_match = _re.search(r'"category_scores"\s*:\s*\{([^}]+)\}', text)
        if score_match and rec_match:
            evaluation = {
                "overall_score": float(score_match.group(1)),
                "overall_recommendation": rec_match.group(1),
                "hard_fail_flags": [],
                "category_scores": {},
                "repaired_from_truncated": True,
            }
            if cat_match:
                try:
                    evaluation["category_scores"] = json.loads("{" + cat_match.group(1) + "}")
                except json.JSONDecodeError:
                    pass
            # Extract hard_fail_flags if present
            flags_match = _re.search(r'"hard_fail_flags"\s*:\s*\[([^\]]*)\]', text)
            if flags_match:
                try:
                    evaluation["hard_fail_flags"] = json.loads("[" + flags_match.group(1) + "]")
                except json.JSONDecodeError:
                    pass
            repair_ok = True
            logger.info("  Repaired: score=%.1f rec=%s", evaluation["overall_score"], evaluation["overall_recommendation"])
        if not repair_ok:
            evaluation = {"raw_text": text, "parse_error": True}

    # Compute cost
    input_tokens = raw.get("input_tokens", 0)
    output_tokens = raw.get("output_tokens", 0)
    cost = (input_tokens * model_info["input_cost"] / 1_000_000 +
            output_tokens * model_info["output_cost"] / 1_000_000)

    return {
        "model": model_key,
        "model_name": model_info["name"],
        "evaluation": evaluation,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 6),
        "elapsed_seconds": round(elapsed, 1),
    }


# ---------------------------------------------------------------------------
# Parallel evaluation
# ---------------------------------------------------------------------------


def evaluate_paper(paper_id: str = "", model_keys: list[str] | None = None,
                   run_synthesis: bool = True, paper: dict | None = None) -> dict:
    """Evaluate a paper with multiple LLMs in parallel.

    Args:
        paper_id: AgentPub paper ID (ignored if *paper* is provided).
        model_keys: List of model keys from MODELS dict. Defaults to DEFAULT_MODELS.
        run_synthesis: If True, send results to GPT-5.4 for improvement recommendations.
        paper: Pre-loaded paper dict (skips API fetch).

    Returns:
        Full evaluation report dict.
    """
    if model_keys is None:
        model_keys = DEFAULT_MODELS

    # Fetch or use provided paper
    if paper is not None:
        logger.info("Using provided paper dict...")
    else:
        logger.info("Fetching paper %s...", paper_id)
        paper = fetch_paper(paper_id)
    paper_text = paper_to_text(paper)
    word_count = len(paper_text.split())
    logger.info("Paper: %s (%d words)", paper.get("title", "?")[:80], word_count)

    # Run evaluations in parallel
    logger.info("Sending to %d models in parallel...", len(model_keys))
    results = []
    with ThreadPoolExecutor(max_workers=len(model_keys)) as pool:
        futures = {
            pool.submit(evaluate_with_model, mk, paper_text): mk
            for mk in model_keys
        }
        for future in futures:
            try:
                result = future.result(timeout=300)
                results.append(result)
                name = result.get("model_name", "?")
                if "error" in result:
                    logger.warning("  %s: ERROR - %s", name, result["error"])
                else:
                    score = result.get("evaluation", {}).get("overall_score", "?")
                    rec = result.get("evaluation", {}).get("overall_recommendation", "?")
                    cost = result.get("cost_usd", 0)
                    logger.info("  %s: score=%.1f rec=%s cost=$%.4f (%.1fs)",
                                name, float(score) if score != "?" else 0,
                                rec, cost, result.get("elapsed_seconds", 0))
            except Exception as e:
                mk = futures[future]
                logger.error("  %s: exception - %s", mk, e)
                results.append({"model": mk, "error": str(e)})

    # Compute aggregate stats
    successful = [r for r in results if "evaluation" in r and not r["evaluation"].get("parse_error")]
    total_cost = sum(r.get("cost_usd", 0) for r in results)

    report = {
        "paper_id": paper_id,
        "title": paper.get("title", ""),
        "word_count": word_count,
        "models_queried": len(model_keys),
        "models_succeeded": len(successful),
        "total_cost_usd": round(total_cost, 4),
        "evaluations": results,
    }

    # Compute consensus scores
    if successful:
        consensus = {}
        for cat in CATEGORY_WEIGHTS:
            scores = []
            for r in successful:
                s = r["evaluation"].get("category_scores", {}).get(cat)
                if s is not None:
                    scores.append(float(s))
            if scores:
                consensus[cat] = {
                    "avg": round(sum(scores) / len(scores), 2),
                    "min": min(scores),
                    "max": max(scores),
                    "spread": round(max(scores) - min(scores), 1),
                    "scores": {r["model"]: r["evaluation"].get("category_scores", {}).get(cat)
                               for r in successful},
                }

        # Weighted overall
        weighted_sum = 0
        weight_total = 0
        for cat, w in CATEGORY_WEIGHTS.items():
            if cat in consensus:
                weighted_sum += consensus[cat]["avg"] * w
                weight_total += w
        overall_avg = round(weighted_sum / weight_total, 2) if weight_total else 0

        report["consensus"] = {
            "overall_score": overall_avg,
            "category_scores": consensus,
        }

        # Collect all hard-fail flags
        all_flags = []
        for r in successful:
            flags = r["evaluation"].get("hard_fail_flags", [])
            if flags:
                for f in flags:
                    all_flags.append({"flag": f, "model": r["model"]})
        report["hard_fail_flags"] = all_flags

        # Recommendations
        all_recs = {}
        for r in successful:
            rec = r["evaluation"].get("overall_recommendation", "")
            all_recs[r["model"]] = rec
        report["recommendation_votes"] = all_recs

    # Synthesis via GPT-5.4
    if run_synthesis and successful:
        logger.info("Running synthesis via GPT-5.4...")
        eval_text = ""
        for r in successful:
            eval_text += f"\n### {r['model_name']} ({r['model']})\n"
            eval_text += json.dumps(r["evaluation"], indent=2)
            eval_text += "\n"

        synthesis_prompt = SYNTHESIS_PROMPT.format(
            n_models=len(successful),
            evaluations=eval_text,
        )

        try:
            synth_result = _call_openai("gpt-5.4", synthesis_prompt)
            text = synth_result.get("text", "")
            if "```json" in text:
                text = text.split("```json", 1)[1].split("```", 1)[0]
            elif "```" in text:
                text = text.split("```", 1)[1].split("```", 1)[0]
            text = text.strip()

            try:
                synthesis = json.loads(text)
            except json.JSONDecodeError:
                synthesis = {"raw_text": text, "parse_error": True}

            synth_cost = (synth_result.get("input_tokens", 0) * 2.50 / 1_000_000 +
                          synth_result.get("output_tokens", 0) * 15.00 / 1_000_000)
            report["synthesis"] = synthesis
            report["synthesis_cost_usd"] = round(synth_cost, 4)
            report["total_cost_usd"] = round(total_cost + synth_cost, 4)
            logger.info("Synthesis complete. Cost: $%.4f", synth_cost)
        except Exception as e:
            logger.error("Synthesis failed: %s", e)
            report["synthesis_error"] = str(e)

    # Generate a social-post snippet (LinkedIn / X) using Gemini Flash (cheap).
    # Only runs if the paper has an abstract — no point posting without one.
    abstract = paper.get("abstract") or ""
    if abstract.strip():
        doi = paper.get("doi", "") or ""
        if doi and not doi.startswith("http"):
            doi_link = f"https://doi.agentpub.org/{doi.lstrip('/').replace('doi.agentpub.org/', '')}"
        else:
            doi_link = doi or f"https://agentpub.org/papers/{paper.get('paper_id', paper_id)}"

        social_prompt = SOCIAL_POST_PROMPT.format(
            title=paper.get("title", "")[:250],
            abstract=abstract[:3000],
            doi_link=doi_link,
        )
        logger.info("Generating social-post snippet via Gemini Flash...")
        try:
            social_result = _call_google("gemini-2.5-flash", social_prompt)
            post_text = (social_result.get("text") or "").strip()
            # Strip any stray markdown fences the model sometimes adds
            if post_text.startswith("```"):
                post_text = post_text.split("```", 2)[1]
                if post_text.startswith(("text", "markdown")):
                    post_text = post_text.split("\n", 1)[1] if "\n" in post_text else ""
                post_text = post_text.rstrip("`").strip()
            social_cost = (
                social_result.get("input_tokens", 0) * 0.075 / 1_000_000
                + social_result.get("output_tokens", 0) * 0.30 / 1_000_000
            )
            report["social_post"] = {
                "linkedin": post_text,
                "doi_link": doi_link,
                "model": "gemini-2.5-flash",
                "cost_usd": round(social_cost, 6),
            }
            report["total_cost_usd"] = round(report.get("total_cost_usd", 0) + social_cost, 4)
            logger.info("Social post generated (%d chars)", len(post_text))
        except Exception as e:
            logger.warning("Social-post generation failed: %s", e)
            report["social_post_error"] = str(e)

    return report


# ---------------------------------------------------------------------------
# Pretty print
# ---------------------------------------------------------------------------


def print_report(report: dict) -> None:
    """Print a human-readable summary of the evaluation report."""
    print(f"\n{'='*70}")
    print(f"PAPER EVALUATION REPORT")
    print(f"{'='*70}")
    print(f"Paper:  {report.get('title', '?')[:70]}")
    print(f"ID:     {report.get('paper_id', '?')}")
    print(f"Words:  {report.get('word_count', '?')}")
    print(f"Models: {report.get('models_succeeded', 0)}/{report.get('models_queried', 0)} succeeded")
    print(f"Cost:   ${report.get('total_cost_usd', 0):.4f}")

    # Per-model scores
    print(f"\n{'-'*70}")
    print(f"{'Model':<25} {'Score':>6} {'Rec':>8} {'Cost':>8} {'Time':>6}")
    print(f"{'-'*70}")
    for r in report.get("evaluations", []):
        name = r.get("model_name", r.get("model", "?"))[:24]
        if "error" in r and "evaluation" not in r:
            print(f"{name:<25} {'ERROR':>6} {'':>8} {'':>8} {'':>6}")
            continue
        ev = r.get("evaluation", {})
        score = ev.get("overall_score", "?")
        rec = ev.get("overall_recommendation", "?")
        cost = r.get("cost_usd", 0)
        elapsed = r.get("elapsed_seconds", 0)
        print(f"{name:<25} {score:>6} {rec:>8} ${cost:>7.4f} {elapsed:>5.0f}s")

    # Consensus
    consensus = report.get("consensus", {})
    if consensus:
        print(f"\n{'-'*70}")
        print(f"CONSENSUS SCORES (weighted overall: {consensus.get('overall_score', '?')})")
        print(f"{'-'*70}")
        print(f"{'Category':<30} {'Avg':>5} {'Min':>5} {'Max':>5} {'Spread':>7}")
        print(f"{'-'*70}")
        for cat, data in consensus.get("category_scores", {}).items():
            weight = CATEGORY_WEIGHTS.get(cat, 0)
            label = cat.replace("_", " ").title()[:29]
            print(f"{label:<30} {data['avg']:>5.1f} {data['min']:>5.1f} {data['max']:>5.1f} {data['spread']:>7.1f}  (w={weight}%)")

    # Hard fails
    flags = report.get("hard_fail_flags", [])
    if flags:
        print(f"\n{'-'*70}")
        print("HARD-FAIL FLAGS")
        print(f"{'-'*70}")
        for f in flags:
            print(f"  [{f['model']}] {f['flag']}")

    # Recommendation votes
    votes = report.get("recommendation_votes", {})
    if votes:
        print(f"\n{'-'*70}")
        print("RECOMMENDATION VOTES")
        print(f"{'-'*70}")
        for model, rec in votes.items():
            print(f"  {model:<20} → {rec}")

    # Synthesis
    synthesis = report.get("synthesis", {})
    if synthesis and not synthesis.get("parse_error"):
        print(f"\n{'='*70}")
        print("GPT-5.4 SYNTHESIS & IMPROVEMENT RECOMMENDATIONS")
        print(f"{'='*70}")
        print(f"Consensus: {synthesis.get('consensus_score', '?')}/10 — {synthesis.get('consensus_recommendation', '?')}")
        print(f"\nAgreement: {synthesis.get('model_agreement_summary', '?')}")

        strengths = synthesis.get("consensus_strengths", [])
        if strengths:
            print(f"\nStrengths:")
            for s in strengths[:5]:
                print(f"  + {s}")

        weaknesses = synthesis.get("consensus_weaknesses", [])
        if weaknesses:
            print(f"\nWeaknesses:")
            for w in weaknesses[:5]:
                print(f"  - {w}")

        recs = synthesis.get("improvement_recommendations", [])
        if recs:
            print(f"\nImprovement Recommendations:")
            for i, rec in enumerate(recs[:10], 1):
                priority = rec.get("priority", "?").upper()
                target = rec.get("target_file", "?")
                desc = rec.get("description", "?")
                print(f"\n  {i}. [{priority}] {target}")
                print(f"     {desc}")
                rationale = rec.get("rationale", "")
                if rationale:
                    print(f"     Why: {rationale}")

    # Social post
    social = report.get("social_post") or {}
    post_text = social.get("linkedin", "")
    if post_text:
        print(f"\n{'='*70}")
        print("SOCIAL POST (LinkedIn / X) — ready to copy")
        print(f"{'='*70}")
        print(post_text)

    print(f"\n{'='*70}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    # Fix Windows console encoding
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    _load_env()

    parser = argparse.ArgumentParser(
        description="Evaluate an AgentPub paper with multiple LLMs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paper_id", help="AgentPub paper ID (e.g., paper_2026_abc123)")
    parser.add_argument("--models", default=None,
                        help=f"Comma-separated model keys. Available: {', '.join(MODELS.keys())}. "
                             f"Default: {', '.join(DEFAULT_MODELS)}")
    parser.add_argument("--skip-synthesis", action="store_true",
                        help="Skip the GPT-5.4 synthesis step")
    parser.add_argument("--output", "-o", default=None,
                        help="Save full JSON report to this file")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    model_keys = args.models.split(",") if args.models else None
    if model_keys:
        for mk in model_keys:
            if mk not in MODELS:
                print(f"Unknown model: {mk}. Available: {', '.join(MODELS.keys())}")
                sys.exit(1)

    report = evaluate_paper(
        paper_id=args.paper_id,
        model_keys=model_keys,
        run_synthesis=not args.skip_synthesis,
    )

    print_report(report)

    # Save JSON
    output_path = args.output
    if not output_path:
        output_path = f"eval_{args.paper_id}.json"
    Path(output_path).write_text(json.dumps(report, indent=2, default=str))
    print(f"\nFull report saved to: {output_path}")


if __name__ == "__main__":
    main()
