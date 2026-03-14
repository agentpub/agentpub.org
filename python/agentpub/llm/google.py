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
_REQUEST_TIMEOUT = 300  # 5 minutes


class GoogleBackend(LLMBackend):
    def __init__(self, model: str = "gemini-2.5-flash", api_key: str | None = None):
        # gemini-2.5-flash is already a reasoning model with built-in thinking
        self._model_name_str = model
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self._api_key:
            raise LLMError("Set GEMINI_API_KEY or GOOGLE_API_KEY, or pass api_key=")
        self._genai = None
        self._model = None

    def _setup(self):
        if self._genai is None:
            try:
                import google.generativeai as genai
            except ImportError:
                raise LLMError("Run: pip install agentpub[google]") from None
            genai.configure(api_key=self._api_key)
            self._genai = genai
            self._model = genai.GenerativeModel(self._model_name_str)

    @property
    def provider_name(self) -> str:
        return "google"

    @property
    def model_name(self) -> str:
        return self._model_name_str

    def generate(
        self,
        system: str,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int = 8000,
        json_mode: bool = False,
    ) -> LLMResponse:
        self._setup()

        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        generation_config: dict = {
            "temperature": temperature,
            "max_output_tokens": self._effective_max_tokens(max_tokens),
        }
        if json_mode:
            generation_config["response_mime_type"] = "application/json"

        try:
            if self.on_token:
                # Stream for live output — with per-chunk stall detection
                text = ""
                try:
                    req_opts = self._genai.types.RequestOptions(timeout=_REQUEST_TIMEOUT)
                except (AttributeError, TypeError):
                    req_opts = None
                call_kwargs = {
                    "generation_config": self._genai.GenerationConfig(**generation_config),
                    "stream": True,
                }
                if req_opts is not None:
                    call_kwargs["request_options"] = req_opts
                resp = self._model.generate_content(full_prompt, **call_kwargs)
                last_chunk_time = threading.Event()
                stall_seconds = _REQUEST_TIMEOUT

                for chunk in resp:
                    delta = ""
                    is_thinking = False
                    try:
                        delta = chunk.text
                    except (ValueError, AttributeError):
                        # Fallback: extract only NON-thinking parts to avoid
                        # corrupting output with internal reasoning text.
                        if chunk.candidates:
                            for part in chunk.candidates[0].content.parts:
                                if getattr(part, "thought", False):
                                    is_thinking = True
                                    # Emit thinking text for display but don't add to output
                                    think_text = getattr(part, "text", "")
                                    if think_text:
                                        self._emit_token(think_text, thinking=True)
                                else:
                                    delta += getattr(part, "text", "")
                    if delta:
                        text += delta
                        # Detect thinking on non-fallback path
                        if not is_thinking and chunk.candidates:
                            for part in chunk.candidates[0].content.parts:
                                if getattr(part, "thought", False):
                                    is_thinking = True
                        self._emit_token(delta, thinking=is_thinking)
                # Resolve the full response for usage metadata
                resp.resolve()
            else:
                # Non-streaming — run in a thread with timeout
                result_container: dict = {}
                exc_container: list = []

                def _call():
                    try:
                        result_container["resp"] = self._model.generate_content(
                            full_prompt,
                            generation_config=self._genai.GenerationConfig(**generation_config),
                        )
                    except Exception as e:
                        exc_container.append(e)

                t = threading.Thread(target=_call, daemon=True)
                t.start()
                t.join(timeout=_REQUEST_TIMEOUT)
                if t.is_alive():
                    logger.error("Gemini API call timed out after %ds", _REQUEST_TIMEOUT)
                    raise LLMError(f"Gemini request timed out after {_REQUEST_TIMEOUT}s")
                if exc_container:
                    raise exc_container[0]
                resp = result_container["resp"]

                text = ""
                try:
                    text = resp.text
                except ValueError:
                    if resp.candidates:
                        for part in resp.candidates[0].content.parts:
                            text += part.text
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"Gemini request failed: {e}") from e

        usage = {}
        if hasattr(resp, "usage_metadata") and resp.usage_metadata:
            um = resp.usage_metadata
            thinking = getattr(um, "thoughts_token_count", 0) or 0
            prompt = getattr(um, "prompt_token_count", 0) or 0
            completion = getattr(um, "candidates_token_count", 0) or 0
            total = getattr(um, "total_token_count", 0) or 0
            usage = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "thinking_tokens": thinking,
                "total_tokens": total,
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
            google_search_tool = self._genai.Tool(
                google_search=self._genai.GoogleSearch()
            )
        except (AttributeError, TypeError):
            # Older SDK version without Google Search tool
            return []

        generation_config = self._genai.GenerationConfig(
            temperature=0.2,
            max_output_tokens=32000,
            response_mime_type="application/json",
        )

        try:
            resp = self._model.generate_content(
                prompt,
                generation_config=generation_config,
                tools=[google_search_tool],
            )
        except Exception as e:
            raise LLMError(f"Gemini web search failed: {e}") from e

        # Track usage
        usage = {}
        if hasattr(resp, "usage_metadata") and resp.usage_metadata:
            um = resp.usage_metadata
            usage = {
                "input_tokens": getattr(um, "prompt_token_count", 0),
                "output_tokens": getattr(um, "candidates_token_count", 0),
                "total_tokens": getattr(um, "total_token_count", 0),
            }
        self._track_usage(usage)

        text = ""
        try:
            text = resp.text
        except ValueError:
            if resp.candidates:
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
