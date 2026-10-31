"""The HTML landing page at `/` — what a browser sees at mcp.icos-cp.eu.

The look is the front-page design of 2026-10-30 (ICOS teal, the photo hero,
domain cards, the passport band); its photos and logos are served from
`static/` beside this module.  What the page SAYS is built from the running
server, never written by hand: the tools and prompts are the server's own
lists, the datasets are the stores the access policy offers an ANONYMOUS
caller (token stores are mentioned, never named), the counts are counted,
and the version is the server's.  What is fixed here is presentation only:
the photo and heading per domain, and which step of the workflow each tool
belongs to -- a tool this table does not know still appears, under "More".
"""

from __future__ import annotations

import html
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

PUBLIC_URL = os.environ.get("ICOS_MCP_PUBLIC_URL", "https://mcp.icos-cp.eu").rstrip("/")

STATIC = Path(__file__).with_name("static")
STATIC_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml"}

ACCENT = "#E41C63"

# Per domain: the order on the page, the card's photo, its colours and heading.
DOMAINS = {
    "atmosphere": (
        "atmosphere.jpg",
        "Drone view of an ICOS atmospheric tall tower among green fields",
        "#00ABC9",
        "#00708A",
        "Greenhouse gases in the air",
    ),
    "ecosystem": (
        "ecosystem.jpg",
        "Sunlit pine forest",
        "#5CB85C",
        "#2E7D32",
        "What land breathes in and out",
    ),
    "ocean": (
        "ocean.jpg",
        "Aerial view of a coastal marine station",
        "#00708A",
        "#00708A",
        "CO₂ at the sea surface",
    ),
}

# The workflow the tools follow; a tool not named here goes under "More".
TOOL_GROUPS = [
    ("Discover", ("list_datasets", "search_variables", "describe_variable")),
    ("Locate & size", ("find_stations", "estimate")),
    ("Compute", ("aggregate",)),
    ("Fetch & cite", ("get_data", "get_citation")),
]

TEAM = [
    (
        "team-mast.jpg",
        "Two ICOS station staff in climbing harnesses next to a measurement mast",
        "50% 50%",
    ),
    (
        "team-coast.jpg",
        "Two researchers checking an instrument enclosure at a coastal site",
        "50% 50%",
    ),
    ("team-lab.jpg", "Scientist in a laboratory", "70% 50%"),
]

_WORDS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]

