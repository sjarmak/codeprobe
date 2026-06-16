"""Served, interactive arm-vs-arm comparison trace viewer (codeprobe-00e).

Builds on :mod:`codeprobe.analysis.run_explorer` (loader + structural
validity flags) to render a side-by-side per-task comparison across >=2
arms/configs, plus a stdlib-only ``--serve`` mode so a human can verify a
run (e.g. the 9tk Sourcegraph comparison) over a port. Modeled on the
EnterpriseBench fable-vs-sonnet comparison viewer.

This is a verification UI over already-computed results — it changes no
reward/observable score. ZFC: every comparison/validity signal is
STRUCTURAL only (status / error_category / hit_max_turns / tool_call_count /
reward / cost / tokens / num_turns / zero-mcp-calls). No semantic
thresholds.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from codeprobe.analysis.run_explorer import (
    build_arm_summaries,
    compute_validity_flags,
    load_run_trials,
    render_html,
)

DEFAULT_PORT = 8766
_MCP_PREFIX = "mcp__"


@dataclass(frozen=True)
class ArmComparison:
    """Per-arm rollup for the comparison summary bar (structural signals)."""

    label: str
    total: int
    mean_reward: float
    total_cost: float | None
    error_count: int
    zero_mcp_count: int


def _arm_of(trial: dict) -> str:
    return str(trial.get("config", "?"))


def _is_error(trial: dict) -> bool:
    return trial.get("status") in ("error", "failed")


def _zero_mcp_calls(trial: dict) -> bool:
    """True when the trial made no ``mcp__*`` tool calls (structural).

    Reads tool_use_by_name; None/absent means usage was not captured, which
    is NOT counted (no false signal). This surfaces the 9tk with-sg-narrow
    'declined the Sourcegraph surface' finding without any semantic judgment
    about whether the arm *should* have called it.
    """
    usage = trial.get("tool_use_by_name")
    if not isinstance(usage, dict):
        return False
    return not any(str(name).startswith(_MCP_PREFIX) for name in usage)


def build_arm_comparisons(trials: list[dict]) -> list[ArmComparison]:
    """Per-arm summary rollups, reusing run_explorer's reward/cost math."""
    # build_arm_summaries delegates mean reward / mean cost to stats; we only
    # add the structural counts (errors, zero-mcp) here.
    summaries = {s.label: s for s in build_arm_summaries(trials)}
    by_arm: dict[str, list[dict]] = {}
    for t in trials:
        by_arm.setdefault(_arm_of(t), []).append(t)

    out: list[ArmComparison] = []
    for label in sorted(by_arm):
        arm_trials = by_arm[label]
        s = summaries.get(label)
        mean_cost = s.mean_cost if s is not None else None
        total_cost = (
            mean_cost * len(arm_trials) if mean_cost is not None else None
        )
        out.append(
            ArmComparison(
                label=label,
                total=len(arm_trials),
                mean_reward=s.mean_reward if s is not None else 0.0,
                total_cost=total_cost,
                error_count=sum(1 for t in arm_trials if _is_error(t)),
                zero_mcp_count=sum(1 for t in arm_trials if _zero_mcp_calls(t)),
            )
        )
    return out


def _reward(trial: dict) -> float | None:
    r = trial.get("reward")
    if r is None:
        r = trial.get("score")
    return float(r) if isinstance(r, (int, float)) else None


def build_task_matrix(trials: list[dict]) -> dict:
    """Build the per-task × arm comparison matrix.

    Returns ``{"arms": [...], "rows": [{task_id, cells: {arm: cell}, delta}]}``
    where each cell aggregates that (task, arm)'s repeats: mean reward, repeat
    count, error count, the union of structural validity flags, and the raw
    per-repeat trials for drill-in. ``delta`` is True when the arms' mean
    rewards are not all equal (a structural difference, not a threshold).
    """
    arms = sorted({_arm_of(t) for t in trials})
    by_key: dict[tuple[str, str], list[dict]] = {}
    for t in trials:
        by_key.setdefault((str(t.get("task_id", "?")), _arm_of(t)), []).append(t)

    task_ids = sorted({str(t.get("task_id", "?")) for t in trials})
    rows: list[dict] = []
    for task_id in task_ids:
        cells: dict[str, dict] = {}
        means: list[float] = []
        for arm in arms:
            reps = by_key.get((task_id, arm), [])
            rewards = [r for r in (_reward(t) for t in reps) if r is not None]
            mean_reward = sum(rewards) / len(rewards) if rewards else None
            if mean_reward is not None:
                means.append(mean_reward)
            flags: set[str] = set()
            for t in reps:
                flags.update(compute_validity_flags(t))
            cells[arm] = {
                "mean_reward": mean_reward,
                "repeats": len(reps),
                "errors": sum(1 for t in reps if _is_error(t)),
                "flags": sorted(flags),
                "trials": reps,
            }
        # Structural delta: rewards present for >1 arm and not all equal.
        delta = len(means) > 1 and (max(means) != min(means))
        rows.append({"task_id": task_id, "cells": cells, "delta": delta})
    return {"arms": arms, "rows": rows}


