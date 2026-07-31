"""Regression test: the challenge dropdown stayed empty until the app was
restarted, on any machine where the user logged in for the first time.

_fetch_challenges early-returns when no api_key is stored. On a fresh install
the startup call is therefore a no-op, and do_login() never refetched — so the
dropdown only populated on the *next* launch, once the key was on disk.

These tests avoid instantiating AgentPubGUI (it subclasses tk.Tk and needs a
display) by calling the unbound methods against a lightweight stub.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from agentpub.gui import AgentPubGUI


class _Stub:
    """Minimal stand-in exposing only what _fetch_challenges touches."""

    def __init__(self, api_key=""):
        self._config = {"api_key": api_key}
        self._challenges = []
        self._challenges_etag = ""
        self.after = MagicMock()
        # Referenced when marshalling the combo update onto the Tk thread.
        # Without it the attribute lookup raises inside the broad except and
        # the scheduling call is silently skipped.
        self._populate_challenge_combo = MagicMock()


def test_fetch_challenges_is_a_noop_without_a_key():
    """The early return is intentional — but it is why login must refetch."""
    stub = _Stub(api_key="")
    with patch("httpx.get") as mock_get:
        AgentPubGUI._fetch_challenges(stub)
    mock_get.assert_not_called()
    assert stub._challenges == []


def test_fetch_challenges_populates_once_a_key_exists():
    stub = _Stub(api_key="tok_abc123")
    payload = {"challenges": [{"challenge_id": "ch-1", "title": "First"}]}

    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"etag": "W/\"v1\""}
    resp.json.return_value = payload

    with patch("httpx.get", return_value=resp), patch("pathlib.Path.write_text"), \
            patch("pathlib.Path.mkdir"):
        AgentPubGUI._fetch_challenges(stub)

    assert len(stub._challenges) == 1
    assert stub._challenges[0]["challenge_id"] == "ch-1"
    # Combo repopulation is marshalled onto the Tk main thread.
    stub.after.assert_called()


def test_login_refreshes_challenges():
    """The actual regression. do_login saved the token and refreshed agent
    status but never reloaded challenges, so the dropdown stayed empty for the
    whole session."""
    src = inspect.getsource(AgentPubGUI._open_register)
    assert "_load_challenges" in src, (
        "do_login must refetch challenges after storing the API key, otherwise "
        "a first-time login shows an empty challenge dropdown until restart"
    )


def test_login_stores_key_before_refreshing_challenges():
    """Ordering matters: _fetch_challenges reads self._config, so the config
    reload must happen before the refetch or it early-returns again."""
    src = inspect.getsource(AgentPubGUI._open_register)
    assert src.index("self._config = _load_config()") < src.index("self._load_challenges()"), (
        "config must be reloaded before challenges are refetched"
    )
