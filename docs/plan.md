# An ICOS MCP server: plan (2026-10-30)

*Alex: "could the zarr proxy be marketed as MCP?" ... "Make a plan for
this ICOS MCP".*

## The answer in one paragraph

The zarr proxy is not an MCP server and should not be called one.  But
everything an MCP server for ICOS data needs is already served by it --
a variable catalogue, server-side selection, size estimates, agent
guides, Croissant sidecars and a data passport with the citation in
every answer.  So the ICOS MCP is a **thin, separate service** that
translates MCP tool calls into proxy requests and shapes the answers for
a language model's context.  It adds no data path of its own: the proxy
stays the one place data leaves, and its gate stays untouched.

## What exists, measured on zarr.icos-cp.eu today

| need | already served | notes |
|---|---|---|
| what data is there | `/` (6 public stores + root agent guide), `/llms.txt` | |
| how to use a store | `/{store}/agent_guide`, `/{store}/croissant` | per store, golden-gated |
| which variables | `/variables` (406 entries; `?store=`, `?domain=`) | id, names, unit, QC companion, domain, ECV/I-ADOPT link, query kind |
| how big is a selection | `/summary?id=...` | n_obs, n_stations, est_bytes, extent -- in ~100 ms |
| the data | `/query?id=...` | bbox, time, height, station, cruise, QC; ndjson, CSV, Arrow, Parquet |
| how to cite it | `_passport` (RO-Crate 1.1) in every answer; X-Data-* headers on CSV | |
| the contract | `/openapi.json` | checked against the routes by `apicheck` |

**One real gap: no station list.**  "Which stations measure CH4 near
Hamburg?" has no endpoint.  The station ids, positions and heights are
in each `_combined/<view>` group (`station`, `latitude`, `longitude`,
`sampling_height`), so the MCP server can read them once per store and
cache them.  If that proves useful beyond MCP, a `/stations?id=`
endpoint in zpd is the follow-up -- not a precondition.

## Goals and non-goals

**Goals**
* An agent connected to one URL can discover ICOS data, pick the right
  variable, find stations or a region, check the size, and get a
  correctly cited answer -- without reading the zarr layout.
* The citation and licence travel with every data answer.
* Large data never goes through the model's context: the server returns
  summaries, a few rows, and a download URL for the full answer.

**Non-goals**
* No new data path, cache or store.  Every number comes from the proxy.
* No writes: the admin routes (`POST/PUT/DELETE /variables`) are not
  exposed.
* No zarr chunk access through MCP.  An agent that wants the arrays is
  pointed at xarray with the URL, as the agent guides already do.
* Not the Carbon Portal as a whole (portal objects, SPARQL, the
  footprint service).  That is a candidate second server, not scope
  creep for this one.

## Architecture

```
MCP client (Claude, ChatGPT, Copilot, an IDE, a notebook agent)
   |  streamable HTTP, https://zarr.icos-cp.eu/mcp   (or mcp.icos-cp.eu)
caddy (icos4)  ->  nginx on CT122
   |-- /mcp  ->  icos-mcp   (Python, MCP SDK, a container on CT122)
   |                 |  plain HTTP over loopback/docker gateway
   '-- /*    ->  zpd 8081  <-'
```

* **Python first, then M9, with the Python server as the ORACLE**
  (Alex, 2026-10-30: "use the python version as oracle and then we can
  follow spec changes by pumping python and having m9 follow").  The
  protocol is JSON-RPC with sessions, streaming and a moving
  specification; the official SDK tracks it, and M9's corpus has
  `HttpServer` and `Json` but nothing for MCP.  So the Python server
  settles the tools and their descriptions quickly, and then serves the
  M9 port the way zppy served the proxy port: the reference it is
  replayed against.  See "The M9 port and its oracle" below.
* **Remote, streamable HTTP**, so a user connects by URL with nothing to
  install.  A stdio build of the same code (`uvx icos-mcp`) comes free
  for local clients.
* **Stateless**: no MCP session state the server needs to keep, so it
  restarts or scales without loss.  The station cache is a per-process
  memo refreshed when the store's `.zmetadata` changes (the `storeVersion`
  the passport already carries).
* **Deployed like the viewers**: a uv-built image on CT122 under the
  existing compose override, autodeployed by the timer installed on
  2026-09-20.  It reaches zpd through the docker host gateway, as the
  viewers do.
* **Public and unauthenticated**, like the proxy.  Rate limiting in
  nginx on `/mcp` (per client IP, using the realip map that already
  exists) is enough to begin with.

## The tools

Seven tools, each a thin call with an answer sized for a context
window.  Names and parameters are drafts; the descriptions are the part
that matters most, because they are what the model reads.

