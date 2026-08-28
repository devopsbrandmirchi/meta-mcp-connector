"""
Meta MCP connector — plain-language access to Meta / Facebook ads via Graph API.

Users ask in natural language about campaigns, ad sets, ads, and performance.
Data is fetched live from the Facebook Marketing API using FACEBOOK_APP_ID
and FACEBOOK_APP_SECRET (app access token).

Run:
  python meta_mcp_server.py           # stdio (Claude Desktop / Cursor)
  python meta_mcp_server.py --http    # http://127.0.0.1:8001/mcp
"""

from __future__ import annotations

import os
import sys
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # optional
    def load_dotenv(*_a, **_k):  # type: ignore
        return False

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from mcp.server import MCPServer as FastMCP

from meta_graph import MetaGraphError, graph_client

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))


def _metadata_get(path: str) -> str:
    """Read Cloud Run / GCE metadata (empty string when not on GCP)."""
    try:
        import httpx

        res = httpx.get(
            f"http://metadata.google.internal/computeMetadata/v1/{path.lstrip('/')}",
            headers={"Metadata-Flavor": "Google"},
            timeout=2.0,
        )
        if res.is_success:
            return (res.text or "").strip()
    except Exception:
        pass
    return ""


def _resolve_public_url() -> str:
    """
    Claude custom connectors need OAuth DCR. Enable when MCP_PUBLIC_URL is set
    (deploy script sets this). Git → Cloud Run often omits it — derive from
    K_SERVICE + metadata, then known production fallbacks.
    """
    for key in ("MCP_PUBLIC_URL", "BASE_URL", "SERVICE_URL"):
        raw = os.environ.get(key, "").strip().rstrip("/")
        if raw:
            return raw

    service = os.environ.get("K_SERVICE", "").strip()
    if not service:
        return ""

    region = (
        os.environ.get("CLOUD_RUN_REGION", "").strip()
        or os.environ.get("GOOGLE_CLOUD_REGION", "").strip()
    )
    project_number = (
        os.environ.get("CLOUD_RUN_PROJECT_NUMBER", "").strip()
        or os.environ.get("GCP_PROJECT_NUMBER", "").strip()
        or os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER", "").strip()
    )

    if not project_number:
        project_number = _metadata_get("project/numeric-project-id")
    if not region:
        # projects/123456789/regions/us-central1
        meta_region = _metadata_get("instance/region")
        if "/" in meta_region:
            region = meta_region.rsplit("/", 1)[-1]

    if service and region and project_number:
        return f"https://{service}-{project_number}.{region}.run.app"
    # Known Git → Cloud Run service until MCP_PUBLIC_URL is set explicitly.
    if service == "meta-mcp-connector-git":
        return "https://meta-mcp-connector-git-573223329822.us-central1.run.app"
    return ""


MCP_PUBLIC_URL = _resolve_public_url()
MCP_OAUTH_PASSWORD = os.environ.get("MCP_OAUTH_PASSWORD", "").strip() or None
MCP_RESOURCE_URL = f"{MCP_PUBLIC_URL}/mcp" if MCP_PUBLIC_URL else ""


