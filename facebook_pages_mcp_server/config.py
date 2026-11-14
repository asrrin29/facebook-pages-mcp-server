"""Configuration for the Facebook Pages MCP server.

All settings come from environment variables (optionally loaded from a
``.env`` file in the working directory or the project root).

Two Facebook tokens matter here:

- ``FB_PAGE_TOKEN`` - a Page access token. It is what Page tools
  (``/ {page-id}``, ``/ {page-id}/posts``, ``/ {page-id}/insights``, ...)
  act on. It is the only token required to run the server.
- ``FB_USER_TOKEN`` - a User access token. It is needed for the edges that
  hang off a person instead of a Page (``/me/accounts`` lists the Pages a
  person manages). When it is unset, those tools fall back to
  ``FB_PAGE_TOKEN``, which works when the Page token was created through a
  person who can perform the needed task.

The MCP layer has its own credential, ``MCP_API_KEY``: the Bearer token
clients must present to reach this server. It is unrelated to Facebook.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

try:  # optional, only needed for .env file support
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

SERVER_NAME = "facebook-pages-mcp-server"
SERVER_VERSION = "1.4.0"

DEFAULT_GRAPH_VERSION = "v27.0"
DEFAULT_GRAPH_BASE_URL = "https://graph.facebook.com"

# A Graph API version path segment is "v", a major and a minor. Graph rejects a
# bare major such as ``v27``.
_VERSION_RE = re.compile(r"^v\d+\.\d+$")

# Environment variable names
ENV_PAGE_TOKEN = "FB_PAGE_TOKEN"
ENV_USER_TOKEN = "FB_USER_TOKEN"
ENV_GRAPH_VERSION = "FB_GRAPH_VERSION"
ENV_GRAPH_BASE_URL = "FB_GRAPH_BASE_URL"
ENV_VERIFY_SSL = "FB_VERIFY_SSL"
ENV_TIMEOUT = "FB_TIMEOUT"
ENV_MAX_RETRIES = "FB_MAX_RETRIES"
ENV_MCP_API_KEY = "MCP_API_KEY"
ENV_HTTP_HOST = "MCP_HTTP_HOST"
ENV_HTTP_PORT = "MCP_HTTP_PORT"
ENV_ALLOWED_HOSTS = "MCP_ALLOWED_HOSTS"
ENV_ALLOWED_ORIGINS = "MCP_ALLOWED_ORIGINS"
ENV_TOOL_PERMISSIONS = "MCP_TOOL_PERMISSIONS"

# Tool permission levels, in escalating order. Each level gates a set of
# tools based on what the underlying Graph API call does:
#   read   - fetches data only (never changes anything)
#   write  - creates or modifies data (publish post, comment, upload photo)
#   delete - permanently removes data (delete comment)
TOOL_PERMISSION_LEVELS: tuple[str, ...] = ("read", "write", "delete")
# Secure default: the AI may only read.
DEFAULT_TOOL_PERMISSIONS: tuple[str, ...] = ("read",)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _load_dotenv() -> None:
    if load_dotenv is None:
        return
    # Prefer a .env in the current directory, then the project root.
    candidates = [Path.cwd() / ".env"]
    project_root = Path(__file__).resolve().parent.parent
    candidates.append(project_root / ".env")
    for path in candidates:
        if path.is_file():
            load_dotenv(path, override=False)
            return


def _env_str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env_str(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    value = _env_str(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def _env_int(name: str, default: int) -> int:
    value = _env_str(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _env_list(name: str) -> tuple[str, ...]:
    value = _env_str(name)
    if value is None:
        return ()
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _env_permissions(name: str) -> tuple[str, ...]:
    """Parse the tool-permission level list (e.g. ``read,write``).

    Unset means the secure default (read-only). Unknown values are a hard
    error so a typo can never silently widen (or narrow) access.
    """
    value = _env_str(name)
    if value is None:
        return DEFAULT_TOOL_PERMISSIONS
    levels = tuple(v.strip().lower() for v in value.split(",") if v.strip())
    if not levels:
        raise ConfigError(
            f"{name} must be a comma-separated list of: {', '.join(TOOL_PERMISSION_LEVELS)}"
        )
    unknown = [v for v in levels if v not in TOOL_PERMISSION_LEVELS]
    if unknown:
        raise ConfigError(
            f"{name} contains unknown permission level(s) {', '.join(unknown)}. "
            f"Valid levels: {', '.join(TOOL_PERMISSION_LEVELS)} "
            "(combine with commas, e.g. read,write)."
        )
    # De-duplicate and return in canonical escalating order.
    return tuple(level for level in TOOL_PERMISSION_LEVELS if level in levels)


def _normalize_version(value: str) -> str:
    """Accept ``27.0`` or ``v27.0``; the Graph API path segment needs the ``v``."""
    value = value.strip().lstrip("/")
    if not value.startswith("v"):
        value = f"v{value}"
    if not _VERSION_RE.fullmatch(value):
        raise ConfigError(
            f"{ENV_GRAPH_VERSION} must look like a Graph API version "
            f"(e.g. v27.0), got {value!r}."
        )
    return value


@dataclass(frozen=True)
class Config:
    """Runtime configuration for the server."""

    page_token: str
    user_token: str = ""
    graph_version: str = DEFAULT_GRAPH_VERSION
    graph_base_url: str = DEFAULT_GRAPH_BASE_URL
    verify_ssl: bool = True
    timeout_seconds: float = 30.0
    max_retries: int = 2
    mcp_api_key: str = ""
    http_host: str = "127.0.0.1"
    http_port: int = 8900
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    tool_permissions: tuple[str, ...] = DEFAULT_TOOL_PERMISSIONS

    def allows_action(self, action: str) -> bool:
        """Whether a tool whose action tier is ``action`` (read/write/delete) is permitted."""
        return action in self.tool_permissions

    @property
    def mcp_api_keys(self) -> tuple[str, ...]:
        """All configured MCP API keys (MCP_API_KEY may hold a comma-separated list)."""
        return tuple(k.strip() for k in self.mcp_api_key.split(",") if k.strip())

    @property
    def api_root(self) -> str:
        """Base URL for versioned Graph API calls, e.g. ``https://graph.facebook.com/v27.0``."""
        return f"{self.graph_base_url.rstrip('/')}/{self.graph_version}"

    def token_for(self, scope: str) -> str:
        """The access token to use for a call of the given scope.

        ``scope`` is ``"page"`` for Page edges and ``"user"`` for edges that
        hang off a person (``/me/...``). A missing user token falls back to
        the page token, because a Page access token issued to a person can
        itself read ``/me/accounts``.
        """
        if scope == "user" and self.user_token:
            return self.user_token
        return self.page_token

    def require_facebook(self) -> None:
        """Validate the settings needed to talk to the Graph API."""
        if not self.page_token:
            raise ConfigError(
                f"Missing required environment variable {ENV_PAGE_TOKEN}. "
                "Get a Page access token from an app in the Graph API Explorer "
                "or your app's access token tool."
            )
        if not self.graph_base_url.startswith(("http://", "https://")):
            raise ConfigError(
                f"{ENV_GRAPH_BASE_URL} must start with http:// or https:// "
                f"(got {self.graph_base_url!r})."
            )

    def require_mcp_key(self) -> None:
        """Validate the MCP server's own API key (required for HTTP transport)."""
        if not self.mcp_api_key:
            raise ConfigError(
                f"Missing required environment variable {ENV_MCP_API_KEY}. "
                "Generate one with: facebook-pages-mcp-server gen-key"
            )

    def with_token(self, scope: str, token: str) -> "Config":
        """A copy of this configuration that uses ``token`` for calls of ``scope``.

        A Page token taken from a short-lived user token expires within a day.
        A replacement can be supplied while the server runs, rather than by
        editing the environment and restarting.
        """
        field = "user_token" if scope == "user" else "page_token"
        return replace(self, **{field: token})