_CSS = """*{box-sizing:border-box}
body{margin:0;background:#fff;color:#353535;font-family:'Open Sans',system-ui,sans-serif;
-webkit-font-smoothing:antialiased;line-height:1.5}
a{color:#00708A}a:hover{color:#005463}
code,pre{font-family:'Roboto Mono',ui-monospace,monospace}
.wrap{max-width:1200px;margin:0 auto;padding-left:24px;padding-right:24px}
.bar{display:block;width:36px;height:4px;background:#00ABC9}
.head{display:flex;flex-direction:column;gap:16px;max-width:760px}
h2{margin:0;font-weight:800;font-size:36px;line-height:1.15}
.lede{margin:0;font-size:17px;line-height:1.65;color:#555557}
header.top{background:#fff;border-bottom:1px solid #DEE2E6}
header.top nav{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;
gap:12px 24px;padding-top:14px;padding-bottom:14px}
.brand{display:flex;align-items:center;gap:16px;text-decoration:none;color:#353535}
.brand img{height:44px;width:auto;display:block}
.brand span{padding-left:16px;border-left:1px solid #DEE2E6;font-weight:600;font-size:15px;
line-height:1.3;color:#656567}.brand b{color:#00708A;font-weight:600}
.menu{display:flex;flex-wrap:wrap;gap:0 28px;font-size:15px;font-weight:600}
.menu a{color:#353535;text-decoration:none;padding:12px 0}.menu a.ext{color:#00708A}
.hero{position:relative;min-height:600px;overflow:hidden;background:#00303A;color:#fff;
display:flex;align-items:flex-end}
.hero>img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;object-position:50% 60%;display:block}
.hero .shade{position:absolute;inset:0;background:linear-gradient(90deg,rgba(0,48,58,.92) 0%,
rgba(0,48,58,.72) 45%,rgba(0,48,58,.15) 100%)}
.hero svg{position:absolute;left:0;right:0;bottom:0;width:100%;height:120px;display:block}
.hero .wrap{position:relative;width:100%;padding-top:96px;padding-bottom:112px;display:flex;
flex-direction:column;gap:24px}
.kicker{display:flex;align-items:center;gap:12px;font-size:14px;font-weight:700;
letter-spacing:.1em;text-transform:uppercase}
.hero h1{margin:0;max-width:780px;font-weight:800;font-size:clamp(36px,5.2vw,62px);
line-height:1.08;letter-spacing:-.01em}
.hero p{margin:0;max-width:620px;font-size:19px;line-height:1.6;color:#E6F2F4}
.hero p.what{font-size:16px;padding-left:14px;border-left:3px solid #F4D35E}
.hero p.what a{color:#fff;font-weight:700}
.cta{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
.btn{display:inline-flex;align-items:center;min-height:48px;padding:0 26px;border-radius:4px;
font-weight:700;font-size:16px;text-decoration:none;border:2px solid #fff;color:#fff}
.btn.primary{background:#00708A;border-color:#00708A}
.btn:hover{color:#fff;opacity:.9}
.endpoint{align-self:flex-start;padding:10px 14px;border-radius:4px;background:rgba(0,0,0,.35);
color:#fff;font-size:14px}
.facts{background:#00708A;color:#fff}
.facts .wrap{padding-top:28px;padding-bottom:28px;display:grid;
grid-template-columns:repeat(auto-fit,minmax(min(170px,100%),1fr));gap:24px}
.facts div{display:flex;flex-direction:column;gap:2px}
.facts b{font-weight:800;font-size:36px;line-height:1.1}.facts span{font-size:14px;color:#E6F2F4}
#connect{padding-top:88px;padding-bottom:72px;display:flex;flex-wrap:wrap;gap:48px}
#connect .head{flex:1 1 340px}
.snips{flex:1.4 1 480px;min-width:0;display:flex;flex-direction:column;gap:16px}
.snip{border-radius:6px;background:#F8F9FA;border:1px solid #DEE2E6;border-left:4px solid #00708A;overflow:hidden}
.snip div{display:flex;align-items:center;justify-content:space-between;gap:12px;
padding:6px 8px 6px 20px;border-bottom:1px solid #DEE2E6;font-size:14px;font-weight:700}
.snip button{min-height:44px;padding:0 16px;border-radius:4px;border:1px solid #00708A;
background:#fff;color:#00708A;font:inherit;font-size:14px;font-weight:600;cursor:pointer}
.snip pre{margin:0;padding:18px 20px;overflow-x:auto;font-size:14px;line-height:1.6}
.snip p{margin:0;padding:0 20px 16px;font-size:14px;line-height:1.5;color:#495057}
.band{background:#F8F9FA;border-top:1px solid #DEE2E6;border-bottom:1px solid #DEE2E6}
.band>.wrap,#tools{padding-top:88px;padding-bottom:88px;display:flex;flex-direction:column;gap:40px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:24px}
.card{display:flex;flex-direction:column;background:#fff;border-radius:6px;overflow:hidden;
box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card>img{width:100%;height:220px;object-fit:cover;display:block}
.card .stripe{height:4px}
.card .body{padding:24px;display:flex;flex-direction:column;gap:12px;flex-grow:1}
.card .dom{font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.card h3{margin:0;font-weight:700;font-size:22px;line-height:1.3}
.card ul{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:12px;
font-size:15px;line-height:1.45}.card li a{font-weight:600}
.card li code{font-size:13px;color:#656567}
.card .note{margin:0;font-size:14px;line-height:1.55;color:#656567}.card .note code{font-size:13px}
.restricted{margin:0;font-size:15px;color:#555557}
.groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(260px,100%),1fr));gap:32px 24px}
.group{display:flex;flex-direction:column;gap:12px}
.group h3{margin:0;display:flex;align-items:center;gap:12px;padding-bottom:12px;
border-bottom:3px solid #00708A;font-weight:700;font-size:18px}
.group h3 span{display:inline-flex;align-items:center;justify-content:center;width:28px;
height:28px;border-radius:50%;background:#00708A;color:#fff;font-size:14px}
.tool{display:flex;flex-direction:column;gap:6px;padding:16px;border-radius:6px;
background:#F8F9FA;border:1px solid #DEE2E6}
.tool code{font-size:15px;font-weight:500;color:#00708A}
.tool span,.prompt span{font-size:14px;line-height:1.55;color:#555557}
.prompts{display:flex;flex-direction:column;gap:16px;padding-top:16px}
.prompts h3{margin:0;font-weight:700;font-size:18px}
.prompts>div{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(260px,100%),1fr));gap:16px}
.prompt{padding:20px;border-radius:6px;border:1px solid #DEE2E6;border-top:4px solid #F4D35E;
display:flex;flex-direction:column;gap:6px}.prompt code{font-size:15px;font-weight:500}
.team{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(260px,100%),1fr));gap:16px}
.team img{width:100%;height:280px;object-fit:cover;border-radius:6px;display:block}
#citing{background:#005463;color:#fff}
#citing>.wrap{padding-top:88px;padding-bottom:88px;display:flex;flex-direction:column;gap:40px}
#citing .lede,#citing p{color:#E6F2F4}#citing a{color:#fff;font-weight:600}
.flow{display:flex;flex-wrap:wrap;align-items:stretch;gap:12px}
.flow>div{flex:1 1 220px;padding:24px;border-radius:6px;background:rgba(255,255,255,.08);
display:flex;flex-direction:column;gap:8px}
.flow small{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#F4D35E}
.flow b{font-weight:700;font-size:18px}.flow>div>span{font-size:14px;line-height:1.55;color:#E6F2F4}
.flow .arrow{flex:0 0 32px;padding:0;background:none;align-items:center;justify-content:center;
color:#F4D35E;font-size:24px;font-weight:700}
.flow .out{flex:1 1 260px;background:#fff;color:#353535;gap:10px}.flow .out small{color:#00708A}
.tags{display:flex;flex-wrap:wrap;gap:8px}.tags span{padding:4px 10px;border-radius:4px;
background:#E6F2F4;color:#005463;font-size:13px;font-weight:600}
#citing p.small{margin:0;font-size:15px}
footer{background:#353535;color:#DEE2E6}
footer .wrap{padding-top:36px;padding-bottom:36px;display:flex;flex-wrap:wrap;align-items:center;
justify-content:space-between;gap:20px;font-size:14px}
footer .who{display:flex;align-items:center;gap:16px}footer img{height:36px;width:auto;display:block}
footer .links{display:flex;flex-wrap:wrap;gap:20px;align-items:center}
footer a{color:#fff}footer a:hover{color:#E6F2F4}footer code{color:#ADB5BD}"""

