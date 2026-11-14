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
| `graph_batch` | `POST /{version}` with a `batch` parameter | read (POST/DELETE entries need write/delete) |
| `graph_follow` | `GET <next_url>` | read |
| `page_publish` | `POST /{page-id}/feed` | write |
| `post_comment_create` | `POST /{post-id}/comments` | write |
| `page_photo_publish` | `POST /{page-id}/photos` | write |
| `page_video_publish` | `POST /{page-id}/videos` | write |
| `graph_write` | `POST <path>` | write |
| `comment_delete` | `DELETE /{comment-id}` | delete |
| `post_delete` | `DELETE /{post-id}` | delete |

Three tools are always available. They describe this server, and only
`facebook_pages_status` and `token_debug` touch Facebook data:

- `facebook_pages_status`: configuration, readiness, and the token's expiry.
- `facebook_pages_set_token`: use a pasted access token for the rest of the
  session, which is how an expired token is replaced without a restart.
- `facebook_pages_list_tools`: the tools currently enabled.

### Reading Page Insights

Insights need explicit metric names, and Meta deprecates them by API version.
Call `page_insights_metrics` to get a grouped list of names worth trying
(`audience`, `engagement`, `followers`, `posts`, `reactions`, `video`, `media`,
`monetization`), then pass them to `page_insights` with a `period` or
`date_preset`. When a metric is unavailable the Graph error names it.

Meta deprecated a large group of Page Insights metrics for every API version, in
waves dated 2025-06-15 and 2025-11-15, and states a further wave for 2026-06-15.
Two families matter here:

- Reach: `page_impressions`, `post_impressions` and their paid, organic, viral,
  fan and non-viral breakdowns. Use `page_media_view` and `post_media_view` for
  impressions, or the `*_total_media_view_unique` metrics for unique reach. The
  paid breakdown is available as a breakdown on the media-view metrics.
- Page likes: `page_fans`, `page_fans_city`, `page_fans_country` and
  `page_fan_adds`. Use the follower metrics instead: `page_follows`,
  `page_follows_city`, `page_follows_country`, `page_daily_follows` and
  `page_daily_unfollows_unique`.

