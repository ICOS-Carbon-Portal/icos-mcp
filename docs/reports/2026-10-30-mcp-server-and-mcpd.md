# ICOS MCP server — work report, 2026-10-30

The day the ICOS MCP server was born, deployed and given an M9 port.  In
the morning a Python prototype (in this repository's `mcp/`) grew from six
read-only tools to a deployed service at <https://mcp.icos-cp.eu>, with an
evaluation on our own Qwen that drove four rounds of fixes to the tools
*and* to the proxy's catalogue.  At midday the server moved to a
repository of its own, ICOS-Carbon-Portal/icos-mcp (private, GPL-3.0).
From the afternoon the M9 port, **mcpd**, was built against it as an
oracle and reached 55 of 55 replayed rows by the evening, the landing page
included.

**Live:** <https://mcp.icos-cp.eu> — the Python server on CT122:8090.
zarrm9 `48a923d` .. `c2c16c0` (30 commits); icos-mcp `7601eef` .. `973656f`
(26 commits).

---

## 1. The Python server: from prototype to deployed

* **Tools**: `list_datasets`, `search_variables`, `describe_variable`,
  `estimate`, `get_data`, `get_citation`, then `find_stations` and
  `aggregate` -- the last added because the evaluation runner showed an
  agent cannot average thousands of rows in its context.  Resources (the
  catalogue, each store's agent guide and Croissant sidecar, `llms.txt`,
  the passport description) and three guided prompts.
* **The store policy is default-deny** (`access.py`): inside CT122 zpd
  answers for all nine stores, and only its host map keeps the shuttle,
  GlobalView and AGAGE off the public name.  Without a config the server
  offers exactly zarr.icos-cp.eu's public stores; `token` stores admit a
  caller whose Bearer token hashes to a listed SHA-256.  Verified: no
  token sees six stores, the shuttle token seven.
* **Deployed** as `icos-mcp.service` on CT122, behind caddy as
  mcp.icos-cp.eu, with a landing page (the new design: photo hero, domain
  cards, the passport band) and `/health`.

## 2. What the evaluation taught -- and what it found in the proxy

The evaluation runs 30 questions against our own Qwen (qwen3-8-27b on the
Nvidia Spark, icoscp-spark1) and grades the route (variable, station, QC)
and the answer against truths computed live from the proxy.  It went from
**17 of 27 answers right in the morning to 24 of 28 by the move**, and the
misses pointed at real defects:

* **67 of 78 icos-obspack catalogue ids answered 501**: every gas
  companion was catalogued on `time` and `intake_height` while its arrays
  sit on `time_<gas>` and the panel calls the height `sampling_height`.
  `Variables.ArrayTimeDim` now takes the time coordinate from the array's
  own dimensions; the production catalogue was corrected (47 answer now,
  20 honestly cannot).
* **The NEE cube trap**: `NEE`, `GPP`, `RECO` are cubes (station, time,
  ustar, variant[, partition]) that /query refuses.  Alex: "THE most
  important variable ... prescribe the preferred version".  The catalogue
  now lists only what /query serves (740 -> 661 entries); the server
  prescribes `NEE_VUT_REF`, `GPP_NT_VUT_REF`, `RECO_NT_VUT_REF`, and the
  other variants stay findable as `access: "zarr"` with the xarray line
  that reads any slice.
* NRT citations per data object, today's date in the instructions, the
  two ocean stores told apart, grid cell counts trusted to `estimate`.

**An outage of my own making**: a 16-worker scratch zpd plus parallel
queries on CT122 took production down for ~4 minutes (10:55-10:59 UTC)
through the OOM killer.  Since then checks on CT122 run under
`systemd-run --scope -p MemoryMax=...`, one query at a time.

## 3. mcpd: the port and its oracle (plan phases 6 and 7)

* **The oracle gate first** (`48a923d`): `mcp/oracle.lock` pins the
  Python server's commit, its SDK and the protocol; `mcp/corpus.json`
  holds the requests; `mcp/mcpreplay.py` sends each to both servers over
  stateless streamable HTTP and compares status, framing and the JSON-RPC
  message, a tool's text both parsed and raw.  Proved on the oracle
  against itself first: 45 rows, 0 differ.
* **The surface is generated, never typed**: `mcp/surface.py` runs in the
  pinned oracle checkout and writes the tool, prompt and resource lists
  and the prompt texts into `mcp/surface.json`.
* **Then the port**, `Mcp.m9` + `Mcpd.m9`: the protocol and
  `list_datasets` (17 of 45 rows), six of seven tools (41), all seven with
  `aggregate`'s numpy-pairwise sums over zpd's Arrow answers (45), a
  bounded cache and pydantic's own validation messages (50).
* **On CT122 against the production zpd: 49 of 50**, and the one was a
  blocker: zpd composes `llms.txt` from the request's Host, and m9's Http
  could not set one, so mcpd served loopback URLs.  Fixed through a
  borrowed copy of m9 0.25.4's Http, then cleanly by **moving the pin to
  0.25.4** the same evening (both borrowed files deleted).
* **The landing page**, built per request from the generated templates:
  55 of 55 rows, byte for byte; `deploy/ct122/mcpd-replay.sh` replays it
  on CT122 under production's policy.

## 4. Also today

* **icos-eco-nrt became queryable**: two panels built by the shuttle's
  band-wise writer, station identity from one SPARQL query, per-product
  citations, the Foken flag as its QC; 66 catalogue entries.
* **The stores follow their DOI metadata**: `Doicheck.m9` and the nightly
  `zarr-doi-refresh` timer rebuild a store whose citations changed.
* **The UTF-8 switch** for M9 0.26.0 was ported on a branch (3,174 sites
  to 0), waiting for the release.

## Open at the end of the day

The conformance suite had not run; the Python server's sign-in was a plan;
mcpd's memory under load was unmeasured.
