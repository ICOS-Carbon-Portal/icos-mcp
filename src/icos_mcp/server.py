"""The ICOS MCP server: ICOS data for AI assistants, over the zarr proxy.

A thin translator (docs/plan.md).  Every number comes from the
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
import contextvars
import datetime as dt
import io
import json
import math
import os
import re
import sys
import time
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

import httpx
import numcodecs
import numpy as np
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.elicitation import ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp_types import ToolAnnotations
from pyarrow import ipc
from pydantic import BaseModel, Field

from icos_mcp import access, auth, cassette, landing, retro

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
3. For a mean, total, range or count, call aggregate -- it computes over
   the WHOLE selection on the server, by station, day, month or year.
   Never average rows from get_data yourself: get_data returns at most a
   few thousand rows, for looking at; for anything larger give the user
   its download_url.
4. The ecosystem fluxes NEE, GPP and RECO are served by /query as their
   PRESCRIBED reference slices -- NEE_VUT_REF (variable u* threshold,
   reference variant), GPP_NT_VUT_REF and RECO_NT_VUT_REF (night-time
   partitioning).  Use those.  The other variants (CUT, the u*
   percentiles, daytime partitioning) live in the zarr cubes NEE, GPP and
   RECO: search_variables lists them with access "zarr", and
   describe_variable gives the xarray line that reads any slice.
   Their UNITS differ by store: icos-fluxnet's daily/weekly/monthly/yearly
   NEE is gC m-2 d-1 (yearly gC m-2 y-1), the near-real-time icos-eco-nrt
   NEE is umol m-2 s-1 per half-hour -- say which when comparing.
5. The released stores (icos-fluxnet, icos-obspack releases) end months
   before today.  For the latest days or weeks -- "this week", "latest",
   "recent" -- use the near-real-time data: icos-eco-nrt for ecosystem
   fluxes and meteorology, the NRT tail of icos-obspack for gases.
6. Two ocean stores, easily confused: icos-socat is the ICOS OCEAN
   NETWORK's own release (ICOS ships and stations, ICOS Ocean Level 2,
   its own DOI) -- "ICOS ocean data" means this one; socat is the GLOBAL
   SOCAT synthesis (every contributor worldwide, SOCAT v2026, the SOCAT
   DOI) -- "SOCAT" means this one, and socat-gridded is its monthly 1x1
   degree grid.  Cite the store you actually used.
7. For a gridded product, estimate's rows ARE the grid cells with data:
   that number answers "how many cells", do not count them by hand.
8. Always cite: every data answer carries the citation, licence (CC BY
   4.0) and DOI.  Pass them on to the user.
Station ids are CASE-SENSITIVE: atmosphere stations are a trigram plus
the sampling height (HTM150 = Hyltemossa, 150 m), FLUXNET sites are
mixed-case site codes (SE-Htm)."""


QUESTION_NOTE = """
9. get_data and aggregate take an optional `question`: pass the user's request
   as they wrote it.  It is kept only if the user agreed, in the licence form,
   to share their questions anonymously to improve this service; otherwise it
   is dropped."""


def _auth_args() -> dict[str, Any]:
    """Sign-in required over HTTP when the config has an oauth section: the
    SDK then serves the protected-resource metadata naming Keycloak and
    answers /mcp without a valid token with 401 (auth.py)."""
    o = POLICY.oauth
    if o is None:
        return {}
    return {
        "auth": AuthSettings(
            issuer_url=o.issuer,
            resource_server_url=o.resource,
            required_scopes=[o.scope] if o.scope else None,
            # the verifier checks the audience itself
            validate_token_resource=False,
        ),
        "token_verifier": auth.Verifier(o, POLICY.store_tokens),
    }


mcp = MCPServer(
    name="icos",
    title="ICOS carbon and greenhouse-gas observations",
    version=VERSION,
    website_url=POLICY.public_url,
    instructions=INSTRUCTIONS + (QUESTION_NOTE if POLICY.questions_log else ""),
    **_auth_args(),
)

_clients: dict[int, httpx.AsyncClient] = {}
# the end user's address for the request in hand, set by _token from the
# headers caddy adds; passed to the proxy when the policy says so
_caller_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "caller_ip", default=None
)
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
        ip = _caller_ip.get() if POLICY.forward_client_ip else None
        r = await _http().get(
            path, params=params, headers={"X-Real-IP": ip} if ip else None
        )
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
        # caddy APPENDS the address it received the request from, so the
        # last X-Forwarded-For element is the one hop this server trusts
        xff = h.get("x-forwarded-for") or h.get("X-Forwarded-For") or ""
        _caller_ip.set(xff.split(",")[-1].strip() or None)
        auth = h.get("authorization") or h.get("Authorization") or ""
        return auth[7:].strip() or None if auth.lower().startswith("bearer ") else None
    return os.environ.get("ICOS_MCP_TOKEN") or None


def _visible(store: str, tok: str | None) -> bool:
    # groups only from the token the SDK verified for this request
    return POLICY.allowed(store, tok, auth.caller_groups())


# ---- the ICOS data licence, asked before data leaves (policy licence_gate) ----
#
# The metadata is CC0 and every metadata tool answers freely; the DATA is
# CC BY 4.0, and the portal asks for acceptance before a download.  So the
# two tools that hand over data values -- get_data and aggregate -- ask the
# USER, through MCP elicitation, which the client shows to the person and
# the model cannot answer for them.
#
# A client on protocol 2026-07-28 or later is asked by an input-required
# answer it retries, which needs no session.  An older client can be asked
# only over a connection that carries server-to-client requests -- a
# session -- and a stateless server refuses it with the licence named.

LICENCE_URL = "https://data.icos-cp.eu/licence"
# Clients may show the message on ONE line (Claude Code 2.1 cuts it), so the
# question and the link come first; the conditions ride on the checkbox.
LICENCE_ASK = "Accept the ICOS data licence (CC BY 4.0, {url}) to retrieve this data?"
LICENCE_TERMS = "Cite the data with its DOI, link the licence, and say if you changed it."


class LicenceAcceptance(BaseModel):
    accept: bool = Field(
        title="I accept the ICOS data licence (CC BY 4.0)", description=LICENCE_TERMS
    )


QUESTIONS_TERMS = "Optional. Kept: the question, its date, the data asked for -- no address or account. Only to improve the service."


class LicenceAndQuestions(LicenceAcceptance):
    """The form when the server keeps consented questions (questions_log)."""

    share_questions: bool = Field(
        default=False,
        title="Share my questions anonymously to help improve this service",
        description=QUESTIONS_TERMS,
    )