_COPY_JS = """document.querySelectorAll('.snip button').forEach(function(b){
b.addEventListener('click',function(){var t=b.closest('.snip').querySelector('pre').textContent;
navigator.clipboard.writeText(t).then(function(){b.textContent='Copied';
setTimeout(function(){b.textContent='Copy'},1500)})})});"""


def _first_sentence(text: str) -> str:
    t = " ".join((text or "").split())
    for end in (". ", ".\n"):
        if end in t:
            return t[: t.index(end) + 1]
    return t


def _wave() -> str:
    """The rising, wavering line along the hero's foot: a measurement record."""
    pts = []
    for i in range(289):
        x = i * 5
        y = 105 - 70 * (x / 1440) - 7 * math.sin((x / 1440) * math.pi * 2 * 14)
        pts.append(f"{'M' if i == 0 else 'L'}{x} {y:.1f}")
    return " ".join(pts)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def render(
    *,
    version: str,
    tools: list,
    prompts: list,
    datasets: list[dict],
    token_stores: bool,
    zarr_url: str,
) -> str:
    e = html.escape
    endpoint = f"{PUBLIC_URL}/mcp"
    static = "/static"

    # Datasets by domain: the known domains in their order, then any other.
    by_dom: dict[str, list[dict]] = {}
    for d in datasets:
        by_dom.setdefault(d["domain"] or "other", []).append(d)
    doms = [k for k in DOMAINS if k in by_dom] + [k for k in by_dom if k not in DOMAINS]
    cards = []
    for dom in doms:
        photo, alt, stripe, label, heading = DOMAINS.get(
            dom, (None, "", "#00708A", "#00708A", dom.capitalize())
        )
        items = "".join(
            f'<li><a href="{e(zarr_url)}/{e(d["store"])}/">{e(d["title"])}</a><br>'
            f"<code>{e(d['store'])}</code></li>"
            for d in by_dom[dom]
        )
        note = (
            '<p class="note">NEE, GPP and RECO are served as their reference slices '
            "(<code>NEE_VUT_REF</code>, <code>GPP_NT_VUT_REF</code>, "
            "<code>RECO_NT_VUT_REF</code>); other variants stay findable with "
            "<code>search_variables</code> and readable with the xarray line "
            "<code>describe_variable</code> gives.</p>"
            if dom == "ecosystem"
            else ""
        )
        img = (
            f'<img src="{static}/{photo}" alt="{e(alt)}" loading="lazy">'
            if photo
            else ""
        )
        cards.append(
            f'<article class="card">{img}<div class="stripe" style="background:{stripe}"></div>'
            f'<div class="body"><span class="dom" style="color:{label}">{e(dom)}</span>'
            f"<h3>{e(heading)}</h3><ul>{items}</ul>{note}</div></article>"
        )
    known_doms = [d for d in doms if d in DOMAINS]
    dom_title = (
        f"{_WORDS[len(doms)].capitalize() if len(doms) < 10 else len(doms)} "
        f"domain{'s' if len(doms) != 1 else ''} of the carbon cycle"
    )
    restricted = (
        '<p class="restricted">Further collections are available to registered users with '
        "an access token, sent as <code>Authorization: Bearer &lt;token&gt;</code>; contact "
        '<a href="mailto:info@icos-cp.eu">info@icos-cp.eu</a>.</p>'
        if token_stores
        else ""
    )

    # Tools in the workflow's steps, every tool the server lists exactly once.
    by_name = {t.name: t for t in tools}
    groups = [
        (title, [by_name[n] for n in names if n in by_name])
        for title, names in TOOL_GROUPS
    ]
    placed = {t.name for _, ts in groups for t in ts}
    rest = [t for t in tools if t.name not in placed]
    if rest:
        groups.append(("More", rest))
    groups = [(title, ts) for title, ts in groups if ts]
    group_html = "".join(
        f'<div class="group"><h3><span>{i}</span>{e(title)}</h3>'
        + "".join(
            f'<div class="tool"><code>{e(t.name)}</code>'
            f"<span>{e(_first_sentence(t.description))}</span></div>"
            for t in ts
        )
        + "</div>"
        for i, (title, ts) in enumerate(groups, 1)
    )
    prompt_html = "".join(
        f'<div class="prompt"><code>{e(p.name)}</code>'
        f"<span>{e(p.description or p.title or '')}</span></div>"
        for p in prompts
    )
    prompts_block = (
        f'<div class="prompts"><h3>Guided prompts</h3><div>{prompt_html}</div></div>'
        if prompts
        else ""
    )
    team = "".join(
        f'<img src="{static}/{f}" alt="{e(alt)}" style="object-position:{pos}" loading="lazy">'
        for f, alt, pos in TEAM
    )
    dom_list = ", ".join(known_doms) or "none"
    snippet_cc = f"claude mcp add --transport http icos {endpoint}"
    # what a cloud environment's network allowlist must name: the server's
    # own domain and its siblings (the zarr service), as one wildcard --
    # a leading "*." matches every subdomain there
    host = urlsplit(endpoint).hostname or endpoint
    domains = "*." + host.split(".", 1)[1] if host.count(".") >= 2 else host
    snippet_json = (
        f'{{"mcpServers": {{"icos": {{"type": "http", "url": "{endpoint}"}}}}}}'
    )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ICOS Carbon Portal MCP server</title>
