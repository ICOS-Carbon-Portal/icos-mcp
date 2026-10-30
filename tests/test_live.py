"""Live smoke tests: every tool against the proxy at ICOS_PROXY.

These read production data, so they assert SHAPES and invariants -- the
citation present, truncation never silent, the download URL naming the
same selection -- rather than values.  The recorded contract tests of
phase 2 will pin values against a frozen corpus.
"""

import json
import logging
from urllib.parse import parse_qs, urlparse

import pytest
from mcp import Client

from icos_mcp.server import ROWS_MAX, mcp

logging.disable(logging.INFO)
pytestmark = pytest.mark.anyio

CO2 = "icos-obspack.co2.co2"
JAN = {"start": "2025-01-01", "end": "2025-02-01"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def call(name, args=None):
    async with Client(mcp) as c:
        r = await c.call_tool(name, args or {})
        return r


async def test_tools_listed_and_read_only():
    async with Client(mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {
        "list_datasets",
        "search_variables",
        "describe_variable",
        "estimate",
        "get_data",
        "get_citation",
    }
    for t in tools.values():
        assert t.description and t.output_schema
        assert t.annotations.read_only_hint and not t.annotations.destructive_hint


async def test_datasets_cover_three_domains():
    d = (await call("list_datasets")).structured_content["datasets"]
    assert {x["domain"] for x in d} >= {"atmosphere", "ocean", "ecosystem"}
    # icos-eco-nrt is not catalogued yet: read with xarray, not /query
    assert all(x["n_variables"] > 0 for x in d if x["store"] != "icos-eco-nrt.zarr")


@pytest.mark.parametrize(
    "text, first",
    [
        ("methane", "icos-obspack.ch4.ch4"),
        ("carbon dioxide", "icos-obspack.co2.co2"),
        ("nitrous oxide", "icos-obspack.n2o.n2o"),
        ("ocean co2", "icos-socat._obs.fCO2"),
    ],
)
async def test_search_finds_the_primary_measurement_first(text, first):
    r = (await call("search_variables", {"text": text})).structured_content
    assert r["variables"][0]["id"] == first


async def test_estimate_counts_a_station_exactly():
    whole = (await call("estimate", {"id": CO2, **JAN})).structured_content
    one = (
        await call("estimate", {"id": CO2, "station": ["HTM150"], **JAN})
    ).structured_content
    assert whole["exact"] and one["exact"]
    assert 0 < one["rows"] < whole["rows"]


async def test_get_data_truncates_loudly_and_cites():
    est = (
        await call("estimate", {"id": CO2, "station": ["HTM150"], **JAN})
    ).structured_content
    d = (
        await call("get_data", {"id": CO2, "station": ["HTM150"], **JAN, "max_rows": 5})
    ).structured_content
    assert d["rows_returned"] == 5 == len(d["rows"])
    assert d["truncated"] and d["rows_total"] == est["rows"]
    assert all(len(r) == len(d["columns"]) for r in d["rows"])
    assert d["citation"]["citation"] and d["citation"]["license"] == "CC-BY-4.0"
    assert d["citation"]["doi"].startswith("https://doi.org/")
    q = parse_qs(urlparse(d["download_url"]).query)
    assert q["id"] == [CO2] and q["station"] == ["HTM150"] and q["format"] == ["csv"]
    assert "max_rows" not in q


async def test_get_data_whole_small_selection_is_not_truncated():
    d = (
        await call(
            "get_data",
            {
                "id": CO2,
                "station": ["HTM150"],
                "start": "2025-01-01",
                "end": "2025-01-01T06:00:00",
            },
        )
    ).structured_content
    assert not d["truncated"] and d["rows_total"] == d["rows_returned"] == 6


async def test_row_cap_is_enforced():
    r = await call("get_data", {"id": CO2, "max_rows": ROWS_MAX + 1})
    assert r.is_error


async def test_unknown_id_is_named():
    r = await call("get_data", {"id": "icos-obspack.co2.nope"})
    assert r.is_error and "no variable" in r.content[0].text


async def test_citation_matches_the_data_answer():
    c = (await call("get_citation", {"id": CO2})).structured_content
    d = (
        await call("get_data", {"id": CO2, "station": ["HTM150"], **JAN, "max_rows": 1})
    ).structured_content
    assert c["citation"] == d["citation"]["citation"]
    assert json.dumps(c["license"]) == json.dumps(d["citation"]["license"])