# Who has accepted, and when (epoch seconds): a signed-in caller (auth.user_id,
# a hash), an MCP session, a stdio connection -- or, for an anonymous caller on
# the stateless protocol (2026-07-28, which has no session at all), its address
# and client name, for ANON_TTL.  That last key is a convenience, not an
# identity: two people behind one address with the same client share it, which
# for a licence acknowledgement (not an access control) is the right trade
# against asking on every call.  Kept in memory: a restart asks again.
_licence_seen: dict[str, float] = {}
# who of them agreed to share their questions (questions_log), same keys
_questions_ok: dict[str, bool] = {}
ANON_TTL = 12 * 3600
_STDIO = False  # main() sets it: a stdio connection is one user
# A question on screen, per key: calls arriving while it is unanswered WAIT for
# that answer instead of asking again -- an agent fires several data calls at
# once, and on the stateless protocol each one would otherwise put its own form.
_licence_asking: dict[str, tuple[float, asyncio.Future]] = {}
ASK_WAIT = 90  # seconds a call waits for another call's question


def _licence_key(ctx: Context | None) -> str | None:
    uid = auth.user_id(get_access_token())
    if uid:
        return "user " + uid
    try:
        h = ctx.headers if ctx is not None else None
    except ValueError:
        h = None
    if h is None:
        # stdio: one process, one user, for the life of the connection
        return "stdio" if _STDIO else None
    sid = h.get("mcp-session-id")
    if sid:
        return "session " + sid
    # stateless and anonymous: the caller's address (as the proxy in front
    # forwards it) and the client's own name
    ip = (h.get("x-real-ip") or h.get("x-forwarded-for", "").split(",")[0]).strip()
    if not ip:
        try:
            ip = ctx.request_context.request.client.host
        except AttributeError:
            ip = ""
    try:
        p = ctx.session.client_params
        name = p.client_info.name if p is not None and p.client_info is not None else ""
    except (AttributeError, ValueError):
        name = ""
    return f"anonymous {ip} {name}".rstrip() if ip else None


def _accepted_by(key: str | None) -> bool:
    if key is None or key not in _licence_seen:
        return False
    if key.startswith("anonymous ") and time.time() - _licence_seen[key] > ANON_TTL:
        del _licence_seen[key]
        return False
    return True


def _answering(ctx: Context) -> bool:
    """This is the round that brings the user's answer back (stateless protocol)."""
    return bool(getattr(ctx, "input_responses", None))


def _client(ctx: Context | None) -> str:
    """The client's own name for itself, for the log -- never for a decision."""
    try:
        p = ctx.session.client_params if ctx is not None else None
        i = p.client_info if p is not None else None
        who = f"{i.name} {i.version}" if i is not None else "unnamed client"
        return f"{who}, protocol {ctx.protocol_version or 'unknown'}"
    except (AttributeError, ValueError):
        return "unnamed client"


def _log(msg: str) -> None:
    print(f"{dt.datetime.now(dt.UTC):%Y-%m-%dT%H:%M:%SZ} icos-mcp: {msg}", file=sys.stderr, flush=True)


def _licence_refusal(why: str) -> ToolError:
    return ToolError(
        "This tool returns ICOS data, which may be retrieved only after the user "
        f"accepts the ICOS data licence (CC BY 4.0, {LICENCE_URL}). {why} "
        "The metadata tools -- search_variables, describe_variable, "
        "find_stations, estimate and get_citation -- need no acceptance."
    )


async def _licence(ctx: Context) -> LicenceAcceptance | Elicit[LicenceAcceptance]:
    """Resolver of the data tools' licence parameter: nothing to ask when the
    gate is off or this caller has accepted already, a refusal when the
    question cannot reach the user, otherwise the question."""
    if not POLICY.licence_gate:
        return LicenceAcceptance(accept=True)
    key = _licence_key(ctx)
    if _accepted_by(key):
        return LicenceAcceptance(accept=True)
    if key is not None and not _answering(ctx) and key in _licence_asking:
        since, fut = _licence_asking[key]
        if time.time() - since < ASK_WAIT:
            try:
                ok = await asyncio.wait_for(asyncio.shield(fut), ASK_WAIT - (time.time() - since))
            except asyncio.TimeoutError:
                ok = None   # unanswered: ask this call's own question below
            if ok is True:
                return LicenceAcceptance(accept=True)
            if ok is False:
                raise _licence_refusal("The user did not accept it.")
    modern = (ctx.protocol_version or "") >= "2026-07-28"
    if not modern and not ctx.session.can_send_request:
        raise _licence_refusal(
            f"This connection (protocol {ctx.protocol_version or 'unknown'}, no "
            "session) cannot carry the question to the user."
        )
    caps = ctx.client_capabilities
    form = caps.elicitation if caps is not None else None
    if form is None or (form.form is None and form.url is not None):
        _log(f"licence refused, {_client(ctx)} declares no form elicitation")
        raise _licence_refusal("This client cannot ask its user (no form elicitation).")
    if key is not None and not _answering(ctx):
        since, fut = _licence_asking.get(key, (0.0, None))
        if fut is None or fut.done() or time.time() - since >= ASK_WAIT:
            _licence_asking[key] = (time.time(), asyncio.get_running_loop().create_future())
        _log(f"asking the ICOS data licence of {_client(ctx)} ({key})")
    schema = LicenceAndQuestions if POLICY.questions_log else LicenceAcceptance
    return Elicit(LICENCE_ASK.format(url=LICENCE_URL), schema)


Question = Annotated[
    str | None,
    Field(
        description="the user's request as they wrote it; kept only if the user "
        "agreed in the licence form to share their questions anonymously, "
        "otherwise dropped"
    ),
]


