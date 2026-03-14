"""Abstract LLM backend interface and shared types."""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def strip_thinking_tags(text: str) -> str:
    """Remove LLM reasoning/thinking blocks from output.

    Strips <think>, <thinking>, <reasoning>, <internal>, <reflection> tags
    (both closed and unclosed).
    """
    for tag in ("think", "thinking", "reasoning", "internal", "reflection"):
        # Closed tags: <tag>...</tag>
        text = re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL)
        # Unclosed tag at end (model stopped mid-thought)
        text = re.sub(rf"<{tag}>.*", "", text, flags=re.DOTALL)
        # Orphaned closing tags (e.g. stray </think> without matching opener)
        text = re.sub(rf"</{tag}>", "", text, flags=re.IGNORECASE)
    return text.strip()


class LLMError(Exception):
    """Raised when an LLM call fails."""


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    usage: dict = field(default_factory=dict)
    finish_reason: str = ""


class LLMBackend(ABC):
    """Base class for pluggable LLM backends."""

    # Token streaming callback: called with each chunk of text as it arrives.
    # Set by the researcher to feed live output to the display.
    on_token: "Callable[[str, bool], None] | None" = None

    # Usage callback: called after each generation with cumulative token counts.
    on_usage: "Callable[[int, int, int], None] | None" = None

    # Heartbeat callback: called periodically during long generations.
    # Receives (elapsed_seconds, words_so_far, is_thinking).
    on_heartbeat: "Callable[[float, int, bool], None] | None" = None

    # Set to True to abort the current generation mid-stream.
    interrupted: bool = False

    # Override in __init__ to set a custom output token limit
    _max_output_tokens: int | None = None

    # Known model output limits (model prefix -> max output tokens)
    # Values from official API docs as of 2025-2026.
    _MODEL_OUTPUT_LIMITS: dict[str, int] = {
        # OpenAI thinking/reasoning models
        "gpt-5.4-pro": 128000,
        "gpt-5.4": 128000,
        "gpt-5.3": 128000,
        "gpt-5.2": 128000,
        "gpt-5.1": 128000,
        "gpt-5-mini": 128000,
        "gpt-5": 128000,
        "o1": 100000,
        "o3": 100000,
        "o4-mini": 100000,
        # OpenAI older models (still functional)
        "gpt-4.1": 32768,
        "gpt-4o": 16384,
        "gpt-4": 16384,
        # Anthropic
        "claude-opus": 32000,
        "claude-sonnet": 64000,
        "claude-haiku": 32000,
        "claude": 32000,
        # Google Gemini
        "gemini-3.1": 65536,
        "gemini-2.5-flash": 65536,
        "gemini-2.5-pro": 65536,
        "gemini-2.0": 65536,
        "gemini-1.5-pro": 65536,
        "gemini-1.5-flash": 65536,
        "gemini": 65536,
        # Mistral
        "magistral": 40000,
        "mistral-large": 128000,
        "mistral-medium": 32000,
        "mistral": 32000,
        # xAI Grok
        "grok-4": 128000,
        "grok-3": 128000,
        "grok": 32000,
        # Local/Ollama models — limited by VRAM
        "deepseek-r1:1.5b": 8000,
        "deepseek-r1:7b": 16000,
        "deepseek-r1:8b": 16000,
        "deepseek-r1:14b": 16000,
        "deepseek-r1:32b": 32000,
        "deepseek-r1:70b": 32000,
        "deepseek-r1": 16000,
        "deepseek-v3": 32000,
        "qwen3.5": 32000,
        "qwen3:0.6b": 8000,
        "qwen3:1.7b": 8000,
        "qwen3:4b": 16000,
        "qwen3:8b": 32000,
        "qwen3:14b": 32000,
        "qwen3:30b": 32000,
        "qwen3:32b": 32000,
        "qwen3": 32000,
        "glm-4.7-flash": 16000,
        "phi4-reasoning": 16000,
        "phi4": 16000,
        "cogito": 16000,
        "gemma3": 16000,
        "gemma2": 8000,
        "llama3.3": 32000,
        "llama3:8b": 8000,
        "llama3": 8000,
        "mixtral": 32000,
        "command-r": 16000,
        "nemotron-3-nano": 32000,
        "gpt-oss": 32000,
    }

    @property
    def max_output_tokens(self) -> int:
        """Effective max output tokens for this model.

        Resolution order:
          1. Explicit override via _max_output_tokens
          2. Lookup in _MODEL_OUTPUT_LIMITS by model name prefix
          3. Default: 16000
        """
        if self._max_output_tokens is not None:
            return self._max_output_tokens
        model = self.model_name.lower()
        # Try exact match first, then progressively shorter prefixes
        for prefix in sorted(self._MODEL_OUTPUT_LIMITS, key=len, reverse=True):
            if model.startswith(prefix):
                return self._MODEL_OUTPUT_LIMITS[prefix]
        return 16000

    def _effective_max_tokens(self, requested: int) -> int:
        """Return the requested value, capped at the model's known limit."""
        return min(requested, self.max_output_tokens)

    def _emit_token(self, text: str, *, thinking: bool = False) -> None:
        """Send a token to the streaming callback if one is registered."""
        if self.interrupted:
            raise LLMError("Generation interrupted")
        if self.on_token and text:
            self.on_token(text, thinking)

    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        ...

    @abstractmethod
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
        ...

    @property
    def supports_web_search(self) -> bool:
        """Whether this backend supports native web search."""
        return False

    def search_web(self, query: str, *, limit: int = 10) -> list[dict]:
        """Search the web for academic papers using the LLM's native search.

        Returns list of dicts with keys:
            title, abstract, authors, year, url, doi, source, citation_count

        Override in backends that support web search. Returns empty list by default.
        """
        return []

    def suggest_papers(self, query: str, *, limit: int = 10) -> list[dict]:
        """Ask the LLM to suggest known academic papers from its training data.

        Returns list of dicts with keys:
            title, authors, year, doi, abstract

        These suggestions should be validated against Crossref/Semantic Scholar
        before being used as references.
        """
        system = (
            "You are an academic research librarian. Given a research topic, suggest real, "
            "published academic papers that you are confident actually exist. "
            "For each paper, provide the exact title, author list, publication year, and DOI if known.\n\n"
            "CRITICAL: Only suggest papers you are highly confident are real. "
            "Do NOT fabricate titles or authors. If unsure, suggest fewer papers rather than hallucinate."
        )
        prompt = f"""Research topic: {query}

Suggest up to {limit} real, published academic papers relevant to this topic.
Prioritize highly-cited, well-known papers in this field.

Return JSON: {{"papers": [
  {{"title": "exact paper title", "authors": ["Author Name"], "year": 2023, "doi": "10.xxxx/..." or null, "abstract": "brief description"}},
  ...
]}}"""
        try:
            result = self.generate_json(system, prompt, temperature=0.3)
            papers = result.get("papers", [])
            if not isinstance(papers, list):
                return []
            return [
                {
                    "title": p.get("title", ""),
                    "authors": p.get("authors", []),
                    "year": p.get("year"),
                    "doi": p.get("doi") or "",
                    "abstract": p.get("abstract", ""),
                    "source": "llm_knowledge",
                }
                for p in papers
                if p.get("title")
            ][:limit]
        except LLMError:
            return []

    @property
    def total_usage(self) -> dict:
        """Accumulated token usage across all generate() calls."""
        if not hasattr(self, "_total_usage"):
            self._total_usage = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "total_tokens": 0}
        return dict(self._total_usage)

    def _track_usage(self, usage: dict) -> None:
        """Accumulate token usage from a single response."""
        if not hasattr(self, "_total_usage"):
            self._total_usage = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "total_tokens": 0}
        self._total_usage["input_tokens"] += usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
        self._total_usage["output_tokens"] += usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
        self._total_usage["thinking_tokens"] += usage.get("thinking_tokens", 0)
        self._total_usage["total_tokens"] += usage.get("total_tokens", 0)
        if self.on_usage:
            try:
                self.on_usage(
                    self._total_usage["input_tokens"],
                    self._total_usage["output_tokens"],
                    self._total_usage["total_tokens"],
                )
            except Exception:
                pass

    def generate_json(
        self,
        system: str,
        prompt: str,
        *,
        temperature: float = 0.4,
        max_tokens: int = 8000,
    ) -> dict:
        """Generate and parse JSON, with fallback brace-extraction + retry.

        Strategy:
          1. json_mode=True  → parse directly
          2. Extract {…} from raw text
          3. Retry WITHOUT json_mode (some models don't support it) → extract {…}
          4. Ask model to repair → extract {…}
          5. Raise LLMError
        """
        # --- Attempt 1: json_mode=True ---
        logger.debug("generate_json attempt 1 (json_mode=True)...")
        resp = self.generate(
            system, prompt, temperature=temperature, max_tokens=max_tokens, json_mode=True, think=False
        )
        parsed = _try_parse(resp.text)
        if parsed is not None:
            return parsed
        logger.warning("generate_json attempt 1 failed to parse (%d chars): %.200s", len(resp.text), resp.text)

        # --- Attempt 2: retry WITHOUT json_mode (freeform output) ---
        # Many local models (gpt-oss, some Ollama models) don't handle
        # Ollama's format:"json" well but can produce JSON in freeform text.
        logger.debug("generate_json attempt 2 (freeform)...")
        json_hint = "\n\nIMPORTANT: Your entire response must be valid JSON. No markdown, no explanation, just the JSON object."
        resp2 = self.generate(
            system + json_hint,
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=False,
            think=False,
        )
        parsed = _try_parse(resp2.text)
        if parsed is not None:
            return parsed
        logger.warning("generate_json attempt 2 failed to parse (%d chars): %.200s", len(resp2.text), resp2.text)

        # --- Attempt 3: ask model to repair the best raw text ---
        logger.debug("generate_json attempt 3 (repair)...")
        best_raw = resp2.text if resp2.text.strip() else resp.text
        fix_resp = self.generate(
            "You are a JSON repair tool. Return ONLY valid JSON, no markdown, no explanation.",
            f"Fix this broken JSON and return only the corrected JSON:\n\n{best_raw[:4000]}",
            temperature=0.0,
            max_tokens=max_tokens,
            think=False,
            json_mode=False,
        )
        parsed = _try_parse(fix_resp.text)
        if parsed is not None:
            return parsed

        raise LLMError(f"Failed to parse JSON from {self.provider_name} after retry")


def _try_parse(text: str) -> dict | None:
    """Try json.loads then brace extraction."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    return _extract_json(text)


def _extract_json(text: str) -> dict | None:
    """Try to extract a JSON object from text with surrounding noise."""
    # Find outermost braces
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    end = -1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        # Try removing trailing commas (common LLM mistake)
        cleaned = re.sub(r",\s*([}\]])", r"\1", text[start:end])
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None
