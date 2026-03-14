"""xAI Grok backend — uses OpenAI-compatible API at api.x.ai.

Requires `pip install agentpub[xai]` (installs openai SDK).
"""

from __future__ import annotations

import json
import os

from .base import LLMBackend, LLMError, LLMResponse


class XAIBackend(LLMBackend):
    def __init__(self, model: str = "grok-4-1-fast-reasoning", api_key: str | None = None):
        self._model = model
        self._api_key = api_key or os.environ.get("XAI_API_KEY")
        if not self._api_key:
            raise LLMError("Set XAI_API_KEY or pass api_key=")
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import openai
            except ImportError:
                raise LLMError("Run: pip install agentpub[xai]") from None
            self._client = openai.OpenAI(
                api_key=self._api_key,
                base_url="https://api.x.ai/v1",
            )
        return self._client

    @property
    def provider_name(self) -> str:
        return "xai"

    @property
    def model_name(self) -> str:
        return self._model

    def generate(
        self,
        system: str,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int = 8000,
        json_mode: bool = False,
    ) -> LLMResponse:
        client = self._get_client()

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # Reasoning models don't support custom temperature
        is_reasoning = "reasoning" in self._model or self._model.startswith("grok-4-0")

        kwargs: dict = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._effective_max_tokens(max_tokens),
        }

        if not is_reasoning:
            kwargs["temperature"] = temperature

        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            if self.on_token:
                kwargs["stream"] = True
                text = ""
                usage = {}
                finish_reason = ""
                model_name = self._model
                stream = client.chat.completions.create(**kwargs)
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        delta = chunk.choices[0].delta.content
                        text += delta
                        self._emit_token(delta, thinking=False)
                    if chunk.choices and chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage = {
                            "input_tokens": chunk.usage.prompt_tokens,
                            "output_tokens": chunk.usage.completion_tokens,
                            "total_tokens": chunk.usage.total_tokens,
                        }
                    if hasattr(chunk, "model") and chunk.model:
                        model_name = chunk.model
            else:
                resp = client.chat.completions.create(**kwargs)
                text = resp.choices[0].message.content or ""
                model_name = resp.model or self._model
                finish_reason = resp.choices[0].finish_reason or ""
                usage = {}
                if resp.usage:
                    usage = {
                        "input_tokens": resp.usage.prompt_tokens,
                        "output_tokens": resp.usage.completion_tokens,
                        "total_tokens": resp.usage.total_tokens,
                    }
        except Exception as e:
            raise LLMError(f"Grok request failed: {e}") from e

        self._track_usage(usage)
        return LLMResponse(
            text=text,
            model=model_name,
            provider="xai",
            usage=usage,
            finish_reason=finish_reason,
        )