def _keep_question(ctx: Context | None, tool: str, question: str | None, sel: dict) -> None:
    """One JSON line for a question the user agreed to share: the date (not the
    time), the question, the tool, the selection, the client's name -- no
    address, account, token or session key."""
    if not (POLICY.questions_log and question and question.strip()):
        return
    if not _questions_ok.get(_licence_key(ctx) or ""):
        return
    try:
        p = ctx.session.client_params if ctx is not None else None
        client = p.client_info.name if p is not None and p.client_info is not None else ""
    except (AttributeError, ValueError):
        client = ""
    line = {"date": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d"), "question": question.strip()[:2000],
            "tool": tool, "selection": {k: v for k, v in sel.items() if v not in (None, "", [])},
            "client": client}
    with open(POLICY.questions_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _licensed(
    outcome: ElicitationResult[LicenceAcceptance], ctx: Context | None, tool: str
) -> None:
    """Refuse unless the user accepted; remember and log an acceptance, and
    release the calls waiting on this question either way."""
    yes = outcome.action == "accept" and bool(outcome.data and outcome.data.accept)
    key = _licence_key(ctx) if POLICY.licence_gate else None
    if key is not None and key in _licence_asking:
        _, fut = _licence_asking.pop(key)
        if not fut.done():
            fut.set_result(yes)
    if not yes:
        _log(f"licence not accepted by {_client(ctx)} ({outcome.action})")
        raise _licence_refusal("The user did not accept it.")
    if POLICY.licence_gate:
        if not _accepted_by(key):
            if key is not None:
                _licence_seen[key] = time.time()
            share = bool(getattr(outcome.data, "share_questions", False))
            if key is not None:
                _questions_ok[key] = share
            _log(f"ICOS data licence accepted ({key or 'anonymous, this call'}) for {tool}, "
                 f"{_client(ctx)}" + (", questions shared" if share else ""))


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
    for z in await _zarr_variants(tok):
        if z["id"] == var_id:
            raise ToolError(
                f"{var_id} is a {len(z['dims'])}-dimensional variable "
                f"({', '.join(z['dims'])}) read through zarr, not served by this tool. "
                + (
                    f"Its PRESCRIBED slice is {z['prescribed']} -- use that id here. "
                    if z["prescribed"]
                    else ""
                )
                + f"Any other slice: {z['recipe']}"
            )
    raise ToolError(f"no variable {var_id!r}; search_variables lists the ids")


# ---------------------------------------------------------------- zarr-only variables

# the preferred label on each ONEFlux axis: variable u* threshold, the
# reference variant, night-time partitioning -- the slice /query serves
# as <VAR>_VUT_REF / <VAR>_NT_VUT_REF (Alex, 2026-10-30: "prescribe the
# preferred version"; the other variants stay findable and readable)
_PREFERRED_LABEL = {
    "ustar_threshold": "VUT",
    "nee_variant": "REF",
    "partition_method": "NT",
}


def _var_id(store: str, group: str, name: str) -> str:
    return f"{store.removesuffix('.zarr')}.{group.replace('/', '-').strip('-')}.{name}"


async def _zarr_variants(tok: str | None) -> list[dict]:
    """The variables a station panel holds beyond (station, time) -- the
    ONEFlux cubes, NEE over ustar_threshold x nee_variant and the like --
    which /query does not serve and the catalogue therefore does not list.
    Read from each visible panel's own index, with their axis labels."""
    cat = await _catalogue(tok)
    ids = {v["id"] for v in cat}
    domain = {v["zarr_store"]: v.get("domain") for v in cat}
    groups = sorted(
        {
            (v["zarr_store"], v["group"])
            for v in cat
            if v.get("query_kind") == "station_time"
        }
    )
    out = []
    for store, group in groups:
        base = f"/{store}/{group}"
        hit = _cache.get(f"variants:{base}")
        if hit and time.monotonic() - hit[0] < CACHE_S:
            rows = hit[1]
        else:
            meta = (await _get(f"{base}/.zmetadata")).json()["metadata"]
            rows = []
            for k in meta:
                if not k.endswith("/.zarray") or k.count("/") != 1:
                    continue
                name = k[:-8]
                at = meta.get(f"{name}/.zattrs", {})
                dims = at.get("_ARRAY_DIMENSIONS", [])
                if (
                    len(dims) <= 2
                    or dims[0] != "station"
                    or not dims[1].startswith("time")
                ):
                    continue
                axes = {
                    d: (
                        [str(x) for x in await _read_1d(base, meta, d)]
                        if f"{d}/.zarray" in meta
                        else []
                    )
                    for d in dims[2:]
                }
                rows.append(
                    {
                        "name": name,
                        "long_name": at.get("long_name"),
                        "unit": at.get("units"),
                        "dims": dims,
                        "axes": axes,
                    }
                )
            _cache[f"variants:{base}"] = (time.monotonic(), rows)
        for r in rows:
            vid = _var_id(store, group, r["name"])
            sel = ", ".join(
                f"{d}={(_PREFERRED_LABEL.get(d) if _PREFERRED_LABEL.get(d) in labs else (labs or [''])[0])!r}"
                for d, labs in r["axes"].items()
            )
            recipe = (
                f'xr.open_zarr("{POLICY.url_for(store)}/{store}", group="{group}", '
                f'consolidated=True)["{r["name"]}"].sel({sel})'
            )
            prescribed = next(
                (i for i in (f"{vid}_VUT_REF", f"{vid}_NT_VUT_REF") if i in ids), None
            )
            out.append(
                {
                    **r,
                    "id": vid,
                    "store": store,
                    "group": group,
                    "domain": domain.get(store),
                    "prescribed": prescribed,
                    "recipe": recipe,
                }
            )
    return out


def _as_zarr_variable(z: dict) -> Variable:
    return Variable(
        id=z["id"],
        name=z["name"],
        long_name=z.get("long_name"),
        unit=z.get("unit"),
        domain=z.get("domain"),
        store=z["store"],
        query_kind="station_time",
        access="zarr",
        axes=z["axes"],
        prescribed=z["prescribed"],
        zarr_recipe=z["recipe"],
    )


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
    access: Literal["query", "zarr"] = Field(
        default="query",
        description="query: estimate/get_data/aggregate serve it; zarr: a "
        "multi-dimensional variable (e.g. the ONEFlux NEE cube) read with xarray -- "
        "see zarr_recipe, and `prescribed` for its /query slice",
    )
    axes: dict[str, list[str]] | None = Field(
        default=None, description="zarr variables: each extra axis and its labels"
    )
    prescribed: str | None = Field(
        default=None,
        description="zarr variables: the id of the preferred slice /query serves",
    )
    zarr_recipe: str | None = None


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
    variants: list[str] | None = Field(
        default=None,
        description="the multi-dimensional variables this one is the PRESCRIBED "
        "slice of; their other slices are read through zarr (describe_variable on one "
        "gives the recipe)",
    )


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
    """In data answers the author list is shortened (first author et al.);
    get_citation gives the full citation."""

    citation: str | None = None
    license: str | None = None
    license_url: str | None = None
    doi: str | None = None
    selection_id: str | None = Field(
        default=None,
        description="stable id of this exact selection over this exact store version",
    )
    store_version: str | None = None
    n_sources: int | None = Field(
        default=None,
        description="how many data objects the selection came from; in a data "
        "answer their citations are listed only when there are three or fewer -- "
        "get_citation (with the same stations) lists them all",
    )
    sources: list[str] | None = Field(
        default=None,
        description="the citation of each data object the selection came from -- "
        "what to cite when there is no collection citation (near-real-time data)",
    )


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
        description="the selection as CSV when it fits one download; when it does "
        "not, the FIRST of download_urls"
    )
    download_urls: list[str] | None = Field(
        default=None,
        description="when the selection is larger than one download (the proxy "
        "caps an answer at 500,000 rows): one CSV link per time window, together "
        "the WHOLE selection -- give the user all of them",
    )
    download_complete: bool = Field(
        default=True,
        description="false when no set of links could be planned to cover the "
        "whole selection; the note says what is missing",
    )
    note: str | None = None
    citation: Citation


