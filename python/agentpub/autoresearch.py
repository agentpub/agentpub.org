"""AutoResearch — self-improving paper quality loop.

Inspired by Karpathy's autoresearch: an automated loop that generates a paper,
evaluates quality with deterministic metrics, identifies failures, and iterates
with improved prompts/post-processing until quality thresholds are met.

The key insight: H1 (search) and H2 (extraction) only run once. The loop only
re-runs H3 (synthesis) and H4 (audit), saving ~80% of LLM cost per iteration.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger("agentpub.autoresearch")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MetricResult:
    """Result for a single quality metric."""
    name: str
    score: float            # 0-100
    weight: float           # importance weight
    details: dict = field(default_factory=dict)
    failing: bool = False   # True if score < threshold


@dataclass
class EvaluationReport:
    """Full evaluation of a generated paper."""
    metrics: list[MetricResult]
    composite_score: float
    passing: bool
    worst_metrics: list[str]
    iteration: int = 0

    def summary(self) -> str:
        lines = [f"Iteration {self.iteration} — Composite: {self.composite_score:.1f}/100 ({'PASS' if self.passing else 'FAIL'})"]
        for m in sorted(self.metrics, key=lambda x: x.score):
            status = "PASS" if not m.failing else "FAIL"
            lines.append(f"  [{status}] {m.name}: {m.score:.0f}/100 (weight={m.weight})")
            if m.failing and m.details:
                for k, v in m.details.items():
                    if isinstance(v, list) and v:
                        lines.append(f"        {k}: {v[:3]}{'...' if len(v) > 3 else ''}")
                    elif isinstance(v, (int, float, str)):
                        lines.append(f"        {k}: {v}")
        return "\n".join(lines)


@dataclass
class OptimizationPlan:
    """What to change for the next iteration."""
    weakness_summary: str
    code_fixes: list[str]
    priority_metrics: list[str]


# ---------------------------------------------------------------------------
# PaperEvaluator — deterministic quality scoring (no LLM needed)
# ---------------------------------------------------------------------------

# Journal name blocklist — these are metadata source names, not actual journals
_JOURNAL_BLOCKLIST = {
    "crossref", "semantic_scholar", "openalex", "arxiv", "scholar",
    "agentpub", "web", "unpaywall", "pmc", "pubmed",
}


class PaperEvaluator:
    """Scores a paper on 10 quality metrics, all deterministic Python."""

    def __init__(self, pass_threshold: float = 75.0, metric_threshold: float = 60.0):
        self.pass_threshold = pass_threshold
        self.metric_threshold = metric_threshold

    def evaluate(self, paper: dict, iteration: int = 0) -> EvaluationReport:
        """Evaluate a paper dict (same structure as saved JSON).

        Expected keys: sections (list of {heading, content}), references (list),
        abstract (str).
        """
        # Convert sections list to dict for easier processing
        draft = {}
        for s in paper.get("sections", []):
            draft[s["heading"]] = s["content"]

        refs = paper.get("references", [])
        abstract = paper.get("abstract", "")

        metrics = [
            self._score_paraphrase_repetition(draft),
            self._score_orphan_references(draft, refs, abstract),
            self._score_quantitative_density(draft),
            self._score_journal_metadata(refs),
            self._score_citation_format_consistency(draft, refs),
            self._score_results_vs_litreview(draft),
            self._score_markdown_leakage(draft),
            self._score_section_word_counts(draft),
            self._score_citation_density(draft),
            self._score_fabrication_markers(draft),
        ]

        # Mark failing
        for m in metrics:
            m.failing = m.score < self.metric_threshold

        # Weighted composite
        total_weight = sum(m.weight for m in metrics)
        if total_weight > 0:
            composite = sum(m.score * m.weight for m in metrics) / total_weight
        else:
            composite = 0.0

        worst = sorted([m.name for m in metrics if m.failing],
                       key=lambda n: next(m.score for m in metrics if m.name == n))

        return EvaluationReport(
            metrics=metrics,
            composite_score=composite,
            passing=composite >= self.pass_threshold,
            worst_metrics=worst,
            iteration=iteration,
        )

    # -- Individual metrics --------------------------------------------------

    def _score_paraphrase_repetition(self, draft: dict[str, str]) -> MetricResult:
        """Detect paraphrased repetition across sections using sentence-level trigrams."""
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

        # Collect sentences by section
        section_sentences: dict[str, list[str]] = {}
        for heading, content in draft.items():
            sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", content) if len(s.split()) >= 8]
            section_sentences[heading] = sents

        # Cross-section comparison
        duplicates: list[dict] = []
        headings = list(section_sentences.keys())
        for i, h1 in enumerate(headings):
            for h2 in headings[i + 1:]:
                for s1 in section_sentences[h1]:
                    for s2 in section_sentences[h2]:
                        sim = _sim(s1, s2)
                        if sim > 0.40:
                            duplicates.append({
                                "sections": f"{h1} <-> {h2}",
                                "similarity": round(sim, 2),
                                "sentence1": s1[:80],
                                "sentence2": s2[:80],
                            })

        score = max(0, 100 - len(duplicates) * 5)
        return MetricResult(
            name="paraphrase_repetition",
            score=score,
            weight=15,
            details={"duplicate_pairs": len(duplicates), "examples": duplicates[:5]},
        )

    def _score_orphan_references(self, draft: dict[str, str], refs: list[dict], abstract: str = "") -> MetricResult:
        """Check for references never cited in text."""
        all_text = abstract + "\n" + "\n".join(draft.values())

        # Extract cited surnames from [Author, Year] patterns
        cited_surnames = set()
        for m in re.finditer(r"\[([A-Z][a-zA-Z\-']+)", all_text):
            cited_surnames.add(m.group(1))
        # Also catch (Author, Year) style
        for m in re.finditer(r"\(([A-Z][a-zA-Z\-']+(?:\s+et\s+al\.?)?,\s*\d{4})\)", all_text):
            surname = m.group(1).split(",")[0].split()[0]
            cited_surnames.add(surname)

        orphans = []
        for ref in refs:
            authors = ref.get("authors", [])
            if not authors:
                continue
            first_author = authors[0] if isinstance(authors[0], str) else authors[0].get("name", "")
            surname = first_author.split()[-1] if first_author else ""
            if surname and surname not in cited_surnames:
                orphans.append(f"{surname} ({ref.get('year', '?')}): {ref.get('title', '?')[:50]}")

        total = len(refs) if refs else 1
        score = max(0, 100 - (len(orphans) / total * 100))
        return MetricResult(
            name="orphan_references",
            score=score,
            weight=10,
            details={"orphan_count": len(orphans), "total_refs": total, "orphans": orphans},
        )

    def _score_quantitative_density(self, draft: dict[str, str]) -> MetricResult:
        """Count numeric/quantitative tokens in Results section."""
        results_text = draft.get("Results", "")
        if not results_text:
            return MetricResult(name="quantitative_density", score=0, weight=15,
                                details={"reason": "No Results section"})

        # Count numeric patterns
        patterns = [
            r"\d+(?:\.\d+)?%",                    # percentages
            r"[Nn]\s*=\s*\d+",                    # sample sizes
            r"[Pp]\s*[<>=]\s*0?\.\d+",            # p-values
            r"[dr]\s*=\s*[\d.]+",                 # effect sizes
            r"CI\s*[\[=(]",                        # confidence intervals
            r"\d+(?:\.\d+)?\s*(?:mg|kg|ml|cm|mm)", # measurements
            r"(?:^|\s)\d{2,}\s",                   # standalone numbers >= 10
            r"\d+\s*(?:of|out of)\s*\d+",         # ratios like "11 of 15"
            r"(?:mean|median|average)\s*(?:=|of|was)\s*[\d.]", # statistics
        ]
        count = 0
        for p in patterns:
            count += len(re.findall(p, results_text, re.IGNORECASE))

        # Also count any bare numbers in context
        bare_numbers = re.findall(r"\b\d+(?:\.\d+)?\b", results_text)
        count += len([n for n in bare_numbers if float(n) > 1])  # exclude 0, 1

        score = min(100, count * 4)  # target: ~25 numeric tokens for full marks
        return MetricResult(
            name="quantitative_density",
            score=score,
            weight=15,
            details={"numeric_token_count": count, "results_words": len(results_text.split())},
        )

    def _score_journal_metadata(self, refs: list[dict]) -> MetricResult:
        """Check for placeholder journal names (e.g., 'crossref')."""
        bad = []
        for ref in refs:
            j = (ref.get("journal") or "").strip().lower()
            if j in _JOURNAL_BLOCKLIST:
                bad.append(f"{ref.get('title', '?')[:40]} -> journal='{j}'")
        total = len(refs) if refs else 1
        score = max(0, 100 - (len(bad) / total * 100))
        return MetricResult(
            name="journal_metadata",
            score=score,
            weight=5,
            details={"bad_journals": len(bad), "examples": bad[:5]},
        )

    def _score_citation_format_consistency(self, draft: dict[str, str], refs: list[dict]) -> MetricResult:
        """Check that same paper is always cited with consistent format."""
        all_text = "\n".join(draft.values())

        # Extract all citation patterns
        cite_re = re.compile(r"\[([A-Z][a-zA-Z\-']+(?:\s+(?:et\s+al\.?|&\s*[A-Z][a-zA-Z\-']+))?,\s*\d{4}[a-z]?)\]")
        citations = cite_re.findall(all_text)

        # Group by (first_surname, year)
        groups: dict[tuple[str, str], set[str]] = {}
        for cite in citations:
            parts = cite.split(",")
            surname_part = parts[0].strip().split()[0]
            year_part = parts[-1].strip()
            key = (surname_part, year_part)
            groups.setdefault(key, set()).add(cite)

        inconsistent = []
        for (surname, year), formats in groups.items():
            if len(formats) > 1:
                inconsistent.append({
                    "author_year": f"{surname}, {year}",
                    "formats": list(formats),
                })

        score = max(0, 100 - len(inconsistent) * 15)
        return MetricResult(
            name="citation_format_consistency",
            score=score,
            weight=10,
            details={"inconsistent_groups": len(inconsistent), "examples": inconsistent[:5]},
        )

    def _score_results_vs_litreview(self, draft: dict[str, str]) -> MetricResult:
        """Check if Results reads like structured findings vs. literature narration."""
        results_text = draft.get("Results", "")
        if not results_text:
            return MetricResult(name="results_vs_litreview", score=50, weight=15,
                                details={"reason": "No Results section"})

        # Hedging verbs (literature review style)
        hedging = len(re.findall(
            r"\b(?:suggest|indicate|argue|contend|posit|propose|imply|hypothesize)\b",
            results_text, re.IGNORECASE,
        ))
        # Reporting verbs (results style)
        reporting = len(re.findall(
            r"\b(?:found|observed|measured|recorded|reported|showed|demonstrated|revealed|identified|detected)\b",
            results_text, re.IGNORECASE,
        ))
        # Paper-by-paper narration (bad pattern)
        narration = len(re.findall(
            r"\[[\w\s&,.]+,\s*\d{4}\]\s+(?:found|showed|demonstrated|reported|observed|argued|suggested)",
            results_text, re.IGNORECASE,
        ))
        # Synthesis language (good pattern)
        synthesis = len(re.findall(
            r"\b(?:across\s+\d+\s+studies|the\s+majority|collectively|consistently|N\s+of\s+\d+|most\s+studies)\b",
            results_text, re.IGNORECASE,
        ))

        # Score: penalize high hedging ratio and narration, reward synthesis
        total_verbs = hedging + reporting + 1
        hedge_ratio = hedging / total_verbs
        score = 100
        score -= hedge_ratio * 40          # up to -40 for all hedging
        score -= narration * 8             # -8 per narration instance
        score += synthesis * 10            # +10 per synthesis marker
        score = max(0, min(100, score))

        return MetricResult(
            name="results_vs_litreview",
            score=score,
            weight=15,
            details={
                "hedging_verbs": hedging,
                "reporting_verbs": reporting,
                "narration_instances": narration,
                "synthesis_markers": synthesis,
            },
        )

    def _score_markdown_leakage(self, draft: dict[str, str]) -> MetricResult:
        """Detect markdown formatting in paper output."""
        all_text = "\n".join(draft.values())

        leaks = 0
        leaks += len(re.findall(r"\*\*[^*]+\*\*", all_text))        # **bold**
        leaks += len(re.findall(r"(?<!\*)\*(?!\*)[^*]+\*(?!\*)", all_text))  # *italic*
        leaks += len(re.findall(r"^#{1,6}\s", all_text, re.MULTILINE))  # headers
        leaks += len(re.findall(r"`[^`]+`", all_text))              # inline code
        leaks += len(re.findall(r"^[-*+]\s", all_text, re.MULTILINE))  # bullets
        leaks += len(re.findall(r"^>\s", all_text, re.MULTILINE))   # blockquotes

        score = max(0, 100 - leaks * 5)
        return MetricResult(
            name="markdown_leakage",
            score=score,
            weight=5,
            details={"leak_count": leaks},
        )

    def _score_section_word_counts(self, draft: dict[str, str]) -> MetricResult:
        """Check each section meets minimum word targets."""
        from ._constants import _SECTION_WORD_MINIMUMS
        scores = []
        details = {}
        for heading, content in draft.items():
            wc = len(content.split())
            minimum = _SECTION_WORD_MINIMUMS.get(heading, 200)
            sec_score = min(100, wc / minimum * 100) if minimum > 0 else 100
            scores.append(sec_score)
            details[heading] = f"{wc}/{minimum}"

        avg = sum(scores) / len(scores) if scores else 0
        return MetricResult(
            name="section_word_counts",
            score=avg,
            weight=10,
            details=details,
        )

    def _score_citation_density(self, draft: dict[str, str]) -> MetricResult:
        """Check that empirical claims have citations."""
        _CITATION_RE = re.compile(r"\[[A-Z][a-zA-Z\-']+(?:\s+et\s+al\.?)?,\s*\d{4}")
        _EMPIRICAL_RE = re.compile(
            r"(?:studies?\s+(?:have\s+)?(?:shown|demonstrated|found|revealed)"
            r"|evidence\s+(?:suggest|indicate|show)"
            r"|(?:has|have)\s+(?:found|shown|demonstrated)"
            r"|\d+(?:\.\d+)?%"
            r"|(?:higher|lower|greater)\s+than)",
            re.IGNORECASE,
        )
        _EVIDENCE_SECTIONS = {"Introduction", "Related Work", "Results", "Discussion"}

        uncited = 0
        total = 0
        for heading, content in draft.items():
            if heading not in _EVIDENCE_SECTIONS:
                continue
            for para in re.split(r"\n\n+", content):
                if len(para.split()) < 20:
                    continue
                total += 1
                has_citation = bool(_CITATION_RE.search(para))
                has_empirical = bool(_EMPIRICAL_RE.search(para))
                if has_empirical and not has_citation:
                    uncited += 1

        score = max(0, 100 - (uncited / max(total, 1)) * 100) if total > 0 else 100
        return MetricResult(
            name="citation_density",
            score=score,
            weight=10,
            details={"uncited_empirical_paragraphs": uncited, "total_evidence_paragraphs": total},
        )

    def _score_fabrication_markers(self, draft: dict[str, str]) -> MetricResult:
        """Check for fabricated methodology claims."""
        _PATTERNS = [
            r"[Cc]ohen['\u2019]?s?\s+kappa",
            r"inter[- ]?rater\s+reliability",
            r"two\s+independent\s+reviewers",
            r"trained\s+human\s+annotators?\s+validated",
            r"(?:IRB|ethics\s+committee)\s+approval",
            r"informed\s+consent\s+was\s+obtained",
            r"verified\s+by\s+(?:a\s+)?human\s+(?:team|expert|reviewer)",
            r"(?:see|as\s+shown\s+in)\s+(?:Figure|Table)\s+\d",
            r"pooled\s+(?:mean|effect\s+size)\s*[=:]\s*[\d.-]+",
            r"(?:95|99)%?\s*CI\s*[\[=(]\s*[\d.-]+",
            r"I[²2]\s*[=:]\s*\d+",
            r"forest\s+plot",
        ]
        combined = re.compile("|".join(_PATTERNS), re.IGNORECASE)

        hits = 0
        examples = []
        for heading, content in draft.items():
            for m in combined.finditer(content):
                hits += 1
                examples.append(f"{heading}: '{m.group()}'")

        score = max(0, 100 - hits * 20)
        return MetricResult(
            name="fabrication_markers",
            score=score,
            weight=5,
            details={"hit_count": hits, "examples": examples[:5]},
        )


# ---------------------------------------------------------------------------
# PromptOptimizer — maps metric failures to prompt/code fixes
# ---------------------------------------------------------------------------

# Templates for weakness_summary injection (no LLM needed)
_WEAKNESS_TEMPLATES: dict[str, str] = {
    "paraphrase_repetition": (
        "CRITICAL: The previous draft had {duplicate_pairs} instances of cross-section repetition. "
        "Each section MUST contain UNIQUE information. Before writing each section, review the "
        "'Previously written sections' context and do NOT repeat any claim, finding, or example "
        "already covered. If a finding was discussed in Introduction, do NOT mention it again in "
        "Results or Discussion. Specific repeated content to avoid:\n{examples_text}"
    ),
    "orphan_references": (
        "CRITICAL: {orphan_count} of {total_refs} references were never cited in the text. "
        "You MUST cite EVERY reference from the bibliography at least once. Distribute citations "
        "across sections — ensure each reference appears in at least one [Author, Year] citation."
    ),
    "quantitative_density": (
        "CRITICAL: The Results section contained only {numeric_token_count} numeric values. "
        "It MUST contain quantitative data. For EVERY finding, report: how many studies found it, "
        "what percentages/effect sizes were reported, and specific numbers from the source papers. "
        "Target: at least 25 numeric values in Results. Example good sentence: 'Of the 15 studies "
        "examining dysbiosis, 11 (73%) reported reduced microbial diversity, with Shannon index "
        "decreases ranging from 0.3 to 1.2 [Author, Year].' Extract real numbers from the source "
        "materials provided."
    ),
    "results_vs_litreview": (
        "CRITICAL: The Results section reads like a literature review, not structured findings. "
        "Do NOT write paper-by-paper narration ('Author (Year) found X. Other (Year) showed Y.'). "
        "Instead organize by FINDING: 'Finding 1: X was observed across N studies [Author, Year; "
        "Other, Year].' Use counting and synthesis language: 'N of M studies', 'the majority of "
        "reviewed papers', 'a consistent pattern across studies', 'collectively, the evidence suggests'."
    ),
    "citation_format_consistency": (
        "CRITICAL: Use CONSISTENT citation format throughout. For single-author papers: "
        "[Surname, Year]. For 2-author papers: [Surname & Surname, Year]. For 3+ authors: "
        "[Surname et al., Year]. NEVER mix formats for the same paper. Inconsistencies found: "
        "{examples_text}"
    ),
    "markdown_leakage": (
        "CRITICAL: Do NOT use any markdown formatting in the output. No **bold**, no *italic*, "
        "no # headers, no ### sub-headers, no `code`, no - bullets, no > blockquotes. "
        "Write ONLY plain academic prose. Use paragraph breaks (blank lines) for structure, "
        "not markdown headers."
    ),
    "citation_density": (
        "CRITICAL: {uncited_empirical_paragraphs} paragraphs contained empirical claims without "
        "any citation. Every paragraph making a factual or empirical claim MUST include at least "
        "one [Author, Year] citation from the bibliography."
    ),
    "section_word_counts": (
        "CRITICAL: Some sections did not meet minimum word counts. Ensure each section is "
        "substantive: Introduction 600+, Related Work 800+, Methodology 500+, Results 800+, "
        "Discussion 800+, Limitations 300+, Conclusion 250+ words."
    ),
}


class PromptOptimizer:
    """Maps evaluation failures to prompt improvements and code fixes."""

    def plan(self, report: EvaluationReport) -> OptimizationPlan:
        """Generate an optimization plan from evaluation results."""
        failing = [m for m in report.metrics if m.failing]
        if not failing:
            return OptimizationPlan(weakness_summary="", code_fixes=[], priority_metrics=[])

        # Sort by worst first
        failing.sort(key=lambda m: m.score)

        # Build weakness summary from templates
        weakness_parts = []
        code_fixes = []

        for m in failing:
            template = _WEAKNESS_TEMPLATES.get(m.name)
            if template:
                # Format template with metric details
                fmt_kwargs = dict(m.details)
                # Build examples text for some metrics
                if "examples" in m.details and isinstance(m.details["examples"], list):
                    examples = m.details["examples"]
                    if examples and isinstance(examples[0], dict):
                        fmt_kwargs["examples_text"] = "\n".join(
                            f"  - {e.get('sentence1', e.get('formats', str(e)))[:100]}"
                            for e in examples[:5]
                        )
                    else:
                        fmt_kwargs["examples_text"] = "\n".join(f"  - {e}" for e in examples[:5])
                elif "orphans" in m.details:
                    fmt_kwargs["examples_text"] = "\n".join(f"  - {o}" for o in m.details["orphans"][:5])
                else:
                    fmt_kwargs["examples_text"] = "(see details above)"

                try:
                    weakness_parts.append(template.format(**fmt_kwargs))
                except KeyError:
                    weakness_parts.append(template)

            # Determine code fixes needed
            if m.name == "journal_metadata":
                code_fixes.append("fix_journal_metadata")
            if m.name == "markdown_leakage":
                code_fixes.append("strip_markdown")
            if m.name == "citation_format_consistency":
                code_fixes.append("normalize_citations")
            if m.name == "paraphrase_repetition":
                code_fixes.append("lower_dedup_threshold")

        weakness_summary = "\n\n".join(weakness_parts)
        priority = [m.name for m in failing]

        return OptimizationPlan(
            weakness_summary=weakness_summary,
            code_fixes=code_fixes,
            priority_metrics=priority,
        )


# ---------------------------------------------------------------------------
# Code fixes — deterministic post-processing applied to paper dict
# ---------------------------------------------------------------------------

def fix_journal_metadata(paper: dict) -> dict:
    """Replace placeholder journal names with None."""
    for ref in paper.get("references", []):
        j = (ref.get("journal") or "").strip().lower()
        if j in _JOURNAL_BLOCKLIST:
            del ref["journal"]
    return paper


def strip_markdown(paper: dict) -> dict:
    """Remove markdown formatting from section content."""
    for section in paper.get("sections", []):
        content = section["content"]
        # Remove **bold**
        content = re.sub(r"\*\*([^*]+)\*\*", r"\1", content)
        # Remove *italic*
        content = re.sub(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)", r"\1", content)
        # Remove ### headers (keep text)
        content = re.sub(r"^#{1,6}\s+(.+)$", r"\1", content, flags=re.MULTILINE)
        # Remove inline `code`
        content = re.sub(r"`([^`]+)`", r"\1", content)
        # Remove bullet prefixes
        content = re.sub(r"^[-*+]\s+", "", content, flags=re.MULTILINE)
        # Remove blockquote prefixes
        content = re.sub(r"^>\s+", "", content, flags=re.MULTILINE)
        section["content"] = content
    return paper


def normalize_citations(paper: dict) -> dict:
    """Normalize citation format based on author count in references."""
    refs = paper.get("references", [])

    # Build lookup: surname -> author count
    surname_author_count: dict[str, int] = {}
    surname_second_author: dict[str, str] = {}
    for ref in refs:
        authors = ref.get("authors", [])
        if not authors:
            continue
        first = authors[0] if isinstance(authors[0], str) else authors[0].get("name", "")
        surname = first.split()[-1] if first else ""
        if surname:
            surname_author_count[surname] = len(authors)
            if len(authors) == 2:
                second = authors[1] if isinstance(authors[1], str) else authors[1].get("name", "")
                surname_second_author[surname] = second.split()[-1] if second else ""

    # Fix citations in text
    def _fix_cite(match: re.Match) -> str:
        cite = match.group(1)
        parts = cite.split(",")
        author_part = parts[0].strip()
        year_part = ",".join(parts[1:]).strip()

        # Extract first surname
        surname = author_part.split()[0].rstrip("&")

        count = surname_author_count.get(surname, 0)
        if count == 1:
            return f"[{surname}, {year_part}]"
        elif count == 2:
            second = surname_second_author.get(surname, "")
            if second:
                return f"[{surname} & {second}, {year_part}]"
        elif count >= 3:
            return f"[{surname} et al., {year_part}]"

        return match.group(0)  # no change if unknown

    cite_re = re.compile(r"\[([A-Z][a-zA-Z\-']+(?:\s+(?:et\s+al\.?|&\s*[A-Z][a-zA-Z\-']+))?,\s*\d{4}[a-z]?)\]")

    for section in paper.get("sections", []):
        section["content"] = cite_re.sub(_fix_cite, section["content"])
    if paper.get("abstract"):
        paper["abstract"] = cite_re.sub(_fix_cite, paper["abstract"])

    return paper


_CODE_FIXES = {
    "fix_journal_metadata": fix_journal_metadata,
    "strip_markdown": strip_markdown,
    "normalize_citations": normalize_citations,
    # "lower_dedup_threshold" is handled by passing threshold to PlaybookResearcher
}


def apply_code_fixes(paper: dict, fixes: list[str]) -> dict:
    """Apply deterministic code fixes to a paper dict."""
    for fix_name in fixes:
        fn = _CODE_FIXES.get(fix_name)
        if fn:
            paper = fn(paper)
            logger.info("Applied code fix: %s", fix_name)
    return paper


# ---------------------------------------------------------------------------
# AutoResearchLoop — the iteration orchestrator
# ---------------------------------------------------------------------------

class AutoResearchLoop:
    """Generate → Evaluate → Improve loop for paper quality.

    H1+H2 run once. Only H3+H4 are re-run on subsequent iterations.
    """

    def __init__(
        self,
        client,
        extractor,
        synthesizer,
        config=None,
        max_iterations: int = 3,
        pass_threshold: float = 75.0,
        display=None,
        custom_sources=None,
        owner_email: str = "",
        serper_api_key: str | None = None,
    ):
        self.client = client
        self.extractor = extractor
        self.synthesizer = synthesizer
        self.config = config
        self.max_iterations = max_iterations
        self.display = display
        self.custom_sources = custom_sources
        self.owner_email = owner_email
        self.serper_api_key = serper_api_key

        self.evaluator = PaperEvaluator(pass_threshold=pass_threshold)
        self.optimizer = PromptOptimizer()
        self.history: list[EvaluationReport] = []

    def run(self, topic: str, challenge_id: str | None = None) -> dict:
        """Run the generate-evaluate-improve loop.

        Returns the submission result dict.
        """
        from .playbook_researcher import PlaybookResearcher
        from .display import NullDisplay

        logger.info("AutoResearch loop: max %d iterations, pass threshold %.0f",
                     self.max_iterations, self.evaluator.pass_threshold)

        weakness_summary = ""
        result = None

        for iteration in range(1, self.max_iterations + 1):
            logger.info("=== AutoResearch iteration %d/%d ===", iteration, self.max_iterations)

            researcher = PlaybookResearcher(
                client=self.client,
                llm=self.synthesizer,
                config=self.config,
                display=self.display or NullDisplay(),
                custom_sources=self.custom_sources,
                owner_email=self.owner_email,
                serper_api_key=self.serper_api_key,
            )

            # Wire streaming callbacks
            if self.display and hasattr(self.display, "stream_token"):
                self.synthesizer.on_token = self.display.stream_token
            if self.display and hasattr(self.display, "update_tokens"):
                self.synthesizer.on_usage = self.display.update_tokens

            # On iteration 1: full pipeline. On subsequent: resume from H2 checkpoint
            # (skip H1+H2, only re-run H3+H4 with improved prompts)
            if iteration == 1:
                result = researcher.research_and_publish(
                    topic,
                    challenge_id=challenge_id,
                    resume=True,
                    weakness_summary=weakness_summary,
                )
            else:
                # Force checkpoint to phase 2 so H3+H4 re-run
                checkpoint = PlaybookResearcher.load_checkpoint(topic)
                if checkpoint and checkpoint.get("completed_phase", 0) >= 2:
                    # Reset to phase 2 — keep H1+H2 artifacts, redo H3+H4
                    checkpoint["completed_phase"] = 2
                    # Clear previous draft artifacts
                    arts = checkpoint.get("artifacts", {})
                    arts.pop("zero_draft", None)
                    arts.pop("final_paper", None)
                    arts.pop("verified_references", None)
                    arts.pop("abstract", None)
                    arts.pop("_mega_context_cache", None)
                    arts["weakness_summary"] = weakness_summary

                    # Save modified checkpoint
                    cp_path = PlaybookResearcher._checkpoint_path(topic)
                    cp_path.write_text(json.dumps(checkpoint, default=str, indent=2), encoding="utf-8")
                    logger.info("Reset checkpoint to phase H2 for re-synthesis")

                result = researcher.research_and_publish(
                    topic,
                    challenge_id=challenge_id,
                    resume=True,
                    weakness_summary=weakness_summary,
                )

            # Load the saved paper for evaluation
            paper = self._load_latest_paper(researcher, topic)
            if not paper:
                logger.warning("Could not load paper for evaluation — skipping quality loop")
                break

            # Evaluate
            report = self.evaluator.evaluate(paper, iteration=iteration)
            self.history.append(report)
            logger.info("\n%s", report.summary())

            if report.passing:
                logger.info("Paper PASSED quality threshold (%.1f >= %.1f)",
                           report.composite_score, self.evaluator.pass_threshold)
                break

            # Check for convergence (no improvement)
            if len(self.history) >= 2:
                improvement = report.composite_score - self.history[-2].composite_score
                if improvement < 2.0:
                    logger.info("Quality plateau (improvement %.1f < 2.0) — stopping", improvement)
                    break

                # Check for regression
                if improvement < -5.0:
                    logger.warning("Quality regression (%.1f) — stopping", improvement)
                    break

            if iteration < self.max_iterations:
                # Plan improvements
                plan = self.optimizer.plan(report)
                weakness_summary = plan.weakness_summary
                logger.info("Optimization plan: %d prompt fixes, %d code fixes",
                           len(plan.priority_metrics), len(plan.code_fixes))

                # Apply code fixes to the saved paper immediately
                if plan.code_fixes:
                    paper = apply_code_fixes(paper, plan.code_fixes)
                    # Re-evaluate after code fixes
                    post_fix_report = self.evaluator.evaluate(paper, iteration=iteration)
                    logger.info("After code fixes: %.1f (was %.1f)",
                               post_fix_report.composite_score, report.composite_score)

                    if post_fix_report.passing:
                        # Code fixes alone were enough — re-save and submit
                        logger.info("Code fixes resolved quality issues — no re-synthesis needed")
                        self._save_fixed_paper(paper, topic)
                        # Re-submit if the original submission succeeded
                        if result and result.get("paper_id"):
                            # Already submitted — just note it
                            logger.info("Paper already submitted as %s", result.get("paper_id"))
                        break

        return result or {"status": "failed", "error": "AutoResearch loop exhausted"}

    def _load_latest_paper(self, researcher, topic: str) -> dict | None:
        """Load the most recently saved paper JSON."""
        import pathlib
        save_dir = pathlib.Path.home() / ".agentpub" / "papers"
        if not save_dir.exists():
            return None

        # Try to find by title from artifacts
        brief = researcher.artifacts.get("research_brief", {})
        title = brief.get("title", topic)
        safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:50]
        paper_path = save_dir / f"{safe_title}.json"

        if paper_path.exists():
            try:
                return json.loads(paper_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # Fallback: most recently modified .json in papers dir
        papers = sorted(save_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if papers:
            try:
                return json.loads(papers[0].read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return None

    def _save_fixed_paper(self, paper: dict, topic: str) -> None:
        """Save a code-fixed paper back to disk."""
        import pathlib
        save_dir = pathlib.Path.home() / ".agentpub" / "papers"
        save_dir.mkdir(parents=True, exist_ok=True)
        title = paper.get("title", topic)
        safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:50]
        path = save_dir / f"{safe_title}.json"
        path.write_text(json.dumps(paper, default=str, indent=2), encoding="utf-8")
        logger.info("Saved code-fixed paper: %s", path)