<meta name="description" content="A Model Context Protocol server for ICOS atmosphere, ocean and ecosystem greenhouse-gas observations, with the citation, licence and DOI in every answer.">
<link rel="icon" href="{e(zarr_url)}/favicon.ico" sizes="any">
<link rel="preconnect" href="https://fonts.bunny.net">
<link rel="stylesheet" href="https://fonts.bunny.net/css?family=open-sans:400,600,700,800|roboto-mono:400,500">
<style>{_CSS}</style></head><body>

<header class="top"><nav class="wrap" aria-label="Main">
<a class="brand" href="https://www.icos-cp.eu/"><img src="{static}/icos-logo.svg" alt="ICOS — Integrated Carbon Observation System">
<span>Carbon Portal<br><b>MCP server</b></span></a>
<div class="menu"><a href="#connect">Connect</a><a href="#domains">Datasets</a><a href="#tools">Tools</a>
<a href="#citing">Citing</a><a class="ext" href="{e(zarr_url)}/">zarr service ↗</a><a href="/r/">Retro</a></div>
</nav></header>

<section class="hero">
<img src="{static}/hero-ocean.jpg" alt="">
<div class="shade" aria-hidden="true"></div>
<svg viewBox="0 0 1440 120" preserveAspectRatio="none" aria-hidden="true"><path d="{_wave()}" fill="none" stroke="{ACCENT}" stroke-width="2.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>
<div class="wrap">
<div class="kicker"><span class="bar" style="background:{ACCENT}"></span>Model Context Protocol server</div>
<h1>ICOS greenhouse-gas data, ready for Responsible AI</h1>
<p>Find atmosphere, ocean and ecosystem data, size a selection, compute statistics on the server and fetch the data — with the citation, licence and DOI of the data sources in every answer, and rich contextual metadata on quality and provenance, fully traceable through data passports.</p>
<p class="what"><a href="https://modelcontextprotocol.io/docs/2026-07-28/getting-started/intro">MCP</a> is an open standard that lets AI assistants such as Claude or ChatGPT use outside tools and data.</p>
<div class="cta"><a class="btn primary" href="#connect">Connect your assistant</a>
<a class="btn" href="{e(zarr_url)}/">Explore the zarr datasets</a></div>
<code class="endpoint">{e(endpoint)} · streamable HTTP</code>
</div></section>