# ---------------------------------------------------------------- filters

Lat = Annotated[float | None, Field(ge=-90, le=90)]
Lon = Annotated[float | None, Field(ge=-180, le=180)]


def _ids(v: list[str] | str) -> list[str]:
    """A list of ids, or one id / a comma-separated string of them."""
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return list(v)


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
        p["station"] = ",".join(_ids(station))
    if cruise:
        p["cruise"] = ",".join(_ids(cruise))
    if apply_qc:
        p["apply_qc"] = "true"
    return p


# /summary narrows by the bounding box and time only; these it does not see
_NOT_SUMMARISED = ("station", "cruise", "height_min", "height_max", "apply_qc")


async def _count_arrow(params: dict) -> tuple[int, str | None, str | None, int | None]:
    """Rows, time span and stations of a selection, counted from its Arrow
    answer (up to the cap)."""
    r = await _get(
        "/query", {**params, "format": "arrow", "max_rows": AGG_ROWS_MAX + 1}
    )
    with ipc.open_stream(io.BytesIO(r.content)) as rd:
        tab = rd.read_all()
    t_min = t_max = None
    tcol = next((c for c in tab.column_names if c.startswith("time")), None)
    if tab.num_rows and tcol:
        t = tab.column(tcol).to_numpy(zero_copy_only=False).astype("datetime64[s]")
        t_min, t_max = f"{t.min()}Z", f"{t.max()}Z"
    n_st = (
        len(set(tab.column("station").to_pylist()))
        if tab.num_rows and "station" in tab.column_names
        else None
    )
    return tab.num_rows, t_min, t_max, n_st


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
            note=(
                f"a GRID: rows are the {s['n_cells']} grid cells with data, over "
                f"{s.get('n_timesteps')} time steps"
                if "n_cells" in s
                else None
            ),
            rows=s.get("n_obs", s.get("n_cells")),
            exact=True,
            n_stations=s.get("n_stations"),
            est_bytes=s.get("est_bytes"),
            time_min=s.get("time_min"),
            time_max=s.get("time_max"),
        )
    bound = s.get("n_obs") if s else None
    # a station or cruise selection is SMALL whatever the box says -- count
    # it; only an unselective one too big to count gets the bound
    selective = "station" in params or "cruise" in params
    if bound is not None and bound > COUNT_MAX and not selective:
        return Estimate(
            id=var_id,
            rows=None,
            rows_at_most=bound,
            exact=False,
            n_stations=s.get("n_stations"),
            time_min=s.get("time_min"),
            time_max=s.get("time_max"),
            note="an upper bound from the box and time alone (quality control and "
            "height are not counted server-side); aggregate counts it exactly",
        )
    n, t_min, t_max, n_st = await _count_arrow({"id": var_id, **params})
    if n > AGG_ROWS_MAX:
        return Estimate(
            id=var_id,
            rows=None,
            rows_at_most=None,
            exact=False,
            note=f"more than {AGG_ROWS_MAX:,} rows",
        )
    note = await _empty_note(var_id, params) if n == 0 else None
    return Estimate(
        id=var_id,
        rows=n,
        exact=True,
        n_stations=n_st,
        time_min=t_min,
        time_max=t_max,
        note=note,
    )


async def _empty_note(var_id: str, params: dict) -> str:
    """Why a selection is empty: the record of the same selection without its
    time window -- so an agent asking for 1990 learns the record starts in
    2017 instead of widening the window until it runs out of turns."""
    rest = {k: v for k, v in params.items() if k not in ("start", "end")}
    if len(rest) == len(params):
        return "no rows: nothing matches these filters at any time"
    r = await _get(
        "/query", {"id": var_id, **rest, "format": "arrow", "max_rows": COUNT_MAX + 1}
    )
    with ipc.open_stream(io.BytesIO(r.content)) as rd:
        tab = rd.read_all()
    tcol = next((c for c in tab.column_names if c.startswith("time")), None)
    if tab.num_rows == 0 or tcol is None:
        return "no rows: nothing matches these filters at any time"
    t = tab.column(tcol).to_numpy(zero_copy_only=False).astype("datetime64[s]")
    return (
        f"no rows in this time window; without it the same selection runs "
        f"{str(t.min())[:10]} to {str(t.max())[:10]}"
        + (" (at least -- capped)" if tab.num_rows > COUNT_MAX else "")
    )


_YEAR = re.compile(r"[ ,(]\(?((?:19|20)\d\d)\)?[.,]")


def _short(cit: str | None) -> str | None:
    """The first author, et al., and everything from the year on (title,
    publisher, DOI): an ICOS author list runs to 5 KB, and a data answer
    repeats it on every call.  get_citation gives the whole text."""
    if not cit or len(cit) <= 300:
        return cit
    m = _YEAR.search(cit)
    if not m:
        return cit
    authors = cit[: m.start()].split("., ")
    if len(authors) <= 3:
        return cit
    return f"{authors[0]}. et al.{cit[m.start() :]}"


def _citation(passport: dict, short: bool = False) -> Citation:
    node = next(
        (
            n
            for n in passport.get("@graph", [])
            if "icos:DataPassport" in (n.get("@type") or [])
        ),
        {},
    )
    # every data object the selection came from: its citation, or -- an
    # obspack source carries none -- its identifier
    objs = [
        n.get("citation") or n.get("@id")
        for n in passport.get("@graph", [])
        if isinstance(n.get("@type"), list)
        and "icos:DataObject" in n["@type"]
        and (n.get("citation") or n.get("@id"))
    ]
    n = len(objs) or None
    if short:
        # a 137-station selection cites 164 objects in 40 KB: in a data
        # answer, the count and a pointer (an external test, 2026-10-30)
        objs = [_short(o) for o in objs] if len(objs) <= 3 else []
    return Citation(
        n_sources=n,
        sources=objs or None,
        citation=_short(node.get("citation")) if short else node.get("citation"),
        license=node.get("license"),
        license_url=node.get("license_url"),
        doi=node.get("collection_doi"),
        selection_id=node.get("selectionId"),
        store_version=node.get("storeVersion"),
    )


