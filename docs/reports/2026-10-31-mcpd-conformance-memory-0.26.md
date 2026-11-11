# ICOS MCP server — work report, 2026-10-31

The conformance suite ran and found the one protocol check mcpd lacked;
the transport's edges were ported; a memory climb under load turned out to
be the M9 runtime keeping freed blocks per thread, in mcpd and in the
production proxy alike, and its proper fix arrived the same evening with
M9 0.26.0, which also made every string UTF-8.  The Python server gained
the retro pages and an OAuth sign-in (off).

**Live:** <https://zarr.icos-cp.eu> on zpd `c8f67d0` (m9 0.26.0, gated);
<https://mcp.icos-cp.eu> the Python server.  zarrm9 `f315e1c` .. `308b187`
(14 commits); icos-mcp `b49ae65`, `1d7f9ff`, `140f5cd`.

---

## 1. Conformance, and the DNS-rebinding check (`f315e1c`)

`@modelcontextprotocol/conformance` 0.1.16 is pinned in `mcp/oracle.lock`
and read **differentially**: 23 of 32 scenarios fail on the oracle too,
because they call the suite's own test tools, so the bar is "every
scenario the oracle passes".  mcpd failed one the oracle passes,
`dns-rebinding-protection` -- it answered a foreign Host with 200.
`Mcp.Admit` is now the SDK's transport security in its order: a POST that
is not `application/json` 400, a Host not admitted 421, a foreign Origin
403; admitted are `ICOS_MCP_ALLOWED_HOSTS` and their https origins, else
loopback.  After it the 32 results equal the oracle's.

## 2. The transport's edges (`ee9864e`)

406 for an Accept that does not admit both JSON and the event stream, 415
for a Content-Type that is not exactly `application/json`, 405 as a
JSON-RPC error (DELETE says sessions are not supported), the parse-error
document -- and **400 for a batch or a body that is no JSON-RPC message,
where mcpd had answered 202 and dropped it**, the sweep's one real bug.
The replay grew an `HTTP` row type and 14 transport rows: 69 rows, 2
declared (pydantic's whole validation report on the oracle against one
line here; same status, same code).

## 3. Memory: m9's per-thread block reuse

24 simultaneous clients took mcpd from 37 to 435 MB and still creeping.
`leaks` found nothing leaked; `vmmap` found the M9 runtime's **kept
blocks**: each thread keeps up to 128 MB of freed large blocks for reuse,
a speed-up for a program reading big files, and in a server a high-water
mark held for ever.  `M9_BLOCK_REUSE=0` flattened mcpd at ~70 MB.  The
production zpd carried the same rule: measured A/B on fresh processes,
**7.33 GB held after a load burst with reuse, 5.43 GB without**, speed
within noise (`3a4224e`).

Alex's position, sent to the m9 session: **releasing a thread's kept
memory is the job of the program that starts the threads**, not of an
environment variable every server must know.  m9 0.26.0 delivered that
(`System.KeepAtMost`, `HttpServer.Config.keepBytes`); production ran a
borrowed pre-release build for the afternoon.

## 4. The retro pages (`e5c594f`)

The oracle grew `/r/` -- its plain home page from before the redesign,
and the zarr service's reading pages put back into their old frame.
mcpd serves them from the generated templates; the routes match
starlette's (301, 307, 404).  76 rows, 2 declared.

## 5. M9 0.26.0: STR is UTF-8 (`7e1bae6`, `c8f67d0`)

The pin moved to the release; the UTF-8 port from the day before was
merged (43 more sites in the new MCP code).  **The first gate was red**,
and it found what the port's own checks could not: under 0.26.0
`Csv.Name`/`Csv.TextAt` answer decoded text, so two "re-decode the
octets" lines in Icosocean and Shuttle became wrong, and two suffix
comparisons cut inside a multi-byte character.  Fixed; every store
rebuilt equal to its golden; **gate PASS, deployed `c8f67d0`**, production
on a gated build again.  zpd now calls `System.KeepAtMost (0)` itself, and
the A/B with and without `M9_BLOCK_REUSE` showed no difference (4.29 vs
4.22 GB after load), so the variable was retired (`4117915`).

mcpd on 0.26.0 on CT122: 77 rows, 2 declared; 130 MB resident under six
parallel replays and flat when idle.

## 6. The Python server

* `b49ae65` the retro pages; `1d7f9ff` the conformance baseline recorded.
* `140f5cd` **sign-in through ICOS's Keycloak** (OAuth resource server,
  stores by Keycloak group), verified against the pilot realm with Claude
  Code and Google; off unless configured.

## Open at the end of the day

The licence question (how a user accepts the ICOS data licence through an
MCP client); which of the Python server and mcpd serves the public.
