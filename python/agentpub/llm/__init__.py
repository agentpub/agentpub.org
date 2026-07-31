"""Pluggable LLM backend factory.

Usage:
    from agentpub.llm import get_backend
    llm = get_backend("openai", model="gpt-5-mini")
    llm = get_backend("google", model="gemini-2.5-flash")
    llm = get_backend("anthropic", model="claude-sonnet-4-6")

Currently enabled in the CLI/GUI: openai, anthropic, google.
Additional backends (ollama, mistral, xai) are implemented but not yet
exposed in the provider menu. They can still be used programmatically
via get_backend() for testing or future integration.
"""

from __future__ import annotations

from .base import LLMBackend, LLMError, LLMResponse

__all__ = ["get_backend", "LLMBackend", "LLMError", "LLMResponse"]

_PROVIDER_ALIASES = {
    "gemini": "google",
    "grok": "xai",
}


def get_backend(provider: str, model: str | None = None, **kwargs) -> LLMBackend:
    """Create an LLM backend by provider name.

    Args:
        provider: One of 'openai', 'anthropic', 'google', 'gemini', 'ollama',
                  'mistral', 'xai', 'grok'.
        model: Model name override (each provider has a sensible default).
        **kwargs: Extra args forwarded to the backend constructor (api_key, host, etc.).
    """
    provider = _PROVIDER_ALIASES.get(provider.lower(), provider.lower())

    build_kwargs = dict(kwargs)
    if model:
        build_kwargs["model"] = model

    if provider == "ollama":
        from .ollama import OllamaBackend
        return OllamaBackend(**build_kwargs)

    if provider == "openai":
        from .openai import OpenAIBackend
        return OpenAIBackend(**build_kwargs)

    if provider == "anthropic":
        from .anthropic import AnthropicBackend
        return AnthropicBackend(**build_kwargs)

    if provider == "google":
        from .google import GoogleBackend
        return GoogleBackend(**build_kwargs)

    if provider == "mistral":
        from .mistral import MistralBackend
        return MistralBackend(**build_kwargs)

    if provider == "xai":
        from .xai import XAIBackend
        return XAIBackend(**build_kwargs)

    raise LLMError(
        f"Unknown LLM provider '{provider}'. "
        f"Supported: openai, anthropic, google/gemini, ollama, mistral, xai/grok"
    )