<section class="facts" aria-label="At a glance"><div class="wrap">
<div><b>{len(datasets)}</b><span>zarr dataset{"s" if len(datasets) != 1 else ""}</span></div>
<div><b>{len(known_doms)}</b><span>domain{"s" if len(known_doms) != 1 else ""}: {e(dom_list)}</span></div>
<div><b>{len(tools)}</b><span>tools and {_count(len(prompts), "guided prompt", "guided prompts")}</span></div>
<div><b>CC BY 4.0</b><span>licence, DOI in every answer</span></div>
</div></section>

<section id="connect" class="wrap">
<div class="head"><span class="bar"></span><h2>Connect in one line</h2>
<p class="lede">One endpoint for any MCP client. The server is a thin layer over the
<a href="{e(zarr_url)}/">ICOS zarr service</a>: every number comes from there, and every data
answer carries its RO-Crate data passport.</p></div>
<div class="snips">
<div class="snip"><div>claude.ai, Claude Desktop and Claude Code on the web<button type="button">Copy</button></div><pre>{e(endpoint)}</pre>
<p>Add a custom connector with this URL at <a href="https://claude.ai/customize/connectors">claude.ai &rsaquo; Connectors</a>
(on Team and Enterprise plans an admin adds it), then start a new chat or session.
A connector reaches Claude Code on the web sessions too, without any network setting.</p></div>
<div class="snip"><div>Claude Code in your terminal or IDE<button type="button">Copy</button></div><pre>{e(snippet_cc)}</pre>
<p>For the sessions you start on that machine from then on. In a session on the web it does
not last: the session's machine is replaced.</p></div>
<div class="snip"><div>A repository's .mcp.json, and other clients<button type="button">Copy</button></div><pre>{e(snippet_json)}</pre>
<p>At the root of a repository, on the branch a session starts from, every Claude Code session
on it loads the server. On the web the session must also be allowed to reach
<code>{e(domains)}</code>, this server and the zarr service its download links point to:
in the cloud environment's settings set <b>Network access</b> to <i>Custom</i> with
<code>{e(domains)}</code> under <b>Allowed domains</b> (or to <i>Full</i>) &mdash; the default
<i>Trusted</i> list does not include it. Not needed with the connector above.
Check with <code>/mcp</code>.
Most other MCP clients take the same block in their own settings.</p></div>
</div></section>

