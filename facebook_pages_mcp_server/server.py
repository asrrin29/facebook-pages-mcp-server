"""MCP server exposing Facebook Graph API (Pages) endpoints as tools.

Tool definitions live in :mod:`facebook_pages_mcp_server.specs`: each tool's
tier, description and the Graph API endpoint behind it. A tool is registered
only when its tier is allowed by ``MCP_TOOL_PERMISSIONS``, so a disallowed
tool never appears in ``tools/list`` and cannot be called.

Transports
----------
- ``stdio``: for local MCP clients (Claude Desktop, IDE plugins, ...).
  No API key is required for the MCP layer itself.
- ``streamable-http``: for remote/network access. Requires ``MCP_API_KEY``;
  clients must send ``Authorization: Bearer <MCP_API_KEY>``.

Tool permissions
----------------
``MCP_TOOL_PERMISSIONS`` gates tools by what the Graph API call does:

- ``read``   - fetches data only (never changes anything)
- ``write``  - creates or modifies data (publish, comment, upload media)
- ``delete`` - permanently removes data (delete a post or comment)

Unset defaults to ``read`` only. ``facebook_pages_status`` and
``facebook_pages_list_tools`` are always available; they describe this server
and touch no Facebook data.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import Annotated, Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AliasChoices, Field
from mcp.types import ToolAnnotations

from .config import SERVER_NAME, SERVER_VERSION, Config
from .graph import GraphClient, GraphError, call_token as graph_call_token, current_call_token
from .tokens import describe_token_error, token_state
from .specs import (
    ALBUM_FIELDS,
    COMMENT_FIELDS,
    CONVERSATION_FIELDS,
    EVENT_FIELDS,
    HELPER_TOOL_SPECS,
    INSIGHTS_DATE_PRESETS,
    INSIGHTS_METRICS,
    INSIGHTS_PERIODS,
    MESSAGE_FIELDS,
    PAGE_FIELDS,
    PAGE_SUMMARY_FIELDS,
    PHOTO_FIELDS,
    POST_DETAIL_FIELDS,
    POST_FIELDS,
    POST_INSIGHTS_METRICS,
    UNUSABLE_INSIGHTS_METRICS,
    TOOL_SPECS,
    VIDEO_FIELDS,
    join_fields,
    spec_by_name,
)

logger = logging.getLogger("facebook_pages_mcp_server")

TIER_DESCRIPTIONS: dict[str, str] = {
    "read": "read tools (fetch data only)",
    "write": "write tools (publish posts, comment, upload media)",
    "delete": "delete tools (permanently remove posts and comments)",
}

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, destructive_hint=False, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)


class ServerState:
    """Holds the Graph client for the process, and any token set at run time."""

    def __init__(self, config: Config):
        # The configuration read at start is kept so a run-time token can be
        # removed again.
        self._startup_config = config
        self.config = config
        self.token_scope_overridden: str | None = None
        self._client: GraphClient | None = None
        self._lock = asyncio.Lock()

    async def get_client(self) -> GraphClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = GraphClient(self.config)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def use_token(self, scope: str, token: str | None) -> None:
        """Use ``token`` for ``scope`` calls for the rest of this process.

        ``token`` of None restores the value read from the environment at start.
        The cached client authenticated the previous token, so it is closed and
        built again on the next call.
        """
        self.config = (
            self._startup_config
            if token is None
            else self.config.with_token(scope, token)
        )
        self.token_scope_overridden = None if token is None else scope
        await self.aclose()

    async def call(self, coro_factory: Callable[[GraphClient], Any], *, scope: str = "page") -> Any:
        """Run one Graph call, turning Graph failures into MCP tool errors.

        The client is created lazily so the server still starts, and still
        answers ``tools/list``, before the Facebook token is valid.
        """
        try:
            client = await self.get_client()
        except Exception as exc:  # a configuration problem is a tool error, not a crash
            raise ToolError(str(exc)) from exc
        try:
            return await coro_factory(client)
        except GraphError as exc:
            raise ToolError(_format_graph_error(exc)) from exc


# ---------------------------------------------------------------------------
# Argument validation helpers
# ---------------------------------------------------------------------------


# Accepted parameter names for the access token. An AI client often names this
# argument `token` or `page_token` rather than `access_token`. A call naming an
# unrecognised parameter is rejected before the handler runs, so the token is
# never sent to Facebook and the failure reads as "the token was not accepted".
# The advertised schema still names only `access_token`; these alternatives are
# accepted on the way in. `access_token` is listed first, so a call that supplies
# two of them uses the canonical name.
TOKEN_ARGUMENT_NAMES = ("access_token", "token", "page_token", "accessToken", "fb_access_token")

TokenAlias = Annotated[
    str,
    Field(validation_alias=AliasChoices(*TOKEN_ARGUMENT_NAMES)),
]


def _require(value: str | None, label: str) -> str:
    if value is None or not str(value).strip():
        hint = " Call pages_list to see the Pages this token manages." if label == "page_id" else ""
        raise ToolError(f"{label} is required.{hint}")
    return str(value).strip()


_BEARER_PREFIX = re.compile(r"^\s*bearer\s+", re.IGNORECASE)


def _normalize_token(value: str) -> str:
    """Return a pasted access token in the form the Graph API expects.

    A token copied from an HTTP header, a curl example, or a config file can
    arrive as ``Bearer EAAG...`` or wrapped in quotes. Sent as written, Graph
    rejects it and the answer reads as though the token were invalid.
    """
    token = _BEARER_PREFIX.sub("", str(value).strip())
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        token = token[1:-1].strip()
    return token


def _date_params(since: int | None, until: int | None) -> dict[str, Any]:
    if since is not None and until is not None and until < since:
        raise ToolError("until must not be earlier than since.")
    return {"since": since, "until": until}


def _metric_list(metrics: list[str] | None, tool_hint: str) -> str:
    names = [m.strip() for m in (metrics or []) if m and m.strip()]
    if not names:
        raise ToolError(f"metrics must list at least one metric name. {tool_hint}")
    # One rejected metric fails the whole Insights request, so a metric the API
    # will not accept is answered here, with its replacement, rather than costing
    # a call that returns an unexplained error. A metric outside this set still
    # reaches the API and is reported as Graph states it.
    retired = [name for name in names if name in UNUSABLE_INSIGHTS_METRICS]
    if retired:
        named = "; ".join(
            f"{name} -> {UNUSABLE_INSIGHTS_METRICS[name]}"
            if UNUSABLE_INSIGHTS_METRICS[name]
            else f"{name} (no replacement)"
            for name in retired
        )
        raise ToolError(
            f"the API rejects the metric(s) {named}; one rejected metric fails the "
            "whole request. Replace them and try again. page_insights_metrics "
            "lists current names and every name the API rejects. "
            "https://developers.facebook.com/documentation/pages-api/"
            "platforminsights/page/deprecated-metrics"
        )
    return ",".join(names)


def _paging_hint(payload: Any) -> dict[str, Any]:
    """Tell the caller whether more pages exist, and how to get them."""
    if isinstance(payload, dict) and isinstance(payload.get("paging"), dict):
        next_link = payload["paging"].get("next")
        if next_link:
            return {"more_available": True, "next_url": next_link}
    return {"more_available": False}


def _edge(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {**payload, **_paging_hint(payload)}
    return payload


def _format_graph_error(exc: GraphError) -> str:
    """Report a Graph failure with its codes, and what to do about a token failure.

    An expired token otherwise reads as a generic authentication error. Graph
    puts the expiry date in the message text, so it is quoted back with the
    steps that replace the token.
    """
    parts = [str(exc)]
    details = [
        f"{label}={value}"
        for label, value in (
            ("code", exc.code),
            ("subcode", exc.error_subcode),
            ("type", exc.error_type),
            ("http_status", exc.http_status),
            ("fbtrace_id", exc.fbtrace_id),
        )
        if value is not None
    ]
    if details:
        parts.append(" ".join(details))
    explanation = describe_token_error(exc)
    if explanation:
        parts.append(explanation)
    return " | ".join(parts)


def _tool_description(name: str) -> str:
    spec = spec_by_name(name)
    if spec is None:  # guards a catalog typo at import time
        raise RuntimeError(f"missing tool spec for {name!r}")
    parts = [spec.summary, spec.description, f"Graph API: {spec.api}."]
    if spec.notes:
        parts.append(spec.notes)
    return " ".join(parts)


def _extract_call_token(arguments: dict[str, Any] | None) -> str | None:
    """Return the access token supplied with a tool call, and remove it.

    A caller may hold a token this server was not configured with, and a
    configured token can expire while the process runs, so every tool accepts an
    access token with the request under any of ``TOKEN_ARGUMENT_NAMES``. The
    value is removed from the arguments here: it is used for the request's
    ``Authorization`` header, and must not be sent to Graph as a parameter named
    ``access_token``.
    """
    if not arguments:
        return None
    token: str | None = None
    for name in TOKEN_ARGUMENT_NAMES:
        value = arguments.pop(name, None)
        if token is None and isinstance(value, str) and value.strip():
            token = _normalize_token(value)
    return token


# Added to every tool's advertised schema. An AI client reaches for this name
# first, and a parameter the client cannot see is a parameter it will not send.
CALL_TOKEN_SCHEMA: dict[str, Any] = {
    "anyOf": [{"type": "string"}, {"type": "null"}],
    "default": None,
    "description": (
        "Optional Facebook Page access token for this call only. Use it when the "
        "token you hold is not the one this server was configured with, or when "
        "the configured token has expired. Takes effect for this call and nothing "
        "else. Also accepted as token or page_token."
    ),
}


def _advertise_call_token(mcp: MCPServer) -> None:
    """Show the optional per-call access token on every tool that makes a call.

    The parameter is handled before any tool runs, so it is absent from the tool
    signatures and would otherwise be invisible to a client that follows the
    advertised schema. ``facebook_pages_set_token`` is skipped: it declares the
    parameter itself, because its job is to keep the token.
    """
    for tool in mcp._tool_manager.list_tools():
        if tool.name == "facebook_pages_set_token":
            continue
        properties = tool.parameters.get("properties")
        if isinstance(properties, dict):
            properties["access_token"] = dict(CALL_TOKEN_SCHEMA)


def _permission_summary(config: Config) -> str:
    if not config.tool_permissions:
        return "NO Facebook tools are enabled (set MCP_TOOL_PERMISSIONS to read, write, or delete)"
    return "enabled: " + ", ".join(TIER_DESCRIPTIONS[t] for t in config.tool_permissions)


def build_server(config: Config) -> tuple[MCPServer, ServerState]:
    """Create the MCPServer with the Facebook Pages tools the permissions allow."""
    state = ServerState(config)

    @asynccontextmanager
    async def lifespan(server: MCPServer):
        yield state
        await state.aclose()

    mcp = MCPServer(
        name=SERVER_NAME,
        title="Facebook Pages MCP Server",
        description=(
            "MCP server for the Facebook Graph API (Pages). Lists the Pages a person "
            "manages and reads Page details, posts, comments, photos, videos, events, "
            "message conversations and Insights. With the write permission it can publish "
            "posts, comment and upload media; with the delete permission it can remove "
            "posts and comments."
        ),
        instructions=(
            "Start with pages_list or pages_find to get a page_id, then read Page data "
            "with page_get, page_posts_list, post_comments_list, page_insights and the "
            "other read tools. The Graph API returns only the fields you ask for; read "
            "tools ship sensible defaults and accept fields or extra_fields to change "
            "them. Edges are paginated: when more_available is true, pass next_url to "
            "graph_follow. Insights need metric names; page_insights_metrics lists names "
            "worth trying. A Facebook access token normally comes from the server "
            "environment. Any tool also accepts an access_token argument, which is used "
            "for that call alone: pass the token you hold when the configured one is "
            "expired or is not yours. "
            f"Tool permissions for this server ({_permission_summary(config)})."
        ),
        version=SERVER_VERSION,
        lifespan=lifespan,
    )

    # ---------------------------------------------------------------
    # Read tools
    # ---------------------------------------------------------------

    async def pages_list(
        limit: int | None = None,
        after: str | None = None,
        fields: list[str] | None = None,
        extra_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "fields": join_fields(PAGE_SUMMARY_FIELDS, fields, extra_fields),
            "limit": limit,
            "after": after,
        }
        payload = await state.call(lambda c: c.get("me/accounts", params, scope="user"), scope="user")
        return _edge(payload)

    async def pages_find(query: str, limit: int | None = None) -> dict[str, Any]:
        needle = (query or "").strip().lower()
        if not needle:
            raise ToolError("query must not be empty.")
        params: dict[str, Any] = {"fields": join_fields(PAGE_SUMMARY_FIELDS, None, None), "limit": limit or 50}
        payload = await state.call(lambda c: c.get("me/accounts", params, scope="user"), scope="user")
        pages = payload.get("data", []) if isinstance(payload, dict) else []
        matches = [
            page
            for page in pages
            if needle in str(page.get("name", "")).lower() or needle in str(page.get("username", "")).lower()
        ]
        return {"query": query, "count": len(matches), "data": matches, **_paging_hint(payload)}

    async def page_get(
        page_id: str,
        fields: list[str] | None = None,
        extra_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        pid = _require(page_id, "page_id")
        params = {"fields": join_fields(PAGE_FIELDS, fields, extra_fields)}
        return await state.call(lambda c: c.get(pid, params))

    async def page_posts_list(
        page_id: str,
        limit: int | None = None,
        after: str | None = None,
        since: int | None = None,
        until: int | None = None,
        include_own: bool | None = None,
        fields: list[str] | None = None,
        extra_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        pid = _require(page_id, "page_id")
        params: dict[str, Any] = {
            "fields": join_fields(POST_FIELDS, fields, extra_fields),
            "limit": limit,
            "after": after,
            "include_own": include_own,
            **_date_params(since, until),
        }
        payload = await state.call(lambda c: c.get(f"{pid}/posts", params))
        return _edge(payload)

    async def post_get(
        post_id: str,
        fields: list[str] | None = None,
        extra_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        nid = _require(post_id, "post_id")
        params = {"fields": join_fields(POST_DETAIL_FIELDS, fields, extra_fields)}
        return await state.call(lambda c: c.get(nid, params))

    async def post_comments_list(
        post_id: str,
        limit: int | None = None,
        after: str | None = None,
        order: str | None = None,
        filter: str | None = None,
        fields: list[str] | None = None,
        extra_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        nid = _require(post_id, "post_id")
        params: dict[str, Any] = {
            "fields": join_fields(COMMENT_FIELDS, fields, extra_fields),
            "limit": limit,
            "after": after,
            "order": order,
            "filter": filter,
        }
        payload = await state.call(lambda c: c.get(f"{nid}/comments", params))
        return _edge(payload)

    async def page_insights(
        page_id: str,
        metrics: list[str],
        period: str | None = None,
        date_preset: str | None = None,
        since: int | None = None,
        until: int | None = None,
        breakdown: list[str] | None = None,
        total: bool | None = None,
    ) -> dict[str, Any]:
        pid = _require(page_id, "page_id")
        params: dict[str, Any] = {
            "metric": _metric_list(metrics, "Call page_insights_metrics for names to try."),
            "period": period,
            "date_preset": date_preset,
            "breakdown": ",".join(b.strip() for b in breakdown if b and b.strip()) if breakdown else None,
            "total": total,
            **_date_params(since, until),
        }
        payload = await state.call(lambda c: c.get(f"{pid}/insights", params))
        return _edge(payload)

    async def post_insights(
        post_id: str,
        metrics: list[str],
        period: str | None = None,
        breakdown: list[str] | None = None,
    ) -> dict[str, Any]:
        nid = _require(post_id, "post_id")
        params: dict[str, Any] = {
            "metric": _metric_list(metrics, "Call page_insights_metrics for post metric names."),
            "period": period,
            "breakdown": ",".join(b.strip() for b in breakdown if b and b.strip()) if breakdown else None,
        }
        payload = await state.call(lambda c: c.get(f"{nid}/insights", params))
        return _edge(payload)

    async def page_insights_metrics(group: str | None = None) -> dict[str, Any]:
        groups = INSIGHTS_METRICS
        if group:
            key = group.strip().lower()
            if key not in groups:
                raise ToolError(f"unknown group {group!r}. Available groups: {', '.join(sorted(groups))}")
            groups = {key: groups[key]}
        return {
            "page_metrics_by_group": {name: list(metrics) for name, metrics in groups.items()},
            "post_metrics": list(POST_INSIGHTS_METRICS),
            # One map keyed by every name the API will not accept, so a metric a
            # caller supplies resolves here whatever its value. The value is the
            # replacement Meta names, or "no replacement" where none is known.
            "retired_metrics": {
                name: (replacement or "no replacement")
                for name, replacement in UNUSABLE_INSIGHTS_METRICS.items()
            },
            "periods": list(INSIGHTS_PERIODS),
            "date_presets": list(INSIGHTS_DATE_PRESETS),
            "note": (
                "Pages under 100 likes return no insights. A retired metric fails the whole "
                "request, so do not add one from retired_metrics; use its replacement "
                "instead. Insights cover at most 90 days per since/until range, and "
                "demographic metrics need data for at least 100 people. "
                "https://developers.facebook.com/documentation/pages-api/platforminsights/"
                "page/deprecated-metrics is the list the API enforces."
            ),
        }

    async def page_photos_list(
        page_id: str,
        limit: int | None = None,
        after: str | None = None,
        since: int | None = None,
        until: int | None = None,
        fields: list[str] | None = None,
        extra_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        pid = _require(page_id, "page_id")
        params: dict[str, Any] = {
            "fields": join_fields(PHOTO_FIELDS, fields, extra_fields),
            "limit": limit,
            "after": after,
            **_date_params(since, until),
        }
        payload = await state.call(lambda c: c.get(f"{pid}/photos", params))
        return _edge(payload)

    async def page_videos_list(
        page_id: str,
        limit: int | None = None,
        after: str | None = None,
        fields: list[str] | None = None,
        extra_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        pid = _require(page_id, "page_id")
        params: dict[str, Any] = {
            "fields": join_fields(VIDEO_FIELDS, fields, extra_fields),
            "limit": limit,
            "after": after,
        }
        payload = await state.call(lambda c: c.get(f"{pid}/videos", params))
        return _edge(payload)

    async def page_albums_list(
        page_id: str,
        limit: int | None = None,
        after: str | None = None,
        fields: list[str] | None = None,
        extra_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        pid = _require(page_id, "page_id")
        params: dict[str, Any] = {
            "fields": join_fields(ALBUM_FIELDS, fields, extra_fields),
            "limit": limit,
            "after": after,
        }
        payload = await state.call(lambda c: c.get(f"{pid}/albums", params))
        return _edge(payload)

    async def page_events_list(
        page_id: str,
        limit: int | None = None,
        after: str | None = None,
        time: str | None = None,
        fields: list[str] | None = None,
        extra_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        pid = _require(page_id, "page_id")
        params: dict[str, Any] = {
            "fields": join_fields(EVENT_FIELDS, fields, extra_fields),
            "limit": limit,
            "after": after,
            "time": time,
        }
        payload = await state.call(lambda c: c.get(f"{pid}/events", params))
        return _edge(payload)

    async def page_conversations_list(
        page_id: str,
        limit: int | None = None,
        after: str | None = None,
        fields: list[str] | None = None,
        extra_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        pid = _require(page_id, "page_id")
        params: dict[str, Any] = {
            "fields": join_fields(CONVERSATION_FIELDS, fields, extra_fields),
            "limit": limit,
            "after": after,
        }
        payload = await state.call(lambda c: c.get(f"{pid}/conversations", params))
        return _edge(payload)

    async def conversation_messages_list(
        conversation_id: str,
        limit: int | None = None,
        after: str | None = None,
        fields: list[str] | None = None,
        extra_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        cid = _require(conversation_id, "conversation_id")
        params: dict[str, Any] = {
            "fields": join_fields(MESSAGE_FIELDS, fields, extra_fields),
            "limit": limit,
            "after": after,
        }
        payload = await state.call(lambda c: c.get(f"{cid}/messages", params))
        return _edge(payload)

    async def token_debug(input_token: str | None = None) -> dict[str, Any]:
        # Graph requires input_token; without it the call always fails. The
        # default subject is the token this call is authenticated with: a token
        # supplied with the request, then a token set at run time, then the
        # configured one. Reading the configuration captured at start would
        # inspect a token that is no longer in use.
        subject = (
            (input_token or "").strip()
            or current_call_token()
            or state.config.page_token
        )
        if not subject:
            raise ToolError(
                "input_token is required and no FB_PAGE_TOKEN is configured to inspect instead."
            )
        return await state.call(lambda c: c.get("debug_token", {"input_token": subject}))

    async def graph_batch(requests: list[dict[str, Any]]) -> Any:
        # Graph names the target of a batch entry `relative_url`. An entry that
        # names anything else has no target, and Graph answers it as a request on
        # the version root: "Unsupported get request".
        entries: list[dict[str, Any]] = []
        for raw in requests or []:
            if not isinstance(raw, dict):
                raise ToolError("each batch entry must be an object with method and relative_url.")
            target = str(raw.get("relative_url") or raw.get("relative_path") or "").strip()
            if not target:
                raise ToolError(
                    "each batch entry needs a relative_url, the Graph path to fetch "
                    "(for example '1001/posts')."
                )
            method = str(raw.get("method", "GET")).upper()
            if method not in ("GET", "POST", "DELETE"):
                raise ToolError(f"unsupported method {method!r} in a batch entry.")
            needed = "delete" if method == "DELETE" else "write"
            if method != "GET" and not state.config.allows_action(needed):
                raise ToolError(
                    f"a {method} batch entry needs the {needed} permission "
                    f"(MCP_TOOL_PERMISSIONS={','.join(state.config.tool_permissions) or '<none>'})."
                )
            entry = dict(raw)
            entry.pop("relative_path", None)
            entry["method"] = method
            entry["relative_url"] = target.lstrip("/")
            entries.append(entry)
        if not entries:
            raise ToolError("requests must be a non-empty list of batch entry objects.")
        # Graph returns a bare JSON array here. Wrapping it keeps the tool
        # result an object, which MCP clients and the SDK handle consistently.
        results = await state.call(lambda c: c.batch(entries))
        return {"results": results if isinstance(results, list) else [results]}

    async def graph_follow(url: str) -> dict[str, Any]:
        target = (url or "").strip()
        allowed_prefixes = ("https://graph.facebook.com/", f"{state.config.graph_base_url}/")
        if not target.startswith(allowed_prefixes):
            raise ToolError(
                f"url must be a Graph API URL from a previous result (it starts with "
                f"{state.config.graph_base_url}/), got {url!r}."
            )
        payload = await state.call(lambda c: c.get_url(target))
        return _edge(payload)

    # ---------------------------------------------------------------
    # Write tools
    # ---------------------------------------------------------------

    async def page_publish(
        page_id: str,
        message: str | None = None,
        link: str | None = None,
        picture: str | None = None,
        published: bool | None = None,
        scheduled_publish_time: int | None = None,
    ) -> dict[str, Any]:
        pid = _require(page_id, "page_id")
        if not any((message, link, picture)):
            raise ToolError("provide at least one of message, link, or picture.")
        if scheduled_publish_time is not None and published is not False:
            raise ToolError(
                "scheduling needs published=false alongside scheduled_publish_time; "
                "the Graph API rejects a scheduled post that is also published."
            )
        data: dict[str, Any] = {
            "message": message,
            "link": link,
            "picture": picture,
            "published": None if published is None else str(published).lower(),
            "scheduled_publish_time": scheduled_publish_time,
        }
        return await state.call(lambda c: c.post(f"{pid}/feed", data))

    async def post_comment_create(post_id: str, message: str) -> dict[str, Any]:
        nid = _require(post_id, "post_id")
        if not message or not message.strip():
            raise ToolError("message must not be empty.")
        return await state.call(lambda c: c.post(f"{nid}/comments", {"message": message}))

    async def page_photo_publish(
        page_id: str,
        url: str,
        caption: str | None = None,
        published: bool | None = None,
    ) -> dict[str, Any]:
        pid = _require(page_id, "page_id")
        image_url = (url or "").strip()
        if not image_url:
            raise ToolError("url must be a public image URL.")
        data: dict[str, Any] = {
            "url": image_url,
            "caption": caption,
            "published": None if published is None else str(published).lower(),
        }
        return await state.call(lambda c: c.post(f"{pid}/photos", data))

    async def page_video_publish(
        page_id: str,
        source: str,
        title: str | None = None,
        description: str | None = None,
        feed_description: str | None = None,
        unpublished: bool | None = None,
    ) -> dict[str, Any]:
        pid = _require(page_id, "page_id")
        video_url = (source or "").strip()
        if not video_url:
            raise ToolError("source must be a public video URL.")
        data: dict[str, Any] = {
            "source": video_url,
            "title": title,
            "description": description,
            "feed_description": feed_description,
            "unpublished": None if unpublished is None else str(unpublished).lower(),
        }
        return await state.call(lambda c: c.post(f"{pid}/videos", data))

    async def graph_write(path: str, params: dict[str, Any] | None = None) -> Any:
        """One Graph API POST for write edges this server does not wrap."""
        clean_path = (path or "").strip().lstrip("/")
        if not clean_path:
            raise ToolError("path is required, for example 123456789/videos.")
        scope = "user" if clean_path.split("/")[0] == "me" else "page"
        return await state.call(lambda c: c.post(clean_path, params or {}, scope=scope), scope=scope)

    # ---------------------------------------------------------------
    # Delete tools
    # ---------------------------------------------------------------

    async def comment_delete(comment_id: str) -> Any:
        cid = _require(comment_id, "comment_id")
        return await state.call(lambda c: c.delete(cid))

    async def post_delete(post_id: str) -> Any:
        nid = _require(post_id, "post_id")
        return await state.call(lambda c: c.delete(nid))

    # ---------------------------------------------------------------
    # Helper tools: describe this server, never Facebook
    # ---------------------------------------------------------------

    async def facebook_pages_status(ping: bool = True) -> dict[str, Any]:
        cfg = state.config
        info: dict[str, Any] = {
            "graph_api_root": cfg.api_root,
            "graph_version": cfg.graph_version,
            "server_version": SERVER_VERSION,
            "page_token_configured": bool(cfg.page_token),
            "user_token_configured": bool(cfg.user_token),
            "tool_permissions": list(cfg.tool_permissions),
            "verify_ssl": cfg.verify_ssl,
            "timeout_seconds": cfg.timeout_seconds,
            "ready": bool(cfg.page_token),
        }
        if state.token_scope_overridden:
            info["token_source"] = (
                f"a {state.token_scope_overridden}-scope token set at run time by "
                "facebook_pages_set_token; a restart returns to the configured value"
            )
        if not info["ready"]:
            info["hint"] = (
                "Set FB_PAGE_TOKEN to a Facebook Page access token, or pass a token "
                "to facebook_pages_set_token, to enable Facebook calls."
            )
            return info
        if not ping:
            return info
        # A token that is configured is not a token that works. An expired Page
        # token otherwise shows here as ready: true until some other call fails.
        # debug_token is one cheap call, so the status is reported live by default.
        # The token inspected is the one this call would use: a token supplied
        # with the request, otherwise the one in force for the process.
        subject = current_call_token() or cfg.page_token
        try:
            payload = await state.call(
                lambda c: c.get("debug_token", {"input_token": subject})
            )
        except ToolError as exc:
            info["ready"] = False
            info["token"] = {"ok": False, "error": str(exc)}
            return info
        info["token"] = {"ok": True, **token_state(payload)}
        if info["token"].get("expired"):
            info["ready"] = False
            info["hint"] = (
                "The configured token has expired. Mint a new one and pass it to "
                "facebook_pages_set_token, or set FB_PAGE_TOKEN and restart."
            )
        return info

    async def facebook_pages_set_token(
        access_token: TokenAlias,
        scope: str = "page",
    ) -> dict[str, Any]:
        """Verify a pasted token, then use it for the rest of this process."""
        token = _normalize_token(_require(access_token, "access_token"))
        wanted = scope.strip().lower()
        if wanted not in ("page", "user"):
            raise ToolError("scope must be 'page' (replaces FB_PAGE_TOKEN) or 'user' (replaces FB_USER_TOKEN).")

        # The new token is applied before it is checked, because the check must
        # be made with it. A token debug_token rejects is removed again, so a
        # typo cannot leave every later call failing.
        await state.use_token(wanted, token)
        try:
            # scope selects which configured token authenticates this call, so the
            # token just supplied is also the caller on debug_token.
            payload = await state.call(
                lambda c: c.get("debug_token", {"input_token": token}, scope=wanted),
                scope=wanted,
            )
        except ToolError as exc:
            await state.use_token(wanted, None)
            raise ToolError(
                f"The token was not accepted, so it was not applied: {exc}"
            ) from exc

        state_report = token_state(payload)
        if not state_report["is_valid"]:
            await state.use_token(wanted, None)
            raise ToolError(
                "debug_token reports this token is not valid, so it was not applied. "
                "Check that it is a Page access token for the Page you want."
            )
        if state_report["expired"]:
            await state.use_token(wanted, None)
            raise ToolError(
                f"The token expired at {state_report['expires_at_utc']}, so it was not "
                "applied. Mint a new one: exchange the user token for a long-lived "
                "one, then call me/accounts and use the access_token returned for the "
                "Page. That kind of Page token does not expire."
            )

        logger.info(
            "Using a %s-scope access token set at run time (Graph API %s)",
            wanted,
            state.config.graph_version,
        )
        return {
            "applied": True,
            "scope": wanted,
            "token": state_report,
            "note": (
                "Every later call in this process uses this token. It is not stored "
                "in the environment, so a restart goes back to the configured value. "
                "Set FB_PAGE_TOKEN to keep it."
            ),
        }

    async def facebook_pages_list_tools() -> dict[str, Any]:
        return {
            "tool_permissions": list(state.config.tool_permissions),
            "tools": [
                {
                    "tool": spec.name,
                    "tier": spec.tier,
                    "graph_api": spec.api,
                    "description": spec.summary,
                    **({"requirements": spec.notes} if spec.notes else {}),
                }
                for spec in (*TOOL_SPECS, *HELPER_TOOL_SPECS)
                if spec.tier == "helper" or state.config.allows_action(spec.tier)
            ],
        }

    # ---------------------------------------------------------------
    # Register only the tools the permissions allow
    # ---------------------------------------------------------------

    handlers: dict[str, Callable[..., Any]] = {
        "pages_list": pages_list,
        "pages_find": pages_find,
        "page_get": page_get,
        "page_posts_list": page_posts_list,
        "post_get": post_get,
        "post_comments_list": post_comments_list,
        "page_insights": page_insights,
        "post_insights": post_insights,
        "page_insights_metrics": page_insights_metrics,
        "page_photos_list": page_photos_list,
        "page_videos_list": page_videos_list,
        "page_albums_list": page_albums_list,
        "page_events_list": page_events_list,
        "page_conversations_list": page_conversations_list,
        "conversation_messages_list": conversation_messages_list,
        "token_debug": token_debug,
        "graph_batch": graph_batch,
        "graph_follow": graph_follow,
        "page_publish": page_publish,
        "post_comment_create": post_comment_create,
        "page_photo_publish": page_photo_publish,
        "page_video_publish": page_video_publish,
        "graph_write": graph_write,
        "comment_delete": comment_delete,
        "post_delete": post_delete,
        "facebook_pages_status": facebook_pages_status,
        "facebook_pages_set_token": facebook_pages_set_token,
        "facebook_pages_list_tools": facebook_pages_list_tools,
    }

    # Extra entries that are not in the spec catalog need a spec to be described.
    missing = set(handlers) - {s.name for s in (*TOOL_SPECS, *HELPER_TOOL_SPECS)}
    if missing:
        raise RuntimeError(f"tools without a spec entry: {', '.join(sorted(missing))}")
    unused = {s.name for s in TOOL_SPECS} - set(handlers)
    if unused:
        raise RuntimeError(f"spec entries without a handler: {', '.join(sorted(unused))}")

    tier_annotations = {"read": READ, "write": WRITE, "delete": DELETE}
    registered: list[str] = []
    gated_out: list[str] = []
    for spec in (*TOOL_SPECS, *HELPER_TOOL_SPECS):
        if spec.tier != "helper" and not config.allows_action(spec.tier):
            gated_out.append(spec.name)
            continue
        handler = handlers[spec.name]
        mcp.add_tool(
            handler,
            name=spec.name,
            description=_tool_description(spec.name),
            annotations=tier_annotations.get(spec.tier),
        )
        registered.append(spec.name)

    # Both transports reach tools through call_tool, so the per-call access token
    # is read and applied here rather than in each tool. Every Graph request the
    # tool makes while the block is open authenticates with that token; nothing
    # the server was configured with is changed, and the token is gone when the
    # call ends.
    call_tool = mcp.call_tool

    async def call_tool_with_token(
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
    ) -> Any:
        # facebook_pages_set_token declares the parameter itself, because keeping
        # the token is its job. Every other tool has it removed here so no tool
        # can forward it to Graph as a parameter named access_token.
        token = None if name == "facebook_pages_set_token" else _extract_call_token(arguments)
        with graph_call_token(token):
            return await call_tool(name, arguments, context)

    mcp.call_tool = call_tool_with_token

    _advertise_call_token(mcp)

    logger.info(
        "Registered %d Facebook tools, gated out %d (MCP_TOOL_PERMISSIONS=%s)",
        len(registered),
        len(gated_out),
        ",".join(config.tool_permissions) or "<none>",
    )

    return mcp, state
