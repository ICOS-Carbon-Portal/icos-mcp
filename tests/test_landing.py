"""The HTML page at `/`: built from the server, and never naming a store an
anonymous visitor cannot use."""

import logging

import httpx
import pytest

from icos_mcp import access, server

logging.disable(logging.INFO)
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _get(path: str) -> httpx.Response:
    app = server.mcp.streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        return await c.get(path)


async def test_the_page_lists_what_an_anonymous_caller_gets(monkeypatch):
    pol = access.Policy(
        proxy=server.POLICY.proxy,
        proxy_host=None,
        public_url=server.POLICY.public_url,
        rules=[
            access.Rule("icos-obspack*", "public"),
            access.Rule("icos-socat*", "token", frozenset({access.hash_token("x")})),
        ],
    )
    monkeypatch.setattr(server, "POLICY", pol)
    r = await _get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    page = r.text
    assert "Datasets (1)" in page and "icos-obspack.zarr" in page
    assert "icos-socat" not in page  # a token store is not named
    assert "Authorization: Bearer" in page  # but its existence is
    for tool in ("search_variables", "find_stations", "aggregate", "get_data"):
        assert f"<code>{tool}</code>" in page
    assert "https://mcp.icos-cp.eu/mcp" in page


async def test_health():
    r = await _get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
