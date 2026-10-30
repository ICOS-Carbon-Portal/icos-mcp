# icos-mcp

ICOS carbon and greenhouse-gas observations for AI assistants, over the
Model Context Protocol.  A thin server over the ICOS zarr proxy
(`https://zarr.icos-cp.eu`): every number comes from the proxy, and every
data answer carries the citation, licence and DOI.  The plan, and why it
is built this way, is [`../docs/icos-mcp-plan.md`](../docs/icos-mcp-plan.md).

Phase 1 (prototype): six read-only tools.

| tool | does |
|---|---|
| `list_datasets` | the stores, with domain, coverage, licence, DOI |
| `search_variables` | variable ids by name, quantity or unit ("methane" finds `ch4`) |
| `describe_variable` | one variable in full, with how its QC flag is read |
| `estimate` | how big a selection is, before fetching it |
| `get_data` | at most 2,000 rows, the total, a CSV download URL for the whole selection, the citation |
| `get_citation` | citation, licence and DOI from the data passport |

## Run it

    uv run icos-mcp                                   # stdio, for a local client
    uv run icos-mcp --transport streamable-http --port 8090   # http://127.0.0.1:8090/mcp

`ICOS_PROXY` points it at another proxy (the RC, a test instance).

In Claude Code:

    claude mcp add icos -- uv --directory /path/to/zarrm9/mcp run icos-mcp

## Test it

    uv run pytest          # live: every tool against the proxy, shapes and invariants
    uv run ruff check src tests

This server is the ORACLE for the M9 port (`mcpd`, plan phase 7): its tool
names, descriptions and answer shapes are the contract the port is
replayed against, so change them deliberately.