<section id="domains" class="band"><div class="wrap">
<div class="head"><span class="bar"></span><h2>{e(dom_title)}</h2>
<p class="lede">What is in the air, what land ecosystems exchange with it, and what the ocean takes up — measured with standardised methods across the ICOS station network.</p></div>
<div class="cards">{"".join(cards)}</div>
{restricted}
</div></section>

<section id="tools" class="wrap">
<div class="head"><span class="bar"></span><h2>Find, size, compute, fetch, cite</h2>
<p class="lede">{e(_WORDS[len(tools)].capitalize() if len(tools) < 10 else str(len(tools)))} tools that follow how a scientist works with data — and keep heavy computation on the server, next to the data.</p></div>
<div class="groups">{group_html}</div>
{prompts_block}
</section>

<section class="band" aria-label="Behind the data"><div class="wrap">
<div class="head"><span class="bar" style="background:{ACCENT}"></span><h2>Behind every number, a station team</h2>
<p class="lede">The values an assistant retrieves here are produced by people in the field and the lab, following common protocols and quality control across the ICOS network.</p></div>
<div class="team">{team}</div>
</div></section>

<section id="citing"><div class="wrap">
<div class="head"><span class="bar" style="background:#F4D35E"></span><h2>Every number carries its passport</h2>
<p class="lede">Climate decisions need traceable evidence. Each data answer includes the citation, licence and DOI from its RO-Crate data passport — near-real-time data cite the exact data object they came from — so an assistant can pass them on.</p></div>
<div class="flow">
<div><small>Source</small><b>ICOS zarr service</b><span>Quality-controlled observations, one store per dataset.</span></div>
<div class="arrow" aria-hidden="true">→</div>
<div><small>This server</small><b>ICOS MCP</b><span>Selects, aggregates and attaches the data passport.</span></div>
<div class="arrow" aria-hidden="true">→</div>
<div class="out"><small>Every answer</small><b>Data + citation</b><div class="tags"><span>citation</span><span>CC BY 4.0</span><span>DOI</span></div></div>
</div>
<p class="small">ICOS data are licensed under <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>.
What the passport records: <a href="{e(zarr_url)}/data-passport.html">the data passport page</a>.</p>
</div></section>

<footer><div class="wrap">
<div class="who"><img src="{static}/icos-logo-footer.png" alt="ICOS"><span>ICOS ERIC — Carbon Portal</span></div>
<div class="links"><a href="{e(zarr_url)}/">zarr datasets</a><a href="{e(zarr_url)}/llms.txt">llms.txt</a>
<a href="mailto:info@icos-cp.eu">info@icos-cp.eu</a><code>icos-mcp {e(version)}</code></div>
</div></footer>
<script>{_COPY_JS}</script>
</body></html>"""
