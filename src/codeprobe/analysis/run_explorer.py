"""Self-contained HTML run-data explorer for manual validity audits.

Reads a ``runs/<id>/`` directory's ``per_trial.json`` and emits a single
offline ``explorer.html`` (inline CSS/JS, no server, no network) so a human
can eyeball every trial, sort/filter, and spot invalid/infra trials at a
glance. Mirrors CodeScaleBench's run-comparison explorer (codeprobe-3cs).

ZFC note: the validity *flags* are STRUCTURAL signals only — trial status,
error_category, the hit_max_turns boolean, a zero tool-call count, a
zero-reward-on-non-completed trial, and the ``missing`` marker. No semantic
scoring or quality thresholds live here; the reward/cost aggregates are
delegated to :func:`codeprobe.analysis.stats.summarize_completed_tasks`.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path

from codeprobe.analysis.stats import summarize_completed_tasks
from codeprobe.models.experiment import CompletedTask, completed_task_from_dict

PER_TRIAL_FILE = "per_trial.json"
EXPLORER_FILE = "explorer.html"


def safe_json_island(data: object) -> str:
    """Serialise ``data`` for embedding inside an inline ``<script>`` tag.

    ``json.dumps`` does not escape ``<``, so a literal ``</script>`` anywhere
    in the payload closes the tag and the remainder is parsed as live markup.
    Trial payloads are copied wholesale from ``results.json`` and carry raw
    agent stderr, so every string in here is attacker-influenced. Escaping
    every ``<`` keeps JSON parsers happy while denying the HTML parser a tag.

    This is stricter than the snapshot browser helper because all less-than
    signs are escaped, not only closing-tag prefixes.
    """
    return json.dumps(data, sort_keys=True).replace("<", "\\u003c")

# Columns surfaced in the trial table, in display order. Each is
# (per_trial key, header label). Kept as data so the table and the CSV-like
# export stay in sync.
TRIAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("config", "Arm"),
    ("task_id", "Task"),
    ("repeat_index", "Rep"),
    ("status", "Status"),
    ("reward", "Reward"),
    ("score", "Score"),
    ("passed", "Passed"),
    ("error_category", "Error"),
    ("hit_max_turns", "MaxTurns"),
    ("result_subtype", "Subtype"),
    ("num_turns", "Turns"),
    ("tool_call_count", "Tools"),
    ("token_cost_usd", "Cost $"),
    ("input_tokens", "In tok"),
    ("output_tokens", "Out tok"),
    ("missing", "Missing"),
)

# Map per_trial.json keys onto CompletedTask fields so the existing stats
# summarizer can be reused without re-deriving reward/cost math. per_trial
# uses run-facing names (reward/score, token_cost_usd, task_time_seconds).
_PER_TRIAL_TO_TASK = {
    "reward": "automated_score",
    "token_cost_usd": "cost_usd",
    "task_time_seconds": "duration_seconds",
}


@dataclass(frozen=True)
class ArmSummary:
    """Per-arm rollup: reused stat aggregates plus structural flag counts."""

    label: str
    total: int
    mean_reward: float
    median_reward: float
    mean_cost: float | None
    flag_counts: dict[str, int]


def newest_run_dir(runs_root: Path) -> Path:
    """Return the most recently modified run dir under *runs_root*.

    A run dir is any subdirectory containing ``per_trial.json``. Raises
    FileNotFoundError when none exist so the caller can fail loudly rather
    than emit an empty explorer.
    """
    candidates = [
        d for d in runs_root.iterdir() if d.is_dir() and (d / PER_TRIAL_FILE).is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No run dirs with {PER_TRIAL_FILE} found under {runs_root}"
        )
    return max(candidates, key=lambda d: (d / PER_TRIAL_FILE).stat().st_mtime)


RESULTS_FILE = "results.json"

# Reverse of _PER_TRIAL_TO_TASK: map a results.json ``completed`` entry
# (CompletedTask shape: automated_score / cost_usd / …) back onto the
# run-facing trial keys the explorer/comparison renderers use.
_TASK_TO_TRIAL = {v: k for k, v in _PER_TRIAL_TO_TASK.items()}


def _normalize_completed_entry(entry: dict, config: str) -> dict:
    """Normalize a results.json ``completed`` entry into a trial dict.

    Renames CompletedTask keys to the run-facing trial keys, mirrors
    automated_score into both ``reward`` and ``score``, and lifts the
    scorer's ``passed`` out of scoring_details when present. Fields the
    results.json layout doesn't carry (e.g. hit_max_turns) are left ABSENT
    rather than fabricated — the renderer shows them blank, so partial data
    is preserved honestly, never invented or dropped (adapter-contract).
    """
    trial: dict = {}
    for key, value in entry.items():
        trial[_TASK_TO_TRIAL.get(key, key)] = value
    trial.setdefault("config", config)
    if "reward" in trial and "score" not in trial:
        trial["score"] = trial["reward"]
    scoring_details = entry.get("scoring_details") or {}
    if isinstance(scoring_details, dict) and "passed" in scoring_details:
        trial.setdefault("passed", scoring_details["passed"])
    return trial


def _load_per_arm_results(run_dir: Path) -> list[dict]:
    """Load every arm's results.json under *run_dir* into one trial list.

    The per-arm layout (the 9tk Sourcegraph comparison) is
    ``<run-dir>/<arm>/results.json`` with ``{config, completed, summary}``.
    Each ``completed`` entry is normalized and tagged with its arm so the
    comparison view can group columns by arm. Returns [] when no arm has a
    results.json (the caller decides whether that's an error).
    """
    trials: list[dict] = []
    for arm_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        results_path = arm_dir / RESULTS_FILE
        if not results_path.is_file():
            continue
        data = json.loads(results_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        config = str(data.get("config") or arm_dir.name)
        for entry in data.get("completed", []):
            trials.append(_normalize_completed_entry(entry, config))
    return trials


def load_run_trials(run_dir: Path) -> list[dict]:
    """Load a run's trials, accepting both on-disk layouts (codeprobe-00e).

    Two layouts are supported (A3):
      * ``<run-dir>/per_trial.json`` — a flat JSON list of trial dicts
        (the codeprobe-3cs single-run shape).
      * ``<run-dir>/<arm>/results.json`` — per-arm CompletedTask lists
        (the 9tk arm-vs-arm comparison shape), normalized + arm-tagged.

    Partial/missing fields are kept as-is (never dropped) so the audit view
    is honest about incomplete data (adapter-contract).
    """
    path = run_dir / PER_TRIAL_FILE
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"{path} must contain a JSON list of trials")
        return data

    per_arm = _load_per_arm_results(run_dir)
    if per_arm:
        return per_arm

    raise FileNotFoundError(
        f"No {PER_TRIAL_FILE} and no <arm>/{RESULTS_FILE} found under {run_dir}"
    )


def compute_validity_flags(trial: dict) -> list[str]:
    """Structural validity flags for one trial (ZFC: no semantic judgment).

    Each flag marks a case a human auditor needs to troubleshoot, derived
    purely from structural fields — never from a quality threshold.
    """
    flags: list[str] = []
    status = trial.get("status")
    if status in ("error", "failed"):
        flags.append(f"status:{status}")
    if trial.get("error_category"):
        flags.append(f"error:{trial['error_category']}")
    if trial.get("hit_max_turns") is True:
        flags.append("max-turns")
    # Zero reward on a trial that did not complete cleanly is the
    # infra-false-zero signature (a 0.0 that is a casualty, not a measured
    # failure) — the exact ambiguity the 9tk audit had to resolve by hand.
    if trial.get("reward") == 0 and status != "completed":
        flags.append("zero-reward-noncomplete")
    # An explicit zero tool-call count means the agent never touched its
    # tool surface (the with-sg-narrow abandonment signal). None = not
    # captured, which is NOT a flag.
    if trial.get("tool_call_count") == 0:
        flags.append("zero-tools")
    missing = trial.get("missing")
    if missing:
        flags.append("missing")
    return flags


def _trial_to_completed_task(trial: dict) -> CompletedTask:
    """Build a CompletedTask from a per_trial dict for stat reuse."""
    mapped: dict = {}
    for key, value in trial.items():
        target = _PER_TRIAL_TO_TASK.get(key, key)
        mapped[target] = value
    # automated_score is required; fall back to score when reward is absent.
    if "automated_score" not in mapped:
        mapped["automated_score"] = trial.get("score", 0.0) or 0.0
    mapped.setdefault("task_id", str(trial.get("task_id", "")))
    return completed_task_from_dict(mapped)


def build_arm_summaries(trials: list[dict]) -> list[ArmSummary]:
    """Group trials by arm and roll up reused stats + structural flag counts."""
    by_arm: dict[str, list[dict]] = {}
    for t in trials:
        by_arm.setdefault(str(t.get("config", "?")), []).append(t)

    summaries: list[ArmSummary] = []
    for label in sorted(by_arm):
        arm_trials = by_arm[label]
        stats = summarize_completed_tasks(
            label, (_trial_to_completed_task(t) for t in arm_trials)
        )
        flag_counts: dict[str, int] = {}
        for t in arm_trials:
            for flag in compute_validity_flags(t):
                # Count by flag family (text before ':') so "status:error"
                # and "status:failed" roll into one auditable bucket.
                family = flag.split(":", 1)[0]
                flag_counts[family] = flag_counts.get(family, 0) + 1
        summaries.append(
            ArmSummary(
                label=label,
                total=len(arm_trials),
                mean_reward=stats.mean_score,
                median_reward=stats.median_score,
                mean_cost=(
                    stats.total_cost_usd / stats.total_tasks
                    if stats.total_cost_usd is not None and stats.total_tasks
                    else None
                ),
                flag_counts=flag_counts,
            )
        )
    return summaries


def render_html(run_id: str, trials: list[dict], summaries: list[ArmSummary]) -> str:
    """Render the self-contained explorer HTML (inline CSS/JS, no deps)."""
    # Enrich each trial with computed flags + JSON-encode for the JS layer.
    enriched = [{**t, "_flags": compute_validity_flags(t)} for t in trials]
    data_json = safe_json_island(enriched)
    cols_json = json.dumps([k for k, _ in TRIAL_COLUMNS])
    headers_json = json.dumps([h for _, h in TRIAL_COLUMNS])
    summary_rows = "".join(_render_summary_row(s) for s in summaries)
    arms = sorted({str(t.get("config", "?")) for t in trials})
    statuses = sorted({str(t.get("status", "?")) for t in trials})
    errcats = sorted({str(t.get("error_category")) for t in trials})
    return _HTML_TEMPLATE.format(
        run_id=html.escape(run_id),
        n_trials=len(trials),
        summary_rows=summary_rows,
        arms_opts=_options(arms),
        status_opts=_options(statuses),
        errcat_opts=_options(errcats),
        data_json=data_json,
        cols_json=cols_json,
        headers_json=headers_json,
    )


def _options(values: list[str]) -> str:
    return "".join(f'<option value="{html.escape(v)}">{html.escape(v)}</option>' for v in values)


def _render_summary_row(s: ArmSummary) -> str:
    flags = ", ".join(f"{k}={v}" for k, v in sorted(s.flag_counts.items())) or "—"
    cost = f"${s.mean_cost:.2f}" if s.mean_cost is not None else "—"
    cls = ' class="dirty"' if s.flag_counts else ""
    return (
        f"<tr{cls}><td>{html.escape(s.label)}</td><td>{s.total}</td>"
        f"<td>{s.mean_reward:.3f}</td><td>{s.median_reward:.3f}</td>"
        f"<td>{cost}</td><td>{html.escape(flags)}</td></tr>"
    )


def write_explorer(run_dir: Path, out_path: Path | None = None) -> Path:
    """Generate the explorer HTML for *run_dir*; return the written path."""
    trials = load_run_trials(run_dir)
    summaries = build_arm_summaries(trials)
    out = out_path or (run_dir / EXPLORER_FILE)
    out.write_text(render_html(run_dir.name, trials, summaries), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Inline template — self-contained: all CSS/JS embedded, no external deps.
# {{ }} are literal braces for the JS/CSS; {name} are Python format fields.
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>codeprobe run explorer — {run_id}</title>
<style>
 body{{font:14px/1.4 system-ui,sans-serif;margin:0;padding:1rem;color:#1a1a1a;background:#fafafa}}
 h1{{font-size:1.2rem;margin:0 0 .25rem}} .muted{{color:#666}}
 table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
 th,td{{border:1px solid #ddd;padding:4px 7px;text-align:left;white-space:nowrap}}
 th{{background:#f0f0f0;cursor:pointer;position:sticky;top:0}}
 th:hover{{background:#e3e8ef}}
 tr.flagged{{background:#fff5f5}} tr.dirty{{background:#fff0f0}}
 .flag{{display:inline-block;background:#c0392b;color:#fff;border-radius:3px;padding:0 5px;margin:1px;font-size:11px}}
 .summary{{margin:.5rem 0 1rem;max-width:980px}}
 .controls{{margin:.5rem 0;display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}}
 select,input{{padding:3px 6px;font:inherit}}
 .detail{{background:#f7f9fc;font-family:ui-monospace,monospace;font-size:12px;white-space:pre-wrap}}
 .ok{{color:#1a7f37}} .bad{{color:#c0392b;font-weight:600}}
 a{{color:#0969da}}
</style></head><body>
<h1>codeprobe run explorer — {run_id}</h1>
<div class="muted">{n_trials} trials. Click a header to sort; use filters to scope; click a row to drill in. Flagged rows are structural validity concerns (status/error/max-turns/zero-tools/zero-reward/missing).</div>

<h2 style="font-size:1rem">Per-arm summary</h2>
<table class="summary"><thead><tr><th>Arm</th><th>N</th><th>Mean reward</th><th>Median reward</th><th>Mean cost</th><th>Flag counts</th></tr></thead>
<tbody>{summary_rows}</tbody></table>

<div class="controls">
 <label>Arm <select id="f-config"><option value="">all</option>{arms_opts}</select></label>
 <label>Status <select id="f-status"><option value="">all</option>{status_opts}</select></label>
 <label>Error <select id="f-error"><option value="">all</option>{errcat_opts}</select></label>
 <label><input type="checkbox" id="f-flagged"> flagged only</label>
 <input id="f-text" placeholder="filter task id…">
 <span id="count" class="muted"></span>
</div>
<table id="trials"><thead><tr id="hrow"></tr></thead><tbody id="tbody"></tbody></table>

<script>
const DATA={data_json};
const COLS={cols_json};
const HEADERS={headers_json};
const FLAGKEYS=new Set(["status","error_category","hit_max_turns","tool_call_count","missing","reward"]);
let sortCol=null, sortAsc=true;

function fmt(v){{
 if(v===null||v===undefined||v==="")return "";
 if(typeof v==="number")return Number.isInteger(v)?v:v.toFixed(4);
 return String(v);
}}
function isFlagged(t){{return t._flags&&t._flags.length>0;}}

function buildHeader(){{
 const hr=document.getElementById("hrow");
 HEADERS.forEach((h,i)=>{{const th=document.createElement("th");th.textContent=h;
  th.onclick=()=>{{if(sortCol===COLS[i])sortAsc=!sortAsc;else{{sortCol=COLS[i];sortAsc=true;}}render();}};hr.appendChild(th);}});
 const th=document.createElement("th");th.textContent="Flags";hr.appendChild(th);
}}
function rows(){{
 const fc=document.getElementById("f-config").value;
 const fs=document.getElementById("f-status").value;
 const fe=document.getElementById("f-error").value;
 const ff=document.getElementById("f-flagged").checked;
 const ft=document.getElementById("f-text").value.toLowerCase();
 let r=DATA.filter(t=>(!fc||String(t.config)===fc)&&(!fs||String(t.status)===fs)
  &&(!fe||String(t.error_category)===fe)&&(!ff||isFlagged(t))
  &&(!ft||String(t.task_id).toLowerCase().includes(ft)));
 if(sortCol){{r=r.slice().sort((a,b)=>{{let x=a[sortCol],y=b[sortCol];
  if(x===null||x===undefined)x="";if(y===null||y===undefined)y="";
  if(typeof x==="number"&&typeof y==="number")return sortAsc?x-y:y-x;
  return sortAsc?String(x).localeCompare(String(y)):String(y).localeCompare(String(x));}});}}
 return r;
}}
function render(){{
 const tb=document.getElementById("tbody");tb.innerHTML="";
 const r=rows();
 r.forEach((t,idx)=>{{
  const tr=document.createElement("tr");if(isFlagged(t))tr.className="flagged";
  COLS.forEach(c=>{{const td=document.createElement("td");
   if(c==="status"){{const sp=document.createElement("span");
    sp.className=(t.status==="completed"?"ok":"bad");sp.textContent=fmt(t[c]);td.appendChild(sp);}}
   else td.textContent=fmt(t[c]);tr.appendChild(td);}});
  const ftd=document.createElement("td");
  (t._flags||[]).forEach(f=>{{const s=document.createElement("span");s.className="flag";s.textContent=f;ftd.appendChild(s);}});
  tr.appendChild(ftd);
  tr.onclick=()=>toggleDetail(tr,t);
  tb.appendChild(tr);
 }});
 document.getElementById("count").textContent=r.length+" / "+DATA.length+" trials";
}}
function toggleDetail(tr,t){{
 if(tr.nextSibling&&tr.nextSibling.classList&&tr.nextSibling.classList.contains("detrow")){{tr.nextSibling.remove();return;}}
 const dr=document.createElement("tr");dr.className="detrow";
 const td=document.createElement("td");td.colSpan=COLS.length+1;td.className="detail";
 td.textContent=JSON.stringify(t,null,2);
 dr.appendChild(td);tr.parentNode.insertBefore(dr,tr.nextSibling);
}}
["f-config","f-status","f-error","f-flagged","f-text"].forEach(id=>{{
 const el=document.getElementById(id);el.addEventListener(el.type==="checkbox"?"change":"input",render);
 el.addEventListener("change",render);
}});
buildHeader();render();
</script></body></html>
"""