def render_comparison_html(run_id: str, trials: list[dict]) -> str:
    """Render the self-contained interactive arm-vs-arm comparison HTML."""
    arms = build_arm_comparisons(trials)
    matrix = build_task_matrix(trials)
    summary_cards = "".join(_render_arm_card(a) for a in arms)
    return _COMPARISON_TEMPLATE.format(
        run_id=html.escape(run_id),
        n_arms=len(arms),
        n_trials=len(trials),
        summary_cards=summary_cards,
        matrix_json=json.dumps(matrix),
        arms_json=json.dumps(matrix["arms"]),
    )


def _render_arm_card(a: ArmComparison) -> str:
    cost = f"${a.total_cost:.0f}" if a.total_cost is not None else "—"
    zero_mcp = (
        f'<span class="warn">{a.zero_mcp_count} zero-MCP</span>'
        if a.zero_mcp_count
        else "0 zero-MCP"
    )
    err = f'<span class="warn">{a.error_count} err</span>' if a.error_count else "0 err"
    return (
        f'<div class="arm-card"><div class="arm-name">{html.escape(a.label)}</div>'
        f'<div class="arm-score">{a.mean_reward:.3f}</div>'
        f'<div class="arm-foot"><span>{a.total} trials</span><span>{cost}</span>'
        f"<span>{err}</span><span>{zero_mcp}</span></div></div>"
    )


# ---------------------------------------------------------------------------
# Serve mode (stdlib http.server only — no new deps)
# ---------------------------------------------------------------------------


