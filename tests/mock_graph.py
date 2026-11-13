"""A small mock of the Facebook Graph API, used by the tests.

It answers the paths the server calls, checks the bearer token, and returns
Graph-shaped JSON: ``{"data": [...]}`` for edges, a node object for nodes, and
``{"error": {...}}`` for failures. It records every request, so tests can
assert on the path and parameters the server produced.

Requests arrive under ``/{version}/...`` exactly as they do at
``graph.facebook.com``, so one dispatcher routes them by path shape rather than
by a table of overlapping routes.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

VALID_TOKEN = "mock-page-token"
VALID_USER_TOKEN = "mock-user-token"

PAGES = [
    {
        "id": "1001",
        "name": "Prime Time IT",
        "username": "primetimeit",
        "tasks": ["ADVERTISE", "MODERATE", "CREATE_CONTENT", "ANALYZE", "PUBLISH"],
        "category": "IT Services",
    },
    {"id": "1002", "name": "Helpdesk Hotline", "username": "helpdeskhotline", "tasks": ["MODERATE"], "category": "Customer Service"},
]

PAGE = {
    "id": "1001",
    "name": "Prime Time IT",
    "about": "Managed IT for small offices.",
    "category": "IT Services",
    "username": "primetimeit",
    "link": "https://www.facebook.com/primetimeit",
    "fan_count": 4210,
    "followers_count": 4890,
    "verification_status": "verified",
}

POSTS = [
    {"id": "1001_7001", "message": "Scheduled maintenance this Saturday.", "created_time": "2026-11-09T08:00:00+0000", "shares": {"count": 3}},
    {"id": "1001_7002", "message": "New ticket portal is live.", "created_time": "2026-11-02T14:30:00+0000", "shares": {"count": 11}},
]

NEXT_POSTS = [{"id": "1001_7003", "message": "Second page of posts."}]
COMMENTS = [
    {"id": "9001", "message": "What time does maintenance start?", "like_count": 2, "from": {"name": "Casey"}},
    {"id": "9002", "message": "Portal login is broken for me.", "like_count": 0, "from": {"name": "Riley"}},
]
EDGE_ITEM = {"id": "9100", "name": "Sample photo", "link": "https://facebook.com/sample"}

DEBUG_TOKEN = {
    "data": {
        "app_id": "1234567890",
        "type": "PAGE",
        "expires_at": 1800000000,
        "is_valid": True,
        "scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts", "read_insights"],
    }
}

# Every request the server made, so tests can assert on paths and parameters.
REQUESTS: list[dict[str, Any]] = []

# Number of upcoming debug_token calls that fail with Graph error code 4
# (rate limit) before the call succeeds. Used to test client retries.
_rate_limited_calls = 0


def reset(*, rate_limited_calls: int = 0) -> None:
    global _rate_limited_calls
    _rate_limited_calls = rate_limited_calls
    REQUESTS.clear()


def find(path: str) -> dict[str, Any] | None:
    """First recorded request for a path."""
    for entry in REQUESTS:
        if entry.get("path") == path:
            return entry
    return None


def find_last(path: str) -> dict[str, Any] | None:
    """Most recent recorded request for a path, when a test calls it twice."""
    for entry in reversed(REQUESTS):
        if entry.get("path") == path:
            return entry
    return None


def _graph_error(message: str, code: int, error_type: str = "GraphMethodException") -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": error_type, "code": code, "fbtrace_id": "MockTrace"}},
        status_code=400,
    )


def _check_auth(request: Request, *, user_ok: bool = False) -> JSONResponse | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return _graph_error("An access token is required for this request.", 104, "OAuthException")
    if token == VALID_TOKEN or (user_ok and token == VALID_USER_TOKEN):
        return None
    return _graph_error("Invalid OAuth access token - cannot parse it.", 190, "OAuthException")


async def _form(request: Request) -> dict[str, Any]:
    """Graph API writes are form-encoded, not JSON bodies."""
    return dict(await request.form())


def _next_url(request: Request, path: str) -> str:
    """Absolute pagination link, built on the origin the client used."""
    return f"{request.url.scheme}://{request.url.netloc}{path}"


async def _dispatch(request: Request) -> JSONResponse:
    """Route one Graph API request by path shape, as the real API does."""
    path = request.url.path.lstrip("/")
    parts = path.split("/")
    if len(parts) < 2:
        return _graph_error("Unknown path. Expected /{version}/{path}.", 100)
    version, rest = parts[0], "/".join(parts[1:])
    segments = [s for s in rest.split("/") if s]
    method = request.method

    REQUESTS.append(
        {
            "path": rest,
            "method": method,
            "query": dict(request.query_params),
        }
    )

    # ---------------- user edges ----------------
    if segments[:2] == ["me", "accounts"]:
        if err := _check_auth(request, user_ok=True):
            return err
        if segments[-1] == "next-page":
            return JSONResponse({"data": PAGES[:1]})
        return JSONResponse({"data": PAGES, "paging": {"next": _next_url(request, f"/{version}/me/accounts/next-page")}})

    # ---------------- batch ----------------
    # Batch requests are POSTed to the version root, with the array in the
    # batch form field.
    if not segments:
        if method != "POST":
            return _graph_error("Unknown path.", 100)
        if err := _check_auth(request):
            return err
        REQUESTS[-1]["path"] = "batch"
        body = await _form(request)
        REQUESTS[-1]["form"] = body
        entries = json.loads(body.get("batch", "[]"))
        REQUESTS[-1]["entries"] = entries
        results = []
        for entry in entries:
            entry_path = entry["relative_path"]
            if entry.get("method", "GET").upper() == "GET" and entry_path.endswith("/posts"):
                results.append({"code": 200, "body": json.dumps({"data": POSTS})})
            else:
                results.append({"code": 200, "body": json.dumps({"id": entry_path.split("/")[0]})})
        return JSONResponse(results)

    # ---------------- token debugging ----------------
    if segments[0] == "debug_token":
        global _rate_limited_calls
        if err := _check_auth(request, user_ok=True):
            return err
        if _rate_limited_calls > 0:
            _rate_limited_calls -= 1
            return _graph_error("Application total call limit exceeded.", 4, "OAuthException")
        return JSONResponse(DEBUG_TOKEN)

    # ---------------- node writes ----------------
    if method == "POST" and len(segments) == 2 and segments[1] == "feed":
        body = await _form(request)
        REQUESTS[-1]["form"] = body
        if err := _check_auth(request):
            return err
        if not any(body.get(k) for k in ("message", "link", "picture")):
            return _graph_error("There was a problem posting to the Page.", 100)
        return JSONResponse({"id": f"{segments[0]}_7777"})

    if method == "POST" and len(segments) == 2 and segments[1] == "photos":
        body = await _form(request)
        REQUESTS[-1]["form"] = body
        if err := _check_auth(request):
            return err
        if not body.get("url"):
            return _graph_error("Missing url.", 100)
        return JSONResponse({"id": "8800", "post_id": f"{segments[0]}_8801"})

    if method == "POST" and len(segments) == 2 and segments[1] == "videos":
        body = await _form(request)
        REQUESTS[-1]["form"] = body
        if err := _check_auth(request):
            return err
        if not body.get("source"):
            return _graph_error("Missing source.", 100)
        return JSONResponse({"id": "8700"})

    if method == "POST" and len(segments) == 2 and segments[1] == "comments":
        body = await _form(request)
        REQUESTS[-1]["form"] = body
        if err := _check_auth(request):
            return err
        if not body.get("message"):
            return _graph_error("Missing message.", 100)
        return JSONResponse({"id": "9500"})

    # any other POST: graph_write passthrough
    if method == "POST":
        body = await _form(request)
        REQUESTS[-1]["form"] = body
        if err := _check_auth(request, user_ok=True):
            return err
        return JSONResponse({"id": "7700"})

    # ---------------- node deletes ----------------
    if method == "DELETE":
        if err := _check_auth(request):
            return err
        if segments[-1] == "undeletable":
            return _graph_error("An object with that ID does not exist.", 100)
        return JSONResponse({"success": True})

    # ---------------- reads ----------------
    if err := _check_auth(request, user_ok=segments[0] == "me"):
        return err

    if segments[-1] == "insights":
        metric = request.query_params.get("metric", "")
        if "not_a_real_metric" in metric:
            return _graph_error("(#100) The metric you requested is not supported.", 100)
        if "_" in segments[0]:  # a post id looks like {page_id}_{recipient_id}
            return JSONResponse({"data": [{"name": "post_impressions", "values": [{"value": 512}]}]})
        return JSONResponse(
            {
                "data": [
                    {
                        "name": "page_impressions",
                        "period": "day",
                        "values": [{"value": 1200, "end_time": "2026-11-08T23:00:00-0800"}],
                        "total_value": {"value": 1200},
                    }
                ]
            }
        )

    if segments[-1] == "posts":
        return JSONResponse({"data": POSTS, "paging": {"next": _next_url(request, f"/{version}/{segments[0]}/next-page")}})

    if segments[-1] == "comments":
        return JSONResponse({"data": COMMENTS})

    if segments[-1] == "next-page":
        return JSONResponse({"data": NEXT_POSTS})

    if len(segments) >= 2 and segments[-1] in ("photos", "videos", "albums", "events", "conversations", "messages"):
        return JSONResponse({"data": [EDGE_ITEM]})

    # single node: a Page id or a Post id (PageID_RecipientID)
    if len(segments) == 1:
        node = segments[0]
        if "_" in node:
            return JSONResponse(POSTS[0])
        if node == "missing":
            return _graph_error(f"Unsupported get request. Object with ID '{node}' does not exist.", 100)
        if node == "1002":
            return JSONResponse({"id": "1002", "name": "Helpdesk Hotline"})
        return JSONResponse(PAGE)

    return JSONResponse({"data": [EDGE_ITEM]})


async def handle(request: Request) -> JSONResponse:
    return await _dispatch(request)


routes = [
    Route("/{path:path}", handle, methods=["GET", "POST", "DELETE"]),
]

app = Starlette(routes=routes)