def _patch_claude_oauth_compat(scope: str = "meta") -> None:
    """
    Claude.ai MCP OAuth requirements (see anthropics/claude-ai-mcp issues #5, #82, #214):

    1. Advertise token_endpoint_auth_methods_supported including \"none\".
    2. Treat DCR clients as public (PKCE) even when they request client_secret_post.
    3. Expose scopes_supported and CIMD support in authorization-server metadata.
    4. Strip unsupported jwt-bearer grant from Claude CIMD registration payloads.
    5. Preserve HTTPS client_id values during DCR (Claude CIMD).
    """
    import json

    import mcp.server.auth.handlers.register as register_mod
    import mcp.server.auth.routes as auth_routes

    if getattr(auth_routes, "_meta_claude_patch", False):
        return

    original_build = auth_routes.build_metadata

    def build_metadata(*args, **kwargs):
        metadata = original_build(*args, **kwargs)
        methods = list(metadata.token_endpoint_auth_methods_supported or [])
        if "none" not in methods:
            methods.insert(0, "none")
        metadata.token_endpoint_auth_methods_supported = methods
        if not metadata.scopes_supported:
            metadata.scopes_supported = [scope]
        metadata.client_id_metadata_document_supported = True
        # Claude canonical URLs omit trailing slashes on the issuer host.
        from pydantic import AnyHttpUrl

        metadata.issuer = AnyHttpUrl(str(metadata.issuer).rstrip("/"))
        return metadata

    auth_routes.build_metadata = build_metadata

    original_create_pr = auth_routes.create_protected_resource_routes

    def create_protected_resource_routes(
        resource_url, authorization_servers, scopes_supported=None, **kwargs
    ):
        from pydantic import AnyHttpUrl

        normalized = [
            AnyHttpUrl(str(url).rstrip("/")) for url in authorization_servers
        ]
        return original_create_pr(
            resource_url,
            normalized,
            scopes_supported=scopes_supported,
            **kwargs,
        )

    auth_routes.create_protected_resource_routes = create_protected_resource_routes

    register_mod._meta_cimd_client_id = None
    original_uuid4 = register_mod.uuid4

    class _CimdClientId:
        def __init__(self, value: str) -> None:
            self._value = value

        def __str__(self) -> str:
            return self._value

    def uuid4_with_cimd():
        cimd = register_mod._meta_cimd_client_id
        if cimd:
            register_mod._meta_cimd_client_id = None
            return _CimdClientId(cimd)
        return original_uuid4()

    register_mod.uuid4 = uuid4_with_cimd

    original_handle = register_mod.RegistrationHandler.handle

    async def handle(self, request):
        try:
            body = await request.body()
            data = json.loads(body)
            if data.get("token_endpoint_auth_method") in (
                None,
                "client_secret_post",
                "client_secret_basic",
            ):
                data["token_endpoint_auth_method"] = "none"
            grants = data.get("grant_types")
            if isinstance(grants, list):
                data["grant_types"] = [
                    g
                    for g in grants
                    if g != "urn:ietf:params:oauth:grant-type:jwt-bearer"
                ]
            client_id = data.get("client_id")
            if isinstance(client_id, str) and client_id.startswith("https://"):
                register_mod._meta_cimd_client_id = client_id
            from starlette.requests import Request as StarletteRequest

            patched = json.dumps(data).encode()

            async def receive():
                return {"type": "http.request", "body": patched, "more_body": False}

            request = StarletteRequest(request.scope, receive)
        except Exception:
            register_mod._meta_cimd_client_id = None
        return await original_handle(self, request)

    register_mod.RegistrationHandler.handle = handle
    auth_routes._meta_claude_patch = True


def _build_mcp():
    if not MCP_PUBLIC_URL:
        return FastMCP("meta")

    _patch_claude_oauth_compat()

    from pydantic import AnyHttpUrl

    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

    from meta_oauth import ClaudeOAuthProvider

    provider = ClaudeOAuthProvider(
        base_url=MCP_PUBLIC_URL,
        password=MCP_OAUTH_PASSWORD,
    )
    # valid_scopes=None → accept whatever Claude sends during DCR (avoids ofid_ register failures).
    # resource_server_url MUST be the MCP path (/mcp) so RFC 9728 metadata is served at
    # /.well-known/oauth-protected-resource/mcp (Claude uses the connector URL as the resource).
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(MCP_PUBLIC_URL),
        resource_server_url=AnyHttpUrl(MCP_RESOURCE_URL),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=None,
            default_scopes=["meta"],
        ),
        required_scopes=["meta"],
    )
    return FastMCP(
        "meta",
        auth_server_provider=provider,
        auth=auth,
    )


mcp = _build_mcp()


def _fb():
    return graph_client()


def _ensure_fb() -> None:
    client = _fb()
    client.validate_config()
    if client.user_access_token:
        client.validate_user_token()
    else:
        client.exchange_token()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str, *, default: Optional[date] = None) -> date:
    raw = (value or "").strip()
    if not raw:
        if default is None:
            raise ValueError("date required (YYYY-MM-DD)")
        return default
    lowered = raw.lower()
    today = date.today()
    if lowered in ("today",):
        return today
    if lowered in ("yesterday",):
        return today - timedelta(days=1)
    return datetime.strptime(raw[:10], "%Y-%m-%d").date()