async def _cite(
    var_id: str, stations: list[str] | None, short: bool = False
) -> Citation:
    """The citation of a selection.  A store with a collection citation
    answers it from one row.  Near-real-time data cite each data OBJECT, and
    a TRUNCATED panel answer drops those from its passport (the transcribed
    reference behaviour: the survivors are recorded after the loop the cap
    breaks), so the newest days are asked for whole."""
    base = {"id": var_id, **({"station": ",".join(stations)} if stations else {})}
    _, passport = await _query_rows({**base, "max_rows": 1})
    c = _citation(passport, short)
    if not c.citation and not c.sources:
        recent = str(dt.datetime.now(dt.UTC).date() - dt.timedelta(days=3))
        _, passport = await _query_rows({**base, "start": recent, "max_rows": 100_000})
        c = _citation(passport, short)
    return c


DL_WINDOWS_MAX = 64


def _link(store: str, params: dict) -> str:
    return f"{POLICY.url_for(store)}/query?{urlencode({**params, 'format': 'csv'})}"


async def _download_links(
    store: str, var_id: str, params: dict, rows: int | None
) -> tuple[list[str], bool, str | None]:
    """CSV links that together hold the WHOLE selection.  The proxy cuts one
    answer at 500,000 rows (and says so in X-Data-Truncated), so a larger
    selection is split by time: each window is halved until /summary's
    count for it -- an UPPER bound, it ignores the station and QC filters --
    fits, so every link is whole.  (An external test, 2026-10-30: the
    full-year link stopped at 500,000 rows with no warning, and the mean
    recomputed from it was 427.58 where the selection's is 426.44.)"""
    if rows is not None and rows <= AGG_ROWS_MAX:
        return [_link(store, params)], True, None
    lo, hi = await _window(var_id, params)
    if lo is None or hi is None:
        return (
            [_link(store, params)],
            False,
            (
                f"the selection is larger than one download ({AGG_ROWS_MAX:,} rows) and "
                "could not be split by time here: the link holds its first "
                f"{AGG_ROWS_MAX:,} rows only"
            ),
        )
    todo, out = [(lo, hi)], []
    while todo:
        a, b = todo.pop(0)
        try:
            s = (
                await _get(
                    "/summary",
                    {
                        "id": var_id,
                        **{
                            k: v
                            for k, v in params.items()
                            if k not in _NOT_SUMMARISED and k not in ("start", "end")
                        },
                        "start": str(a),
                        "end": str(b),
                    },
                )
            ).json()
            n = s.get("n_obs", s.get("n_cells"))
        except ToolError:
            n = None
        if (n is not None and n <= AGG_ROWS_MAX) or b - a < np.timedelta64(2, "h"):
            out.append((a, b))
        elif len(out) + len(todo) + 2 > DL_WINDOWS_MAX:
            return (
                [_link(store, params)],
                False,
                (
                    f"the selection needs more than {DL_WINDOWS_MAX} downloads of "
                    f"{AGG_ROWS_MAX:,} rows: narrow it"
                ),
            )
        else:
            mid = a + (b - a) // 2
            todo[:0] = [(a, mid), (mid, b)]
    out.sort()
    links = [_link(store, {**params, "start": str(a), "end": str(b)}) for a, b in out]
    return (
        links,
        True,
        (
            f"the selection is larger than one download ({AGG_ROWS_MAX:,} rows): "
            f"{len(links)} links, one per time window, together hold all of it"
        ),
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
    tok = _token(ctx)
    # the catalogue's /query variables and, beside them, the zarr-only ones
    cands = [
        (
            v,
            v.get("shortname") or "",
            v.get("longname") or "",
            v.get("zarr_store"),
            v.get("unit") or "",
            "query",
        )
        for v in await _catalogue(tok)
    ]
    cands += [
        (
            z,
            z["name"],
            z.get("long_name") or "",
            z["store"],
            z.get("unit") or "",
            "zarr",
        )
        for z in await _zarr_variants(tok)
    ]
    score_of: dict[str, int] = {}
    for v, short, long_, st, unit, kind in cands:
        if domain and v.get("domain") != domain:
            continue
        if store and st != store:
            continue
        short, long_, vid, unit = (
            short.lower(),
            long_.lower(),
            v["id"].lower(),
            unit.lower(),
        )
        score = 0
        if short == text.lower().strip():
            score += 20
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
        if not ask_nrt and st in _NRT_STORES:
            score -= 6
        score_of[v["id"]] = score
    # a zarr-only cube ranks just BELOW the slice prescribed for it: asked
    # for "NEE", the answer is NEE_VUT_REF first and the cube next to it
    by_id = {c[0]["id"]: c for c in cands}
    for v, *_, kind in cands:
        if kind == "zarr" and v["id"] in score_of and v.get("prescribed") in by_id:
            p_ = v["prescribed"]
            score_of[p_] = max(score_of.get(p_, 0), score_of[v["id"]] + 1)
    ranked = sorted(score_of, key=lambda i: (-score_of[i], i))
    variables = [
        _as_variable(by_id[i][0])
        if by_id[i][5] == "query"
        else _as_zarr_variable(by_id[i][0])
        for i in ranked[:limit]
    ]
    return Variables(variables=variables, n_matches=len(ranked))


@mcp.tool(annotations=READ_ONLY)
async def describe_variable(
    id: Annotated[str, Field(description="a variable id from search_variables")],
    ctx: Context | None = None,  # injected by the SDK, not a tool argument
) -> VariableDetail:
    """One variable in full: names, unit, which dataset, its coverage, how its
    quality flag is read, licence and DOI.  For a multi-dimensional variable
    read through zarr (access 'zarr'), its axes with their labels, the slice
    /query serves (prescribed) and an xarray line for any other slice."""
    tok = _token(ctx)
    zs = await _zarr_variants(tok)
    z = next((z for z in zs if z["id"] == id), None)
    if z is not None:
        a = await _store_attrs(z["store"])
        return VariableDetail(
            **_as_zarr_variable(z).model_dump(),
            dataset_title=a.get("title", z["store"]),
            time_coverage_start=a.get("time_coverage_start"),
            time_coverage_end=a.get("time_coverage_end"),
            license=a.get("license"),
            doi=a.get("collection_doi"),
        )
    v = await _variable(id, tok)
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
        variants=[z["id"] for z in zs if z["prescribed"] == id] or None,
    )


