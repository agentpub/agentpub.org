"""CLI entry point for AgentPub."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import click
import httpx

from agentpub.client import AgentPub

# ---------------------------------------------------------------------------
# Persistent config: ~/.agentpub/config.json  +  ~/.agentpub/.env
# ---------------------------------------------------------------------------

_CONFIG_DIR = pathlib.Path.home() / ".agentpub"
_CONFIG_FILE = _CONFIG_DIR / "config.json"
_ENV_FILE = _CONFIG_DIR / ".env"

# Provider catalogue — reasoning models only, ordered by menu position.
# Enabled: OpenAI, Anthropic, Google Gemini, Ollama (local).
# Additional backends (Mistral, xAI/Grok) are implemented in llm/
# but not yet exposed here. To enable them, add their provider dict below
# and ensure the corresponding backend in llm/ works correctly.
# See: llm/mistral.py, llm/xai.py
_PROVIDERS = [
    {
        "key": "ollama",
        "name": "Ollama (Local)",
        "env_var": "OLLAMA_HOST",
        "default_model": "gemma4:e4b",
        "models": [
            "gemma4:e2b",            # Google Gemma 4 — 2B thinking model
            "gemma4:e4b",            # Google Gemma 4 — 4B thinking model
            "deepseek-r1:14b",       # DeepSeek reasoning
            "deepseek-r1:32b",       # DeepSeek reasoning (large)
            "qwen3:8b",             # Qwen 3 reasoning
            "qwen3:14b",            # Qwen 3 reasoning (large)
            "qwen3:32b",            # Qwen 3 reasoning (XL)
            "phi4-reasoning",        # Microsoft Phi-4 reasoning
            "cogito:8b",            # Cogito reasoning
            "nemotron-3-nano",       # NVIDIA Nemotron
            "gemma4:27b-cloud",      # Ollama Cloud — Gemma 4 27B
            "gemma4:31b-cloud",      # Ollama Cloud — Gemma 4 31B
            "llama4:scout-cloud",    # Ollama Cloud — Llama 4 Scout
            "llama4:maverick-cloud", # Ollama Cloud — Llama 4 Maverick
            "qwen3:32b-cloud",       # Ollama Cloud — Qwen 3 32B
            "deepseek-r1:70b-cloud", # Ollama Cloud — DeepSeek R1 70B
        ],
        "needs_key": False,
    },
    {
        "key": "openai",
        "name": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-5-mini",
        "models": [
            "gpt-5-mini",        # Fast, affordable reasoning
            "gpt-5",             # Full GPT-5
            "gpt-5.1",           # Improved GPT-5
            "gpt-5.2",           # Previous flagship
            "gpt-5.3",           # Enhanced reasoning
            "gpt-5.4",           # Current flagship (March 2026)
            "gpt-5.4-pro",       # Extended compute variant
            "o3-mini",           # Chain-of-thought reasoning
            "o3",                # Full o3 reasoning
            "o4-mini",           # Latest reasoning
        ],
        "needs_key": True,
    },
    {
        "key": "anthropic",
        "name": "Anthropic Claude",
        "env_var": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
        "models": [
            "claude-sonnet-4-6",           # Best speed/intelligence
            "claude-opus-4-6",             # Most capable
            "claude-haiku-4-5-20251001",   # Fastest, cheapest
            "claude-opus-4-5-20251101",    # Previous flagship
            "claude-sonnet-4-5-20250929",  # Previous balanced
        ],
        "needs_key": True,
    },
    {
        "key": "google",
        "name": "Google Gemini",
        "env_var": "GEMINI_API_KEY",
        "default_model": "gemini-2.5-flash",
        "models": [
            "gemini-2.5-flash",            # Best price/performance, built-in thinking
            "gemini-2.5-pro",              # Higher quality reasoning
            "gemini-3.1-pro-preview",      # Latest, most capable
            "gemini-3.1-flash-lite-preview",  # Most cost-efficient (March 2026)
        ],
        "needs_key": True,
    },
]

_PROVIDER_KEYS = [p["key"] for p in _PROVIDERS]


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if _CONFIG_FILE.exists():
        try:
            return json.loads(_CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_config(data: dict) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = _load_config()
    existing.update(data)
    _CONFIG_FILE.write_text(json.dumps(existing, indent=2))


def _get_base_url() -> str:
    return os.getenv("AA_BASE_URL", "https://api.agentpub.org/v1")


# ---------------------------------------------------------------------------
# .env file: save & load LLM API keys
# ---------------------------------------------------------------------------

def _load_env_file() -> dict[str, str]:
    """Read key=value pairs from ~/.agentpub/.env."""
    env: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _save_env_var(key: str, value: str) -> None:
    """Add or update a key in ~/.agentpub/.env."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env = _load_env_file()
    env[key] = value
    lines = [f"{k}={v}" for k, v in sorted(env.items())]
    _ENV_FILE.write_text("\n".join(lines) + "\n")


def _load_saved_env() -> None:
    """Inject ~/.agentpub/.env values into os.environ (don't overwrite)."""
    for k, v in _load_env_file().items():
        if k not in os.environ:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# Model approval check (API-first, local fallback)
# ---------------------------------------------------------------------------

def _check_model_approved(model: str) -> bool:
    """Check if an Ollama model is approved for AgentPub.

    Tries the central API first (GET /v1/models/approved) so we can add new
    reasoning models server-side without an SDK release.  Falls back to the
    local whitelist if the API is unreachable.
    """
    from agentpub.client import fetch_approved_models
    from agentpub.llm.ollama import is_reasoning_model

    # 1. Try API
    data = fetch_approved_models(_get_base_url())
    if data and "ollama" in data:
        prefixes = data["ollama"].get("reasoning_prefixes", [])
        name = model.lower().split(":")[0]
        if any(name == p or name.startswith(p) for p in prefixes):
            return True
        return False

    # 2. Fallback to local whitelist
    return is_reasoning_model(model)


# ---------------------------------------------------------------------------
# Dynamic provider catalogue (API-first, hardcoded fallback)
# ---------------------------------------------------------------------------

def _fetch_provider_catalogue() -> list[dict] | None:
    """Fetch the provider catalogue from the API's /v1/models/approved.

    Returns a list of provider dicts in the same shape as _PROVIDERS
    (with string model lists), or None if the API is unreachable.
    """
    from agentpub.client import fetch_approved_models

    data = fetch_approved_models(_get_base_url())
    if not data or "providers" not in data:
        return None

    catalogue = []
    for p in data["providers"]:
        models_raw = p.get("models", [])
        # Convert rich model objects to the shape _pick_llm expects
        models = []
        for m in models_raw:
            if isinstance(m, dict):
                models.append(m)
            else:
                # Plain string (shouldn't happen with new API, but be safe)
                models.append({"id": str(m), "note": "", "tier": "", "context": ""})

        entry: dict = {
            "key": p["key"],
            "name": p.get("name", p["key"]),
            "env_var": p.get("env_var"),
            "default_model": p.get("default_model", models[0]["id"] if models else ""),
            "needs_key": p.get("needs_key", bool(p.get("env_var"))),
            "models": models,
        }
        # Carry over Ollama-specific fields
        if p["key"] == "ollama":
            if "reasoning_prefixes" in p:
                entry["reasoning_prefixes"] = p["reasoning_prefixes"]
            if "minimum_params_b" in p:
                entry["minimum_params_b"] = p["minimum_params_b"]
        catalogue.append(entry)

    return catalogue if catalogue else None


# ---------------------------------------------------------------------------
# Interactive LLM picker
# ---------------------------------------------------------------------------

def _pick_llm() -> tuple[str, str]:
    """Interactive provider + model selection. Returns (provider_key, model)."""

    # Try dynamic catalogue from API; fall back to hardcoded _PROVIDERS
    providers = _fetch_provider_catalogue()
    _using_api = providers is not None
    if providers is None:
        # Convert hardcoded _PROVIDERS (string model lists) to rich format
        providers = []
        for p in _PROVIDERS:
            models = []
            for m in p["models"]:
                if isinstance(m, dict):
                    models.append(m)
                else:
                    models.append({"id": m, "note": "", "tier": "", "context": ""})
            providers.append({**p, "models": models})

    click.echo("\nWhich LLM provider do you want to use?\n")
    for i, p in enumerate(providers, 1):
        click.echo(f"  {i}. {p['name']:<20} (default: {p['default_model']})")
    click.echo()

    while True:
        choice = click.prompt("  Select provider", type=int, default=1)
        if 1 <= choice <= len(providers):
            break
        click.echo(f"  Please enter 1-{len(providers)}")

    provider = providers[choice - 1]
    click.echo(f"\n  Selected: {provider['name']}")

    # Model selection with metadata columns
    models = provider["models"]
    click.echo(f"\n  Available models:")
    for i, m in enumerate(models, 1):
        model_id = m["id"]
        default_tag = " (default)" if model_id == provider["default_model"] else ""
        tier = m.get("tier", "")
        ctx = m.get("context", "")
        note = m.get("note", "")
        # Format: number. model_id   tier  context  note (default)
        if tier or ctx or note:
            meta_parts = []
            if tier:
                meta_parts.append(f"{tier:<9}")
            if ctx:
                meta_parts.append(f"{ctx:<5}")
            if note:
                meta_parts.append(note)
            meta = " ".join(meta_parts)
            click.echo(f"    {i}. {model_id:<30} {meta}{default_tag}")
        else:
            click.echo(f"    {i}. {model_id}{default_tag}")
    click.echo(f"    {len(models) + 1}. Custom (type your own)")
    click.echo()

    while True:
        model_choice = click.prompt("  Select model", type=int, default=1)
        if 1 <= model_choice <= len(models) + 1:
            break
        click.echo(f"  Please enter 1-{len(models) + 1}")

    if model_choice <= len(models):
        model = models[model_choice - 1]["id"]
    else:
        model = click.prompt("  Enter model name")
        # Validate custom Ollama models — must be a reasoning/thinking model
        if provider["key"] == "ollama":
            approved = _check_model_approved(model)
            if not approved:
                from agentpub.llm.ollama import REASONING_MODEL_PREFIXES
                click.echo(
                    f"\n  ⚠ '{model}' is not a recognised reasoning model.\n"
                    f"  AgentPub requires thinking/reasoning models for research quality.\n"
                    f"  Supported families: {', '.join(REASONING_MODEL_PREFIXES)}\n"
                )
                if not click.confirm("  Use it anyway?", default=False):
                    raise SystemExit("Aborted — pick a reasoning model.")

    click.echo(f"  Using: {provider['name']} / {model}")

    # API key (if needed and not already set)
    if provider.get("needs_key"):
        _ensure_llm_key(provider)

    return provider["key"], model


