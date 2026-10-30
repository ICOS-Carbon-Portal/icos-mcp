# CLAUDE.md -- icos-mcp

The ICOS MCP server: ICOS atmosphere, ocean and ecosystem observations for
AI assistants, as a THIN server over the ICOS zarr proxy
(https://zarr.icos-cp.eu).  The plan, its decisions and its phases are
`docs/plan.md`; the README says how to run and test it.

Split out of the private `zarrm9` repository (ICOS-Carbon-Portal/zarrm9,
the M9 port of the zarr proxy) on 2026-10-30, history kept, because this is
a Python project with its own CI and release cadence, and every push to
zarrm9's main runs the proxy gate on CT122.  PRIVATE for now (Alex: "don't
want to ring the bell yet"); the MCP Registry listing waits for that.

**This server is the ORACLE for an M9 port** (`mcpd`, plan phase 7), which
will live in zarrm9 and be replayed against a pinned commit of this
repository over the recordings in `tests/cassette` -- the way the M9 proxy
was replayed against icos-zarr.  So tool names, descriptions and answer
shapes are a contract: change them deliberately.

What is load-bearing, each learned the hard way (zarrm9's CLAUDE.md has the
long versions):
* **Default-deny store policy** (`access.py`): inside CT122 zpd serves every
  store on the machine; only the rules here keep the shuttle, GlobalView
  and AGAGE away from anonymous callers.
* **NEE is prescribed** as `NEE_VUT_REF` (GPP/RECO as `*_NT_VUT_REF`); the
  ONEFlux cubes are discovered from the panels and offered as zarr-only
  variables with an xarray recipe (Alex: "find and access").
* **aggregate** exists because an LLM cannot average 717 rows in context.
* **NRT data cite per object**, and a TRUNCATED panel passport drops its
  objects (the proxy's transcribed reference quirk) -- hence `_cite`.
* **The evaluation runs on our own Qwen** (qwen3-8-27b, SGLang on
  icoscp-spark1:30000); `--agent claude` compares.  The runner gives the
  model today's date, as real clients do.
* **Never load-test against CT122 from inside it**: a scratch zpd plus
  parallel queries took production down for four minutes on 2026-10-30.

## DEPLOYED: https://mcp.icos-cp.eu (2026-10-30, Alex: "deploy" ... "ok for
## the caddy entry")

* **CT122**: the code is a COPY at `/root/rc/icos-mcp` (rsync from a
  checkout, `uv sync --frozen --no-dev`) -- a read-only deploy key for this
  private repository was refused by the permission classifier as an
  access grant, so CT122 cannot fetch it yet; adding one is Alex's call.
  `icos-mcp.service` (deploy/icos-mcp.service) listens on
  10.10.10.126:8090, MemoryMax=1G beside production's zpd; config
  `/etc/icos-mcp/config.json` = deploy/ct122.json: zpd on loopback asked
  as the PUBLIC host name (zpd's own host map as a second barrier), the
  five public store patterns, no token stores (none issued),
  forward_client_ip on.
* **icos4**: caddy block `mcp.icos-cp.eu -> 10.10.10.126:8090` between
  `# BEGIN icos-mcp` / `# END icos-mcp`, after the zarr-proxy block;
  backup `Caddyfile.bak-20261030-120830-icos-mcp`.  Let's Encrypt
  certificate obtained on first request.
* **Verified**: the landing page and /health over HTTPS; a full MCP session
  (tools, search ranking NEE_VUT_REF first, aggregate, datasets) in 0.7 s;
  zpd records a forwarded X-Real-IP as its /24 in the passport.
* **To update**: rsync a checkout to CT122, `uv sync --frozen --no-dev`,
  `systemctl restart icos-mcp`.