StartT = Annotated[
    str | None, Field(description="ISO date or datetime, inclusive (2025-01-01)")
]
EndT = Annotated[str | None, Field(description="ISO date or datetime, EXCLUSIVE")]
Stations = Annotated[
    list[str] | str | None,
    Field(
        description="station ids -- a list, or one id (or several, comma-separated) "
        "as a string; CASE-SENSITIVE: HTM150 (trigram + sampling height) for "
        "atmosphere, SE-Htm for FLUXNET sites"
    ),
]
Cruises = Annotated[
    list[str] | str | None,
    Field(
        description="cruise expocodes, a list or a string; ocean observation "
        "tables only"
    ),
]
ApplyQc = Annotated[
    bool,
    Field(
        description="DEFAULT TRUE: drop the observations the store's recommended "
        "quality control rejects; false keeps every observation, flagged ones included"
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
    apply_qc: ApplyQc = True,
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
    licence: Annotated[
        ElicitationResult[LicenceAcceptance], Resolve(_licence)
    ],  # asked of the USER when the policy gates data, never an argument
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
    apply_qc: ApplyQc = True,
    max_rows: Annotated[
        int,
        Field(
            ge=1,
            le=ROWS_MAX,
            description="rows to return; the whole selection is at download_url",
        ),
    ] = ROWS_DEFAULT,
    question: Question = None,
    ctx: Context | None = None,  # injected by the SDK, not a tool argument
) -> Data:
    """Observations for one variable, filtered by box, time, station, cruise,
    height and quality control.  Returns at most max_rows rows as a table,
    the total, a download URL for the whole selection, and the citation the
    user must be given with any use of the data."""
    _licensed(licence, ctx, "get_data")
    _keep_question(ctx, "get_data", question, {"id": id, "station": station, "cruise": cruise,
                   "start": start, "end": end, "lat_min": lat_min, "lat_max": lat_max,
                   "lon_min": lon_min, "lon_max": lon_max})
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
    cite = _citation(passport, short=True)
    if not cite.citation and not cite.sources and not cite.n_sources:
        cite = await _cite(id, _ids(station) if station else None, short=True)
    links, complete, note = await _download_links(
        var["zarr_store"], id, params, total if truncated else len(rows)
    )
    return Data(
        id=id,
        columns=cols,
        rows=[[r.get(c) for c in cols] for r in rows],
        rows_returned=len(rows),
        rows_total=total,
        truncated=truncated,
        download_url=links[0],
        download_urls=links if len(links) > 1 else None,
        download_complete=complete,
        note=note,
        citation=cite,
    )


@mcp.tool(annotations=READ_ONLY)
async def get_citation(
    id: Annotated[str, Field(description="a variable id from search_variables")],
    station: Stations = None,
    start: StartT = None,
    end: EndT = None,
    lat_min: Lat = None,
    lat_max: Lat = None,
    lon_min: Lon = None,
    lon_max: Lon = None,
    ctx: Context | None = None,  # injected by the SDK, not a tool argument
) -> Citation:
    """How to cite a variable's data: the citation text, licence and DOI from
    its data passport, and -- for a selection (stations, a time window, a
    box) -- every data object it came from, in full.  Data answers carry a
    short citation and only the COUNT of their sources; this lists them."""
    var = await _variable(id, _token(ctx))
    if not any((station, start, end, lat_min, lat_max, lon_min, lon_max)):
        return await _cite(id, None)
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
            None,
            None,
            None,
            False,
        ),
    }
    state = {"rows": 0, "parts": 0, "passport": {}, "windows": []}
    name = var.get("shortname") or id.rsplit(".", 1)[-1]
    await _aggregate_parts(id, name, "none", params, {}, [], state, depth=0)
    return _citation(_with_objects(state))


# ---------------------------------------------------------------- aggregate

AGG_ROWS_MAX = 500_000  # the proxy's own cap on one answer
AGG_GROUPS_MAX = 500


class Aggregate(BaseModel):
    id: str
    variable: str
    unit: str | None = None
    by: str
    groups: list[dict[str, Any]] = Field(
        description="one entry per group: its keys (station, period), n (finite "
        "values), and mean, min, max, sum, std over them (null when n is 0)"
    )
    n_groups: int
    rows_used: int
    note: str | None = None
    download_url: str
    download_urls: list[str] | None = None
    download_complete: bool = True
    citation: Citation


_BUCKET = {"year": "datetime64[Y]", "month": "datetime64[M]", "day": "datetime64[D]"}


@mcp.tool(annotations=READ_ONLY)
async def aggregate(
    id: Annotated[str, Field(description="a variable id from search_variables")],
    licence: Annotated[
        ElicitationResult[LicenceAcceptance], Resolve(_licence)
    ],  # asked of the USER when the policy gates data, never an argument
    by: Annotated[
        Literal[
            "none", "station", "year", "month", "day", "station_year", "station_month"
        ],
        Field(description="the groups: none (one result), station, a period, or both"),
    ] = "none",
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
    apply_qc: ApplyQc = True,
    question: Question = None,
    ctx: Context | None = None,  # injected by the SDK, not a tool argument
) -> Aggregate:
    """Statistics over a WHOLE selection, computed on the server: count, mean,
    min, max, sum and standard deviation of one variable, overall or per
    station, year, month or day.  Use this for any average, total, range or
    trend instead of reading rows.  Any size: a selection over 500,000 rows
    is split by time on the server and merged exactly.  `std` is the
    POPULATION standard deviation (ddof 0).  `sum` adds the values as
    stored, which is a total only for values that are themselves totals per
    row: summing a RATE (umol m-2 s-1 per half-hour, gC m-2 d-1 per day)
    or a concentration gives no physical quantity -- for a flux total
    multiply the mean by the period."""
    _licensed(licence, ctx, "aggregate")
    _keep_question(ctx, "aggregate", question, {"id": id, "by": by, "station": station,
                   "start": start, "end": end, "lat_min": lat_min, "lat_max": lat_max,
                   "lon_min": lon_min, "lon_max": lon_max})
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
    name = var.get("shortname") or id.rsplit(".", 1)[-1]
    acc: dict[tuple, list[float]] = {}  # group -> [n, sum, sumsq, min, max]
    key_names: list[str] = []
    state = {"rows": 0, "parts": 0, "passport": {}, "windows": []}
    await _aggregate_parts(id, name, by, params, acc, key_names, state, depth=0)
    groups = []
    for lab in sorted(acc):
        n, sm, sq, mn, mx = acc[lab]
        g: dict[str, Any] = dict(zip(key_names, lab, strict=True))
        g["n"] = int(n)
        if n:
            mean = sm / n
            g.update(
                mean=round(mean, 6),
                min=round(mn, 6),
                max=round(mx, 6),
                sum=round(sm, 6),
                std=round(math.sqrt(max(sq / n - mean * mean, 0.0)), 6),
            )
        else:
            g.update(mean=None, min=None, max=None, sum=None, std=None)
        groups.append(g)
    notes = []
    if state["parts"] > 1:
        notes.append(
            f"computed over the whole selection in {state['parts']} time windows "
            f"(more than {AGG_ROWS_MAX:,} rows at once), merged exactly"
        )
    if len(groups) > AGG_GROUPS_MAX:
        notes.append(f"{len(groups)} groups; the first {AGG_GROUPS_MAX} are listed")
    notes.append("quality control " + ("applied" if apply_qc else "NOT applied"))
    # the windows it counted whole are the links: together the selection
    store = var["zarr_store"]
    if state["parts"] > 1:
        links = [
            _link(store, {**params, "start": a, "end": b})
            for a, b in sorted(state["windows"], key=lambda w: str(w[0]))
        ]
        notes.append(
            f"the selection is larger than one download: {len(links)} links, "
            "one per time window, together hold all of it"
        )
    else:
        links = [_link(store, params)]
    return Aggregate(
        id=id,
        variable=name,
        unit=var.get("unit"),
        by=by,
        groups=groups[:AGG_GROUPS_MAX],
        n_groups=len(groups),
        rows_used=state["rows"],
        note="; ".join(notes),
        download_url=links[0],
        download_urls=links if len(links) > 1 else None,
        citation=_citation(_with_objects(state), short=True),
    )


