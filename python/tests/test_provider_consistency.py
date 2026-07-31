"""A provider switched off in Settings must be off everywhere.

Two faults met here. The run screen filtered its model list by the enabled
providers; Evaluate and Discuss listed every model unconditionally, so turning
everything except OpenAI off changed one screen and nothing else. And the filter
itself was ``enabled OR has-a-key-in-the-environment``, which meant a provider
whose key happened to be exported could never be switched off at all — the
Settings switches were one-way.

A setting that applies in one place, or that cannot turn anything off, is worse
than no setting: it tells the user they are in control when they are not.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

import agentpub.gui as gui
from agentpub.paper_evaluator import MODELS as EVAL_MODELS

# Keys the availability check reads. Cleared per-scenario so the developer's own
# exported keys cannot make the test pass or fail by accident — which is exactly
# how the one-way-switch bug hid.
PROVIDER_ENV_VARS = [p.get("env_var") for p in gui._PROVIDERS if p.get("env_var")]


def _evaluate(app, enabled, env=None):
    """Resolve what each screen would offer, with the environment isolated."""
    app._config = dict(app._config, enabled_providers=enabled)
    app._env = env or {}
    stripped = {k: v for k, v in os.environ.items() if k not in PROVIDER_ENV_VARS}
    with patch.dict(os.environ, stripped, clear=True):
        with patch.object(gui, "_load_config", lambda: app._config):
            with patch.object(app, "_ollama_running", return_value=False):
                providers = app._usable_provider_keys()
                evaluate_models = set(app._usable_eval_models())
    discuss_models = [
        m for p in gui._PROVIDERS if p.get("key") in providers
        for m in (p.get("all_models") or p.get("models") or [])
    ]
    return providers, evaluate_models, discuss_models


def test_an_explicit_choice_beats_a_key_in_the_environment(gui_app):
    """The bug: unticking a provider did nothing while its key was exported."""
    providers, _, _ = _evaluate(
        gui_app, ["OpenAI"], env={"ANTHROPIC_API_KEY": "sk-ant-x", "GEMINI_API_KEY": "g"}
    )
    assert providers == {"openai"}, (
        f"a deliberate choice was overridden by exported keys: got {providers}"
    )


@pytest.mark.parametrize(
    "enabled,provider_key",
    [(["OpenAI"], "openai"), (["Anthropic Claude"], "anthropic"), (["Google Gemini"], "google")],
)
def test_all_three_screens_agree(gui_app, enabled, provider_key):
    """Run screen, Evaluate and Discuss must offer the same providers."""
    providers, evaluate_models, discuss_models = _evaluate(gui_app, enabled)

    assert providers == {provider_key}

    for key in evaluate_models:
        assert EVAL_MODELS[key]["provider"] == provider_key, (
            f"Evaluate offers {key} ({EVAL_MODELS[key]['provider']}) "
            f"while only {provider_key} is enabled"
        )

    allowed = {
        m for p in gui._PROVIDERS if p.get("key") == provider_key
        for m in (p.get("all_models") or p.get("models") or [])
    }
    assert set(discuss_models) <= allowed, (
        f"Discuss offers models outside {provider_key}: "
        f"{sorted(set(discuss_models) - allowed)}"
    )


def test_nothing_enabled_offers_nothing(gui_app):
    """Better an empty list with guidance than models that cannot run."""
    providers, evaluate_models, discuss_models = _evaluate(gui_app, [])
    assert not providers and not evaluate_models and not discuss_models


def test_a_fresh_install_still_autodetects(gui_app):
    """Convenience preserved: no choice made yet falls back to key detection,
    so pasting a key works without a second step."""
    providers, _, _ = _evaluate(gui_app, [], env={"OPENAI_API_KEY": "sk-x"})
    assert providers == {"openai"}


def test_no_evaluator_model_lacks_a_settings_entry():
    """Mistral sat in the evaluator with no way to enable or configure it, so
    ticking it could only ever produce a failed call."""
    configurable = {p["key"] for p in gui._PROVIDERS}
    orphans = {
        key: spec["provider"]
        for key, spec in EVAL_MODELS.items()
        if isinstance(spec, dict) and spec.get("provider") not in configurable
    }
    # These are filtered out of the UI by _usable_eval_models; this records
    # which, so adding a provider to the evaluator without a Settings entry is
    # a deliberate act rather than an oversight.
    assert set(orphans) <= {"mistral-large"}, (
        f"evaluator models with no Settings entry: {orphans}"
    )
