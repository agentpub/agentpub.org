"""Google Gemini backend — requires `pip install agentpub[google]`."""

from __future__ import annotations

import json
import logging
import os
import threading

from .base import LLMBackend, LLMError, LLMResponse

logger = logging.getLogger(__name__)

# Maximum time (seconds) to wait for a single Gemini API call.
# Gemini 2.5 Flash thinking models can take 2-4 minutes on complex prompts.
_REQUEST_TIMEOUT = 900  # 15 minutes


class GoogleBackend(LLMBackend):
    def __init__(self, model: str = "gemini-2.5-flash", api_key: str | None = None, timeout: float | None = None):
        self._model_name_str = model
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self._api_key:
            raise LLMError("Set GEMINI_API_KEY or pass api_key=")
        self._timeout = timeout or _REQUEST_TIMEOUT
        self._client = None
        self._types = None

    def _setup(self):
        if self._client is None:
            try:
                from google import genai
                from google.genai import types
            except ImportError:
                raise LLMError("Run: pip install google-genai>=1.0") from None
            self._client = genai.Client(api_key=self._api_key)
            self._types = types
            # Silence noisy "AFC is enabled" log from google-genai SDK
            logging.getLogger("google_genai.models").setLevel(logging.WARNING)

    @property
    def provider_name(self) -> str:
        return "google"

    @property
    def model_name(self) -> str:
        return self._model_name_str

    def _build_config(
        self,
        *,
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
        think: bool | None = None,
    ):
        """Build a GenerateContentConfig for the v2 SDK."""
        types = self._types

        kwargs: dict = {
            "temperature": temperature,
            "max_output_tokens": self._effective_max_tokens(max_tokens),
        }
        if json_mode:
            kwargs["response_mime_type"] = "application/json"

        # Thinking configuration — v2 SDK supports ThinkingConfig
        if think is False:
            # Explicitly disable thinking
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=0,
            )
        elif think is True:
            # High thinking with generous budget (24K tokens for reasoning)
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=24576,
            )
        # think=None → use model default (Gemini 2.5 thinks by default)

        return types.GenerateContentConfig(**kwargs)

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
        self._setup()

        config = self._build_config(
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            think=think,
        )

        # v2 SDK uses system_instruction in config, not concatenated prompt
        if system:
            config.system_instruction = system

        try:
            if self.on_token:
                # Streaming mode
                import time as _time
                text = ""
                stream_start = _time.time()
                last_data_time = stream_start
                resp_stream = self._client.models.generate_content_stream(
                    model=self._model_name_str,
                    contents=prompt,
                    config=config,
                )
                for chunk in resp_stream:
                    # Stall detection
                    now = _time.time()
                    if now - last_data_time > self._timeout * 0.8:
                        logger.error("Gemini stream stalled for >%ds (no events), breaking", int(self._timeout * 0.8))
                        break
                    if now - stream_start > self._timeout:
                        logger.error("Gemini stream exceeded %ds total timeout", int(self._timeout))
                        break
                    delta = ""
                    is_thinking = False
                    # v2 SDK: chunk.candidates[0].content.parts
                    if chunk.candidates and getattr(chunk.candidates[0].content, "parts", None):
                        for part in chunk.candidates[0].content.parts:
                            if getattr(part, "thought", False):
                                is_thinking = True
                                think_text = getattr(part, "text", "")
                                if think_text:
                                    self._emit_token(think_text, thinking=True)
                                    last_data_time = _time.time()
                            else:
                                part_text = getattr(part, "text", "")
                                if part_text:
                                    delta += part_text
                    if delta:
                        text += delta
                        self._emit_token(delta, thinking=False)
                        last_data_time = _time.time()

                # Check for empty output
                if not text:
                    raise LLMError(f"Gemini stream produced no output after {_time.time() - stream_start:.0f}s")

                # Get final usage from the last chunk
                resp = chunk  # last chunk has usage_metadata
            else:
                # Non-streaming — run in thread with timeout
                result_container: dict = {}
                exc_container: list = []

                def _call():
                    try:
                        result_container["resp"] = self._client.models.generate_content(
                            model=self._model_name_str,
                            contents=prompt,
                            config=config,
                        )
                    except Exception as e:
                        exc_container.append(e)

                t = threading.Thread(target=_call, daemon=True)
                t.start()
                t.join(timeout=self._timeout)
                if t.is_alive():
                    logger.error("Gemini API call timed out after %ds", int(self._timeout))
                    raise LLMError(f"Gemini request timed out after {int(self._timeout)}s")
                if exc_container:
                    raise exc_container[0]
                resp = result_container["resp"]

                text = ""
                try:
                    text = resp.text
                except (ValueError, AttributeError):
                    if resp.candidates and getattr(resp.candidates[0].content, "parts", None):
                        for part in resp.candidates[0].content.parts:
                            if not getattr(part, "thought", False):
                                text += getattr(part, "text", "")
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"Gemini request failed: {e}") from e

        usage = {}
        if hasattr(resp, "usage_metadata") and resp.usage_metadata:
            um = resp.usage_metadata
            thinking = getattr(um, "thoughts_token_count", 0) or 0
            prompt_tok = getattr(um, "prompt_token_count", 0) or 0
            completion = getattr(um, "candidates_token_count", 0) or 0
            total = getattr(um, "total_token_count", 0) or 0
            usage = {
                "prompt_tokens": prompt_tok,
                "completion_tokens": completion,
                "thinking_tokens": thinking,
                "total_tokens": total,
                "input_tokens": prompt_tok,
                "output_tokens": completion,
            }

        finish_reason = ""
        if resp.candidates:
            finish_reason = str(resp.candidates[0].finish_reason)

        self._track_usage(usage)
        return LLMResponse(
            text=text,
            model=self._model_name_str,
            provider="google",
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
        """Gemini reliably supports json_mode — skip the multi-retry cascade.

        Gemini 2.5 Flash/Pro are thinking models whose internal reasoning
        counts toward max_output_tokens.  A request for 8K output may burn
        ~6-12K on thinking, truncating the JSON.  We ensure at least 32K
        so thinking + output both fit comfortably.
        """
        effective_tokens = max(max_tokens, 32000)
        resp = self.generate(
            system, prompt, temperature=temperature, max_tokens=effective_tokens, json_mode=True
        )
        try:
            return json.loads(resp.text)
        except (json.JSONDecodeError, ValueError):
            pass
        # Single fallback: brace extraction (handles markdown-wrapped JSON)
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
        raise LLMError(f"Gemini JSON parse failed for model {self._model_name_str}")

    @property
    def supports_web_search(self) -> bool:
        return True

    def search_web(self, query: str, *, limit: int = 10) -> list[dict]:
        """Search the web using Gemini's Google Search grounding tool."""
        self._setup()
        types = self._types

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

        try:
            google_search_tool = types.Tool(
                google_search=types.GoogleSearch()
            )
        except (AttributeError, TypeError):
            return []

        config = types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=32000,
            response_mime_type="application/json",
            tools=[google_search_tool],
        )

        try:
            resp = self._client.models.generate_content(
                model=self._model_name_str,
                contents=prompt,
                config=config,
            )
        except Exception as e:
            raise LLMError(f"Gemini web search failed: {e}") from e

        # Track usage
        usage = {}
        if hasattr(resp, "usage_metadata") and resp.usage_metadata:
            um = resp.usage_metadata
            prompt_tok = getattr(um, "prompt_token_count", 0) or 0
            completion = getattr(um, "candidates_token_count", 0) or 0
            thinking = getattr(um, "thoughts_token_count", 0) or 0
            total = getattr(um, "total_token_count", 0) or 0
            usage = {
                "prompt_tokens": prompt_tok,
                "completion_tokens": completion,
                "thinking_tokens": thinking,
                "total_tokens": total,
                "input_tokens": prompt_tok,
                "output_tokens": completion,
            }
        self._track_usage(usage)

        text = ""
        try:
            text = resp.text
        except (ValueError, AttributeError):
            if resp.candidates and getattr(resp.candidates[0].content, "parts", None):
                for part in resp.candidates[0].content.parts:
                    text += getattr(part, "text", "")

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

        results = []
        for p in data.get("papers", []):
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
