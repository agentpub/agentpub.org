"""Anthropic backend — requires `pip install agentpub[anthropic]`."""

from __future__ import annotations

import json
import logging
import os
import time

from .base import LLMBackend, LLMError, LLMResponse

logger = logging.getLogger("agentpub.llm.anthropic")


class AnthropicBackend(LLMBackend):
    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None, timeout: float | None = None):
        self._model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise LLMError("Set ANTHROPIC_API_KEY or pass api_key=")
        self._timeout = timeout or 600.0
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise LLMError("Run: pip install agentpub[anthropic]") from None
            self._client = anthropic.Anthropic(api_key=self._api_key, timeout=self._timeout)
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
        think: bool | None = None,
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

        # Extended thinking: temperature must be 1.0, budget_tokens controls reasoning depth
        if think:
            thinking_budget = min(max_tokens, 10000)
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
            kwargs["temperature"] = 1.0  # required for extended thinking
            # Increase max_tokens to accommodate thinking + output
            kwargs["max_tokens"] = max(kwargs["max_tokens"], max_tokens + thinking_budget)
        elif not self._model.startswith("o"):
            # Anthropic doesn't support temperature on all models; skip for o-series style
            kwargs["temperature"] = temperature

        max_retries = 4
        for attempt in range(max_retries):
            try:
                if self.on_token:
                    # Stream for live output
                    text = ""
                    usage = {}
                    model_name = self._model
                    finish_reason = ""
                    last_data_time = time.time()
                    with client.messages.stream(**kwargs) as stream:
                        for event in stream:
                            if hasattr(event, "type"):
                                if event.type == "content_block_delta":
                                    delta = getattr(event.delta, "text", "")
                                    if delta:
                                        text += delta
                                        last_data_time = time.time()
                                        is_thinking = getattr(event.delta, "type", "") == "thinking_delta"
                                        self._emit_token(delta, thinking=is_thinking)
                        resp = stream.get_final_message()
                        model_name = resp.model
                        finish_reason = resp.stop_reason or ""
                        if resp.usage:
                            usage = {
                                "prompt_tokens": resp.usage.input_tokens,
                                "completion_tokens": resp.usage.output_tokens,
                                "total_tokens": (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0),
                            }
                    # Check for stalled stream
                    if not text:
                        stall = time.time() - last_data_time
                        if stall > self._timeout * 0.8:
                            raise LLMError(f"Anthropic stream produced no output after {int(stall)}s")
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
                            "total_tokens": (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0),
                        }
                break  # success
            except Exception as e:
                err_str = str(e).lower()
                is_retryable = ("429" in err_str or "rate" in err_str or "overloaded" in err_str
                                or "529" in err_str or "500" in err_str or "502" in err_str
                                or "503" in err_str or "server" in err_str)
                if is_retryable and attempt < max_retries - 1:
                    wait = min(30 * (2 ** attempt), 300)
                    logger.warning("Anthropic retryable error (attempt %d/%d), waiting %ds: %s",
                                   attempt + 1, max_retries, wait, str(e)[:120])
                    time.sleep(wait)
                    continue
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
