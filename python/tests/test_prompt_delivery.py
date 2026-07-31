"""Editing a prompt is not the same as delivering it.

load_prompts() resolves three sources:

    local overrides (~/.agentpub/prompts/*.txt)
      > remote  /v1/prompts/research
        > DEFAULT_PROMPTS in this file

and the remote copy wins whenever `remote_version >= PROMPT_VERSION`. So editing
DEFAULT_PROMPTS without bumping PROMPT_VERSION changes nothing at run time: the
server keeps serving the old text under the same version number and silently
overwrites the edit.

That is precisely what happened with peer_review. The prompt was corrected, the
contract test passed, the local render looked right — and reviews kept scoring
5/10 on every dimension, because the corrected prompt was never the one used.
The API serves from api/_sdk_prompts.py, a copy taken at DEPLOY time, so a fix
also needs a deploy before any other agent sees it.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentpub.prompts import DEFAULT_PROMPTS, PROMPT_VERSION  # noqa: E402


def _parts(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def test_prompt_version_is_ahead_of_the_broken_release():
    """3.2.0 is the version whose peer_review produced no parsable scores.

    Local must be strictly greater, or the server's 3.2.0 copy wins the
    comparison and replaces the corrected prompt.
    """
    assert _parts(PROMPT_VERSION) > (3, 2, 0), (
        f"PROMPT_VERSION is {PROMPT_VERSION}; the remote serves 3.2.0 with the "
        f"broken peer_review prompt, and remote wins on >=. Bump it."
    )


def test_version_is_well_formed():
    assert re.fullmatch(r"\d+\.\d+\.\d+", PROMPT_VERSION), PROMPT_VERSION


def test_the_version_note_explains_the_precedence_trap():
    """The next person to edit a prompt must see why bumping matters."""
    src = (Path(__file__).resolve().parents[1] / "agentpub" / "prompts.py").read_text(encoding="utf-8")
    head = src[: src.index("PROMPT_VERSION")]
    assert "BUMP THIS" in src[: src.index("PROMPT_VERSION") + 200], (
        "the precedence trap is undocumented at the point of edit"
    )
    assert "remote" in head.lower() or "remote" in src[: src.index("PROMPT_VERSION") + 600].lower()


def test_peer_review_default_is_the_corrected_one():
    """Belt and braces with test_review_prompt_contract: the shipped default."""
    pr = DEFAULT_PROMPTS["peer_review"]
    assert "citation_quality" in pr
    assert "Originality: Does it offer" not in pr, "the 3.2.0 prose prompt is back"
