"""The retro pages under `/r/` -- the plain look the site had before the
front-page design of 2026-10-30: the blue header, simple text on white, and
links.  A switch on each front page leads to the other.

The home page is landing.py's as it was before that design (icos-mcp
c3a820c), its links kept under `/r/`.  A store page and the data passport
page are the zarr service's OWN pages, fetched and put back into the frame
they had before it took the landing page's (zarrm9 d74fe8b): the content is
the service's, only the frame is old, so nothing here restates it.
"""

from __future__ import annotations

import html
import re

from .landing import PUBLIC_URL, _first_sentence

_CSS = """:root{--brand:#3768B8;--brand-fg:#ffffff}*{box-sizing:border-box}
body{font-family:'Open Sans',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;
color:#1d2b3a;background:#fff;line-height:1.55}
header.site{background:var(--brand);color:var(--brand-fg);display:flex;flex-wrap:wrap;align-items:center;
gap:16px;padding:14px 28px}header.site img{height:38px;display:block}
header.site .site-title{font-size:1.15rem;font-weight:700}
header.site .switch{margin-left:auto;color:var(--brand-fg);font-size:.92rem}
main{max-width:860px;margin:32px auto;padding:0 24px}
main h1{color:var(--brand);font-weight:700;font-size:1.5rem;margin:0 0 14px}
main h2{font-weight:600;font-size:1.1rem;margin:26px 0 8px}
main p.intro{font-size:1.02rem;color:#334}main a{color:var(--brand)}
code,pre{background:#eef2f8;border-radius:6px;font-family:ui-monospace,Menlo,Consolas,monospace;
font-size:.92em}code{padding:2px 6px}pre{padding:12px 14px;overflow:auto}
ul{padding-left:1.2em}li{margin:5px 0}
table{border-collapse:collapse;width:100%;font-size:.95rem}
td,th{text-align:left;vertical-align:top;padding:6px 8px;border-bottom:1px solid #e3e8f0}
th{font-weight:600;color:#334}td code{white-space:nowrap}
footer.site{max-width:860px;margin:44px auto 30px;padding:16px 24px 0;border-top:1px solid #dde3ec;
color:#5a6b80;font-size:.85rem}footer.site a{color:var(--brand)}
footer.site .build{opacity:.7;font-size:.78rem;margin-top:6px}"""


def _head(title: str, zarr_url: str, extra: str = "") -> str:
    """Everything up to and including the blue header; `title` is HTML."""
    e = html.escape
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="icon" href="{e(zarr_url)}/favicon.ico" sizes="any">
<link rel="preconnect" href="https://fonts.bunny.net">
<link rel="stylesheet" href="https://fonts.bunny.net/css?family=open-sans:400,600,700">
<style>{_CSS}</style>{extra}</head><body>
<header class="site"><a href="https://www.icos-cp.eu/" target="_blank" rel="noopener">
<img src="{e(zarr_url)}/icos_logo.svg" alt="ICOS — Integrated Carbon Observation System"></a>
<span class="site-title"><a href="/r/" style="color:inherit;text-decoration:none">ICOS Carbon Portal · MCP server</a></span>
<a class="switch" href="/">New design</a></header>"""


def _foot(zarr_url: str, version: str, licence: bool) -> str:
    e = html.escape
    lic = (
        '<p>ICOS data is licensed <a href="https://creativecommons.org/licenses/by/4.0/" '
        'target="_blank" rel="noopener">CC-BY-4.0</a> and requires proper citation of all '
        "data used — see each dataset's citation in its agent guide / Croissant metadata.</p>"
        if licence
        else ""
    )
    return f"""<footer class="site">{lic}ICOS ERIC — Carbon Portal ·
<a href="/r/">MCP server</a> ·
<a href="{e(zarr_url)}/">zarr datasets</a> ·
<a href="{e(zarr_url)}/llms.txt">llms.txt</a> ·
<a href="mailto:info@icos-cp.eu">info@icos-cp.eu</a>
<p class="build"><code>icos-mcp {e(version)}</code></p>
</footer></body></html>"""


def home(
    *,
    version: str,
    tools: list,
    prompts: list,
    datasets: list[dict],
    token_stores: bool,
    zarr_url: str,
) -> str:
    """The home page at `/r/`: the server's own tools, prompts and the stores
    an anonymous caller gets, as landing.render has them."""
    e = html.escape
    endpoint = f"{PUBLIC_URL}/mcp"
    tool_rows = "".join(
        f"<tr><td><code>{e(t.name)}</code></td><td>{e(_first_sentence(t.description))}</td></tr>"
        for t in tools
    )
    prompt_items = "".join(
        f"<li><code>{e(p.name)}</code> — {e(p.description or p.title or '')}</li>"
        for p in prompts
    )
    data_items = "".join(
        f'<li><a href="/r/{e(d["store"])}/">{e(d["title"])}</a>'
        f' <span style="color:#5a6b80">({e(d["domain"])}, <code>{e(d["store"])}</code>)</span></li>'
        for d in datasets
    )
    restricted = (
        "<p>Further collections are available to registered users with an access "
        "token, sent as <code>Authorization: Bearer &lt;token&gt;</code>; contact "
        '<a href="mailto:info@icos-cp.eu">info@icos-cp.eu</a>.</p>'
        if token_stores
        else ""
    )
    head = _head("ICOS Carbon Portal · MCP server", zarr_url)
    foot = _foot(zarr_url, version, licence=False)
    return f"""{head}
