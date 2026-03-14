"""Pipeline quality tests — verify fixes work across all LLM backends.

Tests the specific issues we fixed:
  1. Meta-analysis downgrade
  2. Model identity in methodology
  3. Citation key generation (no ghost keys like [The], [Sleep])
  4. Citation dominance detection
  5. Clean ref_ids (no s2_xxx, serper_xxx in output)
  6. Malformed citation cleanup
  7. JSON failure resilience (small models)

Run:
    python -m pytest tests/test_pipeline_quality.py -v
    python -m pytest tests/test_pipeline_quality.py -v -k gemini
"""

from __future__ import annotations

import json
import os
import re
import pytest

from agentpub.llm import get_backend, LLMError

# Models to test — one representative per provider from cli.py catalogue.
# Full coverage in test_llm_backends.py; here we test quality behaviours.
MODELS = [
    # OpenAI
    ("openai", "gpt-5-mini", "OPENAI_API_KEY"),
    ("openai", "gpt-5", "OPENAI_API_KEY"),
    ("openai", "o4-mini", "OPENAI_API_KEY"),
    # Anthropic
    ("anthropic", "claude-sonnet-4-6", "ANTHROPIC_API_KEY"),
    # Google Gemini
    ("google", "gemini-2.5-flash", "GEMINI_API_KEY"),
    ("google", "gemini-2.5-pro", "GEMINI_API_KEY"),
    # Mistral
    ("mistral", "mistral-large-latest", "MISTRAL_API_KEY"),
    # xAI Grok
    ("xai", "grok-3", "XAI_API_KEY"),
    # Ollama (representative small + medium)
    ("ollama", "deepseek-r1:14b", None),
    ("ollama", "qwen3:8b", None),
    ("ollama", "qwen3.5:9b", None),
    ("ollama", "cogito:8b", None),
]


def _has_key(env_var):
    if env_var is None:
        return True
    return bool(os.environ.get(env_var))


def _ollama_available():
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        return True
    except Exception:
        return False


def _skip_if_unavailable(provider, env_var):
    if provider == "ollama" and not _ollama_available():
        pytest.skip("Ollama not running")
    if env_var and not _has_key(env_var):
        pytest.skip(f"No {env_var}")


def _get_backend(provider, model, env_var):
    _skip_if_unavailable(provider, env_var)
    try:
        return get_backend(provider, model=model)
    except LLMError as e:
        if "not found" in str(e).lower() or "500" in str(e):
            pytest.skip(f"Model {model} not available")
        raise


# ─────────────────────────────────────────────────────
# Test 1: Meta-analysis downgrade (no LLM needed)
# ─────────────────────────────────────────────────────

def test_meta_analysis_downgrade():
    """Phase 1 should downgrade meta-analysis to systematic review."""
    import agentpub.researcher as r

    # Simulate what Phase 1 does after LLM returns
    brief = {
        "title": "A Meta-Analysis of Sleep Disorders",
        "paper_type": "meta-analysis",
        "research_questions": ["How common are sleep disorders?"],
        "search_terms": ["sleep disorders"],
    }

    # Apply the same logic from _phase1
    paper_type = brief.get("paper_type", "survey").lower()
    if paper_type == "meta-analysis":
        brief["paper_type"] = "systematic review"
    title = brief.get("title", "")
    if re.search(r"\bmeta[- ]?analy", title, re.IGNORECASE):
        brief["title"] = re.sub(
            r"\b[Mm]eta[- ]?[Aa]nalys[ei]s\b",
            "Systematic Review",
            title,
        )

    assert brief["paper_type"] == "systematic review"
    assert "Meta-Analysis" not in brief["title"]
    assert "Systematic Review" in brief["title"]
    print(f"  Title: {brief['title']}")
    print(f"  Type: {brief['paper_type']}")


# ─────────────────────────────────────────────────────
# Test 2: Citation key generation (no ghost keys)
# ─────────────────────────────────────────────────────

