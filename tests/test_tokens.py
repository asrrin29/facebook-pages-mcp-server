"""Token diagnostics: the expiry text Graph sends, and what a token reports."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from facebook_pages_mcp_server.graph import GraphError
from facebook_pages_mcp_server.tokens import (
    describe_token_error,
    expired_at_text,
    is_token_error,
    token_state,
)

# The message Graph sent for a real expired session.
EXPIRED_MESSAGE = (
    "Error validating access token: Session has expired on Friday, 13-Nov-26 "
    "14:00:00 PST. The current time is Saturday, 14-Nov-26 09:53:26 PST."
)


def _expired_error() -> GraphError:
    return GraphError(EXPIRED_MESSAGE, http_status=401, code=190, error_subcode=463)


def test_expired_session_text_is_recovered_from_the_message():
    assert expired_at_text(_expired_error()) == "Friday, 13-Nov-26 14:00:00 PST"


def test_other_token_failures_are_not_reported_as_expired():
    error = GraphError("Invalid OAuth access token - cannot parse it.", code=190)
    assert is_token_error(error)
    assert expired_at_text(error) is None
    text = describe_token_error(error)
    assert "not accepted" in text
    assert "EXPIRED" not in text


def test_expired_token_error_names_the_date_and_the_repair():
    text = describe_token_error(_expired_error())
    assert "EXPIRED on Friday, 13-Nov-26 14:00:00 PST" in text
    assert "facebook_pages_set_token" in text
    assert "grant_type=fb_exchange_token" in text


def test_expired_session_subcode_is_recognised_without_a_date():
    # Graph normally states the date, but subcode 463 identifies the same
    # failure when the message does not.
    error = GraphError(
        "Error validating access token: The session has been invalidated.",
        code=190,
        error_subcode=463,
    )
    assert expired_at_text(error) is None
    text = describe_token_error(error)
    assert "EXPIRED" in text
    assert "facebook_pages_set_token" in text


def test_non_token_errors_get_no_explanation():
    assert describe_token_error(GraphError("Unsupported get request.", code=100)) == ""
    assert not is_token_error(GraphError("Rate limit", code=4))


def test_token_state_reports_expiry_as_a_utc_instant_and_days_left():
    now = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)
    expires = int((now.replace(day=20)).timestamp())
    state = token_state({"data": {"is_valid": True, "type": "PAGE", "expires_at": expires}}, now=now)
    assert state["expired"] is False
    assert state["never_expires"] is False
    assert state["expires_at"] == expires
    assert state["expires_at_utc"].startswith("2026-11-20T")
    assert state["days_left"] == 6
    assert "6 day(s)" in state["note"]


def test_token_state_reports_an_expired_token():
    now = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)
    state = token_state(
        {"data": {"is_valid": True, "expires_at": int(now.timestamp()) - 60}}, now=now
    )
    assert state["expired"] is True
    assert state["seconds_left"] < 0
    assert "expired" in state["note"]


def test_expires_at_zero_means_the_token_never_expires():
    # A Page token from a long-lived user token has no expiry. That is the
    # preferred state, so it must not read as expired.
    state = token_state({"data": {"is_valid": True, "type": "PAGE", "expires_at": 0}})
    assert state["never_expires"] is True
    assert state["expired"] is False
    assert "note" not in state


def test_token_state_tolerates_a_missing_or_unparsable_expiry():
    for payload in ({"data": {"is_valid": True}}, {"data": {"expires_at": "soon"}}, {}):
        state = token_state(payload)
        assert state["never_expires"] is True
        assert state["expired"] is False


@pytest.mark.parametrize("payload", [None, "text", 7, {"data": "text"}])
def test_token_state_accepts_a_payload_that_is_not_an_object(payload):
    state = token_state(payload)
    assert state["is_valid"] is False
    assert state["scopes"] == []
