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
