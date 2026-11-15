"""Unit tests for configuration, the Graph client, and the field helpers."""

from __future__ import annotations

import asyncio
import json

import pytest
import uvicorn

from facebook_pages_mcp_server.config import (
    DEFAULT_GRAPH_VERSION,
    Config,
    ConfigError,
    load_config,
)
from facebook_pages_mcp_server.graph import GraphClient, GraphError, call_token
from facebook_pages_mcp_server.specs import (
    INSIGHTS_METRICS,
    PAGE_FIELDS,
    POST_DETAIL_FIELDS,
    POST_INSIGHTS_METRICS,
    POST_FIELDS,
    UNUSABLE_INSIGHTS_METRICS,
    join_fields,
)
from tests import mock_graph

MOCK_ORIGIN = "https://graph.example.test"


def _config(**overrides) -> Config:
    base = dict(
        page_token=mock_graph.VALID_TOKEN,
        graph_base_url=MOCK_ORIGIN,
        graph_version="v27.0",
        timeout_seconds=10.0,
    )
    base.update(overrides)
    return Config(**base)


class MockGraph:
    """Runs tests.mock_graph.app on a free localhost port."""

    def __init__(self):
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.config = uvicorn.Config(mock_graph.app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(self.config)
        self._task: asyncio.Task | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self):
        self._task = asyncio.create_task(self.server.serve())
        for _ in range(200):
            if self.server.started:
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("mock Graph API did not start")

    async def stop(self):
        self.server.should_exit = True
        if self._task:
            await self._task


@pytest.fixture()
async def graph():
    m = MockGraph()
    await m.start()
    mock_graph.reset()
    yield m
    await m.stop()


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


def test_load_config_reads_environment(monkeypatch):
    monkeypatch.setenv("FB_PAGE_TOKEN", "  page-token  ")
    monkeypatch.setenv("FB_GRAPH_VERSION", "24.0")
    monkeypatch.setenv("MCP_TOOL_PERMISSIONS", "write,read")
    monkeypatch.delenv("FB_USER_TOKEN", raising=False)
    cfg = load_config()
    assert cfg.page_token == "page-token"
    assert cfg.graph_version == "v24.0"  # a bare number gets the v prefix
    assert cfg.api_root.endswith("/v24.0")
    assert cfg.tool_permissions == ("read", "write")  # canonical order, de-duplicated


def test_default_graph_version_is_current():
    # Meta issues app tokens only for the current version line, so the default
    # has to be a version a new app can actually be created on.
    assert DEFAULT_GRAPH_VERSION == "v27.0"


def test_graph_version_shape_is_checked(monkeypatch):
    # Graph itself rejects a bare major: /v27/me returns "Unknown path components".
    for bad in ("latest", "v", "vx.y", "27-beta", "v27"):
        monkeypatch.setenv("FB_GRAPH_VERSION", bad)
        with pytest.raises(ConfigError, match="FB_GRAPH_VERSION"):
            load_config()


def test_blank_graph_version_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("FB_PAGE_TOKEN", "page-token")
    monkeypatch.setenv("FB_GRAPH_VERSION", "   ")
    assert load_config().graph_version == DEFAULT_GRAPH_VERSION


def test_missing_page_token_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("FB_PAGE_TOKEN", raising=False)
    cfg = load_config()
    with pytest.raises(ConfigError, match="FB_PAGE_TOKEN"):
        cfg.require_facebook()


