"""OpenAI backend — requires `pip install agentpub[openai]`.

Uses the Responses API (/v1/responses), NOT Chat Completions.
"""

from __future__ import annotations

import json
import os

from .base import LLMBackend, LLMError, LLMResponse


class OpenAIBackend(LLMBackend):
    def __init__(self, model: str = "gpt-5-mini", api_key: str | None = None):
        self._model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self._api_key:
            raise LLMError("Set OPENAI_API_KEY or pass api_key=")
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import openai
            except ImportError:
                raise LLMError("Run: pip install agentpub[openai]") from None
            self._client = openai.OpenAI(api_key=self._api_key)
        return self._client

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_web_search(self) -> bool:
        return True

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

        # Build input array for the Responses API
        input_items = []
        if system:
            input_items.append({"role": "system", "content": system})
        input_items.append({"role": "user", "content": prompt})

        # Reasoning models (o-series, gpt-5+) don't support custom temperature
        is_reasoning = self._model.startswith("o") or self._model.startswith("gpt-5")

        kwargs: dict = {
            "model": self._model,
            "input": input_items,
            "max_output_tokens": max(16, self._effective_max_tokens(max_tokens)),
        }

        if not is_reasoning:
            kwargs["temperature"] = temperature

        if json_mode:
            kwargs["text"] = {"format": {"type": "json_object"}}

        try:
            if self.on_token:
                # Stream for live output
                kwargs["stream"] = True
                text = ""
                usage = {}
                finish_reason = ""
                model_name = self._model
                stream = client.responses.create(**kwargs)
                for event in stream:
                    event_type = getattr(event, "type", "")
                    if event_type == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if delta:
                            text += delta
                            self._emit_token(delta, thinking=False)
                    elif event_type == "response.completed":
                        resp = getattr(event, "response", None)
                        if resp:
                            model_name = getattr(resp, "model", self._model)
                            finish_reason = getattr(resp, "status", "") or ""
                            if resp.usage:
                                usage = {
                                    "input_tokens": resp.usage.input_tokens,
                                    "output_tokens": resp.usage.output_tokens,
                                    "total_tokens": resp.usage.total_tokens,
                                }
            else:
                # Non-streaming
                resp = client.responses.create(**kwargs)
                text = resp.output_text
                model_name = resp.model
                finish_reason = resp.status or ""
                usage = {}
                if resp.usage:
                    usage = {
                        "input_tokens": resp.usage.input_tokens,
                        "output_tokens": resp.usage.output_tokens,
                        "total_tokens": resp.usage.total_tokens,
                    }
        except Exception as e:
            raise LLMError(f"OpenAI request failed: {e}") from e

        self._track_usage(usage)
        return LLMResponse(
            text=text,
            model=model_name,
            provider="openai",
            usage=usage,
            finish_reason=finish_reason,
        )

    def generate_json(
        self,
        system: str,
        prompt: str,
        *,
        temperature: float = 0.4,
        max_tokens: int = 8000,
    ) -> dict:
        """Generate JSON with OpenAI, falling back to base class retry logic for reasoning models."""
        resp = self.generate(
            system, prompt, temperature=temperature, max_tokens=max_tokens, json_mode=True
        )
        try:
            return json.loads(resp.text)
        except (json.JSONDecodeError, ValueError):
            pass
        # Brace extraction (handles markdown-wrapped JSON)
        text = resp.text
        start = text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except (json.JSONDecodeError, ValueError):
                            break
        # Fall back to the base class 3-attempt strategy (retry + repair)
        return super().generate_json(system, prompt, temperature=temperature, max_tokens=max_tokens)

    def search_web(self, query: str, *, limit: int = 10) -> list[dict]:
        """Search the web using OpenAI's built-in web_search tool.

        Returns list of dicts with keys:
            title, abstract, authors, year, url, doi, source, citation_count
        """
        client = self._get_client()

        prompt = f"""Search for {limit} real, published academic papers about: {query}

Return a JSON object with key "papers" containing a list of objects, each with:
- "title": full paper title
- "authors": list of author names
- "year": publication year (integer)
- "abstract": 2-3 sentence summary of the paper
- "url": URL to the paper (prefer doi.org, arxiv.org, or semanticscholar.org)
- "doi": DOI identifier if available (e.g. "10.1234/...")
- "citation_count": approximate citation count (integer, 0 if unknown)

IMPORTANT: Only include papers that actually exist. Do not fabricate papers or authors."""

        is_reasoning = self._model.startswith("o") or self._model.startswith("gpt-5")
        kwargs: dict = {
            "model": self._model,
            "input": prompt,
            "tools": [{"type": "web_search_preview", "search_context_size": "low"}],
            "max_output_tokens": 4000,
            # Note: JSON mode ("text": {"format": ...}) is incompatible with web_search_preview.
            # We parse JSON from the text output instead (brace extraction fallback below).
        }
        if not is_reasoning:
            kwargs["temperature"] = 0.2

        try:
            resp = client.responses.create(**kwargs)
        except Exception as e:
            raise LLMError(f"OpenAI web search failed: {e}") from e

        # Track usage
        usage = {}
        if resp.usage:
            usage = {
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        self._track_usage(usage)

        # Parse structured response
        text = resp.output_text
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try brace extraction
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start:end])
                except json.JSONDecodeError:
                    return []
            else:
                return []

        papers = data.get("papers", [])

        # Also collect URL citations from annotations as supplementary sources
        url_citations = {}
        for item in resp.output:
            if hasattr(item, "content"):
                for block in item.content:
                    if hasattr(block, "annotations"):
                        for ann in block.annotations:
                            if hasattr(ann, "url") and hasattr(ann, "title"):
                                url_citations[ann.url] = ann.title

        # Normalize results
        results = []
        for p in papers:
            if not p.get("title"):
                continue
            results.append({
                "title": p["title"],
                "abstract": p.get("abstract", ""),
                "authors": p.get("authors", []),
                "year": p.get("year"),
                "citation_count": p.get("citation_count", 0),
                "url": p.get("url", ""),
                "doi": p.get("doi", ""),
                "source": "llm_web_search",
            })

        return results[:limit]
