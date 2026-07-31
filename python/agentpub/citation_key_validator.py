"""Citation key validator — catches malformed and orphaned citations.

This module runs after the writer finishes and before the citation justification
audit. It catches three classes of citation defect that the existing
`[Author, \\d{4}]` audit regex skips:

1. **Malformed citation keys** — `[Author, None]`, `[Author, ]`, `[None, 2024]`,
   citations with no year, citations with non-numeric year, etc. These slip
   past the audit because the regex requires `\\d{4}`.
2. **Orphan in-text citations** — `[Author, Year]` patterns that have no
   matching entry in the reference list. The writer sometimes invents citation
   keys mid-draft for sources it remembers but never added to the ref list.
3. **Unused references** — entries in the reference list that aren't cited
   anywhere. Less critical (just noise) but useful to know.

The validator is intentionally separate from the LLM-based citation
justification audit. This one is deterministic, regex-driven, zero-cost, and
catches a different class of bug.

Usage:

    from agentpub.citation_key_validator import validate_citations

    report = validate_citations(sections, references)
    if report.has_blocking_issues():
        # Either re-write the offending citations or block submission.
        ...
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable


# Match anything that looks like an in-text citation: `[...]` containing a
# comma. This is broader than the audit regex on purpose — we want to FIND
# malformed citations, not skip them.
_CITE_PATTERN = re.compile(r"\[([^\[\]]{1,200}?)\]")

# A "well-formed" citation has a non-empty author and a 4-digit year.
_WELLFORMED_PATTERN = re.compile(r"^\s*(.+?)\s*,\s*(\d{4})(?:[a-z])?\s*$")


def _normalize_author(s: str) -> str:
    """Lowercased + diacritics stripped, for fuzzy matching against ref list."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.strip().lower())


def _first_surname(authors_field) -> str | None:
    """Best-effort first-author surname from a ref's `authors` field.

    `authors` can be a list of strings (preferred), a single string, or None.
    Each entry can be "Smith, John" or "John Smith" or "Smith J.". We just need
    a stable surname for matching.
    """
    if not authors_field:
        return None
    if isinstance(authors_field, str):
        authors_field = [authors_field]
    if not authors_field:
        return None
    first = str(authors_field[0]).strip()
    if not first:
        return None
    # "Smith, John" → "Smith"
    if "," in first:
        return _normalize_author(first.split(",", 1)[0])
    # "John A. Smith" → "Smith" (last token)
    tokens = first.split()
    return _normalize_author(tokens[-1]) if tokens else None


@dataclass
class CitationIssue:
    section: str
    citation: str
    sentence: str
    kind: str  # 'malformed' | 'orphan'
    reason: str

    def snapshot(self) -> dict:
        return {
            "section": self.section,
            "citation": self.citation,
            "sentence": (self.sentence or "")[:300],
            "kind": self.kind,
            "reason": self.reason,
        }


@dataclass
class CitationKeyReport:
    malformed: list[CitationIssue] = field(default_factory=list)
    orphan: list[CitationIssue] = field(default_factory=list)
    unused_refs: list[str] = field(default_factory=list)
    total_citations: int = 0
    wellformed_citations: int = 0

    def has_blocking_issues(self) -> bool:
        """True if any malformed or orphan citation is present.

        These are deterministic defects with no LLM-judgement involved. They
        should never ship — the writer either uses a valid `[Author, Year]`
        pair that resolves to a ref-list entry, or it doesn't cite at all.
        """
        return bool(self.malformed) or bool(self.orphan)

    def summary(self) -> dict:
        return {
            "total_citations": self.total_citations,
            "wellformed_citations": self.wellformed_citations,
            "malformed_count": len(self.malformed),
            "orphan_count": len(self.orphan),
            "unused_ref_count": len(self.unused_refs),
            "malformed": [i.snapshot() for i in self.malformed],
            "orphan": [i.snapshot() for i in self.orphan],
            "unused_refs": list(self.unused_refs),
        }