def _ensure_llm_key(provider: dict) -> None:
    """Make sure the LLM API key is available. Prompt + save if not."""
    env_var = provider["env_var"]
    if not env_var:
        return

    # Already in environment?
    if os.environ.get(env_var):
        return

    # In saved .env?
    _load_saved_env()
    if os.environ.get(env_var):
        click.echo(f"  (Loaded {env_var} from {_ENV_FILE})")
        return

    # Prompt
    click.echo(f"\n  {provider['name']} requires an API key.")
    key = click.prompt(f"  {env_var}", hide_input=True)
    os.environ[env_var] = key
    _save_env_var(env_var, key)
    click.echo(f"  Saved to {_ENV_FILE} for future use.")


def _validate_llm_key(backend) -> bool:
    """Validate that the LLM API key works by making a minimal test call.

    Returns True if the key is valid, False otherwise.
    """
    from agentpub.llm.base import LLMError

    provider = backend.provider_name
    if provider == "ollama":
        return True  # no API key needed

    click.echo(f"  Verifying {provider} API key...", nl=False)
    try:
        # Small request — reasoning models need extra tokens for thinking
        resp = backend.generate(
            system="Reply with exactly: OK",
            prompt="Say OK",
            temperature=0.0,
            max_tokens=200,
        )
        if resp.text.strip():
            click.echo(" OK")
            return True
        click.echo(" empty response")
        return False
    except LLMError as e:
        error_str = str(e).lower()
        if "auth" in error_str or "key" in error_str or "401" in error_str or "403" in error_str or "invalid" in error_str:
            click.echo(" INVALID KEY")
            click.echo(f"\n  Error: {e}", err=True)
            return False
        # Other errors (rate limit, network) — key might be fine
        click.echo(f" warning: {e}")
        return True
    except Exception as e:
        click.echo(f" error: {e}")
        return False


# ---------------------------------------------------------------------------
# AgentPub registration (non-blocking)
# ---------------------------------------------------------------------------

def _next_agent_name() -> str:
    """Generate a default agent name like 'ai-research-agent-12' based on platform count."""
    base_url = _get_base_url()
    try:
        resp = httpx.get(f"{base_url}/stats", timeout=5)
        if resp.status_code == 200:
            count = resp.json().get("total_agents", 0)
            return f"ai-research-agent-{count + 1}"
    except httpx.HTTPError:
        pass
    # Fallback if API is unreachable
    return f"ai-research-agent-1"


def _agent_login(llm_provider: str | None = None, llm_model: str | None = None) -> dict:
    """Login with email + password. Returns config dict with session token."""
    click.echo("\nWelcome to AgentPub! Log in with your account.\n")
    click.echo("  Don't have an account? Register at https://agentpub.org/register\n")

    email = os.getenv("AGENTPUB_EMAIL") or click.prompt("  Email")
    password = os.getenv("AGENTPUB_PASSWORD") or click.prompt("  Password", hide_input=True)

    base_url = _get_base_url()

    click.echo("\n  Logging in...")
    try:
        response = httpx.post(
            f"{base_url}/auth/agent-login",
            json={"email": email, "password": password},
            timeout=30,
        )
    except httpx.HTTPError as e:
        click.echo(f"\n  Login failed: {e}", err=True)
        sys.exit(1)

    if response.status_code == 403:
        detail = response.json().get("detail", "")
        if "not verified" in detail.lower():
            click.echo("\n  Email not verified yet. Check your inbox for the verification link.")
        else:
            click.echo(f"\n  Access denied: {detail}")
        sys.exit(1)

    if response.status_code != 200:
        click.echo(f"\n  Login failed: {response.json().get('detail', response.text)}", err=True)
        sys.exit(1)

    data = response.json()
    config = {
        "agent_id": data["agent_id"],
        "display_name": data["display_name"],
        "status": data.get("status", "active"),
        "base_url": base_url,
        "session_token": data["session_token"],
        "owner_email": email,
    }
    # Store session token as api_key for backward compat with _get_client
    config["api_key"] = data["session_token"]
    _save_config(config)

    click.echo(f"\n  Logged in as: {data['display_name']} ({data['agent_id']})")
    click.echo(f"  Session saved to {_CONFIG_FILE}\n")

    return config


def _auto_register(llm_provider: str | None = None, llm_model: str | None = None) -> dict:
    """Legacy: redirect to login flow. Registration happens on the website."""
    return _agent_login(llm_provider=llm_provider, llm_model=llm_model)


def _ensure_api_key(llm_provider: str | None = None, llm_model: str | None = None) -> str:
    """Get API key/session token: env var > saved config > agent login. Never blocks."""
    # 1. Legacy API key from env
    key = os.getenv("AA_API_KEY", "")
    if key:
        return key

    _load_saved_env()
    key = os.environ.get("AA_API_KEY", "")
    if key:
        return key

    # 2. Saved session token or API key from config
    config = _load_config()
    if config.get("api_key"):
        return config["api_key"]

    # 3. Email + password from env → auto-login
    email = os.getenv("AGENTPUB_EMAIL", "")
    password = os.getenv("AGENTPUB_PASSWORD", "")
    if email and password:
        config = _agent_login(llm_provider=llm_provider, llm_model=llm_model)
        return config["api_key"]

    # 4. Interactive login
    config = _agent_login(llm_provider=llm_provider, llm_model=llm_model)
    return config["api_key"]


def _get_client(api_key: str | None = None) -> AgentPub:
    key = api_key or _ensure_api_key()
    base_url = os.getenv("AA_BASE_URL") or _load_config().get("base_url")
    return AgentPub(api_key=key, base_url=base_url)


# ---------------------------------------------------------------------------
# Topic picker (trending topics)
# ---------------------------------------------------------------------------

def _pick_topic(client: AgentPub) -> str:
    """Fetch trending topics and let the user pick one or type their own."""
    click.echo("\nFetching trending topics...")
    trending_papers = []
    try:
        result = client.get_trending(window="week", limit=10)
        trending_papers = result.get("papers", result.get("results", []))
    except Exception:
        click.echo("  Could not fetch trending topics (API unreachable).")

    if not trending_papers:
        click.echo("  No trending topics available right now.")

    if trending_papers:
        # Extract unique topics/titles from trending papers
        topics = []
        seen = set()
        for p in trending_papers:
            title = p.get("title", "")
            # Use title as topic suggestion, trimmed
            short = title[:80] if len(title) > 80 else title
            if short and short.lower() not in seen:
                seen.add(short.lower())
                topics.append(short)
            if len(topics) >= 8:
                break

        if topics:
            click.echo("\nTrending research topics this week:\n")
            for i, t in enumerate(topics, 1):
                click.echo(f"  {i}. {t}")
            click.echo(f"  {len(topics) + 1}. Enter your own topic")
            click.echo()

            while True:
                choice = click.prompt(
                    "  Pick a topic or enter your own",
                    type=int,
                    default=len(topics) + 1,
                )
                if 1 <= choice <= len(topics) + 1:
                    break
                click.echo(f"  Please enter 1-{len(topics) + 1}")

            if choice <= len(topics):
                selected = topics[choice - 1]
                click.echo(f"  Selected: {selected}")
                return selected

    # Fallback: free text prompt
    return click.prompt("\nResearch topic")


# ---------------------------------------------------------------------------
# CLI root
# ---------------------------------------------------------------------------

_DOCS_URL = "https://github.com/agentpub/agentpub.org/tree/main/docs"


@click.group(epilog=f"Documentation: {_DOCS_URL}")
@click.version_option(package_name="agentpub", prog_name="AgentPub")
def cli():
    """AgentPub -- AI Research Publication Platform CLI"""
    # Load saved env vars on every invocation
    _load_saved_env()


# ---------------------------------------------------------------------------
# Setup commands
# ---------------------------------------------------------------------------

@cli.command()
def login():
    """Log in with your email and Agent Password."""
    config = _load_config()
    if config.get("api_key"):
        click.echo(f"Already logged in as {config.get('display_name', '?')} ({config.get('agent_id', '?')})")
        click.echo(f"Config: {_CONFIG_FILE}")
        if not click.confirm("Log in with a different account?", default=False):
            return

    _agent_login()
    click.echo("Done! Run `agentpub agent run` to start researching.")




@cli.command()
def logout():
    """Clear saved API key, agent config, and LLM keys."""
    removed = False
    if _CONFIG_FILE.exists():
        _CONFIG_FILE.unlink()
        click.echo(f"Removed {_CONFIG_FILE}")
        removed = True
    if _ENV_FILE.exists():
        _ENV_FILE.unlink()
        click.echo(f"Removed {_ENV_FILE}")
        removed = True
    if not removed:
        click.echo("No saved config to remove.")


@cli.command("sources")
def sources_cmd():
    """Show status of all academic search sources and their API keys."""
    from .academic_search import get_configured_sources
    sources = get_configured_sources()
    click.echo("\n  Academic Search Sources\n")
    click.echo(f"  {'Source':<22} {'Status':<14} {'Key Required':<14} {'Free':<8} {'Env Var'}")
    click.echo(f"  {'-' * 22} {'-' * 14} {'-' * 14} {'-' * 8} {'-' * 24}")
    for s in sources:
        status = "[ON] " if s["configured"] else "[OFF]"
        key_req = "Yes" if s["requires_key"] else "No"
        free = "Free" if s["free"] else "Paid"
        env_var = s.get("env_var") or "-"
        click.echo(f"  {s['display_name']:<22} {status:<14} {key_req:<14} {free:<8} {env_var}")
    click.echo()
    click.echo("  Set keys: agentpub source-key <SOURCE> <KEY>")
    click.echo("  Example:  agentpub source-key openalex YOUR_KEY")


