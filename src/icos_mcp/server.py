"""The ICOS MCP server: ICOS data for AI assistants, over the zarr proxy.

A thin translator (docs/icos-mcp-plan.md).  Every number comes from the
proxy (ICOS_PROXY, or the proxy of ICOS_MCP_CONFIG); this server adds no
data path, only shapes answers for a language model's context, and
offers only the stores access.py allows the caller -- default-deny:

* data answers carry a few rows, the total, a download URL for the whole
  selection, and the citation, licence and DOI from the data passport;
* nothing large passes through: ``get_data`` caps rows, ``estimate``
  says how big a selection is before anything is fetched.

Phases 1 and 2 of the plan.  This server is also the ORACLE the M9 port is
replayed against, so its tool names, descriptions and answer shapes are
the contract: change them deliberately.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import re
import time
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
import numcodecs
import numpy as np
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp_types import ToolAnnotations
from pyarrow import ipc
from pydantic import BaseModel, Field

from icos_mcp import access, cassette

POLICY = access.load()
PROXY = POLICY.proxy
VERSION = "0.2.0"

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
   find_stations lists the stations carrying a variable, near a place,
   in a box or by name -- use it to turn "Hyltemossa" into HTM150.
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
    website_url=POLICY.public_url,
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
            base_url=PROXY,
            timeout=120,
            headers={
                "User-Agent": f"icos-mcp/{VERSION}",
                **({"Host": POLICY.proxy_host} if POLICY.proxy_host else {}),
            },
            transport=cassette.transport(),
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


def _token(ctx: Context | None) -> str | None:
    """The caller's token.  A request that came over HTTP carries headers,
    and only its own `Authorization: Bearer` counts; a local caller (stdio,
    in-process) has none and is the server's own operator, whose token is
    ICOS_MCP_TOKEN.  The environment never speaks for an HTTP caller."""
    try:
        h = ctx.headers if ctx is not None else None
    except ValueError:
        h = None
    if h is not None:
        auth = h.get("authorization") or h.get("Authorization") or ""
        return auth[7:].strip() or None if auth.lower().startswith("bearer ") else None
    return os.environ.get("ICOS_MCP_TOKEN") or None


def _visible(store: str, tok: str | None) -> bool:
    return POLICY.allowed(store, tok)


async def _catalogue(tok: str | None) -> list[dict]:
    return [
        v
        for v in await _cached_json("/variables")
        if _visible(v.get("zarr_store", ""), tok)
    ]


async def _variable(var_id: str, tok: str | None) -> dict:
    for v in await _catalogue(tok):
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


async def _estimate(var_id: str, params: dict, tok: str | None) -> Estimate:
    await _variable(var_id, tok)  # an unknown id is named, not a proxy 404
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
async def list_datasets(ctx: Context) -> Datasets:
    """List the ICOS datasets: one per data store, with domain (atmosphere,
    ocean, ecosystem), time and space coverage, licence and DOI."""
    root = await _cached_json("/")
    tok = _token(ctx)
    cat = await _catalogue(tok)
    out = []
    for store in root["stores"]:
        if not _visible(store, tok):
            continue
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
                agent_guide_url=f"{POLICY.url_for(store)}/{store}/agent_guide",
                croissant_url=f"{POLICY.url_for(store)}/{store}/croissant",
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
    "air temperature": "ta at",
    "humidity": "rh",
    "relative humidity": "rh",
    "pressure": "pa pres ap",
    "air pressure": "pa ap",
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


_NRT = re.compile(
    r"\b(nrt|near[- ]real[- ]time|real[- ]time|latest|recent|today)\b", re.IGNORECASE
)
_NRT_STORES = {"icos-eco-nrt.zarr"}


def _concepts(text: str) -> list[tuple[str, ...]]:
    """The query as CONCEPTS, each a tuple of alternative spellings: a
    phrase the synonym table knows ("carbon dioxide") is ONE concept with
    its codes beside it (co2), so a long name that spells out both the
    words and the code is not counted twice."""
    t = text.lower()
    out: list[tuple[str, ...]] = []
    for phrase in sorted(_SYNONYMS, key=len, reverse=True):
        if phrase in t:
            out.append((phrase, *_SYNONYMS[phrase].split()))
            t = t.replace(phrase, " ")
    out.extend((w,) for w in re.split(r"[^0-9a-z]+", t) if w)
    return out


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
    ctx: Context | None = None,  # injected by the SDK, not a tool argument
) -> Variables:
    """Find variable ids in the ICOS catalogue by name, quantity or unit.
    The ids are what estimate and get_data take.  Primary measurements come
    before their companions (standard deviation, value counts, flags)."""
    concepts = _concepts(text)
    ask_companion = bool(_COMPANION.search(text))
    ask_nrt = bool(_NRT.search(text))
    scored = []
    for v in await _catalogue(_token(ctx)):
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
        unit = (v.get("unit") or "").lower()
        for alts in concepts:
            if short in alts:
                score += 10
            elif any(a in short for a in alts if " " not in a):
                score += 5
            if any(re.search(rf"\b{re.escape(a)}", long_) for a in alts):
                score += 4
            if any(a in vid for a in alts if " " not in a):
                score += 2
            if any(a in unit for a in alts if " " not in a):
                score += 1
        if not score:
            continue
        if not ask_companion and _COMPANION.search(short):
            score -= 8
        # a released, curated store answers a general question before
        # the near-real-time one, which is asked for by name
        if not ask_nrt and v.get("zarr_store") in _NRT_STORES:
            score -= 6
        scored.append((-score, v["id"], v))
    scored.sort(key=lambda t: (t[0], t[1]))
    return Variables(
        variables=[_as_variable(v) for _, _, v in scored[:limit]], n_matches=len(scored)
    )


@mcp.tool(annotations=READ_ONLY)
async def describe_variable(
    id: Annotated[str, Field(description="a variable id from search_variables")],
    ctx: Context | None = None,  # injected by the SDK, not a tool argument
) -> VariableDetail:
    """One variable in full: names, unit, which dataset, its coverage, how its
    quality flag is read, licence and DOI."""
    v = await _variable(id, _token(ctx))
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
    ctx: Context | None = None,  # injected by the SDK, not a tool argument
) -> Estimate:
    """How big a selection is -- rows, stations, time span -- without fetching
    it.  Call this before get_data."""
    tok = _token(ctx)
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
        tok,
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
    ctx: Context | None = None,  # injected by the SDK, not a tool argument
) -> Data:
    """Observations for one variable, filtered by box, time, station, cruise,
    height and quality control.  Returns at most max_rows rows as a table,
    the total, a download URL for the whole selection, and the citation the
    user must be given with any use of the data."""
    tok = _token(ctx)
    var = await _variable(id, tok)
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
        est = await _estimate(id, {k: v for k, v in params.items() if k != "id"}, tok)
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
        download_url=f"{POLICY.url_for(var['zarr_store'])}/query?"
        f"{urlencode({**params, 'format': 'csv'})}",
        citation=_citation(passport),
    )


@mcp.tool(annotations=READ_ONLY)
async def get_citation(
    id: Annotated[str, Field(description="a variable id from search_variables")],
    ctx: Context | None = None,  # injected by the SDK, not a tool argument
) -> Citation:
    """How to cite a variable's data: the citation text, licence and DOI from
    its data passport."""
    await _variable(id, _token(ctx))
    _, passport = await _query_rows({"id": id, "max_rows": 1})
    return _citation(passport)


# ---------------------------------------------------------------- stations


class Station(BaseModel):
    id: str = Field(description="the id station= takes, case-sensitive")
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    elevation: float | None = None
    sampling_height: float | None = Field(
        default=None, description="intake height above ground, m (atmosphere)"
    )
    country: str | None = None
    ecosystem: str | None = None
    url: str | None = None
    distance_km: float | None = None


class StationList(BaseModel):
    variable: str
    stations: list[Station]
    n_matches: int


# the per-station coordinates a panel lifts, by the names the stores use
_COORD = {
    "name": ("station_name", "site_name"),
    "latitude": ("latitude",),
    "longitude": ("longitude",),
    "elevation": ("elevation",),
    "sampling_height": ("sampling_height", "intake_height"),
    "country": ("country",),
    "ecosystem": ("ecosystem", "ecosystem_igbp"),
    "url": ("station_url", "source_download_url"),
}


async def _read_1d(base: str, meta: dict, name: str) -> list:
    """A small 1-D array of a panel, whole: its chunks fetched through the
    proxy and decoded with the codecs its .zarray names."""
    z = meta[f"{name}/.zarray"]
    n, c = z["shape"][0], z["chunks"][0]
    comp = numcodecs.get_codec(z["compressor"]) if z.get("compressor") else None
    filters = [numcodecs.get_codec(f) for f in (z.get("filters") or [])]
    out: list = []
    for k in range((n + c - 1) // c):
        raw = (await _get(f"{base}/{name}/{k}")).content
        if comp:
            raw = comp.decode(raw)
        for f in reversed(filters):
            raw = f.decode(raw)
        a = (
            raw
            if isinstance(raw, np.ndarray) and raw.dtype == object
            else np.frombuffer(raw, dtype=np.dtype(z["dtype"]))
        )
        out.extend(a[: min(c, n - k * c)].tolist())
    return out


async def _station_table(v: dict) -> list[dict]:
    if v.get("query_kind") != "station_time":
        raise ToolError(
            f"{v['id']} is a {v.get('query_kind')} variable: it has no station "
            "list; select by bounding box (lat_min..lon_max) or, for ocean "
            "observations, by cruise"
        )
    base = f"/{v['zarr_store']}/{v['group']}"
    hit = _cache.get(f"stations:{base}")
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    meta = (await _get(f"{base}/.zmetadata")).json()["metadata"]
    ids = await _read_1d(base, meta, "station")
    cols: dict[str, list] = {}
    for key, names in _COORD.items():
        for nm in names:
            if f"{nm}/.zarray" in meta:
                cols[key] = await _read_1d(base, meta, nm)
                break
    rows = []
    for i, sid in enumerate(ids):
        r = {"id": sid}
        for key, vals in cols.items():
            x = vals[i]
            if isinstance(x, float) and math.isnan(x):
                x = None
            r[key] = x or None if isinstance(x, str) else x
        rows.append(r)
    _cache[f"stations:{base}"] = (time.monotonic(), rows)
    return rows


def _km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return float(6371.0 * 2 * np.arcsin(np.sqrt(a)))


@mcp.tool(annotations=READ_ONLY)
async def find_stations(
    id: Annotated[
        str, Field(description="a station-based variable id from search_variables")
    ],
    near_lat: Annotated[
        float | None,
        Field(
            ge=-90,
            le=90,
            description="search around this point, with near_lon and radius_km",
        ),
    ] = None,
    near_lon: Annotated[float | None, Field(ge=-180, le=180)] = None,
    radius_km: Annotated[float, Field(gt=0, le=20000)] = 300,
    lat_min: Lat = None,
    lat_max: Lat = None,
    lon_min: Lon = None,
    lon_max: Lon = None,
    text: Annotated[
        str | None,
        Field(
            description="part of a station's id or name, e.g. "
            "'Hyltemossa', 'HTM', 'Hyy'"
        ),
    ] = None,
    country: Annotated[
        str | None, Field(description="two-letter code, e.g. SE")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ctx: Context | None = None,  # injected by the SDK, not a tool argument
) -> StationList:
    """The stations that carry a variable: id, name, position, sampling height,
    country, ecosystem -- near a point (sorted by distance), in a box, by name
    or by country.  The ids are what estimate and get_data take as station=."""
    rows = await _station_table(await _variable(id, _token(ctx)))
    out = []
    for r in rows:
        lat, lon = r.get("latitude"), r.get("longitude")
        if text and text.lower() not in f"{r['id']} {r.get('name') or ''}".lower():
            continue
        if country and (r.get("country") or "").upper() != country.upper():
            continue
        boxed = None not in (lat_min, lat_max, lon_min, lon_max)
        if boxed and (
            lat is None
            or lon is None
            or not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max)
        ):
            continue
        d = None
        if near_lat is not None and near_lon is not None:
            if lat is None or lon is None:
                continue
            d = round(_km(near_lat, near_lon, lat, lon), 1)
            if d > radius_km:
                continue
        out.append(Station(**r, distance_km=d))
    out.sort(key=lambda s: (s.distance_km if s.distance_km is not None else 0, s.id))
    return StationList(variable=id, stations=out[:limit], n_matches=len(out))


# ---------------------------------------------------------------- resources


def _store_name(store: str) -> str:
    return store if store.endswith(".zarr") else f"{store}.zarr"


def _visible_store(store: str, ctx: Context) -> str:
    name = _store_name(store)
    if not _visible(name, _token(ctx)):
        raise ResourceNotFoundError(f"no store {store!r}; list_datasets lists them")
    return name


@mcp.resource(
    "icos://stores/{store}/agent-guide",
    mime_type="application/json",
    description="how to open and read one ICOS store: layout, coordinates, QC, "
    "citation -- written for agents",
)
async def agent_guide(store: str, ctx: Context) -> str:
    return (await _get(f"/{_visible_store(store, ctx)}/agent_guide")).text


@mcp.resource(
    "icos://stores/{store}/croissant",
    mime_type="application/ld+json",
    description="the store's Croissant (ML dataset) description: every queryable "
    "variable with its unit",
)
async def croissant(store: str, ctx: Context) -> str:
    return (await _get(f"/{_visible_store(store, ctx)}/croissant")).text


@mcp.resource(
    "icos://catalogue",
    mime_type="application/json",
    description="every queryable variable of the public stores: id, names, unit, QC "
    "flag, store",
)
async def catalogue() -> str:
    # a static resource is not told who is asking (the SDK passes it no
    # context), so it lists the PUBLIC stores only; a token holder finds
    # the rest through search_variables
    return json.dumps(await _catalogue(None), ensure_ascii=False)


@mcp.resource(
    "icos://llms.txt",
    mime_type="text/plain",
    description="the service's own guide for language models",
)
async def llms_txt() -> str:
    return (await _get("/llms.txt")).text


@mcp.resource(
    "icos://data-passport",
    mime_type="text/markdown",
    description="what the data passport in every answer contains and how to cite",
)
async def data_passport() -> str:
    return (await _get("/data-passport.md")).text


# ---------------------------------------------------------------- prompts


@mcp.prompt(title="A station's record")
def station_record(
    quantity: str = "CO2",
    station: str = "Hyltemossa",
    period: str = "the last full year",
) -> str:
    """Plot or describe one station's time series."""
    return (
        f"Show {quantity} at {station} over {period}. Use search_variables to find the "
        f"variable, find_stations to get the station id, estimate the selection, then "
        f"get_data. If the selection is larger than get_data returns, summarise what "
        f"you fetched and give the download_url for the rest. Report the unit, the "
        f"quality-control choice (apply_qc), and the citation, licence and DOI."
    )


