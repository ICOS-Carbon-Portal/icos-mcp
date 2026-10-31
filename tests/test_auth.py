"""Signing in (auth.py): with an oauth section, /mcp over HTTP wants a
Keycloak token -- signature, issuer, audience and expiry checked -- or a
listed store token; the metadata naming Keycloak, the landing page and
/health stay open."""

import json
import logging
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.settings import AuthSettings

from icos_mcp import access, auth, server

logging.disable(logging.INFO)
pytestmark = pytest.mark.anyio

ISS = "https://sso.example.org/realms/icos-test"
AUD = "https://mcp.example.org"
OAUTH = auth.OAuth(ISS, AUD, AUD, "icos-mcp")
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
STORE_TOKEN = "s3cret-store-token"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def token(key=KEY, **over) -> str:
    c = {
        "iss": ISS,
        "aud": AUD,
        "sub": "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",
        "azp": "claude-dcr-client",
        "scope": "openid profile icos-mcp",
        "identity_provider": "orcid",
        "exp": int(time.time()) + 300,
        **over,
    }
    return jwt.encode(c, key, algorithm="RS256")


def verifier() -> auth.Verifier:
    return auth.Verifier(
        OAUTH,
        frozenset({access.hash_token(STORE_TOKEN)}),
        key_for=lambda t: KEY.public_key(),
    )


async def test_a_keycloak_token_is_accepted_and_names_the_user():
    t = await verifier().verify_token(token())
    assert t is not None
    assert t.client_id == "claude-dcr-client"
    assert t.subject == "f81d4fae-7dec-11d0-a765-00a0c91e6bf6"
    assert t.claims["idp"] == "orcid"
    # what gets recorded is a hash, never the Keycloak user id
    uid = auth.user_id(t)
    assert len(uid) == 16 and t.subject not in uid


@pytest.mark.parametrize(
    "bad",
    [
        {"aud": "account"},  # issued for another client of the realm
        {"iss": "https://sso.example.org/realms/other"},
        {"exp": int(time.time()) - 10},
        {"sub": None},
    ],
)
async def test_a_token_not_for_this_server_is_refused(bad):
    c = {k: v for k, v in bad.items() if v is not None}
    t = token(**c)
    if bad.get("sub", "") is None:
        claims = jwt.decode(t, options={"verify_signature": False})
        del claims["sub"]
        t = jwt.encode(claims, KEY, algorithm="RS256")
    assert await verifier().verify_token(t) is None


async def test_a_forged_or_garbled_token_is_refused():
    assert await verifier().verify_token(token(key=OTHER)) is None
    assert await verifier().verify_token("not-a-jwt") is None


async def test_a_store_token_still_counts():
    t = await verifier().verify_token(STORE_TOKEN)
    assert t is not None and t.client_id == "store-token"
    assert t.scopes == ["icos-mcp"]
    assert auth.user_id(t) is None


def test_oauth_without_audience_is_refused_at_load(tmp_path, monkeypatch):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"stores": [], "oauth": {"issuer": ISS}}))
    monkeypatch.setenv("ICOS_MCP_CONFIG", str(cfg))
    with pytest.raises(ValueError, match="audience"):
        access.load()


def test_the_oauth_section_is_read(tmp_path, monkeypatch):
    cfg = tmp_path / "c.json"
    cfg.write_text(
        json.dumps(
            {
                "stores": [],
                "oauth": {"issuer": ISS + "/", "audience": AUD, "scope": "icos-mcp"},
            }
        )
    )
    monkeypatch.setenv("ICOS_MCP_CONFIG", str(cfg))
    assert access.load().oauth == OAUTH


@pytest.fixture
def signed_in(monkeypatch):
    """The module's server as an oauth config builds it -- set after the
    fact, since the server object is made at import."""
    monkeypatch.setattr(
        server.mcp.settings,
        "auth",
        AuthSettings(
            issuer_url=ISS,
            resource_server_url=AUD,
            required_scopes=["icos-mcp"],
            validate_token_resource=False,
        ),
    )
    monkeypatch.setattr(server.mcp, "_token_verifier", verifier())
    app = server.mcp.streamable_http_app(stateless_http=True)  # as deployed
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8090"
    )


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


