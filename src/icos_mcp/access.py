"""Which stores this server offers, and to whom.

The proxy behind this server may serve more than the public: inside
CT122, zpd answers for every store on the machine -- the FLUXNET shuttle,
NOAA GlobalView, AGAGE -- and its own host map is the only thing that
keeps those off zarr.icos-cp.eu.  So the MCP server keeps its OWN list,
and it is DEFAULT-DENY: a store no rule names is invisible, whatever the
proxy would serve.

``ICOS_MCP_CONFIG`` names a JSON file; without one the rules are exactly
the public stores of zarr.icos-cp.eu.  The file:

    {
      "proxy": "http://127.0.0.1:8081",          # where requests go
      "proxy_host": "zarr-proxy",                # Host header sent (optional)
      "public_url": "https://zarr.icos-cp.eu",   # base of download links
      "forward_client_ip": true,                 # caller -> proxy as X-Real-IP
      "stores": [                                # first matching rule wins
        {"match": "icos-*",  "access": "public"},
        {"match": "socat*",  "access": "public"},
        {"match": "fluxnet-shuttle*", "access": "token",
         "tokens_file": "/etc/icos-mcp/shuttle.sha256",
         "public_url": "https://fluxnet-shuttle-zarr.icos-cp.eu"}
      ]
    }

``match`` is a shell pattern over the store name, with or without
``.zarr``.  A ``token`` store is offered only to a caller presenting a
token whose SHA-256 is listed -- one hex digest per line in
``tokens_file``, or comma-separated in the environment variable
``tokens_env`` names.  Plain tokens are never stored: ``icos-mcp
--hash-token TOKEN`` prints the line to add.  Over HTTP the caller sends
``Authorization: Bearer <token>``; over stdio the server's own
``ICOS_MCP_TOKEN`` is the caller's.

A hidden store is reported exactly as one that does not exist.

An ``oauth`` section makes signing in REQUIRED over HTTP (auth.py); the
store tokens above count as signed in.  It also allows a ``group`` rule:

    {"match": "agage*", "access": "group", "groups": ["agage"],
     "public_url": "https://agage-zarr.icos-cp.eu"}

offers the store to a caller signed in as a member of a listed Keycloak
group, or of a subgroup of one.  Group names are paths below /apps/mcp
("agage" is /apps/mcp/agage), as the realm's mapper sends them.
Membership is managed in Keycloak, not here.  A ``group`` rule may list store tokens as well, for scripts.

``"licence_gate": true`` (or ``ICOS_MCP_LICENCE_GATE=1`` without a config)
makes the tools that hand over DATA -- get_data and aggregate -- ask the
user to accept the ICOS data licence first, through MCP elicitation.  The
metadata tools are CC0 and never ask.  Off by default.

``"questions_log": "/path/questions.jsonl"`` (or ``ICOS_MCP_QUESTIONS_LOG``
without a config) adds to that licence form an OPTIONAL consent, off unless
ticked: share my questions, anonymously, to improve the service.  With it, the
data tools keep the ``question`` argument the model passes (the user's request
as they wrote it) as one JSON line -- date, question, tool, selection, client
name, and nothing that identifies the caller.  Without consent the question is
dropped.  Needs the licence gate: the consent is asked in its form.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field

from icos_mcp import auth

DEFAULT_PROXY = "https://zarr.icos-cp.eu"
# zarr.icos-cp.eu's own host map (deploy/ct122 zpd-shadow.sh), 2026-10-30
DEFAULT_PUBLIC = [
    "icos-obspack*",
    "icos-socat*",
    "icos-fluxnet*",
    "socat*",
    "icos-eco-nrt*",
]


@dataclass
class Rule:
    match: str
    access: str  # "public" | "token" | "group"
    tokens: frozenset[str] = frozenset()
    public_url: str | None = None
    groups: frozenset[str] = frozenset()

    def admits(self, groups: frozenset[str]) -> bool:
        return any(g == r or g.startswith(r + "/") for g in groups for r in self.groups)


@dataclass
class Policy:
    proxy: str
    proxy_host: str | None
    public_url: str
    rules: list[Rule] = field(default_factory=list)
    # pass the caller's address to the proxy as X-Real-IP, so its passports
    # and its Matomo logging see the END USER and not this server -- only
    # where the proxy believes that header from us (zpd on loopback)
    forward_client_ip: bool = False
    oauth: auth.OAuth | None = None
    # get_data and aggregate ask the user to accept the ICOS data licence
    licence_gate: bool = False
    # where consented questions go (JSON lines); empty: the form does not ask
    questions_log: str = ""

    @property
    def store_tokens(self) -> frozenset[str]:
        return frozenset().union(*(r.tokens for r in self.rules))

    def rule_for(self, store: str) -> Rule | None:
        name = store.removesuffix(".zarr")
        for r in self.rules:
            pat = r.match.removesuffix(".zarr")
            if fnmatch.fnmatchcase(name, pat):
                return r
        return None

    def allowed(
        self, store: str, token: str | None, groups: frozenset[str] = frozenset()
    ) -> bool:
        """``groups``: the caller's Keycloak groups, from a VERIFIED token
        only (auth.py) -- never from anything the caller merely asserts."""
        r = self.rule_for(store)
        if r is None:
            return False
        if r.access == "public":
            return True
        if r.access == "group" and r.admits(groups):
            return True
        if token and r.tokens:
            h = hash_token(token)
            return any(hmac.compare_digest(h, t) for t in r.tokens)
        return False

    def url_for(self, store: str) -> str:
        r = self.rule_for(store)
        return (r.public_url if r and r.public_url else self.public_url).rstrip("/")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _tokens(spec: dict) -> frozenset[str]:
    out: set[str] = set()
    if spec.get("tokens_file"):
        with open(spec["tokens_file"], encoding="utf-8") as f:
            out |= {
                ln.strip().lower() for ln in f if ln.strip() and not ln.startswith("#")
            }
    if spec.get("tokens_env"):
        out |= {
            t.strip().lower()
            for t in os.environ.get(spec["tokens_env"], "").split(",")
            if t.strip()
        }
    return frozenset(out)


def load() -> Policy:
    path = os.environ.get("ICOS_MCP_CONFIG", "")
    if not path:
        proxy = os.environ.get("ICOS_PROXY", DEFAULT_PROXY).rstrip("/")
        return Policy(
            proxy=proxy,
            proxy_host=None,
            public_url=os.environ.get("ICOS_PUBLIC_URL", proxy),
            rules=[Rule(m, "public") for m in DEFAULT_PUBLIC],
            licence_gate=os.environ.get("ICOS_MCP_LICENCE_GATE") == "1",
            questions_log=os.environ.get("ICOS_MCP_QUESTIONS_LOG", ""),
        )
    with open(path, encoding="utf-8") as f:
        c = json.load(f)
    rules = []
    for s in c.get("stores", []):
        if s.get("access") not in ("public", "token", "group"):
            raise ValueError(
                f"{path}: store rule {s.get('match')!r}: access must be "
                f"'public', 'token' or 'group'"
            )
        groups = frozenset(g.strip("/") for g in s.get("groups", []))
        r = Rule(s["match"], s["access"], _tokens(s), s.get("public_url"), groups)
        if r.access == "group" and not r.groups:
            raise ValueError(f"{path}: group store {r.match!r} lists no groups")
        if r.access == "group" and not c.get("oauth"):
            raise ValueError(
                f"{path}: group store {r.match!r} needs an oauth section -- "
                f"without sign-in nobody has groups, so it would be unreachable"
            )
        if r.access == "token" and not r.tokens:
            raise ValueError(
                f"{path}: token store {r.match!r} lists no tokens -- it would be "
                f"unreachable; remove it or give it tokens_file / tokens_env"
            )
        rules.append(r)
    proxy = c.get("proxy", DEFAULT_PROXY).rstrip("/")
    return Policy(
        proxy=proxy,
        proxy_host=c.get("proxy_host"),
        public_url=c.get("public_url", proxy),
        rules=rules,
        forward_client_ip=bool(c.get("forward_client_ip", False)),
        oauth=auth.from_config(c.get("oauth"), path),
        licence_gate=bool(c.get("licence_gate", False)),
        questions_log=str(c.get("questions_log", "")),
    )
