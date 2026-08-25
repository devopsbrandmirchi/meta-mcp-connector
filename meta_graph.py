"""
Facebook Graph API client for the Meta MCP connector.

Auth uses FACEBOOK_APP_ID + FACEBOOK_APP_SECRET only (app access token via
client_credentials — same app credentials as fb-full-sync, without a stored user token).
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

FB_API_VERSION = os.environ.get("FACEBOOK_API_VERSION", "v21.0").strip() or "v21.0"
FB_BASE = f"https://graph.facebook.com/{FB_API_VERSION}"

INSIGHT_FIELDS = [
    "campaign_id",
    "campaign_name",
    "adset_id",
    "adset_name",
    "ad_id",
    "ad_name",
    "impressions",
    "clicks",
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


class MetaGraphError(Exception):
    """Facebook Graph API error."""


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


def normalize_insight_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map Graph API insight row to MCP-friendly metric names."""
    return {
        "campaign_id": row.get("campaign_id") or "",
        "campaign_name": row.get("campaign_name") or "",
        "adset_id": row.get("adset_id") or "",
        "adset_name": row.get("adset_name") or "",
        "ad_id": row.get("ad_id") or "",
        "ad_name": row.get("ad_name") or "",
        "day": row.get("date_start") or row.get("date_stop") or "",
        "publisher_platform": row.get("publisher_platform") or "",
        "platform_position": row.get("platform_position") or "",
        "placement": row.get("platform_position") or row.get("publisher_platform") or "",
        "platform": row.get("publisher_platform") or "",
        "amount_spent_usd": float(row.get("spend") or 0),
        "impressions": int(float(row.get("impressions") or 0)),
        "reach": int(float(row.get("reach") or 0)),
        "clicks_all": int(float(row.get("clicks") or 0)),
        "purchases": int(_get_act(row.get("actions"), "purchase")),
        "meta_purchases": int(_get_act(row.get("actions"), "omni_purchase")),
        "purchases_value": _get_act_val(row.get("action_values"), "purchase"),
        "meta_purchase_value": _get_act_val(row.get("action_values"), "omni_purchase"),
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

    def _graph_get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if path.startswith("http"):
            with httpx.Client(timeout=120.0) as client:
                res = client.get(path)
                data = res.json() if res.content else {}
        else:
            q = dict(params or {})
            if "access_token" not in q:
                q["access_token"] = self.access_token()
            url = f"{FB_BASE}/{path.lstrip('/')}"
            with httpx.Client(timeout=120.0) as client:
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
                        f"Facebook auth failed: {msg}. Check FACEBOOK_APP_ID and FACEBOOK_APP_SECRET."
                    )
                raise MetaGraphError(f"Graph API error: {msg}")
            raise MetaGraphError(f"Graph API HTTP {res.status_code}: {data}")
        return data

    def fetch_insights(
        self,
        account_id: str,
        *,
        level: str,
        since: date,
        until: date,
        breakdowns: Optional[str] = None,
        time_increment: int = 1,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Paginated insights fetch (same fields/levels as fb-full-sync)."""
        act = self.resolve_account_id(account_id)
        time_range = f'{{"since":"{since.isoformat()}","until":"{until.isoformat()}"}}'
        params: dict[str, Any] = {
            "fields": ",".join(INSIGHT_FIELDS),
            "level": level,
            "time_range": time_range,
            "time_increment": time_increment,
            "limit": limit,
        }
        if breakdowns:
            params["breakdowns"] = breakdowns

        url: Optional[str] = f"act_{act}/insights?{urlencode(params)}"
        rows: list[dict[str, Any]] = []
        while url:
            data = self._graph_get(url)
            batch = data.get("data") or []
            rows.extend(batch)
            url = (data.get("paging") or {}).get("next")
        return [normalize_insight_row(r) for r in rows]

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
        """List ad accounts with parent Business Manager (works with app token)."""
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
                "me/adaccounts", {"fields": fields, "limit": cap}
            )
            _append(data.get("data") or [], "user")
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
                    data = self._graph_get(
                        f"{biz_id}/{edge}", {"fields": fields, "limit": cap}
                    )
                    _append(data.get("data") or [], relationship, parent)
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
        time_range = f'{{"since":"{since.isoformat()}","until":"{until.isoformat()}"}}'
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
                        "time_increment": 1,
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
            "fields": "id,name,status,objective",
            "limit": min(limit, 500),
        }
        if search.strip():
            params["filtering"] = (
                f'[{{"field":"name","operator":"CONTAIN","value":"{search.strip()}"}}]'
            )
        data = self._graph_get(f"act_{act}/campaigns", params)
        return data.get("data") or []

    def list_adset_objects(
        self, account_id: str = "", search: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        act = self.resolve_account_id(account_id)
        params: dict[str, Any] = {
            "fields": "id,name,status,campaign_id",
            "limit": min(limit, 500),
        }
        if search.strip():
            params["filtering"] = (
                f'[{{"field":"name","operator":"CONTAIN","value":"{search.strip()}"}}]'
            )
        data = self._graph_get(f"act_{act}/adsets", params)
        return data.get("data") or []

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
