"""Who is calling: OAuth sign-in through ICOS's Keycloak, required over HTTP
when the config has an ``oauth`` section.

The server is a RESOURCE SERVER only.  Keycloak is the authorization
server: MCP clients (Claude, Claude Code, ...) find it from this server's
protected-resource metadata, register themselves with it dynamically,
and send the user to sign in with whatever it brokers -- cpauth, Google,
ORCID.  Anyone may sign in; the point is to know who uses the server, not
to keep anyone out (zarr.icos-cp.eu itself stays open).

    "oauth": {
      "issuer":   "https://sso.fs4vm.icos-cp.eu/realms/icos-test",
      "audience": "https://mcp.icos-cp.eu",      # Keycloak's audience mapper
      "resource": "https://mcp.icos-cp.eu",      # what clients are told (default: audience)
      "scope":    "icos-mcp"                     # asked for, and required (optional)
    }

The scope is the Keycloak client scope that carries the audience mapper:
clients request what the protected-resource metadata lists, so only a
token asked for THIS server carries its audience.

A token is a Keycloak JWT -- signature by the realm's published keys,
issuer, audience and expiry all checked -- or one of the store tokens of
access.py, which keep working as they did.  The local stdio caller is
the operator and is never asked.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]


@dataclass(frozen=True)
class OAuth:
    issuer: str
    audience: str
    resource: str
    scope: str | None = None

    @property
    def jwks_uri(self) -> str:
        # Keycloak's; fixed by the realm, so not looked up per start
        return f"{self.issuer}/protocol/openid-connect/certs"


def from_config(c: dict | None, path: str) -> OAuth | None:
    if not c:
        return None
    for k in ("issuer", "audience"):
        if not c.get(k):
            raise ValueError(
                f"{path}: oauth needs {k!r} -- without an audience a token "
                f"Keycloak issued for ANY client of the realm would pass"
            )
    issuer = c["issuer"].rstrip("/")
    return OAuth(
        issuer, c["audience"], c.get("resource", c["audience"]), c.get("scope")
    )


def caller_groups() -> frozenset[str]:
    """The Keycloak groups of the caller of the request in hand: the
    ``groups`` claim of the token the SDK's bearer check VERIFIED -- paths
    below /apps/mcp ("agage", "agage/ops"), the only slice the realm's
    mapper gives this server.  None over stdio or without sign-in."""
    tok = get_access_token()
    g = (tok.claims or {}).get("groups") if tok else None
    return (
        frozenset(x.strip("/") for x in g if isinstance(x, str)) if g else frozenset()
    )


def user_id(tok: AccessToken | None) -> str | None:
    """The caller as recorded anywhere: a hash of issuer and subject, never
    the subject (a Keycloak user id, linkable to a person) itself."""
    if tok is None or tok.subject is None:
        return None
    iss = (tok.claims or {}).get("iss", "")
    return hashlib.sha256(f"{iss} {tok.subject}".encode()).hexdigest()[:16]


class Verifier:
    """The SDK's TokenVerifier: a Keycloak JWT, or a listed store token."""

    def __init__(
        self,
        oauth: OAuth,
        store_tokens: frozenset[str],
        key_for: Callable[[str], object] | None = None,
    ):
        self.oauth = oauth
        self.store_tokens = store_tokens
        if key_for is None:
            jwks = jwt.PyJWKClient(oauth.jwks_uri, cache_keys=True, lifespan=3600)
            key_for = lambda t: jwks.get_signing_key_from_jwt(t).key
        self.key_for = key_for

    async def verify_token(self, token: str) -> AccessToken | None:
        h = hashlib.sha256(token.encode()).hexdigest()
        if any(hmac.compare_digest(h, t) for t in self.store_tokens):
            # signed in by other means: it counts as holding the scope
            scopes = [self.oauth.scope] if self.oauth.scope else []
            return AccessToken(token=token, client_id="store-token", scopes=scopes)
        try:
            # the key fetch is blocking (and cached): off the event loop
            key = await asyncio.to_thread(self.key_for, token)
            c = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=self.oauth.audience,
                issuer=self.oauth.issuer,
                options={"require": ["exp", "iss", "sub"]},
            )
        except (jwt.PyJWTError, ValueError):
            return None
        return AccessToken(
            token=token,
            client_id=c.get("azp") or c.get("client_id") or "",
            scopes=(c.get("scope") or "").split(),
            expires_at=c["exp"],
            resource=self.oauth.resource,
            subject=c["sub"],
            claims={
                "iss": c["iss"],
                "idp": c.get("identity_provider"),
                "groups": c.get("groups") or [],
            },
        )
