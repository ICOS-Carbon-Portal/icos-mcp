"""Run the evaluation set (eval/questions.json) -- docs/plan.md, Quality 3.

For each question:

* the TRUTH, computed now from the proxy itself (Arrow or /summary), so
  the set does not go stale as the data grow -- station sets and
  citations come from the server's own readers, everything numeric
  straight from the proxy;
* an AGENT run, every tool call recorded.  The default agent is OUR OWN
  model -- Qwen (qwen3-8-27b) served by SGLang on the Nvidia Spark
  (icoscp-spark1:30000, OpenAI-compatible; Alex, 2026-10-30) -- driven by
  the loop below over the MCP server's own tool list, schemas and
  instructions, exactly what an MCP client hands a model.  `--agent
  claude` runs a headless Claude Code session instead, with ONLY this
  server's tools, in an empty directory so no project instructions leak;
* a GRADE in two parts -- ROUTE (did it use an acceptable variable,
  station and QC choice?) and ANSWER (is the truth in its final text,
  within the question's tolerance?).  Questions whose truth is a
  behaviour (a refusal handled, an empty result said so) are marked for
  a human to read.

    uv run python eval/run.py --truth-only            # the truths, no agent
    uv run python eval/run.py --only atm-01,eco-01    # a few, with Qwen
    uv run python eval/run.py                         # all of them
    uv run python eval/run.py --agent claude          # Claude Code instead

QWEN_BASE_URL and QWEN_MODEL override the endpoint and the model.

Results go to eval/results/<UTC stamp>.jsonl, one line per question.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time

import httpx
import numpy as np
from mcp import Client
from pyarrow import ipc

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
from icos_mcp import server

logging.disable(logging.INFO)
PROXY = server.POLICY.proxy

# ---------------------------------------------------------------- truth


def _arrow(params: dict):
    r = httpx.get(f"{PROXY}/query", params={**params, "format": "arrow"}, timeout=600)
    if r.status_code != 200:
        return None, f"{r.status_code} {r.text[:120]}"
    with ipc.open_stream(io.BytesIO(r.content)) as rd:
        return rd.read_all(), None


def _summary(params: dict) -> dict:
    keep = {
        k: v
        for k, v in params.items()
        if k not in ("station", "cruise", "apply_qc", "height_min", "height_max")
    }
    return httpx.get(f"{PROXY}/summary", params=keep, timeout=300).json()


async def _tool(name: str, args: dict):
    async with Client(server.mcp) as c:
        r = await c.call_tool(name, args)
        return r


def _when(v: str) -> str:
    """'-14d' is fourteen days before today (UTC): the NRT questions move."""
    if isinstance(v, str) and v.startswith("-") and v.endswith("d"):
        return str(dt.datetime.now(dt.UTC).date() - dt.timedelta(days=int(v[1:-1])))
    return v


def truth(q: dict):
    t = q["truth"]
    stat = t["stat"]
    if "query" in t:
        params = {k: _when(v) for k, v in t["query"].items()}
        if t.get("endpoint") == "summary":
            s = _summary(params)
            return s.get(stat), None
        if stat in ("refusal_handled",):
            r = httpx.get(
                f"{PROXY}/query", params={**params, "max_rows": 1}, timeout=60
            )
            return f"proxy answers {r.status_code}", None
        if stat == "passport_object_citation":
            r = httpx.get(f"{PROXY}/query", params=params, timeout=60)
            p = json.loads(r.text.strip().splitlines()[-1])["_passport"]
            cits = [
                n.get("citation")
                for n in p["@graph"]
                if isinstance(n.get("@type"), list) and "icos:DataObject" in n["@type"]
            ]
            return cits[0] if cits else None, None
        tab, err = _arrow(params)
        if tab is None:
            return None, err
        if stat == "count":
            return tab.num_rows, None
        if stat == "download_url":
            return f"{tab.num_rows} rows", None
        col = t["column"]
        v = np.asarray(tab.column(col).to_numpy(zero_copy_only=False), dtype=float)
        ok = np.isfinite(v)
        if stat == "last_time":
            tcol = next(c for c in tab.column_names if c.startswith("time"))
            ok_t = [tab.column(tcol)[i].as_py() for i in range(tab.num_rows) if ok[i]]
            return (str(ok_t[-1])[:16] if ok_t else None), None
        if stat == "mean" and t.get("also"):
            # several right answers: the same selection over each named store
            vals = [float(v[ok].mean())]
            for alt in t["also"]:
                tb, _ = _arrow({**params, **alt})
                x = np.asarray(
                    tb.column(alt.get("column", col)).to_numpy(zero_copy_only=False),
                    dtype=float,
                )
                vals.append(float(x[np.isfinite(x)].mean()))
            return vals, None
        if stat == "mean":
            return (float(v[ok].mean()) if ok.any() else None), None
        if stat == "sum":
            return float(v[ok].sum()), None
        if stat == "min_max":
            return (
                [float(v[ok].min()), float(v[ok].max())] if ok.any() else None
            ), None
        if stat in ("first", "sign"):
            x = float(v[ok][0]) if ok.any() else None
            return (
                ("sink" if x < 0 else "source")
                if stat == "sign" and x is not None
                else x
            ), None
        if stat == "mean_by_station":
            st = np.asarray(tab.column("station").to_pylist())
            return {s: float(v[ok & (st == s)].mean()) for s in sorted(set(st))}, None
        if stat == "argmax_time":
            tcol = next(c for c in tab.column_names if c.startswith("time"))
            i = int(np.nanargmax(v))
            return str(tab.column(tcol)[i].as_py())[:7], None
        return None, f"no truth rule for {stat}"
    # behaviours read through the server's own tools
    r = asyncio.run(_tool(t["tool"], t["args"]))
    d = r.structured_content or {}
    if stat == "station_set":
        return sorted(s["id"] for s in d.get("stations", [])), None
    if stat == "exists":
        return bool(d.get("stations")), None
    if stat == "empty":
        return not d.get("stations"), None
    if stat == "doi":
        return d.get("doi"), None
    if stat == "domains":
        return sorted({x["domain"] for x in d.get("datasets", [])}), None
    if stat == "refusal_handled":
        return "refused" if r.is_error else "answered", None
    if stat == "time_max_recent":
        return d.get("time_max"), None
    return None, f"no truth rule for {stat}"


# ---------------------------------------------------------------- the agents


QWEN_URL = os.environ.get("QWEN_BASE_URL", "http://icoscp-spark1:30000").rstrip("/")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen3-8-27b")
TOOL_RESULT_MAX = 12_000  # characters of one tool result handed back to the model
TURNS_MAX = 14
# what a real MCP client tells its model and a bare endpoint does not: the date
TODAY = f"\n\nToday is {dt.datetime.now(dt.UTC).date()} (UTC)."


async def _qwen(question: str, model: str, think: bool) -> dict:
    """An OpenAI-style tool loop over the MCP server: the model sees the
    server's instructions as the system prompt and its tools as functions;
    each tool call goes through MCP and its result comes back as a tool
    message.  Ends at the first answer without a tool call."""
    calls, answer, usage = [], "", {"prompt": 0, "completion": 0}
    t0 = time.time()
    async with Client(server.mcp) as c, httpx.AsyncClient(timeout=600) as http:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.input_schema,
                },
            }
            for t in (await c.list_tools()).tools
        ]
        messages = [
            {"role": "system", "content": (c.instructions or "") + TODAY},
            {"role": "user", "content": question},
        ]
        for _ in range(TURNS_MAX):
            r = await http.post(
                f"{QWEN_URL}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.2,
                    "max_tokens": 4096,
                    "chat_template_kwargs": {"enable_thinking": think},
                },
            )
            r.raise_for_status()
            d = r.json()
            u = d.get("usage") or {}
            usage["prompt"] += u.get("prompt_tokens", 0)
            usage["completion"] += u.get("completion_tokens", 0)
            msg = d["choices"][0]["message"]
            tcs = msg.get("tool_calls") or []
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    **({"tool_calls": tcs} if tcs else {}),
                }
            )
            if not tcs:
                answer = msg.get("content") or ""
                break
            for tc in tcs:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                calls.append({"tool": name, "input": args})
                res = await c.call_tool(name, args)
                text = (
                    json.dumps(res.structured_content, ensure_ascii=False)
                    if res.structured_content is not None
                    else " ".join(getattr(x, "text", "") for x in res.content)
                )
                if res.is_error:
                    text = "ERROR: " + text
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", name),
                        "content": text[:TOOL_RESULT_MAX],
                    }
                )
        else:
            answer = "(no answer within the turn limit)"
    return {
        "calls": calls,
        "answer": answer,
        "seconds": round(time.time() - t0, 1),
        "tokens": usage,
        "cost_usd": None,
        "stderr": "",
    }


def agent(
    question: str, model: str | None, kind: str = "qwen", think: bool = False
) -> dict:
    if kind == "qwen":
        return asyncio.run(_qwen(question, model or QWEN_MODEL, think))
    return _claude(question, model)


def _claude(question: str, model: str | None) -> dict:
    cfg = {
        "mcpServers": {
            "icos": {
                "command": "uv",
                "args": ["--directory", os.path.join(HERE, ".."), "run", "icos-mcp"],
                "env": {k: v for k, v in os.environ.items() if k.startswith("ICOS_")},
            }
        }
    }
    with tempfile.TemporaryDirectory() as d:
        cpath = os.path.join(d, "mcp.json")
        with open(cpath, "w") as f:
            json.dump(cfg, f)
        cmd = [
            "claude",
            "-p",
            question,
            "--mcp-config",
            cpath,
            "--strict-mcp-config",
            "--allowedTools",
            "mcp__icos",
            "--output-format",
            "stream-json",
            "--verbose",
            "--disallowedTools",
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebFetch",
            "WebSearch",
            "Monitor",
            "Task",
            "Agent",
            "NotebookEdit",
        ]
        if model:
            cmd += ["--model", model]
        t0 = time.time()
        p = subprocess.run(
            cmd, check=False, cwd=d, capture_output=True, text=True, timeout=900
        )
    calls, final, cost = [], "", None
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    calls.append(
                        {"tool": c["name"].split("__")[-1], "input": c.get("input", {})}
                    )
        if ev.get("type") == "result":
            final = ev.get("result") or ""
            cost = ev.get("total_cost_usd")
    return {
        "calls": calls,
        "answer": final,
        "seconds": round(time.time() - t0, 1),
        "cost_usd": cost,
        "stderr": p.stderr[-400:] if p.returncode else "",
    }


# ---------------------------------------------------------------- grading


def _numbers(text: str) -> list[float]:
    return [
        float(x.replace(",", ""))
        for x in re.findall(r"-?\d[\d,]*\.?\d*(?:[eE]-?\d+)?", text)
        if re.search(r"\d", x)
    ]


def grade_route(q: dict, calls: list[dict]) -> tuple[bool | None, str]:
    e = q["expect"]
    data = [
        c["input"]
        for c in calls
        if c["tool"]
        in (
            "get_data",
            "estimate",
            "aggregate",
            "get_citation",
            "describe_variable",
            "find_stations",
        )
    ]
    notes = []
    ok = True
    if e.get("variable") and not any(c.get("id") in e["variable"] for c in data):
        ok = False
        notes.append(f"variable not used (wanted {e['variable'][0]})")
    if e.get("station"):
        used = {
            s
            for c in calls
            if c["tool"] in ("get_data", "estimate", "aggregate")
            for s in (c["input"].get("station") or [])
        }
        if not used & set(e["station"]):
            ok = False
            notes.append(
                f"station not used (wanted one of {e['station'][:3]}, used {sorted(used)[:4]})"
            )
    if e.get("apply_qc") and not any(
        c["input"].get("apply_qc")
        for c in calls
        if c["tool"] in ("get_data", "estimate", "aggregate")
    ):
        ok = False
        notes.append("apply_qc not set")
    if not e.get("variable") and not e.get("station"):
        return None, "no route expectation"
    return ok, "; ".join(notes)


def grade_answer(q: dict, want, answer: str) -> tuple[bool | None, str]:
    t = q["truth"]
    stat, tol = t["stat"], t.get("tolerance", 0)
    nums = _numbers(answer)
    low = answer.lower()
    if want is None:
        return None, "no truth"
    if stat == "last_time":
        return want[:10] in answer, f"truth {want}"
    if (
        want == 0
        and stat == "count"
        and re.search(
            r"\b(no data|no (co2|ch4|observations|rows|values|measurements)|not covered|"
            r"(starts|begins) in)\b",
            low,
        )
    ):
        return True, "an empty selection said so"
    if isinstance(want, float | int) and not isinstance(want, bool):
        tol = tol or max(1.0, abs(want) * 0.01)
        hit = any(abs(n - want) <= tol for n in nums)
        return hit, f"truth {want:.4g}"
    if stat == "mean" and isinstance(want, list):
        hit = any(any(abs(n - w) <= tol for n in nums) for w in want)
        return hit, f"truth any of {[round(w, 2) for w in want]}"
    if stat == "min_max":
        return all(
            any(abs(n - w) <= max(tol, 1e-6) for n in nums) for w in want
        ), f"truth {want}"
    if stat == "mean_by_station":
        return all(
            any(abs(n - w) <= tol for n in nums) for w in want.values()
        ), f"truth {want}"
    if stat == "station_set":
        # an atmosphere station is several inlets (GAT30 ... GAT341): naming
        # the station, by its trigram, names them all
        def named(sid):
            tri = re.match(r"[A-Z]{3}(?=\d)", sid)
            return sid.lower() in low or bool(
                tri and re.search(rf"\b{tri.group(0)}\b", answer)
            )

        found = [s for s in want if named(s)]
        return len(found) >= 0.8 * len(want), f"{len(found)}/{len(want)} stations named"
    if stat == "download_url":
        return (
            "format=csv" in answer
            or "format=arrow" in answer
            or "format=parquet" in answer
        ), "a download link given"
    if stat in ("doi", "passport_object_citation"):
        key = (
            want.split("doi.org/")[-1]
            if stat == "doi"
            else want.split("hdl.handle.net/")[-1]
        )
        return key.lower() in low, "citation key " + key[:40]
    if stat == "sign":
        return want in low, f"truth {want}"
    if stat == "argmax_time":
        return want in answer or dt.date.fromisoformat(want + "-01").strftime(
            "%B"
        ).lower() in low, f"truth {want}"
    if stat == "domains":
        return all(d in low for d in want), f"truth {want}"
    return None, f"read by hand ({stat}: {want})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--truth-only", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--agent", choices=("qwen", "claude"), default="qwen")
    ap.add_argument(
        "--think", action="store_true", help="Qwen: reasoning on (off by default)"
    )
    a = ap.parse_args()
    with open(os.path.join(HERE, "questions.json"), encoding="utf-8") as f:
        qs = json.load(f)["questions"]
    if a.only:
        keep = set(a.only.split(","))
        qs = [q for q in qs if q["id"] in keep]
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = open(os.path.join(HERE, "results", f"{stamp}.jsonl"), "w")  # noqa: SIM115
    tally = {"route": [0, 0], "answer": [0, 0]}
    for q in qs:
        want, err = truth(q)
        row = {"id": q["id"], "truth": want, "truth_error": err}
        line = f"{q['id']:7s} truth={str(want)[:60]!s:62s}"
        if not a.truth_only:
            r = agent(q["question"], a.model, a.agent, a.think)
            ro, rn = grade_route(q, r["calls"])
            ao, an = grade_answer(q, want, r["answer"])
            row.update(r, route_ok=ro, route_note=rn, answer_ok=ao, answer_note=an)
            for k, v in (("route", ro), ("answer", ao)):
                if v is not None:
                    tally[k][1] += 1
                    tally[k][0] += int(v)
            mark = {True: "ok ", False: "BAD", None: " ? "}
            line += (
                f" route {mark[ro]} answer {mark[ao]} {r['seconds']:5.0f}s"
                f" {len(r['calls'])} calls  {rn or ''} {an}"
            )
        print(line, flush=True)
        out.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    out.close()
    if not a.truth_only:
        print(
            f"route {tally['route'][0]}/{tally['route'][1]}, "
            f"answer {tally['answer'][0]}/{tally['answer'][1]}; results in eval/results/{stamp}.jsonl"
        )


if __name__ == "__main__":
    main()
