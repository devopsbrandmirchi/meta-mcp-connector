"""
Facebook Graph API client for the Meta MCP connector.

Auth prefers FACEBOOK_ACCESS_TOKEN (user token with ads_read). Falls back to
app token via client_credentials when no user token is set.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

FB_API_VERSION = os.environ.get("FACEBOOK_API_VERSION", "v21.0").strip() or "v21.0"
FB_BASE = f"https://graph.facebook.com/{FB_API_VERSION}"

logger = logging.getLogger("meta-graph")

# Cap creative image downloads so MCP responses stay usable for Claude sheets.
CREATIVE_IMAGE_MAX_BYTES = 5 * 1024 * 1024

# Core insight fields. Name fields are required when level is set — without them
# Graph returns blank campaign/adset/ad names (defect D-2 / D-3).
INSIGHT_FIELDS = [
    "campaign_id",
    "campaign_name",
    "adset_id",
    "adset_name",
    "ad_id",
    "ad_name",
    "impressions",
    "clicks",
    "inline_link_clicks",
    "spend",
    "cpc",
    "cpm",
    "ctr",
    "reach",
    "frequency",
    "actions",
    "action_values",
    "cost_per_action_type",
    "purchase_roas",
    "date_start",
    "date_stop",
]

# Prefer omni_purchase (consolidated). Do not sum with purchase — that double-counts.
PURCHASE_ACTION = "omni_purchase"
PURCHASE_FALLBACK = "purchase"


class MetaGraphError(Exception):
    """Facebook Graph API error."""


@dataclass
class CampaignScope:
    """How a campaign argument maps onto insights.

    campaign_id is set only for a numeric id, a unique exact name, or a unique
    partial name. Several name matches leave campaign_id empty so the caller
    aggregates every campaign whose name contains the query.
    """

    query: str
    campaign_id: Optional[str] = None
    matched: int = 0
    truncated: bool = False

    def note(self) -> str:
        if not self.query:
            return ""
        if self.campaign_id:
            return f"Campaign filter: {self.query} (1 campaign)"
        if self.matched > 0:
            count = f"{self.matched}+" if self.truncated else str(self.matched)
            noun = "campaign" if count == "1" else "campaigns"
            return f"Campaign filter: {self.query} ({count} {noun}, contains)"
        return f"Campaign filter: {self.query} (contains; no campaign name matched)"


def select_campaign_scope(
    campaign: str,
    rows: list[dict[str, Any]],
    *,
    truncated: bool = False,
) -> CampaignScope:
    """Pick one campaign id, or a contains-match over every returned name."""
    q = (campaign or "").strip()
    q_lower = q.lower()
    exact = [r for r in rows if (r.get("name") or "").lower() == q_lower]
    if len(exact) == 1 and not truncated:
        return CampaignScope(
            query=q, campaign_id=str(exact[0].get("id") or "") or None, matched=1
        )
    pool = exact if len(exact) > 1 else [
        r for r in rows if q_lower in (r.get("name") or "").lower()
    ]
    if len(pool) == 1 and not truncated:
        return CampaignScope(
            query=q, campaign_id=str(pool[0].get("id") or "") or None, matched=1
        )
    if pool:
        return CampaignScope(
            query=q, campaign_id=None, matched=len(pool), truncated=truncated
        )
    return CampaignScope(query=q, campaign_id=None, matched=0, truncated=truncated)


def _get_act(actions: Optional[list[dict]], action_type: str) -> float:
    if not actions:
        return 0.0
    for a in actions:
        if a.get("action_type") == action_type:
            try:
                return float(a.get("value") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _get_act_val(values: Optional[list[dict]], action_type: str) -> float:
    if not values:
        return 0.0
    for a in values:
        if a.get("action_type") == action_type:
            try:
                return float(a.get("value") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def extract_purchases(row: dict[str, Any]) -> tuple[int, float]:
    """Unpack purchases from nested actions / action_values (prefer omni_purchase)."""
    purchases = int(_get_act(row.get("actions"), PURCHASE_ACTION))
    value = _get_act_val(row.get("action_values"), PURCHASE_ACTION)
    if purchases == 0 and value == 0:
        purchases = int(_get_act(row.get("actions"), PURCHASE_FALLBACK))
        value = _get_act_val(row.get("action_values"), PURCHASE_FALLBACK)
    return purchases, value


def normalize_insight_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map Graph API insight row to MCP-friendly metric names."""
    purchases, purchase_value = extract_purchases(row)
    clicks = int(float(row.get("clicks") or 0))
    link_clicks = int(float(row.get("inline_link_clicks") or 0))
    return {
        "campaign_id": row.get("campaign_id") or "",
        "campaign_name": row.get("campaign_name") or "",
        "adset_id": row.get("adset_id") or "",
        "adset_name": row.get("adset_name") or "",
        "ad_id": row.get("ad_id") or "",
        "ad_name": row.get("ad_name") or "",
        "day": row.get("date_start") or row.get("date_stop") or "",
        "date_start": row.get("date_start") or "",
        "date_stop": row.get("date_stop") or "",
        "publisher_platform": row.get("publisher_platform") or "",
        "platform_position": row.get("platform_position") or "",
        "placement": row.get("platform_position") or row.get("publisher_platform") or "",
        "platform": row.get("publisher_platform") or "",
        # Geographic breakdowns (Meta Insights API: region ≈ state/province)
        "region": row.get("region") or "",
        "country": row.get("country") or "",
        "dma": row.get("dma") or "",
        # Demographic / hourly breakdowns
        "age": row.get("age") or "",
        "gender": row.get("gender") or "",
        "hourly_stats_aggregated_by_advertiser_time_zone": row.get(
            "hourly_stats_aggregated_by_advertiser_time_zone"
        )
        or "",
        "hourly_stats_aggregated_by_audience_time_zone": row.get(
            "hourly_stats_aggregated_by_audience_time_zone"
        )
        or "",
        "hour": (
            row.get("hourly_stats_aggregated_by_advertiser_time_zone")
            or row.get("hourly_stats_aggregated_by_audience_time_zone")
            or ""
        ),
        "amount_spent_usd": float(row.get("spend") or 0),
        "impressions": int(float(row.get("impressions") or 0)),
        "reach": int(float(row.get("reach") or 0)),
        "clicks_all": clicks,
        "inline_link_clicks": link_clicks,
        "purchases": purchases,
        "meta_purchases": purchases,
        "purchases_value": purchase_value,
        "meta_purchase_value": purchase_value,
        "frequency": float(row.get("frequency") or 0),
        "cpc": float(row.get("cpc") or 0),
        "cpm": float(row.get("cpm") or 0),
        "ctr": float(row.get("ctr") or 0),
    }


