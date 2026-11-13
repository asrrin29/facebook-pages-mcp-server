# Facebook Pages MCP Server

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that lets AI
agents work with Facebook Pages through the [Graph API](https://developers.facebook.com/docs/graph-api).

An agent can list the Pages a person manages, read Page details, posts, comments,
photos, videos, events, Messenger conversations and Page Insights. If you allow
it, it can also publish posts, comment, upload media, and delete content.

Built on the [MCP Python SDK](https://py.sdk.modelcontextprotocol.io) (v2, `MCPServer`).
Targets Graph API `v27.0`, the current version; any version can be selected with
`FB_GRAPH_VERSION`.

## Disclaimer

This is an **unofficial, fan-made contribution**. It is **not** associated with,
endorsed by, or affiliated with Meta Platforms, Inc. or Facebook in any way.

This project was written **with the assistance of a large language model (LLM)**. Please
review the code before relying on it, especially for anything security-sensitive.

## Graph API coverage

Tools are grouped by what the underlying Graph API call does. The group decides
whether a tool is registered at all. See [Tool permissions](#tool-permissions).

| Tool | Graph API | Group |
|---|---|---|
| `pages_list` | `GET /me/accounts` | read |
| `pages_find` | `GET /me/accounts` (filtered locally) | read |
| `page_get` | `GET /{page-id}` | read |
| `page_posts_list` | `GET /{page-id}/posts` | read |
| `post_get` | `GET /{post-id}` | read |
| `post_comments_list` | `GET /{post-id}/comments` | read |
| `page_insights` | `GET /{page-id}/insights` | read |
| `post_insights` | `GET /{post-id}/insights` | read |
| `page_insights_metrics` | (metric name reference) | read |
| `page_photos_list` | `GET /{page-id}/photos` | read |
| `page_videos_list` | `GET /{page-id}/videos` | read |
| `page_albums_list` | `GET /{page-id}/albums` | read |
| `page_events_list` | `GET /{page-id}/events` | read |
| `page_conversations_list` | `GET /{page-id}/conversations` | read |
| `conversation_messages_list` | `GET /{conversation-id}/messages` | read |
| `token_debug` | `GET /debug_token` | read |
| `graph_batch` | `POST /batch` | read (POST/DELETE entries need write/delete) |
| `graph_follow` | `GET <next_url>` | read |
| `page_publish` | `POST /{page-id}/feed` | write |
| `post_comment_create` | `POST /{post-id}/comments` | write |
| `page_photo_publish` | `POST /{page-id}/photos` | write |
| `page_video_publish` | `POST /{page-id}/videos` | write |
| `graph_write` | `POST <path>` | write |
| `comment_delete` | `DELETE /{comment-id}` | delete |
| `post_delete` | `DELETE /{post-id}` | delete |

Two tools are always available because they describe this server and touch no
Facebook data: `facebook_pages_status` (configuration and a live token check) and
`facebook_pages_list_tools` (the tools currently enabled).

### Reading Page Insights

Insights need explicit metric names, and Meta deprecates them by API version.
Call `page_insights_metrics` to get a grouped list of names worth trying
(`audience`, `engagement`, `followers`, `posts`, `reactions`, `video`, `media`,
`monetization`), then pass them to `page_insights` with a `period` or
`date_preset`. When a metric is unavailable the Graph error names it.

Reach metrics (`page_impressions`, `post_impressions` and their paid, organic,
viral and fan breakdowns) were retired by Meta around Graph API v26.0. Only
total reach has a replacement: `page_total_media_view_unique` and
`post_total_media_view_unique`. The other breakdowns are gone with no
equivalent. The tool lists the retired names and their replacements under
`retired_metrics` so it can answer "how many people saw this post?" with a
metric that still works.

Two constraints worth knowing before you build a report: Page Insights are only
available for Pages with 100 or more likes, and most metrics update once per day.

### Choosing fields

The Graph API returns only the `id` of a node unless you ask for fields. Each
read tool therefore ships a default field set taken from the current reference.
Pass `fields` to replace that set outright, or `extra_fields` to add to it.

One behaviour to plan around: a field the API does not recognise fails the whole
request with error 100 rather than returning nothing. The error names the field,
so drop it and retry.

### Pagination

Read tools that return an edge add `more_available` and `next_url` to the
response. Pass that URL to `graph_follow` to get the next page; Graph pagination
links already carry the cursor and token. `limit` and `after` work too.

## Tool permissions

`MCP_TOOL_PERMISSIONS` decides which tools the AI can see and call. It is
grouped by what the underlying call does to Facebook data:

| Level | What it allows | Effect |
|---|---|---|
| `read` | list and fetch data | Nothing changes. Safe. |
| `write` | `page_publish`, `post_comment_create`, `page_photo_publish`, `page_video_publish`, `graph_write` | The AI publishes publicly under your Page name. |
| `delete` | `post_delete`, `comment_delete` | Content is removed for good. |

Levels do not imply one another. List everything you want:

```bash
MCP_TOOL_PERMISSIONS=read                # AI can only look at data
MCP_TOOL_PERMISSIONS=read,write          # AI can look at AND publish
MCP_TOOL_PERMISSIONS=read,write,delete   # AI has full control (dangerous)
```

Unset means read-only. An unknown value makes the server refuse to start, so a
typo can never silently widen access. A disabled tool is not registered, so it
never appears in `tools/list` and cannot be called.

## Quick start

### 1. Get a Page access token

1. Create (or open) an app in the [Meta for Developers](https://developers.facebook.com/apps/) dashboard.
2. Open the **Graph API Explorer**, pick your app, and request the Pages
   permissions you need:
   - read tools: `pages_show_list`, `pages_read_engagement`, `read_insights`
   - write tools: `pages_manage_posts`, `pages_manage_engagement`
3. Pick the Page and copy the token.
4. Exchange it for a **long-lived** token (60 days). Short-lived tokens expire in
   about two hours and will break the server mid-session. The
   [access token debugging tool](https://developers.facebook.com/tools/debug/accesstoken/)
   shows what you have and when it expires.

An app in *Development Mode* can only reach test users and Pages you own. That
is fine for trying this out; it is the safest way to run it.

A new app is created on the current Graph API version, and Meta issues its
access tokens for that version. The default `FB_GRAPH_VERSION` matches it. If you
pin an older version, set it to the one your app was created on; an app cannot
issue tokens for a version older than itself.

### 2. Install

```bash
cd facebook-pages-mcp-server
pip install -e .
```

### 3. Configure

```bash
cp .env.example .env
# edit .env:
#   FB_PAGE_TOKEN=...      (required)
#   FB_USER_TOKEN=...      (optional, only for pages_list / pages_find)
#   MCP_API_KEY=...        (only needed for serve-http)
```

Generate the MCP server's own Bearer token:

```bash
facebook-pages-mcp-server gen-key
```

Verify the configuration and the token:

```bash
facebook-pages-mcp-server check
```

That runs one live `debug_token` call and prints the issuing app, the granted
scopes and the expiry.

### 4. Run

**stdio** (the default, for local MCP clients):

```bash
facebook-pages-mcp-server
# or: facebook-pages-mcp-server serve
```

**streamable HTTP** (for remote access, protected by a Bearer token):

```bash
facebook-pages-mcp-server serve-http
# → http://127.0.0.1:8900/mcp   (clients send: Authorization: Bearer <MCP_API_KEY>)
#   http://127.0.0.1:8900/health (unauthenticated liveness probe)
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `FB_PAGE_TOKEN` | yes | Facebook Page access token. Every Page tool acts with it. Never a tool parameter. |
| `FB_USER_TOKEN` | no | User access token for `me/accounts` (`pages_list`, `pages_find`). Unset means those calls use `FB_PAGE_TOKEN`. |
| `FB_GRAPH_VERSION` | no | Graph API version (default `v27.0`). Accepts `v27.0` or `27.0`; a bare major such as `v27` is rejected by the API. Set it to the version your app was created on. |
| `FB_GRAPH_BASE_URL` | no | Graph API base URL (default `https://graph.facebook.com`). Change only for a test double or proxy. |
| `MCP_API_KEY` | for `serve-http` | Bearer token(s) clients must present to use *this* MCP server. Comma-separated list supported. |
| `MCP_HTTP_HOST` | no | Bind host for `serve-http` (default `127.0.0.1`). |
| `MCP_HTTP_PORT` | no | Bind port for `serve-http` (default `8900`). |
| `MCP_ALLOWED_HOSTS` | no | Comma-separated `Host` allowlist for the HTTP transport (see note below). |
| `MCP_ALLOWED_ORIGINS` | no | Comma-separated browser `Origin` allowlist for the HTTP transport. |
| `MCP_TOOL_PERMISSIONS` | no | `read`, `write`, `delete`, comma-separated. Default (unset): `read`. See [Tool permissions](#tool-permissions). |
| `FB_VERIFY_SSL` | no | `true` (default) or `false`. Set `false` only for a local test double with a self-signed certificate. |
| `FB_TIMEOUT` | no | HTTP timeout in seconds (default `30`). |
| `FB_MAX_RETRIES` | no | Extra retries on transient Graph failures (default `2`). |
| `MCP_LOG_LEVEL` | no | `DEBUG`, `INFO`, `WARNING`, `ERROR` (default `INFO`). Logs go to stderr. |

A `.env` file in the working directory (or the project root) is loaded
automatically if `python-dotenv` is installed; real environment variables always
win.

**DNS rebinding protection:** when `serve-http` binds to a loopback host the SDK
enables DNS-rebinding protection and only allows `127.0.0.1`/`localhost` Host
headers. Serving from a real hostname? Bind to that interface (e.g.
`MCP_HTTP_HOST=0.0.0.0`) and set `MCP_ALLOWED_HOSTS`/`MCP_ALLOWED_ORIGINS`.

## MCP client configuration

### Claude Desktop / any stdio client

```json
{
  "mcpServers": {
    "facebook-pages": {
      "command": "facebook-pages-mcp-server",
      "env": {
        "FB_PAGE_TOKEN": "your-page-access-token",
        "MCP_TOOL_PERMISSIONS": "read"
      }
    }
  }
}
```

(Or point `command` at `python` with `args: ["-m", "facebook_pages_mcp_server"]`
and put the config in a `.env` file.)

### Streamable HTTP client

- URL: `http://<host>:8900/mcp`
- Header: `Authorization: Bearer <MCP_API_KEY>`

## Docker

A production `Dockerfile` (multi-stage, non-root, slim) and `docker-compose.yml`
are included. The container serves streamable HTTP on port `8900` by default.

```bash
# 1. configure
cp .env.example .env   # fill in FB_PAGE_TOKEN and MCP_API_KEY

# 2. build and run
docker compose up -d --build

# 3. verify
curl http://localhost:8900/health
# → {"status": "ok", "server": "facebook-pages-mcp-server", "version": "1.1.0"}
```

MCP clients connect to `http://<host>:8900/mcp` with
`Authorization: Bearer <MCP_API_KEY>`.

`docker-compose.yml` passes every environment variable through from `.env`
(required ones fail fast with a clear message if unset) and applies container
hardening: non-root user, `cap_drop: ALL`, `no-new-privileges`, read-only root
filesystem, bounded log files, and a `/health`-based healthcheck.

Running without compose:

```bash
docker build -t facebook-pages-mcp-server .
docker run --rm -d --name fb-pages-mcp -p 8900:8900 --env-file .env \
  -e MCP_HTTP_HOST=0.0.0.0 facebook-pages-mcp-server
```

**Serving behind a real hostname:** DNS-rebinding protection auto-enables for
loopback binds. For a public hostname, set `MCP_ALLOWED_HOSTS` (and
`MCP_ALLOWED_ORIGINS` for browser clients) in `.env`. Terminate TLS at a reverse
proxy (Caddy/Traefik/nginx) in front of the container; the server itself speaks
plain HTTP.

**stdio in a container** (for MCP clients that spawn processes):

```bash
docker run --rm -i --env-file .env facebook-pages-mcp-server serve
```

## Development

```bash
pip install -e ".[dev]"
pytest            # unit tests (mocked Graph API) + end-to-end tests
```

The end-to-end suite starts the real server (stdio and streamable HTTP) against a
bundled mock Graph API (`tests/mock_graph.py`) that implements the documented
request and response shapes, so no live Facebook token is needed. The mock also
records the requests the server made, so the tests assert on the paths and
parameters, not only on the results.

## Project layout

```
facebook_pages_mcp_server/
├── __init__.py      # package metadata
├── __main__.py      # CLI: serve, serve-http, gen-key, check
├── config.py        # environment variable handling
├── graph.py         # async Graph API client (httpx) + error handling
├── server.py        # MCPServer + tool registration + HTTP auth
└── specs.py         # catalog of every tool: tier, description, endpoint
tests/
├── mock_graph.py    # mock Graph API for tests
├── test_graph.py    # client and configuration unit tests
└── test_e2e.py      # full server tests (stdio + HTTP)
```

## Security notes

- The Facebook token and the MCP Bearer token are only ever read from the
  environment. They are never logged and never a tool parameter.
- The Facebook token is sent in the `Authorization: Bearer` header, so it never
  appears in a URL, an access log, or a Graph pagination link.
- `MCP_API_KEY` comparison is constant-time (`hmac.compare_digest`).
- Facebook access tokens expire. A 60-day long-lived token still needs
  replacing; `facebook_pages_status` with `ping=true` reports the expiry.
- Use HTTPS for anything beyond localhost, and rotate the MCP token regularly.
- Keep the app in Development Mode, or scope the token to one test Page, before
  enabling `write` or `delete`.
- `MCP_TOOL_PERMISSIONS` is the first line of defense: the AI only ever sees the
  tools you allow (read-only by default). It complements, not replaces, the
  scopes on the Facebook token. Use both.

## License

Distributed under the [MIT License](LICENSE). See the `LICENSE` file for full
terms.