<main>
<h1>ICOS data for AI assistants</h1>
<p class="intro">This is a <a href="https://modelcontextprotocol.io/" target="_blank"
rel="noopener">Model Context Protocol</a> server: connect an AI assistant to it and the
assistant can find ICOS atmosphere, ocean and ecosystem observations, check how large a
selection is, compute statistics over it on the server, and fetch it — with the citation,
licence and DOI of the data in every answer. It is a thin layer over the
<a href="{e(zarr_url)}/">ICOS zarr service</a>: every number comes from there, and every
data answer carries its RO-Crate data passport.</p>

<h2>Connect</h2>
<p>The endpoint is <code>{e(endpoint)}</code> (streamable HTTP).</p>
<p>claude.ai, Claude Desktop and Claude Code on the web: add it as a custom connector, then
start a new chat or session.</p>
<p>Claude Code in your terminal or IDE:</p>
<pre>claude mcp add --transport http icos {e(endpoint)}</pre>
<p>A repository's <code>.mcp.json</code>, and most other clients:</p>
<pre>{{"mcpServers": {{"icos": {{"type": "http", "url": "{e(endpoint)}"}}}}}}</pre>
<p>Each, with what it needs on the web: <a href="/#connect">the connect section of the front page</a>.</p>

<h2>Datasets ({len(datasets)})</h2>
<ul>{data_items}</ul>
{restricted}

<h2>Tools ({len(tools)})</h2>
<table><tr><th>tool</th><th>what it does</th></tr>{tool_rows}</table>
<p>The ecosystem fluxes NEE, GPP and RECO are served as their preferred reference slices
(<code>NEE_VUT_REF</code>, <code>GPP_NT_VUT_REF</code>, <code>RECO_NT_VUT_REF</code>); the
other variants stay findable through <code>search_variables</code> and readable with the
xarray line <code>describe_variable</code> gives.</p>

<h2>Prompts</h2>
<ul>{prompt_items}</ul>

<h2>Citing the data</h2>
<p>ICOS data are licensed under <a href="https://creativecommons.org/licenses/by/4.0/"
target="_blank" rel="noopener">CC BY 4.0</a>. Every data answer carries its citation and DOI
— near-real-time data cite the data object they came from — and an assistant should pass
them on. What the passport records is described on the
<a href="/r/data-passport.html">data passport page</a>.</p>
</main>
{foot}"""


_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
_LD = re.compile(r'<script type="application/ld\+json">.*?</script>', re.DOTALL)
_BODY = re.compile(r'<main class="wrap page">(.*?)<p class="licence">', re.DOTALL)


def reframe(page: str, *, zarr_url: str, stores: list[str], version: str) -> str | None:
    """A zarr service reading page (a store landing, the passport page) in
    the old frame: its title, JSON-LD and body kept as the service wrote
    them; a relative link made absolute to the service; a link to a store
    page in `stores`, to the passport page or to the service's front page
    (whose datasets the retro home lists) kept under `/r/`.  None when
    the page is not one of those (the service's frame changed)."""
    title, body = _TITLE.search(page), _BODY.search(page)
    if not title or not body:
        return None
    ld = _LD.search(page)
    base = zarr_url.rstrip("/")
    text = re.sub(r'href="/(?!/)', f'href="{base}/', body.group(1))
    text = text.replace(
        f'href="{base}/data-passport.html"', 'href="/r/data-passport.html"'
    )
    text = text.replace(
        f'href="{base}/"', 'href="/r/"'
    )  # "all datasets": the retro home lists them
    for s in stores:
        text = text.replace(f'href="{base}/{s}/"', f'href="/r/{s}/"')
    head = _head(title.group(1), zarr_url, ld.group(0) if ld else "")
    foot = _foot(zarr_url, version, licence=True)
    return f"{head}\n<main>{text}</main>\n{foot}"
