# icos-mcp

ICOS carbon and greenhouse-gas observations for AI assistants, over the
Model Context Protocol.  A thin server over the ICOS zarr proxy
(`https://zarr.icos-cp.eu`): every number comes from the proxy, and every
data answer carries the citation, licence and DOI.  The plan, and why it
is built this way, is [`docs/plan.md`](docs/plan.md).

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

Over HTTP the server also answers `GET /` with an HTML page in the style
of zarr.icos-cp.eu -- built from what it offers an anonymous caller: the
endpoint and how to connect, the public datasets, the tools and prompts,
the citation rule (`landing.py`; `ICOS_MCP_PUBLIC_URL` sets the endpoint
it names, default https://mcp.icos-cp.eu) -- and `GET /health`.

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

## Signing in

An `oauth` section in the config makes signing in required over HTTP.
Anyone can sign in. The aim is to know who uses the server, not to keep
anyone out:

    "oauth": {"issuer": "https://sso.fs4vm.icos-cp.eu/realms/icos-test",
              "audience": "https://mcp.icos-cp.eu"}

The server is only a resource server. ICOS's Keycloak handles the
sign-in, through cpauth, Google or ORCID. MCP clients find Keycloak in
`/.well-known/oauth-protected-resource`, register themselves with it and
send the user to sign in. A token must be signed by the realm and carry
this server's `audience`, which needs an audience mapper in Keycloak.
The store tokens above still work. The landing page and `/health` stay
open, and the stdio operator is never asked to sign in (`auth.py`).

Signing in also allows a `group` store rule. The store is offered to
members of a Keycloak group, or of a subgroup of one, and to no one else.
The groups come from the verified token's `groups` claim:

    {"match": "agage*", "access": "group", "groups": ["agage"],
     "public_url": "https://agage-zarr.icos-cp.eu"}

Group names are paths below `/apps/mcp` in the realm: `agage` is
`/apps/mcp/agage`. That is the only part of a user's groups Keycloak
sends this server.

Access is granted and withdrawn in Keycloak, not in the config. A `group`
rule may also list store tokens for scripts. A `group` rule without an
`oauth` section is refused at load, because nobody could reach it.

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
    conformance/run.sh                            # the MCP conformance suite (needs Node)

`conformance/run.sh` starts a local server and runs the MCP project's own
conformance suite against it, pinned to 0.1.16 by
`conformance/package-lock.json`.  `conformance/baseline.yml` lists the
known failures and why each one fails (mostly the suite's own test
fixtures, which a real server doesn't carry).  It is read in both
directions: a new failure fails the run, and so does an entry that now
passes.

`eval/questions.json` is the DRAFT evaluation set: 30 questions across
the three domains, each naming the variable and station a correct answer
must use and a truth computed from the proxy at run time.  It needs one
reviewer per domain before it is relied on; the runner is not written yet.

This server is the ORACLE for the M9 port (`mcpd`, plan phase 7): its tool
names, descriptions and answer shapes are the contract the port is
replayed against, so change them deliberately.

## Licence

Copyright (C) 2026 ICOS ERIC -- Carbon Portal.

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option)
any later version.  See [`LICENSE`](LICENSE).

The DATA this server serves are not covered by this licence: each answer
carries the licence of its dataset (ICOS data are CC BY 4.0) and the
citation to give with it.