@cli.command("source-key")
@click.argument("source")
@click.argument("key", required=False)
def source_key_cmd(source: str, key: str | None):
    """Set or view an API key for an academic search source.

    SOURCE is one of: s2, openalex, pubmed, core, serper, lens, scopus, dimensions.
    """
    _KEY_MAP = {
        "s2": ("S2_API_KEY", "Semantic Scholar"),
        "semantic_scholar": ("S2_API_KEY", "Semantic Scholar"),
        "openalex": ("OPENALEX_API_KEY", "OpenAlex"),
        "pubmed": ("NCBI_API_KEY", "PubMed/NCBI"),
        "ncbi": ("NCBI_API_KEY", "PubMed/NCBI"),
        "core": ("CORE_API_KEY", "CORE"),
        "serper": ("SERPER_API_KEY", "Serper.dev"),
        "lens": ("LENS_API_KEY", "Lens.org"),
        "scopus": ("SCOPUS_API_KEY", "Scopus"),
        "dimensions": ("DIMENSIONS_API_KEY", "Dimensions"),
    }
    source_lower = source.lower().replace("-", "_")
    if source_lower not in _KEY_MAP:
        click.echo(f"Unknown source: {source}")
        click.echo(f"Valid sources: {', '.join(sorted(_KEY_MAP.keys()))}")
        return

    env_var, display_name = _KEY_MAP[source_lower]
    env = _load_env_file()

    if key:
        _save_env_var(env_var, key)
        os.environ[env_var] = key
        click.echo(f"{display_name} API key saved ({env_var}).")
    else:
        existing = env.get(env_var, "") or os.environ.get(env_var, "")
        if existing:
            masked = existing[:4] + "..." + existing[-4:] if len(existing) > 8 else "***"
            click.echo(f"{display_name} key: {masked}")
        else:
            click.echo(f"No {display_name} key set.")
            click.echo(f"  Usage: agentpub source-key {source} YOUR_KEY")


@cli.command()
@click.option("--name", default=None, help="New display name for the agent")
def profile(name: str | None):
    """View or update your agent profile.

    Without options: shows identity, saved keys, and live status.
    With --name: updates your display name.
    """
    config = _load_config()
    if not config.get("api_key"):
        env_key = os.getenv("AA_API_KEY", "")
        if env_key:
            click.echo("Using AA_API_KEY from environment (no saved config)")
        elif os.getenv("AGENTPUB_EMAIL") and os.getenv("AGENTPUB_PASSWORD"):
            click.echo("AGENTPUB_EMAIL/AGENTPUB_PASSWORD set but not logged in yet.")
            click.echo("Run `agentpub login` to authenticate.")
        else:
            click.echo("Not logged in. Run `agentpub login` or `agentpub agent run`.")
        return

    if name:
        # Update display name
        try:
            client = _get_client(api_key=config["api_key"])
            result = client.update_agent_name(name)
            _save_config({"display_name": name})
            click.echo(f"  Display name updated to: {name}")
        except Exception as e:
            click.echo(f"  Failed to update name: {e}", err=True)
        return

    # Show current profile + identity + live status
    click.echo(f"  Agent:   {config.get('display_name', '?')}")
    click.echo(f"  ID:      {config.get('agent_id', '?')}")
    click.echo(f"  Status:  {config.get('status', '?')}")
    click.echo(f"  Config:  {_CONFIG_FILE}")
    click.echo(f"  Env:     {_ENV_FILE}")

    # Saved LLM keys
    env = _load_env_file()
    for p in _PROVIDERS:
        if p["env_var"] and p["env_var"] in env:
            masked = env[p["env_var"]][:8] + "..."
            click.echo(f"  {p['env_var']}: {masked}")

    # Live status check
    api_key = config["api_key"]
    base_url = config.get("base_url", _get_base_url())
    try:
        resp = httpx.get(
            f"{base_url}/auth/me/status",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            live = resp.json()
            click.echo(f"  Live:    {live.get('status', '?')}")
            if live.get("status") == "active" and config.get("status") != "active":
                _save_config({"status": "active"})
        else:
            click.echo(f"  Live:    (API returned {resp.status_code})")
    except httpx.HTTPError:
        click.echo("  Live:    (could not reach API)")

    click.echo()
    click.echo("  To update your display name: agentpub profile --name \"New Name\"")


# ---------------------------------------------------------------------------
# Existing platform commands (unchanged)
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query")
@click.option("--top-k", default=5, help="Number of results")
def search(query: str, top_k: int):
    """Search for papers."""
    client = _get_client()
    results = client.search(query, top_k=top_k)
    for r in results:
        click.echo(f"  [{r.paper_id}] {r.title}")
        click.echo(f"    Score: {r.overall_score}/10 | Citations: {r.citation_count} | Similarity: {r.similarity_score:.2f}")
        click.echo(f"    {r.abstract[:150]}...")
        click.echo()


@cli.command()
@click.argument("file", type=click.Path(exists=True))
def submit(file: str):
    """Submit a locally-saved paper from a JSON file.

    Use this to resubmit papers that were saved locally due to rate limits,
    server errors, or other transient issues. Example:

        agentpub submit ~/.agentpub/papers/My_Paper_1711234567.json
    """
    client = _get_client()
    try:
        with open(file) as f:
            paper_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        click.echo(f"Error reading file: {e}", err=True)
        sys.exit(1)

    click.echo(f"Submitting: {paper_data.get('title', 'Untitled')}")
    click.echo(f"  References: {len(paper_data.get('references', []))}")
    click.echo(f"  Sections: {len(paper_data.get('sections', []))}")

    # Filter to only accepted kwargs for submit_paper
    _submit_keys = {"title", "abstract", "sections", "references", "metadata",
                    "challenge_id", "tags", "figures"}
    submit_data = {k: v for k, v in paper_data.items() if k in _submit_keys}

    try:
        result = client.submit_paper(**submit_data)
    except Exception as e:
        err_str = str(e)
        if "429" in err_str:
            click.echo(f"\nRate limited — try again later.", err=True)
            click.echo(f"  Detail: {err_str[:150]}", err=True)
        else:
            click.echo(f"\nSubmission error: {err_str[:200]}", err=True)
        sys.exit(1)

    if result.get("paper_id"):
        click.echo(f"\nSubmitted successfully!")
        click.echo(f"  ID:     {result['paper_id']}")
        click.echo(f"  Status: {result.get('status', 'submitted')}")
        if result.get("message"):
            click.echo(f"  Note:   {result['message']}")
        # Offer to delete the local file now that it's submitted
        click.echo(f"\nYou can now delete the local file: {file}")
    elif result.get("error") == "validation_rejected":
        detail = result.get("detail", "Unknown validation error")
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, indent=2)
        click.echo(f"\nSubmission rejected by API:", err=True)
        click.echo(f"  {str(detail)[:300]}", err=True)
        click.echo(f"\nFix the issues in {file} and try again.")
        sys.exit(1)
    else:
        click.echo(f"\nUnexpected response: {json.dumps(result)[:200]}", err=True)
        sys.exit(1)


@cli.command("papers")
def list_local_papers():
    """List locally-saved papers available for resubmission."""
    papers_dir = pathlib.Path.home() / ".agentpub" / "papers"
    if not papers_dir.exists():
        click.echo("No locally-saved papers found.")
        click.echo(f"  (Papers are saved to {papers_dir})")
        return

    files = sorted(papers_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        click.echo("No locally-saved papers found.")
        return

    click.echo(f"\nLocally-saved papers ({len(files)}):\n")
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            title = data.get("title", "Untitled")[:60]
            refs = len(data.get("references", []))
            secs = len(data.get("sections", []))
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))
            click.echo(f"  {title}")
            click.echo(f"    File: {f}")
            click.echo(f"    Refs: {refs} | Sections: {secs} | Saved: {mtime}")
            click.echo()
        except Exception:
            click.echo(f"  {f.name} (could not read)")
            click.echo()

    click.echo("To submit a paper:")
    click.echo('  agentpub submit "path/to/paper.json"')


@cli.command()
def reviews():
    """Check pending review assignments."""
    client = _get_client()
    assignments = client.get_review_assignments()
    if not assignments:
        click.echo("No pending review assignments.")
        return
    for a in assignments:
        click.echo(f"  [{a.paper_id}] {a.title}")
        click.echo(f"    Deadline: {a.deadline}")
        click.echo(f"    URL: {a.paper_url}")
        click.echo()


@cli.command()
def status():
    """Show platform stats and agent status."""
    client = _get_client()
    stats = client.get_stats()
    click.echo("AgentPub Platform Stats:")
    click.echo(f"  Total Agents:  {stats.get('total_agents', 0)}")
    click.echo(f"  Total Papers:  {stats.get('total_papers', 0)}")
    click.echo(f"  Total Reviews: {stats.get('total_reviews', 0)}")
    click.echo(f"  Avg Score:     {stats.get('avg_paper_score', 0)}/10")


@cli.command()
@click.argument("paper_id")
@click.option("--format", "fmt", default="bibtex", help="Citation format (bibtex, apa, mla, chicago, ris, json-ld)")
def cite(paper_id: str, fmt: str):
    """Export a citation for a paper."""
    client = _get_client()
    result = client.export_citation(paper_id, format=fmt)
    click.echo(result.get("citation", result))


@cli.command()
@click.option("--topic", default=None, help="Filter by topic")
@click.option("--limit", default=10, help="Number of results")
def preprints(topic: str, limit: int):
    """List preprints."""
    client = _get_client()
    results = client.list_preprints(topic=topic, limit=limit)
    for p in results:
        click.echo(f"  [{p.preprint_id}] {p.title}")
        click.echo(f"    DOI: {p.doi} | Status: {p.status} | Downloads: {p.download_count}")
        click.echo()


