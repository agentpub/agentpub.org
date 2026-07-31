"""Unit tests for the schema-constrained paragraph writer.

These lock in the quality guarantees structured_writer is meant to enforce at
generation time:
- the cite_key enum is restricted to the corpus (model can't invent keys)
- evidence points whose supporting_quote isn't verbatim in the source are
  dropped as likely fabrications
- the deterministic stitcher introduces no new claims and renders one canonical
  [cite] per point
"""
from __future__ import annotations

from agentpub.structured_writer import (
    build_paragraph_schema,
    stitch_paragraph,
    validate_structured_paragraph,
)


# ---------------------------------------------------------------------------
# build_paragraph_schema
# ---------------------------------------------------------------------------

def test_schema_enum_is_the_corpus():
    schema = build_paragraph_schema(["Smith2020", "Jones2019"], min_points=2, max_points=5)
    item = schema["properties"]["evidence_points"]["items"]
    assert item["properties"]["cite_key"]["enum"] == ["Smith2020", "Jones2019"]
    assert schema["properties"]["evidence_points"]["minItems"] == 2
    assert schema["properties"]["evidence_points"]["maxItems"] == 5
    assert item["required"] == ["claim", "cite_key", "supporting_quote", "interpretation"]


def test_schema_dedupes_and_drops_blanks():
    schema = build_paragraph_schema(["A", "A", "", "  ", "B", None])  # type: ignore[list-item]
    assert schema["properties"]["evidence_points"]["items"]["properties"]["cite_key"]["enum"] == ["A", "B"]


def test_schema_empty_corpus_gets_placeholder():
    # An empty enum would be invalid / block all output; a placeholder keeps the
    # schema valid and is dropped by the validator downstream.
    schema = build_paragraph_schema([])
    assert schema["properties"]["evidence_points"]["items"]["properties"]["cite_key"]["enum"] == [
        "__NO_REFERENCES_AVAILABLE__"
    ]


# ---------------------------------------------------------------------------
# validate_structured_paragraph
# ---------------------------------------------------------------------------

SOURCE = {
    "Smith2020": "Cognitive load increases error rates in complex multitasking environments.",
    "Jones2019": "Sleep deprivation impairs working memory consolidation over time.",
}


def _point(cite, quote, claim="A claim is made here.", interp="It connects."):
    return {"cite_key": cite, "supporting_quote": quote, "claim": claim, "interpretation": interp}


def test_valid_point_is_kept():
    payload = {"evidence_points": [_point("Smith2020", "increases error rates in complex multitasking")]}
    res = validate_structured_paragraph(
        payload, allowed_cite_keys=SOURCE.keys(), source_text_by_key=SOURCE
    )
    assert res.kept_count == 1
    assert res.valid_points[0]["cite_key"] == "Smith2020"
    assert not res.dropped


def test_out_of_corpus_cite_key_is_dropped():
    payload = {"evidence_points": [_point("Ghost2099", "increases error rates in complex multitasking")]}
    res = validate_structured_paragraph(
        payload, allowed_cite_keys=SOURCE.keys(), source_text_by_key=SOURCE
    )
    assert res.kept_count == 0
    assert "not in allowed set" in res.dropped[0]["reason"]


def test_fabricated_quote_is_dropped():
    # Quote does not appear anywhere in the cited source.
    payload = {"evidence_points": [_point("Smith2020", "quantum entanglement observed in neurons directly")]}
    res = validate_structured_paragraph(
        payload, allowed_cite_keys=SOURCE.keys(), source_text_by_key=SOURCE
    )
    assert res.kept_count == 0
    assert "not found in source" in res.dropped[0]["reason"]


def test_quote_too_short_is_dropped():
    payload = {"evidence_points": [_point("Smith2020", "error rates")]}  # 2 words < min 3
    res = validate_structured_paragraph(
        payload, allowed_cite_keys=SOURCE.keys(), source_text_by_key=SOURCE
    )
    assert res.kept_count == 0
    assert "too short" in res.dropped[0]["reason"]


def test_empty_quote_and_empty_claim_dropped():
    payload = {"evidence_points": [
        _point("Smith2020", ""),
        _point("Jones2019", "impairs working memory consolidation over time", claim=""),
    ]}
    res = validate_structured_paragraph(
        payload, allowed_cite_keys=SOURCE.keys(), source_text_by_key=SOURCE
    )
    assert res.kept_count == 0
    reasons = " ".join(d["reason"] for d in res.dropped)
    assert "empty supporting_quote" in reasons
    assert "empty claim" in reasons


def test_quote_matching_is_punctuation_insensitive():
    # An em dash in the quote should fold to an ASCII hyphen so it still matches
    # a plain-ASCII source. chr(0x2014) keeps this test file pure ASCII while
    # producing a real em dash at runtime.
    em_dash = chr(0x2014)
    src = {"Lee2021": "A so-called deep-reading approach improves retention markedly."}
    quote = f"deep{em_dash}reading approach improves"
    payload = {"evidence_points": [_point("Lee2021", quote)]}
    res = validate_structured_paragraph(
        payload, allowed_cite_keys=src.keys(), source_text_by_key=src
    )
    assert res.kept_count == 1


# ---------------------------------------------------------------------------
# stitch_paragraph
# ---------------------------------------------------------------------------

def test_stitch_renders_canonical_cites_and_strips_strays():
    payload = {
        "topic_sentence": "Working memory is finite [stray]",
        "synthesis_sentence": "Together these findings converge",
    }
    points = [{"claim": "Load raises errors [inline]", "cite_key": "Smith2020", "interpretation": "This matters"}]
    out = stitch_paragraph(payload, points)
    assert "[Smith2020]" in out
    assert "[stray]" not in out  # topic-sentence bracket removed
    assert "[inline]" not in out  # claim-inlined cite removed
    assert out.endswith("converge.")  # synthesis terminated with a period
    assert "Working memory is finite." in out
