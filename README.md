# icos-mcp

ICOS carbon and greenhouse-gas observations for AI assistants, over the
Model Context Protocol.  A thin server over the ICOS zarr proxy
(`https://zarr.icos-cp.eu`): every number comes from the proxy, and every
data answer carries the citation, licence and DOI.  The plan, and why it
is built this way, is [`../docs/icos-mcp-plan.md`](../docs/icos-mcp-plan.md).

Phases 1 and 2: seven read-only tools, five resources, three prompts, and
a per-store access policy.

| tool | does |
|---|---|
| `list_datasets` | the stores, with domain, coverage, licence, DOI |
| `search_variables` | variable ids by name, quantity or unit ("methane" finds `ch4`) |
| `describe_variable` | one variable in full, with how its QC flag is read |
| `find_stations` | the stations carrying a variable: near a point, in a box, by name or country |
| `estimate` | how big a selection is, before fetching it |
| `get_data` | at most 2,000 rows, the total, a CSV download URL for the whole selection, the citation |
| `get_citation` | citation, licence and DOI from the data passport |

## Run it

    uv run icos-mcp                                   # stdio, for a local client
    uv run icos-mcp --transport streamable-http --port 8090   # http://127.0.0.1:8090/mcp

`ICOS_PROXY` points it at another proxy (the RC, a test instance).

## Which stores it offers

DEFAULT-DENY: a store is offered only when a rule names it, whatever the
proxy behind the server would serve.  Without a config file the rules are
exactly the public stores of zarr.icos-cp.eu (`config/public.json` writes
them out).  `ICOS_MCP_CONFIG=/path/to/config.json` sets the proxy, the
`Host` header to send it, the base of download links, and an ordered list
of store rules, each `public` or `token`:

    uv run icos-mcp --show-policy          # what this config offers, to whom
    uv run icos-mcp --hash-token TOKEN     # the line a tokens_file takes

`config/ct122.example.json` is the shape for running beside zpd on CT122,
where zpd answers for EVERY store on the machine (the shuttle, GlobalView,
AGAGE): the public stores by name, those three behind tokens, anything
else hidden.  A token store is offered to an HTTP caller sending
`Authorization: Bearer <token>` whose SHA-256 is in its `tokens_file`
(plain tokens are never stored); a local stdio caller is the operator and
uses `ICOS_MCP_TOKEN`, which never speaks for an HTTP caller.  A hidden
store answers exactly as one that does not exist, and the static
`icos://catalogue` resource -- which the protocol does not tell who is
asking -- lists the public stores only.

In Claude Code:

    claude mcp add icos -- uv --directory /path/to/zarrm9/mcp run icos-mcp

Resources: `icos://stores/{store}/agent-guide`, `icos://stores/{store}/croissant`,
`icos://catalogue`, `icos://llms.txt`, `icos://data-passport`.  Prompts:
`station_record`, `compare_stations`, `ocean_region`.

## Test it

    uv run pytest                                 # over RECORDED proxy answers (tests/cassette)
    ICOS_CASSETTE_MODE=record uv run pytest       # re-record from the live proxy
    ICOS_LIVE=1 uv run pytest                     # live, nothing recorded
    uv run ruff check src tests

`eval/questions.json` is the DRAFT evaluation set: 30 questions across
the three domains, each naming the variable and station a correct answer
must use and a truth computed from the proxy at run time.  It needs one
reviewer per domain before it is relied on; the runner is not written yet.

This server is the ORACLE for the M9 port (`mcpd`, plan phase 7): its tool
names, descriptions and answer shapes are the contract the port is
replayed against, so change them deliberately.
