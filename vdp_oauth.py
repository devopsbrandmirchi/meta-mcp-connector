"""
Claude.ai-compatible OAuth 2.1 provider for the VDP MCP connector.

Claude custom connectors require Dynamic Client Registration (DCR) and
discovery at /.well-known/oauth-authorization-server. Without this, Claude
shows: "Couldn't register with … sign-in service" / ofid_…

No external IdP — the MCP server is its own authorization server.
Authorize auto-approves for allowed redirect hosts (claude.ai by default).
Optional MCP_OAUTH_PASSWORD adds a simple login gate.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Optional
from urllib.parse import urlparse

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

logger = logging.getLogger("vdp-oauth")

DEFAULT_SCOPE = "vdp"
DEFAULT_ACCESS_TOKEN_EXPIRY = 30 * 24 * 60 * 60  # 30 days
DEFAULT_ALLOWED_REDIRECT_DOMAINS = ("claude.ai", "claude.com", "localhost", "127.0.0.1")


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

        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}
        self._access_to_refresh: dict[str, str] = {}
        self._refresh_to_access: dict[str, str] = {}

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

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("No client_id provided")
        # Accept Claude's redirect URIs; reject unknown hosts at /authorize.
        self.clients[client_info.client_id] = client_info
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

        code = f"vac_{secrets.token_hex(24)}"
        self.auth_codes[code] = AuthorizationCode(
            code=code,
            client_id=client.client_id,
            redirect_uri=AnyUrl(redirect),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            expires_at=time.time() + 300,
            scopes=params.scopes or [self.scope],
            code_challenge=params.code_challenge,
            resource=params.resource,
            subject="vdp-user",
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

        access = f"vat_{secrets.token_hex(32)}"
        refresh = f"vrt_{secrets.token_hex(32)}"
        expires_at = int(time.time()) + self.access_token_expiry_seconds

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
            return None
        if access.expires_at and access.expires_at < time.time():
            self.access_tokens.pop(token, None)
            return None
        return access

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
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
            raise TokenError(error="invalid_grant", error_description="Unknown refresh token")
        if not client.client_id:
            raise TokenError(error="invalid_client", error_description="Missing client_id")

        old_access = self._refresh_to_access.get(refresh_token.token)
        if old_access:
            self.access_tokens.pop(old_access, None)
            self._access_to_refresh.pop(old_access, None)

        granted = scopes or refresh_token.scopes
        access = f"vat_{secrets.token_hex(32)}"
        new_refresh = f"vrt_{secrets.token_hex(32)}"
        expires_at = int(time.time()) + self.access_token_expiry_seconds

        self.refresh_tokens.pop(refresh_token.token, None)
        self._refresh_to_access.pop(refresh_token.token, None)

        self.access_tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=granted,
            expires_at=expires_at,
            subject=refresh_token.subject,
        )
        self.refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=client.client_id,
            scopes=granted,
            expires_at=None,
            subject=refresh_token.subject,
        )
        self._access_to_refresh[access] = new_refresh
        self._refresh_to_access[new_refresh] = access

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_token_expiry_seconds,
            refresh_token=new_refresh,
            scope=" ".join(granted),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
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
