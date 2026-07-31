"""Remote model registry.

Lets the set of selectable models be updated by editing a single hosted JSON
file — the tool downloads it on startup — instead of shipping a new SDK/app
build for every model change.

Resolution order (best-effort, never fatal):
    remote URL  ->  on-disk cache (last-known-good)  ->  bundled fallback

The "bundled fallback" is the hardcoded Python list/dict already compiled into
the SDK/exe (``cli._PROVIDERS`` and ``paper_evaluator.MODELS``). So if the host
is unreachable, validation fails, or we're offline, the picker still works and
shows the models that shipped with this build.

The hosted JSON shape mirrors those structures exactly::

    {
      "schemaVersion": 1,
      "updatedAt": "2026-06-25",
      "providers": [ {<same shape as cli._PROVIDERS entries>} ],
      "evaluator_models": { "<key>": {<same shape as paper_evaluator.MODELS values>} }
    }

Override the source URL with the ``AGENTPUB_REGISTRY_URL`` env var.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib

import httpx

logger = logging.getLogger(__name__)

# Where the canonical model list lives. Default: served by the website (behind
# Cloudflare). Editing that file updates every client on its next start — no
# SDK/app release required. Overridable for testing / self-hosting.
REGISTRY_URL = os.environ.get("AGENTPUB_REGISTRY_URL", "https://agentpub.org/registry.json")

_CACHE_FILE = pathlib.Path.home() / ".agentpub" / "registry_cache.json"
_TIMEOUT_SECONDS = 3.0

# Memoize within a single process run so we hit the network at most once.
_cached: dict | None = None
_last_source: str = "bundled"


def _valid(data: object) -> bool:
    """Structural sanity check — reject anything that would break the picker."""
    if not isinstance(data, dict):
        return False
    providers = data.get("providers")
    if not isinstance(providers, list) or not providers:
        return False
    for p in providers:
        if not isinstance(p, dict):
            return False
        if "key" not in p or "name" not in p:
            return False
        if not isinstance(p.get("models"), list) or not p["models"]:
            return False
    return True


def _read_cache() -> dict | None:
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        return data if _valid(data) else None
    except Exception:
        return None


def _write_cache(data: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_name(_CACHE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, _CACHE_FILE)
    except Exception:
        pass


def fetch(force: bool = False) -> dict | None:
    """Return the registry dict from remote (preferred) or cache, else ``None``.

    Memoized for the process. A successful remote fetch refreshes the on-disk
    cache so a later offline start still gets the latest-known list.
    """
    global _cached, _last_source
    if _cached is not None and not force:
        return _cached

    # 1. Remote — the source of truth.
    try:
        resp = httpx.get(
            REGISTRY_URL,
            timeout=_TIMEOUT_SECONDS,
            headers={"User-Agent": "agentpub-sdk-registry"},
        )
        if resp.status_code == 200:
            data = resp.json()
            if _valid(data):
                _write_cache(data)
                _cached = data
                _last_source = "remote"
                logger.info("Model registry loaded from %s", REGISTRY_URL)
                return data
            logger.warning("Model registry at %s failed validation; ignoring", REGISTRY_URL)
        else:
            logger.info("Model registry HTTP %s from %s; using cache/bundled", resp.status_code, REGISTRY_URL)
    except Exception as exc:  # noqa: BLE001 — registry is best-effort
        logger.info("Model registry fetch failed (%s); using cache/bundled", exc)

    # 2. Cache — last-known-good.
    data = _read_cache()
    if data is not None:
        _cached = data
        _last_source = "cache"
        return data

    # 3. Nothing — caller keeps its bundled fallback.
    _last_source = "bundled"
    return None


def refresh_into_globals() -> str:
    """Download the registry and apply it IN PLACE to the SDK's bundled lists.

    Mutates ``cli._PROVIDERS`` / ``cli._PROVIDER_KEYS`` and
    ``paper_evaluator.MODELS`` so every existing reference picks up the current
    models without any other code change. Safe to call at every startup; never
    raises. Returns the source that was applied: ``'remote'``, ``'cache'`` or
    ``'bundled'`` (bundled = remote+cache both unavailable, lists left as-is).
    """
    data = fetch()
    if data is None:
        return "bundled"

    providers = data.get("providers")
    if isinstance(providers, list) and providers:
        try:
            from agentpub import cli
            cli._PROVIDERS[:] = providers
            cli._PROVIDER_KEYS[:] = [p["key"] for p in providers]
        except Exception:  # noqa: BLE001
            logger.debug("Could not apply providers to cli", exc_info=True)

    evaluator_models = data.get("evaluator_models")
    if isinstance(evaluator_models, dict) and evaluator_models:
        try:
            from agentpub import paper_evaluator
            paper_evaluator.MODELS.clear()
            paper_evaluator.MODELS.update(evaluator_models)
        except Exception:  # noqa: BLE001
            logger.debug("Could not apply evaluator_models to paper_evaluator", exc_info=True)

    return _last_source