def test_unknown_permission_level_is_rejected(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PERMISSIONS", "read,bogus")
    with pytest.raises(ConfigError, match="bogus"):
        load_config()


def test_default_permissions_are_read_only(monkeypatch):
    monkeypatch.delenv("MCP_TOOL_PERMISSIONS", raising=False)
    cfg = load_config()
    assert cfg.tool_permissions == ("read",)
    assert cfg.allows_action("read") and not cfg.allows_action("write")


def test_token_selection_falls_back_to_page_token():
    cfg = _config(user_token="user-token")
    assert cfg.token_for("user") == "user-token"
    assert cfg.token_for("page") == mock_graph.VALID_TOKEN
    without_user = _config()
    assert without_user.token_for("user") == mock_graph.VALID_TOKEN


def test_multiple_mcp_api_keys_are_split():
    cfg = _config(mcp_api_key="key-one, key-two ,")
    assert cfg.mcp_api_keys == ("key-one", "key-two")


def test_app_credentials_are_read_from_the_environment(monkeypatch):
    """Both app credentials are needed, and neither is enough on its own.

    The token exchange authenticates as the app, so a half-set pair cannot do
    anything. Reading them is what makes facebook_pages_refresh_token possible.
    """
    monkeypatch.setenv("FB_PAGE_TOKEN", "page-token")
    monkeypatch.setenv("FB_APP_ID", "  1234567890  ")
    monkeypatch.setenv("FB_APP_SECRET", "app-secret")
    cfg = load_config()
    assert cfg.app_id == "1234567890"
    assert cfg.app_secret == "app-secret"
    assert cfg.app_credentials_configured is True

    monkeypatch.delenv("FB_APP_SECRET", raising=False)
    cfg = load_config()
    assert cfg.app_credentials_configured is False
    # Graph answers a half-set pair as a bad secret, so the name of the variable
    # that is empty is the only useful thing to report.
    assert cfg.missing_app_credential == "FB_APP_SECRET"

    monkeypatch.delenv("FB_APP_ID", raising=False)
    assert load_config().missing_app_credential is None

    monkeypatch.setenv("FB_APP_SECRET", "app-secret")
    assert load_config().missing_app_credential == "FB_APP_ID"

    assert _config().app_credentials_configured is False
    assert _config().missing_app_credential is None


# ----------------------------------------------------------------------
# Field selection
# ----------------------------------------------------------------------


def test_join_fields_defaults_and_extras():
    out = join_fields(("id", "name"), None, ["fan_count"])
    assert out == "id,name,fan_count"


def test_join_fields_replaces_defaults_when_fields_given():
    out = join_fields(PAGE_FIELDS, ["id", "name"], None)
    assert out == "id,name"


def test_join_fields_keeps_one_entry_per_field_name():
    # Graph allows one entry per field name; a second entry for the same name is
    # a syntax error (code 2500). The entry already present wins because it
    # carries the modifiers this server chose.
    out = join_fields(
        ("comments.summary(true).filter(toplevel).limit(3)", "likes.summary(true)"),
        None,
        ["comments", "likes", "shares"],
    )
    assert out == "comments.summary(true).filter(toplevel).limit(3),likes.summary(true),shares"


def test_offered_insights_metrics_are_not_retired():
    # One rejected metric fails the whole Insights request, so the catalog must
    # never offer a name that the API rejects.
    offered = [m for group in INSIGHTS_METRICS.values() for m in group] + list(POST_INSIGHTS_METRICS)
    assert not [m for m in offered if m in UNUSABLE_INSIGHTS_METRICS]
    # The mock rejects the names a live Page was observed rejecting.
    assert mock_graph.REJECTED_INSIGHTS_METRICS <= set(UNUSABLE_INSIGHTS_METRICS)


def test_post_field_defaults_omit_link():
    # `link` on a Post fails the whole request with Graph error 12, naming
    # deprecate_post_aggregated_fields_for_attachement. The error text points at
    # attachments, which is not the field at fault: `attachments` is accepted on
    # both a post node and the /posts edge, and `link` is rejected on both.
    # `permalink_url` answers the same need and is kept.
    assert "link" not in POST_FIELDS
    assert "link" not in POST_DETAIL_FIELDS
    assert "permalink_url" in POST_FIELDS
    # Kept out of the list default to keep list responses small, not because the
    # API rejects it.
    assert "attachments" not in POST_FIELDS
    assert "attachments" in POST_DETAIL_FIELDS


def test_join_fields_drops_duplicates_and_blanks():
    out = join_fields(("id",), ["id", " name ", ""], ["id", "link"])
    assert out == "id,name,link"


# ----------------------------------------------------------------------
# Graph client
# ----------------------------------------------------------------------


async def test_get_sends_bearer_token_and_parses_data(graph):
    async with GraphClient(_config(graph_base_url=graph.base_url)) as client:
        payload = await client.get("1001", {"fields": "id,name"})
    assert payload["name"] == "Prime Time IT"
    entry = mock_graph.find("1001")
    assert entry["query"]["fields"] == "id,name"


async def test_a_supplied_token_overrides_the_configured_one(graph):
    """A token carried by the request is the token the request uses.

    The configured token is a default. A caller that holds a token must be able to
    use it without changing the process, which is the case where a token supplied
    through a prompt was ignored and every call reported the configured token's
    error.
    """
    config = _config(graph_base_url=graph.base_url, page_token=mock_graph.EXPIRED_TOKEN)
    async with GraphClient(config) as client:
        with call_token(mock_graph.FRESH_TOKEN):
            payload = await client.get("1001", {"fields": "id,name"})
        assert payload["name"] == "Prime Time IT"
        entry = mock_graph.find_last("1001")
        assert entry["headers"]["authorization"] == f"Bearer {mock_graph.FRESH_TOKEN}"
        # The token authorizes the call; it is not a Graph parameter.
        assert "access_token" not in entry["query"]

        # Outside the context the configured token is used again, unchanged.
        with pytest.raises(GraphError):
            await client.get("1001", {"fields": "id,name"})
        assert mock_graph.find_last("1001")["headers"]["authorization"] == (
            f"Bearer {mock_graph.EXPIRED_TOKEN}"
        )

        # An explicit argument does the same without a context, and wins over the
        # context value so a wrapped call can still name its own token.
        with call_token(mock_graph.VALID_TOKEN):
            await client.get("1001", {"fields": "id,name"}, token=mock_graph.FRESH_TOKEN)
        assert mock_graph.find_last("1001")["headers"]["authorization"] == (
            f"Bearer {mock_graph.FRESH_TOKEN}"
        )


async def test_get_public_sends_no_bearer_token(graph):
    """The token exchange authenticates with its parameters, not a bearer token.

    ``oauth/access_token`` is rejected when it also carries an access token in the
    Authorization header, so the request must go without one even though the
    client normally has a token to send.
    """
    config = _config(
        graph_base_url=graph.base_url,
        app_id=mock_graph.APP_ID,
        app_secret=mock_graph.APP_SECRET,
    )
    async with GraphClient(config) as client:
        payload = await client.get_public(
            "oauth/access_token",
            {
                "grant_type": "fb_exchange_token",
                "client_id": config.app_id,
                "client_secret": config.app_secret,
                "fb_exchange_token": mock_graph.VALID_USER_TOKEN,
            },
        )
    assert payload["access_token"] == mock_graph.LONG_LIVED_USER_TOKEN
    entry = mock_graph.find_last("oauth/access_token")
    assert entry["headers"]["authorization"] == ""
    assert entry["query"]["grant_type"] == "fb_exchange_token"


async def test_get_public_ignores_the_configured_token(graph):
    """The exchange works while the configured token is dead.

    The exchange is the step that produces a working Page token, so it cannot
    depend on the one already configured being valid. That is the state a server
    reaches when its token expires: the only way out runs without it.
    """
    config = _config(
        graph_base_url=graph.base_url,
        page_token=mock_graph.EXPIRED_TOKEN,
        app_id=mock_graph.APP_ID,
        app_secret=mock_graph.APP_SECRET,
    )
    async with GraphClient(config) as client:
        payload = await client.get_public(
            "oauth/access_token",
            {
                "grant_type": "fb_exchange_token",
                "client_id": config.app_id,
                "client_secret": config.app_secret,
                "fb_exchange_token": mock_graph.VALID_USER_TOKEN,
            },
        )
    assert payload["token_type"] == "bearer"
    assert mock_graph.find_last("oauth/access_token")["headers"]["authorization"] == ""

    # The same client still uses the configured token for an ordinary call, so
    # sending no header is specific to the exchange.
    async with GraphClient(config) as client:
        with pytest.raises(GraphError):
            await client.get("1001", {"fields": "id,name"})
    assert mock_graph.find_last("1001")["headers"]["authorization"] == (
        f"Bearer {mock_graph.EXPIRED_TOKEN}"
    )


async def test_graph_error_fields_are_exposed(graph):
    async with GraphClient(_config(graph_base_url=graph.base_url)) as client:
        with pytest.raises(GraphError) as excinfo:
            await client.get("missing")
    err = excinfo.value
    assert err.code == 100
    assert err.error_type == "GraphMethodException"
    assert err.fbtrace_id == "MockTrace"
    assert "does not exist" in str(err)


async def test_bad_token_raises_190(graph):
    async with GraphClient(_config(page_token="wrong", graph_base_url=graph.base_url)) as client:
        with pytest.raises(GraphError) as excinfo:
            await client.get("1001")
    assert excinfo.value.code == 190


async def test_missing_token_for_scope_fails_before_requesting(graph):
    cfg = _config(page_token="")
    with pytest.raises(ConfigError):
        GraphClient(cfg)


async def test_post_sends_form_encoded_parameters(graph):
    async with GraphClient(_config(graph_base_url=graph.base_url)) as client:
        payload = await client.post("1001/feed", {"message": "Hello"})
    assert payload == {"id": "1001_7777"}
    assert mock_graph.find("1001/feed")["form"]["message"] == "Hello"


async def test_delete_returns_success(graph):
    async with GraphClient(_config(graph_base_url=graph.base_url)) as client:
        payload = await client.delete("9001")
    assert payload == {"success": True}


async def test_empty_parameters_are_dropped(graph):
    async with GraphClient(_config(graph_base_url=graph.base_url)) as client:
        await client.get("1001", {"fields": "id", "after": "", "limit": None})
    query = mock_graph.find("1001")["query"]
    assert query == {"fields": "id"}


async def test_retryable_error_code_is_retried(graph):
    """Graph error code 4 (app rate limit) is retried, then the call succeeds."""
    mock_graph.reset(rate_limited_calls=1)
    async with GraphClient(_config(graph_base_url=graph.base_url, max_retries=1)) as client:
        payload = await client.get("debug_token", {"input_token": "mock-page-token"})
    assert payload["data"]["is_valid"] is True
    assert len([r for r in mock_graph.REQUESTS if r.get("path") == "debug_token"]) == 2


async def test_non_retryable_error_is_not_retried(graph):
    mock_graph.reset()
    async with GraphClient(_config(graph_base_url=graph.base_url, max_retries=2)) as client:
        with pytest.raises(GraphError) as excinfo:
            await client.get("missing")
    assert excinfo.value.code == 100
    assert len([r for r in mock_graph.REQUESTS if r.get("path") == "missing"]) == 1


async def test_rate_limit_surfaces_when_retries_run_out(graph):
    mock_graph.reset(rate_limited_calls=3)
    async with GraphClient(_config(graph_base_url=graph.base_url, max_retries=1)) as client:
        with pytest.raises(GraphError) as excinfo:
            await client.get("debug_token", {"input_token": "mock-page-token"})
    assert excinfo.value.code == 4


async def test_batch_sends_json_in_form_field(graph):
    async with GraphClient(_config(graph_base_url=graph.base_url)) as client:
        results = await client.batch([{"method": "GET", "relative_url": "1001/posts"}])
    assert isinstance(results, list)
    assert results[0]["code"] == 200
    assert json.loads(results[0]["body"])["data"][0]["id"] == "1001_7001"
    assert mock_graph.find("batch")["entries"][0]["relative_url"] == "1001/posts"


async def test_get_url_follows_absolute_pagination_link(graph):
    async with GraphClient(_config(graph_base_url=graph.base_url)) as client:
        payload = await client.get_url(f"{graph.base_url}/v27.0/1001/next-page")
    assert payload["data"][0]["id"] == "1001_7003"
