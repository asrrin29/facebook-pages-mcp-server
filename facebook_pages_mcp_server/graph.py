"""Thin async client for the Facebook Graph API.

Graph API overview (https://developers.facebook.com/docs/graph-api):

- Base URL: ``{base}/{version}/{node-or-edge-path}``, e.g.
  ``https://graph.facebook.com/v27.0/123/posts``.
- Auth: the access token rides in the ``Authorization: Bearer`` header, so it
  never appears in URLs or logs.
- Errors: ``{"error": {"message", "type", "code", "error_subcode",
  "fbtrace_id"}}``. The HTTP status is 400 for most Graph errors, so the
  error ``code`` is what identifies the failure.
- Rate limits: reported in the ``X-App-Usage`` / ``X-Page-Usage`` response
  headers. Graph error code 4 means the app quota is exhausted; code 17 or
  32 usually means a temporary or per-page limit. Both are retried.
- Pagination: edges return ``paging.next`` (a full URL with the token
  already included). Callers pass that URL to :meth:`GraphClient.get_url`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from .config import Config

logger = logging.getLogger("facebook_pages_mcp_server")

# Graph error codes that mean "try again later" rather than "your request is wrong".
RETRYABLE_ERROR_CODES: frozenset[int] = frozenset({1, 2, 4, 17, 32})
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class GraphError(RuntimeError):
    """A Graph API request failed (transport, HTTP, or Graph error object)."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        code: int | None = None,
        error_type: str | None = None,
        error_subcode: int | None = None,
        fbtrace_id: str | None = None,
        payload: Any = None,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.error_type = error_type
        self.error_subcode = error_subcode
        self.fbtrace_id = fbtrace_id
        self.payload = payload

    def as_dict(self) -> dict[str, Any]:
        """Machine-readable form, so tools can return the Graph error verbatim."""
        out: dict[str, Any] = {"error": str(self)}
        for key, value in (
            ("http_status", self.http_status),
            ("code", self.code),
            ("type", self.error_type),
            ("subcode", self.error_subcode),
            ("fbtrace_id", self.fbtrace_id),
        ):
            if value is not None:
                out[key] = value
        return out


class GraphClient:
    """Async client for one Graph API version, authenticated by bearer token."""

    def __init__(self, config: Config):
        config.require_facebook()
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.api_root,
            timeout=httpx.Timeout(config.timeout_seconds),
            verify=config.verify_ssl,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GraphClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Public request helpers
    # ------------------------------------------------------------------

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        scope: str = "page",
    ) -> Any:
        """GET a path under the versioned root (``/{page_id}/posts`` and friends)."""
        return await self._request("GET", path.lstrip("/"), query=params, scope=scope)

    async def get_url(self, url: str, *, scope: str = "page") -> Any:
        """GET an absolute URL, used to follow ``paging.next`` links."""
        return await self._request("GET", url, scope=scope)

    async def post(
        self,
        path: str,
        data: dict[str, Any] | None = None,
        *,
        query: dict[str, Any] | None = None,
        scope: str = "page",
    ) -> Any:
        """POST to a path; parameters go in the form body to keep URLs short."""
        return await self._request("POST", path.lstrip("/"), query=query, form=data, scope=scope)

    async def delete(self, path: str, *, scope: str = "page") -> Any:
        """DELETE a node (Graph delete calls take no parameters)."""
        return await self._request("DELETE", path.lstrip("/"), scope=scope)

    async def batch(self, entries: list[dict[str, Any]]) -> Any:
        """Run a batch of requests in one call to the version root.

        ``entries`` is the Graph batch array (``{"method": "GET", "relative_url":
        "..."}``). Sent as form data because the batch parameter must be
        form-encoded, not a JSON body.
        """
        return await self._request(
            "POST",
            "",
            form={"batch": json.dumps(entries), "include_headers": "false"},
            scope="page",
        )

    # ------------------------------------------------------------------
    # Core request handling
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        scope: str = "page",
    ) -> Any:
        token = self._config.token_for(scope)
        if not token:
            raise GraphError(
                f"No Facebook access token configured for a {scope}-scoped call. "
                f"Set {'FB_USER_TOKEN' if scope == 'user' else 'FB_PAGE_TOKEN'}."
            )
        headers = {"Authorization": f"Bearer {token}"}
        clean_query = _clean(query)
        clean_form = _clean(form)

        last_error: Exception | None = None
        attempts = self._config.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(
                    method, path, params=clean_query, data=clean_form, headers=headers
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < attempts:
                    await asyncio.sleep(0.5 * attempt)
                    continue
                raise GraphError(f"Request to the Graph API failed: {exc}") from exc

            payload = self._parse(response)
            graph_error = _graph_error(payload, response.status_code)

            if graph_error is not None and _should_retry(graph_error, response.status_code) and attempt < attempts:
                last_error = graph_error
                await asyncio.sleep(min(2.0 * attempt, 8.0))
                continue

            if graph_error is not None:
                raise graph_error
            if response.status_code >= 400:
                raise GraphError(
                    f"Graph API returned HTTP {response.status_code}: {_short(payload)}",
                    http_status=response.status_code,
                    payload=payload,
                )
            return payload

        raise GraphError(f"Request to the Graph API failed after {attempts} attempts: {last_error}")

    @staticmethod
    def _parse(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            body = response.text or ""
            return {"error": {"message": f"Non-JSON response: {body[:300]}", "type": "InternalServerError"}}


def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop empty values; Graph treats an empty parameter as a real value."""
    if not params:
        return None
    out = {k: v for k, v in params.items() if v is not None and v != ""}
    return out or None


def _graph_error(payload: Any, http_status: int) -> GraphError | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    message = str(error.get("message") or "Graph API error")
    code = error.get("code")
    subcode = error.get("error_subcode")
    return GraphError(
        message,
        http_status=http_status,
        code=code if isinstance(code, int) else None,
        error_type=str(error["type"]) if error.get("type") else None,
        error_subcode=subcode if isinstance(subcode, int) else None,
        fbtrace_id=str(error["fbtrace_id"]) if error.get("fbtrace_id") else None,
        payload=payload,
    )


def _should_retry(error: GraphError, http_status: int) -> bool:
    if http_status in RETRYABLE_STATUS_CODES:
        return True
    return error.code in RETRYABLE_ERROR_CODES


def _short(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
    return str(payload)[:300]
