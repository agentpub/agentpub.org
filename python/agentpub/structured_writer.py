"""Schema-constrained paragraph writer.

The legacy paragraph writer (``_write_paragraph`` in ``playbook_researcher``)
asks the LLM for free-form prose with citation markers. The LLM frequently:
- invents cite keys not in the corpus
- attributes claims to sources that don't support them
- pads paragraphs with low-overlap citations to hit a word target
- concentrates citations on whichever source it likes best

This module replaces that with a structured-output call:

1. A JSON schema is built per paragraph that enumerates the allowed cite
   keys. Gemini's ``response_schema`` makes it structurally impossible for
   the model to emit a cite key outside the enum.
2. Each evidence point must include a ``supporting_quote`` — a verbatim
   ≤30-word snippet from the cited source's abstract/content. After the
   model responds, we verify the quote actually appears in the source. If
   it doesn't, the evidence point is dropped (or the paragraph retried).
3. A deterministic stitcher converts validated structured points into
   prose. No second LLM call can introduce new claims.

This pushes quality to generation time instead of relying on post-write
edit passes to repair fabricated content.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def build_paragraph_schema(allowed_cite_keys: list[str], *, min_points: int = 1, max_points: int = 4) -> dict:
    """Build a JSON-schema-style dict for one paragraph's structured output.

    ``allowed_cite_keys`` becomes the enum for every ``cite_key`` field, so
    the model literally cannot emit a key outside the corpus.
    """
    # Sanitize the enum: enums must be non-empty unique strings. If the
    # caller passed an empty list, we'd block any output, so synthesize a
    # placeholder that the validator drops downstream.
    cleaned = []
    seen = set()
    for k in allowed_cite_keys or []:
        if not k:
            continue
        s = str(k).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        cleaned.append(s)
    if not cleaned:
        cleaned = ["__NO_REFERENCES_AVAILABLE__"]

    return {
        "type": "object",
        "properties": {
            "topic_sentence": {
                "type": "string",
                "description": (
                    "Lead sentence stating the paragraph's main point. "
                    "Must NOT contain bracketed citations — those belong on evidence_points."
                ),
            },
            "evidence_points": {
                "type": "array",
                "minItems": min_points,
                "maxItems": max_points,
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {
                            "type": "string",
                            "description": "Specific factual claim being made (one sentence).",
                        },
                        "cite_key": {
                            "type": "string",
                            "enum": cleaned,
                            "description": "Exactly one of the allowed cite keys.",
                        },
                        "supporting_quote": {
                            "type": "string",
                            "description": (
                                "VERBATIM phrase (≤30 words) copied from the cited source's "
                                "supplied content that proves the claim. If you cannot find "
                                "such a phrase, OMIT this evidence_point — do NOT fabricate."
                            ),
                        },
                        "interpretation": {
                            "type": "string",
                            "description": (
                                "One sentence connecting the quote to the claim — your "
                                "synthesis, not a verbatim copy from the source."
                            ),
                        },
                    },
                    "required": ["claim", "cite_key", "supporting_quote", "interpretation"],
                },
            },
            "synthesis_sentence": {
                "type": "string",
                "description": (
                    "Closing sentence integrating the evidence points. May not introduce "
                    "claims that aren't grounded in evidence_points."
                ),
            },
        },
        "required": ["topic_sentence", "evidence_points", "synthesis_sentence"],
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _normalize_for_match(s: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation that varies between
    rendering passes ("smart" quotes, em dashes, non-breaking spaces).

    Used for fuzzy substring containment when checking whether the LLM's
    ``supporting_quote`` actually appears in the source content.
    """
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\u2014", "-").replace("\u2013", "-")
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip()


@dataclass
class ValidationResult:
    valid_points: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)  # {"reason": str, "point": dict}

    @property
    def kept_count(self) -> int:
        return len(self.valid_points)


