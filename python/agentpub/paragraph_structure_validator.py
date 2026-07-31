"""Paragraph-structure validator — catches wall-of-text sections.

Run 1 of the AI/LTIPs test had an Introduction that was a single ~1,800-word
paragraph rehearsing the same "narrative literature review of N papers" boiler
plate seven times with minor wording changes. The reviewer correctly flagged
this as a quality issue: a research-paper section that's structurally one
paragraph is hard to read, signals the writer wasn't planning sub-topics, and
typically correlates with repetition.

This validator catches that pattern deterministically:

1. **Wall-of-text sections** — a section is one paragraph (no blank-line
   breaks) AND that paragraph is longer than a configurable threshold
   (default 500 words).
2. **Extreme single paragraphs** — any paragraph in any section longer than
   a hard ceiling (default 800 words), regardless of structure.

Both signals are reported. The block is configurable via
`config.paragraph_structure_required` (default True) and the thresholds via
`config.paragraph_structure_wall_threshold_words` and
`config.paragraph_structure_paragraph_max_words`.

The validator does NOT try to enforce minimum paragraph count or specific
section structures — those are too topic-dependent. It only flags the
egregious case of "one giant paragraph stands in for a multi-thesis section."

Usage:

    from agentpub.paragraph_structure_validator import validate_paragraph_structure

    report = validate_paragraph_structure(sections,
                                          wall_threshold_words=500,
                                          paragraph_max_words=800)
    if report.has_blocking_issues():
        ...
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")


@dataclass
class StructureIssue:
    section: str
    kind: str  # 'wall_of_text' | 'oversized_paragraph'
    paragraph_count: int
    longest_paragraph_words: int
    reason: str

    def snapshot(self) -> dict:
        return {
            "section": self.section,
            "kind": self.kind,
            "paragraph_count": self.paragraph_count,
            "longest_paragraph_words": self.longest_paragraph_words,
            "reason": self.reason,
        }


@dataclass
class ParagraphStructureReport:
    wall_of_text_sections: list[StructureIssue] = field(default_factory=list)
    oversized_paragraphs: list[StructureIssue] = field(default_factory=list)
    per_section_stats: dict[str, dict[str, int]] = field(default_factory=dict)

    def has_blocking_issues(self) -> bool:
        return bool(self.wall_of_text_sections) or bool(self.oversized_paragraphs)

    def summary(self) -> dict:
        return {
            "wall_of_text_count": len(self.wall_of_text_sections),
            "oversized_paragraph_count": len(self.oversized_paragraphs),
            "wall_of_text": [i.snapshot() for i in self.wall_of_text_sections],
            "oversized_paragraphs": [i.snapshot() for i in self.oversized_paragraphs],
            "per_section_stats": dict(self.per_section_stats),
        }


def validate_paragraph_structure(
    sections: dict[str, str],
    *,
    wall_threshold_words: int = 500,
    paragraph_max_words: int = 800,
) -> ParagraphStructureReport:
    """Scan each section for wall-of-text and oversized-paragraph defects.

    Args:
        sections: mapping of section heading → text.
        wall_threshold_words: if a section is one paragraph AND has more than
            this many words, flag as wall_of_text.
        paragraph_max_words: any single paragraph above this is flagged as
            oversized regardless of section structure.

    Returns:
        ParagraphStructureReport with issue lists and per-section stats.
    """
    report = ParagraphStructureReport()
    for section_name, text in (sections or {}).items():
        if not text:
            continue
        paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]
        para_count = len(paragraphs)
        word_counts = [len(p.split()) for p in paragraphs]
        longest = max(word_counts) if word_counts else 0
        total = sum(word_counts)

        report.per_section_stats[section_name] = {
            "paragraph_count": para_count,
            "total_words": total,
            "longest_paragraph_words": longest,
        }

        # Wall of text: single paragraph + over threshold
        if para_count == 1 and longest >= wall_threshold_words:
            report.wall_of_text_sections.append(StructureIssue(
                section=section_name,
                kind="wall_of_text",
                paragraph_count=para_count,
                longest_paragraph_words=longest,
                reason=(
                    f"Section is a single paragraph of {longest} words "
                    f"(threshold {wall_threshold_words}). Split into "
                    f"discrete sub-topics with paragraph breaks."
                ),
            ))

        # Oversized: any single paragraph over the hard ceiling
        for i, wc in enumerate(word_counts):
            if wc >= paragraph_max_words:
                report.oversized_paragraphs.append(StructureIssue(
                    section=section_name,
                    kind="oversized_paragraph",
                    paragraph_count=para_count,
                    longest_paragraph_words=wc,
                    reason=(
                        f"Paragraph {i + 1} in {section_name} is {wc} words "
                        f"(ceiling {paragraph_max_words}). Break at topic shifts."
                    ),
                ))
    return report