def validate_citations(
    sections: dict[str, str],
    references: Iterable[dict],
) -> CitationKeyReport:
    """Scan section text and reference list, return a defect report.

    Args:
        sections: mapping of section heading → markdown/text content.
        references: iterable of reference dicts. Each ref is expected to have
            `authors` (list[str]) and `year` (int|str|None).

    Returns:
        A `CitationKeyReport`. Use `.has_blocking_issues()` to decide whether
        to block submission, and `.summary()` to serialize into artifacts.
    """
    # Build an index of valid (surname, year) tuples from the ref list.
    valid_keys: set[tuple[str, str]] = set()
    seen_refs: set[tuple[str, str]] = set()
    ref_index: dict[tuple[str, str], int] = {}
    refs_list = list(references or [])
    for idx, ref in enumerate(refs_list):
        if not isinstance(ref, dict):
            continue
        surname = _first_surname(ref.get("authors"))
        year = ref.get("year")
        if surname is None or year is None or year == "":
            continue
        year_str = str(year).strip()
        if not re.fullmatch(r"\d{4}", year_str):
            continue
        key = (surname, year_str)
        valid_keys.add(key)
        ref_index[key] = idx

    cited_keys: set[tuple[str, str]] = set()
    report = CitationKeyReport()

    for section_name, text in (sections or {}).items():
        if not text:
            continue
        # Split into sentences for better surrounding-context messages.
        sentences = re.split(r"(?<=[.!?])\s+", text)
        for sentence in sentences:
            for match in _CITE_PATTERN.finditer(sentence):
                raw_bracket = match.group(1).strip()
                # Multi-citation: "[Jensen, 1976; Eisenhardt, 1989]" — split on
                # `;` and validate each sub-citation independently.
                parts = [p.strip() for p in raw_bracket.split(";") if p.strip()]
                for raw in parts:
                    # Skip obvious non-citations: pure numbers ([1], [12-15],
                    # [3,5]), URLs, footnote markers, single tokens.
                    if "," not in raw:
                        continue
                    if all(p.strip().isdigit() for p in raw.split(",")):
                        continue
                    # Skip ref-id citations like `[ref-11, ref-2]` or
                    # `[ref_5]` — those are a different (also valid) writer
                    # citation format that maps to ref-list IDs, not
                    # `[Author, Year]` pairs. Validating them is a separate
                    # concern.
                    if re.match(r"^\s*ref[-_]?\d+", raw, re.IGNORECASE):
                        continue
                    report.total_citations += 1
                    wf = _WELLFORMED_PATTERN.match(raw)
                    if not wf:
                        # Only flag as MALFORMED when this looked like an
                        # attempted author-year citation: starts with an
                        # uppercase letter (a surname). Lowercase/short
                        # tokens are probably non-citations the regex caught
                        # incidentally — skip them rather than block.
                        if not re.match(r"^\s*[A-Z]", raw):
                            report.total_citations -= 1  # uncount
                            continue
                        report.malformed.append(CitationIssue(
                            section=section_name,
                            citation=raw,
                            sentence=sentence,
                            kind="malformed",
                            reason="Citation does not match `[Author, YYYY]` pattern "
                                   "— year is missing, null, or non-numeric.",
                        ))
                        continue
                    report.wellformed_citations += 1
                    author_part, year_part = wf.group(1), wf.group(2)
                    first_token = re.split(r"\s+(?:et al\.?|and|&|,)\s*", author_part, maxsplit=1)[0]
                    norm_author = _normalize_author(first_token)
                    key = (norm_author, year_part)
                    if key not in valid_keys:
                        report.orphan.append(CitationIssue(
                            section=section_name,
                            citation=raw,
                            sentence=sentence,
                            kind="orphan",
                            reason=f"No reference list entry for ({norm_author}, {year_part}).",
                        ))
                    else:
                        cited_keys.add(key)
                        seen_refs.add(key)

    # Unused refs: present in ref list, never cited.
    for key in valid_keys - seen_refs:
        idx = ref_index.get(key, -1)
        title = ""
        if 0 <= idx < len(refs_list):
            title = str(refs_list[idx].get("title", ""))[:120]
        report.unused_refs.append(f"({key[0]}, {key[1]}) — {title}")

    return report
