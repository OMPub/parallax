"""introspect — parallax reasoning about its own runs (the meta loop), with triage.

The diagnosis is LLM-driven ON PURPOSE: a fixed checklist can only catch failure
modes we already named, but parallax pushes novel approaches and WILL hit failures
nobody anticipated. So we hand the raw run telemetry to a model and let it reason
openly about what's degrading runs — known modes and unknown ones alike.

Then we triage each issue:
  * trivial/simple AND a safe, bounded config change  -> JUST DO IT (apply locally)
  * anything else (code, logic, judgement)            -> open a PR against the
    engine repo with a why/what/how explanation for the owner.

The deterministic gather()/recommend() below remain as the evidence collector and
an offline fallback when no engine is reachable.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from . import engines
from .memory import Logbook
from .sightline import load_dir

# Config keys introspect may auto-tune, with (yaml-leaf, lo, hi, cast). Anything
# outside this whitelist (or out of bounds) is escalated to a PR, never auto-applied.
SAFE_CONFIG = {
    "local.max_tokens": ("max_tokens", 1024, 65536, int),
    "novelty.cos_threshold": ("cos_threshold", 0.5, 0.9, float),
    "novelty.jaccard_threshold": ("jaccard_threshold", 0.4, 0.85, float),
    "max_files": ("max_files", 5, 200, int),
    "spawn.per_survey": ("per_survey", 0, 10, int),
}
REPO = "OMPub/parallax"


# --- evidence ---------------------------------------------------------------
def _load_surveys(chart):
    out = []
    for f in sorted(Path(chart.surveys_dir).glob("survey-*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            pass
    return out


def gather(chart):
    surveys = _load_surveys(chart)
    lb = Logbook(chart)
    engine_status, truncations = {}, 0
    total_hyp = total_dups = 0
    aborts, outcomes = [], {}
    for s in surveys:
        for t in s.get("engine_trace", []):
            es = engine_status.setdefault(t.get("engine"), {})
            st = t.get("status", "?")
            es[st] = es.get(st, 0) + 1
            if st == "error" and "truncat" in (t.get("detail") or "").lower():
                truncations += 1
        total_hyp += len(s.get("hypotheses", []))
        total_dups += len(s.get("skipped_duplicates", []))
        for h in s.get("hypotheses", []):
            outcomes[h.get("outcome")] = outcomes.get(h.get("outcome"), 0) + 1
        for a in s.get("operational_aborts", []):
            aborts.append({"survey": s.get("survey_id"), "sightline": a.get("sightline"),
                           "reasons": a.get("reasons"), "traces": a.get("traces")})
    return {
        "surveys": len(surveys),
        "engine_status": engine_status,
        "truncations": truncations,
        "total_hypotheses": total_hyp,
        "total_deduped": total_dups,
        "dedup_rate": round(total_dups / (total_hyp + total_dups), 2) if (total_hyp + total_dups) else 0.0,
        "outcome_counts": outcomes,
        "operational_aborts": aborts,
        "incubator": len(load_dir(chart.incubator_dir)),
        "yields": lb.yields(),
    }


def _config_snapshot(chart):
    return {
        "engines": chart.engines,
        "local.max_tokens": chart.local_max_tokens,
        "novelty.cos_threshold": chart.embed_threshold,
        "novelty.jaccard_threshold": chart.jaccard_threshold,
        "max_files": chart.max_files,
        "spawn.per_survey": chart.spawn_per_survey,
        "local.endpoints": getattr(chart, "local_endpoints", None) or [chart.local_endpoint],
    }


# --- LLM diagnosis (open-ended) ---------------------------------------------
def _diagnose_call(chart, prompt):
    for eng in [e for e in ("claude", "local", "codex") if engines.available(e, chart)]:
        try:
            raw = engines.call(eng, prompt, chart, temperature=0.4,
                               max_tokens=getattr(chart, "local_max_tokens", 8000))
        except Exception:
            continue
        data = engines.extract_json(raw)
        if isinstance(data, dict):
            return data, eng
    return None, None


def diagnose(chart, evidence):
    """Return (issues, engine). LLM reasons openly over telemetry; falls back to
    deterministic recommendations if no engine is reachable."""
    ev = {k: v for k, v in evidence.items() if k != "yields"}
    ev["yields"] = {sid: {"runs": y.get("runs", 0), "confirmed": y.get("confirmed", 0),
                          "operational_aborts": y.get("operational_aborts", 0)}
                    for sid, y in evidence.get("yields", {}).items()}
    prompt = (
        "### PARALLAX ROLE: INTROSPECT (operations / SRE meta-review)\n"
        "You are the operations brain for parallax, an autonomous LLM security scanner.\n"
        "Do NOT review target code. Review parallax's OWN recent runs and find anything\n"
        "degrading them for reasons unrelated to scan quality. Consider known modes\n"
        "(engine errors, rate limits, unreachable endpoints, unparsable output, novelty\n"
        "saturation) but ALSO infer NOVEL/unexpected problems from the raw signals — we\n"
        "push novel approaches and will hit failures no checklist anticipates. Operational\n"
        "failures must never be mistaken for low scan quality.\n\n"
        "## Recent run telemetry\n" + json.dumps(ev, indent=2) + "\n\n"
        "## Current tunable config\n" + json.dumps(_config_snapshot(chart), indent=2) + "\n\n"
        "Return ONE JSON object:\n"
        '{"issues":[{"title":"short","evidence":"the signals that show it",'
        '"operational":true,"severity":"low|medium|high","complexity":"trivial|simple|complex",'
        '"fix_kind":"config|code|human",'
        '"config_change":{"key":"local.max_tokens","to":<value>} or null,'
        '"explanation":"why / what / how — enough for a repo owner to act"}]}\n'
        'Return {"issues":[]} if runs look healthy. Do not invent problems. '
        "Only output the JSON object."
    )
    data, eng = _diagnose_call(chart, prompt)
    if data and isinstance(data.get("issues"), list):
        return data["issues"], eng
    return [{"title": r, "evidence": "deterministic heuristic", "operational": True,
             "severity": "medium", "complexity": "simple", "fix_kind": "human",
             "config_change": None, "explanation": r} for r in recommend(chart, evidence)], None


# --- triage + act -----------------------------------------------------------
def _safe_config(cc):
    if not cc or cc.get("key") not in SAFE_CONFIG:
        return None
    leaf, lo, hi, cast = SAFE_CONFIG[cc["key"]]
    try:
        v = cast(cc.get("to"))
    except Exception:
        return None
    v = max(lo, min(hi, v))
    return (leaf, v)


def triage(issue):
    if issue.get("fix_kind") == "config" and issue.get("complexity") in ("trivial", "simple") \
            and _safe_config(issue.get("config_change")):
        return "auto"
    return "pr"


def _set_yaml_scalar(text, leaf, value):
    val = str(value)
    pat = re.compile(r"^(\s*" + re.escape(leaf) + r":\s*).*$", re.M)
    new, n = pat.subn(lambda m: m.group(1) + val, text, count=1)
    return new if n else None


def apply_config_fix(chart, cc):
    sc = _safe_config(cc)
    if not sc:
        return False, "not a safe/bounded config change"
    leaf, value = sc
    path = chart.dir / "chart.yaml"
    try:
        text = path.read_text()
    except Exception as e:
        return False, f"cannot read chart.yaml: {e}"
    new = _set_yaml_scalar(text, leaf, value)
    if not new:
        return False, f"key '{leaf}' not found in chart.yaml"
    path.write_text(new)
    return True, f"{cc['key']} -> {value}"


def _proposal_md(chart, issue):
    return (f"# [parallax introspect] {issue.get('title')}\n\n"
            f"_Auto-filed by `parallax introspect` after reviewing **{chart.name}** runs._\n\n"
            f"- **severity:** {issue.get('severity')}\n"
            f"- **complexity:** {issue.get('complexity')}\n"
            f"- **fix kind:** {issue.get('fix_kind')}\n"
            f"- **operational (not scan-quality):** {issue.get('operational')}\n\n"
            f"## Evidence\n{issue.get('evidence')}\n\n"
            f"## Why / what / how\n{issue.get('explanation')}\n\n"
            f"---\ncc @brook (repo owner) — review and merge or close.\n")


def open_pr(chart, issue):
    root = chart.generic_atlas.parent  # parallax engine repo root

    def g(*a):
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True)

    if g("status", "--porcelain").stdout.strip():
        return {"ok": False, "detail": "engine repo has uncommitted changes; skipped auto-PR"}
    orig = g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    h = hashlib.sha1((issue.get("title") or "issue").encode()).hexdigest()[:8]
    slug = (re.sub(r"[^a-z0-9]+", "-", (issue.get("title") or "issue").lower()).strip("-")[:40] or "issue")
    branch = f"introspect/{slug}-{h}"
    body = _proposal_md(chart, issue)
    propdir = root / "proposals"
    propdir.mkdir(exist_ok=True)
    (propdir / f"{slug}-{h}.md").write_text(body)
    if g("checkout", "-b", branch).returncode != 0:
        g("checkout", orig)
        return {"ok": False, "detail": f"could not create branch {branch}"}
    g("add", "proposals")
    g("commit", "-m", f"introspect: {issue.get('title')}")
    g("push", "-u", "origin", branch)
    pr = subprocess.run(["gh", "pr", "create", "--repo", REPO, "--head", branch, "--base", orig,
                         "--title", f"[introspect] {issue.get('title')}", "--body", body, "--draft"],
                        capture_output=True, text=True)
    g("checkout", orig)
    return {"ok": pr.returncode == 0, "detail": (pr.stdout or pr.stderr).strip()}


def run(chart, act=False):
    evidence = gather(chart)
    issues, eng = diagnose(chart, evidence)
    actions = []
    for issue in issues:
        decision = triage(issue)
        result = "reported"
        if act and decision == "auto":
            ok, msg = apply_config_fix(chart, issue.get("config_change"))
            result = f"AUTO-APPLIED {msg}" if ok else f"auto-apply failed: {msg}"
        elif act and decision == "pr":
            pr = open_pr(chart, issue)
            result = f"PR {'opened' if pr['ok'] else 'FAILED'}: {pr['detail']}"
        elif decision == "auto":
            result = f"would auto-apply {issue.get('config_change')}"
        else:
            result = "would open PR"
        actions.append({"issue": issue, "decision": decision, "result": result})
    return {"evidence": evidence, "engine": eng, "issues": issues, "actions": actions, "acted": act}


# --- deterministic fallback / extra signal ----------------------------------
def recommend(chart, g):
    recs = []
    if g["truncations"]:
        cap = getattr(chart, "local_max_tokens", 0)
        if cap and cap < 16384:
            recs.append(f"Local truncated {g['truncations']}x at a low cap ({cap}) — raise local.max_tokens.")
        else:
            recs.append(f"Local truncated {g['truncations']}x despite an ample cap ({cap}); rare spiral, "
                        f"absorbed by fallback — no action needed.")
    for eng, st in g["engine_status"].items():
        total = sum(st.values())
        errs = st.get("error", 0) + st.get("unparsable", 0)
        if total >= 2 and errs == total:
            recs.append(f"Engine '{eng}' failed every call ({errs}/{total}) — likely down or rate-limited.")
    if g["operational_aborts"]:
        per = {}
        for a in g["operational_aborts"]:
            per[a["sightline"]] = per.get(a["sightline"], 0) + 1
        for sid, n in sorted(per.items(), key=lambda x: -x[1]):
            recs.append(f"Sightline '{sid}' aborted operationally {n}x (NOT penalized; re-armed).")
    if g["total_hypotheses"] + g["total_deduped"] >= 8 and g["dedup_rate"] >= 0.7:
        recs.append(f"{g['dedup_rate']*100:.0f}% of hypotheses deduped — novelty saturating; rotate lenses.")
    if g["incubator"] >= 5:
        recs.append(f"{g['incubator']} incubator candidates unreviewed — run `parallax promote`.")
    return recs or ["No operational issues detected — runs look healthy."]


def render(chart, res):
    g = res["evidence"]
    L = [f"# parallax introspection — {chart.name}",
         f"_diagnosis engine: {res.get('engine') or 'deterministic-fallback'}; "
         f"acted: {res.get('acted')}_", "",
         f"- surveys analyzed: {g['surveys']}  |  hypotheses: {g['total_hypotheses']} "
         f"({g['dedup_rate']*100:.0f}% deduped)  |  incubator: {g['incubator']}",
         "- engine attempts: " + (", ".join(
             f"{e}({sum(s.values())})" for e, s in g["engine_status"].items()) or "none yet"),
         "", f"## Issues ({len(res['issues'])})"]
    if not res["issues"]:
        L.append("_none_")
    for a in res["actions"]:
        i = a["issue"]
        L.append("")
        L.append(f"### [{i.get('severity', '?').upper()}/{i.get('complexity', '?')}] {i.get('title')}")
        L.append(f"- triage: **{a['decision']}** → {a['result']}")
        L.append(f"- evidence: {i.get('evidence')}")
        L.append(f"- fix ({i.get('fix_kind')}): {i.get('explanation')}")
    return "\n".join(L) + "\n"