def _fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def _fmt_float(n: Any, *, decimals: int = 2) -> str:
    try:
        return f"{float(n):,.{decimals}f}"
    except (TypeError, ValueError):
        return "0.00"


def _fmt_usd(n: Any) -> str:
    return f"${_fmt_float(n)}"


def _sum_field(rows: list[dict], field: str, *, as_float: bool = False) -> float:
    total = 0.0
    for r in rows:
        v = r.get(field)
        if v is None:
            continue
        try:
            total += float(v) if as_float else int(v)
        except (TypeError, ValueError):
            pass
    return total


def _table_lines(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "No rows matched."
    lines = [" | ".join(headers)]
    lines.append(" | ".join("---" for _ in headers))
    for row in rows:
        lines.append(" | ".join(str(c) for c in row))
    return "\n".join(lines)


def _date_range(start_date: str, end_date: str = "") -> tuple[date, date]:
    start = _parse_date(start_date)
    end = _parse_date(end_date, default=start)
    if end < start:
        start, end = end, start
    return start, end


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    campaign: str = "",
) -> list[dict[str, Any]]:
    if not campaign.strip():
        return rows
    q = campaign.strip().lower()
    return [
        r
        for r in rows
        if q in (r.get("campaign_name") or "").lower()
        or q in (r.get("campaign_id") or "").lower()
        or q in (r.get("adset_name") or "").lower()
        or q in (r.get("ad_name") or "").lower()
    ]


def _fetch_insights(
    *,
    level: str,
    start: date,
    end: date,
    account_id: str = "",
    breakdowns: Optional[str] = None,
    campaign: str = "",
    time_increment: Any = 1,
) -> list[dict[str, Any]]:
    client = _fb()
    if campaign.strip():
        return client.fetch_insights_for_campaign(
            account_id,
            campaign,
            level=level,
            since=start,
            until=end,
            breakdowns=breakdowns,
            time_increment=time_increment,
        )
    return client.fetch_insights(
        account_id,
        level=level,
        since=start,
        until=end,
        breakdowns=breakdowns,
        time_increment=time_increment,
    )


def _aggregate_metrics(
    rows: list[dict],
    *,
    group_by: Optional[str] = None,
) -> dict[str, dict[str, float]]:
    metrics = [
        "amount_spent_usd",
        "impressions",
        "reach",
        "clicks_all",
        "purchases",
        "meta_purchases",
        "purchases_value",
        "meta_purchase_value",
    ]
    buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {m: 0.0 for m in metrics}
    )
    for r in rows:
        label = str(r.get(group_by) or "(blank)") if group_by else "__total__"
        for m in metrics:
            try:
                buckets[label][m] += float(r.get(m) or 0)
            except (TypeError, ValueError):
                pass
    return buckets


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def help_meta() -> str:
    """Explain what this Meta ads connector can answer in simple language."""
    return """You can ask things like:

ACCOUNTS
- "List my Meta ad accounts"
- "Is the Facebook integration configured?"

CAMPAIGNS & AD SETS
- "List campaigns for Drag Race"
- "List ad sets"

PERFORMANCE (live from Facebook Graph API)
- "Meta ads summary from 2026-08-01 to 2026-08-15"
- "Daily spend and impressions last 7 days"
- "Break down spend by campaign / ad set / ad / platform / placement"
- "Top ads by spend this month"

Data is fetched live — not from a database cache.
Dates are YYYY-MM-DD (or yesterday / today).
Set FACEBOOK_AD_ACCOUNT_ID in .env or pass account_id (act_…)."""