| tool | does | proxy calls |
|---|---|---|
| `list_datasets` | the stores with title, domain, coverage, licence, DOI | `/`, root `.zattrs` |
| `search_variables(text, domain?, store?)` | ranked catalogue matches on id, names, unit, ECV; returns ids with units and query kind | `/variables` (cached) |
| `describe_variable(id)` | one entry plus its QC rule in words and the store's coverage | `/variables/{id}`, agent guide |
| `find_stations(id, near?/bbox?, radius_km?)` | stations carrying that variable, with position, height, record span | `_combined` coordinate arrays (cached) |
| `estimate(id, filters...)` | rows, stations, bytes, extent -- before fetching | `/summary` |
| `get_data(id, filters..., max_rows=200)` | the first rows as a table, the total count, a **download URL** for the full selection in CSV/Arrow/Parquet, and the citation | `/query` |
| `get_citation(id or url)` | citation, licence, DOI and the passport link | `/query?max_rows=1`, passport |

**A second phase, if the first is used:**

| tool | why |
|---|---|
| `aggregate(id, filters..., by=day/month/year, stat=mean/min/max/count)` | most questions are about trends, and computing a monthly mean from 80,000 hourly rows in the model's context is both wasteful and wrong.  Done in the MCP server from the Arrow answer (pyarrow), so the proxy stays unchanged. |
| `compare_stations(id, stations[], period)` | the commonest multi-station question, as one call |
| `python_snippet(id, filters...)` | the xarray / pandas code that reproduces the selection, for the user to run -- the "take it home" answer |

**Answer shape, the rule that makes it work.**  Every tool answer is
structured (`structuredContent` with an output schema) *and* has a short
text rendering.  Every data answer carries:
* `rows_returned` and `rows_total`, so truncation is never silent;
* `download_url` -- the same `/query` URL with `format=csv` (or arrow),
  which the user or a code tool can fetch whole;
* `citation`, `licence`, `doi` from the passport, verbatim.

A hard cap on rows (200 by default, 2,000 at most) and on bytes keeps
every answer well inside a context window.  `estimate` is what an agent
should call first, and the tool descriptions say so.

## Resources and prompts

* **Resources** (read by the client on request, not pushed): each
  store's agent guide and Croissant document, the variable catalogue,
  `/llms.txt`, and the data passport description
  (`/data-passport.md`).  URIs: `icos://store/icos-obspack/guide` and so
  on.
* **Prompts** (user-selectable starting points), three to begin with:
  "plot a station's CO2 record", "compare a variable across stations",
  "what ocean CO2 data exists in a region".  Each expands to the
  tool-call sequence and the citation reminder.

## Quality: how to know it works

The proxy has a golden-master gate; the MCP server needs its own, and
it is a different kind of check.
1. **Contract tests** (pytest, in CI): every tool against a recorded
   set of proxy answers -- schema of the output, row caps, the citation
   present, the download URL reproducing the rows.
2. **Live smoke** after deploy: the same calls against the live proxy,
   in the deploy script, beside the existing post-checks.