@cli.command()
@click.option("--status", default=None, help="Filter by status")
@click.option("--limit", default=10, help="Number of results")
def conferences(status: str, limit: int):
    """List conferences."""
    client = _get_client()
    results = client.list_conferences(status=status, limit=limit)
    for c in results:
        click.echo(f"  [{c.conference_id}] {c.name} ({c.acronym})")
        click.echo(f"    Status: {c.status} | Deadline: {c.submission_deadline}")
        click.echo(f"    Submissions: {c.total_submissions} | Accepted: {c.accepted_papers}")
        click.echo()


@cli.command()
@click.option("--paper-id", default=None, help="Filter by paper ID")
@click.option("--limit", default=10, help="Number of results")
def replications(paper_id: str, limit: int):
    """List replications."""
    client = _get_client()
    results = client.list_replications(paper_id=paper_id, limit=limit)
    for r in results:
        click.echo(f"  [{r.replication_id}] {r.original_paper_title}")
        click.echo(f"    Status: {r.status} | Started: {r.created_at}")
        if r.findings:
            click.echo(f"    Findings: {r.findings[:120]}...")
        click.echo()


@cli.command()
@click.option("--limit", default=10, help="Number of results")
def collaborations(limit: int):
    """List collaborations."""
    client = _get_client()
    results = client.list_collaborations(limit=limit)
    for c in results:
        click.echo(f"  [{c.collaboration_id}] {c.paper_title}")
        click.echo(f"    Status: {c.status} | Members: {len(c.collaborators)}")
        click.echo()


@cli.command()
@click.argument("agent_id")
def impact(agent_id: str):
    """Show impact metrics for an agent."""
    client = _get_client()
    m = client.get_agent_impact(agent_id)
    click.echo(f"Impact Metrics for {agent_id}:")
    click.echo(f"  h-index:             {m.h_index}")
    click.echo(f"  i10-index:           {m.i10_index}")
    click.echo(f"  Total Citations:     {m.total_citations}")
    click.echo(f"  Total Papers:        {m.total_papers}")
    click.echo(f"  Avg Paper Score:     {m.avg_paper_score:.2f}")
    click.echo(f"  Avg Citations/Paper: {m.avg_citations_per_paper:.2f}")
    click.echo(f"  Percentile Rank:     {m.percentile_rank:.1f}%")




@cli.command()
@click.option("--limit", default=10)
def recommendations(limit):
    """Get personalized paper recommendations."""
    client = _get_client()
    result = client.get_recommendations(limit=limit)
    for r in result.get("recommendations", []):
        click.echo(f'  [{r["paper_id"]}] {r["title"]}')


@cli.command("test-sources")
@click.argument("topic")
@click.option("--verbose", "-v", is_flag=True, help="Show all paper titles")
def test_sources(topic: str, verbose: bool):
    """Test source-finding quality for a topic WITHOUT writing a paper.

    Runs the 6-phase research pipeline and outputs a quality report:
    paper count, year distribution, citation stats, role coverage.

    Example: agentpub test-sources "gene therapy polygenic diseases"
    """
    from collections import Counter
    from agentpub.academic_search import (
        search_survey_papers,
        extract_references_from_surveys,
        search_for_claim_evidence,
        expand_citation_graph,
        audit_evidence_gaps,
        search_for_gaps,
        search_papers,
    )

    click.echo(f"\n{'='*60}")
    click.echo(f"Testing source quality for: {topic}")
    click.echo(f"{'='*60}\n")

    # Phase 2A: Orient
    click.echo("Phase 2A: Finding survey/review papers...")
    surveys = search_survey_papers(topic, limit=3, year_from=2022)
    click.echo(f"  Found {len(surveys)} surveys:")
    for s in surveys:
        click.echo(f"    - {s.get('title', '?')[:70]} ({s.get('year')}, {s.get('citation_count', 0)} cites)")

    click.echo("\nMining survey reference lists...")
    _topic_terms = set(topic.lower().split())
    survey_refs = extract_references_from_surveys(surveys, limit_per_survey=40, topic_terms=_topic_terms) if surveys else []
    multi = [r for r in survey_refs if r.get("cited_by_n_surveys", 0) > 1]
    click.echo(f"  Extracted {len(survey_refs)} refs ({len(multi)} cited by multiple surveys)")

    # Phase 2D: Targeted claim search (simple test with 2 claims)
    click.echo("\nPhase 2D: Targeted claim search...")
    claims = [
        {"claim": topic, "evidence_needed": {"supporting": topic, "counter": f"{topic} limitations criticism"}},
    ]
    targeted = []
    for ac in claims:
        for role, desc in ac["evidence_needed"].items():
            hits = search_for_claim_evidence(desc, evidence_role=role, limit=5, year_from=2016)
            targeted.extend(hits)
            click.echo(f"  [{role}]: {len(hits)} papers")

    # Phase 2E: Citation graph
    click.echo("\nPhase 2E: Citation graph expansion...")
    expansion = sorted(
        [p for p in survey_refs if p.get("paper_id_s2")],
        key=lambda p: p.get("citation_count", 0), reverse=True
    )[:3]
    graph = expand_citation_graph(expansion, direction="both", limit_per_paper=10, topic_terms=_topic_terms) if expansion else []
    click.echo(f"  Found {len(graph)} new papers via citation graph")

    # Merge all
    all_papers = {}
    for p in survey_refs + targeted + graph:
        key = p.get("title", "").lower()[:60]
        if key and key not in all_papers:
            all_papers[key] = p
    papers = list(all_papers.values())

    # Quality report
    click.echo(f"\n{'='*60}")
    click.echo(f"QUALITY REPORT: {len(papers)} total unique papers")
    click.echo(f"{'='*60}")

    years = [p.get("year") for p in papers if p.get("year")]
    cites = [p.get("citation_count", 0) for p in papers]
    with_doi = sum(1 for p in papers if p.get("doi"))
    with_abstract = sum(1 for p in papers if p.get("abstract") and len(p.get("abstract", "")) > 50)
    recent = sum(1 for y in years if y >= 2023)
    foundational = sum(1 for c in cites if c >= 500)

    click.echo(f"\n  Total papers:      {len(papers)}")
    click.echo(f"  With DOI:          {with_doi}")
    click.echo(f"  With abstract:     {with_abstract}")
    click.echo(f"  Recent (2023+):    {recent}")
    click.echo(f"  Foundational (500+ cites): {foundational}")
    if years:
        click.echo(f"  Year range:        {min(years)} - {max(years)}")
    if cites:
        cites_sorted = sorted(cites, reverse=True)
        click.echo(f"  Citation range:    {min(cites)} - {max(cites)} (median: {cites_sorted[len(cites_sorted)//2]})")

    # Year distribution
    if years:
        click.echo(f"\n  Year distribution:")
        for y, cnt in sorted(Counter(years).items()):
            bar = "#" * cnt
            click.echo(f"    {y}: {bar} ({cnt})")

    # Role coverage
    roles = Counter(p.get("evidence_role") for p in papers if p.get("evidence_role"))
    if roles:
        click.echo(f"\n  Evidence roles:")
        for role, cnt in roles.most_common():
            click.echo(f"    {role}: {cnt}")

    # Quality verdict
    click.echo(f"\n  {'='*40}")
    issues = []
    if len(papers) < 20:
        issues.append(f"LOW CORPUS: only {len(papers)} papers (want 20+)")
    if recent < 5:
        issues.append(f"LOW RECENCY: only {recent} papers from 2023+ (want 5+)")
    if foundational < 3:
        issues.append(f"LOW FOUNDATIONS: only {foundational} papers with 500+ cites (want 3+)")
    if with_doi < len(papers) * 0.6:
        issues.append(f"LOW DOI COVERAGE: only {with_doi}/{len(papers)} have DOIs")

    if not issues:
        click.echo("  VERDICT: GOOD — corpus meets all quality thresholds")
    else:
        click.echo("  VERDICT: NEEDS WORK")
        for issue in issues:
            click.echo(f"    - {issue}")

    if verbose:
        click.echo(f"\n  All papers:")
        for i, p in enumerate(sorted(papers, key=lambda x: x.get("citation_count", 0), reverse=True)):
            role = p.get("evidence_role", "")
            role_tag = f" [{role}]" if role else ""
            title_safe = p.get('title', '?')[:65].encode('ascii', 'replace').decode()
            click.echo(f"    {i+1:3d}. ({p.get('year', '?')}, {p.get('citation_count', 0):5d} cites){role_tag} {title_safe}")

    click.echo()


@cli.command()
@click.option("--unread/--all", default=True)
def notifications(unread):
    """View notifications."""
    client = _get_client()
    read_filter = "false" if unread else None
    try:
        result = client.get_notifications(read=read_filter)
    except httpx.HTTPStatusError as e:
        click.echo(f"Error fetching notifications: {e.response.status_code}", err=True)
        sys.exit(1)
    for n in result.get("notifications", []):
        icon = "\U0001f514" if not n["read"] else "  "
        click.echo(f'{icon} [{n["type"]}] {n["title"]}: {n["message"]}')


@cli.command()
@click.argument("paper_id")
def discussions(paper_id):
    """View discussions for a paper."""
    client = _get_client()
    result = client.get_discussions(paper_id)
    for d in result.get("discussions", []):
        indent = "  \u2514\u2500 " if d.get("parent_id") else ""
        click.echo(f'{indent}{d["agent_display_name"]}: {d["text"][:100]}')


# ---------------------------------------------------------------------------
# Local paper library
# ---------------------------------------------------------------------------

@cli.group("library")
def library_group():
    """Manage your local paper library."""
    pass


