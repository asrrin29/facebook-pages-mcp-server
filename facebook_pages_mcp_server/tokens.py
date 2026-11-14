"""Token diagnostics: explain token failures and report token expiry.

The Graph API reports an expired access token as error code 190, with the expiry
written into the message text rather than into a field. Both the tools and the
command line use these helpers, so a dead token reads as "expired on <date>" and
says how to replace it, instead of failing as a generic authentication error.

A Page access token obtained from a long-lived user token never expires, so
``expires_at`` of 0 is a normal and preferred state, not an error.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .graph import GraphError

# Graph code 190 covers every token problem: invalid, expired, revoked, wrong
# app, or the wrong kind of token for the endpoint.
TOKEN_ERROR_CODE = 190

# Graph's subcode for an expired session. It identifies that failure when the
# message text does not state a date.
EXPIRED_SESSION_SUBCODE = 463

# "Session has expired on Friday, 13-Nov-26 14:00:00 PST." The text is kept
# verbatim; Graph writes it in an abbreviated form that is not worth parsing.
_EXPIRED_RE = re.compile(r"Session has expired on\s+(?P<when>.+?)\s*\.", re.IGNORECASE)

# The text that replaces a dead token. Naming the steps is what makes the error
# actionable: the short-lived token in the environment is not repairable, only
# replaceable.
_EXPIRED_HINT = (
    "The Facebook access token EXPIRED on {when}. A new token is needed. Exchange "
    "the user token for a long-lived one (GET /oauth/access_token with "
    "grant_type=fb_exchange_token), then call me/accounts with it and use the "
    "access_token returned for the Page: a Page token from a long-lived user token "
    "does not expire. Pass the new token to facebook_pages_set_token to use it at "
    "once, or set FB_PAGE_TOKEN and restart the server."
)

_TOKEN_ERROR_HINT = (
    "The Facebook access token is not accepted. It is invalid, expired, revoked, "
    "issued to a different app, or it is a user token where the endpoint requires "
    "a Page token. Call token_debug to see which, and facebook_pages_status to see "
    "which token this server is using."
)


def expired_at_text(exc: GraphError) -> str | None:
    """The expiry Graph states in the message, or None if it does not state one."""
    match = _EXPIRED_RE.search(str(exc))
    return match.group("when") if match else None


def is_token_error(exc: GraphError) -> bool:
    """Whether a Graph failure is about the access token itself."""
    return exc.code == TOKEN_ERROR_CODE


def describe_token_error(exc: GraphError) -> str:
    """A plain-language explanation for a token failure, or "" for any other."""
    if not is_token_error(exc):
        return ""
    expired = expired_at_text(exc)
    if expired:
        return _EXPIRED_HINT.format(when=expired)
    if exc.error_subcode == EXPIRED_SESSION_SUBCODE:
        return _EXPIRED_HINT.format(when="an earlier date (Graph reports subcode 463)")
    return _TOKEN_ERROR_HINT


def _as_timestamp(value: Any) -> int | None:
    """Coerce a debug_token ``expires_at`` value to a Unix timestamp."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def token_state(debug_payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Report a token's type, scopes, and expiry from a ``debug_token`` response.

    ``expires_at`` is 0 for a token that never expires. That is the state a
    Page token from a long-lived user token has, so it is reported as
    ``never_expires`` rather than as an expired token.
    """
    data = debug_payload.get("data", debug_payload) if isinstance(debug_payload, dict) else {}
    if not isinstance(data, dict):
        data = {}

    state: dict[str, Any] = {
        "is_valid": bool(data.get("is_valid", False)),
        "type": data.get("type"),
        "app_id": data.get("app_id"),
        "scopes": data.get("scopes") or [],
    }

    expires_at = _as_timestamp(data.get("expires_at"))
    if expires_at is None or expires_at == 0:
        state["never_expires"] = True
        state["expired"] = False
        state["expires_at"] = 0
        return state

    moment = datetime.fromtimestamp(expires_at, tz=timezone.utc)
    reference = now or datetime.now(tz=timezone.utc)
    seconds_left = int((moment - reference).total_seconds())
    state["never_expires"] = False
    state["expired"] = seconds_left <= 0
    state["expires_at"] = expires_at
    state["expires_at_utc"] = moment.isoformat()
    state["seconds_left"] = seconds_left
    state["days_left"] = seconds_left // 86400
    if state["expired"]:
        state["note"] = (
            "The token has expired. Replace it with a Page token from a long-lived "
            "user token, which does not expire."
        )
    elif seconds_left < 7 * 86400:
        state["note"] = (
            f"The token expires in {state['days_left']} day(s). Replace it with a "
            "Page token from a long-lived user token, which does not expire."
        )
    return state