def _with_objects(state: dict) -> dict:
    """The first window's passport with the data objects of EVERY window:
    a split selection's sources are the union of its parts'."""
    pp = state["passport"] or {}
    graph = [
        nd
        for nd in pp.get("@graph", [])
        if not (isinstance(nd.get("@type"), list) and "icos:DataObject" in nd["@type"])
    ]
    return {**pp, "@graph": graph + list(state.get("objects", {}).values())}


AGG_PARTS_MAX = 64  # windows one aggregate may split into: 32 M rows at most


async def _aggregate_parts(
    var_id: str,
    name: str,
    by: str,
    params: dict,
    acc: dict,
    key_names: list,
    state: dict,
    depth: int,
) -> None:
    """Accumulate one window's partial statistics -- count, sum, sum of
    squares, min, max per group, which merge EXACTLY -- and, when the window
    holds more rows than one answer may, split it in two by time and recurse.
    The model asks once; the stitching is done here, where it cannot go wrong."""
    r = await _get(
        "/query", {**params, "format": "arrow", "max_rows": AGG_ROWS_MAX + 1}
    )
    with ipc.open_stream(io.BytesIO(r.content)) as rd:
        tab = rd.read_all()
    if tab.num_rows > AGG_ROWS_MAX:
        lo, hi = await _window(var_id, params)
        if (
            lo is None
            or hi is None
            or state["parts"] + 2 > AGG_PARTS_MAX
            or hi - lo < np.timedelta64(2, "h")
        ):
            raise ToolError(
                f"the selection is too large to aggregate even in parts "
                f"(over {AGG_PARTS_MAX} windows of {AGG_ROWS_MAX:,} rows): narrow the "
                "time window, the box or the stations"
            )
        mid = lo + (hi - lo) // 2
        fmt = lambda t: str(t.astype("datetime64[s]"))
        await _aggregate_parts(
            var_id,
            name,
            by,
            {**params, "start": fmt(lo), "end": fmt(mid)},
            acc,
            key_names,
            state,
            depth + 1,
        )
        await _aggregate_parts(
            var_id,
            name,
            by,
            {**params, "start": fmt(mid), "end": fmt(hi)},
            acc,
            key_names,
            state,
            depth + 1,
        )
        return
    state["parts"] += 1
    state["rows"] += tab.num_rows
    state["windows"].append((params.get("start"), params.get("end")))
    md = tab.schema.metadata or {}
    pp = json.loads(md[b"data_passport"]) if b"data_passport" in md else {}
    for nd in pp.get("@graph", []):
        if isinstance(nd.get("@type"), list) and "icos:DataObject" in nd["@type"]:
            state.setdefault("objects", {})[nd.get("@id")] = nd
    if not state["passport"]:
        state["passport"] = pp
    if tab.num_rows == 0:
        return
    if name not in tab.column_names:
        raise ToolError(f"{var_id}: the answer carries no column {name!r}")
    try:
        v = np.asarray(tab.column(name).to_numpy(zero_copy_only=False), dtype=float)
    except (TypeError, ValueError):
        raise ToolError(
            f"{var_id} is not numeric ({tab.schema.field(name).type}): there is "
            "nothing to average -- use get_data to list its values"
        ) from None
    keys: list[tuple[str, np.ndarray]] = []
    if by.startswith("station"):
        if "station" not in tab.column_names:
            raise ToolError(f"{var_id} has no stations; group by a period instead")
        keys.append(
            ("station", np.asarray(tab.column("station").to_pylist(), dtype=object))
        )
    period = by.split("_")[-1] if by not in ("none", "station") else None
    if period:
        tcol = next((c for c in tab.column_names if c.startswith("time")), None)
        if tcol is None:
            raise ToolError(f"{var_id} has no time column to group by {period}")
        t = tab.column(tcol).to_numpy(zero_copy_only=False).astype("datetime64[ns]")
        keys.append((period, t.astype(_BUCKET[period]).astype(str)))
    if not key_names:
        key_names.extend(k[0] for k in keys)
    ok = np.isfinite(v)
    if keys:
        combo = np.array(
            ["\x1f".join(str(k[1][i]) for k in keys) for i in range(len(v))],
            dtype=object,
        )
        labels, inverse = np.unique(combo, return_inverse=True)
    else:
        labels, inverse = np.array([""]), np.zeros(len(v), dtype=int)
    for gi, lab in enumerate(labels):
        x = v[(inverse == gi) & ok]
        key = tuple(str(lab).split("\x1f")) if keys else ()
        a = acc.setdefault(key, [0, 0.0, 0.0, math.inf, -math.inf])
        if x.size:
            a[0] += x.size
            a[1] += float(x.sum())
            a[2] += float((x * x).sum())
            a[3] = min(a[3], float(x.min()))
            a[4] = max(a[4], float(x.max()))


