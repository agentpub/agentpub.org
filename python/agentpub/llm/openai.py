"""OpenAI backend — requires `pip install agentpub[openai]`.

Uses the Responses API (/v1/responses), NOT Chat Completions.
"""

from __future__ import annotations

import json
import logging
import os
import time

from .base import LLMBackend, LLMError, LLMResponse

logger = logging.getLogger("agentpub.llm.openai")


class OpenAIBackend(LLMBackend):
    def __init__(self, model: str = "gpt-5-mini", api_key: str | None = None, timeout: float | None = None):
        self._model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self._api_key:
            raise LLMError("Set OPENAI_API_KEY or pass api_key=")
        self._timeout = timeout or 600.0
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import openai
            except ImportError:
                raise LLMError("Run: pip install agentpub[openai]") from None
            self._client = openai.OpenAI(api_key=self._api_key, timeout=self._timeout)
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
        think: bool | None = None,
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
        elif is_reasoning and think is not None:
            # Control reasoning effort: think=False → low (fast JSON), think=True → high (deep writing)
            effort = "low" if not think else "high"
            kwargs["reasoning"] = {"effort": effort}

        if json_mode:
            kwargs["text"] = {"format": {"type": "json_object"}}
            # OpenAI requires the word "json" in the prompt when using json_object format
            combined = (system or "") + " " + prompt
            if "json" not in combined.lower():
                input_items[-1]["content"] += "\n\nRespond with valid JSON."

        max_retries = 4
        for attempt in range(max_retries):
            try:
                if self.on_token:
                    # Stream for live output
                    kwargs["stream"] = True
                    text = ""
                    usage = {}
                    finish_reason = ""
                    model_name = self._model
                    stream = client.responses.create(**kwargs)
                    stream_start = time.time()
                    last_data_time = stream_start
                    for event in stream:
                        now = time.time()
                        # Mid-stream stall detection: break if no events at all for 80% of timeout.
                        # Thinking models can go quiet for minutes — only break on genuine hangs.
                        stall_limit = self._timeout * 0.8
                        if now - last_data_time > stall_limit:
                            logger.error("OpenAI stream stalled for >%ds (no events), breaking", int(stall_limit))
                            break
                        # Total stream timeout
                        if now - stream_start > self._timeout:
                            logger.error("OpenAI stream exceeded %ds total timeout", int(self._timeout))
                            break
                        event_type = getattr(event, "type", "")
                        if event_type == "response.output_text.delta":
                            delta = getattr(event, "delta", "")
                            if delta:
                                text += delta
                                self._emit_token(delta, thinking=False)
                                last_data_time = time.time()
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
                            last_data_time = time.time()
                        else:
                            # Any event counts as activity (heartbeat, thinking, etc.)
                            last_data_time = time.time()
                    # Check for empty output after stream
                    if not text:
                        elapsed = time.time() - stream_start
                        raise LLMError(f"OpenAI stream produced no output after {int(elapsed)}s")
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
                break  # success
            except Exception as e:
                err_str = str(e).lower()
                is_rate_limit = "429" in err_str or "rate" in err_str or "quota" in err_str
                if is_rate_limit and attempt < max_retries - 1:
                    wait = min(30 * (2 ** attempt), 300)  # 30s, 60s, 120s, cap 300s
                    logger.warning("OpenAI rate-limited (attempt %d/%d), waiting %ds...",
                                   attempt + 1, max_retries, wait)
                    time.sleep(wait)
                    # Reset stream state for retry
                    kwargs.pop("stream", None)
                    continue
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
        """Generate JSON with OpenAI, falling back to base class retry logic for reasoning models.

        For reasoning models (gpt-5*, o*), internal thinking tokens count against
        max_output_tokens.  Bump to at least 16K so the JSON isn't truncated.
        """
        is_reasoning = self._model.startswith("o") or self._model.startswith("gpt-5")
        effective_tokens = max(max_tokens, 16000) if is_reasoning else max_tokens
        resp = self.generate(
            system, prompt, temperature=temperature, max_tokens=effective_tokens, json_mode=True
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
