"""A minimal auto-approving OAuth 2.1 provider so claude.ai's web connector (which
*requires* OAuth and has no 'no auth' option) can register and connect.

There are NO users and no consent screen: registration and authorization auto-succeed,
and a bearer token is issued. Access is still gated solely by knowing the secret MCP URL
(the token in the path) — this layer exists only to satisfy Claude's connector flow, not
to add real identity. The SDK handles PKCE, the metadata endpoints, and /register
/authorize /token routing; we just provide the (trivial) provider logic + in-memory
stores.
"""

from __future__ import annotations

import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

_CODE_TTL = 300  # seconds


class AutoApproveOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(self) -> None:
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._tokens: dict[str, AccessToken] = {}

    # -- client registration (accept any) -----------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    # -- authorization (auto-approve, no consent UI) ------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        code = "ac_" + secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + _CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        ac = self._codes.get(authorization_code)
        if ac and ac.client_id == client.client_id and ac.expires_at >= time.time():
            return ac
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        token = "at_" + secrets.token_urlsafe(32)
        self._tokens[token] = AccessToken(
            token=token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=None,
            resource=authorization_code.resource,
        )
        return OAuthToken(
            access_token=token,
            token_type="Bearer",
            scope=" ".join(authorization_code.scopes) or None,
        )

    # -- token validation (used to gate MCP requests) -----------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self._tokens.get(token)

    # -- refresh tokens: not issued / not supported -------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        raise NotImplementedError("refresh tokens are not supported")

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._tokens.pop(getattr(token, "token", ""), None)