async def test_mcp_without_a_token_is_401_pointing_at_the_metadata(signed_in):
    async with signed_in as c:
        r = await c.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert r.status_code == 401
        assert "resource_metadata=" in r.headers["www-authenticate"]
        r = await c.post(
            "/mcp",
            json=INIT,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token(aud='x')}"},
        )
        assert r.status_code == 401
        # signed, for this server, but not asked for the scope
        r = await c.post(
            "/mcp",
            json=INIT,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token(scope='openid')}"},
        )
        assert r.status_code == 403


async def test_the_metadata_names_keycloak(signed_in):
    async with signed_in as c:
        r = await c.get("/.well-known/oauth-protected-resource")
        assert r.status_code == 200
        m = r.json()
        assert m["authorization_servers"] == [ISS]
        assert m["resource"].rstrip("/") == AUD
        assert m["scopes_supported"] == ["icos-mcp"]


async def test_a_signed_in_caller_gets_in(signed_in):
    # past the bearer check, /mcp needs the session manager running
    app = signed_in._transport.app
    async with app.router.lifespan_context(app), signed_in as c:
        for t in (token(), STORE_TOKEN):
            r = await c.post(
                "/mcp",
                json=INIT,
                headers={**MCP_HEADERS, "Authorization": f"Bearer {t}"},
            )
            assert r.status_code == 200, r.text


async def test_the_landing_page_and_health_stay_open(signed_in):
    async with signed_in as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.get("/")).status_code == 200


def test_a_group_store_admits_members_and_subgroups_only():
    # names below /apps/mcp, as the realm's subtree mapper sends them
    r = access.Rule("agage*", "group", groups=frozenset({"agage"}))
    p = access.Policy("http://p", None, "http://p", [r])
    assert p.allowed("agage.zarr", None, frozenset({"agage"}))
    assert p.allowed("agage.zarr", None, frozenset({"agage/ops"}))
    for g in ({"agage-x"}, {"globalview"}, {"ops/agage"}, set()):
        assert not p.allowed("agage.zarr", None, frozenset(g)), g


def test_a_group_store_may_take_a_store_token_too():
    tok = "script-token"
    r = access.Rule(
        "agage*",
        "group",
        tokens=frozenset({access.hash_token(tok)}),
        groups=frozenset({"agage"}),
    )
    p = access.Policy("http://p", None, "http://p", [r])
    assert p.allowed("agage.zarr", tok) and not p.allowed("agage.zarr", "x")


@pytest.mark.parametrize(
    "cfg, msg",
    [
        ({"stores": [{"match": "agage*", "access": "group"}]}, "lists no groups"),
        (
            {"stores": [{"match": "agage*", "access": "group", "groups": ["/a"]}]},
            "needs an oauth section",
        ),
    ],
)
def test_an_unreachable_group_store_is_refused_at_load(tmp_path, monkeypatch, cfg, msg):
    (tmp_path / "c.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("ICOS_MCP_CONFIG", str(tmp_path / "c.json"))
    with pytest.raises(ValueError, match=msg):
        access.load()


async def _stores(c, bearer: str) -> list[str]:
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "list_datasets", "arguments": {}},
    }
    r = await c.post(
        "/mcp",
        json=call,
        headers={
            **MCP_HEADERS,
            "Authorization": f"Bearer {bearer}",
            "Mcp-Protocol-Version": "2025-11-25",
        },
    )
    assert r.status_code == 200, r.text
    body = r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        body = next(
            ln[5:]
            for ln in body.splitlines()
            if ln.startswith("data:") and ln[5:].strip()
        )
    res = json.loads(body)["result"]["structuredContent"]
    return [d["store"] for d in res["datasets"]]


async def test_a_group_store_appears_only_to_a_signed_in_member(signed_in, monkeypatch):
    pol = access.Policy(
        proxy=server.POLICY.proxy,
        proxy_host=None,
        public_url=server.POLICY.public_url,
        rules=[
            access.Rule("icos-obspack*", "public"),
            access.Rule("icos-socat*", "group", groups=frozenset({"ocean"})),
        ],
    )
    monkeypatch.setattr(server, "POLICY", pol)
    app = signed_in._transport.app
    async with app.router.lifespan_context(app), signed_in as c:
        assert await _stores(c, token()) == ["icos-obspack.zarr"]
        member = token(groups=["globalview", "ocean"])
        assert await _stores(c, member) == ["icos-obspack.zarr", "icos-socat.zarr"]
        # groups count only from a token that verified: a store token has none
        assert await _stores(c, STORE_TOKEN) == ["icos-obspack.zarr"]
