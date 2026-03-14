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

# Model registry: id, provider, display name, input $/1M, output $/1M
MODELS = {
    "gemini-flash": {
        "provider": "google",
        "model": "gemini-2.5-flash",
        "name": "Gemini 2.5 Flash",
        "input_cost": 0.15,
        "output_cost": 0.60,
    },
    "gemini-pro": {
        "provider": "google",
        "model": "gemini-2.5-pro",
        "name": "Gemini 2.5 Pro",
        "input_cost": 1.25,
        "output_cost": 10.00,
    },
    "gpt-5.4-mini": {
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "name": "GPT-5.4 mini",
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
        "model": "claude-opus-4-6",
        "name": "Claude Opus 4.6",
        "input_cost": 5.00,
        "output_cost": 25.00,
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

## HARD-FAIL FLAGS
Flag any of these if present (these override scores):
- Fabricated or unverifiable references
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
DOI link: {doi_link}

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
   - The playbook instructions (AGENT_PLAYBOOK.md, WRITING_RULES.md, RESEARCH_GUIDE.md)?
   - The SDK code (playbook_researcher.py, academic_search.py)?
   - The utility scripts (agentpub_utils.py)?
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


def _call_google(model: str, prompt: str) -> dict:
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
            "maxOutputTokens": 16384,
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
    payload = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
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
    payload = {
        "model": model,
        "max_tokens": 16384,
        "temperature": 0.2,
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

    text = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")

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
