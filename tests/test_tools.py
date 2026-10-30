"""Contract tests: every tool, resource and prompt, over recorded proxy
answers (conftest.py).  They assert the invariants a client relies on --
the citation present, truncation never silent, the download URL naming
the same selection, station ids that get_data accepts -- and, because
the answers are recorded, they also pin the values.
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
        "find_stations",
        "estimate",
        "aggregate",
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
        ("air temperature", "icos-obspack.mto.at"),
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


async def test_find_stations_by_name_gives_ids_get_data_accepts():
    r = (
        await call("find_stations", {"id": CO2, "text": "Hyltemossa"})
    ).structured_content
    ids = [s["id"] for s in r["stations"]]
    assert ids == ["HTM150", "HTM30", "HTM70"]
    assert all(
        s["name"] == "Hyltemossa" and s["sampling_height"] for s in r["stations"]
    )
    d = (
        await call("get_data", {"id": CO2, "station": ["HTM150"], **JAN, "max_rows": 1})
    ).structured_content
    assert d["rows"][0][d["columns"].index("station")] == "HTM150"


async def test_find_stations_near_a_point_is_sorted_by_distance():
    r = (
        await call(
            "find_stations",
            {
                "id": "icos-obspack.ch4.ch4",
                "near_lat": 53.55,
                "near_lon": 9.99,
                "radius_km": 250,
            },
        )
    ).structured_content
    d = [s["distance_km"] for s in r["stations"]]
    assert d == sorted(d) and all(x <= 250 for x in d) and r["n_matches"] == len(d)


@pytest.mark.parametrize(
    "args, want",
    [
        ({"id": "icos-fluxnet._combined-fluxnet_dd.NEE", "country": "FI"}, "FI-Hyy"),
        (
            {
                "id": "icos-eco-nrt._combined-meteo.TA",
                "lat_min": 60,
                "lat_max": 71,
                "lon_min": 0,
                "lon_max": 35,
            },
            "FI-Sod",
        ),
    ],
)
async def test_find_stations_ecosystem_stores(args, want):
    r = (await call("find_stations", args)).structured_content
    assert want in [s["id"] for s in r["stations"]]


async def test_find_stations_refuses_an_obs_table_by_name():
    r = await call("find_stations", {"id": "icos-socat._obs.fCO2"})
    assert r.is_error and "bounding box" in r.content[0].text


async def test_resources_and_prompts():
    async with Client(mcp) as c:
        uris = {x.uri for x in (await c.list_resources()).resources}
        assert {"icos://catalogue", "icos://llms.txt", "icos://data-passport"} <= uris
        g = await c.read_resource("icos://stores/icos-obspack/agent-guide")
        assert json.loads(g.contents[0].text)["domain"] == "atmosphere"
        names = {p.name for p in (await c.list_prompts()).prompts}
        assert names == {"station_record", "compare_stations", "ocean_region"}
        p = await c.get_prompt(
            "station_record", {"quantity": "methane", "station": "Norunda"}
        )
        assert "find_stations" in p.messages[0].content.text


async def test_search_offers_nrt_when_asked():
    r = (
        await call("search_variables", {"text": "near-real-time carbon dioxide flux"})
    ).structured_content
    assert r["variables"][0]["store"] == "icos-eco-nrt.zarr"


async def test_aggregate_over_the_whole_selection():
    d = (
        await call("aggregate", {"id": CO2, "station": ["HTM150"], **JAN})
    ).structured_content
    est = (
        await call("estimate", {"id": CO2, "station": ["HTM150"], **JAN})
    ).structured_content
    (g,) = d["groups"]
    assert d["rows_used"] == est["rows"] and g["n"] <= est["rows"]
    assert g["min"] <= g["mean"] <= g["max"] and g["std"] > 0
    assert d["citation"]["citation"] and d["unit"] == "ppm"


async def test_aggregate_by_station_and_month():
    d = (
        await call(
            "aggregate",
            {
                "id": "icos-obspack.n2o.n2o",
                "station": ["JFJ13", "IZO29"],
                "start": "2024-01-01",
                "end": "2025-01-01",
                "by": "station_month",
            },
        )
    ).structured_content
    # a station may miss a month (JFJ13 does in 2024): at most 2 x 12 groups
    assert 12 < d["n_groups"] <= 24
    assert {g["station"] for g in d["groups"]} == {"JFJ13", "IZO29"}
    assert min(g["month"] for g in d["groups"]) == "2024-01"
    assert all(g["n"] > 0 for g in d["groups"])
