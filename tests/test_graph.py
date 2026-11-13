"""Unit tests for configuration, the Graph client, and the field helpers."""

from __future__ import annotations

import asyncio
import json

import pytest
import uvicorn

from facebook_pages_mcp_server.config import Config, ConfigError, load_config
from facebook_pages_mcp_server.graph import GraphClient, GraphError
from facebook_pages_mcp_server.specs import PAGE_FIELDS, join_fields
from tests import mock_graph

MOCK_ORIGIN = "https://graph.example.test"


def _config(**overrides) -> Config:
    base = dict(
        page_token=mock_graph.VALID_TOKEN,
        graph_base_url=MOCK_ORIGIN,
        graph_version="v25.0",
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


# ----------------------------------------------------------------------
# Field selection
# ----------------------------------------------------------------------


def test_join_fields_defaults_and_extras():
    out = join_fields(("id", "name"), None, ["fan_count"])
    assert out == "id,name,fan_count"


def test_join_fields_replaces_defaults_when_fields_given():
    out = join_fields(PAGE_FIELDS, ["id", "name"], None)
    assert out == "id,name"


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
        payload = await client.get("debug_token")
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
            await client.get("debug_token")
    assert excinfo.value.code == 4


async def test_batch_sends_json_in_form_field(graph):
    async with GraphClient(_config(graph_base_url=graph.base_url)) as client:
        results = await client.batch([{"method": "GET", "relative_path": "1001/posts"}])
    assert isinstance(results, list)
    assert results[0]["code"] == 200
    assert json.loads(results[0]["body"])["data"][0]["id"] == "1001_7001"
    assert mock_graph.find("batch")["entries"][0]["relative_path"] == "1001/posts"


async def test_get_url_follows_absolute_pagination_link(graph):
    async with GraphClient(_config(graph_base_url=graph.base_url)) as client:
        payload = await client.get_url(f"{graph.base_url}/v25.0/1001/next-page")
    assert payload["data"][0]["id"] == "1001_7003"
