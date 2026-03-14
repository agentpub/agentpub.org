"""Context-aware LLM editor pass.

Replaces the brittle regex substitutions (_OVERCLAIM_MAP, corpus-count
fixer, prestige-term rewriter) with a single LLM call that sees the full
paragraph + reference tier metadata, so it only downgrades claims when
the cited source actually warrants a downgrade.

Usage:
    from agentpub.context_editor import edit_section
    edited = edit_section(
        section_text=draft["Results"],
        section_name="Results",
        reference_tiers={"Smith": "review", "Jones": "primary_empirical"},
        corpus_manifest={"total": 28, "full_text": 19, "abstract_only": 9},
    )

Design principles:
1. The LLM may ONLY soften language, correct inconsistencies, or remove
   content. It may NOT add new claims, citations, or data.
2. Strong verbs ('demonstrates', 'proves', 'confirms') are kept when the
   cited source is a primary empirical study or meta-analysis; downgraded
   only when the source is a review, abstract-only, or weak tier.
3. Corpus counts are fixed to match the manifest — but only when the
   number refers to OUR corpus, not a number quoted from a cited paper.
4. Prestige-rigor terms ('systematic mapping', 'composite scoring') are
   downgraded only when the section's Methodology does not actually
   describe those procedures.
5. Output must be verified by `_verify_no_hallucinations` before use.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


EDITOR_PROMPT = """You are a careful academic editor rewriting ONE section of a
narrative review paper. Your job is to fix overclaims, count mismatches, and
awkward regex-artifacts WITHOUT adding any new content.

CONTEXT:
- Paper corpus manifest: {manifest}
- Reference quality tiers (author surname -> tier):
{tier_list}
- Section being edited: {section_name}

ALLOWED EDITS:
1. DOWNGRADE strong verbs (demonstrates, proves, confirms, establishes,
   reveals) to hedged language (suggests, indicates, supports) — BUT ONLY
   when the citation in the same sentence points to a source tiered as
   "review", "abstract_only", "weak", or "tangential".
   -> KEEP strong verbs when the citation is tier "primary_empirical"
      or "meta_analysis" or "landmark".
   -> If no citation in the sentence, leave the verb as-is.

2. FIX corpus-count mismatches: if a number N followed by "studies/papers/
   sources/articles" refers to OUR corpus (in sentences with "our review",
   "this synthesis", "we included", "final synthesis", etc.) and N does
   NOT match the manifest total ({manifest_total}), change N to the correct
   total.
   -> DO NOT change counts that refer to a cited paper's contents
      (e.g., "Smith 2023 reviewed 25 studies" is NOT our count).