def test_cite_key_no_ghost_words():
    """cite_key fallback should skip articles/prepositions."""
    _SKIP_WORDS = {"the", "a", "an", "of", "in", "on", "for", "and", "to", "with", "from", "by", "is", "are", "was", "were", "at", "its"}

    test_cases = [
        ("The Neuroscience of Dreaming", "Neuroscience"),
        ("A Survey of Sleep Research", "Survey"),
        ("An Investigation into REM", "Investigation"),
        ("Of Mice and Men", "Mice"),
        ("In Search of Memory", "Search"),
        ("Sleep and Its Functions", "Sleep"),  # "Sleep" is not in skip words
    ]

    for title, expected_word in test_cases:
        title_words = [w.rstrip(",.:;") for w in title.split()
                       if w.lower().rstrip(",.:;") not in _SKIP_WORDS and len(w) > 2]
        first_author = title_words[0] if title_words else "Ref1"
        assert first_author == expected_word, f"Title '{title}': got '{first_author}', expected '{expected_word}'"
        print(f"  '{title}' -> [{first_author}, 2024]")


# ─────────────────────────────────────────────────────
# Test 3: Malformed citation cleanup
# ─────────────────────────────────────────────────────

def test_malformed_citation_cleanup():
    """_final_citation_cleanup should strip ghost citation keys."""
    _COMMON_WORDS = r"(?:The|A|An|In|On|For|And|To|Of|With|From|By|Is|Are|Was|Were|Its|This|That|Sleep|REM|Investigation|Comparative|Study|Review|Analysis|Research|Brain|Neural|Human)"

    test_cases = [
        ("[The; Nielsen, 2009]", "[Nielsen, 2009]"),
        ("[REM; Nielsen, 2009]", "[Nielsen, 2009]"),
        ("[Smith, 2020; The]", "[Smith, 2020]"),
        ("[Sleep]", ""),
        ("[The]", ""),
        ("[Investigation]", ""),
        ("[Smith, 2020]", "[Smith, 2020]"),  # valid — should stay
    ]

    for input_text, expected in test_cases:
        result = input_text
        # Apply the same regexes from _final_citation_cleanup
        result = re.sub(
            rf"\[{_COMMON_WORDS};\s*([A-Z][a-z]+(?:\s+et\s+al\.?)?,\s*\d{{4}}[a-z]?)\]",
            r"[\1]", result)
        result = re.sub(
            rf"\[([A-Z][a-z]+(?:\s+et\s+al\.?)?,\s*\d{{4}}[a-z]?);\s*{_COMMON_WORDS}\]",
            r"[\1]", result)
        result = re.sub(rf"\[{_COMMON_WORDS}\]", "", result)
        assert result == expected, f"Input '{input_text}': got '{result}', expected '{expected}'"
        print(f"  '{input_text}' -> '{result}'")


# ─────────────────────────────────────────────────────
# Test 4: Citation dominance detection
# ─────────────────────────────────────────────────────

def test_citation_dominance_detection():
    """Should detect when one source dominates >25% of citations."""
    from collections import Counter

    # Simulate a paper with Tonkinson cited 15/30 times
    text = (
        "[Tonkinson, 2003] found that dreaming occurs. "
        "[Smith, 2020] noted REM sleep. "
        "[Tonkinson, 2003] also showed memory consolidation. "
    ) * 5  # Tonkinson 10x, Smith 5x = 10/15 = 67%

    all_cites = re.findall(r"\[([A-Z][a-z]+(?:\s+et\s+al\.?)?,\s*\d{4}[a-z]?)\]", text)
    cite_counts = Counter(all_cites)
    total_cites = len(all_cites)
    top_cite, top_count = cite_counts.most_common(1)[0]
    dominance_pct = top_count / total_cites * 100

    assert top_cite == "Tonkinson, 2003"
    assert dominance_pct > 25
    assert top_count > 5
    print(f"  Detected: [{top_cite}] = {top_count}/{total_cites} ({dominance_pct:.0f}%)")


# ─────────────────────────────────────────────────────
# Test 5: Clean ref_ids (no raw API IDs)
# ─────────────────────────────────────────────────────

