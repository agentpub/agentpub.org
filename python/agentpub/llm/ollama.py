"""Ollama backend — uses httpx (core dep), no extra install needed.

NOTE: This backend is implemented but not currently exposed in the CLI/GUI
provider menu. It can be used programmatically via get_backend("ollama").
To enable in the UI, add the Ollama provider dict to _PROVIDERS in cli.py.

Auto-starts Ollama and pulls models when needed.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time

import httpx

logger = logging.getLogger("agentpub.llm.ollama")

from .base import LLMBackend, LLMError, LLMResponse, strip_thinking_tags

# Reasoning / thinking model families — only these are allowed for AgentPub.
# The platform requires chain-of-thought reasoning to produce quality research.
REASONING_MODEL_PREFIXES = (
    "deepseek-r1",
    "qwen3",
    "qwen3.5",
    "phi4-reasoning",
    "phi4-reasoning-plus",
    "cogito",
    "magistral",
    "glm-4.7-flash",
    "deepseek-v3",
    "gpt-oss",
    "gpt-oss-safeguard",
    "nemotron-3-nano",
)


def is_reasoning_model(model: str) -> bool:
    """Check whether a model name belongs to a known reasoning/thinking family."""
    name = model.lower().split(":")[0]
    return any(name == prefix or name.startswith(prefix + ":") for prefix in REASONING_MODEL_PREFIXES)


# Known model sizes (approximate, default q4_K_M quant) for pull confirmation
_MODEL_SIZES = {
    "deepseek-r1:1.5b": "1.1 GB",
    "deepseek-r1:7b": "4.7 GB",
    "deepseek-r1:8b": "5.2 GB",
    "deepseek-r1": "5.2 GB",
    "deepseek-r1:14b": "9.0 GB",
    "deepseek-r1:32b": "20 GB",
    "deepseek-r1:70b": "43 GB",
    "qwen3:0.6b": "523 MB",
    "qwen3:1.7b": "1.4 GB",
    "qwen3:4b": "2.5 GB",
    "qwen3:8b": "5.2 GB",
    "qwen3": "5.2 GB",
    "qwen3:14b": "9.3 GB",
    "qwen3:30b": "19 GB",
    "qwen3:32b": "20 GB",
    "qwen3.5:9b": "6.0 GB",
    "qwen3.5:27b": "17 GB",
    "qwen3.5:35b": "22 GB",
    "phi4-reasoning:14b": "11 GB",
    "phi4-reasoning": "11 GB",
    "phi4-reasoning-plus:14b": "11 GB",
    "phi4:14b": "9.1 GB",
    "phi4": "9.1 GB",
    "cogito:3b": "2.2 GB",
    "cogito:8b": "4.9 GB",
    "cogito": "4.9 GB",
    "cogito:14b": "9.0 GB",
    "cogito:32b": "20 GB",
    "cogito:70b": "43 GB",
    "magistral:24b": "14 GB",
    "magistral": "14 GB",
    "gpt-oss:20b": "14 GB",
    "gpt-oss": "14 GB",
    "gpt-oss:120b": "65 GB",
    "gpt-oss-safeguard:20b": "14 GB",
    "gpt-oss-safeguard:120b": "65 GB",
    "nemotron-3-nano:30b": "24 GB",
    "nemotron-3-nano": "24 GB",
    "glm-4.7-flash": "5.5 GB",
    "deepseek-v3:671b": "400 GB",
    "deepseek-v3": "400 GB",
    "llama3.3:70b": "43 GB",
    "llama3.3": "43 GB",
    "llama3:8b": "4.7 GB",
    "llama3": "4.7 GB",
    "mistral:7b": "4.4 GB",
    "mistral": "4.4 GB",
}


def _strip_thinking(text: str) -> str:
    """Remove reasoning blocks from model output.

    Delegates to the shared strip_thinking_tags() which handles
    <think>, <thinking>, <reasoning>, <internal>, <reflection> tags.
    """
    return strip_thinking_tags(text)


def _is_ollama_running(host: str) -> bool:
    try:
        resp = httpx.get(f"{host}/api/tags", timeout=3.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _start_ollama(host: str) -> bool:
    """Try to start ollama serve in the background. Returns True if successful."""
    ollama_bin = shutil.which("ollama")
    if not ollama_bin:
        return False

    try:
        subprocess.Popen(
            [ollama_bin, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False

    # Wait up to 10 seconds for it to start
    for _ in range(20):
        time.sleep(0.5)
        if _is_ollama_running(host):
            return True
    return False


def _model_exists(host: str, model: str) -> bool:
    """Check if a model is already pulled."""
    try:
        resp = httpx.get(f"{host}/api/tags", timeout=5.0)
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            # Match by name (with or without :latest tag)
            for m in models:
                name = m.get("name", "")
                if name == model or name == f"{model}:latest" or name.split(":")[0] == model.split(":")[0] and (
                    ":" not in model or name == model
                ):
                    return True
    except httpx.HTTPError:
        pass
    return False


def _pull_model(host: str, model: str) -> None:
    """Pull a model with real-time progress via ollama CLI."""
    ollama_bin = shutil.which("ollama")
    if ollama_bin:
        # Use CLI for nice progress bar
        subprocess.run([ollama_bin, "pull", model], check=True)
    else:
        # Fallback: HTTP API (no progress display)
        print(f"  Pulling {model} (this may take a while)...")
        resp = httpx.post(
            f"{host}/api/pull",
            json={"name": model, "stream": False},
            timeout=3600.0,
        )
        resp.raise_for_status()


class OllamaBackend(LLMBackend):
    def __init__(self, model: str = "deepseek-r1:14b", host: str = "http://localhost:11434", max_output_tokens: int | None = None):
        self._model = model
        self._host = host.rstrip("/")
        self._ready = False
        self._max_output_tokens = max_output_tokens

    @property
    def provider_name(self) -> str:
        return "ollama"

    @property
    def model_name(self) -> str:
        return self._model

    def _ensure_ready(self) -> None:
        """Make sure Ollama is running and the model is available."""
        if self._ready:
            return

        # 1. Check if Ollama is running
        if not _is_ollama_running(self._host):
            if not shutil.which("ollama"):
                raise LLMError(
                    "Ollama is not installed.\n"
                    "Install it from: https://ollama.com/download\n"
                    "Or use a cloud provider: agentpub agent run --llm openai"
                )
            print("  Ollama is not running. Starting it...")
            if not _start_ollama(self._host):
                raise LLMError(
                    f"Could not start Ollama at {self._host}.\n"
                    "Try starting it manually: ollama serve"
                )
            print("  Ollama started.")

        # 2. Check if model exists
        if not _model_exists(self._host, self._model):
            size = _MODEL_SIZES.get(self._model, "unknown size")
            print(f"\n  Model '{self._model}' is not downloaded yet ({size}).")

            try:
                answer = input(f"  Download it now? [Y/n] ").strip().lower()
            except (EOFError, OSError):
                answer = "y"

            if answer in ("", "y", "yes"):
                _pull_model(self._host, self._model)
                print(f"  Model '{self._model}' ready.")
            else:
                raise LLMError(
                    f"Model '{self._model}' not available.\n"
                    f"Pull it manually: ollama pull {self._model}"
                )

        self._ready = True

    # Models that support Ollama's "think" parameter for toggling reasoning
    _THINKING_MODELS = frozenset({
        "deepseek-r1", "qwen3", "qwen3.5", "phi4-reasoning",
        "cogito", "magistral", "lfm2.5-thinking", "deepscaler",
    })

    def _is_thinking_model(self) -> bool:
        """Check if the current model supports thinking toggle."""
        model_lower = self._model.lower()
        return any(model_lower.startswith(prefix) for prefix in self._THINKING_MODELS)

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
        self._ensure_ready()

        payload: dict = {
            "model": self._model,
            "prompt": prompt,
            "stream": True,
            "options": {"temperature": temperature, "num_predict": self._effective_max_tokens(max_tokens)},
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"

        # Control thinking for models that support it
        if think is not None and self._is_thinking_model():
            payload["think"] = think
        elif think is None and json_mode and self._is_thinking_model():
            # Auto-disable thinking for JSON mode — thinking wastes tokens
            # and the output is mechanical (structured data extraction)
            payload["think"] = False

        # Thinking models that ignore think=false (e.g., phi4-reasoning) will
        # burn most of num_predict on <think> blocks. Compensate by tripling
        # the token budget so enough tokens remain for actual content.
        if self._is_thinking_model() and think is False:
            current_predict = payload["options"]["num_predict"]
            payload["options"]["num_predict"] = min(current_predict * 3, 65536)
            logger.info(
                "Thinking model with think=False: num_predict %d -> %d",
                current_predict, payload["options"]["num_predict"],
            )

        # Non-streaming path for short extraction calls with thinking disabled.
        # Streaming + thinking models causes stalls because the model pauses
        # between thinking token bursts, triggering read timeout detection.
        use_streaming = True
        if self._is_thinking_model() and payload.get("think") is False:
            use_streaming = False
            payload["stream"] = False
            logger.info("Using non-streaming mode (think=False on thinking model)")

        if not use_streaming:
            try:
                resp = httpx.post(
                    f"{self._host}/api/generate",
                    json=payload,
                    timeout=600.0,  # 10 min for non-streaming
                )
                resp.raise_for_status()
                data = resp.json()
                full_text = data.get("response", "")
                clean = _strip_thinking(full_text)
                return LLMResponse(
                    text=clean,
                    model=self._model,
                    provider="ollama",
                    usage={
                        "prompt_tokens": data.get("prompt_eval_count", 0),
                        "completion_tokens": data.get("eval_count", 0),
                    },
                )
            except httpx.ReadTimeout:
                raise LLMError("Ollama non-streaming call timed out after 600s")
            except httpx.ConnectError:
                raise LLMError(
                    f"Lost connection to Ollama at {self._host}.\n"
                    "Check if Ollama is still running: ollama serve"
                )

        # Timeout: 30 min for thinking models (they need time for <think> blocks),
        # 5 min for normal models
        stream_timeout = 1800.0 if self._is_thinking_model() else 300.0
        # Read timeout: thinking models can pause for long stretches between
        # token bursts during <think> phase. Use 5 min for thinking models.
        read_timeout = 300.0 if self._is_thinking_model() else 120.0

        try:
            # Stream the response so we can show live output
            full_text = ""
            final_data = {}
            stream_start = time.time()
            last_heartbeat = stream_start
            heartbeat_interval = 30.0  # log progress every 30s
            with httpx.stream(
                "POST",
                f"{self._host}/api/generate",
                json=payload,
                timeout=httpx.Timeout(stream_timeout, connect=30.0, read=read_timeout),
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    token = chunk.get("response", "")
                    if token:
                        # Detect thinking vs output for deepseek-r1 models
                        # deepseek-r1 wraps thinking in <think>...</think> tags
                        full_text += token
                        is_thinking = "<think>" in full_text and "</think>" not in full_text
                        self._emit_token(token, thinking=is_thinking)

                    # Heartbeat: log progress every 30s so the user knows it's alive
                    now = time.time()
                    if now - last_heartbeat >= heartbeat_interval:
                        last_heartbeat = now
                        elapsed = now - stream_start
                        clean = _strip_thinking(full_text)
                        word_count = len(clean.split()) if clean.strip() else 0
                        is_thinking_now = "<think>" in full_text and "</think>" not in full_text
                        logger.info(
                            "Generating... %ds elapsed, %d words%s",
                            int(elapsed), word_count,
                            " (thinking)" if is_thinking_now else "",
                        )
                        if self.on_heartbeat:
                            self.on_heartbeat(elapsed, word_count, is_thinking_now)

                        # Thinking stall: model streams thinking tokens but never
                        # produces content. Abort after 5 min of pure thinking.
                        if is_thinking_now and word_count == 0 and elapsed > 300:
                            logger.warning(
                                "Thinking stall: %ds of thinking with 0 content words — aborting",
                                int(elapsed),
                            )
                            raise LLMError(
                                f"Ollama stalled with no output after {int(elapsed)}s "
                                "(model stuck in thinking mode)"
                            )

                    if chunk.get("done"):
                        final_data = chunk
                        break

        except httpx.ReadTimeout:
            # Stall detection: no data from Ollama for read_timeout seconds.
            # If we got partial output, use it; if empty, raise so retry kicks in.
            logger.warning("Ollama stalled (no data for %.0fs) — aborting generation", read_timeout)
            if not full_text.strip():
                raise LLMError(f"Ollama stalled with no output after {read_timeout:.0f}s")
        except httpx.ConnectError:
            raise LLMError(
                f"Lost connection to Ollama at {self._host}.\n"
                "Check if Ollama is still running: ollama serve"
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise LLMError(
                    f"Model '{self._model}' not found.\n"
                    f"Pull it: ollama pull {self._model}"
                ) from e
            raise LLMError(f"Ollama returned HTTP {e.response.status_code}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama request failed: {e}") from e

        # Strip <think>...</think> reasoning tags so downstream parsers
        # (especially generate_json) only see the actual output.
        clean_text = _strip_thinking(full_text)

        # Estimate thinking tokens from <think> block length.
        # Ollama doesn't separate thinking from output in eval_count,
        # so we approximate by character ratio.
        eval_count = final_data.get("eval_count", 0)
        thinking_tokens = 0
        if "<think>" in full_text and len(full_text) > 0:
            thinking_chars = len(full_text) - len(clean_text)
            thinking_ratio = thinking_chars / len(full_text)
            thinking_tokens = int(eval_count * thinking_ratio)

        prompt_tokens = final_data.get("prompt_eval_count", 0)
        usage = {
            "input_tokens": prompt_tokens,
            "output_tokens": eval_count - thinking_tokens,
            "thinking_tokens": thinking_tokens,
            "total_tokens": prompt_tokens + eval_count,
        }
        self._track_usage(usage)
        return LLMResponse(
            text=clean_text,
            model=self._model,
            provider="ollama",
            usage=usage,
            finish_reason=final_data.get("done_reason", ""),
        )

    @property
    def supports_web_search(self) -> bool:
        """Ollama web search requires an OLLAMA_API_KEY from ollama.com."""
        import os
        return bool(os.environ.get("OLLAMA_API_KEY"))

    def search_web(self, query: str, *, limit: int = 10) -> list[dict]:
        """Search the web using Ollama's web search API.

        Requires OLLAMA_API_KEY from https://ollama.com/settings/keys.

        Returns list of dicts with keys:
            title, abstract, authors, year, url, doi, source, citation_count
        """
        import os

        api_key = os.environ.get("OLLAMA_API_KEY")
        if not api_key:
            return []

        # Step 1: Get raw web results from Ollama's search API
        try:
            resp = httpx.post(
                "https://ollama.com/api/web_search",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"query": f"academic papers {query}", "max_results": min(limit, 10)},
                timeout=15.0,
            )
            resp.raise_for_status()
            raw_results = resp.json().get("results", [])
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama web search failed: {e}") from e

        if not raw_results:
            return []

        # Step 2: Ask the local LLM to extract paper metadata from search results
        search_context = "\n\n".join(
            f"[{i+1}] {r.get('title', '')}\nURL: {r.get('url', '')}\n{r.get('content', '')[:500]}"
            for i, r in enumerate(raw_results)
        )

        system = (
            "You are a research librarian. Extract academic paper metadata from web search results. "
            "Only include results that are actual academic papers (from journals, arXiv, conferences, etc.)."
        )
        prompt = f"""Web search results for: {query}

{search_context}

From these results, extract academic papers and return JSON:
{{"papers": [
  {{"title": "paper title", "authors": ["Author Name"], "year": 2023,
   "abstract": "brief description", "url": "paper URL", "doi": "10.xxxx/..." or ""}}
]}}

Only include items that are real academic papers. Skip blog posts, news articles, etc."""

        result = self.generate_json(system, prompt, temperature=0.2, max_tokens=4000)
        papers = result.get("papers", [])
        if not isinstance(papers, list):
            return []

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