@mcp.prompt(title="Compare stations")
def compare_stations(
    quantity: str = "CH4",
    stations: str = "Hyltemossa, Norunda, Svartberget",
    period: str = "2025",
) -> str:
    """Compare one quantity across several stations."""
    return (
        f"Compare {quantity} at {stations} during {period}. Resolve each station with "
        f"find_stations, check sizes with estimate, fetch with get_data (one station= list "
        f"is fine), and compare like with like: same sampling height where it matters, "
        f"the same QC. Cite every dataset you used."
    )


@mcp.prompt(title="Ocean CO2 in a region")
def ocean_region(region: str = "the Baltic Sea", period: str = "2024") -> str:
    """What surface-ocean CO2 observations exist in a region."""
    return (
        f"What surface-ocean CO2 (fCO2) observations exist in {region} during {period}? "
        f"Use search_variables (ocean), estimate with a bounding box for the region, and "
        f"get_data for a sample. Say how many observations and cruises there are, give "
        f"the download_url for the full selection, and cite the data."
    )


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="icos-mcp", description=__doc__.split("\n")[0])
    ap.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio"
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument(
        "--hash-token",
        metavar="TOKEN",
        help="print the SHA-256 line a tokens_file takes for TOKEN, and exit",
    )
    ap.add_argument(
        "--show-policy",
        action="store_true",
        help="print which stores are offered, and to whom, and exit",
    )
    a = ap.parse_args()
    if a.hash_token:
        print(access.hash_token(a.hash_token))
        return
    if a.show_policy:
        print(
            f"proxy {POLICY.proxy}  Host {POLICY.proxy_host or '-'}  links {POLICY.public_url}"
        )
        for r in POLICY.rules:
            print(
                f"  {r.match:24s} {r.access:6s} {len(r.tokens)} token(s)  {r.public_url or ''}"
            )
        print("  anything else          hidden")
        return
    if a.transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host=a.host, port=a.port, stateless_http=True)


if __name__ == "__main__":
    main()