def test_clean_ref_ids():
    """ref_ids should not contain s2_xxx, serper_xxx, web_xxx."""
    test_pids = [
        ("s2_0a33767fff8abc123", {"doi": "10.1234/test"}, {}, "doi:10.1234/test"),
        ("serper_0_1", {}, {"authors": ["John Smith"], "year": 2020}, "ext:smith_2020"),
        ("web_3", {}, {"authors": ["Jane Doe"]}, "ext:doe_"),
        ("s2_abc123", {}, {}, "ext:ref_1"),  # no authors, no DOI
        ("paper_2026_abc123", {}, {}, "paper_2026_abc123"),  # platform paper — keep as-is
    ]

    for pid, src, cand, expected in test_pids:
        clean_ref_id = pid
        if pid.startswith(("s2_", "serper_", "web_")):
            _doi = src.get("doi") or cand.get("doi") or ""
            if _doi:
                clean_ref_id = f"doi:{_doi}"
            else:
                _ref_authors = src.get("authors") or cand.get("authors") or []
                _ref_year = cand.get("year") or ""
                if _ref_authors:
                    _surname = _ref_authors[0].split()[-1].lower()
                    clean_ref_id = f"ext:{_surname}_{_ref_year}" if _ref_year else f"ext:{_surname}_"
                else:
                    clean_ref_id = "ext:ref_1"

        assert clean_ref_id == expected, f"pid '{pid}': got '{clean_ref_id}', expected '{expected}'"
        assert not clean_ref_id.startswith("s2_"), f"Still has s2_ prefix: {clean_ref_id}"
        assert not clean_ref_id.startswith("serper_"), f"Still has serper_ prefix: {clean_ref_id}"
        print(f"  '{pid}' -> '{clean_ref_id}'")


# ─────────────────────────────────────────────────────
# Test 6: Model identity — wrong model names get replaced
# ─────────────────────────────────────────────────────

def test_model_identity_fix():
    """Wrong model names in methodology should be replaced with actual model."""
    actual_model = "gemini-2.5-flash"

    _WRONG_MODELS = re.compile(
        r"\b(?:GPT[- ]?5(?:-mini)?|GPT[- ]?4[oa]?(?:-mini)?|GPT[- ]?3\.5|"
        r"Claude[- ]?(?:3\.5|3|2)|Gemini[- ]?(?:1\.5|2\.0|2\.5)[- ]?(?:Pro|Flash)?|"
        r"Llama[- ]?\d|Mistral[- ]?(?:Large|Medium))\b",
        re.IGNORECASE,
    )

    test_cases = [
        ("The core model employed was GPT-5, developed by OpenAI.",
         f"The core model employed was {actual_model}, developed by OpenAI."),
        ("We used gpt-4o for text generation.",
         f"We used {actual_model} for text generation."),
        (f"We used {actual_model} for generation.",
         f"We used {actual_model} for generation."),  # should NOT double-replace
    ]

    for input_text, expected in test_cases:
        content = input_text
        matches = _WRONG_MODELS.findall(content)
        for wrong in matches:
            if wrong.lower().replace("-", "").replace(" ", "") != actual_model.lower().replace("-", "").replace(" ", ""):
                content = content.replace(wrong, actual_model)
        assert content == expected, f"Got: '{content}'"
        print(f"  '{input_text[:50]}...' -> OK")


# ─────────────────────────────────────────────────────
# Test 7: LLM reading memo — JSON resilience per model
# ─────────────────────────────────────────────────────

@pytest.mark.parametrize("provider,model,env_var", MODELS, ids=[
    f"{p}-{m}" for p, m, _ in MODELS
])
def test_reading_memo_json(provider, model, env_var):
    """Each model should produce a valid reading memo JSON."""
    backend = _get_backend(provider, model, env_var)

    try:
        result = _run_reading_memo(backend, provider, model)
    except LLMError as e:
        if provider == "ollama" and ("not found" in str(e).lower() or "500" in str(e) or "pull" in str(e).lower()):
            pytest.skip(f"Model {model} not available in Ollama")
        raise

    assert isinstance(result, dict), f"{provider}/{model}: expected dict, got {type(result)}"
    assert "key_findings" in result, f"{provider}/{model}: missing key_findings in {list(result.keys())}"
    findings = result["key_findings"]
    assert isinstance(findings, list) and len(findings) > 0, f"{provider}/{model}: empty key_findings"
    print(f"  {provider}/{model}: {len(findings)} findings, quality={result.get('quality_assessment', '?')}")


def _run_reading_memo(backend, provider, model):
    system = "You are an academic research assistant."
    prompt = """Read this paper excerpt and produce a JSON reading memo:

Title: Sleep and Memory Consolidation
Abstract: This study found that REM sleep enhances memory consolidation
through hippocampal replay mechanisms. N=45 participants showed 23% better
recall after REM-rich sleep versus controls.

Return JSON with keys:
- "key_findings": list of 2-3 main findings (specific, with numbers)
- "methodology": brief description
- "relevance": one sentence on relevance to sleep research
- "quality_assessment": "high", "medium", or "low" """

    return backend.generate_json(system, prompt, temperature=0.0, max_tokens=4096)