@mcp.tool()
def list_accounts(limit: int = 20) -> str:
    """List Meta ad accounts accessible with the configured Facebook token."""
    rows = _fb().list_all_ad_accounts(limit=max(1, min(limit, 100)))
    if not rows:
        return "No ad accounts returned. Check FACEBOOK_APP_ID / FACEBOOK_APP_SECRET."
    body = [
        [
            f"act_{r.get('account_id', '')}",
            r.get("name") or "",
            r.get("parent_business_name") or "—",
            r.get("parent_business_id") or "—",
            r.get("relationship") or "",
            r.get("currency") or "",
            r.get("account_status_label") or r.get("account_status") or "",
        ]
        for r in rows
    ]
    return (
        f"Found {len(rows)} ad account(s) via Graph API.\n"
        + _table_lines(
            [
                "Account ID",
                "Name",
                "Parent Business",
                "Parent Business ID",
                "Relationship",
                "Currency",
                "Status",
            ],
            body,
        )
    )


@mcp.tool()
def list_campaigns(
    search: str = "",
    account_id: str = "",
    limit: int = 50,
) -> str:
    """List campaigns from the Facebook ad account (live Graph API)."""
    rows = _fb().list_campaign_objects(
        account_id=account_id, search=search, limit=max(1, min(limit, 200))
    )
    if not rows:
        return "No campaigns matched."
    body = [
        [
            r.get("name") or "",
            r.get("id") or "",
            r.get("status") or "",
            r.get("objective") or "",
        ]
        for r in rows
    ]
    return (
        f"Found {len(rows)} campaign(s).\n"
        + _table_lines(["Campaign", "ID", "Status", "Objective"], body)
    )


@mcp.tool()
def list_adsets(
    search: str = "",
    account_id: str = "",
    limit: int = 50,
) -> str:
    """List ad sets from the Facebook ad account (live Graph API)."""
    rows = _fb().list_adset_objects(
        account_id=account_id, search=search, limit=max(1, min(limit, 200))
    )
    if not rows:
        return "No ad sets matched."
    body = [
        [
            r.get("name") or "",
            r.get("id") or "",
            r.get("status") or "",
            r.get("campaign_id") or "",
        ]
        for r in rows
    ]
    return (
        f"Found {len(rows)} ad set(s).\n"
        + _table_lines(["Ad set", "ID", "Status", "Campaign ID"], body)
    )


@mcp.tool()
def get_integration_status() -> str:
    """Check Facebook app credentials and token health (no secrets exposed)."""
    client = _fb()
    lines = [
        "Facebook integration (Graph API)",
        f"App ID: {'configured' if client.app_id else 'missing FACEBOOK_APP_ID'}",
        f"App secret: {'configured' if client.app_secret else 'missing FACEBOOK_APP_SECRET'}",
        f"User token: {'configured' if client.user_access_token else 'not set (FACEBOOK_ACCESS_TOKEN)'}",
        f"Default account: act_{client.default_account}" if client.default_account else "Default account: not set (FACEBOOK_AD_ACCOUNT_ID)",
    ]
    try:
        client.validate_config()
        if client.user_access_token:
            lines.append("Auth mode: user token (FACEBOOK_ACCESS_TOKEN)")
            try:
                debug = client.validate_user_token()
            except MetaGraphError as e:
                lines.append(f"User token: error — {e}")
                return "\n".join(lines)
            lines.append(f"User token valid: {debug.get('is_valid', 'unknown')}")
            token_app = debug.get("app_id")
            if token_app:
                lines.append(f"Token app ID: {token_app}")
            scopes = debug.get("scopes") or []
            if scopes:
                lines.append("User token scopes: " + ", ".join(scopes))
            exp = debug.get("expires_at")
            if exp:
                lines.append(
                    f"User token expires: {datetime.utcfromtimestamp(int(exp)).isoformat()}Z"
                )
        else:
            client.exchange_token()
            lines.append("Auth mode: app token only (client_credentials)")
            lines.append("App token: OK")
            lines.append(
                "Note: set FACEBOOK_ACCESS_TOKEN with ads_read to list accounts and pull insights."
            )
    except (MetaGraphError, RuntimeError) as e:
        lines.append(f"Status: error — {e}")
    return "\n".join(lines)


