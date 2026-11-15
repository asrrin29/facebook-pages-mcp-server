"""Mint a Page access token that does not expire.

A Page token inherits the lifetime of the User token it was read with. Read with
a short-lived User token, it dies within a day. Read with a long-lived User
token, it has no expiry at all. The two calls below are what produce that second
kind, and both the MCP tool and the command line use them.

    GET /oauth/access_token?grant_type=fb_exchange_token   short -> long-lived
    GET me/accounts?fields=...,access_token                Page token per Page

The exchange authenticates as the app, with the App ID and App Secret as
parameters and no bearer token. Graph refuses an expired User token here, so a
token that has already died cannot be exchanged: a new one has to be issued.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .graph import GraphClient
from .specs import PAGE_ACCESS_TOKEN_FIELDS
from .tokens import token_state


class RefreshError(ValueError):
    """The Page token could not be minted, with the reason to show a person."""


async def mint_page_token(
    client: GraphClient,
    *,
    app_id: str,
    app_secret: str,
    user_token: str,
    page_id: str | None = None,
) -> dict[str, Any]:
    """Mint the Page token for one Page and report its state.

    The result carries the Page entry, the new ``page_token``, that token's
    ``token`` state, and ``user_token_expires_at_utc`` when the exchange stated an
    expiry. The User token's own lifetime is worth reporting: it decides how long
    this result stays renewable.

    ``page_id`` selects one Page. When it is omitted, the call succeeds only if the
    User token manages exactly one Page, because guessing would install the wrong
    Page's token.

    Graph failures propagate as :class:`~facebook_pages_mcp_server.graph.GraphError`;
    a problem with the inputs or the result raises :class:`RefreshError`.
    """
    if not (app_id and app_secret):
        missing = [
            name
            for name, present in (
                ("FB_APP_ID", app_id),
                ("FB_APP_SECRET", app_secret),
            )
            if not present
        ]
        names = " and ".join(missing)
        verb = "are" if len(missing) > 1 else "is"
        raise RefreshError(
            f"{names} {verb} not set, so the token cannot be exchanged: the "
            "exchange authenticates as the app, not with an access token. Set both "
            "in the server's environment, or pass app_id and app_secret with this "
            "call. They are in your app's dashboard under Settings > Basic."
        )
    if not user_token:
        raise RefreshError(
            "No User access token to exchange. A Page token cannot be refreshed on "
            "its own: the new Page token is read from me/accounts, which a person's "
            "token calls. Pass user_token (a User token that has not expired), or "
            "set FB_USER_TOKEN. A User token is issued by the Graph API Explorer; "
            "the token in FB_PAGE_TOKEN is a Page token and cannot be exchanged."
        )

    # Graph names the token to exchange with fb_exchange_token, and takes the app
    # credentials as client_id and client_secret. No bearer token may be sent.
    exchanged = await client.get_public(
        "oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": user_token,
        },
    )
    long_lived = exchanged.get("access_token") if isinstance(exchanged, dict) else None
    if not long_lived:
        raise RefreshError(
            "The token exchange returned no access_token, so nothing was changed. "
            "Check that user_token is a User access token for the app named by "
            "FB_APP_ID, and that it has not expired."
        )

    accounts = await client.get(
        "me/accounts",
        {"fields": ",".join(PAGE_ACCESS_TOKEN_FIELDS)},
        scope="user",
        token=long_lived,
    )
    entries = (
        [e for e in accounts.get("data", []) if isinstance(e, dict)]
        if isinstance(accounts, dict)
        else []
    )
    if not entries:
        raise RefreshError(
            "me/accounts returned no Pages, so no Page token could be read. A Page "
            "token has no accounts edge, so the value passed here must be a User "
            "access token. If it is one, the person manages no Page: check that the "
            "token was issued with pages_show_list and holds a role on the Page."
        )

    chosen = _select_page(entries, page_id)

    new_token = str(chosen.get("access_token") or "").strip()
    if not new_token:
        raise RefreshError(
            f"me/accounts returned no access_token for Page {chosen.get('id')}. The "
            "User token is missing the permission that lets it act on the Page. "
            "Re-issue it with pages_show_list and the Page management permissions."
        )

    # debug_token is called with the long-lived User token, because the new Page
    # token may not be usable as the credential that authorizes a call yet.
    payload = await client.get("debug_token", {"input_token": new_token}, token=long_lived)
    result: dict[str, Any] = {
        "page": {"id": chosen.get("id"), "name": chosen.get("name")},
        "page_token": new_token,
        "token": token_state(payload),
    }

    expires_in = exchanged.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        moment = datetime.fromtimestamp(time.time() + float(expires_in), tz=timezone.utc)
        result["user_token_expires_at_utc"] = moment.isoformat()
    return result


def _select_page(entries: list[dict[str, Any]], page_id: str | None) -> dict[str, Any]:
    """Pick the Page entry whose token is wanted."""
    wanted = (page_id or "").strip()
    if wanted:
        chosen = next((e for e in entries if str(e.get("id")) == wanted), None)
        if chosen is None:
            raise RefreshError(
                f"The User token manages no Page with id {wanted}. It manages: "
                f"{_page_list(entries)}."
            )
        return chosen
    if len(entries) == 1:
        return entries[0]
    raise RefreshError(
        f"The User token manages {len(entries)} Pages, so no single Page token can "
        f"be chosen. Pass page_id. Available: {_page_list(entries)}."
    )


def _page_list(entries: list[dict[str, Any]]) -> str:
    return ", ".join(f"{e.get('id')} ({e.get('name')})" for e in entries)
