"""Discussion comments must reach the provider that owns the chosen model.

``generate_discussion`` accepted any model name and then called Google
unconditionally. Choosing a GPT or Claude model in the UI therefore sent that
name to Gemini's API, which answered "404 model not found" — an error that reads
like a missing paper and has nothing to do with the paper at all.

The failure was invisible for a specific reason: the default model is a Gemini
one, so every default path worked. Only a deliberate change of model broke it,
and then blamed the wrong thing.
"""

from __future__ import annotations

import pytest

from agentpub.paper_discuss import _provider_caller_for
from agentpub.paper_evaluator import PROVIDER_CALLERS

_BY_FN = {fn: name for name, fn in PROVIDER_CALLERS.items()}


def _provider(model: str) -> str:
    return _BY_FN[_provider_caller_for(model)]


@pytest.mark.parametrize(
    "model,expected",
    [
        # OpenAI, including the reasoning-model families
        ("gpt-5-mini", "openai"),
        ("gpt-5.4", "openai"),
        ("GPT-5.4", "openai"),          # case must not matter
        ("o3-mini", "openai"),
        ("o4-mini", "openai"),
        ("chatgpt-4o-latest", "openai"),
        # Anthropic
        ("claude-opus-5", "anthropic"),
        ("claude-sonnet-5", "anthropic"),
        ("claude-haiku-4-5", "anthropic"),
        # Google, including local Gemma tags served through the same path
        ("gemini-2.5-flash", "google"),
        ("gemini-3-flash-preview", "google"),
        ("gemma4:e2b", "google"),
        # Mistral
        ("mistral-large-latest", "mistral"),
        ("ministral-8b", "mistral"),
    ],
)
def test_each_model_routes_to_its_own_provider(model, expected):
    assert _provider(model) == expected


def test_a_claude_model_never_goes_to_google():
    """The exact bug: this produced a 404 from Gemini's API."""
    assert _provider("claude-opus-5") != "google"


def test_a_gpt_model_never_goes_to_google():
    assert _provider("gpt-5-mini") != "google"


def test_an_unknown_model_falls_back_rather_than_raising():
    """A model the registry has just started advertising must still work.

    The model list is fetched at runtime precisely so new models appear without
    an SDK release; routing that raised on an unfamiliar name would undo that.
    """
    assert _provider("some-model-released-tomorrow") in PROVIDER_CALLERS


@pytest.mark.parametrize("bad", ["", None])
def test_empty_model_does_not_crash(bad):
    assert _provider_caller_for(bad) in PROVIDER_CALLERS.values()


# --------------------------------------------------------------------------
# The tests above check the router in isolation. That is not enough.
#
# When the real fix was reverted — putting `_call_google(model, prompt)` back —
# every test above still passed, because none of them exercised the call site.
# The router was correct and simply unused, which is precisely the shape of the
# original bug. These tests drive generate_discussion itself and assert which
# backend actually received the call.
# --------------------------------------------------------------------------

_PAPER = {
    "paper_id": "paper_2026_test01",
    "title": "A Test Paper",
    "abstract": "An abstract.",
    "author_agent_id": "agent_someone_else",
    "sections": [{"heading": "Introduction", "content": "Body text."}],
}

_REPLY = {
    "text": '{"comment": "A thoughtful remark about the paper.", "stance": "neutral"}',
    "cost_usd": 0.0,
    "input_tokens": 10,
    "output_tokens": 10,
}


@pytest.mark.parametrize(
    "model,expected_provider",
    [
        ("claude-opus-5", "anthropic"),
        ("gpt-5-mini", "openai"),
        ("gemini-2.5-flash", "google"),
        ("mistral-large-latest", "mistral"),
    ],
)
def test_generate_discussion_calls_the_right_backend(monkeypatch, model, expected_provider):
    """End-to-end: the chosen model must reach its own provider.

    Sending a Claude model name to Gemini's API is what produced the
    "404 model not found" that looked like a missing paper.
    """
    import agentpub.paper_evaluator as ev
    import agentpub.paper_discuss as pd

    called: list[str] = []

    def _spy(name):
        def _fn(model_arg, prompt, *a, **k):
            called.append(name)
            return dict(_REPLY)
        return _fn

    patched = {name: _spy(name) for name in PROVIDER_CALLERS}
    monkeypatch.setattr(ev, "PROVIDER_CALLERS", patched, raising=True)
    # The old code held a direct reference to _call_google, so patching the
    # table alone would not catch a regression — patch the name too.
    monkeypatch.setattr(pd, "_call_google", patched["google"], raising=False)

    pd.generate_discussion(paper=_PAPER, model=model, acting_agent_id="agent_me")

    assert called, "no provider was called at all"
    assert called[0] == expected_provider, (
        f"model {model!r} was sent to {called[0]!r}, not {expected_provider!r} — "
        f"this is the bug that returned '404 model not found'"
    )


def test_a_claude_model_does_not_reach_google_end_to_end(monkeypatch):
    """The reported failure, pinned at the call site rather than the router."""
    import agentpub.paper_evaluator as ev
    import agentpub.paper_discuss as pd

    called: list[str] = []
    patched = {
        name: (lambda n: lambda m, p, *a, **k: (called.append(n), dict(_REPLY))[1])(name)
        for name in PROVIDER_CALLERS
    }
    monkeypatch.setattr(ev, "PROVIDER_CALLERS", patched, raising=True)
    monkeypatch.setattr(pd, "_call_google", patched["google"], raising=False)

    pd.generate_discussion(paper=_PAPER, model="claude-opus-5", acting_agent_id="agent_me")

    assert "google" not in called, (
        "a Claude model was sent to Google's API — Gemini answers 404 for it"
    )
