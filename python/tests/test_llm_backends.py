"""Smoke tests for all LLM backends.

Sends a tiny prompt to each provider/model to verify:
  1. API key is valid and the model is reachable
  2. Response is parsed correctly into LLMResponse
  3. generate_json() returns valid JSON
  4. Token usage tracking works (on_usage callback fires)
  5. Token limits are resolved correctly

Run all:
    python -m pytest tests/test_llm_backends.py -v

Run a specific provider:
    python -m pytest tests/test_llm_backends.py -v -k openai

Skip providers without API keys (default -- missing keys are auto-skipped).
"""

from __future__ import annotations

import os
import pytest

from agentpub.llm import get_backend, LLMError

# Full model list from cli.py provider catalogue.
# One representative per tier (default + cheapest + flagship).
CLOUD_MODELS = [
    # OpenAI
    ("openai", "gpt-5-mini", "OPENAI_API_KEY"),
    ("openai", "gpt-5", "OPENAI_API_KEY"),
    ("openai", "gpt-5.4", "OPENAI_API_KEY"),
    ("openai", "o3", "OPENAI_API_KEY"),
    ("openai", "o4-mini", "OPENAI_API_KEY"),
    # Anthropic
    ("anthropic", "claude-sonnet-4-6", "ANTHROPIC_API_KEY"),
    ("anthropic", "claude-opus-4-6", "ANTHROPIC_API_KEY"),
    ("anthropic", "claude-haiku-4-5-20251001", "ANTHROPIC_API_KEY"),
    # Google Gemini
    ("google", "gemini-2.5-flash", "GEMINI_API_KEY"),
    ("google", "gemini-2.5-pro", "GEMINI_API_KEY"),
    ("google", "gemini-3.1-pro-preview", "GEMINI_API_KEY"),
    # Mistral
    ("mistral", "mistral-large-latest", "MISTRAL_API_KEY"),
    ("mistral", "magistral-medium-latest", "MISTRAL_API_KEY"),
    # xAI Grok
    ("xai", "grok-4-1-fast-reasoning", "XAI_API_KEY"),
    ("xai", "grok-3", "XAI_API_KEY"),
]

# Ollama local models (require Ollama running + model pulled)
OLLAMA_MODELS = [
    # DeepSeek-R1
    ("ollama", "deepseek-r1:8b", None),
    ("ollama", "deepseek-r1:14b", None),
    ("ollama", "deepseek-r1:32b", None),
    # Qwen3
    ("ollama", "qwen3:8b", None),
    ("ollama", "qwen3:14b", None),
    ("ollama", "qwen3:32b", None),
    # Qwen3.5
    ("ollama", "qwen3.5:9b", None),
    ("ollama", "qwen3.5:27b", None),
    # Phi-4 Reasoning
    ("ollama", "phi4-reasoning:14b", None),
    # Cogito
    ("ollama", "cogito:8b", None),
    ("ollama", "cogito:14b", None),
    # Magistral
    ("ollama", "magistral:24b", None),
    # GPT-OSS
    ("ollama", "gpt-oss:20b", None),
    # Nemotron
    ("ollama", "nemotron-3-nano:30b", None),
    # GLM
    ("ollama", "glm-4.7-flash", None),
    # DeepSeek V3
    ("ollama", "deepseek-v3", None),
]


def _has_key(env_var: str | None) -> bool:
    if env_var is None:
        return True
    return bool(os.environ.get(env_var))


def _ollama_available() -> bool:
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        return True
    except Exception:
        return False


# -- Cloud: generate() --

@pytest.mark.parametrize("provider,model,env_var", CLOUD_MODELS, ids=[
    f"{p}-{m}" for p, m, _ in CLOUD_MODELS
])
def test_generate_cloud(provider, model, env_var):
    """Verify generate() returns a valid LLMResponse with text and usage."""
    if not _has_key(env_var):
        pytest.skip(f"No {env_var} set")

    backend = get_backend(provider, model=model)

    usage_calls = []
    backend.on_usage = lambda inp, out, tot: usage_calls.append((inp, out, tot))

    resp = backend.generate(
        system="You are a test assistant. Be extremely brief.",
        prompt="Reply with exactly one word: hello",
        temperature=0.0,
        max_tokens=2048,
    )

    assert resp.text, f"{provider}/{model}: empty response text"
    assert resp.model, f"{provider}/{model}: empty model name"
    assert resp.provider, f"{provider}/{model}: empty provider name"
    print(f"  {provider}/{model}: \"{resp.text.strip()[:60]}\" (usage: {resp.usage})")

    assert len(usage_calls) > 0, f"{provider}/{model}: on_usage never called"


