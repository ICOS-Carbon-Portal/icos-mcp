"""Which stores the server offers, and to whom (access.py): default-deny,
public by rule, token stores only with a listed token -- and a hidden
store indistinguishable from one that does not exist."""

import json
import logging

import pytest
from mcp import Client

from icos_mcp import access, server
from icos_mcp.server import mcp

logging.disable(logging.INFO)
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_default_policy_is_zarr_icos_cp_eu_public_stores():
    p = access.load()
    for s in (
        "icos-obspack.zarr",
        "icos-socat.zarr",
        "icos-fluxnet.zarr",
        "socat.zarr",
        "socat-gridded.zarr",
        "icos-eco-nrt.zarr",
    ):
        assert p.allowed(s, None), s
    # what zpd also serves inside CT122, and nobody listed
    for s in (
        "fluxnet-shuttle.zarr",
        "noaa-obspack.zarr",
        "agage.zarr",
        "something-new.zarr",
    ):
        assert not p.allowed(s, None), s


def test_token_store_needs_a_listed_token(tmp_path, monkeypatch):
    good = "s3cret-token"
    (tmp_path / "shuttle.sha256").write_text(
        f"# shuttle readers\n{access.hash_token(good)}\n"
    )
    cfg = {
        "proxy": "http://127.0.0.1:8081",
        "proxy_host": "zarr-proxy",
        "public_url": "https://zarr.icos-cp.eu",
        "stores": [
            {"match": "icos-*", "access": "public"},
            {
                "match": "fluxnet-shuttle*",
                "access": "token",
                "tokens_file": str(tmp_path / "shuttle.sha256"),
                "public_url": "https://fluxnet-shuttle-zarr.icos-cp.eu",
            },
        ],
    }
    (tmp_path / "c.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("ICOS_MCP_CONFIG", str(tmp_path / "c.json"))
    p = access.load()
    assert p.proxy_host == "zarr-proxy" and p.allowed("icos-obspack.zarr", None)
    assert not p.allowed("fluxnet-shuttle.zarr", None)
    assert not p.allowed("fluxnet-shuttle.zarr", "wrong")
    assert p.allowed("fluxnet-shuttle.zarr", good)
    assert not p.allowed("socat.zarr", good)  # not listed: denied, token or not
    assert (
        p.url_for("fluxnet-shuttle.zarr") == "https://fluxnet-shuttle-zarr.icos-cp.eu"
    )
    assert p.url_for("icos-obspack.zarr") == "https://zarr.icos-cp.eu"


def test_a_token_store_without_tokens_is_refused_at_load(tmp_path, monkeypatch):
    (tmp_path / "c.json").write_text(
        json.dumps({"stores": [{"match": "agage*", "access": "token"}]})
    )
    monkeypatch.setenv("ICOS_MCP_CONFIG", str(tmp_path / "c.json"))
    with pytest.raises(ValueError, match="lists no tokens"):
        access.load()


class _Ctx:
    def __init__(self, headers):
        self.headers = headers


def test_an_http_caller_is_judged_by_its_own_header_only(monkeypatch):
    monkeypatch.setenv("ICOS_MCP_TOKEN", "operator-token")
    assert server._token(_Ctx({"authorization": "Bearer abc"})) == "abc"
    # an HTTP request WITHOUT a token gets none -- never the operator's
    assert server._token(_Ctx({"user-agent": "x"})) is None
    # a local caller (no headers at all) is the operator
    assert server._token(_Ctx(None)) == "operator-token"


async def _ids(text):
    async with Client(mcp) as c:
        r = (
            await c.call_tool("search_variables", {"text": text, "limit": 50})
        ).structured_content
    return [v["id"] for v in r["variables"]]


async def test_a_hidden_store_disappears_from_every_tool(monkeypatch):
    hidden = access.Policy(
        proxy=server.POLICY.proxy,
        proxy_host=None,
        public_url=server.POLICY.public_url,
        rules=[access.Rule("icos-obspack*", "public")],
    )
    monkeypatch.setattr(server, "POLICY", hidden)
    assert not any(i.startswith("icos-socat") for i in await _ids("ocean co2"))
    async with Client(mcp) as c:
        d = (await c.call_tool("list_datasets", {})).structured_content["datasets"]
        assert [x["store"] for x in d] == ["icos-obspack.zarr"]
        r = await c.call_tool("get_data", {"id": "icos-socat._obs.fCO2"})
        assert (
            r.is_error and "no variable" in r.content[0].text
        )  # as if it did not exist
        r = await c.call_tool("find_stations", {"id": "icos-socat._obs.fCO2"})
        assert r.is_error and "no variable" in r.content[0].text
        with pytest.raises(Exception, match="no store"):
            await c.read_resource("icos://stores/icos-socat/agent-guide")


async def test_a_token_store_appears_for_its_token_holder(monkeypatch):
    tok = "ocean-reader"
    pol = access.Policy(
        proxy=server.POLICY.proxy,
        proxy_host=None,
        public_url=server.POLICY.public_url,
        rules=[
            access.Rule("icos-obspack*", "public"),
            access.Rule("icos-socat*", "token", frozenset({access.hash_token(tok)})),
        ],
    )
    monkeypatch.setattr(server, "POLICY", pol)
    monkeypatch.delenv("ICOS_MCP_TOKEN", raising=False)
    assert not any(i.startswith("icos-socat") for i in await _ids("ocean co2"))
    monkeypatch.setenv("ICOS_MCP_TOKEN", tok)  # the local operator's token
    assert "icos-socat._obs.fCO2" in await _ids("ocean co2")