async def _window(var_id: str, params: dict):
    """The time window of a selection: its own start/end, else the
    variable's coverage from /summary over the same box."""
    lo, hi = params.get("start"), params.get("end")
    if lo is None or hi is None:
        try:
            s = (
                await _get(
                    "/summary",
                    {
                        "id": var_id,
                        **{
                            k: v
                            for k, v in params.items()
                            if k not in _NOT_SUMMARISED and k not in ("start", "end")
                        },
                    },
                )
            ).json()
        except ToolError:
            s = {}
        lo = lo or (s.get("time_min") or "").rstrip("Z") or None
        hi = hi or (s.get("time_max") or "").rstrip("Z") or None
        if hi and params.get("end") is None:
            hi = str(np.datetime64(hi) + np.timedelta64(1, "s"))  # end is exclusive
    if lo is None or hi is None:
        return None, None
    return np.datetime64(lo, "s"), np.datetime64(hi, "s")


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


# ---------------------------------------------------------------- the web page


async def _anonymous_datasets() -> list[dict]:
    """The stores the policy offers an anonymous caller, with their titles
    and domains, as the front pages list them."""
    root = await _cached_json("/")
    datasets = []
    for store in root["stores"]:
        if not POLICY.allowed(store, None):
            continue
        a = await _store_attrs(store)
        datasets.append(
            {
                "store": store,
                "title": a.get("title", store),
                "domain": a.get("icos_domain", ""),
            }
        )
    return datasets


async def _page_args() -> dict:
    return {
        "version": VERSION,
        "tools": await mcp.list_tools(),
        "prompts": await mcp.list_prompts(),
        "datasets": await _anonymous_datasets(),
        "token_stores": any(r.access == "token" for r in POLICY.rules),
        "zarr_url": POLICY.public_url,
    }


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def landing_page(request):
    """The HTML page at the server's root, built from what it offers an
    anonymous caller (landing.py)."""
    from starlette.responses import HTMLResponse

    return HTMLResponse(landing.render(**await _page_args()))


@mcp.custom_route("/r", methods=["GET"], include_in_schema=False)
async def retro_root(request):
    from starlette.responses import RedirectResponse

    return RedirectResponse("/r/", status_code=301)


@mcp.custom_route("/r/", methods=["GET"], include_in_schema=False)
async def retro_home(request):
    """The retro home page: the same server, the plain look (retro.py)."""
    from starlette.responses import HTMLResponse

    return HTMLResponse(retro.home(**await _page_args()))


async def _retro_reading(path: str):
    """A zarr service reading page at `path`, fetched as HTML and put in the
    retro frame; cached as the proxy's JSON is."""
    from starlette.responses import HTMLResponse, PlainTextResponse

    key = f"html {path}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        page = hit[1]
    else:
        try:
            # as the front page asks: a web route has no caller address to pass
            r = await _http().get(path, headers={"Accept": "text/html"})
        except httpx.HTTPError:
            return PlainTextResponse(
                "the ICOS zarr service did not answer", status_code=502
            )
        if r.status_code == 404:
            return PlainTextResponse("not found", status_code=404)
        if r.status_code >= 400 or not r.headers.get("content-type", "").startswith(
            "text/html"
        ):
            return PlainTextResponse(
                f"the ICOS zarr service answered {r.status_code}", status_code=502
            )
        page = r.text
        _cache[key] = (time.monotonic(), page)
    stores = [d["store"] for d in await _anonymous_datasets()]
    out = retro.reframe(
        page, zarr_url=POLICY.public_url, stores=stores, version=VERSION
    )
    if out is None:
        return PlainTextResponse(
            "the zarr service's page has a frame this server does not know",
            status_code=502,
        )
    return HTMLResponse(out)


@mcp.custom_route("/r/data-passport.html", methods=["GET"], include_in_schema=False)
async def retro_passport(request):
    return await _retro_reading("/data-passport.html")


@mcp.custom_route("/r/{store}/", methods=["GET"], include_in_schema=False)
async def retro_store(request):
    """A store page, retro; only a store the anonymous caller is offered,
    so a token store is not even confirmed to exist."""
    from starlette.responses import PlainTextResponse

    store = request.path_params["store"]
    if store not in [d["store"] for d in await _anonymous_datasets()]:
        return PlainTextResponse("not found", status_code=404)
    return await _retro_reading(f"/{store}/")


@mcp.custom_route("/static/{name}", methods=["GET"], include_in_schema=False)
async def static_file(request):
    """The page's photos and logos, from the package's own `static/` folder;
    a name is looked up in that folder's listing, never joined as a path."""
    from starlette.responses import PlainTextResponse, Response

    name = request.path_params["name"]
    files = {
        p.name: p for p in landing.STATIC.iterdir() if p.suffix in landing.STATIC_TYPES
    }
    if name not in files:
        return PlainTextResponse("not found", status_code=404)
    p = files[name]
    return Response(
        p.read_bytes(),
        media_type=landing.STATIC_TYPES[p.suffix],
        headers={"Cache-Control": "public, max-age=86400"},
    )


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request):
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok", "version": VERSION})


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="icos-mcp", description=__doc__.split("\n")[0])
    ap.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio"
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument(
        "--stateful",
        action="store_true",
        help="keep MCP sessions over HTTP: needed to put the licence question "
        "(licence_gate) to clients on protocol 2025-11-25 and older",
    )
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
                f"  {r.match:24s} {r.access:6s} {len(r.tokens)} token(s)  "
                f"{' '.join(sorted(r.groups)) + '  ' if r.groups else ''}{r.public_url or ''}"
            )
        print("  anything else          hidden")
        o = POLICY.oauth
        print(
            f"sign-in over HTTP: {o.issuer}  audience {o.audience}"
            if o
            else "sign-in over HTTP: none (anonymous)"
        )
        return
    if a.transport == "stdio":
        global _STDIO
        _STDIO = True
        mcp.run("stdio")
    else:
        from mcp.server.transport_security import TransportSecuritySettings

        hosts = [
            h.strip()
            for h in os.environ.get("ICOS_MCP_ALLOWED_HOSTS", "").split(",")
            if h.strip()
        ]
        security = None
        if hosts:
            # DNS-rebinding protection with the public name(s) admitted: the
            # SDK's default admits localhost only
            security = TransportSecuritySettings(
                allowed_hosts=hosts,
                allowed_origins=[f"https://{h.split(':')[0]}" for h in hosts],
            )
        mcp.run(
            "streamable-http",
            host=a.host,
            port=a.port,
            stateless_http=not a.stateful,
            transport_security=security,
        )


if __name__ == "__main__":
    main()