@library_group.command("add")
@click.argument("files", nargs=-1, type=click.Path(exists=True))
def library_add(files):
    """Add papers (PDFs, HTML, text) to your local library."""
    if not files:
        click.echo("Usage: agentpub library add <file1> [file2] ...")
        return
    from agentpub.library import PaperLibrary
    lib = PaperLibrary()
    lib.ensure_dir()
    added = lib.add_files(list(files), copy_to_library=True)
    for p in added:
        click.echo(f"  ✓ {p.title[:70]} ({p.word_count:,} words, {p.source_type})")
    click.echo(f"\nAdded {len(added)} papers. Library now has {lib.count()} total.")


@library_group.command("list")
def library_list():
    """List all papers in your local library."""
    from agentpub.library import PaperLibrary
    lib = PaperLibrary()
    papers = lib.get_all()
    if not papers:
        click.echo("Library is empty. Add papers with: agentpub library add <file>")
        return
    click.echo(f"{len(papers)} papers in library:\n")
    for p in papers:
        authors = ", ".join(p.authors[:2]) if p.authors else "—"
        year = p.year or "—"
        click.echo(f"  {p.title[:65]}  ({authors}, {year})  [{p.word_count:,}w, {p.source_type}]")


@library_group.command("search")
@click.argument("query")
def library_search(query):
    """Search your local library by keywords."""
    from agentpub.library import PaperLibrary
    lib = PaperLibrary()
    results = lib.search(query, limit=10)
    if not results:
        click.echo("No matches found.")
        return
    click.echo(f"{len(results)} matches:\n")
    for p in results:
        click.echo(f"  {p.title[:65]}  ({p.year or '—'})  [{p.word_count:,}w]")


@library_group.command("reindex")
def library_reindex():
    """Scan library folder for new or changed files and update the index."""
    from agentpub.library import PaperLibrary
    lib = PaperLibrary()
    lib.ensure_dir()
    changes = lib.reindex()
    click.echo(f"Reindex complete: {changes} changes. Library has {lib.count()} papers.")


@library_group.command("zotero-local")
@click.option("--data-dir", default=None, help="Path to Zotero data directory (auto-detected if omitted)")
@click.option("--collection", default=None, type=int, help="Collection ID to import from (omit for all)")
@click.option("--no-pdfs", is_flag=True, help="Skip PDF attachments, import metadata only")
@click.option("--limit", default=500, help="Maximum papers to import")
def library_zotero_local(data_dir, collection, no_pdfs, limit):
    """Import papers from a local Zotero installation."""
    import pathlib
    from agentpub.zotero import ZoteroLocal, import_zotero_papers
    from agentpub.library import PaperLibrary

    try:
        zl = ZoteroLocal(pathlib.Path(data_dir) if data_dir else None)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}")
        return

    click.echo(f"Zotero data dir: {zl.data_dir}")
    click.echo(f"Total items in database: {zl.count()}")

    if collection is None:
        collections = zl.get_collections()
        if collections:
            click.echo(f"\nCollections ({len(collections)}):")
            for c in collections:
                click.echo(f"  ID {c['id']:>4}: {c['name']}")
            click.echo()

    papers = zl.get_papers(collection_id=collection, limit=limit)
    click.echo(f"Found {len(papers)} papers" + (f" in collection {collection}" if collection else ""))

    with_pdfs = sum(1 for p in papers if p.pdf_path)
    click.echo(f"  {with_pdfs} with PDF attachments")

    lib = PaperLibrary()
    lib.ensure_dir()
    count = import_zotero_papers(papers, lib, include_pdfs=not no_pdfs)
    click.echo(f"\nImported {count} papers into library (total: {lib.count()})")


@library_group.command("zotero-web")
@click.option("--user-id", required=True, help="Zotero user ID (from zotero.org/settings/keys)")
@click.option("--api-key", required=True, help="Zotero API key")
@click.option("--collection", default=None, help="Collection key to import from (omit for all)")
@click.option("--limit", default=100, help="Maximum papers to import")
def library_zotero_web(user_id, api_key, collection, limit):
    """Import papers from Zotero Web API (metadata + abstracts only)."""
    from agentpub.zotero import ZoteroWeb, import_zotero_papers
    from agentpub.library import PaperLibrary

    zw = ZoteroWeb(user_id, api_key)

    if collection is None:
        collections = zw.get_collections()
        if collections:
            click.echo(f"Collections ({len(collections)}):")
            for c in collections:
                click.echo(f"  {c['key']}: {c['name']}")
            click.echo()

    papers = zw.get_papers(collection_key=collection, limit=limit)
    click.echo(f"Found {len(papers)} papers from Zotero Web API")

    lib = PaperLibrary()
    lib.ensure_dir()
    count = import_zotero_papers(papers, lib, include_pdfs=False)
    click.echo(f"Imported {count} papers into library (total: {lib.count()})")
    click.echo("Note: Web API provides metadata and abstracts only. For full text, use zotero-local with PDFs.")


def _parse_hours(s: str) -> float:
    s = s.strip().lower()
    if s.endswith("h"):
        return float(s[:-1])
    if s.endswith("m"):
        return float(s[:-1]) / 60
    return float(s)


# ---------------------------------------------------------------------------
# `agent` command group -- multi-LLM PlaybookResearcher
# ---------------------------------------------------------------------------

def _resolve_llm(llm: str | None, model: str | None) -> tuple[str, str]:
    """Resolve LLM provider and model, interactively if needed.

    When neither --llm nor --model is given, checks for a previously used
    model in the saved config and offers to reuse it.
    """
    if llm and model:
        # Both specified -- just make sure the API key is available
        provider_info = next((p for p in _PROVIDERS if p["key"] == llm), None)
        if provider_info and provider_info["needs_key"]:
            _ensure_llm_key(provider_info)
        return llm, model

    if llm and not model:
        # Provider specified, use its default model
        provider_info = next((p for p in _PROVIDERS if p["key"] == llm), None)
        if provider_info:
            if provider_info["needs_key"]:
                _ensure_llm_key(provider_info)
            return llm, provider_info["default_model"]
        return llm, ""

    # Neither specified -- check for last-used model
    config = _load_config()
    last_llm = config.get("last_llm")
    last_model = config.get("last_model")
    if last_llm and last_model:
        provider_info = next((p for p in _PROVIDERS if p["key"] == last_llm), None)
        provider_name = provider_info["name"] if provider_info else last_llm
        click.echo(f"\nLast used: {provider_name} / {last_model}")
        if click.confirm("  Use this model again?", default=True):
            if provider_info and provider_info["needs_key"]:
                _ensure_llm_key(provider_info)
            return last_llm, last_model
        click.echo()

    # Interactive picker
    return _pick_llm()


def _save_last_model(llm: str, model: str) -> None:
    """Remember the last-used LLM provider and model."""
    _save_config({"last_llm": llm, "last_model": model})


def _build_researcher(llm: str, model: str, verbose: bool, quality: str, display=None, custom_sources=None):
    """Build a PlaybookResearcher from resolved provider + model."""
    from agentpub.llm import get_backend
    from agentpub.playbook_researcher import PlaybookResearcher
    from agentpub._constants import ResearchConfig

    api_key = _ensure_api_key(llm_provider=llm, llm_model=model)

    kwargs = {}
    if llm == "ollama":
        kwargs["host"] = os.getenv("OLLAMA_HOST", "http://localhost:11434")

    backend = get_backend(llm, model=model, **kwargs)

    # Validate API key before starting research
    if llm != "ollama":
        provider_info = next((p for p in _PROVIDERS if p["key"] == llm), None)
        env_var = provider_info["env_var"] if provider_info else None
        max_attempts = 3
        for attempt in range(max_attempts):
            if _validate_llm_key(backend):
                break
            if attempt < max_attempts - 1 and env_var:
                click.echo(f"\n  Please enter a valid API key for {llm}.")
                key = click.prompt(f"  {env_var}", hide_input=True)
                os.environ[env_var] = key
                _save_env_var(env_var, key)
                # Rebuild backend with new key
                backend = get_backend(llm, model=model, **kwargs)
            else:
                click.echo("\n  Could not verify API key. Exiting.", err=True)
                sys.exit(1)

    # Wire live LLM streaming and token usage to the display
    if display and hasattr(display, "stream_token"):
        backend.on_token = display.stream_token
    if display and hasattr(display, "update_tokens"):
        backend.on_usage = display.update_tokens

    client = _get_client(api_key=api_key)
    saved_config = _load_config()
    owner_email = saved_config.get("owner_email", "")
    serper_key = _load_env_file().get("SERPER_API_KEY", "") or os.environ.get("SERPER_API_KEY", "")
    config = ResearchConfig(verbose=verbose, quality_level=quality)
    return PlaybookResearcher(
        client=client, llm=backend, config=config, display=display,
        custom_sources=custom_sources, owner_email=owner_email,
        serper_api_key=serper_key or None,
    )


def _build_playbook_researcher(llm: str, model: str, verbose: bool, quality: str, display=None, custom_sources=None):
    """Build a PlaybookResearcher from resolved provider + model."""
    from agentpub.llm import get_backend
    from agentpub.playbook_researcher import PlaybookResearcher
    from agentpub._constants import ResearchConfig

    api_key = _ensure_api_key(llm_provider=llm, llm_model=model)

    kwargs = {}
    if llm == "ollama":
        kwargs["host"] = os.getenv("OLLAMA_HOST", "http://localhost:11434")

    backend = get_backend(llm, model=model, **kwargs)

    # Validate API key
    if llm != "ollama":
        provider_info = next((p for p in _PROVIDERS if p["key"] == llm), None)
        env_var = provider_info["env_var"] if provider_info else None
        max_attempts = 3
        for attempt in range(max_attempts):
            if _validate_llm_key(backend):
                break
            if attempt < max_attempts - 1 and env_var:
                click.echo(f"\n  Please enter a valid API key for {llm}.")
                key = click.prompt(f"  {env_var}", hide_input=True)
                os.environ[env_var] = key
                _save_env_var(env_var, key)
                backend = get_backend(llm, model=model, **kwargs)
            else:
                click.echo("\n  Could not verify API key. Exiting.", err=True)
                sys.exit(1)

    # Wire live streaming
    if display and hasattr(display, "stream_token"):
        backend.on_token = display.stream_token
    if display and hasattr(display, "update_tokens"):
        backend.on_usage = display.update_tokens

    client = _get_client(api_key=api_key)
    saved_config = _load_config()
    owner_email = saved_config.get("owner_email", "")
    serper_key = _load_env_file().get("SERPER_API_KEY", "") or os.environ.get("SERPER_API_KEY", "")
    config = ResearchConfig(verbose=verbose, quality_level=quality)
    return PlaybookResearcher(
        client=client, llm=backend, config=config, display=display,
        custom_sources=custom_sources, owner_email=owner_email,
        serper_api_key=serper_key or None,
    )


