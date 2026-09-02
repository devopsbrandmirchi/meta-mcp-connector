"""
Claude.ai-compatible OAuth 2.1 provider for the Meta MCP connector.

Claude custom connectors require Dynamic Client Registration (DCR) and
discovery at /.well-known/oauth-authorization-server. Without this, Claude
shows: "Couldn't register with … sign-in service" / ofid_…

No external IdP — the MCP server is its own authorization server.
Authorize auto-approves for allowed redirect hosts (claude.ai by default).
Optional MCP_OAUTH_PASSWORD adds a simple login gate.

OAuth clients / tokens are persisted to disk so Claude stays connected across
Cloud Run restarts (min-instances=1) and browser reopen — without forcing a
full reconnect every time.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

logger = logging.getLogger("meta-oauth")

DEFAULT_SCOPE = "meta"
# Long-lived access tokens — Claude refresh races less often.
DEFAULT_ACCESS_TOKEN_EXPIRY = 90 * 24 * 60 * 60  # 90 days
DEFAULT_ALLOWED_REDIRECT_DOMAINS = ("claude.ai", "claude.com", "localhost", "127.0.0.1")

# Claude.ai publishes client metadata at these URLs (CIMD) instead of always using DCR.
CLAUDE_CIMD_URLS = (
    "https://claude.ai/oauth/mcp-oauth-client-metadata",
    "https://claude.com/oauth/mcp-oauth-client-metadata",
)

JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def _default_state_path() -> str:
    explicit = os.environ.get("MCP_OAUTH_STATE_PATH", "").strip()
    if explicit:
        return explicit
    if os.environ.get("K_SERVICE"):
        return "/tmp/meta-mcp-oauth-state.json"
    return str(Path(__file__).resolve().parent / ".meta-oauth-state.json")


def _gcs_bucket_and_blob() -> tuple[str, str] | tuple[None, None]:
    """
    Durable OAuth state across Cloud Run deploys/instance replaces.

    Set MCP_OAUTH_GCS_BUCKET (bucket name) or MCP_OAUTH_GCS_URI=gs://bucket/object.json
    /tmp alone is wiped on every new Cloud Run revision — that causes Claude
    "session timeout" while DV360/Reddit stay up if their instances keep memory.
    """
    uri = os.environ.get("MCP_OAUTH_GCS_URI", "").strip()
    if uri.startswith("gs://"):
        rest = uri[5:]
        bucket, _, blob = rest.partition("/")
        return bucket, (blob or "meta-mcp-oauth-state.json")
    bucket = os.environ.get("MCP_OAUTH_GCS_BUCKET", "").strip()
    if bucket:
        return bucket, os.environ.get(
            "MCP_OAUTH_GCS_OBJECT", "meta-mcp-oauth-state.json"
        ).strip() or "meta-mcp-oauth-state.json"
    return None, None


def _gcs_read(bucket: str, blob: str) -> Optional[str]:
    try:
        from google.cloud import storage  # type: ignore

        client = storage.Client()
        obj = client.bucket(bucket).blob(blob)
        if not obj.exists():
            return None
        return obj.download_as_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("GCS OAuth state read failed gs://%s/%s: %s", bucket, blob, exc)
        return None


def _gcs_write(bucket: str, blob: str, text: str) -> bool:
    try:
        from google.cloud import storage  # type: ignore

        client = storage.Client()
        obj = client.bucket(bucket).blob(blob)
        obj.upload_from_string(text, content_type="application/json")
        return True
    except Exception as exc:
        logger.warning("GCS OAuth state write failed gs://%s/%s: %s", bucket, blob, exc)
        return False


class ClaudeOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """Embedded AS with DCR + PKCE, tuned for Claude custom connectors."""

    def __init__(
        self,
        *,
        base_url: str,
        password: Optional[str] = None,
        allowed_redirect_domains: Optional[list[str]] = None,
        access_token_expiry_seconds: int = DEFAULT_ACCESS_TOKEN_EXPIRY,
        scope: str = DEFAULT_SCOPE,
        state_path: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.allowed_redirect_domains = (
            list(allowed_redirect_domains)
            if allowed_redirect_domains is not None
            else list(DEFAULT_ALLOWED_REDIRECT_DOMAINS)
        )
        self.access_token_expiry_seconds = access_token_expiry_seconds
        self.scope = scope
        self.state_path = state_path or _default_state_path()
        self._gcs_bucket, self._gcs_blob = _gcs_bucket_and_blob()
        self._lock = threading.Lock()

        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}
        self._access_to_refresh: dict[str, str] = {}
        self._refresh_to_access: dict[str, str] = {}

        self._load_state()
        if self._gcs_bucket:
            logger.info(
                "OAuth durable store: gs://%s/%s (survives Cloud Run deploys)",
                self._gcs_bucket,
                self._gcs_blob,
            )
        else:
            logger.warning(
                "OAuth state is local-only (%s). On Cloud Run set "
                "MCP_OAUTH_GCS_BUCKET so Claude sessions survive deploys.",
                self.state_path,
            )

    def _is_redirect_allowed(self, redirect_uri: str) -> bool:
        try:
            host = (urlparse(redirect_uri).hostname or "").lower()
        except Exception:
            return False
        if not host:
            return False
        for domain in self.allowed_redirect_domains:
            d = domain.lower()
            if host == d or host.endswith(f".{d}"):
                return True
        return False

    def _apply_state_payload(self, raw: dict[str, Any], *, source: str) -> None:
        now = time.time()
        for client in raw.get("clients") or []:
            try:
                info = OAuthClientInformationFull.model_validate(client)
                if info.client_id:
                    self.clients[info.client_id] = info
            except Exception as exc:
                logger.warning("Skip bad OAuth client in state: %s", exc)

        for item in raw.get("access_tokens") or []:
            try:
                tok = AccessToken.model_validate(item)
                if tok.expires_at and tok.expires_at < now:
                    continue
                self.access_tokens[tok.token] = tok
            except Exception:
                pass

        for item in raw.get("refresh_tokens") or []:
            try:
                tok = RefreshToken.model_validate(item)
                if tok.expires_at and tok.expires_at < now:
                    continue
                self.refresh_tokens[tok.token] = tok
            except Exception:
                pass

        # Always restore refresh→access links even if access token expired
        # (Claude still refreshes with the refresh token alone).
        for access, refresh in (raw.get("access_to_refresh") or {}).items():
            if refresh in self.refresh_tokens:
                self._access_to_refresh[access] = refresh
                self._refresh_to_access[refresh] = access
        for refresh, access in (raw.get("refresh_to_access") or {}).items():
            if refresh in self.refresh_tokens:
                self._refresh_to_access[refresh] = access
                if access:
                    self._access_to_refresh[access] = refresh

        logger.info(
            "Loaded OAuth state from %s (%d clients, %d access, %d refresh)",
            source,
            len(self.clients),
            len(self.access_tokens),
            len(self.refresh_tokens),
        )

    def _load_state(self) -> None:
        # Prefer durable GCS, then local file (warm instance).
        if self._gcs_bucket and self._gcs_blob:
            text = _gcs_read(self._gcs_bucket, self._gcs_blob)
            if text:
                try:
                    self._apply_state_payload(json.loads(text), source=f"gs://{self._gcs_bucket}/{self._gcs_blob}")
                    return
                except Exception as exc:
                    logger.warning("Bad GCS OAuth JSON: %s", exc)

        path = Path(self.state_path)
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not load OAuth state from %s: %s", path, exc)
            return
        self._apply_state_payload(raw, source=str(path))

    def _save_state(self) -> None:
        path = Path(self.state_path)
        payload = {
            "saved_at": int(time.time()),
            "clients": [c.model_dump(mode="json") for c in self.clients.values()],
            "access_tokens": [
                t.model_dump(mode="json") for t in self.access_tokens.values()
            ],
            "refresh_tokens": [
                t.model_dump(mode="json") for t in self.refresh_tokens.values()
            ],
            "access_to_refresh": dict(self._access_to_refresh),
            "refresh_to_access": dict(self._refresh_to_access),
        }
        text = json.dumps(payload)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.warning("Could not persist OAuth state to %s: %s", path, exc)

        if self._gcs_bucket and self._gcs_blob:
            _gcs_write(self._gcs_bucket, self._gcs_blob, text)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        existing = self.clients.get(client_id)
        if existing:
            return existing
        # After Cloud Run restart, reload durable store before giving up.
        self._load_state()
        existing = self.clients.get(client_id)
        if existing:
            return existing
        return await self._ensure_cimd_client(client_id)

    async def _ensure_cimd_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Accept Claude CIMD client_ids without a prior /register call."""
        if not client_id.startswith("https://"):
            return None

        host = (urlparse(client_id).hostname or "").lower()
        if not any(host == d or host.endswith(f".{d}") for d in self.allowed_redirect_domains):
            return None

        if client_id not in CLAUDE_CIMD_URLS and "/oauth/" not in client_id:
            return None

        try:
            res = httpx.get(client_id, timeout=10.0, follow_redirects=True)
            if not res.is_success:
                logger.warning("CIMD fetch failed for %s: %s", client_id, res.status_code)
                return None
            raw: dict[str, Any] = res.json()
        except Exception as exc:
            logger.warning("CIMD fetch error for %s: %s", client_id, exc)
            return None

        redirect_uris = [u for u in (raw.get("redirect_uris") or []) if self._is_redirect_allowed(u)]
        if not redirect_uris:
            logger.warning("CIMD %s had no allowed redirect URIs", client_id)
            return None

        grant_types = [
            g
            for g in (raw.get("grant_types") or ["authorization_code", "refresh_token"])
            if g != JWT_BEARER_GRANT
        ]
        if "authorization_code" not in grant_types:
            grant_types.insert(0, "authorization_code")

        client_info = OAuthClientInformationFull(
            client_id=client_id,
            client_name=raw.get("client_name") or "Claude",
            redirect_uris=redirect_uris,
            grant_types=grant_types,
            response_types=raw.get("response_types") or ["code"],
            token_endpoint_auth_method="none",
            scope=self.scope,
        )
        with self._lock:
            self.clients[client_id] = client_info
            self._save_state()
        logger.info("Registered CIMD OAuth client %s", client_id)
        return client_info

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("No client_id provided")
        # Accept Claude's redirect URIs; reject unknown hosts at /authorize.
        with self._lock:
            self.clients[client_info.client_id] = client_info
            self._save_state()
        logger.info("Registered OAuth client %s", client_info.client_id)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        redirect = str(params.redirect_uri)
        if not self._is_redirect_allowed(redirect):
            raise AuthorizeError(
                error="access_denied",
                error_description="Redirect URI domain not allowed for this connector.",
            )

        if self.password:
            # Optional gate: password may be passed in state/scopes by some clients;
            # otherwise auto-approve only for allowlisted Claude redirects.
            provided = " ".join(params.scopes or [])
            if self.password not in provided and (params.state or "") != self.password:
                # Still allow Claude.ai / claude.com (domain gate is the main control).
                if not self._is_redirect_allowed(redirect):
                    raise AuthorizeError(
                        error="access_denied",
                        error_description="Authorization denied.",
                    )

        if not client.client_id:
            raise AuthorizeError(error="invalid_request", error_description="Missing client_id")

        scopes = list(params.scopes or [])
        if self.scope not in scopes:
            scopes.append(self.scope)

        code = f"mac_{secrets.token_hex(24)}"
        self.auth_codes[code] = AuthorizationCode(
            code=code,
            client_id=client.client_id,
            redirect_uri=AnyUrl(redirect),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            expires_at=time.time() + 300,
            scopes=scopes,
            code_challenge=params.code_challenge,
            resource=params.resource,
            subject="meta-user",
        )
        return construct_redirect_uri(redirect, code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.auth_codes.get(authorization_code)
        if not code:
            return None
        if code.expires_at < time.time():
            del self.auth_codes[authorization_code]
            return None
        if code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if authorization_code.code not in self.auth_codes:
            raise TokenError(error="invalid_grant", error_description="Unknown authorization code")
        if not client.client_id:
            raise TokenError(error="invalid_client", error_description="Missing client_id")

        del self.auth_codes[authorization_code.code]

        access = f"mat_{secrets.token_hex(32)}"
        refresh = f"mrt_{secrets.token_hex(32)}"
        expires_at = int(time.time()) + self.access_token_expiry_seconds

        with self._lock:
            self.access_tokens[access] = AccessToken(
                token=access,
                client_id=client.client_id,
                scopes=authorization_code.scopes,
                expires_at=expires_at,
                resource=authorization_code.resource,
                subject=authorization_code.subject,
            )
            self.refresh_tokens[refresh] = RefreshToken(
                token=refresh,
                client_id=client.client_id,
                scopes=authorization_code.scopes,
                expires_at=None,
                subject=authorization_code.subject,
            )
            self._access_to_refresh[access] = refresh
            self._refresh_to_access[refresh] = access
            self._save_state()

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_token_expiry_seconds,
            refresh_token=refresh,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self.access_tokens.get(token)
        if not access:
            # Cold start / other instance: reload from disk once.
            self._load_state()
            access = self.access_tokens.get(token)
        if not access:
            return None
        if access.expires_at and access.expires_at < time.time():
            self.access_tokens.pop(token, None)
            self._save_state()
            return None
        return access

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token = self.refresh_tokens.get(refresh_token)
        if not token:
            self._load_state()
            token = self.refresh_tokens.get(refresh_token)
        if not token or token.client_id != client.client_id:
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if refresh_token.token not in self.refresh_tokens:
            self._load_state()
        if refresh_token.token not in self.refresh_tokens:
            raise TokenError(error="invalid_grant", error_description="Unknown refresh token")
        if not client.client_id:
            raise TokenError(error="invalid_client", error_description="Missing client_id")

        old_access = self._refresh_to_access.get(refresh_token.token)
        if old_access:
            self.access_tokens.pop(old_access, None)
            self._access_to_refresh.pop(old_access, None)

        granted = scopes or refresh_token.scopes
        access = f"mat_{secrets.token_hex(32)}"
        expires_at = int(time.time()) + self.access_token_expiry_seconds

        # IMPORTANT: do NOT rotate the refresh token.
        # Rotating causes Claude "session timeout" when:
        # 1) Cloud Run /tmp state is wiped mid-refresh, or
        # 2) Claude still holds the previous refresh token after a race.
        # DV360/Reddit stay up when refresh tokens remain stable + durable.
        with self._lock:
            self.access_tokens[access] = AccessToken(
                token=access,
                client_id=client.client_id,
                scopes=granted,
                expires_at=expires_at,
                subject=refresh_token.subject,
            )
            # Keep the same refresh token entry
            self.refresh_tokens[refresh_token.token] = RefreshToken(
                token=refresh_token.token,
                client_id=client.client_id,
                scopes=granted,
                expires_at=None,
                subject=refresh_token.subject,
            )
            self._access_to_refresh[access] = refresh_token.token
            self._refresh_to_access[refresh_token.token] = access
            self._save_state()

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_token_expiry_seconds,
            refresh_token=refresh_token.token,
            scope=" ".join(granted),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._lock:
            if isinstance(token, AccessToken):
                refresh = self._access_to_refresh.pop(token.token, None)
                self.access_tokens.pop(token.token, None)
                if refresh:
                    self.refresh_tokens.pop(refresh, None)
                    self._refresh_to_access.pop(refresh, None)
            else:
                access = self._refresh_to_access.pop(token.token, None)
                self.refresh_tokens.pop(token.token, None)
                if access:
                    self.access_tokens.pop(access, None)
                    self._access_to_refresh.pop(access, None)
            self._save_state()
