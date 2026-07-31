"""Corpus-consistency validator — catches contradictory paper-count narratives.

The writer LLM is free to mention numeric corpus stats in any section (abstract,
methodology, results). When it freelances those numbers per-section, they drift:
the abstract says "15 papers", the methodology says "12 papers passed initial
screening + 3 added = 12" (which is also arithmetically wrong), and the reader
loses trust.

This validator extracts every `\\d+ (papers|studies|articles|references|sources)`
pattern from the final draft, builds the canonical corpus-stat set from
`search_audit` + the reference list, and flags any mentioned number that isn't
in the canonical set. The intent is "warn loudly, block on egregious drift" —
not to police every legitimate sub-count (e.g. "5 papers focused on governance"
is a perfectly fine sub-corpus mention).

Heuristic, not exact. The signal: when the same noun (e.g. "papers in this
review") gets two different numbers across two sections, that's the bug the
reviewer flagged.

Usage:

    report = validate_corpus_consistency(sections, search_audit, references)
    if report.has_blocking_issues():
        ...
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


# Numeric corpus-count phrases the writer typically uses. Limit to the nouns
# that imply "this is a corpus stat" — avoid matching figures, table rows,
# years, percentages, etc.
_CORPUS_NOUNS = (
    "papers",
    "studies",
    "articles",
    "references",
    "sources",
    "publications",
    "works",
)

_NOUN_ALTERNATION = r"(?:" + "|".join(_CORPUS_NOUNS) + r")"

# Match "N papers", "N (15) papers", "N peer-reviewed papers", etc. Allow a
# small modifier window between the number and the noun (e.g. "15
# peer-reviewed papers"). Cap the number at 4 digits to avoid catching years.
_COUNT_PATTERN = re.compile(
    rf"\b(\d{{1,4}})\s+(?:[A-Za-z\-]+\s+){{0,3}}({_NOUN_ALTERNATION})\b",
    re.IGNORECASE,
)


@dataclass
class CorpusCountMention:
    section: str
    sentence: str
    count: int
    noun: str

    def snapshot(self) -> dict:
        return {
            "section": self.section,
            "sentence": (self.sentence or "")[:280],
            "count": self.count,
            "noun": self.noun,
        }


@dataclass
class CorpusConsistencyReport:
    canonical: dict[str, int] = field(default_factory=dict)
    mentions: list[CorpusCountMention] = field(default_factory=list)
    off_canonical: list[CorpusCountMention] = field(default_factory=list)
    cross_section_drift: list[dict] = field(default_factory=list)

    def has_blocking_issues(self) -> bool:
        """True if cross-section drift exists — i.e. two sections claim
        different `N <noun>` for the same noun. Off-canonical mentions are
        warnings only because legitimate sub-counts exist.
        """
        return bool(self.cross_section_drift)

    def summary(self) -> dict:
        return {
            "canonical": dict(self.canonical),
            "total_mentions": len(self.mentions),
            "off_canonical_count": len(self.off_canonical),
            "cross_section_drift_count": len(self.cross_section_drift),
            "off_canonical": [m.snapshot() for m in self.off_canonical[:20]],
            "cross_section_drift": list(self.cross_section_drift)[:20],
        }


def _build_canonical(search_audit: dict | None, references: Iterable) -> dict[str, int]:
    """Pull the legitimate corpus-stat values from search_audit + ref list."""
    sa = search_audit or {}
    canonical: dict[str, int] = {}
    for k in (
        "total_retrieved",
        "total_after_dedup",
        "total_after_filter",
        "total_included",
    ):
        v = sa.get(k)
        if isinstance(v, int) and v > 0:
            canonical[k] = v
    refs = list(references or [])
    if refs:
        canonical["total_references"] = len(refs)
    return canonical


def _is_legitimate_funnel_drift(
    counts_seen: set[int],
    canonical: dict[str, int],
) -> bool:
    """Return True when the drift is the writer narrating the search funnel.

    The search funnel naturally produces multiple distinct "papers" counts
    (retrieved → after_dedup → after_filter → included → references). When a
    methodology says "30 papers after filtering" and an introduction says "32
    papers in this review", both numbers are canonical — the writer isn't
    contradicting itself, it's describing different stages of the same funnel.

    Suppress drift only when:
    1. Every count seen is one of the canonical funnel stages, AND
    2. ``total_references`` is among them (so the final corpus is anchored
       somewhere in the paper — without this, a paper that quotes only
       upstream funnel stages would slip through).
    """
    canonical_values = set(canonical.values())
    if not canonical_values:
        return False
    if not counts_seen.issubset(canonical_values):
        return False
    total_refs = canonical.get("total_references")
    return total_refs is not None and total_refs in counts_seen


def validate_corpus_consistency(
    sections: dict[str, str],
    search_audit: dict | None,
    references: Iterable,
) -> CorpusConsistencyReport:
    """Scan sections for numeric corpus mentions and flag inconsistencies."""
    report = CorpusConsistencyReport(canonical=_build_canonical(search_audit, references))
    canonical_values = set(report.canonical.values())

    # Track per-noun counts: noun → {section: set of counts seen}
    per_noun: dict[str, dict[str, set[int]]] = {}

    for section_name, text in (sections or {}).items():
        if not text:
            continue
        sentences = re.split(r"(?<=[.!?])\s+", text)
        for sentence in sentences:
            for m in _COUNT_PATTERN.finditer(sentence):
                try:
                    n = int(m.group(1))
                except ValueError:
                    continue
                noun = m.group(2).lower()
                # Filter out tiny incidental numbers ("3 articles" cited in a
                # specific subsection) and year-shaped numbers (1900-2099).
                if 1900 <= n <= 2099:
                    continue
                if n < 3:
                    continue
                mention = CorpusCountMention(section=section_name, sentence=sentence, count=n, noun=noun)
                report.mentions.append(mention)
                if canonical_values and n not in canonical_values:
                    report.off_canonical.append(mention)
                per_noun.setdefault(noun, {}).setdefault(section_name, set()).add(n)

    # Cross-section drift: same noun, multiple sections, different counts. We
    # only flag when the SAME noun appears with different values across
    # different sections — that's the writer contradicting itself, not a
    # legitimate sub-count.
    for noun, by_section in per_noun.items():
        all_counts: set[int] = set()
        for counts in by_section.values():
            all_counts.update(counts)
        if len(all_counts) <= 1:
            continue
        # Different counts across different sections → drift candidate.
        sections_with_distinct_values = sum(1 for c in by_section.values() if c)
        if sections_with_distinct_values < 2:
            continue
        # Suppress when the drift is the writer narrating legitimate search
        # funnel stages (retrieved → after_dedup → after_filter → included →
        # references) and the final corpus size is anchored somewhere.
        if _is_legitimate_funnel_drift(all_counts, report.canonical):
            continue
        report.cross_section_drift.append({
            "noun": noun,
            "counts_seen": sorted(all_counts),
            "by_section": {sec: sorted(cs) for sec, cs in by_section.items() if cs},
            "canonical_values_for_noun": sorted(canonical_values),
        })
    return report
