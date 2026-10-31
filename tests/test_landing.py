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
    assert "<b>1</b><span>zarr dataset</span>" in page and "icos-obspack.zarr" in page
    assert "One domain of the carbon cycle" in page
    assert "icos-socat" not in page  # a token store is not named
    assert "Authorization: Bearer" in page  # but its existence is
    tools = await server.mcp.list_tools()
    for t in tools:  # every tool the server lists, once
        assert page.count(f'<div class="tool"><code>{t.name}</code>') == 1
    assert f"<b>{len(tools)}</b><span>tools and 3 guided prompts" in page
    assert "https://mcp.icos-cp.eu/mcp" in page
    for img in ("icos-logo.svg", "hero-ocean.jpg", "atmosphere.jpg", "team-lab.jpg"):
        assert f"/static/{img}" in page


async def test_the_static_files_are_served_and_nothing_else():
    r = await _get("/static/hero-ocean.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert (await _get("/static/icos-logo.svg")).headers[
        "content-type"
    ] == "image/svg+xml"
    assert (await _get("/static/nope.jpg")).status_code == 404
    assert (await _get("/static/..%2Fserver.py")).status_code == 404


async def test_health():
    r = await _get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_the_web_instructions_name_the_network_setting():
    # Claude Code on the web: a repository's .mcp.json loads the server,
    # but the cloud environment's default "Trusted" network level does not
    # reach this host -- the page must say which setting lets it through
    page = (await _get("/")).text
    assert (
        "&quot;type&quot;: &quot;http&quot;, &quot;url&quot;: &quot;https://mcp.icos-cp.eu/mcp&quot;"
        in page
    )
    assert "<b>Network access</b>" in page and "<i>Trusted</i>" in page
    assert page.count("<code>*.icos-cp.eu</code>") == 2


async def test_the_front_page_switches_to_retro():
    assert '<a href="/r/">Retro</a>' in (await _get("/")).text
    r = await _get("/r")
    assert r.status_code == 301 and r.headers["location"] == "/r/"


async def test_the_retro_home_page_keeps_its_links_under_r():
    r = await _get("/r/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    page = r.text
    assert '<header class="site">' in page and "--brand:#3768B8" in page
    assert '<a class="switch" href="/">New design</a>' in page
    assert '<li><a href="/r/icos-obspack.zarr/">' in page
    assert '<a href="/r/data-passport.html">data passport page</a>' in page
    for t in await server.mcp.list_tools():
        assert page.count(f"<tr><td><code>{t.name}</code></td>") == 1
    assert "/static/" not in page  # no pictures
    assert "<code>icos-mcp " in page  # the version, where the M9 replay masks it


async def test_a_retro_store_page_is_the_services_own_in_the_old_frame():
    r = await _get("/r/icos-obspack.zarr/")
    assert r.status_code == 200
    page = r.text
    assert '<header class="site">' in page and '<header class="top">' not in page
    assert '<main class="wrap page">' not in page and "<main>" in page
    assert '<script type="application/ld+json">' in page
    assert "https://zarr.icos-cp.eu/icos-obspack.zarr/agent_guide" in page
    assert '<a href="/r/">all datasets</a>' in page
    assert 'href="/' not in page.replace('href="/r/', "").replace('href="/"', "")


async def test_the_retro_passport_page():
    r = await _get("/r/data-passport.html")
    assert r.status_code == 200
    page = r.text
    assert (
        "<title>The data passport</title>" in page and '<header class="site">' in page
    )
    assert (
        'href="https://zarr.icos-cp.eu/data-passport.md"' in page
    )  # relative made absolute


async def test_a_retro_store_page_only_for_an_offered_store(monkeypatch):
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
    assert (await _get("/r/icos-socat.zarr/")).status_code == 404  # a token store
    assert (await _get("/r/nope.zarr/")).status_code == 404
    assert (await _get("/r/icos-obspack.zarr/")).status_code == 200
