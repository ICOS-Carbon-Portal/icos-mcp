"""The HTML landing page at `/` — what a browser sees at mcp.icos-cp.eu.

Built from the running server, never written by hand: the tools and prompts
are the server's own lists, the datasets are the stores the access policy
offers an ANONYMOUS caller (token stores are mentioned, never named), and
the version is the server's.  The look is zarr.icos-cp.eu's landing page:
the same header, colour and type, so the two read as one service.
"""

from __future__ import annotations

import html
import os

PUBLIC_URL = os.environ.get("ICOS_MCP_PUBLIC_URL", "https://mcp.icos-cp.eu").rstrip("/")

_CSS = """:root{--brand:#3768B8;--brand-fg:#ffffff}*{box-sizing:border-box}
body{font-family:'Open Sans',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;
color:#1d2b3a;background:#f6f8fb;line-height:1.55}
header.site{background:var(--brand);color:var(--brand-fg);display:flex;align-items:center;
gap:16px;padding:14px 28px}header.site img{height:38px;display:block}
header.site .site-title{font-size:1.15rem;font-weight:700}
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
color:#5a6b80;font-size:.85rem}footer.site a{color:var(--brand)}"""


def _first_sentence(text: str) -> str:
    t = " ".join((text or "").split())
    for end in (". ", ".\n"):
        if end in t:
            return t[: t.index(end) + 1]
    return t


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
    tool_rows = "".join(
        f"<tr><td><code>{e(t.name)}</code></td><td>{e(_first_sentence(t.description))}</td></tr>"
        for t in tools
    )
    prompt_items = "".join(
        f"<li><code>{e(p.name)}</code> — {e(p.description or p.title or '')}</li>"
        for p in prompts
    )
    data_items = "".join(
        f'<li><a href="{e(zarr_url)}/{e(d["store"])}/">{e(d["title"])}</a>'
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
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ICOS Carbon Portal · MCP server</title>
<link rel="icon" href="{e(zarr_url)}/favicon.ico" sizes="any">
<link rel="preconnect" href="https://fonts.bunny.net">
<link rel="stylesheet" href="https://fonts.bunny.net/css?family=open-sans:400,600,700">
<style>{_CSS}</style></head><body>
<header class="site"><a href="https://www.icos-cp.eu/" target="_blank" rel="noopener">
<img src="{e(zarr_url)}/icos_logo.svg" alt="ICOS — Integrated Carbon Observation System"></a>
<span class="site-title">ICOS Carbon Portal · MCP server</span></header>
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
<p>Claude Code:</p>
<pre>claude mcp add --transport http icos {e(endpoint)}</pre>
<p>Claude Desktop, VS Code and other clients take the URL in their MCP configuration:</p>
<pre>{{"mcpServers": {{"icos": {{"type": "http", "url": "{e(endpoint)}"}}}}}}</pre>

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
-- near-real-time data cite the data object they came from — and an assistant should pass
them on. What the passport records is described on the
<a href="{e(zarr_url)}/data-passport.html">data passport page</a>.</p>
</main>
<footer class="site">ICOS ERIC — Carbon Portal ·
<a href="{e(zarr_url)}/">zarr datasets</a> ·
<a href="{e(zarr_url)}/llms.txt">llms.txt</a> ·
<a href="mailto:info@icos-cp.eu">info@icos-cp.eu</a>
<div style="opacity:.7;font-size:.78rem;margin-top:6px">icos-mcp {e(version)}</div>
</footer></body></html>"""
