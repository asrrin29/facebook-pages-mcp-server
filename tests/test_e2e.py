"""End-to-end tests: the real server process against a mock Graph API.

- stdio transport: spawns the installed module with the MCP SDK's high-level
  client and calls tools that hit the mock Graph API.
- HTTP transport: spawns ``serve-http``, checks Bearer auth (MCP_API_KEY) and
  a tool call over streamable HTTP.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys

import httpx
import pytest
import uvicorn
from mcp import Client
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from tests import mock_graph

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockGraph:
    def __init__(self):
        self.port = _free_port()
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


def _env(mock: MockGraph, **extra) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        FB_PAGE_TOKEN=mock_graph.VALID_TOKEN,
        FB_GRAPH_BASE_URL=mock.base_url,
        FB_GRAPH_VERSION="v27.0",
        MCP_LOG_LEVEL="WARNING",
    )
    env.update({k: v for k, v in extra.items() if v is not None})
    # A .env in the project root must not leak into the spawned server.
    env.setdefault("FB_USER_TOKEN", "")
    return env


def _server_params(env: dict[str, str], *args: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "facebook_pages_mcp_server", *args],
        env=env,
        cwd=PROJECT_ROOT,
    )


@pytest.fixture()
async def mock():
    m = MockGraph()
    await m.start()
    mock_graph.reset()
    yield m
    await m.stop()


def _tool_text(result) -> dict:
    assert not result.is_error, f"tool returned error: {result.content[0].text}"
    return json.loads(result.content[0].text)


# ---------------------------------------------------------------------------
# Read path over stdio
# ---------------------------------------------------------------------------

READ_ONLY_TOOLS = {
    "pages_list",
    "pages_find",
    "page_get",
    "page_posts_list",
    "post_get",
    "post_comments_list",
    "page_insights",
    "post_insights",
    "page_insights_metrics",
    "page_photos_list",
    "page_videos_list",
    "page_albums_list",
    "page_events_list",
    "page_conversations_list",
    "conversation_messages_list",
    "token_debug",
    "graph_batch",
    "graph_follow",
    "facebook_pages_status",
    "facebook_pages_list_tools",
}


async def test_stdio_tool_list_and_calls(mock):
    env = _env(mock, MCP_TOOL_PERMISSIONS="read,write,delete")
    async with Client(_server_params(env)) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        expected = {
            "facebook_pages_status",
            "facebook_pages_list_tools",
            "pages_list",
            "page_get",
            "page_posts_list",
            "post_comments_list",
            "page_insights",
            "page_publish",
            "post_comment_create",
            "page_photo_publish",
            "page_video_publish",
            "graph_write",
            "comment_delete",
            "post_delete",
        }
        assert expected <= names, f"missing tools: {expected - names}"
        assert len(names) == 27  # 25 Graph tools + 2 helpers

        # status + ping (a live call against the mock Graph API)
        data = _tool_text(await client.call_tool("facebook_pages_status", {"ping": True}))
        assert data["ready"] is True
        assert data["ping"]["ok"] is True
        assert data["graph_version"] == "v27.0"

        # list the Pages the token manages
        data = _tool_text(await client.call_tool("pages_list", {"limit": 10}))
        assert data["data"][0]["name"] == "Prime Time IT"
        assert data["more_available"] is True
        assert "next_url" in data

        # find one Page by name
        data = _tool_text(await client.call_tool("pages_find", {"query": "hotline"}))
        assert data["count"] == 1
        assert data["data"][0]["id"] == "1002"

        # read a Page
        data = _tool_text(await client.call_tool("page_get", {"page_id": "1001"}))
        assert data["name"] == "Prime Time IT"

        # list posts, then follow the pagination link
        data = _tool_text(await client.call_tool("page_posts_list", {"page_id": "1001", "limit": 2}))
        assert [p["id"] for p in data["data"]] == ["1001_7001", "1001_7002"]
        data = _tool_text(await client.call_tool("graph_follow", {"url": data["next_url"]}))
        assert data["data"][0]["id"] == "1001_7003"

        # comments on a post
        data = _tool_text(await client.call_tool("post_comments_list", {"post_id": "1001_7001"}))
        assert data["data"][0]["id"] == "9001"

        # insights with metric names
        data = _tool_text(
            await client.call_tool(
                "page_insights",
                {"page_id": "1001", "metrics": ["page_impressions"], "period": "day"},
            )
        )
        assert data["data"][0]["name"] == "page_impressions"

        # an unknown metric surfaces the Graph error, naming the code
        result = await client.call_tool(
            "page_insights", {"page_id": "1001", "metrics": ["not_a_real_metric"]}
        )
        assert result.is_error
        assert "code=100" in result.content[0].text

        # metric reference
        data = _tool_text(await client.call_tool("page_insights_metrics", {"group": "engagement"}))
        assert "page_post_engagements" in data["page_metrics_by_group"]["engagement"]
        assert set(data["page_metrics_by_group"]) == {"engagement"}

        all_metrics = _tool_text(await client.call_tool("page_insights_metrics", {}))
        # A retired metric would fail the whole Insights request, so it is never
        # offered; the tool names its replacement instead.
        offered = [
            metric for metrics in all_metrics["page_metrics_by_group"].values() for metric in metrics
        ] + all_metrics["post_metrics"]
        assert not [m for m in offered if m in all_metrics["retired_metrics"]]
        assert all_metrics["retired_metrics"]["page_impressions"] == "page_total_media_view_unique"

        # batch read: one call, two Graph requests
        data = _tool_text(
            await client.call_tool(
                "graph_batch",
                {
                    "requests": [
                        {"method": "GET", "relative_path": "1001/posts", "name": "posts"},
                        {"method": "GET", "relative_path": "1002", "name": "other_page"},
                    ]
                },
            )
        )
        assert len(data["results"]) == 2
        assert data["results"][0]["code"] == 200
        assert json.loads(data["results"][0]["body"])["data"][0]["id"] == "1001_7001"
        assert data["results"][1]["body"] == json.dumps({"id": "1002"})

        # module discovery
        data = _tool_text(await client.call_tool("facebook_pages_list_tools", {}))
        assert {t["tool"] for t in data["tools"]} >= {"page_get", "page_publish", "comment_delete"}


async def test_stdio_write_tools_change_data(mock):
    env = _env(mock, MCP_TOOL_PERMISSIONS="read,write")
    async with Client(_server_params(env)) as client:
        data = _tool_text(
            await client.call_tool("page_publish", {"page_id": "1001", "message": "Hello from MCP"})
        )
        assert data["id"] == "1001_7777"
        form = mock_graph.find("1001/feed")["form"]
        assert form["message"] == "Hello from MCP"

        data = _tool_text(
            await client.call_tool("post_comment_create", {"post_id": "1001_7001", "message": "Thanks!"})
        )
        assert data["id"] == "9500"

        data = _tool_text(
            await client.call_tool(
                "page_photo_publish", {"page_id": "1001", "url": "https://example.test/a.png", "caption": "hi"}
            )
        )
        assert data["post_id"] == "1001_8801"

        data = _tool_text(
            await client.call_tool(
                "page_video_publish", {"page_id": "1001", "source": "https://example.test/a.mp4"}
            )
        )
        assert data["id"] == "8700"

        # graph_write reaches the same endpoint through an explicit path
        data = _tool_text(
            await client.call_tool("graph_write", {"path": "1001/feed", "params": {"message": "via graph_write"}})
        )
        assert data["id"] == "1001_7777"
        assert mock_graph.find_last("1001/feed")["form"]["message"] == "via graph_write"


async def test_stdio_delete_tools(mock):
    env = _env(mock, MCP_TOOL_PERMISSIONS="read,write,delete")
    async with Client(_server_params(env)) as client:
        data = _tool_text(await client.call_tool("comment_delete", {"comment_id": "9001"}))
        assert data["success"] is True
        data = _tool_text(await client.call_tool("post_delete", {"post_id": "1001_7002"}))
        assert data["success"] is True
        result = await client.call_tool("comment_delete", {"comment_id": "undeletable"})
        assert result.is_error


# ---------------------------------------------------------------------------
# Argument validation happens before any network call
# ---------------------------------------------------------------------------


async def test_stdio_missing_page_id_is_rejected(mock):
    env = _env(mock)
    async with Client(_server_params(env)) as client:
        result = await client.call_tool("page_get", {"page_id": "   "})
        assert result.is_error
        assert "page_id is required" in result.content[0].text
        assert "pages_list" in result.content[0].text


async def test_stdio_empty_metrics_is_rejected(mock):
    env = _env(mock)
    async with Client(_server_params(env)) as client:
        result = await client.call_tool("page_insights", {"page_id": "1001", "metrics": []})
        assert result.is_error
        assert "page_insights_metrics" in result.content[0].text


async def test_stdio_bad_token_reports_graph_error(mock):
    env = _env(mock, FB_PAGE_TOKEN="wrong-token")
    async with Client(_server_params(env)) as client:
        result = await client.call_tool("page_get", {"page_id": "1001"})
        assert result.is_error
        assert "code=190" in result.content[0].text


async def test_stdio_missing_token_still_starts_and_reports_unready(mock):
    env = _env(mock)
    env["FB_PAGE_TOKEN"] = ""
    async with Client(_server_params(env)) as client:
        data = _tool_text(await client.call_tool("facebook_pages_status", {}))
        assert data["ready"] is False
        assert "FB_PAGE_TOKEN" in data["hint"]
        result = await client.call_tool("page_get", {"page_id": "1001"})
        assert result.is_error


# ---------------------------------------------------------------------------
# MCP_TOOL_PERMISSIONS gates registration: a disallowed tool is invisible
# ---------------------------------------------------------------------------


async def _tool_names(mock, **extra) -> set[str]:
    env = _env(mock, **extra)
    async with Client(_server_params(env)) as client:
        tools = await client.list_tools()
        return {t.name for t in tools.tools}


async def test_stdio_read_only_by_default(mock):
    # No MCP_TOOL_PERMISSIONS set -> read-only: 18 read tools + 2 helpers.
    names = await _tool_names(mock)
    assert names == READ_ONLY_TOOLS
    assert "page_publish" not in names
    assert "comment_delete" not in names


async def test_stdio_read_write_permissions(mock):
    names = await _tool_names(mock, MCP_TOOL_PERMISSIONS="read,write")
    assert {"page_publish", "post_comment_create", "page_photo_publish", "graph_write"} <= names
    assert {"comment_delete", "post_delete"}.isdisjoint(names)
    assert len(names) == len(READ_ONLY_TOOLS) + 5


async def test_stdio_delete_permission_exposes_delete_tools(mock):
    names = await _tool_names(mock, MCP_TOOL_PERMISSIONS="read,write,delete")
    assert {"comment_delete", "post_delete"} <= names
    assert len(names) == 27


async def test_stdio_status_reports_permissions(mock):
    env = _env(mock, MCP_TOOL_PERMISSIONS="read,write")
    async with Client(_server_params(env)) as client:
        data = _tool_text(await client.call_tool("facebook_pages_status", {}))
        assert data["tool_permissions"] == ["read", "write"]
        data = _tool_text(await client.call_tool("facebook_pages_list_tools", {}))
        listed = {t["tool"] for t in data["tools"]}
        assert "comment_delete" not in listed
        assert "page_publish" in listed


async def test_stdio_write_in_batch_needs_write_permission(mock):
    env = _env(mock)  # read-only
    async with Client(_server_params(env)) as client:
        result = await client.call_tool(
            "graph_batch", {"requests": [{"method": "POST", "relative_path": "1001/feed"}]}
        )
        assert result.is_error
        assert "write permission" in result.content[0].text


async def test_stdio_invalid_permission_level_refuses_to_start(mock):
    env = _env(mock, MCP_TOOL_PERMISSIONS="read,bogus")
    proc = subprocess.run(
        [sys.executable, "-m", "facebook_pages_mcp_server", "serve"],
        env=env,
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "MCP_TOOL_PERMISSIONS" in proc.stderr
    assert "bogus" in proc.stderr


# ---------------------------------------------------------------------------
# Streamable HTTP transport with Bearer tokens
# ---------------------------------------------------------------------------


async def test_http_requires_bearer_key(mock):
    port = _free_port()
    env = _env(
        mock,
        MCP_API_KEY="mcp-secret-key-123",
        MCP_HTTP_PORT=str(port),
        MCP_HTTP_HOST="127.0.0.1",
        MCP_TOOL_PERMISSIONS="read",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "facebook_pages_mcp_server", "serve-http"],
        env=env,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(200):
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early: {proc.stderr.read().decode()[:1000]}")
            try:
                async with httpx.AsyncClient() as hc:
                    r = await hc.get(f"http://127.0.0.1:{port}/health", timeout=2)
                if r.status_code == 200:
                    break
            except (httpx.HTTPError, OSError):
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("HTTP server did not come up")

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as hc:
            # health needs no key
            r = await hc.get("/health")
            assert r.status_code == 200
            assert r.json()["server"] == "facebook-pages-mcp-server"

            # no key -> 401
            r = await hc.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            assert r.status_code == 401

            # wrong key -> 401
            r = await hc.post(
                "/mcp",
                headers={"Authorization": "Bearer wrong"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert r.status_code == 401

            # a non-bearer scheme -> 401
            r = await hc.post(
                "/mcp",
                headers={"Authorization": "Token mcp-secret-key-123"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert r.status_code == 401

        # a full MCP session with the right key
        http_client = create_mcp_http_client(headers={"Authorization": "Bearer mcp-secret-key-123"})
        async with http_client:
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http_client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("page_posts_list", {"page_id": "1001", "limit": 2})
                    assert not result.is_error
                    data = json.loads(result.content[0].text)
                    assert len(data["data"]) == 2
                    # write tools are not visible under read-only permissions
                    tools = await session.list_tools()
                    assert "page_publish" not in {t.name for t in tools.tools}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
