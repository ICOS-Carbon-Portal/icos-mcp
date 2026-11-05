"""The licence gate (access.py ``licence_gate``): the data tools ask the user
to accept the ICOS data licence through elicitation, the metadata tools never
ask, and with the gate off nothing is asked and nothing in the tools' schemas
changes.  In process, over the recorded proxy answers (conftest.py)."""

import logging

import pytest
from mcp import Client
from mcp_types import ElicitResult

from icos_mcp import server

logging.disable(logging.INFO)
pytestmark = pytest.mark.anyio

CO2 = "icos-obspack.co2.co2"
SEL = {"id": CO2, "station": ["HTM150"], "start": "2025-01-01", "end": "2025-02-01"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(server.POLICY, "licence_gate", True)
    server._licence_seen.clear()
    yield
    server._licence_seen.clear()


def user(answer, asked):
    async def cb(context, params):
        asked.append(params.message)
        if answer is None:
            return ElicitResult(action="decline")
        return ElicitResult(action="accept", content={"accept": answer})

    return cb


def text(r):
    return "".join(getattr(c, "text", "") for c in r.content)


async def test_no_licence_argument_in_any_schema():
    async with Client(server.mcp) as c:
        for t in (await c.list_tools()).tools:
            assert "licence" not in t.input_schema.get("properties", {}), t.name


async def test_gate_off_asks_nothing():
    asked = []
    async with Client(server.mcp, elicitation_callback=user(False, asked)) as c:
        r = await c.call_tool("get_data", {**SEL, "max_rows": 5})
    assert not r.is_error and not asked


async def test_accept_returns_data(gate):
    asked = []
    async with Client(server.mcp, elicitation_callback=user(True, asked)) as c:
        r = await c.call_tool("get_data", {**SEL, "max_rows": 5})
        a = await c.call_tool("aggregate", SEL)
    assert not r.is_error and not a.is_error
    assert asked and "CC BY 4.0" in asked[0] and server.LICENCE_URL in asked[0]


@pytest.mark.parametrize("answer", [None, False])
async def test_decline_or_untick_refuses(gate, answer):
    asked = []
    async with Client(server.mcp, elicitation_callback=user(answer, asked)) as c:
        r = await c.call_tool("get_data", {**SEL, "max_rows": 5})
    assert r.is_error and len(asked) == 1
    assert server.LICENCE_URL in text(r) and "did not accept" in text(r)


async def test_client_that_cannot_ask_is_refused(gate):
    async with Client(server.mcp) as c:
        r = await c.call_tool("aggregate", SEL)
    assert r.is_error and server.LICENCE_URL in text(r)


async def test_metadata_tools_never_ask(gate):
    asked = []
    async with Client(server.mcp, elicitation_callback=user(False, asked)) as c:
        for name, args in [
            ("search_variables", {"text": "co2"}),
            ("describe_variable", {"id": CO2}),
            ("estimate", SEL),
            ("get_citation", {"id": CO2}),
        ]:
            r = await c.call_tool(name, args)
            assert not r.is_error, (name, text(r))
    assert not asked