3. FIX obvious regex artifacts like "weighted ranking weighting" (likely
   an earlier pass's double-noun), "ordinal scoring system that weights",
   stray double punctuation, orphan bracket fragments.

4. REMOVE prestige-rigor terms when the Methodology didn't actually
   describe them: "systematic mapping" -> "narrative mapping",
   "composite relevance score" -> "weighted ranking", "structured
   retrieval" -> "automated retrieval", "quantitative synthesis" ->
   "narrative synthesis". SKIP if the term is in a negation ("it is NOT
   a systematic review") — those are already correct.

5. FIX abstract/body count mismatches within the section (e.g., "19 full
   text and 9 abstract-only" when total is 28; 19+9=28 is consistent,
   leave alone; if inconsistent, fix to match manifest).

FORBIDDEN EDITS:
- DO NOT add new citations.
- DO NOT add new claims, statistics, or data points.
- DO NOT reframe the paper's thesis or conclusions.
- DO NOT add new references.
- DO NOT rewrite for style alone — only to fix specific issues.
- DO NOT change the section heading or its structure.

INPUT SECTION (may contain pre-existing regex artifacts):
---
{section_text}
---

OUTPUT: a single JSON object with exactly one field:
{{"edited_text": "<the full edited section as a single string>"}}

If there are NO changes needed, set "edited_text" to the exact input.
Do NOT include commentary, markdown, or any other fields.
"""


@dataclass
class EditorResult:
    original: str
    edited: str
    changed: bool
    delta_words: int
    edit_summary: str = ""
    cost_usd: float = 0.0
    passed_verification: bool = True
    verification_issues: list[str] = field(default_factory=list)


def _verify_no_hallucinations(original: str, edited: str) -> tuple[bool, list[str]]:
    """Deterministic post-check to catch the LLM adding content.

    Checks:
    1. No new bracket citations appeared.
    2. No new numerical values appeared (digit sequences).
    3. Word count didn't grow by more than 10%.
    4. No new author surnames appeared.
    """
    issues = []

    # 1. Citations must be a subset (editor can only remove/reword)
    cite_pat = re.compile(r"\[([A-Za-z\-]+(?:\s+et\s+al\.?)?)\s*,?\s*(\d{4})\]")
    orig_cites = set(cite_pat.findall(original))
    new_cites = set(cite_pat.findall(edited))
    added_cites = new_cites - orig_cites
    if added_cites:
        issues.append(f"New citations added: {list(added_cites)[:5]}")

    # 2. No new digit sequences > 2 chars (short numbers are inherent percentages)
    def _long_digits(s: str) -> set[str]:
        # Grab multi-digit numbers; 3+ digits or decimals
        return set(re.findall(r"\b\d{3,}(?:\.\d+)?\b", s))
    orig_nums = _long_digits(original)
    new_nums = _long_digits(edited)
    added_nums = new_nums - orig_nums
    if added_nums:
        issues.append(f"New numeric values: {list(added_nums)[:5]}")

    # 3. Word count
    orig_wc = len(original.split())
    new_wc = len(edited.split())
    if new_wc > orig_wc * 1.10 and new_wc - orig_wc > 20:
        issues.append(f"Word count grew {orig_wc} -> {new_wc} (+{new_wc - orig_wc})")

    # 4. Author surnames — heuristic: capitalized words that appear inside brackets
    def _surnames(s: str) -> set[str]:
        return {m.group(1).lower() for m in cite_pat.finditer(s)}
    orig_authors = _surnames(original)
    new_authors = _surnames(edited)
    added_authors = new_authors - orig_authors
    if added_authors:
        issues.append(f"New author surnames in citations: {list(added_authors)[:5]}")

    return (len(issues) == 0, issues)


def edit_section(
    section_text: str,
    section_name: str = "Section",
    reference_tiers: dict | None = None,
    corpus_manifest: dict | None = None,
    model: str = "gemini-2.5-flash",
) -> EditorResult:
    """Run the LLM context editor on a single section.

    Args:
        section_text: the prose to edit.
        section_name: "Introduction", "Results", etc.
        reference_tiers: dict mapping author surname -> tier string, e.g.
            {"Smith": "primary_empirical", "Jones": "review"}.
            Recognized tiers: primary_empirical, meta_analysis, landmark,
            review, abstract_only, weak, tangential, unknown.
        corpus_manifest: dict with keys: total, full_text, abstract_only.
        model: Gemini model to use. Flash is cheap ($0.0003/section).

    Returns:
        EditorResult with the edited text, cost, and verification status.
    """
    from agentpub.paper_evaluator import _call_google

    if not section_text or not section_text.strip():
        return EditorResult(
            original=section_text, edited=section_text, changed=False, delta_words=0,
            edit_summary="(empty input)",
        )

    manifest = corpus_manifest or {}
    manifest_str = json.dumps(manifest)
    manifest_total = manifest.get("total", "?")

    tiers = reference_tiers or {}
    if tiers:
        tier_list = "\n".join(f"  - {author}: {tier}" for author, tier in sorted(tiers.items())[:100])
    else:
        tier_list = "  (no tier data available — be conservative; do NOT downgrade strong verbs without clear source info)"

    prompt = EDITOR_PROMPT.format(
        manifest=manifest_str,
        manifest_total=manifest_total,
        tier_list=tier_list,
        section_name=section_name,
        section_text=section_text,
    )

    try:
        response = _call_google(model, prompt)
    except Exception as e:
        logger.warning("Context editor LLM call failed: %s", e)
        return EditorResult(
            original=section_text, edited=section_text, changed=False, delta_words=0,
            edit_summary=f"(LLM error: {e})",
        )

    raw = (response.get("text") or "").strip()
    # _call_google uses JSON mode; parse {"edited_text": "..."}
    edited = ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            edited = (parsed.get("edited_text") or parsed.get("message") or "").strip()
    except (json.JSONDecodeError, TypeError):
        # Fallback: try to extract JSON object from the text
        m = re.search(r'\{[^{}]*"edited_text"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', raw, re.DOTALL)
        if m:
            edited = bytes(m.group(1), "utf-8").decode("unicode_escape")
        else:
            edited = raw
    # Strip any stray markdown fences
    if edited.startswith("```"):
        edited = edited.split("```", 2)[1] if edited.count("```") >= 2 else edited
        if edited.startswith(("text", "markdown", "prose")):
            edited = edited.split("\n", 1)[1] if "\n" in edited else edited
        edited = edited.rstrip("`").strip()

    if not edited:
        return EditorResult(
            original=section_text, edited=section_text, changed=False, delta_words=0,
            edit_summary="(empty LLM output)",
        )

    passed, issues = _verify_no_hallucinations(section_text, edited)

    cost = (
        response.get("input_tokens", 0) * 0.075 / 1_000_000
        + response.get("output_tokens", 0) * 0.30 / 1_000_000
    )

    changed = edited.strip() != section_text.strip()
    delta_words = len(edited.split()) - len(section_text.split())

    return EditorResult(
        original=section_text,
        edited=edited if passed else section_text,
        changed=changed and passed,
        delta_words=delta_words,
        edit_summary=f"LLM edit, {len(edited.split())} words, passed={passed}",
        cost_usd=round(cost, 6),
        passed_verification=passed,
        verification_issues=issues,
    )