class MetaGraphClient:
    def __init__(self) -> None:
        self.app_id = (
            os.environ.get("FACEBOOK_APP_ID", "").strip()
            or os.environ.get("FB_APP_ID", "").strip()
        )
        self.app_secret = (
            os.environ.get("FACEBOOK_APP_SECRET", "").strip()
            or os.environ.get("FB_APP_SECRET", "").strip()
        )
        self.default_account = (
            os.environ.get("FACEBOOK_AD_ACCOUNT_ID", "").strip()
            or os.environ.get("FB_AD_ACCOUNT_ID", "").strip()
        ).removeprefix("act_")
        self.user_access_token = (
            os.environ.get("FACEBOOK_ACCESS_TOKEN", "").strip()
            or os.environ.get("FB_ACCESS_TOKEN", "").strip()
        )
        self._access_token: Optional[str] = None

    def validate_config(self) -> None:
        missing = []
        if not self.app_id:
            missing.append("FACEBOOK_APP_ID")
        if not self.app_secret:
            missing.append("FACEBOOK_APP_SECRET")
        if missing:
            raise RuntimeError(
                "Missing Facebook credentials in .env:\n"
                + "\n".join(f"  - {k}" for k in missing)
            )

    def resolve_account_id(self, account_id: str = "") -> str:
        raw = (account_id or self.default_account).strip().removeprefix("act_")
        if not raw:
            raise ValueError(
                "account_id is required (e.g. act_114810198697538) "
                "or set FACEBOOK_AD_ACCOUNT_ID in .env"
            )
        return raw

    def exchange_token(self) -> str:
        """Obtain app access token from APP_ID + APP_SECRET (client_credentials)."""
        self.validate_config()
        params = {
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "grant_type": "client_credentials",
        }
        with httpx.Client(timeout=60.0) as client:
            res = client.get(f"{FB_BASE}/oauth/access_token", params=params)
            data = res.json() if res.content else {}

        if not res.is_success or not data.get("access_token"):
            err = data.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or str(err)
                raise MetaGraphError(f"App token request failed: {msg}")
            raise MetaGraphError(
                f"App token request failed (HTTP {res.status_code}): {data}"
            )

        self._access_token = data["access_token"]
        return self._access_token

    def access_token(self) -> str:
        if self.user_access_token:
            return self.user_access_token
        if not self._access_token:
            return self.exchange_token()
        return self._access_token

    @staticmethod
    def _safe_log_url(url: str, params: Optional[dict[str, Any]] = None) -> str:
        """Loggable URL with secrets redacted (defect D-8)."""
        safe_params = dict(params or {})
        for key in ("access_token", "input_token", "client_secret"):
            if key in safe_params:
                safe_params[key] = "***"
        if safe_params:
            return f"{url}?{urlencode({k: str(v) for k, v in safe_params.items()})}"
        redacted = url
        for key in ("access_token", "input_token", "client_secret"):
            if f"{key}=" in redacted:
                # crude redact of query value
                parts = redacted.split(f"{key}=")
                if len(parts) > 1:
                    rest = parts[1]
                    amp = rest.find("&")
                    redacted = parts[0] + f"{key}=***" + (rest[amp:] if amp >= 0 else "")
        return redacted

    def _graph_get(
        self, path: str, params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """
        GET Graph API.

        Always pass query params as a dict for relative paths. Never embed the
        query string in `path` — Graph silently drops malformed/duplicated
        params (root cause of D-1 / D-2 / D-4).
        """
        if path.startswith("http"):
            log_url = self._safe_log_url(path)
            logger.info("Graph GET %s", log_url)
            with httpx.Client(timeout=180.0) as client:
                res = client.get(path)
                data = res.json() if res.content else {}
        else:
            # Strip any accidental query fragment from path.
            clean_path = path.split("?", 1)[0].lstrip("/")
            q = dict(params or {})
            if "access_token" not in q:
                q["access_token"] = self.access_token()
            # Never send date_preset alongside time_range — date_preset wins.
            if "time_range" in q and "date_preset" in q:
                q.pop("date_preset", None)
            url = f"{FB_BASE}/{clean_path}"
            log_url = self._safe_log_url(url, q)
            logger.info("Graph GET %s", log_url)
            with httpx.Client(timeout=180.0) as client:
                res = client.get(url, params=q)
                data = res.json() if res.content else {}

        if not res.is_success:
            err = data.get("error")
            if isinstance(err, dict):
                code = err.get("code")
                msg = err.get("message") or str(err)
                if code == 190:
                    self._access_token = None
                    raise MetaGraphError(
                        f"Facebook auth failed: {msg}. Check FACEBOOK_APP_ID / "
                        "FACEBOOK_APP_SECRET / FACEBOOK_ACCESS_TOKEN."
                    )
                raise MetaGraphError(f"Graph API error: {msg}")
            raise MetaGraphError(f"Graph API HTTP {res.status_code}: {data}")
        return data

    def _graph_post(
        self, path: str, params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        clean_path = path.split("?", 1)[0].lstrip("/")
        q = dict(params or {})
        if "access_token" not in q:
            q["access_token"] = self.access_token()
        url = f"{FB_BASE}/{clean_path}"
        logger.info("Graph POST %s", self._safe_log_url(url, q))
        with httpx.Client(timeout=180.0) as client:
            res = client.post(url, data=q)
            data = res.json() if res.content else {}
        if not res.is_success:
            err = data.get("error")
            if isinstance(err, dict):
                raise MetaGraphError(f"Graph API error: {err.get('message') or err}")
            raise MetaGraphError(f"Graph API HTTP {res.status_code}: {data}")
        return data

    def _graph_post_multipart(
        self,
        path: str,
        *,
        data: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """POST with multipart form (used for local/base64 ad image upload)."""
        clean_path = path.split("?", 1)[0].lstrip("/")
        form = dict(data or {})
        if "access_token" not in form:
            form["access_token"] = self.access_token()
        url = f"{FB_BASE}/{clean_path}"
        logger.info("Graph POST multipart %s", self._safe_log_url(url, form))
        with httpx.Client(timeout=180.0) as client:
            res = client.post(url, data=form, files=files or None)
            payload = res.json() if res.content else {}
        if not res.is_success:
            err = payload.get("error")
            if isinstance(err, dict):
                raise MetaGraphError(f"Graph API error: {err.get('message') or err}")
            raise MetaGraphError(f"Graph API HTTP {res.status_code}: {payload}")
        return payload

    def _paginate(
        self, path: str, params: Optional[dict[str, Any]] = None, *, max_pages: int = 50
    ) -> list[dict[str, Any]]:
        """Follow paging.next until exhausted (defect D-6)."""
        rows: list[dict[str, Any]] = []
        data = self._graph_get(path, params)
        rows.extend(data.get("data") or [])
        pages = 1
        next_url = (data.get("paging") or {}).get("next")
        while next_url and pages < max_pages:
            data = self._graph_get(next_url)
            batch = data.get("data") or []
            if not batch:
                break
            rows.extend(batch)
            next_url = (data.get("paging") or {}).get("next")
            pages += 1
        return rows

    def fetch_insights(
        self,
        account_id: str,
        *,
        level: str,
        since: date,
        until: date,
        breakdowns: Optional[str] = None,
        time_increment: Any = 1,
        limit: int = 500,
        filtering: Optional[list[dict[str, Any]]] = None,
        object_id: str = "",
        use_async: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Paginated insights fetch with correct Graph params (D-1..D-4, D-7, D-8).

        time_range MUST be a JSON string. level / breakdowns / time_increment /
        fields must be sent as query params (not baked into a broken path).
        """
        act = self.resolve_account_id(account_id)
        # JSON string — raw dict is silently discarded by Graph (D-1).
        time_range = json.dumps(
            {"since": since.isoformat(), "until": until.isoformat()},
            separators=(",", ":"),
        )
        params: dict[str, Any] = {
            "fields": ",".join(INSIGHT_FIELDS),
            "level": level,
            "time_range": time_range,
            "time_increment": time_increment,
            "limit": min(limit, 500),
            "action_attribution_windows": json.dumps(
                ["7d_click", "1d_view"], separators=(",", ":")
            ),
        }
        if breakdowns:
            params["breakdowns"] = breakdowns
        if filtering:
            params["filtering"] = json.dumps(filtering)

        edge = f"{object_id}/insights" if object_id else f"act_{act}/insights"

        # Wide ad-level ranges: prefer async report run (D-7).
        day_span = (until - since).days
        if use_async or (level == "ad" and day_span > 14 and not object_id):
            try:
                return self._fetch_insights_async(edge, params)
            except MetaGraphError as exc:
                logger.warning("Async insights failed (%s); falling back to sync", exc)

        raw = self._paginate(edge, params)
        return [normalize_insight_row(r) for r in raw]

    def _fetch_insights_async(
        self, edge: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """POST /insights → report_run_id → poll until complete (D-7)."""
        post_params = dict(params)
        data = self._graph_post(edge, post_params)
        run_id = data.get("report_run_id") or data.get("id")
        if not run_id:
            raise MetaGraphError(f"Async insights did not return report_run_id: {data}")

        # Poll job status
        for _ in range(60):
            status = self._graph_get(
                str(run_id),
                {"fields": "async_status,async_percent_completion"},
            )
            state = (status.get("async_status") or "").upper()
            if state == "JOB_COMPLETED":
                break
            if state in ("JOB_FAILED", "JOB_SKIPPED"):
                raise MetaGraphError(f"Async insights job failed: {status}")
            time.sleep(2)
        else:
            raise MetaGraphError("Async insights job timed out")

        raw = self._paginate(f"{run_id}/insights", {"limit": 500})
        return [normalize_insight_row(r) for r in raw]

    def resolve_campaign_scope(self, account_id: str, campaign: str) -> CampaignScope:
        """Resolve a campaign argument to one id, or a contains-match over many."""
        q = (campaign or "").strip()
        if not q:
            return CampaignScope(query="")
        if q.isdigit() or q.startswith("120"):
            return CampaignScope(
                query=q, campaign_id=q.removeprefix("act_"), matched=1
            )
        rows = self.list_campaign_objects(account_id=account_id, search=q, limit=200)
        return select_campaign_scope(q, rows, truncated=len(rows) >= 200)

    def resolve_campaign_id(self, account_id: str, campaign: str) -> Optional[str]:
        """Resolve a numeric id, exact name, or unique partial name to one campaign id.

        A substring that matches several campaigns returns None so callers
        include every match instead of the first one (D-5).
        """
        return self.resolve_campaign_scope(account_id, campaign).campaign_id

    def fetch_insights_for_campaign(
        self,
        account_id: str,
        campaign: str,
        *,
        level: str,
        since: date,
        until: date,
        breakdowns: Optional[str] = None,
        time_increment: Any = 1,
    ) -> tuple[list[dict[str, Any]], CampaignScope]:
        """Fetch insights for one campaign, or every name that contains the query."""
        scope = self.resolve_campaign_scope(account_id, campaign)
        if scope.campaign_id:
            rows = self.fetch_insights(
                account_id,
                level=level,
                since=since,
                until=until,
                breakdowns=breakdowns,
                time_increment=time_increment,
                object_id=scope.campaign_id,
            )
            return rows, scope
        rows = self.fetch_insights(
            account_id,
            level=level,
            since=since,
            until=until,
            breakdowns=breakdowns,
            time_increment=time_increment,
            filtering=[
                {
                    "field": "campaign.name",
                    "operator": "CONTAIN",
                    "value": campaign.strip(),
                }
            ],
        )
        return rows, scope

    _AD_ACCOUNT_FIELDS = (
        "account_id,name,account_status,currency,"
        "business{id,name},owner,parent_advertiser_id"
    )

    @staticmethod
    def account_status_label(status: Any) -> str:
        labels = {
            1: "ACTIVE",
            2: "DISABLED",
            3: "UNSETTLED",
            7: "PENDING_RISK_REVIEW",
            8: "PENDING_SETTLEMENT",
            9: "IN_GRACE_PERIOD",
            100: "PENDING_CLOSURE",
            101: "CLOSED",
            201: "ANY_ACTIVE",
            202: "ANY_CLOSED",
        }
        try:
            code = int(status)
        except (TypeError, ValueError):
            return str(status or "")
        return labels.get(code, str(code))

    def _normalize_ad_account_row(
        self,
        row: dict[str, Any],
        *,
        relationship: str,
        parent_business: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        account_id = (row.get("account_id") or str(row.get("id") or "")).removeprefix(
            "act_"
        )
        biz = row.get("business") if isinstance(row.get("business"), dict) else {}
        parent = parent_business if isinstance(parent_business, dict) else biz
        return {
            "account_id": account_id,
            "name": row.get("name") or "",
            "currency": row.get("currency") or "",
            "account_status": row.get("account_status"),
            "account_status_label": self.account_status_label(row.get("account_status")),
            "relationship": relationship,
            "parent_business_id": (parent or {}).get("id") or "",
            "parent_business_name": (parent or {}).get("name") or "",
            "owner": row.get("owner") or "",
            "parent_advertiser_id": row.get("parent_advertiser_id") or "",
        }

    def list_ad_accounts(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.list_all_ad_accounts(limit=limit)

    def list_all_ad_accounts(self, limit: int = 50) -> list[dict[str, Any]]:
        """List ad accounts with parent Business Manager."""
        cap = max(1, min(limit, 500))
        fields = self._AD_ACCOUNT_FIELDS
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []

        def _append(
            items: list[dict[str, Any]],
            relationship: str,
            parent_business: Optional[dict[str, Any]] = None,
        ) -> None:
            for item in items:
                normalized = self._normalize_ad_account_row(
                    item, relationship=relationship, parent_business=parent_business
                )
                aid = normalized["account_id"]
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                rows.append(normalized)

        try:
            data = self._graph_get(
                "me/adaccounts", {"fields": fields, "limit": min(cap, 100)}
            )
            _append(data.get("data") or [], "user")
            next_url = (data.get("paging") or {}).get("next")
            while next_url and len(rows) < cap:
                data = self._graph_get(next_url)
                _append(data.get("data") or [], "user")
                next_url = (data.get("paging") or {}).get("next")
            if rows:
                return rows[:cap]
        except MetaGraphError:
            pass

        act = self.default_account
        if not act:
            return rows[:cap]

        try:
            seed = self._graph_get(f"act_{act}", {"fields": fields})
        except MetaGraphError as exc:
            if not self.user_access_token:
                raise MetaGraphError(
                    f"{exc} Set FACEBOOK_ACCESS_TOKEN (ads_read) in .env, "
                    "or grant this app ads_read on the ad account in Business Manager."
                ) from exc
            raise

        biz = seed.get("business") if isinstance(seed.get("business"), dict) else {}
        biz_id = (biz.get("id") or "").strip()
        parent = {"id": biz_id, "name": biz.get("name") or ""} if biz_id else None

        if biz_id:
            for relationship, edge in (
                ("owned", "owned_ad_accounts"),
                ("client", "client_ad_accounts"),
            ):
                try:
                    batch = self._paginate(
                        f"{biz_id}/{edge}",
                        {"fields": fields, "limit": min(cap, 100)},
                    )
                    _append(batch, relationship, parent)
                except MetaGraphError:
                    continue

        if not rows:
            _append([seed], "default", parent)

        return rows[:cap]

    def list_accounts_with_spend(
        self,
        since: date,
        until: date,
        *,
        account_limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List ad accounts with account-level spend for a date range."""
        accounts = self.list_all_ad_accounts(limit=account_limit)
        time_range = json.dumps(
            {"since": since.isoformat(), "until": until.isoformat()},
            separators=(",", ":"),
        )
        results: list[dict[str, Any]] = []
        for acct in accounts:
            aid = acct["account_id"]
            spend = 0.0
            impressions = 0
            clicks = 0
            try:
                data = self._graph_get(
                    f"act_{aid}/insights",
                    {
                        "fields": "spend,impressions,clicks",
                        "level": "account",
                        "time_range": time_range,
                        "time_increment": "all_days",
                        "limit": 1,
                    },
                )
                rows = data.get("data") or []
                if rows:
                    spend = float(rows[0].get("spend") or 0)
                    impressions = int(float(rows[0].get("impressions") or 0))
                    clicks = int(float(rows[0].get("clicks") or 0))
            except MetaGraphError:
                pass
            results.append(
                {
                    **acct,
                    "account_id": f"act_{aid}",
                    "spend": round(spend, 2),
                    "impressions": impressions,
                    "clicks": clicks,
                }
            )
        results.sort(key=lambda row: row.get("spend", 0), reverse=True)
        return results

    def list_campaign_objects(
        self, account_id: str = "", search: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        act = self.resolve_account_id(account_id)
        params: dict[str, Any] = {
            "fields": (
                "id,name,status,effective_status,objective,"
                "daily_budget,lifetime_budget,budget_remaining,"
                "bid_strategy,start_time,stop_time"
            ),
            "limit": min(max(limit, 1), 200),
        }
        if search.strip():
            params["filtering"] = json.dumps(
                [
                    {
                        "field": "name",
                        "operator": "CONTAIN",
                        "value": search.strip(),
                    }
                ]
            )
        rows = self._paginate(f"act_{act}/campaigns", params)
        return rows[:limit] if limit else rows

    def list_adset_objects(
        self, account_id: str = "", search: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        act = self.resolve_account_id(account_id)
        params: dict[str, Any] = {
            "fields": (
                "id,name,status,effective_status,campaign_id,"
                "daily_budget,lifetime_budget,budget_remaining,"
                "optimization_goal,billing_event,bid_amount,"
                "targeting,start_time,end_time"
            ),
            "limit": min(max(limit, 1), 200),
        }
        if search.strip():
            params["filtering"] = json.dumps(
                [
                    {
                        "field": "name",
                        "operator": "CONTAIN",
                        "value": search.strip(),
                    }
                ]
            )
        rows = self._paginate(f"act_{act}/adsets", params)
        return rows[:limit] if limit else rows

    _AD_CREATIVE_FIELDS = (
        "id,name,status,effective_status,adset_id,campaign_id,"
        "creative{id,name,thumbnail_url,image_url,image_hash,object_type,"
        "title,body,call_to_action_type,link_url,video_id,"
        "effective_object_story_id,object_story_spec,asset_feed_spec}"
    )

    @staticmethod
    def _collect_image_hashes(creative: dict[str, Any]) -> list[str]:
        """Gather image hashes from a creative and nested story / feed specs."""
        hashes: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            h = str(value or "").strip()
            if h and h not in seen:
                seen.add(h)
                hashes.append(h)

        add(creative.get("image_hash"))
        story = creative.get("object_story_spec")
        if isinstance(story, dict):
            for key in ("link_data", "video_data", "photo_data"):
                block = story.get(key)
                if isinstance(block, dict):
                    add(block.get("image_hash"))
                    add(block.get("hash"))
        feed = creative.get("asset_feed_spec")
        if isinstance(feed, dict):
            for img in feed.get("images") or []:
                if isinstance(img, dict):
                    add(img.get("hash"))
                    add(img.get("image_hash"))
            for vid in feed.get("videos") or []:
                if isinstance(vid, dict):
                    add(vid.get("thumbnail_hash"))
                    add(vid.get("image_hash"))
                    add(vid.get("video_thumbnail_hash"))
        return hashes

    @staticmethod
    def _collect_direct_image_urls(creative: dict[str, Any]) -> list[str]:
        """URLs embedded in story/feed specs (often better than creative.thumbnail_url)."""
        urls: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            u = str(value or "").strip()
            if u.startswith(("http://", "https://")) and u not in seen:
                seen.add(u)
                urls.append(u)

        add(creative.get("image_url"))
        story = creative.get("object_story_spec")
        if isinstance(story, dict):
            for key in ("link_data", "video_data", "photo_data"):
                block = story.get(key)
                if isinstance(block, dict):
                    add(block.get("picture"))
                    add(block.get("image_url"))
                    add(block.get("url"))
        feed = creative.get("asset_feed_spec")
        if isinstance(feed, dict):
            for img in feed.get("images") or []:
                if isinstance(img, dict):
                    add(img.get("url"))
                    add(img.get("thumbnail_url"))
            for vid in feed.get("videos") or []:
                if isinstance(vid, dict):
                    add(vid.get("thumbnail_url"))
                    add(vid.get("picture"))
        return urls

    @staticmethod
    def _collect_video_ids(creative: dict[str, Any]) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            vid = str(value or "").strip()
            if vid and vid not in seen:
                seen.add(vid)
                ids.append(vid)

        add(creative.get("video_id"))
        story = creative.get("object_story_spec")
        if isinstance(story, dict):
            video_data = story.get("video_data")
            if isinstance(video_data, dict):
                add(video_data.get("video_id"))
        feed = creative.get("asset_feed_spec")
        if isinstance(feed, dict):
            for vid in feed.get("videos") or []:
                if isinstance(vid, dict):
                    add(vid.get("video_id"))
                    add(vid.get("id"))
        return ids

    def resolve_adimage_urls(
        self, account_id: str, hashes: list[str]
    ) -> dict[str, str]:
        """Map image hashes to permalink/url via act_*/adimages."""
        act = self.resolve_account_id(account_id)
        unique = [h for h in dict.fromkeys(hashes) if h]
        if not unique:
            return {}
        out: dict[str, str] = {}
        # Graph accepts a JSON array of hashes; batch to keep query size sane.
        for i in range(0, len(unique), 50):
            batch = unique[i : i + 50]
            try:
                data = self._graph_get(
                    f"act_{act}/adimages",
                    {
                        "hashes": json.dumps(batch, separators=(",", ":")),
                        "fields": "hash,url,permalink_url,name",
                        "limit": min(len(batch), 100),
                    },
                )
            except MetaGraphError as exc:
                logger.warning("adimages lookup failed: %s", exc)
                continue
            for row in data.get("data") or []:
                if not isinstance(row, dict):
                    continue
                h = str(row.get("hash") or "").strip()
                url = str(row.get("permalink_url") or row.get("url") or "").strip()
                if h and url:
                    out[h] = url
        return out

    def resolve_video_pictures(self, video_ids: list[str]) -> dict[str, str]:
        """Map video ids → best still URL (picture / preferred thumbnail)."""
        out: dict[str, str] = {}
        unique = [v for v in dict.fromkeys(video_ids) if v]
        for vid in unique:
            try:
                data = self._graph_get(
                    vid,
                    {"fields": "id,picture,thumbnails"},
                )
            except MetaGraphError as exc:
                logger.warning("video picture lookup failed for %s: %s", vid, exc)
                continue
            picture = str(data.get("picture") or "").strip()
            if picture:
                out[vid] = picture
                continue
            thumbs = data.get("thumbnails")
            # thumbnails may be {data: [...]} or a list
            rows = []
            if isinstance(thumbs, dict):
                rows = thumbs.get("data") or []
            elif isinstance(thumbs, list):
                rows = thumbs
            preferred = ""
            for t in rows:
                if not isinstance(t, dict):
                    continue
                uri = str(t.get("uri") or t.get("id") or "").strip()
                if not uri.startswith("http"):
                    continue
                if t.get("is_preferred"):
                    preferred = uri
                    break
                if not preferred:
                    preferred = uri
            if preferred:
                out[vid] = preferred
        return out

    def resolve_story_pictures(self, story_ids: list[str]) -> dict[str, str]:
        """Map effective_object_story_id → full_picture / picture."""
        out: dict[str, str] = {}
        unique = [s for s in dict.fromkeys(story_ids) if s]
        for sid in unique:
            try:
                data = self._graph_get(
                    sid,
                    {"fields": "id,full_picture,picture"},
                )
            except MetaGraphError as exc:
                logger.warning("story picture lookup failed for %s: %s", sid, exc)
                continue
            url = str(data.get("full_picture") or data.get("picture") or "").strip()
            if url.startswith("http"):
                out[sid] = url
        return out

    def _best_image_url(
        self,
        creative: dict[str, Any],
        hash_urls: dict[str, str],
    ) -> str:
        """Backward-compatible helper — prefer resolve_creative_image()."""
        resolved = self.resolve_creative_image(
            creative, hash_urls=hash_urls, video_pictures={}, story_pictures={}
        )
        return str(resolved.get("url") or "")

    def resolve_creative_image(
        self,
        creative: dict[str, Any],
        *,
        hash_urls: dict[str, str],
        video_pictures: dict[str, str],
        story_pictures: dict[str, str],
    ) -> dict[str, str]:
        """
        Pick the best still for a creative and label the source.

        Priority: creative image_url → story/feed direct URLs → adimages hashes →
        video picture → effective story full_picture → creative thumbnail_url.
        """
        hashes = self._collect_image_hashes(creative)
        video_ids = self._collect_video_ids(creative)
        story_id = str(creative.get("effective_object_story_id") or "").strip()

        direct = str(creative.get("image_url") or "").strip()
        if direct.startswith("http"):
            return {
                "url": direct,
                "source": "creative_image_url",
                "video_id": video_ids[0] if video_ids else "",
            }

        for url in self._collect_direct_image_urls(creative):
            # Skip if it's the same as weak thumbnail and we have better options later —
            # still prefer direct story URLs over thumbnail.
            if url and url != str(creative.get("thumbnail_url") or "").strip():
                return {
                    "url": url,
                    "source": "story_or_feed_url",
                    "video_id": video_ids[0] if video_ids else "",
                }
            if url:
                return {
                    "url": url,
                    "source": "story_or_feed_url",
                    "video_id": video_ids[0] if video_ids else "",
                }

        for h in hashes:
            url = hash_urls.get(h)
            if url:
                return {
                    "url": url,
                    "source": "adimages_hash",
                    "video_id": video_ids[0] if video_ids else "",
                }

        for vid in video_ids:
            url = video_pictures.get(vid)
            if url:
                return {"url": url, "source": "video_picture", "video_id": vid}

        if story_id:
            url = story_pictures.get(story_id)
            if url:
                return {
                    "url": url,
                    "source": "object_story_full_picture",
                    "video_id": video_ids[0] if video_ids else "",
                }

        thumb = str(creative.get("thumbnail_url") or "").strip()
        if thumb:
            return {
                "url": thumb,
                "source": "thumbnail_fallback",
                "video_id": video_ids[0] if video_ids else "",
            }
        return {"url": "", "source": "none", "video_id": video_ids[0] if video_ids else ""}

    def list_ad_creatives(
        self,
        account_id: str = "",
        search: str = "",
        limit: int = 50,
        *,
        include_preview: bool = True,
        ad_ids: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """List ads with the best available creative still URL + source label."""
        act = self.resolve_account_id(account_id)
        rows: list[dict[str, Any]] = []
        wanted = [str(a).strip() for a in (ad_ids or []) if str(a).strip()]
        if wanted:
            for ad_id in wanted[: max(1, min(limit, 100))]:
                try:
                    row = self._graph_get(ad_id, {"fields": self._AD_CREATIVE_FIELDS})
                    if isinstance(row, dict) and row.get("id"):
                        rows.append(row)
                except MetaGraphError as exc:
                    logger.warning("Failed to load ad %s: %s", ad_id, exc)
        else:
            params: dict[str, Any] = {
                "fields": self._AD_CREATIVE_FIELDS,
                "limit": min(max(limit, 1), 100),
            }
            if search.strip():
                params["filtering"] = json.dumps(
                    [
                        {
                            "field": "name",
                            "operator": "CONTAIN",
                            "value": search.strip(),
                        }
                    ]
                )
            rows = self._paginate(f"act_{act}/ads", params)

        creatives = [
            row.get("creative") if isinstance(row.get("creative"), dict) else {}
            for row in rows[:limit]
        ]
        all_hashes: list[str] = []
        all_video_ids: list[str] = []
        all_story_ids: list[str] = []
        for creative in creatives:
            all_hashes.extend(self._collect_image_hashes(creative))
            all_video_ids.extend(self._collect_video_ids(creative))
            sid = str(creative.get("effective_object_story_id") or "").strip()
            if sid:
                all_story_ids.append(sid)

        hash_urls = self.resolve_adimage_urls(act, all_hashes)
        video_pictures = self.resolve_video_pictures(all_video_ids)
        # Only fetch stories when hash/video resolution is incomplete for that creative.
        need_stories: list[str] = []
        for creative in creatives:
            provisional = self.resolve_creative_image(
                creative,
                hash_urls=hash_urls,
                video_pictures=video_pictures,
                story_pictures={},
            )
            if provisional.get("source") in ("thumbnail_fallback", "none"):
                sid = str(creative.get("effective_object_story_id") or "").strip()
                if sid:
                    need_stories.append(sid)
        story_pictures = self.resolve_story_pictures(need_stories)

        out: list[dict[str, Any]] = []
        for row, creative in zip(rows[:limit], creatives):
            ad_id = str(row.get("id") or "")
            preview_url = ""
            if include_preview and ad_id:
                try:
                    prev = self._graph_get(
                        f"{ad_id}/previews",
                        {"ad_format": "DESKTOP_FEED_STANDARD"},
                    )
                    bodies = prev.get("data") or []
                    if bodies:
                        # iframe HTML; extract src if present
                        html = bodies[0].get("body") or ""
                        preview_url = html
                        if 'src="' in html:
                            preview_url = html.split('src="', 1)[1].split('"', 1)[0]
                except MetaGraphError:
                    preview_url = ""
            image_url = str(creative.get("image_url") or "").strip()
            thumbnail_url = str(creative.get("thumbnail_url") or "").strip()
            resolved = self.resolve_creative_image(
                creative,
                hash_urls=hash_urls,
                video_pictures=video_pictures,
                story_pictures=story_pictures,
            )
            video_ids = self._collect_video_ids(creative)
            out.append(
                {
                    "ad_id": ad_id,
                    "ad_name": row.get("name") or "",
                    "status": row.get("effective_status") or row.get("status") or "",
                    "adset_id": row.get("adset_id") or "",
                    "campaign_id": row.get("campaign_id") or "",
                    "creative_id": creative.get("id") or "",
                    "creative_name": creative.get("name") or "",
                    "object_type": creative.get("object_type") or "",
                    "thumbnail_url": thumbnail_url,
                    "image_url": image_url,
                    "best_image_url": resolved.get("url") or "",
                    "image_source": resolved.get("source") or "none",
                    "image_hashes": self._collect_image_hashes(creative),
                    "effective_object_story_id": creative.get("effective_object_story_id")
                    or "",
                    "title": creative.get("title") or "",
                    "body": creative.get("body") or "",
                    "cta": creative.get("call_to_action_type") or "",
                    "link_url": creative.get("link_url") or "",
                    "video_id": resolved.get("video_id")
                    or (video_ids[0] if video_ids else ""),
                    "video_ids": video_ids,
                    "preview_url": preview_url,
                    "ads_manager_url": (
                        f"https://www.facebook.com/adsmanager/manage/ads"
                        f"?act={act}&selected_ad_ids={ad_id}"
                        if ad_id
                        else ""
                    ),
                }
            )
        return out

    def download_image_bytes(
        self, url: str, *, max_bytes: int = CREATIVE_IMAGE_MAX_BYTES
    ) -> dict[str, Any]:
        """Download an image URL on the connector (bypasses Claude CDN blocks)."""
        target = (url or "").strip()
        if not target:
            raise MetaGraphError("image url is empty")
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            res = client.get(target)
        if not res.is_success:
            raise MetaGraphError(f"Image download HTTP {res.status_code}")
        content = res.content or b""
        if len(content) > max_bytes:
            raise MetaGraphError(
                f"Image exceeds {max_bytes} bytes ({len(content)} bytes)"
            )
        if not content:
            raise MetaGraphError("Image download returned empty body")
        mime = (res.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not mime.startswith("image/"):
            if content[:3] == b"\xff\xd8\xff":
                mime = "image/jpeg"
            elif content[:8] == b"\x89PNG\r\n\x1a\n":
                mime = "image/png"
            elif content[:6] in (b"GIF87a", b"GIF89a"):
                mime = "image/gif"
            elif content[:4] == b"RIFF" and content[8:12] == b"WEBP":
                mime = "image/webp"
            else:
                mime = mime or "application/octet-stream"
        return {
            "mime_type": mime,
            "byte_size": len(content),
            "image_base64": base64.b64encode(content).decode("ascii"),
        }

    def fetch_creative_images(
        self,
        account_id: str = "",
        search: str = "",
        limit: int = 20,
        *,
        ad_ids: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Resolve creatives and download best image bytes for each ad."""
        rows = self.list_ad_creatives(
            account_id=account_id,
            search=search,
            limit=max(1, min(limit, 20)),
            include_preview=False,
            ad_ids=ad_ids,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            image_url = (
                str(row.get("best_image_url") or "").strip()
                or str(row.get("image_url") or "").strip()
                or str(row.get("thumbnail_url") or "").strip()
            )
            image_source = str(row.get("image_source") or "").strip() or "none"
            item: dict[str, Any] = {
                "ad_id": row.get("ad_id") or "",
                "ad_name": row.get("ad_name") or "",
                "status": row.get("status") or "",
                "campaign_id": row.get("campaign_id") or "",
                "creative_id": row.get("creative_id") or "",
                "title": row.get("title") or "",
                "cta": row.get("cta") or "",
                "video_id": row.get("video_id") or "",
                "image_hashes": row.get("image_hashes") or [],
                "image_url": image_url,
                "image_source": image_source,
                "ads_manager_url": row.get("ads_manager_url") or "",
                "ok": False,
                "error": "",
                "mime_type": "",
                "byte_size": 0,
                "image_base64": "",
            }
            if not image_url:
                item["error"] = "No image URL on this creative (video-only or empty)."
                out.append(item)
                continue
            try:
                downloaded = self.download_image_bytes(image_url)
                item.update(downloaded)
                item["ok"] = True
                if image_source == "thumbnail_fallback":
                    item["error"] = (
                        "Downloaded Meta thumbnail_fallback — may not match the "
                        "on-ad creative for video/dynamic ads."
                    )
            except MetaGraphError as exc:
                item["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001 — surface download failures per ad
                item["error"] = f"Download failed: {exc}"
            out.append(item)
        return out

    def get_adset_targeting(
        self, account_id: str = "", search: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return ad sets with a readable targeting summary."""
        rows = self.list_adset_objects(
            account_id=account_id, search=search, limit=limit
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            targeting = row.get("targeting") if isinstance(row.get("targeting"), dict) else {}
            out.append(
                {
                    "adset_id": row.get("id") or "",
                    "adset_name": row.get("name") or "",
                    "status": row.get("effective_status") or row.get("status") or "",
                    "campaign_id": row.get("campaign_id") or "",
                    "optimization_goal": row.get("optimization_goal") or "",
                    "targeting_summary": self._summarize_targeting(targeting),
                    "targeting": targeting,
                }
            )
        return out

    @staticmethod
    def _summarize_targeting(targeting: dict[str, Any]) -> str:
        if not targeting:
            return "(no targeting)"
        parts: list[str] = []
        age_min = targeting.get("age_min")
        age_max = targeting.get("age_max")
        if age_min or age_max:
            parts.append(f"age {age_min or '?'}-{age_max or '?'}")
        genders = targeting.get("genders")
        if genders:
            gmap = {1: "male", 2: "female"}
            parts.append(
                "gender "
                + ",".join(gmap.get(int(g), str(g)) for g in genders)
            )
        geos = targeting.get("geo_locations") or {}
        countries = geos.get("countries") or []
        regions = geos.get("regions") or []
        cities = geos.get("cities") or []
        if countries:
            parts.append("countries " + ",".join(str(c) for c in countries[:8]))
        if regions:
            names = [
                (r.get("name") if isinstance(r, dict) else str(r)) for r in regions[:6]
            ]
            parts.append("regions " + ",".join(names))
        if cities:
            names = [
                (c.get("name") if isinstance(c, dict) else str(c)) for c in cities[:6]
            ]
            parts.append("cities " + ",".join(names))
        for key, label in (
            ("flexible_spec", "interests/behaviors"),
            ("interests", "interests"),
            ("behaviors", "behaviors"),
            ("custom_audiences", "custom audiences"),
            ("excluded_custom_audiences", "excluded audiences"),
            ("publisher_platforms", "platforms"),
            ("facebook_positions", "FB positions"),
            ("instagram_positions", "IG positions"),
            ("device_platforms", "devices"),
        ):
            val = targeting.get(key)
            if not val:
                continue
            if key == "flexible_spec" and isinstance(val, list):
                names: list[str] = []
                for block in val[:3]:
                    if not isinstance(block, dict):
                        continue
                    for k in ("interests", "behaviors", "life_events", "family_statuses"):
                        for item in block.get(k) or []:
                            if isinstance(item, dict) and item.get("name"):
                                names.append(str(item["name"]))
                if names:
                    parts.append(f"{label}: " + ", ".join(names[:10]))
            elif isinstance(val, list):
                names = []
                for item in val[:8]:
                    if isinstance(item, dict):
                        names.append(str(item.get("name") or item.get("id") or item))
                    else:
                        names.append(str(item))
                parts.append(f"{label}: " + ", ".join(names))
        return "; ".join(parts) if parts else json.dumps(targeting)[:240]

    # Modern Outcome API objectives (Marketing API).
    CAMPAIGN_OBJECTIVES = frozenset(
        {
            "OUTCOME_AWARENESS",
            "OUTCOME_ENGAGEMENT",
            "OUTCOME_LEADS",
            "OUTCOME_SALES",
            "OUTCOME_TRAFFIC",
            "OUTCOME_APP_PROMOTION",
        }
    )
    SPECIAL_AD_CATEGORIES = frozenset(
        {
            "NONE",
            "EMPLOYMENT",
            "HOUSING",
            "CREDIT",
            "ISSUES_ELECTIONS_POLITICS",
            "FINANCIAL_PRODUCTS_SERVICES",
        }
    )
    ADSET_OPTIMIZATION_GOALS = frozenset(
        {
            "NONE",
            "LINK_CLICKS",
            "LANDING_PAGE_VIEWS",
            "IMPRESSIONS",
            "REACH",
            "OFFSITE_CONVERSIONS",
            "VALUE",
            "LEAD_GENERATION",
            "CONVERSATIONS",
            "THRUPLAY",
            "APP_INSTALLS",
        }
    )
    ADSET_BILLING_EVENTS = frozenset(
        {
            "IMPRESSIONS",
            "LINK_CLICKS",
            "THRUPLAY",
            "APP_INSTALLS",
        }
    )
    AD_CTA_TYPES = frozenset(
        {
            "LEARN_MORE",
            "SHOP_NOW",
            "SIGN_UP",
            "SUBSCRIBE",
            "CONTACT_US",
            "DOWNLOAD",
            "BOOK_NOW",
            "GET_OFFER",
            "APPLY_NOW",
            "BUY_NOW",
        }
    )

    @staticmethod
    def _budget_display(raw: Any) -> str:
        """Meta budgets are in account currency minor units (cents)."""
        try:
            cents = float(raw)
        except (TypeError, ValueError):
            return ""
        if cents <= 0:
            return ""
        return f"{cents / 100:,.2f}"

    @staticmethod
    def _budget_to_cents(amount: Any) -> Optional[int]:
        """Convert major currency units (e.g. 50.00) to Meta minor units."""
        if amount is None or amount == "":
            return None
        try:
            major = float(amount)
        except (TypeError, ValueError) as exc:
            raise MetaGraphError(
                f"Budget must be a number in major currency units (e.g. 50.00), got {amount!r}"
            ) from exc
        if major <= 0:
            raise MetaGraphError("Budget must be greater than zero")
        return int(round(major * 100))

    def create_campaign(
        self,
        name: str,
        objective: str,
        *,
        account_id: str = "",
        status: str = "PAUSED",
        daily_budget: Any = "",
        lifetime_budget: Any = "",
        special_ad_categories: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """
        Create a Meta campaign (needs ads_management).

        Defaults to PAUSED. special_ad_categories is required by Graph; empty
        list means no special category.
        """
        campaign_name = (name or "").strip()
        if not campaign_name:
            raise MetaGraphError("Campaign name is required")

        obj = (objective or "").strip().upper()
        if obj not in self.CAMPAIGN_OBJECTIVES:
            allowed = ", ".join(sorted(self.CAMPAIGN_OBJECTIVES))
            raise MetaGraphError(
                f"Unknown objective {objective!r}. Use one of: {allowed}"
            )

        desired = (status or "PAUSED").strip().upper() or "PAUSED"
        if desired not in ("ACTIVE", "PAUSED"):
            raise MetaGraphError("status must be ACTIVE or PAUSED")
        # Safety: never invent ACTIVE when status is blank/garbage — already checked.
        # Callers should pass PAUSED unless they explicitly want ACTIVE.

        cats_raw = special_ad_categories
        if cats_raw is None:
            cats: list[str] = []
        else:
            cats = []
            for c in cats_raw:
                label = str(c or "").strip().upper()
                if not label or label == "NONE":
                    continue
                if label not in self.SPECIAL_AD_CATEGORIES:
                    allowed = ", ".join(sorted(self.SPECIAL_AD_CATEGORIES))
                    raise MetaGraphError(
                        f"Unknown special_ad_category {c!r}. Use none or: {allowed}"
                    )
                if label not in cats:
                    cats.append(label)

        daily_cents = self._budget_to_cents(daily_budget) if daily_budget not in ("", None) else None
        lifetime_cents = (
            self._budget_to_cents(lifetime_budget)
            if lifetime_budget not in ("", None)
            else None
        )
        if daily_cents is not None and lifetime_cents is not None:
            raise MetaGraphError(
                "Pass only one of daily_budget or lifetime_budget, not both"
            )

        act = self.resolve_account_id(account_id)
        params: dict[str, Any] = {
            "name": campaign_name,
            "objective": obj,
            "status": desired,
            "special_ad_categories": json.dumps(cats, separators=(",", ":")),
        }
        if daily_cents is not None:
            params["daily_budget"] = daily_cents
        if lifetime_cents is not None:
            params["lifetime_budget"] = lifetime_cents

        data = self._graph_post(f"act_{act}/campaigns", params)
        campaign_id = str(data.get("id") or "").strip()
        if not campaign_id:
            raise MetaGraphError(f"Campaign create returned no id: {data}")

        return {
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "status": desired,
            "objective": obj,
            "account_id": f"act_{act}",
            "daily_budget": self._budget_display(daily_cents) if daily_cents else "",
            "lifetime_budget": (
                self._budget_display(lifetime_cents) if lifetime_cents else ""
            ),
            "special_ad_categories": cats,
            "ads_manager_url": (
                f"https://www.facebook.com/adsmanager/manage/campaigns"
                f"?act={act}&selected_campaign_ids={campaign_id}"
            ),
            "raw": data,
        }

    @staticmethod
    def build_simple_targeting(
        *,
        countries: str = "US",
        age_min: Any = 18,
        age_max: Any = 65,
        genders: str = "",
    ) -> dict[str, Any]:
        """Build a minimal Meta targeting object from Claude-friendly fields."""
        country_list = [
            c.strip().upper()
            for c in (countries or "US").replace(";", ",").split(",")
            if c.strip()
        ]
        if not country_list:
            country_list = ["US"]
        try:
            amin = int(age_min if age_min not in ("", None) else 18)
            amax = int(age_max if age_max not in ("", None) else 65)
        except (TypeError, ValueError) as exc:
            raise MetaGraphError("age_min and age_max must be integers") from exc
        if amin < 13 or amax > 65 or amin > amax:
            raise MetaGraphError(
                "age_min/age_max must satisfy 13 ≤ age_min ≤ age_max ≤ 65"
            )
        targeting: dict[str, Any] = {
            "geo_locations": {"countries": country_list},
            "age_min": amin,
            "age_max": amax,
        }
        gender_vals: list[int] = []
        for g in (genders or "").replace(";", ",").split(","):
            g = g.strip()
            if not g:
                continue
            try:
                gi = int(g)
            except ValueError as exc:
                raise MetaGraphError(
                    "genders must be Meta codes: 1 (men), 2 (women)"
                ) from exc
            if gi not in (1, 2):
                raise MetaGraphError(
                    "genders must be Meta codes: 1 (men), 2 (women)"
                )
            if gi not in gender_vals:
                gender_vals.append(gi)
        if gender_vals:
            targeting["genders"] = gender_vals
        return targeting

    def create_adset(
        self,
        name: str,
        campaign_id: str,
        optimization_goal: str,
        *,
        account_id: str = "",
        status: str = "PAUSED",
        billing_event: str = "IMPRESSIONS",
        daily_budget: Any = "",
        lifetime_budget: Any = "",
        countries: str = "US",
        age_min: Any = 18,
        age_max: Any = 65,
        genders: str = "",
        targeting_json: str = "",
        promoted_object_json: str = "",
        start_time: str = "",
        end_time: str = "",
    ) -> dict[str, Any]:
        """
        Create a Meta ad set under an existing campaign (needs ads_management).

        Defaults to PAUSED. Prefer targeting_json when set; otherwise build from
        countries / age / genders.
        """
        adset_name = (name or "").strip()
        if not adset_name:
            raise MetaGraphError("Ad set name is required")
        camp_id = (campaign_id or "").strip()
        if not camp_id:
            raise MetaGraphError("campaign_id is required")

        goal = (optimization_goal or "").strip().upper()
        if goal not in self.ADSET_OPTIMIZATION_GOALS:
            allowed = ", ".join(sorted(self.ADSET_OPTIMIZATION_GOALS))
            raise MetaGraphError(
                f"Unknown optimization_goal {optimization_goal!r}. Use one of: {allowed}"
            )

        billing = (billing_event or "IMPRESSIONS").strip().upper() or "IMPRESSIONS"
        if billing not in self.ADSET_BILLING_EVENTS:
            allowed = ", ".join(sorted(self.ADSET_BILLING_EVENTS))
            raise MetaGraphError(
                f"Unknown billing_event {billing_event!r}. Use one of: {allowed}"
            )

        desired = (status or "PAUSED").strip().upper() or "PAUSED"
        if desired not in ("ACTIVE", "PAUSED"):
            raise MetaGraphError("status must be ACTIVE or PAUSED")

        raw_targeting = (targeting_json or "").strip()
        if raw_targeting:
            try:
                targeting = json.loads(raw_targeting)
            except json.JSONDecodeError as exc:
                raise MetaGraphError(
                    f"targeting_json must be valid JSON: {exc}"
                ) from exc
            if not isinstance(targeting, dict):
                raise MetaGraphError("targeting_json must be a JSON object")
        else:
            targeting = self.build_simple_targeting(
                countries=countries,
                age_min=age_min,
                age_max=age_max,
                genders=genders,
            )

        daily_cents = (
            self._budget_to_cents(daily_budget)
            if daily_budget not in ("", None)
            else None
        )
        lifetime_cents = (
            self._budget_to_cents(lifetime_budget)
            if lifetime_budget not in ("", None)
            else None
        )
        if daily_cents is not None and lifetime_cents is not None:
            raise MetaGraphError(
                "Pass only one of daily_budget or lifetime_budget, not both"
            )

        promoted: Optional[dict[str, Any]] = None
        raw_promoted = (promoted_object_json or "").strip()
        if raw_promoted:
            try:
                promoted = json.loads(raw_promoted)
            except json.JSONDecodeError as exc:
                raise MetaGraphError(
                    f"promoted_object_json must be valid JSON: {exc}"
                ) from exc
            if not isinstance(promoted, dict):
                raise MetaGraphError("promoted_object_json must be a JSON object")

        start = (start_time or "").strip()
        if not start:
            start = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        act = self.resolve_account_id(account_id)
        params: dict[str, Any] = {
            "name": adset_name,
            "campaign_id": camp_id,
            "optimization_goal": goal,
            "billing_event": billing,
            "status": desired,
            "targeting": json.dumps(targeting, separators=(",", ":")),
            "start_time": start,
        }
        end = (end_time or "").strip()
        if end:
            params["end_time"] = end
        if daily_cents is not None:
            params["daily_budget"] = daily_cents
        if lifetime_cents is not None:
            params["lifetime_budget"] = lifetime_cents
        if promoted is not None:
            params["promoted_object"] = json.dumps(promoted, separators=(",", ":"))

        data = self._graph_post(f"act_{act}/adsets", params)
        adset_id = str(data.get("id") or "").strip()
        if not adset_id:
            raise MetaGraphError(f"Ad set create returned no id: {data}")

        return {
            "adset_id": adset_id,
            "adset_name": adset_name,
            "campaign_id": camp_id,
            "status": desired,
            "optimization_goal": goal,
            "billing_event": billing,
            "account_id": f"act_{act}",
            "daily_budget": self._budget_display(daily_cents) if daily_cents else "",
            "lifetime_budget": (
                self._budget_display(lifetime_cents) if lifetime_cents else ""
            ),
            "start_time": start,
            "end_time": end,
            "targeting": targeting,
            "targeting_summary": self._summarize_targeting(targeting),
            "promoted_object": promoted or {},
            "ads_manager_url": (
                f"https://www.facebook.com/adsmanager/manage/adsets"
                f"?act={act}&selected_adset_ids={adset_id}"
            ),
            "raw": data,
        }

    def list_campaign_budgets(
        self, account_id: str = "", search: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = self.list_campaign_objects(
            account_id=account_id, search=search, limit=limit
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "campaign_id": row.get("id") or "",
                    "campaign_name": row.get("name") or "",
                    "status": row.get("status") or "",
                    "effective_status": row.get("effective_status") or "",
                    "objective": row.get("objective") or "",
                    "daily_budget": self._budget_display(row.get("daily_budget")),
                    "lifetime_budget": self._budget_display(row.get("lifetime_budget")),
                    "budget_remaining": self._budget_display(row.get("budget_remaining")),
                    "bid_strategy": row.get("bid_strategy") or "",
                    "start_time": row.get("start_time") or "",
                    "stop_time": row.get("stop_time") or "",
                }
            )
        return out

    @staticmethod
    def _parse_adimages_response(
        data: dict[str, Any], *, account_id: str, fallback_url: str = ""
    ) -> dict[str, Any]:
        images = data.get("images")
        if isinstance(images, dict) and images:
            first = next(iter(images.values()))
            if isinstance(first, dict):
                image_hash = str(first.get("hash") or "").strip()
                if image_hash:
                    return {
                        "image_hash": image_hash,
                        "url": str(first.get("url") or fallback_url),
                        "name": str(first.get("name") or ""),
                        "account_id": f"act_{account_id}",
                        "raw": data,
                    }
        image_hash = str(data.get("hash") or "").strip()
        if image_hash:
            return {
                "image_hash": image_hash,
                "url": fallback_url,
                "name": "",
                "account_id": f"act_{account_id}",
                "raw": data,
            }
        raise MetaGraphError(f"adimages upload returned no hash: {data}")

    def upload_ad_image_from_url(
        self, image_url: str, *, account_id: str = ""
    ) -> dict[str, Any]:
        """Upload a public image URL into the ad account library → image_hash."""
        url = (image_url or "").strip()
        if not url:
            raise MetaGraphError("image_url is required")
        if not url.startswith(("http://", "https://")):
            raise MetaGraphError("image_url must be an http(s) URL")
        act = self.resolve_account_id(account_id)
        data = self._graph_post(f"act_{act}/adimages", {"url": url})
        return self._parse_adimages_response(data, account_id=act, fallback_url=url)

    def upload_ad_image_bytes(
        self,
        image_bytes: bytes,
        *,
        account_id: str = "",
        mime_type: str = "image/jpeg",
        filename: str = "",
    ) -> dict[str, Any]:
        """Upload raw image bytes (from Claude base64) into the ad account library."""
        content = image_bytes or b""
        if not content:
            raise MetaGraphError("image bytes are empty")
        if len(content) > CREATIVE_IMAGE_MAX_BYTES:
            raise MetaGraphError(
                f"Image exceeds {CREATIVE_IMAGE_MAX_BYTES} bytes ({len(content)} bytes)"
            )
        mime = (mime_type or "image/jpeg").split(";")[0].strip().lower() or "image/jpeg"
        if not mime.startswith("image/"):
            raise MetaGraphError(f"mime_type must be an image/* type, got {mime_type!r}")
        ext = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }.get(mime, ".jpg")
        fname = (filename or "").strip() or f"upload{ext}"
        if "." not in fname:
            fname = f"{fname}{ext}"
        act = self.resolve_account_id(account_id)
        # Meta adimages multipart field name is "filename".
        data = self._graph_post_multipart(
            f"act_{act}/adimages",
            files={"filename": (fname, content, mime)},
        )
        return self._parse_adimages_response(data, account_id=act)

    def upload_ad_image_base64(
        self,
        image_base64: str,
        *,
        account_id: str = "",
        mime_type: str = "image/jpeg",
        filename: str = "",
    ) -> dict[str, Any]:
        """Decode base64 (data-URL tolerant) and upload via multipart."""
        raw = (image_base64 or "").strip()
        if not raw:
            raise MetaGraphError("image_base64 is required")
        mime = (mime_type or "image/jpeg").strip()
        if raw.startswith("data:") and "," in raw:
            header, raw = raw.split(",", 1)
            # data:image/png;base64,...
            if ";base64" in header and header.startswith("data:"):
                declared = header[5:].split(";", 1)[0].strip()
                if declared:
                    mime = declared
        try:
            content = base64.b64decode(raw, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise MetaGraphError(f"image_base64 is not valid base64: {exc}") from exc
        return self.upload_ad_image_bytes(
            content,
            account_id=account_id,
            mime_type=mime,
            filename=filename,
        )

    def _resolve_image_hash(
        self,
        *,
        account_id: str,
        image_hash: str = "",
        image_base64: str = "",
        image_url: str = "",
        image_mime: str = "image/jpeg",
        image_filename: str = "",
    ) -> str:
        """Priority: image_hash > image_base64 > image_url."""
        existing = (image_hash or "").strip()
        if existing:
            return existing
        if (image_base64 or "").strip():
            uploaded = self.upload_ad_image_base64(
                image_base64,
                account_id=account_id,
                mime_type=image_mime,
                filename=image_filename,
            )
            return str(uploaded.get("image_hash") or "")
        if (image_url or "").strip():
            uploaded = self.upload_ad_image_from_url(
                image_url, account_id=account_id
            )
            return str(uploaded.get("image_hash") or "")
        return ""

    def create_ad_creative(
        self,
        *,
        page_id: str,
        link_url: str,
        message: str,
        image_hash: str,
        account_id: str = "",
        name: str = "",
        headline: str = "",
        description: str = "",
        cta: str = "LEARN_MORE",
    ) -> dict[str, Any]:
        """Create a link-ad creative (object_story_spec.link_data)."""
        pid = (page_id or "").strip()
        link = (link_url or "").strip()
        body = (message or "").strip()
        img_hash = (image_hash or "").strip()
        if not pid:
            raise MetaGraphError("page_id is required to create a link creative")
        if not link:
            raise MetaGraphError("link_url is required to create a link creative")
        if not body:
            raise MetaGraphError("message is required to create a link creative")
        if not img_hash:
            raise MetaGraphError("image_hash is required to create a link creative")

        cta_type = (cta or "LEARN_MORE").strip().upper() or "LEARN_MORE"
        if cta_type not in self.AD_CTA_TYPES:
            allowed = ", ".join(sorted(self.AD_CTA_TYPES))
            raise MetaGraphError(f"Unknown cta {cta!r}. Use one of: {allowed}")

        creative_name = (name or "").strip() or f"Creative {pid}"
        link_data: dict[str, Any] = {
            "image_hash": img_hash,
            "link": link,
            "message": body,
            "call_to_action": {
                "type": cta_type,
                "value": {"link": link},
            },
        }
        headline_text = (headline or "").strip()
        if headline_text:
            link_data["name"] = headline_text
        desc = (description or "").strip()
        if desc:
            link_data["description"] = desc

        act = self.resolve_account_id(account_id)
        object_story_spec = {"page_id": pid, "link_data": link_data}
        params: dict[str, Any] = {
            "name": creative_name,
            "object_story_spec": json.dumps(object_story_spec, separators=(",", ":")),
        }
        data = self._graph_post(f"act_{act}/adcreatives", params)
        creative_id = str(data.get("id") or "").strip()
        if not creative_id:
            raise MetaGraphError(f"Ad creative create returned no id: {data}")
        return {
            "creative_id": creative_id,
            "creative_name": creative_name,
            "page_id": pid,
            "link_url": link,
            "image_hash": img_hash,
            "cta": cta_type,
            "account_id": f"act_{act}",
            "raw": data,
        }

    def create_ad(
        self,
        name: str,
        adset_id: str,
        *,
        account_id: str = "",
        status: str = "PAUSED",
        creative_id: str = "",
        page_id: str = "",
        link_url: str = "",
        message: str = "",
        image_hash: str = "",
        image_base64: str = "",
        image_url: str = "",
        image_mime: str = "image/jpeg",
        image_filename: str = "",
        headline: str = "",
        description: str = "",
        cta: str = "LEARN_MORE",
        creative_name: str = "",
    ) -> dict[str, Any]:
        """
        Create a Meta ad under an existing ad set (needs ads_management).

        Path A: pass creative_id to reuse an existing creative.
        Path B: page_id + link_url + message + image_hash / image_base64 / image_url.
        Image priority: image_hash > image_base64 > image_url.
        creative_id wins when both paths are provided. Defaults to PAUSED.
        """
        ad_name = (name or "").strip()
        if not ad_name:
            raise MetaGraphError("Ad name is required")
        aset_id = (adset_id or "").strip()
        if not aset_id:
            raise MetaGraphError("adset_id is required")

        desired = (status or "PAUSED").strip().upper() or "PAUSED"
        if desired not in ("ACTIVE", "PAUSED"):
            raise MetaGraphError("status must be ACTIVE or PAUSED")

        act = self.resolve_account_id(account_id)
        cid = (creative_id or "").strip()
        uploaded_hash = ""
        created_creative: Optional[dict[str, Any]] = None

        if not cid:
            img_hash = self._resolve_image_hash(
                account_id=act,
                image_hash=image_hash,
                image_base64=image_base64,
                image_url=image_url,
                image_mime=image_mime,
                image_filename=image_filename,
            )
            uploaded_hash = img_hash
            if not (
                (page_id or "").strip()
                and (link_url or "").strip()
                and (message or "").strip()
                and img_hash
            ):
                raise MetaGraphError(
                    "Pass creative_id, or page_id + link_url + message + "
                    "image_hash (or image_base64 / image_url) to create a link creative."
                )
            created_creative = self.create_ad_creative(
                page_id=page_id,
                link_url=link_url,
                message=message,
                image_hash=img_hash,
                account_id=act,
                name=(creative_name or "").strip() or ad_name,
                headline=headline,
                description=description,
                cta=cta,
            )
            cid = str(created_creative.get("creative_id") or "")

        if not cid:
            raise MetaGraphError("creative_id could not be resolved")

        params: dict[str, Any] = {
            "name": ad_name,
            "adset_id": aset_id,
            "status": desired,
            "creative": json.dumps({"creative_id": cid}, separators=(",", ":")),
        }
        data = self._graph_post(f"act_{act}/ads", params)
        ad_id = str(data.get("id") or "").strip()
        if not ad_id:
            raise MetaGraphError(f"Ad create returned no id: {data}")

        return {
            "ad_id": ad_id,
            "ad_name": ad_name,
            "adset_id": aset_id,
            "creative_id": cid,
            "status": desired,
            "account_id": f"act_{act}",
            "image_hash": uploaded_hash
            or (
                str((created_creative or {}).get("image_hash") or "")
                if created_creative
                else ""
            ),
            "cta": str((created_creative or {}).get("cta") or "")
            if created_creative
            else "",
            "link_url": str((created_creative or {}).get("link_url") or "")
            if created_creative
            else "",
            "creative_created": bool(created_creative),
            "ads_manager_url": (
                f"https://www.facebook.com/adsmanager/manage/ads"
                f"?act={act}&selected_ad_ids={ad_id}"
            ),
            "raw": data,
        }

    def update_campaign(
        self,
        campaign_id: str,
        *,
        name: str = "",
        daily_budget: Any = "",
        lifetime_budget: Any = "",
    ) -> dict[str, Any]:
        """Partial update for an existing campaign (name / budget). No delete."""
        oid = (campaign_id or "").strip()
        if not oid:
            raise MetaGraphError("campaign_id is required")
        params: dict[str, Any] = {}
        new_name = (name or "").strip()
        if new_name:
            params["name"] = new_name
        daily_cents = (
            self._budget_to_cents(daily_budget)
            if daily_budget not in ("", None)
            else None
        )
        lifetime_cents = (
            self._budget_to_cents(lifetime_budget)
            if lifetime_budget not in ("", None)
            else None
        )
        if daily_cents is not None and lifetime_cents is not None:
            raise MetaGraphError(
                "Pass only one of daily_budget or lifetime_budget, not both"
            )
        if daily_cents is not None:
            params["daily_budget"] = daily_cents
        if lifetime_cents is not None:
            params["lifetime_budget"] = lifetime_cents
        if not params:
            raise MetaGraphError(
                "Nothing to update. Pass name and/or daily_budget or lifetime_budget."
            )
        data = self._graph_post(oid, params)
        return {
            "campaign_id": oid,
            "updated_fields": sorted(params.keys()),
            "daily_budget": self._budget_display(daily_cents) if daily_cents else "",
            "lifetime_budget": (
                self._budget_display(lifetime_cents) if lifetime_cents else ""
            ),
            "name": new_name,
            "success": data.get("success", True),
            "raw": data,
        }

    def update_adset(
        self,
        adset_id: str,
        *,
        name: str = "",
        daily_budget: Any = "",
        lifetime_budget: Any = "",
        start_time: str = "",
        end_time: str = "",
        countries: str = "",
        age_min: Any = "",
        age_max: Any = "",
        genders: str = "",
        targeting_json: str = "",
        optimization_goal: str = "",
        billing_event: str = "",
    ) -> dict[str, Any]:
        """Partial update for an existing ad set. No delete."""
        oid = (adset_id or "").strip()
        if not oid:
            raise MetaGraphError("adset_id is required")
        params: dict[str, Any] = {}
        new_name = (name or "").strip()
        if new_name:
            params["name"] = new_name

        daily_cents = (
            self._budget_to_cents(daily_budget)
            if daily_budget not in ("", None)
            else None
        )
        lifetime_cents = (
            self._budget_to_cents(lifetime_budget)
            if lifetime_budget not in ("", None)
            else None
        )
        if daily_cents is not None and lifetime_cents is not None:
            raise MetaGraphError(
                "Pass only one of daily_budget or lifetime_budget, not both"
            )
        if daily_cents is not None:
            params["daily_budget"] = daily_cents
        if lifetime_cents is not None:
            params["lifetime_budget"] = lifetime_cents

        start = (start_time or "").strip()
        if start:
            params["start_time"] = start
        end = (end_time or "").strip()
        if end:
            params["end_time"] = end

        goal = (optimization_goal or "").strip().upper()
        if goal:
            if goal not in self.ADSET_OPTIMIZATION_GOALS:
                allowed = ", ".join(sorted(self.ADSET_OPTIMIZATION_GOALS))
                raise MetaGraphError(
                    f"Unknown optimization_goal {optimization_goal!r}. Use one of: {allowed}"
                )
            params["optimization_goal"] = goal
        billing = (billing_event or "").strip().upper()
        if billing:
            if billing not in self.ADSET_BILLING_EVENTS:
                allowed = ", ".join(sorted(self.ADSET_BILLING_EVENTS))
                raise MetaGraphError(
                    f"Unknown billing_event {billing_event!r}. Use one of: {allowed}"
                )
            params["billing_event"] = billing

        targeting: Optional[dict[str, Any]] = None
        raw_targeting = (targeting_json or "").strip()
        if raw_targeting:
            try:
                targeting = json.loads(raw_targeting)
            except json.JSONDecodeError as exc:
                raise MetaGraphError(
                    f"targeting_json must be valid JSON: {exc}"
                ) from exc
            if not isinstance(targeting, dict):
                raise MetaGraphError("targeting_json must be a JSON object")
        elif any(
            str(v).strip()
            for v in (countries, genders)
        ) or age_min not in ("", None) or age_max not in ("", None):
            targeting = self.build_simple_targeting(
                countries=countries or "US",
                age_min=age_min if age_min not in ("", None) else 18,
                age_max=age_max if age_max not in ("", None) else 65,
                genders=genders,
            )
        if targeting is not None:
            params["targeting"] = json.dumps(targeting, separators=(",", ":"))

        if not params:
            raise MetaGraphError(
                "Nothing to update. Pass name, budget, schedule, targeting, "
                "optimization_goal, and/or billing_event."
            )
        data = self._graph_post(oid, params)
        return {
            "adset_id": oid,
            "updated_fields": sorted(
                k for k in params.keys() if k != "targeting"
            )
            + (["targeting"] if "targeting" in params else []),
            "name": new_name,
            "daily_budget": self._budget_display(daily_cents) if daily_cents else "",
            "lifetime_budget": (
                self._budget_display(lifetime_cents) if lifetime_cents else ""
            ),
            "targeting_summary": (
                self._summarize_targeting(targeting) if targeting else ""
            ),
            "success": data.get("success", True),
            "raw": data,
        }

    def update_ad(
        self,
        ad_id: str,
        *,
        account_id: str = "",
        name: str = "",
        creative_id: str = "",
        page_id: str = "",
        link_url: str = "",
        message: str = "",
        image_hash: str = "",
        image_base64: str = "",
        image_url: str = "",
        image_mime: str = "image/jpeg",
        image_filename: str = "",
        headline: str = "",
        description: str = "",
        cta: str = "LEARN_MORE",
        creative_name: str = "",
    ) -> dict[str, Any]:
        """
        Partial update for an existing ad (name and/or creative swap).

        Creative swap: pass creative_id, or Path B fields to create a new link
        creative and attach it. Does not delete the old creative.
        """
        oid = (ad_id or "").strip()
        if not oid:
            raise MetaGraphError("ad_id is required")
        params: dict[str, Any] = {}
        new_name = (name or "").strip()
        if new_name:
            params["name"] = new_name

        act = self.resolve_account_id(account_id) if (
            (creative_id or "").strip()
            or (page_id or "").strip()
            or (image_hash or "").strip()
            or (image_base64 or "").strip()
            or (image_url or "").strip()
        ) else ""

        cid = (creative_id or "").strip()
        created_creative: Optional[dict[str, Any]] = None
        uploaded_hash = ""
        if not cid and (
            (page_id or "").strip()
            or (link_url or "").strip()
            or (message or "").strip()
            or (image_hash or "").strip()
            or (image_base64 or "").strip()
            or (image_url or "").strip()
        ):
            if not act:
                act = self.resolve_account_id(account_id)
            img_hash = self._resolve_image_hash(
                account_id=act,
                image_hash=image_hash,
                image_base64=image_base64,
                image_url=image_url,
                image_mime=image_mime,
                image_filename=image_filename,
            )
            uploaded_hash = img_hash
            if not (
                (page_id or "").strip()
                and (link_url or "").strip()
                and (message or "").strip()
                and img_hash
            ):
                raise MetaGraphError(
                    "To create a new creative for update_ad, pass page_id + "
                    "link_url + message + image_hash (or image_base64 / image_url)."
                )
            created_creative = self.create_ad_creative(
                page_id=page_id,
                link_url=link_url,
                message=message,
                image_hash=img_hash,
                account_id=act,
                name=(creative_name or "").strip() or new_name or f"Creative {oid}",
                headline=headline,
                description=description,
                cta=cta,
            )
            cid = str(created_creative.get("creative_id") or "")

        if cid:
            params["creative"] = json.dumps(
                {"creative_id": cid}, separators=(",", ":")
            )

        if not params:
            raise MetaGraphError(
                "Nothing to update. Pass name and/or creative_id / link-creative fields."
            )
        data = self._graph_post(oid, params)
        updated = []
        if "name" in params:
            updated.append("name")
        if "creative" in params:
            updated.append("creative")
        return {
            "ad_id": oid,
            "updated_fields": updated,
            "name": new_name,
            "creative_id": cid,
            "creative_created": bool(created_creative),
            "image_hash": uploaded_hash
            or (
                str((created_creative or {}).get("image_hash") or "")
                if created_creative
                else ""
            ),
            "success": data.get("success", True),
            "raw": data,
        }

    def update_object_status(
        self, object_id: str, status: str
    ) -> dict[str, Any]:
        """Pause or activate a campaign / ad set / ad (needs ads_management)."""
        oid = (object_id or "").strip()
        if not oid:
            raise MetaGraphError("object_id is required")
        desired = (status or "").strip().upper()
        if desired not in ("ACTIVE", "PAUSED"):
            raise MetaGraphError("status must be ACTIVE or PAUSED")
        return self._graph_post(oid, {"status": desired})

    def debug_token(self, *, input_token: Optional[str] = None) -> dict[str, Any]:
        """Inspect a token (defaults to the active connector token)."""
        self.validate_config()
        app_token = f"{self.app_id}|{self.app_secret}"
        data = self._graph_get(
            "debug_token",
            {
                "input_token": input_token or self.access_token(),
                "access_token": app_token,
            },
        )
        return data.get("data") or {}

    def validate_user_token(self) -> dict[str, Any]:
        """Fail fast when FACEBOOK_ACCESS_TOKEN does not match the configured app."""
        if not self.user_access_token:
            return {}

        try:
            debug = self.debug_token(input_token=self.user_access_token)
        except MetaGraphError as exc:
            msg = str(exc)
            if "did not match the Viewing App" in msg or "App_id in the input_token" in msg:
                raise MetaGraphError(
                    f"FACEBOOK_ACCESS_TOKEN was not generated for app {self.app_id}. "
                    "Open Graph API Explorer, select this app, add ads_read, "
                    "and generate a new token."
                ) from exc
            raise

        if not debug.get("is_valid"):
            raise MetaGraphError(
                "FACEBOOK_ACCESS_TOKEN is invalid or expired. "
                "Generate a new token in Graph API Explorer."
            )

        token_app = str(debug.get("app_id") or "").strip()
        if token_app and self.app_id and token_app != self.app_id:
            raise MetaGraphError(
                f"FACEBOOK_ACCESS_TOKEN belongs to app {token_app}, "
                f"but FACEBOOK_APP_ID is {self.app_id}. "
                "Select the configured app in Graph API Explorer and generate a new token."
            )

        scopes = debug.get("scopes") or []
        if "ads_read" not in scopes and "ads_management" not in scopes:
            raise MetaGraphError(
                "FACEBOOK_ACCESS_TOKEN is missing ads_read / ads_management. "
                "In Graph API Explorer, add those permissions before generating the token."
            )
        return debug


_client: Optional[MetaGraphClient] = None


def graph_client() -> MetaGraphClient:
    global _client
    if _client is None:
        _client = MetaGraphClient()
    return _client
