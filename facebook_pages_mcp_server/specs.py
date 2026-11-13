"""Catalog of the Graph API (Pages) endpoints this server exposes as tools.

One entry per tool. The catalog is the single source of truth for three
things, so they cannot drift apart:

- which tools get registered (a tool is registered only when its tier is
  allowed by ``MCP_TOOL_PERMISSIONS``),
- the name and description the AI sees,
- what :func:`the discovery tool <server.build_server>` reports.

``api`` is documentation for humans and agents: the Graph API path and method
the tool calls. ``path`` uses placeholders (``{page_id}``, ``{post_id}``).

Field defaults are part of the catalog because the Graph API returns only the
``id`` of a node unless you ask for fields. Each read tool ships a curated
default field set and lets the caller override or extend it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    """A Graph API endpoint exposed as an MCP tool."""

    name: str
    tier: str  # read | write | delete
    summary: str
    api: str  # documented Graph API path + method
    description: str
    notes: str = ""


# ---------------------------------------------------------------------------
# Default field sets (see module docstring for why these exist)
# ---------------------------------------------------------------------------

# An unknown field fails the whole request with Graph error 100, so a default
# set lists only fields the current reference documents. Callers add the rest
# through extra_fields.
PAGE_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "about",
    "category",
    "username",
    "link",
    "website",
    "phone",
    "emails",
    "fan_count",
    "followers_count",
    "talking_about_count",
    "verification_status",
    "is_published",
    "can_post",
    "general_info",
    "hours",
    "location",
    "cover",
    "engagement",
    "picture.width(200)",
)

# The Graph API adds the caller's `tasks` to every me/accounts node by itself,
# so it is not requested.
PAGE_SUMMARY_FIELDS: tuple[str, ...] = ("id", "name", "link", "username", "category")

POST_FIELDS: tuple[str, ...] = (
    "id",
    "message",
    "status_type",
    "created_time",
    "updated_time",
    "permalink_url",
    "link",
    "full_picture",
    "attachments",
    "shares",
    "comments.summary(true).filter(toplevel).limit(3)",
    "reactions.summary(true)",
    "likes.summary(true)",
    "is_published",
    "with_tags",
)

COMMENT_FIELDS: tuple[str, ...] = (
    "id",
    "message",
    "created_time",
    "like_count",
    "comment_count",
    "permalink_url",
    "from",
    "hidden",
    "is_private",
)

PHOTO_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "link",
    "images",
    "created_time",
    "updated_time",
    "like_count",
    "comments.summary(true)",
)

VIDEO_FIELDS: tuple[str, ...] = (
    "id",
    "description",
    "title",
    "created_time",
    "updated_time",
    "length",
    "permalink_url",
    "picture",
    "status",
)

ALBUM_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "description",
    "count",
    "link",
    "created_time",
    "updated_time",
    "location",
)

EVENT_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "description",
    "start_time",
    "end_time",
    "place",
    "cover",
    "attending_count",
    "interested_count",
    "is_online_event",
)

CONVERSATION_FIELDS: tuple[str, ...] = (
    "id",
    "snippet",
    "updated_time",
    "participants",
    "unread_count",
    "folder",
    "muted_until",
)

MESSAGE_FIELDS: tuple[str, ...] = (
    "id",
    "created_time",
    "message",
    "from",
    "to",
    "attachments",
    "sticker",
)

# A starter list of Page Insights metrics. Meta changes this list often and
# deprecates metrics by API version, so treat it as a starting point and let
# the API report an invalid-metric error rather than trusting this list.
INSIGHTS_METRICS: dict[str, tuple[str, ...]] = {
    "audience": ("page_fans", "page_fan_adds", "page_fan_removes", "page_fans_country", "page_fans_city"),
    "engagement": (
        "page_post_engagements",
        "page_impressions",
        "page_impressions_paid",
        "page_total_actions",
        "page_views_total",
    ),
    "followers": ("page_daily_follows", "page_daily_unfollows_unique", "page_lifetime_engaged_followers_unique"),
    "posts": (
        "page_posts_impressions",
        "page_posts_impressions_paid",
        "post_clicks",
        "post_activity_by_action_type",
    ),
    "reactions": (
        "page_actions_post_reactions_total",
        "page_actions_post_reactions_like_total",
        "page_actions_post_reactions_love_total",
        "page_actions_post_reactions_anger_total",
    ),
    "video": (
        "page_video_views",
        "page_video_views_paid",
        "page_video_views_organic",
        "page_video_complete_views_30s",
        "page_video_view_time",
    ),
    "media": ("page_media_view", "page_total_media_view_unique"),
    "monetization": ("content_monetization_earnings", "monetization_approximate_earnings"),
}

POST_INSIGHTS_METRICS: tuple[str, ...] = (
    "post_impressions",
    "post_impressions_paid",
    "post_impressions_organic",
    "post_clicks",
    "post_media_view",
    "post_reactions_by_type_total",
    "post_video_views",
    "post_activity_by_action_type",
)


TOOL_SPECS: tuple[ToolSpec, ...] = (
    # ---------------- read ----------------
    ToolSpec(
        name="pages_list",
        tier="read",
        summary="List the Pages the token's person manages",
        api="GET me/accounts",
        description=(
            "List the Facebook Pages the access token's person can perform tasks on, "
            "with the tasks (ADVERTISE, MODERATE, CREATE_CONTENT, ANALYZE, ...) they hold "
            "on each. Use this first to find a page_id."
        ),
        notes=(
            "Needs a User access token with pages_show_list. Set FB_USER_TOKEN for a "
            "dedicated user token, or use a Page token issued through a person."
        ),
    ),
    ToolSpec(
        name="pages_find",
        tier="read",
        summary="Find a Page by name among the managed Pages",
        api="GET me/accounts (filtered locally)",
        description=(
            "Search the Pages the token manages by a case-insensitive substring of the "
            "Page name or username. Returns matching page ids and names. Cheaper than "
            "listing every Page when you only need one."
        ),
    ),
    ToolSpec(
        name="page_get",
        tier="read",
        summary="Read a Page's fields",
        api="GET {page_id}",
        description=(
            "Read one Page's details: name, category, about, follower and like counts, "
            "contact info, opening hours, location, picture, verification status. Pass "
            "fields to choose exactly what you need, or extra_fields to add to the defaults."
        ),
    ),
    ToolSpec(
        name="page_posts_list",
        tier="read",
        summary="List a Page's posts",
        api="GET {page_id}/posts",
        description=(
            "List posts published by a Page, newest first, with message, timestamp, "
            "engagement counts and attachment metadata. Filter by date with since/until "
            "(Unix timestamps), paginate with limit and after."
        ),
    ),
    ToolSpec(
        name="post_get",
        tier="read",
        summary="Read one post",
        api="GET {post_id}",
        description=(
            "Read a single post by id (PageID_RecipientID). Returns the message, "
            "timestamps, link, picture, share and reaction summaries."
        ),
    ),
    ToolSpec(
        name="post_comments_list",
        tier="read",
        summary="List comments on a post",
        api="GET {post_id}/comments",
        description=(
            "List comments on a post. Defaults to top-level comments; set filter=stream "
            "for the full threaded stream, order=chronological_ascending to read oldest first."
        ),
    ),
    ToolSpec(
        name="page_insights",
        tier="read",
        summary="Read Page metrics",
        api="GET {page_id}/insights",
        description=(
            "Read Page-level metrics (impressions, reach, engagements, follower growth, "
            "video views, reactions, demographics). Pass one or more metric names, a "
            "period (day, week, days_28, lifetime) and either date_preset or since/until. "
            "Call page_insights_metrics for a list of metric names to try."
        ),
        notes=(
            "Requires a token that can perform the ANALYZE task, and Pages with fewer "
            "than 100 likes return no insights. Some metrics are deprecated in newer API "
            "versions; an invalid-metric error names the metric that failed."
        ),
    ),
    ToolSpec(
        name="post_insights",
        tier="read",
        summary="Read one post's metrics",
        api="GET {post_id}/insights",
        description=(
            "Read metrics for a single post: impressions, reach, clicks, media views, "
            "reactions by type, video views. Pass one or more metric names."
        ),
    ),
    ToolSpec(
        name="page_insights_metrics",
        tier="read",
        summary="List Page Insights metric names to try",
        api="(static reference)",
        description=(
            "List common Page and post Insights metric names grouped by purpose, with the "
            "periods each supports. Meta changes this list often, so treat it as a "
            "starting point and let the API tell you when a metric is unavailable."
        ),
    ),
    ToolSpec(
        name="page_photos_list",
        tier="read",
        summary="List a Page's photos",
        api="GET {page_id}/photos",
        description="List photos posted to a Page, with image URLs, captions and comment counts.",
    ),
    ToolSpec(
        name="page_videos_list",
        tier="read",
        summary="List a Page's videos",
        api="GET {page_id}/videos",
        description="List videos published by a Page, with length, permalinks, thumbnail and publish status.",
    ),
    ToolSpec(
        name="page_albums_list",
        tier="read",
        summary="List a Page's photo albums",
        api="GET {page_id}/albums",
        description="List a Page's photo albums with names, counts and links.",
    ),
    ToolSpec(
        name="page_events_list",
        tier="read",
        summary="List a Page's events",
        api="GET {page_id}/events",
        description="List events created by a Page with start and end times, place and attendee counts.",
    ),
    ToolSpec(
        name="page_conversations_list",
        tier="read",
        summary="List a Page's message conversations",
        api="GET {page_id}/conversations",
        description=(
            "List Messenger conversations a Page has with people, with the last snippet, "
            "participants and unread count. Needs a token with pages_messaging."
        ),
    ),
    ToolSpec(
        name="conversation_messages_list",
        tier="read",
        summary="List messages in a conversation",
        api="GET {conversation_id}/messages",
        description="Read the messages of one Page conversation, newest first.",
    ),
    ToolSpec(
        name="token_debug",
        tier="read",
        summary="Inspect an access token's app, scopes and expiry",
        api="GET debug_token",
        description=(
            "Debug a Facebook access token: which app issued it, the granted scopes, the "
            "Page or user it belongs to, and when it expires. Omit input_token to inspect "
            "the token this server is configured with."
        ),
        notes="Needs an app access token to see other apps' tokens; a Page token can debug itself.",
    ),
    ToolSpec(
        name="graph_follow",
        tier="read",
        summary="Follow a Graph API pagination link",
        api="GET <absolute url>",
        description=(
            "Fetch the next page of an edge from the next_url a previous read returned. "
            "Graph pagination links already carry the cursor and token, so this is the "
            "reliable way to page through a long edge."
        ),
    ),
    ToolSpec(
        name="graph_batch",
        tier="read",
        summary="Run several Graph API calls in one request",
        api="POST batch",
        description=(
            "Run a batch of Graph API requests in one call to save round trips. Each entry "
            "is {method, relative_path, name}. GET entries are always allowed; POST and "
            "DELETE entries need the write permission."
        ),
    ),
    # ---------------- write ----------------
    ToolSpec(
        name="page_publish",
        tier="write",
        summary="Publish a post to a Page",
        api="POST {page_id}/feed",
        description=(
            "Publish a post on a Page. Provide message, and optionally link, picture, "
            "a published flag, or scheduled_publish_time (Unix epoch) to schedule it. "
            "Returns the new post id."
        ),
        notes="Needs a token that can perform the PUBLISH task (pages_manage_posts).",
    ),
    ToolSpec(
        name="post_comment_create",
        tier="write",
        summary="Comment on a post",
        api="POST {post_id}/comments",
        description="Add a comment to a post as the Page. Returns the new comment id.",
        notes="Needs a token that can perform the COMMENT task (pages_manage_engagement).",
    ),
    ToolSpec(
        name="page_photo_publish",
        tier="write",
        summary="Publish a photo from a URL",
        api="POST {page_id}/photos",
        description=(
            "Publish a photo to a Page from a public image URL, with an optional caption. "
            "Set published=false to create an unpublished photo whose id can be attached "
            "to a later post."
        ),
    ),
    ToolSpec(
        name="page_video_publish",
        tier="write",
        summary="Publish a video from a URL",
        api="POST {page_id}/videos",
        description=(
            "Publish a video to a Page from a public video URL, with optional title and "
            "description. Publishing is asynchronous: the video appears once processing ends."
        ),
    ),
    ToolSpec(
        name="graph_write",
        tier="write",
        summary="Make one Graph API POST to any path",
        api="POST <path>",
        description=(
            "POST to a Graph API path with explicit parameters, for write endpoints this "
            "server does not wrap directly. A path that starts with me uses the user "
            "token when one is configured."
        ),
    ),
    # ---------------- delete ----------------
    ToolSpec(
        name="comment_delete",
        tier="delete",
        summary="Delete a comment",
        api="DELETE {comment_id}",
        description="Delete a comment by id. The comment is gone for good and cannot be restored.",
        notes="A Page can only delete comments on its own posts.",
    ),
    ToolSpec(
        name="post_delete",
        tier="delete",
        summary="Delete a post",
        api="DELETE {post_id}",
        description="Delete a post by id. The post and its comments are gone for good.",
        notes="A Page can only delete its own posts.",
    ),
)

# Tools that exist regardless of MCP_TOOL_PERMISSIONS: they describe this
# server rather than touching Facebook data.
HELPER_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="facebook_pages_status",
        tier="helper",
        summary="Report this server's configuration and readiness",
        api="(local)",
        description=(
            "Report the configured Graph API version, which tokens are set, and the "
            "enabled tool permissions. Set ping=true to make one live Graph API call "
            "(debug_token) and confirm the token works."
        ),
    ),
    ToolSpec(
        name="facebook_pages_list_tools",
        tier="helper",
        summary="List the tools this server exposes",
        api="(local)",
        description=(
            "List every tool the AI may call, grouped by tier, with the Graph API "
            "endpoint behind each. Use this to discover what is available."
        ),
    ),
)


def spec_by_name(name: str) -> ToolSpec | None:
    for spec in (*TOOL_SPECS, *HELPER_TOOL_SPECS):
        if spec.name == name:
            return spec
    return None


def join_fields(defaults: tuple[str, ...], fields: list[str] | None, extra_fields: list[str] | None) -> str:
    """Build the Graph ``fields`` parameter.

    ``fields`` replaces the defaults outright. ``extra_fields`` adds to them.
    Order is preserved and duplicates are dropped, because the Graph API
    rejects nothing but silently returns less useful payloads when the caller
    asks twice.
    """
    chosen = list(fields) if fields else list(defaults)
    if extra_fields:
        chosen.extend(extra_fields)
    seen: list[str] = []
    for item in chosen:
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return ",".join(seen)