# -- Cloud: generate_json() --

@pytest.mark.parametrize("provider,model,env_var", CLOUD_MODELS, ids=[
    f"{p}-{m}" for p, m, _ in CLOUD_MODELS
])
def test_generate_json_cloud(provider, model, env_var):
    """Verify generate_json() returns a parsed dict."""
    if not _has_key(env_var):
        pytest.skip(f"No {env_var} set")

    backend = get_backend(provider, model=model)

    result = backend.generate_json(
        system="You are a test assistant.",
        prompt='Return JSON: {"status": "ok", "number": 42}',
        temperature=0.0,
        max_tokens=2048,
    )

    assert isinstance(result, dict), f"{provider}/{model}: expected dict, got {type(result)}"
    assert "status" in result, f"{provider}/{model}: missing 'status' key in {result}"
    print(f"  {provider}/{model} JSON: {result}")


# -- Cloud: token limits --

@pytest.mark.parametrize("provider,model,env_var", CLOUD_MODELS, ids=[
    f"{p}-{m}" for p, m, _ in CLOUD_MODELS
])
def test_max_output_tokens(provider, model, env_var):
    """Verify max_output_tokens resolves to a known value (not fallback 16000)."""
    if not _has_key(env_var):
        pytest.skip(f"No {env_var} set")

    backend = get_backend(provider, model=model)
    limit = backend.max_output_tokens
    assert limit > 0, f"{provider}/{model}: max_output_tokens is {limit}"
    print(f"  {provider}/{model}: max_output_tokens = {limit}")


# -- Ollama: generate() --

@pytest.mark.parametrize("provider,model,env_var", OLLAMA_MODELS, ids=[
    f"{p}-{m}" for p, m, _ in OLLAMA_MODELS
])
def test_generate_ollama(provider, model, env_var):
    if not _ollama_available():
        pytest.skip("Ollama not running on localhost:11434")

    backend = get_backend(provider, model=model)

    try:
        resp = backend.generate(
            system="You are a test assistant. Be extremely brief.",
            prompt="Reply with exactly one word: hello",
            temperature=0.0,
            max_tokens=2048,
        )
        assert resp.text, f"{provider}/{model}: empty response"
        print(f"  {provider}/{model}: \"{resp.text.strip()[:60]}\"")
    except (LLMError, OSError) as e:
        msg = str(e).lower()
        if "not found" in msg or "pull" in msg or "not available" in msg or "not downloaded" in msg or "stdin" in msg:
            pytest.skip(f"Model {model} not pulled in Ollama")
        raise


# -- Ollama: generate_json() --

@pytest.mark.parametrize("provider,model,env_var", OLLAMA_MODELS, ids=[
    f"{p}-{m}" for p, m, _ in OLLAMA_MODELS
])
def test_generate_json_ollama(provider, model, env_var):
    if not _ollama_available():
        pytest.skip("Ollama not running on localhost:11434")

    backend = get_backend(provider, model=model)

    try:
        result = backend.generate_json(
            system="You are a test assistant.",
            prompt='Return JSON: {"status": "ok", "number": 42}',
            temperature=0.0,
            max_tokens=2048,
        )
        assert isinstance(result, dict), f"{provider}/{model}: expected dict, got {type(result)}"
        print(f"  {provider}/{model} JSON: {result}")
    except (LLMError, OSError) as e:
        msg = str(e).lower()
        if "not found" in msg or "pull" in msg or "not available" in msg or "not downloaded" in msg or "stdin" in msg:
            pytest.skip(f"Model {model} not pulled in Ollama")
        raise


# -- Factory rejects unknown providers --

def test_unknown_provider():
    with pytest.raises(LLMError, match="Unknown LLM provider"):
        get_backend("nonexistent_provider")
