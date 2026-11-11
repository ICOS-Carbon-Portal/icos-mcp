# ICOS MCP server — work report, 2026-11-05

The ICOS data are CC BY 4.0, and the portal asks a user to accept the
licence before a download.  Today the Python MCP server learned to ask the
same question through MCP itself -- and, at Alex's request, to ask the
user's consent to keep their questions anonymously.  It went live for one
evening and was **rolled back the same night**, for reasons that are
about the clients, not the code.  The work happened in icos-mcp; this
repository's ports were not changed.

icos-mcp `c4fcbc4`, `c477cba`, `dcceafc`, `b893823` (4 commits).
Production at the end of the day: the gate code deployed, **switched off**.

---

## 1. The licence gate (`c4fcbc4`)

Off by default (`"licence_gate": true`, or `ICOS_MCP_LICENCE_GATE=1`).
When on, the two tools that hand over **data** -- `get_data` and
`aggregate` -- put the question to the user through MCP **elicitation**
(a form with one checkbox, the CC BY 4.0 terms and the licence link)
before any proxy request; the metadata tools are CC0 and never ask.  The
answer arrives as a resolved parameter, so no tool schema changes and a
model cannot pass it as an argument.  A decline, an unticked box, a client
without form elicitation, or an older client over stateless HTTP gets a
tool error naming the licence and the tools that need no acceptance.
Tested in ten scenarios over HTTP and in-process.

## 2. Made usable in a real client (`c477cba`, `dcceafc`)

* Claude Code 2.1 renders the form but shows its message on **one line**,
  cutting the conditions: the question and the link are the message now,
  the conditions ride on the checkbox's description.
* Claude Code speaks the **stateless** protocol (2026-07-28), so there is
  no session to remember an acceptance in.  Alex's test -- "plot the co2 at
  cabauw in 2026" -- fired several calls and the form came back ten times.
  Now an anonymous stateless caller is remembered by its address and
  client name for 12 hours, a stdio connection for its life, and a call
  arriving while the question is still on screen **waits** for that answer
  instead of asking again.  Measured: four parallel calls ask once.

## 3. Questions, with consent (`b893823`)

Alex: "Ask permission of the user to pass the question on so we can
improve the services ... anonymously, and follow the GDPR rules."  An MCP
server never sees the user's message, only tool calls, so the data tools
take an optional `question` the model fills with the request as written,
and the licence form gains a second checkbox, off unless ticked.  With
consent each call appends one JSON line -- date (not time), question,
tool, selection, client name; no address, account, token or session key.
Without consent the question is dropped unread.

## 4. Live, and rolled back (22:48 UTC)

Two problems showed in production within the evening:

* **The claude.ai connector answered the form with "decline" in about a
  second, without showing it**, so claude.ai users could get no data at
  all.
* **Every claude.ai user arrives from Anthropic's relay address**, so the
  anonymous address-plus-client key would have shared one acceptance --
  and one question consent -- among strangers.

The gate was switched off in the config (backups on CT122 in
`/root/rc/backups`).  The agreed direction, parked on 2026-11-06: mandatory
sign-in through ICOS Keycloak, acceptance and consent stored per hashed
user with a way to withdraw, form elicitation where the client can show
it, otherwise a link to a consent page; first on a test instance.

## Consequence for the ports

mcpd and the Scala server do not implement the gate, the question log or
sign-in, and **refuse to start** on a configuration asking for any of them
(added 2026-11-09), so neither can come up silently without a gate it was
asked for.
