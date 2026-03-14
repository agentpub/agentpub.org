"""Anthropic backend — requires `pip install agentpub[anthropic]`."""

from __future__ import annotations

import json
import os

from .base import LLMBackend, LLMError, LLMResponse


class AnthropicBackend(LLMBackend):
    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None):
        self._model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise LLMError("Set ANTHROPIC_API_KEY or pass api_key=")
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise LLMError("Run: pip install agentpub[anthropic]") from None
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    @property
    def provider_name(self) -> str:
        return "anthropic"

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

        effective_system = system
        if json_mode:
            json_instruction = "\n\nIMPORTANT: Respond with ONLY valid JSON. No markdown, no explanation, no code fences."
            effective_system = (system + json_instruction) if system else json_instruction.strip()

        kwargs: dict = {
            "model": self._model,
            "max_tokens": self._effective_max_tokens(max_tokens),
            "messages": [{"role": "user", "content": prompt}],
        }
        if effective_system:
            kwargs["system"] = effective_system
        # Anthropic doesn't support temperature on all models; skip for o-series style
        if not self._model.startswith("o"):
            kwargs["temperature"] = temperature

        try:
            if self.on_token:
                # Stream for live output
                text = ""
                usage = {}
                model_name = self._model
                finish_reason = ""
                with client.messages.stream(**kwargs) as stream:
                    for event in stream:
                        if hasattr(event, "type"):
                            if event.type == "content_block_delta":
                                delta = getattr(event.delta, "text", "")
                                if delta:
                                    text += delta
                                    # Anthropic extended thinking comes as thinking blocks
                                    is_thinking = getattr(event.delta, "type", "") == "thinking_delta"
                                    self._emit_token(delta, thinking=is_thinking)
                    resp = stream.get_final_message()
                    model_name = resp.model
                    finish_reason = resp.stop_reason or ""
                    if resp.usage:
                        usage = {
                            "prompt_tokens": resp.usage.input_tokens,
                            "completion_tokens": resp.usage.output_tokens,
                        }
            else:
                resp = client.messages.create(**kwargs)
                text = ""
                for block in resp.content:
                    if hasattr(block, "text"):
                        text += block.text
                model_name = resp.model
                finish_reason = resp.stop_reason or ""
                usage = {}
                if resp.usage:
                    usage = {
                        "prompt_tokens": resp.usage.input_tokens,
                        "completion_tokens": resp.usage.output_tokens,
                    }
        except Exception as e:
            raise LLMError(f"Anthropic request failed: {e}") from e

        self._track_usage(usage)
        return LLMResponse(
            text=text,
            model=model_name,
            provider="anthropic",
            usage=usage,
            finish_reason=finish_reason,
        )

    @property
    def supports_web_search(self) -> bool:
        return True

    def search_web(self, query: str, *, limit: int = 10) -> list[dict]:
        """Search the web using Claude's built-in web_search tool.

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

IMPORTANT: Only include papers that actually exist. Use web search to verify."""

        try:
            resp = client.messages.create(
                model=self._model,
                max_tokens=8000,
                temperature=0.2,
                messages=[{"role": "user", "content": prompt}],
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 5,
                }],
            )
        except Exception as e:
            raise LLMError(f"Anthropic web search failed: {e}") from e

        # Track usage
        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.input_tokens,
                "completion_tokens": resp.usage.output_tokens,
            }
        self._track_usage(usage)

        # Extract text from response (may have multiple content blocks)
        text = ""
        url_citations = {}
        for block in resp.content:
            if hasattr(block, "text"):
                text += block.text
                # Collect URL citations from inline citations
                if hasattr(block, "citations"):
                    for cit in block.citations:
                        if hasattr(cit, "url") and hasattr(cit, "title"):
                            url_citations[cit.url] = cit.title

        # Parse JSON from the response
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
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