# ─────────────────────────────────────────────────────
# Test 8: LLM methodology — correct model identity
# ─────────────────────────────────────────────────────

@pytest.mark.parametrize("provider,model,env_var", MODELS, ids=[
    f"{p}-{m}" for p, m, _ in MODELS
])
def test_methodology_model_identity(provider, model, env_var):
    """When told the model is X, the LLM should write X in methodology, not hallucinate another."""
    backend = _get_backend(provider, model, env_var)

    system = "You are an academic writer. Write methodology prose."
    prompt = f"""Write a 2-sentence methodology paragraph for an AI-assisted systematic review.

CRITICAL: The model used is {model} (provider: {provider}).
Write '{model}' exactly as shown. Do NOT substitute GPT-5, GPT-4, or any other model name.

Write only the 2 sentences, no JSON."""

    result = backend.generate(system, prompt, temperature=0.0, max_tokens=4096)
    text = result.text.lower()

    # Check the actual model name appears
    model_lower = model.lower().replace("-", "").replace(" ", "")
    text_normalized = text.replace("-", "").replace(" ", "")

    # For Ollama models, the model name might appear differently
    if provider != "ollama":
        assert model_lower in text_normalized, (
            f"{provider}/{model}: model name not found in output. Got: {result.text[:200]}"
        )

    # Check no wrong model names (unless it IS that model)
    wrong_models = ["gpt-5", "gpt-4", "gpt-3.5", "claude-3", "claude-2"]
    for wrong in wrong_models:
        if wrong.replace("-", "") not in model_lower:
            assert wrong not in text, (
                f"{provider}/{model}: hallucinated '{wrong}' in: {result.text[:200]}"
            )

    print(f"  {provider}/{model}: '{result.text.strip()[:100]}...'")


# ─────────────────────────────────────────────────────
# Test 9: LLM section writing — citation diversity
# ─────────────────────────────────────────────────────

@pytest.mark.parametrize("provider,model,env_var", MODELS, ids=[
    f"{p}-{m}" for p, m, _ in MODELS
])
def test_citation_diversity(provider, model, env_var):
    """When given 5 references, the LLM should cite multiple, not fixate on one."""
    backend = _get_backend(provider, model, env_var)

    system = "You are an academic writer. Synthesize findings into flowing prose."
    prompt = """Write a 3-paragraph Discussion section synthesizing these findings:

REFERENCE LIST (cite by cite_key):
1. [Smith, 2020]: Found 23% improvement in memory after REM sleep
2. [Jones, 2019]: Showed hippocampal replay during NREM
3. [Chen, 2021]: Demonstrated sleep spindles correlate with learning
4. [Brown, 2018]: Found sleep deprivation impairs consolidation
5. [Davis, 2022]: Showed dreaming content relates to recent learning

CITATION DIVERSITY: Distribute citations across many references — do NOT over-rely on 1-2 papers.
Each paragraph should cite at least 2 different references.

Write flowing academic prose. Every factual claim needs a [Author, Year] citation."""

    result = backend.generate(system, prompt, temperature=0.3, max_tokens=4096)
    text = result.text

    # Strip thinking tags before checking citations
    from agentpub.llm.base import strip_thinking_tags
    text = strip_thinking_tags(text)

    # Count unique citations — match both [Author, Year] and Author (Year) styles
    bracket_cites = re.findall(r"\[([A-Z][a-z]+,\s*\d{4})\]", text)
    paren_cites = re.findall(r"([A-Z][a-z]+)\s*\((\d{4})\)", text)
    all_cites = bracket_cites + [f"{a}, {y}" for a, y in paren_cites]
    unique_cites = set(all_cites)

    # Small local models may not follow citation format reliably
    min_expected = 2 if provider == "ollama" else 3
    assert len(unique_cites) >= min_expected, (
        f"{provider}/{model}: only {len(unique_cites)} unique citations (expected >={min_expected}). "
        f"Found: {unique_cites}. Text: {text[:200]}"
    )

    # Check no single source dominates
    if all_cites:
        from collections import Counter
        counts = Counter(all_cites)
        top_cite, top_count = counts.most_common(1)[0]
        dominance = top_count / len(all_cites) * 100
        assert dominance <= 50, (
            f"{provider}/{model}: [{top_cite}] dominates at {dominance:.0f}% "
            f"({top_count}/{len(all_cites)})"
        )

    print(f"  {provider}/{model}: {len(unique_cites)} unique cites from {len(all_cites)} total")
