"""The ICOS MCP server: ICOS data for AI assistants, over the zarr proxy.

A thin translator (docs/icos-mcp-plan.md).  Every number comes from the
proxy at ICOS_PROXY; this server adds no data path, only shapes answers
for a language model's context:

* data answers carry a few rows, the total, a download URL for the whole
  selection, and the citation, licence and DOI from the data passport;
* nothing large passes through: ``get_data`` caps rows, ``estimate``
  says how big a selection is before anything is fetched.

Phase 1 of the plan.  This server is also the ORACLE the M9 port is
replayed against, so its tool names, descriptions and answer shapes are
the contract: change them deliberately.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pyarrow import ipc
from pydantic import BaseModel, Field

PROXY = os.environ.get("ICOS_PROXY", "https://zarr.icos-cp.eu").rstrip("/")
VERSION = "0.1.0"

ROWS_DEFAULT = 100  # rows get_data returns unless asked
ROWS_MAX = 2000  # the most it ever returns: a context window, not a download
COUNT_MAX = 200_000  # largest selection estimate counts exactly through Arrow
CACHE_S = 600  # catalogue and store attributes, re-read after this

INSTRUCTIONS = """\
ICOS (Integrated Carbon Observation System) observations: atmospheric
greenhouse gases (CO2, CH4, N2O, CO, radon, 14CO2) and meteorology at
ICOS stations, surface-ocean CO2 (SOCAT and ICOS ocean), and ecosystem
fluxes (FLUXNET and near-real-time ETC data).

How to use these tools:
1. search_variables to find the variable id -- ids look like
   icos-obspack.co2.co2.  Prefer the primary measurement over its
   companions (std_dev, nvalue, flags).
2. estimate before get_data: it says how many rows a selection has.
3. get_data returns at most a few thousand rows.  For anything larger,
   give the user its download_url, or summarise from what you fetched
   and say so.
4. Always cite: every data answer carries the citation, licence (CC BY
   4.0) and DOI.  Pass them on to the user.
