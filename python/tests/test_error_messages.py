"""User-facing error messages.

The app's users are not the people who wrote it. A status code shown verbatim
("401") is not a sentence they can act on, and it is the single most common
thing an authenticated app shows when a session quietly expires — which is
exactly what happened here: a token expired and the app displayed "401" while
the daemon failed silently for 33 days.

The other half of the problem is that the API already writes a good message.
Its error bodies carry a plain-English ``detail`` ("Daily evaluation limit
reached (5/day). Try again tomorrow.") and printing the status code throws that
away. These tests pin both properties: technical failures become plain English,
and the server's own words are never discarded.
"""

from __future__ import annotations

import pytest

from agentpub.gui import _error_line, _explain_error, _is_auth_error, _token_expiry


class _Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _HTTPError(Exception):
    def __init__(self, msg, resp):
        super().__init__(msg)
        self.response = resp


def _err(code, body=None):
    return _HTTPError(f"HTTP {code}", _Resp(code, body))


# --------------------------------------------------------------------------
# The server's own message must survive
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", [400, 422, 429, 402, 404])
def test_the_servers_detail_is_shown_verbatim(code):
    """The API writes actionable errors on purpose. Never discard them."""
    detail = "Daily evaluation limit reached (5/day). Try again tomorrow."
    assert detail in _error_line(_err(code, {"detail": detail}))


def test_a_missing_detail_still_produces_guidance():
    """A bare status code with no body must not degrade to nothing useful."""
    head, guidance = _explain_error(_err(404))
    assert head and guidance


# --------------------------------------------------------------------------
# No raw status codes leak to the user
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", [401, 403, 404, 429, 500, 502, 503])
def test_the_status_code_is_never_the_whole_message(code):
    line = _error_line(_err(code))
    assert str(code) not in line, f"raw {code} leaked to the user: {line!r}"
    assert len(line.split()) >= 4, f"too terse to act on: {line!r}"


def test_expired_session_says_what_to_do():
    line = _error_line(_err(401)).lower()
    assert "expired" in line
    assert "sign in" in line


def test_server_faults_are_not_blamed_on_the_user():
    line = _error_line(_err(500)).lower()
    assert "not something you did" in line


# --------------------------------------------------------------------------
# Network-layer failures, which arrive as exception types not status codes
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "exc,expect",
    [
        (TimeoutError("Read timed out"), "too long"),
        (ConnectionError("getaddrinfo failed"), "could not reach"),
        (Exception("certificate verify failed: unable to get local issuer certificate"), "secure connection"),
    ],
)
def test_network_failures_are_translated(exc, expect):
    assert expect in _error_line(exc).lower()


def test_an_unrecognised_error_keeps_its_original_text():
    """Better an honest unknown than a confident wrong guess — the raw text is
    the only real information left at that point."""
    line = _error_line(RuntimeError("some novel failure"))
    assert "some novel failure" in line


# --------------------------------------------------------------------------
# Auth detection and local expiry parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code,expected", [(401, True), (403, True), (429, False), (500, False)])
def test_auth_errors_are_identified(code, expected):
    assert _is_auth_error(_err(code)) is expected


def test_token_expiry_is_read_without_a_network_call():
    """Expiry lives in the token, so it can be checked offline at startup —
    which is what turns a silent month-long failure into a visible one."""
    import base64
    import datetime
    import json

    exp = int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=5)).timestamp())
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    parsed = _token_expiry(f"header.{payload}.signature")

    assert parsed is not None
    assert abs((parsed - datetime.datetime.fromtimestamp(exp, datetime.timezone.utc)).total_seconds()) < 2


@pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.b", "a.!!!!.c"])
def test_an_unreadable_token_returns_none_rather_than_raising(bad):
    """An unparseable token is unknown, not expired — let the server judge."""
    assert _token_expiry(bad) is None