The reference tables still print the deprecated names, so they are not a reliable
guide. The list the API enforces is
[Deprecated Facebook Page Insights Metrics](https://developers.facebook.com/documentation/pages-api/platforminsights/page/deprecated-metrics).
One rejected metric fails the whole Insights request with error 100, which is why
the tools never offer them. `page_insights_metrics` lists each name with its
replacement under `retired_metrics`, so a question like "how many people saw this
post?" is answered with a metric that still works.

Three constraints worth knowing before you build a report: Page Insights are only
available for Pages with 100 or more likes, most metrics update once per day, and
a `since`/`until` range covers at most 90 days.

### Choosing fields

The Graph API returns only the `id` of a node unless you ask for fields. Each
read tool therefore ships a default field set taken from the current reference.
Pass `fields` to replace that set outright, or `extra_fields` to add to it.

Two behaviours to plan around:

- A field the API does not recognise fails the whole request with error 100
  rather than returning nothing. The error names the field, so drop it and retry.
- Some fields are accepted on one endpoint and rejected on another. `attachments`
  works on a single post node and fails the `/{page-id}/posts` edge with error
  12, so the default for `page_posts_list` leaves it out and `post_get` includes
  it. Read attachments one post at a time.

The tools never send one field name twice. Graph reports a syntax error (2500)
when `fields` and `extra_fields` name the same field, so a name already present
is not repeated.

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
4. Exchange it for a **long-lived** user token (60 days), then call
   `GET /me/accounts?fields=id,name,access_token` with it and copy the
   `access_token` returned for your Page. Short-lived tokens expire in about two
   hours, and a Page token taken from a short-lived user token expires within a
   day. **A Page token taken from a long-lived user token has no expiry.** That
   is the value `FB_PAGE_TOKEN` should hold. The
   [access token debugging tool](https://developers.facebook.com/tools/debug/accesstoken/)
   shows what you have and when it expires.

`check` reports the same thing before you start the server:

```bash
facebook-pages-mcp-server check
#   OK: type=PAGE app='1234567890' scopes=4
#   Expiry: none (this token does not expire)
```

### Replacing an expired token without a restart

A token can expire while the server runs. Rather than editing `.env` and
restarting, pass the new token to `facebook_pages_set_token`:

```json
{"tool": "facebook_pages_set_token", "arguments": {"access_token": "EAAG..."}}
```

The token is checked with `debug_token` first. A token that Graph rejects, or
that is already expired, is not applied, so a typo cannot leave every later call
failing. A token that is accepted is used by every later call in that process.
It is not written to the environment, so a restart goes back to the configured
value; set `FB_PAGE_TOKEN` to keep it permanently.

The same tool replaces the optional `FB_USER_TOKEN` when called with
`"scope": "user"`.

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
# → {"status": "ok", "server": "facebook-pages-mcp-server", "version": "1.3.0"}
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

## Troubleshooting

**The server reports an unexpected Graph API version.** A process keeps the
configuration it read at start, so a server that is still running after you
changed `.env` or reinstalled the package keeps answering with the old values.
Restart it. The startup line and `facebook_pages_status` both name the version in
use, so compare them with `FB_GRAPH_VERSION`. If you installed with `pip install .`
rather than `pip install -e .`, a later source edit is not picked up until you
install again.

**Insights return an invalid-metric error (code 100).** One unsupported metric
fails the whole request. Call `page_insights_metrics` and read `retired_metrics`
for the replacement. The list Meta enforces is linked under "Reading Page
Insights" above; the reference tables still print deprecated names.

**A read fails on a field you did not ask for.** Drop the field named in the
error, or pass your own `fields`. Some fields are valid on one endpoint and not
another, as described under "Choosing fields".

**Every call fails with code 190.** The Facebook token is the problem, not the
code. An expired token says so and quotes the date Graph gave, for example
`Session has expired on Friday, 13-Nov-26 14:00:00 PST`. Mint a new one as
described under "Get a Page access token", then pass it to
`facebook_pages_set_token`. `facebook_pages_status` reports the expiry of the
token in use, and `check` prints it before the server starts.

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
├── specs.py         # catalog of every tool: tier, description, endpoint
└── tokens.py        # token expiry and token-failure messages
tests/
├── mock_graph.py    # mock Graph API for tests
├── test_graph.py    # client and configuration unit tests
├── test_tokens.py   # token diagnostics unit tests
└── test_e2e.py      # full server tests (stdio + HTTP)
```

## Security notes

- The MCP Bearer token is read only from the environment and is never logged.
- The Facebook token is read from the environment, except when you pass a
  replacement to `facebook_pages_set_token`. It is never logged, never written to
  the environment by the server, and never included in a tool response. Only the
  token's type, scopes and expiry are reported.
- The Facebook token is sent in the `Authorization: Bearer` header, so it never
  appears in a URL, an access log, or a Graph pagination link.
- `MCP_API_KEY` comparison is constant-time (`hmac.compare_digest`).
- Facebook access tokens expire. `facebook_pages_status` reports the type,
  scopes and expiry of the token in use, and an expired token makes it report
  `ready: false`. A Page token from a long-lived user token has no expiry, which
  is the state to aim for; `facebook_pages_set_token` replaces a dead token
  without a restart.
- Use HTTPS for anything beyond localhost, and rotate the MCP token regularly.
- Keep the app in Development Mode, or scope the token to one test Page, before
  enabling `write` or `delete`.
- `MCP_TOOL_PERMISSIONS` is the first line of defense: the AI only ever sees the
  tools you allow (read-only by default). It complements, not replaces, the
  scopes on the Facebook token. Use both.

## License

Distributed under the [MIT License](LICENSE). See the `LICENSE` file for full
terms.