3. **An evaluation set of real questions** -- 30 to 50, written with
   the ICOS domain people (atmosphere, ocean, ecosystem), each with the
   expected variable id, station set and a checkable number ("mean CO2
   at HTM150 in January 2025").  Run against two or three clients and
   models before any announcement, and after any change to tool
   descriptions.  This is the check that tells whether the descriptions
   work; nothing else will.
4. **Protocol conformance, in CI**: the MCP project's own conformance
   suite (`npx @modelcontextprotocol/conformance server --url
   http://localhost:<port>/mcp --suite active`), version-pinned and run
   against the container before deploy, with any known failures listed
   in its baseline file -- the `gate/expected.txt` idea, applied to the
   protocol.  There is no MCP certification programme: a passing
   conformance run plus a registry listing is what "it is an MCP
   server" can honestly rest on.
5. **MCP Inspector** by hand at each phase, for what a suite cannot
   judge: whether the tool list and answers read well.

## The M9 port and its oracle

The proxy port's method, applied a second time, with the lessons it
paid for.

**The oracle is pinned and asserted.**  `mcp-oracle.lock` names the
Python server's commit, its MCP SDK version and the conformance suite
version, and the gate checks all three before it compares a byte -- the
`reference.sha` lesson: an oracle whose identity nothing checks produces
differences that read as port bugs.

**What is compared.**  A recorded corpus of MCP sessions --
`initialize`, `tools/list`, `resources/list`, `prompts/list`, and a
`tools/call` for every question in the evaluation set, plus the
refusals (unknown tool, bad arguments, an id that does not exist, a
selection over the row cap) -- replayed against both servers over the
same proxy and the same stores.  Compared on the JSON-RPC result,
parsed and canonicalised, with the masks named in one place: the
session id, `serverInfo` (each names itself, as the version badge
does), and the passport's per-request members the proxy gate already
masks.  The tool DESCRIPTIONS and schemas are compared byte for byte:
they are the product.

**Both run the conformance suite** at the pinned version, and the M9
server's known-failure baseline must be no longer than the Python's.

**Decided divergences are declared**, in an expected file read in both
directions, exactly as `gate/expected.txt` works for the proxy: an
undeclared difference fails, and so does a declared one that no longer
differs.

**Following a spec change** is then a fixed procedure:
1. bump the SDK and the conformance pin in the Python server; it passes
   the suite and its own contract tests;
2. move `mcp-oracle.lock` to that commit;
3. the M9 gate now fails with the list of differences and the
   conformance tests it newly fails -- that list IS the port's work;
4. port, until the gate is green, and promote.
Production keeps serving the previous M9 build throughout; the Python
server never needs to serve the public.

**Where the M9 server lives**: a separate binary (`mcpd`) calling zpd
over loopback, the same shape as the Python server, so the replay
compares like with like.  Folding it into zpd as a route (no hop, the
catalogue already in memory) is a later option once the two agree.

**Its size**, estimated from what the corpus already has: JSON-RPC
framing and dispatch, the streamable-HTTP transport with sessions and
server-sent events, the seven tools, and the station cache -- some
1,000 to 1,500 lines.

**Sign-in is part of the contract (added 2026-10-31).**  The Python
server checks OAuth tokens from ICOS's Keycloak and offers `group`
stores (`auth.py`; the move to the production SSO is
`docs/sso-production.md`).  mcpd ports it, and the corpus records it.
* *What the M9 library already has:* `Rsa` (RS256 verification:
  PKCS#1 v1.5 over SHA-256, using OpenSSL, held against Python's
  `cryptography`, forgeries included), `Hash` (SHA-256 and HMAC, for
  the store tokens), `Text.FromBase64Url`, `Json`, and the HTTP client.
* *What mcpd adds:*
  * Reading the realm's keys (JWK: modulus and exponent). `Rsa` reads
    only PEM and DER, so this is a small DER encoder, or better an
    `Rsa.PublicKeyNE` in the library. Plus the key set fetched over
    HTTPS, cached for an hour, and refetched for an unknown `kid`.
  * The token checks in the oracle's order: RS256 only, then issuer,
    audience (a string or a list), expiry and `sub`, then the scope
    (403), then `groups` for `group` rules.
  * The protected-resource metadata and the 401 and 403
    `WWW-Authenticate` headers, worded as the SDK words them, because
    they are compared byte for byte.
  * The `group` rule type and its load-time refusals.
  
  About 2 to 3 days on top of the port.
* *Made comparable on the Python side first:* it accepts RS256 only,
  so the two refuse the same tokens, and `jwks_file` loads a fixed key
  set in place of fetching Keycloak's.
* *The recordings:*
  * Sessions are signed by a test key held in the repository (its JWKS
    under `tests/`, served through `jwks_file`), with tokens that outlive
    the recording.
  * Real Keycloak tokens never enter the corpus: they expire, the keys
    rotate, and they name people.
  * The corpus covers: no token, a forged signature, a wrong audience,
    an expired token, a missing scope (403), a store token, a group
    member and a non-member.
* *Conformance* still runs WITHOUT the oauth section: with sign-in on,
  every scenario stops at the 401.

## Phases

| phase | content | effort |
|---|---|---|
| 0. decisions | the open questions below | a conversation |
| 1. prototype | `list_datasets`, `search_variables`, `estimate`, `get_data`, `get_citation`; stdio + HTTP; run against the RC from a laptop with Claude Desktop | 2-3 days |
| 2. stations and evaluation | `find_stations` with its cache, `describe_variable`, resources and prompts; the question set written with domain people; contract tests in CI | ~1 week |
| 3. deployment | container on CT122, nginx route and rate limit, caddy (one line), autodeploy, live smoke in the deploy script | 1-2 days |
| 4. phase-two tools | `aggregate`, `compare_stations`, `python_snippet`, if the logs show the demand | ~1 week |
| 5. announcement | listing (below), a page in the landing site, a notebook showing it | when 2-3 hold |
| 6. the oracle gate | the session corpus recorded from the Python server, `mcp-oracle.lock`, the replay and conformance phases in CI on CT122 | 2-3 days |
| 7. the M9 port | `mcpd`, until the gate is green; then it takes `/mcp` and the Python server becomes the oracle only | 2-3 weeks |

## Where it stands (2026-11-11)

Phases 0-4 and 6-7 are done; daily reports are in `docs/reports/`.

* **The Python server** serves <https://mcp.icos-cp.eu> (CT122:8090),
  0.3.0 (`4da9c57`), public and in the MCP Registry.  The licence gate
  and the question log are on `main` but **off** in production since the
  2026-11-05 rollback (`docs/reports/2026-11-05-mcp-licence-gate.md`).
  Sign-in through ICOS Keycloak is the agreed direction and is parked.
* **mcpd** (M9, in ICOS-Carbon-Portal/zarrm9) is gated against this
  repository on every push there: 90 corpus rows, only 2 transport rows
  differ, both declared; conformance results identical.  It runs as a
  shadow on 10.10.10.126:8094.  It **refuses to start** on a
  configuration asking for sign-in, group stores, the licence gate or
  the question log, so the sign-in contract above is not yet ported.
* **A Scala port** (`mcp-scala/` in zarrm9), added 2026-11-09 to
  `cp-kernel`'s conventions, answers the same corpus with the same 2
  declared differences, runs in the same gate, and is a shadow on :8095.
  This repository is the oracle for both ports.
* **Claude evaluation:** both ports answered 26 of 28 correctly, with
  the same two misses as the Python server.  The Qwen evaluation needs
  the ICOS network and was not run on the ports.
* **Open:** which port replaces the Python server.  Only one will be
  chosen; the switch is the caddy line for mcp.icos-cp.eu.

## Making it known

Once phase 3 is live and the evaluation set passes:
* the **official MCP Registry** (registry.modelcontextprotocol.io), with
  a server.json describing the remote endpoint;
* the **ENVRI-Hub Catalogue of Services** -- a second `epos:WebService`
  beside the proxy's, pointing at the same datasets;
* the proxy's own landing page, `/llms.txt` and root agent guide, so an
  agent that finds the proxy learns the MCP endpoint exists;
* the ICOS website and the CodingHelpAndExamples repository, with one
  worked example.

The claim this supports, honestly: *"ICOS atmosphere, ocean and
ecosystem data, queryable by AI assistants through MCP, with the
citation in every answer."*

## Risks

* **Wrong variable, confident answer.**  The biggest risk is not the
  protocol but an agent picking `co2_nvalue` for `co2`, or a fluxnet
  flux in the wrong unit.  Mitigations: the search ranks primary
  variables first and hides QC/count companions; every answer states the
  variable's long name and unit; the evaluation set covers the traps the
  port already found (the aggregated fluxnet units, the flag-as-variable
  404).
* **Load.**  An agent loop can call `get_data` many times.  The row cap,
  `estimate`-first descriptions and nginx rate limiting bound it; the
  proxy handled 64 concurrent clients in the benchmark.
* **Specification drift.**  MCP is still moving.  Using the SDK, and
  keeping the server thin, makes an upgrade a dependency bump, and the pinned conformance suite says when an upgrade is due.
* **Two things to maintain.**  The catalogue and agent guides are the
  source of truth; the MCP server must read them, never copy them, so it
  cannot drift from the proxy.

## Decisions (Alex, 2026-10-30) -- none open

1. **Where it lives: `mcp.icos-cp.eu`**, its own name (room for a wider
   Carbon Portal server later).  NB: the name resolves today to
   194.47.223.139 (fsicos2), not to icos4's 130.235.92.135 where caddy
   serves the zarr names -- DNS has to move before it can serve.
2. **Repository: `ICOS-Carbon-Portal/icos-mcp`**, private, GPL-3.0-or-later
   -- done, split out of zarrm9 with its history.
3. **Stores: internal behind a token** -- `access.py`, default-deny, the
   public stores of zarr.icos-cp.eu unless `ICOS_MCP_CONFIG` says
   otherwise, token stores by SHA-256 of a bearer token.  Verified over
   HTTP beside zpd on CT122, where zpd serves all nine stores.
4. **Usage logging: none in the MCP server.**  The data passports the
   proxy mints for every data answer are the usage record, and the zarr
   proxy will do the Matomo logging.  Consequence for deployment: the
   proxy must see the END USER, not the MCP server, so the server
   forwards the caller's address to the proxy -- otherwise every MCP
   user is one passport session and one Matomo visitor, the bug the
   sidecar's realip fix closed for the zarr names on 2026-09-08.
5. **The question set: generated** (`eval/questions.json`, 30 questions,
   every id, station and truth query checked against the live proxy);
   domain review is work for later.  Qwen (qwen3-8-27b, the Spark) answers
   27 of 28 right after the fixes the set itself drove.