@mcp.tool()
def get_ads_summary(
    start_date: str,
    end_date: str = "",
    campaign: str = "",
    account_id: str = "",
) -> str:
    """Get Meta ads KPI totals for a date range (live Graph API insights)."""
    start, end = _date_range(start_date, end_date)
    # Account-level aggregate for the window (time_increment=all_days). Campaign
    # filter resolves to the campaign edge when possible (D-5).
    rows = _fetch_insights(
        level="account" if not campaign.strip() else "campaign",
        start=start,
        end=end,
        account_id=account_id,
        campaign=campaign,
        time_increment="all_days",
    )
    if campaign.strip():
        rows = _filter_rows(rows, campaign=campaign)
    if not rows:
        return f"No ad data found for {start.isoformat()} to {end.isoformat()}."

    spend = _sum_field(rows, "amount_spent_usd", as_float=True)
    impressions = _sum_field(rows, "impressions")
    # Reach is not additive across rows — use max for multi-row, else the value.
    reach_vals = [int(r.get("reach") or 0) for r in rows]
    reach = max(reach_vals) if len(reach_vals) > 1 else (reach_vals[0] if reach_vals else 0)
    clicks = _sum_field(rows, "clicks_all")
    purchases = _sum_field(rows, "purchases")
    purchase_value = _sum_field(rows, "purchases_value", as_float=True)

    lines = [
        "Meta ads summary (Graph API)",
        f"Dates: {start.isoformat()} to {end.isoformat()}",
    ]
    if account_id.strip():
        lines.append(f"Account: {account_id.strip()}")
    elif _fb().default_account:
        lines.append(f"Account: act_{_fb().default_account}")
    if campaign.strip():
        lines.append(f"Filter: {campaign.strip()}")
    lines.extend([
        f"Spend: {_fmt_usd(spend)}",
        f"Impressions: {_fmt_int(impressions)}",
        f"Reach: {_fmt_int(reach)}",
        f"Clicks: {_fmt_int(clicks)}",
        f"Purchases (omni): {_fmt_int(purchases)}",
        f"Purchase value: {_fmt_usd(purchase_value)}",
    ])
    if clicks > 0:
        lines.append(f"CPC: {_fmt_usd(spend / clicks)}")
    if impressions > 0:
        lines.append(f"CPM: {_fmt_usd(spend / impressions * 1000)}")
    return "\n".join(lines)


@mcp.tool()
def get_daily_trend(
    start_date: str,
    end_date: str = "",
    campaign: str = "",
    account_id: str = "",
) -> str:
    """Daily Meta ads spend, impressions, clicks, and purchases (Graph API)."""
    start, end = _date_range(start_date, end_date)
    rows = _fetch_insights(
        level="account" if not campaign.strip() else "campaign",
        start=start,
        end=end,
        account_id=account_id,
        campaign=campaign,
        time_increment=1,
    )
    if campaign.strip():
        rows = _filter_rows(rows, campaign=campaign)

    daily: dict[str, dict[str, float]] = defaultdict(
        lambda: {"spend": 0.0, "impressions": 0, "clicks": 0, "purchases": 0}
    )
    for r in rows:
        d = str(r.get("date_start") or r.get("day") or "")[:10]
        if not d:
            continue
        daily[d]["spend"] += float(r.get("amount_spent_usd") or 0)
        daily[d]["impressions"] += int(r.get("impressions") or 0)
        daily[d]["clicks"] += int(r.get("clicks_all") or 0)
        daily[d]["purchases"] += int(r.get("purchases") or 0)

    if not daily:
        label = campaign.strip() or "account"
        return f"No daily data for {label} in that range."

    table = [
        [
            d,
            _fmt_usd(v["spend"]),
            _fmt_int(v["impressions"]),
            _fmt_int(v["clicks"]),
            _fmt_int(v["purchases"]),
        ]
        for d, v in sorted(daily.items())
    ]
    return (
        f"Daily trend (Graph API)\n"
        f"{start.isoformat()} to {end.isoformat()}\n"
        + _table_lines(["Date", "Spend", "Impressions", "Clicks", "Purchases"], table)
    )