Station ids are CASE-SENSITIVE: atmosphere stations are a trigram plus
the sampling height (HTM150 = Hyltemossa, 150 m), FLUXNET sites are
mixed-case site codes (SE-Htm)."""

mcp = MCPServer(
    name="icos",
    title="ICOS carbon and greenhouse-gas observations",
    version=VERSION,
    website_url=PROXY,
    instructions=INSTRUCTIONS,
)

_clients: dict[int, httpx.AsyncClient] = {}
_cache: dict[str, tuple[float, Any]] = {}


def _http() -> httpx.AsyncClient:
    """One pooled client per event loop: a client is bound to the loop it
    first ran on, and a test runner or a host may run several."""
    loop = id(asyncio.get_running_loop())
    if loop not in _clients:
        _clients[loop] = httpx.AsyncClient(
            base_url=PROXY, timeout=120, headers={"User-Agent": f"icos-mcp/{VERSION}"}
        )
    return _clients[loop]


async def _get(path: str, params: dict | None = None) -> httpx.Response:
    try:
        r = await _http().get(path, params=params)
    except httpx.HTTPError as e:
        raise ToolError(f"the ICOS proxy did not answer: {e}") from e
    if r.status_code >= 400:
        raise ToolError(f"ICOS proxy {r.status_code} for {path}: {_reason(r)}")
    return r


def _reason(r: httpx.Response) -> str:
    try:
        d = r.json()
        return str(d.get("detail") or d.get("error") or d)[:500]
    except ValueError:
        return r.text[:500]


async def _cached_json(path: str) -> Any:
    hit = _cache.get(path)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    v = (await _get(path)).json()
    _cache[path] = (time.monotonic(), v)
    return v


async def _catalogue() -> list[dict]:
    return await _cached_json("/variables")


async def _variable(var_id: str) -> dict:
    for v in await _catalogue():
        if v["id"] == var_id:
            return v
    raise ToolError(f"no variable {var_id!r}; search_variables lists the ids")


async def _store_attrs(store: str) -> dict:
    return await _cached_json(f"/{store}/.zattrs")


# ---------------------------------------------------------------- answers


class Dataset(BaseModel):
    store: str
    title: str
    domain: str
    summary: str | None = None
    time_coverage_start: str | None = None
    time_coverage_end: str | None = None
    lat_min: float | None = None
    lat_max: float | None = None
    lon_min: float | None = None
    lon_max: float | None = None
    license: str | None = None
    doi: str | None = None
    n_variables: int = Field(
        description="variables queryable through estimate and "
        "get_data; 0 means the store is read with xarray only (see "
        "its agent guide)"
    )
    agent_guide_url: str
    croissant_url: str


class Datasets(BaseModel):
    datasets: list[Dataset]


class Variable(BaseModel):
    id: str
    name: str
    long_name: str | None = None
    unit: str | None = None
    domain: str | None = None
    store: str
    query_kind: str = Field(
        description="station_time (series at stations), obs_table "
        "(ship/cruise observations) or grid (gridded product)"
    )
    quality_flag: str | None = None
    vocabulary_uri: str | None = None


class Variables(BaseModel):
    variables: list[Variable]
    n_matches: int


class VariableDetail(Variable):
    dataset_title: str
    time_coverage_start: str | None = None
    time_coverage_end: str | None = None
    quality_control: str | None = Field(
        default=None,
        description="how the store's QC flag "
        "is read; apply_qc=true applies the recommended rule",
    )
    license: str | None = None
    doi: str | None = None


class Estimate(BaseModel):
    id: str
    rows: int | None = Field(
        description="rows the selection holds; None when only an upper bound is known"
    )
    rows_at_most: int | None = None
    exact: bool
    n_stations: int | None = None
    est_bytes: int | None = None
    time_min: str | None = None
    time_max: str | None = None
    note: str | None = None


class Citation(BaseModel):
    citation: str | None = None
    license: str | None = None
    license_url: str | None = None
    doi: str | None = None
    selection_id: str | None = Field(
        default=None,
        description="stable id of this exact selection over this exact store version",
    )
    store_version: str | None = None


class Data(BaseModel):
    id: str
    columns: list[str]
    rows: list[list[Any]]
    rows_returned: int
    rows_total: int | None = Field(
        description="rows in the whole selection, when known"
    )
    truncated: bool
    download_url: str = Field(
        description="the WHOLE selection as CSV (the proxy caps one "
        "answer at 500,000 rows); change format=csv to arrow or "
        "parquet for the binary twins"
    )
    citation: Citation


# ---------------------------------------------------------------- filters

Lat = Annotated[float | None, Field(ge=-90, le=90)]
Lon = Annotated[float | None, Field(ge=-180, le=180)]


def _filters(
    lat_min,
    lat_max,
    lon_min,
    lon_max,
    start,
    end,
    station,
    cruise,
    height_min,
    height_max,
    apply_qc,
) -> dict:
    p: dict[str, Any] = {}
    for k, v in (
        ("lat_min", lat_min),
        ("lat_max", lat_max),
        ("lon_min", lon_min),
        ("lon_max", lon_max),
        ("start", start),
        ("end", end),
        ("height_min", height_min),
        ("height_max", height_max),
    ):
        if v is not None:
            p[k] = v
    if station:
        p["station"] = ",".join(station)
    if cruise:
        p["cruise"] = ",".join(cruise)
    if apply_qc:
        p["apply_qc"] = "true"
    return p


# /summary narrows by the bounding box and time only; these it does not see
_NOT_SUMMARISED = ("station", "cruise", "height_min", "height_max", "apply_qc")


async def _count_arrow(params: dict) -> int:
    r = await _get("/query", {**params, "format": "arrow", "max_rows": COUNT_MAX + 1})
    with ipc.open_stream(io.BytesIO(r.content)) as rd:
        return sum(b.num_rows for b in rd)


async def _estimate(var_id: str, params: dict) -> Estimate:
    await _variable(var_id)  # an unknown id is named, not a proxy 404
    s = None
    try:
        s = (
            await _get(
                "/summary",
                {
                    "id": var_id,
                    **{k: params[k] for k in params if k not in _NOT_SUMMARISED},
                },
            )
        ).json()
    except ToolError:
        pass  # not served for this kind: count instead
    narrowed = any(k in params for k in _NOT_SUMMARISED)
    if s and not narrowed:
        return Estimate(
            id=var_id,
            rows=s.get("n_obs"),
            exact=True,
            n_stations=s.get("n_stations"),
            est_bytes=s.get("est_bytes"),
            time_min=s.get("time_min"),
            time_max=s.get("time_max"),
        )
    bound = s.get("n_obs") if s else None
    if bound is not None and bound > COUNT_MAX:
        return Estimate(
            id=var_id,
            rows=None,
            rows_at_most=bound,
            exact=False,
            n_stations=s.get("n_stations"),
            time_min=s.get("time_min"),
            time_max=s.get("time_max"),
            note="the station/cruise/height/QC filters are not counted "
            "server-side; this is the bound from the box and time alone",
        )
    n = await _count_arrow({"id": var_id, **params})
    if n > COUNT_MAX:
        return Estimate(
            id=var_id,
            rows=None,
            rows_at_most=None,
            exact=False,
            note=f"more than {COUNT_MAX:,} rows",
        )
    return Estimate(
        id=var_id,
        rows=n,
        exact=True,
        n_stations=None if narrowed else (s or {}).get("n_stations"),
    )


def _citation(passport: dict) -> Citation:
    node = next(
        (
            n
            for n in passport.get("@graph", [])
            if "icos:DataPassport" in (n.get("@type") or [])
        ),
        {},
    )
    return Citation(
        citation=node.get("citation"),
        license=node.get("license"),
        license_url=node.get("license_url"),
        doi=node.get("collection_doi"),
        selection_id=node.get("selectionId"),
        store_version=node.get("storeVersion"),
    )


async def _query_rows(params: dict) -> tuple[list[dict], dict]:
    r = await _get("/query", {**params, "format": "ndjson"})
    rows, passport = [], {}
    for line in r.text.splitlines():
        if not line:
            continue
        d = json.loads(line)
        if "_passport" in d:
            passport = d["_passport"]
        else:
            rows.append(d)
    return rows, passport


# ---------------------------------------------------------------- tools

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


@mcp.tool(annotations=READ_ONLY)
async def list_datasets() -> Datasets:
    """List the ICOS datasets: one per data store, with domain (atmosphere,
    ocean, ecosystem), time and space coverage, licence and DOI."""
    root = await _cached_json("/")
    cat = await _catalogue()
    out = []
    for store in root["stores"]:
        a = await _store_attrs(store)
        name = store.removesuffix(".zarr")
        out.append(
            Dataset(
                store=store,
                title=a.get("title", name),
                domain=a.get("icos_domain", ""),
                summary=a.get("summary"),
                time_coverage_start=a.get("time_coverage_start"),
                time_coverage_end=a.get("time_coverage_end"),
                lat_min=a.get("geospatial_lat_min"),
                lat_max=a.get("geospatial_lat_max"),
                lon_min=a.get("geospatial_lon_min"),
                lon_max=a.get("geospatial_lon_max"),
                license=a.get("license"),
                doi=a.get("collection_doi"),
                n_variables=sum(1 for v in cat if v.get("zarr_store") == store),
                agent_guide_url=f"{PROXY}/{store}/agent_guide",
                croissant_url=f"{PROXY}/{store}/croissant",
            )
        )
    return Datasets(datasets=out)


# a companion of a measurement, not the measurement: ranked last unless asked for
_COMPANION = re.compile(
    r"(^|_)(std|stdev|std_dev|nvalue|n_value|uncertainty|unc|flag|qc|"
    r"count|nobs|obs_flag|src|datalevel|icos_datalevel|sd|assimilation|concerns|ltr|smr|"
    r"original|era)($|_)",
    re.IGNORECASE,
)


# the catalogue names a gas by its formula (ch4) and a quantity by its
# ONEFlux or SOCAT code (NEE, fCO2); a person asks in words
_SYNONYMS = {
    "methane": "ch4",
    "carbon dioxide": "co2",
    "nitrous oxide": "n2o",
    "carbon monoxide": "co",
    "radon": "rn",
    "radiocarbon": "14co2",
    "temperature": "ta temp",
    "air temperature": "ta temp",
    "humidity": "rh",
    "relative humidity": "rh",
    "pressure": "pa pres",
    "wind speed": "ws",
    "wind direction": "wd",
    "precipitation": "p",
    "rain": "p",
    "net ecosystem exchange": "nee",
    "respiration": "reco",
    "ecosystem respiration": "reco",
    "photosynthesis": "gpp",
    "gross primary production": "gpp",
    "latent heat": "le",
    "sensible heat": "h",
    "evapotranspiration": "le",
    "fugacity": "fco2",
    "ocean co2": "fco2",
    "sea surface temperature": "sst",
    "salinity": "sal",
    "fluxes": "nee",
}


def _expand(text: str) -> str:
    t = text.lower()
    for phrase in sorted(_SYNONYMS, key=len, reverse=True):
        if phrase in t:
            t += " " + _SYNONYMS[phrase]
    return t


def _as_variable(v: dict) -> Variable:
    return Variable(
        id=v["id"],
        name=v.get("shortname") or v["id"].rsplit(".", 1)[-1],
        long_name=v.get("longname"),
        unit=v.get("unit"),
        domain=v.get("domain"),
        store=v.get("zarr_store", ""),
        query_kind=v.get("query_kind") or "",
        quality_flag=v.get("quality_flag_variable"),
        vocabulary_uri=v.get("iadopt_uri"),
    )


@mcp.tool(annotations=READ_ONLY)
async def search_variables(
    text: Annotated[
        str,
        Field(
            description="what to look for: a gas, a quantity or a name, "
            "e.g. 'co2', 'methane', 'net ecosystem exchange', 'fCO2'"
        ),
    ],
    domain: Annotated[
        str | None, Field(description="atmosphere, ocean or ecosystem")
    ] = None,
    store: Annotated[
        str | None, Field(description="limit to one store, e.g. icos-obspack.zarr")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 15,
) -> Variables:
    """Find variable ids in the ICOS catalogue by name, quantity or unit.
    The ids are what estimate and get_data take.  Primary measurements come
    before their companions (standard deviation, value counts, flags)."""
    words = [w for w in re.split(r"[^0-9a-z]+", _expand(text)) if w]
    ask_companion = bool(_COMPANION.search(text))
    scored = []
    for v in await _catalogue():
        if domain and v.get("domain") != domain:
            continue
        if store and v.get("zarr_store") != store:
            continue
        short = (v.get("shortname") or "").lower()
        long_ = (v.get("longname") or "").lower()
        vid = v["id"].lower()
        score = 0
        if short == text.lower().strip():
            score += 20
        for w in words:
            if w == short:
                score += 10
            elif w in short:
                score += 5
            if re.search(rf"\b{re.escape(w)}", long_):
                score += 4
            if w in vid:
                score += 2
            if w in (v.get("unit") or "").lower():
                score += 1
        if not score:
            continue
        if not ask_companion and _COMPANION.search(short):
            score -= 8
        scored.append((-score, v["id"], v))
    scored.sort(key=lambda t: (t[0], t[1]))
    return Variables(
        variables=[_as_variable(v) for _, _, v in scored[:limit]], n_matches=len(scored)
    )


@mcp.tool(annotations=READ_ONLY)
async def describe_variable(
    id: Annotated[str, Field(description="a variable id from search_variables")],
) -> VariableDetail:
    """One variable in full: names, unit, which dataset, its coverage, how its
    quality flag is read, licence and DOI."""
    v = await _variable(id)
    store = v.get("zarr_store", "")
    a = await _store_attrs(store)
    guide = a.get("agent_guide") or {}
    qc = (guide.get("conventions") or {}).get("quality_control")
    return VariableDetail(
        **_as_variable(v).model_dump(),
        dataset_title=a.get("title", store),
        time_coverage_start=a.get("time_coverage_start"),
        time_coverage_end=a.get("time_coverage_end"),
        quality_control=qc if isinstance(qc, str) else (json.dumps(qc) if qc else None),
        license=a.get("license"),
        doi=a.get("collection_doi"),
    )


StartT = Annotated[
    str | None, Field(description="ISO date or datetime, inclusive (2025-01-01)")
]
EndT = Annotated[str | None, Field(description="ISO date or datetime, EXCLUSIVE")]
Stations = Annotated[
    list[str] | None,
    Field(
        description="station ids, CASE-SENSITIVE: "
        "HTM150 (trigram + sampling height) for atmosphere, SE-Htm for "
        "FLUXNET sites"
    ),
]
Cruises = Annotated[
    list[str] | None,
    Field(description="cruise expocodes; ocean observation tables only"),
]
ApplyQc = Annotated[
    bool,
    Field(
        description="drop observations the store's recommended quality control rejects"
    ),
]


@mcp.tool(annotations=READ_ONLY)
async def estimate(
    id: Annotated[str, Field(description="a variable id from search_variables")],
    lat_min: Lat = None,
    lat_max: Lat = None,
    lon_min: Lon = None,
    lon_max: Lon = None,
    start: StartT = None,
    end: EndT = None,
    station: Stations = None,
    cruise: Cruises = None,
    height_min: float | None = None,
    height_max: float | None = None,
    apply_qc: ApplyQc = False,
) -> Estimate:
    """How big a selection is -- rows, stations, time span -- without fetching
    it.  Call this before get_data."""
    return await _estimate(
        id,
        _filters(
            lat_min,
            lat_max,
            lon_min,
            lon_max,
            start,
            end,
            station,
            cruise,
            height_min,
            height_max,
            apply_qc,
        ),
    )


@mcp.tool(annotations=READ_ONLY)
async def get_data(
    id: Annotated[str, Field(description="a variable id from search_variables")],
    lat_min: Lat = None,
    lat_max: Lat = None,
    lon_min: Lon = None,
    lon_max: Lon = None,
    start: StartT = None,
    end: EndT = None,
    station: Stations = None,
    cruise: Cruises = None,
    height_min: float | None = None,
    height_max: float | None = None,
    apply_qc: ApplyQc = False,
    max_rows: Annotated[
        int,
        Field(
            ge=1,
            le=ROWS_MAX,
            description="rows to return; the whole selection is at download_url",
        ),
    ] = ROWS_DEFAULT,
) -> Data:
    """Observations for one variable, filtered by box, time, station, cruise,
    height and quality control.  Returns at most max_rows rows as a table,
    the total, a download URL for the whole selection, and the citation the
    user must be given with any use of the data."""
    await _variable(id)
    params = {
        "id": id,
        **_filters(
            lat_min,
            lat_max,
            lon_min,
            lon_max,
            start,
            end,
            station,
            cruise,
            height_min,
            height_max,
            apply_qc,
        ),
    }
    rows, passport = await _query_rows({**params, "max_rows": max_rows + 1})
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    total: int | None = len(rows)
    if truncated:
        est = await _estimate(id, {k: v for k, v in params.items() if k != "id"})
        total = est.rows
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return Data(
        id=id,
        columns=cols,
        rows=[[r.get(c) for c in cols] for r in rows],
        rows_returned=len(rows),
        rows_total=total,
        truncated=truncated,
        download_url=f"{PROXY}/query?{urlencode({**params, 'format': 'csv'})}",
        citation=_citation(passport),
    )


@mcp.tool(annotations=READ_ONLY)
async def get_citation(
    id: Annotated[str, Field(description="a variable id from search_variables")],
) -> Citation:
    """How to cite a variable's data: the citation text, licence and DOI from
    its data passport."""
    await _variable(id)
    _, passport = await _query_rows({"id": id, "max_rows": 1})
    return _citation(passport)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="icos-mcp", description=__doc__.split("\n")[0])
    ap.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio"
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    a = ap.parse_args()
    if a.transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host=a.host, port=a.port, stateless_http=True)


if __name__ == "__main__":
    main()