def validate_structured_paragraph(
    payload: dict,
    *,
    allowed_cite_keys: Iterable[str],
    source_text_by_key: dict[str, str],
    min_quote_words: int = 3,
    max_quote_words: int = 50,
) -> ValidationResult:
    """Verify each evidence_point's supporting_quote actually appears in the
    cited source. Drop points that fail; keep the rest.

    Returns a ValidationResult with the kept points and reasons for any
    dropped points (useful to feed back into a retry).
    """
    result = ValidationResult()
    allowed = set(allowed_cite_keys or [])

    points = payload.get("evidence_points") or []
    if not isinstance(points, list):
        return result

    for p in points:
        if not isinstance(p, dict):
            result.dropped.append({"reason": "not_an_object", "point": p})
            continue

        cite = (p.get("cite_key") or "").strip()
        quote = (p.get("supporting_quote") or "").strip()
        claim = (p.get("claim") or "").strip()
        interp = (p.get("interpretation") or "").strip()

        if not cite or cite not in allowed:
            result.dropped.append({"reason": f"cite_key not in allowed set: {cite!r}", "point": p})
            continue
        if not claim:
            result.dropped.append({"reason": "empty claim", "point": p})
            continue
        if not quote:
            result.dropped.append({"reason": "empty supporting_quote", "point": p})
            continue

        # Word-count guardrails on the quote.
        qwords = quote.split()
        if len(qwords) < min_quote_words:
            result.dropped.append({"reason": f"quote too short ({len(qwords)} words)", "point": p})
            continue
        if len(qwords) > max_quote_words:
            # Trim and proceed — most LLMs honor ≤30 but occasionally overshoot.
            quote = " ".join(qwords[:max_quote_words])
            p = {**p, "supporting_quote": quote}

        source_text = source_text_by_key.get(cite, "")
        if not source_text:
            result.dropped.append({"reason": f"no source text for cite_key {cite!r}", "point": p})
            continue

        if _normalize_for_match(quote) not in _normalize_for_match(source_text):
            result.dropped.append({
                "reason": "supporting_quote not found in source content (likely fabricated)",
                "point": p,
            })
            continue

        # All checks passed.
        result.valid_points.append({
            "claim": claim,
            "cite_key": cite,
            "supporting_quote": quote,
            "interpretation": interp,
        })

    return result


# ---------------------------------------------------------------------------
# Deterministic prose stitching
# ---------------------------------------------------------------------------

def stitch_paragraph(payload: dict, valid_points: list[dict]) -> str:
    """Combine the topic sentence, validated evidence points, and synthesis
    sentence into a single paragraph string.

    No new content is introduced. Cite keys are emitted as [Author, Year].
    """
    pieces: list[str] = []

    topic = (payload.get("topic_sentence") or "").strip()
    if topic:
        # Strip any stray brackets the model may have inserted.
        topic = re.sub(r"\s*\[[^\]]+\]\s*", " ", topic).strip()
        if topic and not topic.endswith((".", "!", "?")):
            topic += "."
        pieces.append(topic)

    for pt in valid_points:
        claim = pt["claim"].strip()
        cite = pt["cite_key"].strip()
        interp = pt.get("interpretation", "").strip()
        # Strip any cite markers the model may have inlined in the claim
        # text — we render the canonical cite ourselves to avoid duplication.
        claim = re.sub(r"\s*\[[^\]]+\]\s*", " ", claim).strip()
        if claim.endswith((".", "!", "?")):
            claim = claim[:-1]
        sentence = f"{claim} [{cite}]."
        pieces.append(sentence)
        if interp:
            interp = re.sub(r"\s*\[[^\]]+\]\s*", " ", interp).strip()
            if interp and not interp.endswith((".", "!", "?")):
                interp += "."
            pieces.append(interp)

    synth = (payload.get("synthesis_sentence") or "").strip()
    if synth:
        synth = re.sub(r"\s*\[[^\]]+\]\s*", " ", synth).strip()
        if synth and not synth.endswith((".", "!", "?")):
            synth += "."
        pieces.append(synth)

    return " ".join(p for p in pieces if p)