@mcp.tool()
def get_performance_breakdown(
    start_date: str,
    end_date: str = "",
    by: str = "campaign",
    campaign: str = "",
    account_id: str = "",
    limit: int = 25,
) -> str:
    """Break down Meta ads performance by campaign, adset, ad, platform, or placement."""
    # level sets row granularity; breakdowns sets dimensional splits (D-2).
    dim_map = {
        "campaign": ("campaign", "campaign_name", None),
        "adset": ("adset", "adset_name", None),
        "ad": ("ad", "ad_name", None),
        "platform": ("ad", "publisher_platform", "publisher_platform"),
        "placement": ("ad", "placement", "publisher_platform,platform_position"),
    }
    key = (by or "campaign").strip().lower()
    spec = dim_map.get(key)
    if not spec:
        return "Unknown breakdown. Use by=campaign|adset|ad|platform|placement."

    start, end = _date_range(start_date, end_date)
    level, col, breakdowns = spec

    rows = _fetch_insights(
        level=level,
        start=start,
        end=end,
        account_id=account_id,
        breakdowns=breakdowns,
        campaign=campaign,
        time_increment="all_days",
    )
    if campaign.strip() and not _fb().resolve_campaign_id(account_id, campaign):
        rows = _filter_rows(rows, campaign=campaign)

    if key == "placement":
        for r in rows:
            r["placement"] = (
                f"{r.get('publisher_platform') or 'unknown'}/"
                f"{r.get('platform_position') or 'unknown'}"
            )

    if not rows:
        return f"No data for breakdown by {key} in that range."

    buckets = _aggregate_metrics(rows, group_by=col)
    ranked = sorted(
        buckets.items(),
        key=lambda kv: kv[1]["amount_spent_usd"],
        reverse=True,
    )[: max(1, min(limit, 100))]

    table = [
        [
            name if name and name != "(blank)" else "(unnamed)",
            _fmt_usd(v["amount_spent_usd"]),
            _fmt_int(v["impressions"]),
            _fmt_int(v["clicks_all"]),
            _fmt_int(v["purchases"]),
        ]
        for name, v in ranked
    ]
    return (
        f"Performance by {key} (Graph API)\n"
        f"{start.isoformat()} to {end.isoformat()}\n"
        + _table_lines(
            [key.title(), "Spend", "Impressions", "Clicks", "Purchases"], table
        )
    )


@mcp.tool()
def get_top_ads(
    start_date: str,
    end_date: str = "",
    campaign: str = "",
    account_id: str = "",
    sort_by: str = "spend",
    limit: int = 20,
) -> str:
    """Top-performing ads ranked by spend, impressions, clicks, or purchases."""
    start, end = _date_range(start_date, end_date)
    rows = _fetch_insights(
        level="ad",
        start=start,
        end=end,
        account_id=account_id,
        campaign=campaign,
        time_increment="all_days",
    )
    if campaign.strip() and not _fb().resolve_campaign_id(account_id, campaign):
        rows = _filter_rows(rows, campaign=campaign)
    if not rows:
        return "No ad-level data in that range."

    buckets: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
        lambda: {
            "amount_spent_usd": 0.0,
            "impressions": 0.0,
            "clicks_all": 0.0,
            "purchases": 0.0,
        }
    )
    for r in rows:
        k = (
            r.get("ad_name") or "(unnamed)",
            r.get("adset_name") or "",
            r.get("campaign_name") or "",
        )
        for m in ("amount_spent_usd", "impressions", "clicks_all", "purchases"):
            buckets[k][m] += float(r.get(m) or 0)

    sort_key = {
        "spend": "amount_spent_usd",
        "impressions": "impressions",
        "clicks": "clicks_all",
        "purchases": "purchases",
    }.get((sort_by or "spend").lower(), "amount_spent_usd")

    ranked = sorted(buckets.items(), key=lambda kv: kv[1][sort_key], reverse=True)[
        : max(1, min(limit, 100))
    ]
    table = [
        [
            ad[:50],
            adset[:40],
            _fmt_usd(v["amount_spent_usd"]),
            _fmt_int(v["impressions"]),
            _fmt_int(v["clicks_all"]),
            _fmt_int(v["purchases"]),
        ]
        for (ad, adset, _), v in ranked
    ]
    return (
        f"Top ads by {sort_by} (Graph API)\n"
        f"{start.isoformat()} to {end.isoformat()}\n"
        + _table_lines(
            ["Ad", "Ad set", "Spend", "Impressions", "Clicks", "Purchases"], table
        )
    )


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