def load_config() -> Config:
    """Build a Config from the environment."""
    _load_dotenv()
    return Config(
        page_token=_env_str(ENV_PAGE_TOKEN, "") or "",
        user_token=_env_str(ENV_USER_TOKEN, "") or "",
        graph_version=_normalize_version(_env_str(ENV_GRAPH_VERSION, DEFAULT_GRAPH_VERSION) or DEFAULT_GRAPH_VERSION),
        graph_base_url=(_env_str(ENV_GRAPH_BASE_URL, DEFAULT_GRAPH_BASE_URL) or DEFAULT_GRAPH_BASE_URL).rstrip("/"),
        verify_ssl=_env_bool(ENV_VERIFY_SSL, True),
        timeout_seconds=_env_float(ENV_TIMEOUT, 30.0),
        max_retries=_env_int(ENV_MAX_RETRIES, 2),
        mcp_api_key=_env_str(ENV_MCP_API_KEY, "") or "",
        http_host=_env_str(ENV_HTTP_HOST, "127.0.0.1") or "127.0.0.1",
        http_port=_env_int(ENV_HTTP_PORT, 8900),
        allowed_hosts=_env_list(ENV_ALLOWED_HOSTS),
        allowed_origins=_env_list(ENV_ALLOWED_ORIGINS),
        tool_permissions=_env_permissions(ENV_TOOL_PERMISSIONS),
    )