def make_server(html_body: str, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> HTTPServer:
    """Build an HTTPServer that serves *html_body* at ``/`` (and ``/index.html``).

    Returns the bound server WITHOUT starting it, so callers (and tests) own
    the lifecycle. Pass ``port=0`` for an ephemeral port; read the actual
    port from ``server.server_address[1]``.
    """
    payload = html_body.encode("utf-8")

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib handler contract
            if self.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_error(404, "Not found")

        def log_message(self, *args) -> None:  # silence per-request stderr noise
            return

    return HTTPServer((host, port), _Handler)


def build_html(run_dir: Path) -> str:
    """Load a run and render comparison HTML (>=2 arms) or single-run HTML.

    Falls back to the codeprobe-3cs single-run explorer when only one arm is
    present, so ``--serve`` works for both layouts.
    """
    trials = load_run_trials(run_dir)
    arms = {_arm_of(t) for t in trials}
    if len(arms) >= 2:
        return render_comparison_html(run_dir.name, trials)
    return render_html(run_dir.name, trials, build_arm_summaries(trials))


def serve(run_dir: Path, port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> None:
    """Build the viewer HTML for *run_dir* and serve it until interrupted."""
    server = make_server(build_html(run_dir), host=host, port=port)
    bound = server.server_address[1]
    print(f"Serving {run_dir} at http://{host}:{bound}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# Inline template — self-contained: all CSS/JS embedded, no external deps.
# {{ }} are literal braces; {name} are Python format fields.
# ---------------------------------------------------------------------------

_COMPARISON_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>codeprobe comparison — {run_id}</title>
<style>
 body{{font:14px/1.45 system-ui,sans-serif;margin:0;padding:1rem;color:#1a1a1a;background:#fafafa}}
 h1{{font-size:1.2rem;margin:0 0 .25rem}} .muted{{color:#666;font-size:13px}}
 .arms{{display:flex;gap:.75rem;flex-wrap:wrap;margin:.75rem 0 1rem}}
 .arm-card{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px 14px;min-width:160px}}
 .arm-name{{font-weight:600}} .arm-score{{font:600 28px "Source Code Pro",monospace;margin:2px 0}}
 .arm-foot{{display:flex;gap:12px;flex-wrap:wrap;color:#555;font-size:12px}}
 .warn{{color:#c0392b;font-weight:600}}
 table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
 th,td{{border:1px solid #ddd;padding:5px 8px;text-align:left;white-space:nowrap}}
 th{{background:#f0f0f0;position:sticky;top:0}}
 tr.delta td.taskcell{{border-left:3px solid #c0392b}}
 tr.taskrow{{cursor:pointer}} tr.taskrow:hover{{background:#f6f9ff}}
 .cell-ok{{color:#1a7f37}} .cell-bad{{color:#c0392b;font-weight:600}}
 .flag{{display:inline-block;background:#c0392b;color:#fff;border-radius:3px;padding:0 5px;margin:1px;font-size:10px}}
 .detail{{background:#f7f9fc;font-family:ui-monospace,monospace;font-size:12px}}
 .detail table{{font-size:12px;margin:4px 0}}
 .controls{{margin:.5rem 0}} input{{padding:3px 6px;font:inherit}}
</style></head><body>
<h1>codeprobe arm-vs-arm comparison — {run_id}</h1>
<div class="muted">{n_arms} arms · {n_trials} trials. Click a task row to drill into each arm's per-repeat validity signals. Red left-border = arms' mean reward differs.</div>

<div class="arms">{summary_cards}</div>

<div class="controls"><label><input type="checkbox" id="f-delta"> only tasks where arms differ</label>
 &nbsp; <input id="f-text" placeholder="filter task id…"> <span id="count" class="muted"></span></div>

<table id="cmp"><thead><tr id="hrow"></tr></thead><tbody id="tbody"></tbody></table>

<script>
const MATRIX={matrix_json};
const ARMS={arms_json};

function fmt(v){{ if(v===null||v===undefined||v==="")return ""; if(typeof v==="number")return Number.isInteger(v)?v:v.toFixed(3); return String(v); }}

function header(){{
 const hr=document.getElementById("hrow");
 let th=document.createElement("th");th.textContent="Task";hr.appendChild(th);
 ARMS.forEach(a=>{{const t=document.createElement("th");t.textContent=a;hr.appendChild(t);}});
}}
function rows(){{
 const fd=document.getElementById("f-delta").checked;
 const ft=document.getElementById("f-text").value.toLowerCase();
 return MATRIX.rows.filter(r=>(!fd||r.delta)&&(!ft||r.task_id.toLowerCase().includes(ft)));
}}
function cellHtml(c){{
 if(!c||c.repeats===0)return '<span class="muted">—</span>';
 const cls=c.errors>0?"cell-bad":"cell-ok";
 let s='<span class="'+cls+'">'+fmt(c.mean_reward)+'</span> <span class="muted">×'+c.repeats+'</span>';
 (c.flags||[]).forEach(f=>{{s+=' <span class="flag">'+f+'</span>';}});
 return s;
}}
function detailTable(r){{
 let h='<div class="detail"><table><thead><tr><th>arm</th><th>rep</th><th>status</th><th>reward</th><th>error</th><th>maxTurns</th><th>tools</th><th>turns</th><th>cost</th><th>in/out tok</th></tr></thead><tbody>';
 ARMS.forEach(a=>{{(r.cells[a]&&r.cells[a].trials||[]).forEach(t=>{{
  const bad=(t.status==="error"||t.status==="failed");
  h+='<tr><td>'+a+'</td><td>'+fmt(t.repeat_index)+'</td><td class="'+(bad?"cell-bad":"cell-ok")+'">'+fmt(t.status)+'</td><td>'+fmt(t.reward!=null?t.reward:t.score)+'</td><td>'+fmt(t.error_category)+'</td><td>'+fmt(t.hit_max_turns)+'</td><td>'+fmt(t.tool_call_count)+'</td><td>'+fmt(t.num_turns)+'</td><td>'+fmt(t.token_cost_usd)+'</td><td>'+fmt(t.input_tokens)+'/'+fmt(t.output_tokens)+'</td></tr>';
 }});}});
 return h+'</tbody></table></div>';
}}
function render(){{
 const tb=document.getElementById("tbody");tb.innerHTML="";
 const rs=rows();
 rs.forEach(r=>{{
  const tr=document.createElement("tr");tr.className="taskrow"+(r.delta?" delta":"");
  let td=document.createElement("td");td.className="taskcell";td.textContent=r.task_id;tr.appendChild(td);
  ARMS.forEach(a=>{{const c=document.createElement("td");c.innerHTML=cellHtml(r.cells[a]);tr.appendChild(c);}});
  tr.onclick=()=>{{
   if(tr.nextSibling&&tr.nextSibling.classList&&tr.nextSibling.classList.contains("detrow")){{tr.nextSibling.remove();return;}}
   const dr=document.createElement("tr");dr.className="detrow";
   const dtd=document.createElement("td");dtd.colSpan=ARMS.length+1;dtd.innerHTML=detailTable(r);
   dr.appendChild(dtd);tr.parentNode.insertBefore(dr,tr.nextSibling);
  }};
  tb.appendChild(tr);
 }});
 document.getElementById("count").textContent=rs.length+" / "+MATRIX.rows.length+" tasks";
}}
["f-delta","f-text"].forEach(id=>{{const el=document.getElementById(id);el.addEventListener("input",render);el.addEventListener("change",render);}});
header();render();
</script></body></html>
"""