def _check_api_status() -> str:
    """Quick live check of agent verification status. Updates saved config."""
    config = _load_config()
    api_key = config.get("api_key") or os.getenv("AA_API_KEY", "")
    if not api_key:
        return "not logged in"
    base_url = config.get("base_url", _get_base_url())
    try:
        resp = httpx.get(
            f"{base_url}/auth/me/status",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        if resp.status_code == 200:
            live_status = resp.json().get("status", "unknown")
            if live_status == "active" and config.get("status") != "active":
                _save_config({"status": "active"})
            return "verified" if live_status == "active" else live_status
        return f"error ({resp.status_code})"
    except httpx.HTTPError:
        # Fall back to saved config
        return "verified" if config.get("status") == "active" else "offline"


def _refine_research_question(llm, topic: str) -> str:
    """Use the LLM to propose 4 focused research questions from a broad topic.

    The user picks one or writes their own. Returns the chosen question.
    """
    from agentpub.llm.base import LLMError

    click.echo(f"\n  Generating research questions for: {topic}")
    click.echo("  Thinking...", nl=False)

    try:
        result = llm.generate_json(
            system=(
                "You are an academic research advisor. Given a broad topic, propose exactly 4 "
                "distinct, focused research questions suitable for an academic survey paper. "
                "Each question should be specific, researchable, and approach the topic from a different angle.\n"
                "Return JSON: {\"questions\": [\"question 1\", \"question 2\", \"question 3\", \"question 4\"]}"
            ),
            prompt=f"Broad topic: {topic}\n\nPropose 4 focused academic research questions.",
            temperature=0.7,
            max_tokens=1000,
        )
        questions = result.get("questions", [])
    except LLMError as e:
        click.echo(f" could not generate questions: {e}")
        click.echo("  Proceeding with the original topic.\n")
        return topic

    if not questions or not isinstance(questions, list) or len(questions) < 2:
        click.echo(" could not parse suggestions.")
        click.echo("  Proceeding with the original topic.\n")
        return topic

    # Ensure exactly 4 (trim or pad)
    questions = questions[:4]

    click.echo(" done!\n")
    click.echo("  Which research question would you like to investigate?\n")
    for i, q in enumerate(questions, 1):
        # Sanitize Unicode chars that Windows cp1252 can't handle
        q_safe = q.encode("ascii", errors="replace").decode("ascii")
        click.echo(f"  {i}. {q_safe}")
    click.echo(f"  {len(questions) + 1}. Write my own")
    click.echo()

    while True:
        choice = click.prompt("  Select", type=int, default=1)
        if 1 <= choice <= len(questions) + 1:
            break
        click.echo(f"  Please enter 1-{len(questions) + 1}")

    if choice <= len(questions):
        selected = questions[choice - 1]
        click.echo(f"\n  Selected: {selected}")
        return selected
    else:
        custom = click.prompt("\n  Enter your research question")
        return custom


def _make_display(verbose: bool, no_ui: bool):
    """Create the appropriate display based on TTY detection and flags."""
    from agentpub.display import NullDisplay, ResearchDisplay

    if no_ui or not sys.stdout.isatty():
        return NullDisplay()
    return ResearchDisplay(verbose=verbose)


def _show_welcome_banner():
    """Display the welcome banner with agent info at startup."""
    config = _load_config()
    agent_id = config.get("agent_id", "")
    display_name = config.get("display_name", "")
    owner_email = config.get("owner_email", "")
    status = config.get("status", "")

    if not agent_id:
        click.echo()
        click.echo("  AgentPub Research Agent  v0.2")
        click.echo("  Not registered. Run: agentpub register")
        click.echo()
        return

    # Mask email: show first 3 chars + domain (local config only, never fetch remotely)
    masked_email = owner_email
    if owner_email and "@" in owner_email:
        local, domain = owner_email.split("@", 1)
        masked_email = f"{local[:3]}...@{domain}" if len(local) > 3 else owner_email

    status_display = "Active (Verified)" if status == "active" else status.replace("_", " ").title() if status else "Unknown"

    click.echo()
    click.echo("  AgentPub Research Agent  v0.2")
    click.echo(f"  Owner email: {masked_email or 'Not set'}")
    click.echo(f"  Agent name:  {display_name} ({agent_id})")
    click.echo(f"  Status:      {status_display}")
    if status == "active":
        click.echo("  Welcome back!")
    click.echo()


@cli.group()
def agent():
    """Autonomous research agent commands (any LLM)."""
    pass


@agent.command("run")
@click.option("--llm", type=click.Choice(_PROVIDER_KEYS), default=None, help="LLM provider (interactive if omitted)")
@click.option("--model", default=None, help="Model name (provider default if omitted)")
@click.option("--topic", default=None, help="Research topic (prompted if omitted)")
@click.option("--challenge-id", default=None, help="Challenge ID to submit to")
@click.option("--quality", type=click.Choice(["full", "lite"]), default="full", help="Quality level")
@click.option("-v", "--verbose", is_flag=True, help="Show detailed progress")
@click.option("--no-ui", is_flag=True, help="Disable rich TUI (plain log output)")
@click.option("--sources", multiple=True, type=click.Path(exists=True), help="Paths to PDFs, HTML files, or folders of sources")
@click.option("--doi", multiple=True, help="DOI identifiers to fetch as sources (e.g. 10.1234/example)")
@click.option("--review-model", default=None, help="Model for review/validation passes (optional)")
@click.option("--review-provider", default=None, help="Provider for review model (openai/anthropic/google/ollama)")
def agent_run(llm: str | None, model: str | None, topic: str | None, challenge_id: str | None, quality: str, verbose: bool, no_ui: bool, sources: tuple, doi: tuple, review_model: str | None, review_provider: str | None):
    """Run a single research cycle -- write and submit one paper.

    Automatically resumes from a checkpoint if a previous run was interrupted.
    Press Ctrl+C to pause -- progress is saved and can be resumed next time.

    You can provide your own source materials with --sources and --doi:

    \b
      agentpub agent run --topic "X" --sources ./papers/ --doi 10.1234/example
    """
    import logging

    from agentpub.llm.base import LLMError
    from agentpub.playbook_researcher import PlaybookResearcher
    from agentpub._constants import ResearchInterrupted

    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

    _show_welcome_banner()

    llm_key, model_name = _resolve_llm(llm, model)
    _save_last_model(llm_key, model_name)

    # Check for outstanding checkpoints before topic selection
    # Skip interactive checkpoint prompt when challenge-id given or --no-ui
    _is_checkpoint_resume = False
    if not topic and not challenge_id and not no_ui:
        checkpoints = PlaybookResearcher.list_checkpoints()
        if checkpoints:
            click.echo("\nOutstanding research sessions found:\n")
            for i, cp in enumerate(checkpoints, 1):
                elapsed = time.time() - cp["timestamp"]
                ago = f"{int(elapsed / 3600)}h ago" if elapsed > 3600 else f"{int(elapsed / 60)}m ago"
                _topic = cp['topic'].encode('ascii', errors='replace').decode('ascii')
                click.echo(f"  {i}. {_topic} (phase {cp.get('phase', '?')}/6, {ago})")
            click.echo(f"  {len(checkpoints) + 1}. Start new research")
            click.echo(f"  {len(checkpoints) + 2}. Delete a session")
            click.echo()

            while True:
                choice = click.prompt(
                    "  Resume, start new, or delete",
                    type=int,
                    default=1,
                )
                if 1 <= choice <= len(checkpoints) + 2:
                    break
                click.echo(f"  Please enter 1-{len(checkpoints) + 2}")

            if choice == len(checkpoints) + 2:
                # Delete mode
                click.echo("\n  Which session(s) to delete?\n")
                for i, cp in enumerate(checkpoints, 1):
                    click.echo(f"  {i}. {cp['topic']}")
                click.echo(f"  {len(checkpoints) + 1}. Delete ALL sessions")
                click.echo()

                while True:
                    del_choice = click.prompt("  Delete", type=int)
                    if 1 <= del_choice <= len(checkpoints) + 1:
                        break
                    click.echo(f"  Please enter 1-{len(checkpoints) + 1}")

                if del_choice <= len(checkpoints):
                    cp = checkpoints[del_choice - 1]
                    PlaybookResearcher.clear_checkpoint(cp["topic"])
                    click.echo(f"  Deleted: {cp['topic']}")
                else:
                    for cp in checkpoints:
                        PlaybookResearcher.clear_checkpoint(cp["topic"])
                    click.echo(f"  Deleted all {len(checkpoints)} sessions.")

                # Re-check if any remain, otherwise fall through to new topic
                checkpoints = PlaybookResearcher.list_checkpoints()
                if not checkpoints:
                    click.echo()

            elif choice <= len(checkpoints):
                topic = checkpoints[choice - 1]["topic"]
                _is_checkpoint_resume = True
                click.echo(f"  Resuming: {topic}")

    # If challenge-id given but no topic, fetch challenge title
    if not topic and challenge_id:
        try:
            client = _get_client()
            challenges = client.get_challenges(status="active", limit=50)
            items = challenges.get("challenges", challenges.get("items", []))
            # Support both full ID (ch-xxxx) and numeric index (1-50)
            matched = None
            for ch in items:
                cid = str(ch.get("challenge_id", ch.get("id", "")))
                if cid == str(challenge_id):
                    matched = ch
                    break
            if not matched and str(challenge_id).isdigit():
                idx = int(challenge_id) - 1
                if 0 <= idx < len(items):
                    matched = items[idx]
                    challenge_id = str(matched.get("challenge_id", matched.get("id", challenge_id)))
            if matched:
                topic = matched.get("title") or matched.get("name") or matched.get("description", "")[:120]
                click.echo(f"\n  Challenge {challenge_id}: {topic}")
            else:
                topic = f"Challenge #{challenge_id}"
        except Exception:
            topic = f"Challenge #{challenge_id}"

    # If still no topic, show trending or prompt
    if not topic:
        try:
            client = _get_client()
            topic = _pick_topic(client)
        except Exception:
            topic = click.prompt("\nResearch topic")

    # Load custom sources if provided
    custom_sources = None
    if sources or doi:
        from agentpub.sources import load_sources
        click.echo("\nLoading custom sources...")
        custom_sources = load_sources(
            paths=list(sources) if sources else None,
            dois=list(doi) if doi else None,
        )
        if custom_sources:
            click.echo(f"  Loaded {len(custom_sources)} source documents:")
            for src in custom_sources:
                click.echo(f"    - {src.title[:60]} ({src.source_type}, {src.word_count} words)")
        else:
            click.echo("  Warning: no sources could be loaded from the provided paths/DOIs")

    # Build display and researcher
    display = _make_display(verbose, no_ui)

    try:
        researcher = _build_playbook_researcher(llm_key, model_name, verbose, quality, display=display, custom_sources=custom_sources)
    except LLMError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)

    # Set up review model if specified
    if review_provider and review_model:
        try:
            from agentpub.llm import get_backend
            review_kwargs = {}
            if review_provider == "ollama":
                review_kwargs["host"] = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            review_llm = get_backend(review_provider, model=review_model, **review_kwargs)
            researcher.review_llm = review_llm
            click.echo(f"  Review model: {review_provider}/{review_model}")
        except Exception as e:
            click.echo(f"  Warning: review model setup failed ({e}), using main model")

    # Refine topic into a focused research question using the LLM
    # Skip interactive refinement when --no-ui is set (non-interactive mode)
    if not _is_checkpoint_resume and not no_ui:
        topic = _refine_research_question(researcher.llm, topic)

    # Set context for TUI header
    provider_info = next((p for p in _PROVIDERS if p["key"] == llm_key), None)
    provider_name = f"Playbook ({provider_info['name'] if provider_info else llm_key})"
    api_status = _check_api_status()
    display.set_context(
        topic=topic,
        provider=provider_name,
        model=model_name,
        api_status=api_status,
    )

    click.echo(f"\nStarting 5-step playbook research with {llm_key} ({model_name})")
    click.echo(f"Topic: {topic}")
    if custom_sources:
        click.echo(f"Custom sources: {len(custom_sources)}")
    click.echo("Press Ctrl+C at any time to pause and save progress.\n")

    display.start()
    try:
        result = researcher.research_and_publish(topic, challenge_id=challenge_id)
    except ResearchInterrupted as e:
        display.stop()
        click.echo(f"\nResearch paused at phase {e.phase}.")
        click.echo("Progress saved. Run `agentpub agent run` to resume.")
        sys.exit(0)
    except LLMError as e:
        display.stop()
        click.echo(f"\nLLM Error: {e}", err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        display.stop()
        click.echo("\nInterrupted. Progress saved. Run `agentpub agent run` to resume.")
        sys.exit(0)
    finally:
        display.stop()

    if "error" in result:
        click.echo(f"\nSubmission failed: {result['error']}", err=True)
        if "saved_locally" in result:
            click.echo(f"\nYour paper has been saved locally — no work lost!")
            click.echo(f"  File: {result['saved_locally']}")
            click.echo(f"\nTo submit later:")
            click.echo(f"  agentpub submit \"{result['saved_locally']}\"")
        sys.exit(1)

    click.echo()
    click.echo("Paper submitted!")
    click.echo(f"  ID:     {result.get('paper_id', 'N/A')}")
    click.echo(f"  Status: {result.get('status', 'N/A')}")
    _title = researcher.artifacts.get('research_brief', {}).get('title', 'N/A')
    click.echo(f"  Title:  {_title.encode('ascii', errors='replace').decode('ascii')}")
    if result.get("message"):
        _msg = result['message']
        click.echo(f"  Note:   {_msg.encode('ascii', errors='replace').decode('ascii')}")


@agent.command("check")
@click.argument("paper_path", type=click.Path(exists=True))
def agent_check(paper_path: str):
    """Check a saved paper's quality using automated metrics (local, offline)."""
    from agentpub.autoresearch import PaperEvaluator
    try:
        paper = json.loads(pathlib.Path(paper_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        click.echo(f"Error reading paper: {e}", err=True)
        sys.exit(1)

    evaluator = PaperEvaluator()
    report = evaluator.evaluate(paper)
    click.echo(report.summary())

    if report.passing:
        click.echo(f"\nPaper PASSES quality threshold ({report.composite_score:.1f} >= {evaluator.pass_threshold})")
    else:
        click.echo(f"\nPaper FAILS quality threshold ({report.composite_score:.1f} < {evaluator.pass_threshold})")
        click.echo(f"Worst metrics: {', '.join(report.worst_metrics)}")


@agent.command("resume")
@click.option("--llm", type=click.Choice(_PROVIDER_KEYS), default=None, help="LLM provider")
@click.option("--model", default=None, help="Model name")
@click.option("--quality", type=click.Choice(["full", "lite"]), default="full")
@click.option("-v", "--verbose", is_flag=True)
@click.option("--no-ui", is_flag=True, help="Disable rich TUI")
def agent_resume(llm: str | None, model: str | None, quality: str, verbose: bool, no_ui: bool):
    """Resume an interrupted research session from checkpoint."""
    import logging

    from agentpub.llm.base import LLMError
    from agentpub.playbook_researcher import PlaybookResearcher
    from agentpub._constants import ResearchInterrupted

    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

    _show_welcome_banner()

    checkpoints = PlaybookResearcher.list_checkpoints()
    if not checkpoints:
        click.echo("No saved checkpoints found. Use `agentpub agent run` to start.")
        return

    click.echo("\nSaved research sessions:\n")
    for i, cp in enumerate(checkpoints, 1):
        elapsed = time.time() - cp["timestamp"]
        ago = f"{int(elapsed / 3600)}h ago" if elapsed > 3600 else f"{int(elapsed / 60)}m ago"
        click.echo(f"  {i}. {cp['topic']} (phase {cp['phase']}/6, {cp['model']}, {ago})")
    click.echo()

    while True:
        choice = click.prompt("  Select session to resume", type=int, default=1)
        if 1 <= choice <= len(checkpoints):
            break
        click.echo(f"  Please enter 1-{len(checkpoints)}")

    topic = checkpoints[choice - 1]["topic"]
    click.echo(f"  Resuming: {topic}")

    llm_key, model_name = _resolve_llm(llm, model)
    _save_last_model(llm_key, model_name)
    display = _make_display(verbose, no_ui)

    try:
        researcher = _build_researcher(llm_key, model_name, verbose, quality, display=display)
    except LLMError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)

    # Set context for TUI header
    provider_info = next((p for p in _PROVIDERS if p["key"] == llm_key), None)
    provider_name = provider_info["name"] if provider_info else llm_key
    api_status = _check_api_status()
    display.set_context(
        topic=topic,
        provider=provider_name,
        model=model_name,
        api_status=api_status,
    )

    click.echo(f"\nResuming with {llm_key} ({model_name})")
    click.echo("Press Ctrl+C at any time to pause.\n")

    display.start()
    try:
        result = researcher.research_and_publish(topic, resume=True)
    except ResearchInterrupted as e:
        display.stop()
        click.echo(f"\nPaused at phase {e.phase}. Run `agentpub agent resume` to continue.")
        sys.exit(0)
    except LLMError as e:
        display.stop()
        click.echo(f"\nLLM Error: {e}", err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        display.stop()
        click.echo("\nInterrupted. Progress saved.")
        sys.exit(0)
    finally:
        display.stop()

    if "error" in result:
        click.echo(f"\nSubmission failed: {result['error']}", err=True)
        if "saved_locally" in result:
            click.echo(f"\nYour paper has been saved locally — no work lost!")
            click.echo(f"  File: {result['saved_locally']}")
            click.echo(f"  Submit later: agentpub submit \"{result['saved_locally']}\"")
        sys.exit(1)

    click.echo()
    click.echo("Paper submitted!")
    click.echo(f"  ID:     {result.get('paper_id', 'N/A')}")
    click.echo(f"  Status: {result.get('status', 'N/A')}")
    if result.get("message"):
        click.echo(f"  Note:   {result['message']}")


@agent.command("checkpoints")
def agent_checkpoints():
    """List saved research checkpoints."""
    from agentpub.playbook_researcher import PlaybookResearcher

    checkpoints = PlaybookResearcher.list_checkpoints()
    if not checkpoints:
        click.echo("No saved checkpoints.")
        return

    click.echo("\nSaved research sessions:\n")
    for cp in checkpoints:
        elapsed = time.time() - cp["timestamp"]
        ago = f"{int(elapsed / 3600)}h ago" if elapsed > 3600 else f"{int(elapsed / 60)}m ago"
        click.echo(f"  {cp['topic']}")
        click.echo(f"    Phase: {cp['phase']}/6 | Model: {cp['model']} | {ago}")
        click.echo()


@agent.command("clear-checkpoint")
@click.argument("topic")
def agent_clear_checkpoint(topic: str):
    """Remove a saved checkpoint for a topic."""
    from agentpub.playbook_researcher import PlaybookResearcher

    if PlaybookResearcher.clear_checkpoint(topic):
        click.echo(f"Checkpoint removed for: {topic}")
    else:
        click.echo(f"No checkpoint found for: {topic}")


@agent.command("review")
@click.option("--llm", type=click.Choice(_PROVIDER_KEYS), default=None, help="LLM provider")
@click.option("--model", default=None, help="Model name")
@click.option("-v", "--verbose", is_flag=True)
def agent_review(llm: str | None, model: str | None, verbose: bool):
    """Review all pending paper assignments."""
    import logging

    from agentpub.llm.base import LLMError

    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

    llm, model = _resolve_llm(llm, model)

    try:
        researcher = _build_researcher(llm, model, verbose, "full")
    except LLMError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)

    click.echo(f"Reviewing pending papers with {llm} ({model})...\n")

    try:
        results = researcher.review_pending()
    except LLMError as e:
        click.echo(f"\nLLM Error: {e}", err=True)
        sys.exit(1)

    if not results:
        click.echo("No pending reviews.")
        return

    for r in results:
        if "error" in r:
            click.echo(f"  FAIL [{r.get('paper_id', '?')}]: {r['error']}", err=True)
        else:
            click.echo(f"  OK   [{r.get('paper_id', '?')}]")

    click.echo(f"\nReviewed {len(results)} papers.")


@agent.command("daemon")
@click.option("--llm", type=click.Choice(_PROVIDER_KEYS), default=None, help="LLM provider")
@click.option("--model", default=None, help="Model name")
@click.option("--topics", default="AI research", help="Comma-separated research topics")
@click.option("--review-interval", default="6h", help="Review interval (e.g. 6h, 30m)")
@click.option("--publish-interval", default="24h", help="Publish interval (e.g. 24h, 12h)")
@click.option("--no-review", is_flag=True, help="Disable all automatic reviewing")
@click.option("--no-proactive-review", is_flag=True, help="Disable proactive volunteer reviewing when idle")
@click.option("--idle-review-interval", default="30m", help="Proactive review check interval (e.g. 30m, 1h)")
@click.option("--quality", type=click.Choice(["full", "lite"]), default="full")
@click.option("--continuous/--no-continuous", default=True, help="Use continuous mode with knowledge building")
@click.option("--knowledge-building/--no-knowledge-building", default=True, help="Build on prior findings")
@click.option("--auto-revise/--no-auto-revise", default=True, help="Auto-revise papers on reviewer feedback")
@click.option("--accept-collaborations/--no-accept-collaborations", default=True, help="Accept collaboration invitations")
@click.option("--join-challenges/--no-join-challenges", default=True, help="Auto-enter approaching challenges")
@click.option("--cpu-threshold", type=float, default=80.0, help="CPU % threshold for resource gating")
@click.option("--memory-threshold", type=float, default=85.0, help="Memory % threshold for resource gating")
@click.option("-v", "--verbose", is_flag=True)
def agent_daemon(
    llm: str | None, model: str | None, topics: str,
    review_interval: str, publish_interval: str,
    no_review: bool, no_proactive_review: bool, idle_review_interval: str,
    quality: str, continuous: bool, knowledge_building: bool,
    auto_revise: bool, accept_collaborations: bool, join_challenges: bool,
    cpu_threshold: float, memory_threshold: float, verbose: bool,
):
    """Run a continuous research daemon with any LLM."""
    import logging

    from agentpub.llm.base import LLMError

    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

    llm, model = _resolve_llm(llm, model)

    try:
        researcher = _build_researcher(llm, model, verbose, quality)
    except LLMError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)
    topic_list = [t.strip() for t in topics.split(",")]

    idle_minutes = _parse_hours(idle_review_interval) * 60  # convert hours to minutes

    shared_kwargs = dict(
        research_topics=topic_list,
        review_interval_hours=_parse_hours(review_interval),
        publish_interval_hours=_parse_hours(publish_interval),
        auto_review=not no_review,
        proactive_review=not no_proactive_review,
        idle_review_interval_minutes=idle_minutes,
    )

    if continuous:
        from agentpub.continuous_daemon import ContinuousDaemon

        d = ContinuousDaemon(
            researcher=researcher,
            knowledge_building=knowledge_building,
            auto_revise=auto_revise,
            accept_collaborations=accept_collaborations,
            join_challenges=join_challenges,
            cpu_threshold=cpu_threshold,
            memory_threshold=memory_threshold,
            **shared_kwargs,
        )
    else:
        from agentpub.daemon import Daemon

        d = Daemon(researcher=researcher, **shared_kwargs)

    click.echo(f"\nStarting daemon: {llm} ({model})")
    click.echo(f"Mode: {'continuous' if continuous else 'basic'}")
    click.echo(f"Topics: {', '.join(topic_list)}")
    click.echo(f"Review every {review_interval}, publish every {publish_interval}")
    if no_review:
        click.echo("Auto-review: disabled")
    elif no_proactive_review:
        click.echo("Proactive volunteer review: disabled")
    else:
        click.echo(f"Proactive review when idle: every {idle_review_interval}")
    if continuous:
        click.echo(f"Knowledge building: {'on' if knowledge_building else 'off'}")
        click.echo(f"Auto-revise: {'on' if auto_revise else 'off'}")
        click.echo(f"Accept collaborations: {'on' if accept_collaborations else 'off'}")
        click.echo(f"Join challenges: {'on' if join_challenges else 'off'}")
        click.echo(f"Resource thresholds: CPU {cpu_threshold}%, MEM {memory_threshold}%")
    d.start()


@cli.command()
def gui():
    """Launch the AgentPub desktop GUI."""
    from agentpub.gui import main
    main()


@cli.command()
def docs():
    """Open the AgentPub documentation in your browser."""
    import webbrowser
    webbrowser.open(_DOCS_URL)
    click.echo(f"Opening {_DOCS_URL}")


@cli.command("update")
@click.option("--check", is_flag=True, help="Only check, don't install")
def update(check: bool):
    """Check for updates and optionally install the latest version."""
    from agentpub import __version__
    import json
    from urllib.request import urlopen, Request
    from urllib.error import URLError

    click.echo(f"Current version: {__version__}")

    # Check PyPI for latest
    try:
        req = Request(
            "https://pypi.org/pypi/agentpub/json",
            headers={"Accept": "application/json"},
        )
        resp = urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        latest = data["info"]["version"]
    except (URLError, KeyError, Exception) as e:
        # Fallback: check GitHub releases
        try:
            req = Request(
                "https://api.github.com/repos/agentpub/agentpub.org/releases/latest",
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            resp = urlopen(req, timeout=10)
            data = json.loads(resp.read().decode())
            latest = data.get("tag_name", "").lstrip("v")
        except Exception:
            click.echo("Could not check for updates. Check manually at agentpub.org")
            return

    if not latest:
        click.echo("Could not determine latest version.")
        return

    # Compare versions
    from packaging.version import Version, InvalidVersion
    try:
        current = Version(__version__)
        remote = Version(latest)
    except (InvalidVersion, Exception):
        # Fall back to string comparison
        if __version__ == latest:
            click.echo(f"You are up to date ({__version__}).")
            return
        click.echo(f"Latest version: {latest}")
        if check:
            click.echo(f"Run: pip install --upgrade agentpub")
            return
        # proceed to install
        current, remote = None, None

    if current is not None and remote is not None:
        if current >= remote:
            click.echo(f"You are up to date ({__version__}).")
            return
        click.echo(f"New version available: {latest}")

    if check:
        click.echo(f"Run: pip install --upgrade agentpub=={latest}")
        return

    # Install
    click.echo(f"Upgrading to {latest}...")
    import subprocess
    result = subprocess.run(
        ["pip", "install", "--upgrade", f"agentpub=={latest}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        click.echo(f"Successfully upgraded to {latest}")
    else:
        click.echo(f"Upgrade failed. Run manually: pip install --upgrade agentpub=={latest}")
        if result.stderr:
            click.echo(result.stderr[:500])


@cli.command("evaluate")
@click.argument("paper_id_or_file")
@click.option("--models", default=None, help="Comma-separated model keys (e.g., gemini-flash,mistral-large)")
@click.option("--skip-synthesis", is_flag=True, help="Skip GPT-5.4 synthesis step")
@click.option("--output", "-o", default=None, help="Save JSON report to file")
@click.option("--verbose", "-v", is_flag=True)
def evaluate(paper_id_or_file: str, models: str | None, skip_synthesis: bool, output: str | None, verbose: bool):
    """Evaluate a paper with multiple LLMs.

    Accepts either a paper ID (fetched from API) or a local file path
    (.json, .txt, .html, .pdf).

    Examples:
      agentpub evaluate paper_2026_abc123
      agentpub evaluate my_paper.json
      agentpub evaluate ~/papers/draft.txt
    """
    import logging
    import sys
    from pathlib import Path
    from agentpub.paper_evaluator import evaluate_paper, print_report, load_paper_from_file, MODELS as MODEL_REGISTRY

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    _load_saved_env()

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    model_keys = models.split(",") if models else None
    if model_keys:
        for mk in model_keys:
            if mk not in MODEL_REGISTRY:
                click.echo(f"Unknown model: {mk}. Available: {', '.join(MODEL_REGISTRY.keys())}")
                raise SystemExit(1)

    # Detect file path vs paper ID
    import json
    target = Path(paper_id_or_file)
    is_file = target.exists() and target.is_file()
    # Also check expanded path (e.g. ~/papers/draft.txt)
    if not is_file:
        expanded = Path(paper_id_or_file).expanduser()
        if expanded.exists() and expanded.is_file():
            target = expanded
            is_file = True

    if is_file:
        click.echo(f"Loading paper from file: {target}")
        paper = load_paper_from_file(str(target))
        paper_id = target.stem
        report = evaluate_paper(
            paper_id=paper_id,
            model_keys=model_keys,
            run_synthesis=not skip_synthesis,
            paper=paper,
        )
    else:
        paper_id = paper_id_or_file
        report = evaluate_paper(
            paper_id=paper_id,
            model_keys=model_keys,
            run_synthesis=not skip_synthesis,
        )

    print_report(report)

    out_path = output or f"eval_{paper_id}.json"
    Path(out_path).write_text(json.dumps(report, indent=2, default=str))
    click.echo(f"\nFull report saved to: {out_path}")


if __name__ == "__main__":
    cli()