@mcp.custom_route("/.well-known/openid-configuration", methods=["GET", "OPTIONS"])
async def openid_configuration(request):
    """Claude falls back to OIDC discovery when RFC 8414 metadata is unavailable."""
    from starlette.responses import JSONResponse, Response

    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_cors_headers())
    if not MCP_PUBLIC_URL:
        return JSONResponse({"error": "oauth_disabled"}, status_code=404)

    from pydantic import AnyHttpUrl

    from mcp.server.auth.routes import build_metadata
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions

    metadata = build_metadata(
        AnyHttpUrl(MCP_PUBLIC_URL.rstrip("/") + "/"),
        None,
        ClientRegistrationOptions(
            enabled=True,
            valid_scopes=None,
            default_scopes=["meta"],
        ),
        RevocationOptions(),
    )
    return JSONResponse(
        metadata.model_dump(mode="json", exclude_none=True),
        headers={"Cache-Control": "public, max-age=3600", **_cors_headers()},
    )


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Public health check (no auth) — used to verify Cloud Run / OAuth readiness."""
    from starlette.responses import JSONResponse

    oauth_ready = bool(MCP_PUBLIC_URL)
    return JSONResponse(
        {
            "status": "ok",
            "service": "meta",
            "oauth_enabled": oauth_ready,
            "public_url": MCP_PUBLIC_URL or None,
            "mcp_url": MCP_RESOURCE_URL or None,
            "oauth_discovery": (
                f"{MCP_PUBLIC_URL}/.well-known/oauth-authorization-server"
                if oauth_ready
                else None
            ),
            "protected_resource_metadata": (
                f"{MCP_PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"
                if oauth_ready
                else None
            ),
        }
    )


@mcp.custom_route("/api/account-spend", methods=["GET", "OPTIONS"])
async def api_account_spend(request):
    """JSON endpoint for account-spend.html — spend per ad account for a date."""
    from starlette.responses import JSONResponse, Response

    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_cors_headers())

    raw_date = (request.query_params.get("date") or "").strip()
    if raw_date:
        try:
            report_date = _parse_date(raw_date)
        except ValueError as e:
            return JSONResponse(
                {"error": str(e), "accounts": []},
                status_code=400,
                headers=_cors_headers(),
            )
    else:
        report_date = date.today() - timedelta(days=1)

    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))

    try:
        _ensure_fb()
        rows = _fb().list_accounts_with_spend(
            report_date, report_date, account_limit=limit
        )
        with_spend = [r for r in rows if float(r.get("spend") or 0) > 0]
        total_spend = round(sum(float(r.get("spend") or 0) for r in rows), 2)
        highest = with_spend[0] if with_spend else None
        lowest = with_spend[-1] if with_spend else None
        return JSONResponse(
            {
                "date": report_date.isoformat(),
                "account_count": len(rows),
                "accounts_with_spend": len(with_spend),
                "total_spend": total_spend,
                "highest": highest,
                "lowest": lowest,
                "accounts": rows,
            },
            headers=_cors_headers(),
        )
    except (MetaGraphError, RuntimeError, ValueError) as e:
        return JSONResponse(
            {"error": str(e), "accounts": []},
            status_code=500,
            headers=_cors_headers(),
        )


@mcp.custom_route("/account-spend", methods=["GET"])
async def account_spend_page(request):
    """Serve account-spend.html from the connector."""
    from starlette.responses import FileResponse

    return FileResponse(
        os.path.join(_SCRIPT_DIR, "account-spend.html"),
        media_type="text/html",
    )


@mcp.custom_route("/api/accounts", methods=["GET", "OPTIONS"])
async def api_list_accounts(request):
    """JSON endpoint for test-accounts.html (local HTTP mode)."""
    from starlette.responses import JSONResponse, Response

    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_cors_headers())

    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))
    try:
        _ensure_fb()
        rows = _fb().list_all_ad_accounts(limit=limit)
        return JSONResponse(
            {
                "count": len(rows),
                "accounts": [
                    {
                        **row,
                        "account_id": f"act_{row.get('account_id', '')}",
                    }
                    for row in rows
                ],
            },
            headers=_cors_headers(),
        )
    except (MetaGraphError, RuntimeError, ValueError) as e:
        return JSONResponse(
            {"error": str(e), "accounts": []},
            status_code=500,
            headers=_cors_headers(),
        )


@mcp.custom_route("/test-accounts", methods=["GET"])
async def test_accounts_page(request):
    """Serve test-accounts.html from the connector (avoids file:// CORS issues)."""
    from starlette.responses import FileResponse

    return FileResponse(
        os.path.join(_SCRIPT_DIR, "test-accounts.html"),
        media_type="text/html",
    )


@mcp.custom_route("/get-token", methods=["GET"])
async def get_token_page(request):
    """Serve get-token.html from the connector."""
    from starlette.responses import FileResponse

    return FileResponse(
        os.path.join(_SCRIPT_DIR, "get-token.html"),
        media_type="text/html",
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    on_cloud = bool(os.environ.get("K_SERVICE"))
    default_http = (
        os.environ.get("MCP_TRANSPORT", "").lower() == "http" or on_cloud
    )
    default_host = os.environ.get("HOST") or (
        "0.0.0.0" if (default_http or on_cloud) else "127.0.0.1"
    )
    default_port = int(os.environ.get("PORT", "8080" if on_cloud else "8001"))

    parser = argparse.ArgumentParser(
        description="Meta MCP connector (Facebook Graph API)."
    )
    parser.add_argument("--http", action="store_true", default=default_http)
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    print("Meta MCP → Facebook Graph API", file=sys.stderr, flush=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    # httpx logs full request URLs (including tokens) at INFO — keep those quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if MCP_PUBLIC_URL:
        print(f"OAuth for Claude: enabled (issuer={MCP_PUBLIC_URL})", file=sys.stderr, flush=True)
    elif on_cloud:
        print(
            "WARNING: MCP_PUBLIC_URL not set — Claude OAuth DCR is disabled. "
            "Set MCP_PUBLIC_URL=https://YOUR-SERVICE.run.app on Cloud Run.",
            file=sys.stderr,
            flush=True,
        )
    try:
        _ensure_fb()
        print("Facebook app credentials loaded.", file=sys.stderr, flush=True)
    except (RuntimeError, MetaGraphError) as e:
        # Do not crash the process — OAuth /health must stay up for Claude register.
        print(f"WARNING: Facebook credentials not ready: {e}", file=sys.stderr, flush=True)

    tools = [
        "help_meta",
        "list_accounts",
        "list_campaigns",
        "list_adsets",
        "get_integration_status",
        "get_ads_summary",
        "get_daily_trend",
        "get_performance_breakdown",
        "get_top_ads",
    ]
    print("Tools: " + ", ".join(tools), file=sys.stderr, flush=True)

    try:
        if args.http:
            endpoint = f"http://{args.host}:{args.port}/mcp"
            public = f"{MCP_PUBLIC_URL}/mcp" if MCP_PUBLIC_URL else endpoint
            print(
                f"\nHTTP mode\n"
                f"  MCP endpoint: {public}\n"
                f"  Account spend: http://{args.host}:{args.port}/account-spend\n"
                f"  Test page:     http://{args.host}:{args.port}/test-accounts\n"
                f"  Test API:      http://{args.host}:{args.port}/api/accounts\n"
                f"  Stop: Ctrl+C\n",
                file=sys.stderr,
                flush=True,
            )
            from mcp.server.transport_security import TransportSecuritySettings

            mcp.run(
                transport="streamable-http",
                host=args.host,
                port=args.port,
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=False
                ),
            )
        else:
            mcp.run()
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
