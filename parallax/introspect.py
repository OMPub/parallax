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
CONSENT_FILE = Path.home() / ".parallax" / "contribute-consent"


def _has_consent():
    return CONSENT_FILE.exists()


def _grant_consent():
    CONSENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONSENT_FILE.write_text("Consented to file sanitized, engine-scope issues upstream to "
                            + REPO + " as draft PRs.\n")


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
    aborts, outcomes, findings = [], {}, []
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
        for f in s.get("findings", []):  # so introspect can vet finding QUALITY (catch FPs)
            findings.append({"sightline": f.get("sightline"), "severity": f.get("severity"),
                             "claim": (f.get("claim") or "")[:300],
                             "evidence": (f.get("evidence") or "")[:200]})
    return {
        "surveys": len(surveys),
        "engine_status": engine_status,
        "truncations": truncations,
        "total_hypotheses": total_hyp,
        "total_deduped": total_dups,
        "dedup_rate": round(total_dups / (total_hyp + total_dups), 2) if (total_hyp + total_dups) else 0.0,
        "outcome_counts": outcomes,
        "operational_aborts": aborts,
        "confirmed_findings": findings,
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
        "failures must never be mistaken for low scan quality.\n"
        "Also vet `confirmed_findings` for QUALITY: flag any that are vacuous, merely restate\n"
        "a code line, or don't assert a concrete exploitable defect (e.g. 'no hypothesis could\n"
        "be established') — that's a false positive escaping the finding gate (a scan-quality\n"
        "issue, fix_kind=code, scope=engine).\n\n"
        "## Recent run telemetry\n" + json.dumps(ev, indent=2) + "\n\n"
        "## Current tunable config\n" + json.dumps(_config_snapshot(chart), indent=2) + "\n\n"
        "Classify each issue's SCOPE:\n"
        " - 'local'  = specific to THIS deployment's config/environment (fix on this machine).\n"
        " - 'engine' = a GENERALIZABLE defect in parallax itself that would affect any user\n"
        "   (a candidate to contribute upstream). For 'engine' issues, write title/evidence/\n"
        "   explanation GENERICALLY about harness behavior — never name this target repo, its\n"
        "   files, paths, or any scanned code/findings (that stays private).\n\n"
        "Return ONE JSON object:\n"
        '{"issues":[{"title":"short","evidence":"the signals that show it",'
        '"operational":true,"scope":"local|engine","severity":"low|medium|high",'
        '"complexity":"trivial|simple|complex","fix_kind":"config|code|human",'
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


def _sanitize(chart, text):
    """Strip target-identifying info from anything destined for a PUBLIC upstream
    PR — repo name, absolute target path, and user home dirs. Defense in depth on
    top of the model being told to phrase engine-scope issues generically."""
    if not text:
        return text
    text = text.replace(str(chart.target), "<target>")
    if chart.name:
        text = re.sub(re.escape(chart.name), "<target>", text)
    text = re.sub(r"/Users/[^/\s]+", "/Users/<user>", text)
    text = re.sub(r"/home/[^/\s]+", "/home/<user>", text)
    return text


def _local_proposal_md(chart, issue):
    return (f"# [parallax introspect] {issue.get('title')}\n\n"
            f"_Local proposal for **{chart.name}** — stays on this machine; review and act._\n\n"
            f"- severity: {issue.get('severity')} | complexity: {issue.get('complexity')} | "
            f"scope: {issue.get('scope')} | fix: {issue.get('fix_kind')}\n\n"
            f"## Evidence\n{issue.get('evidence')}\n\n"
            f"## Why / what / how\n{issue.get('explanation')}\n")


def _contribution_md(chart, issue):
    """Sanitized, target-agnostic body for an UPSTREAM PR to the shared engine."""
    return _sanitize(chart,
                     f"# [parallax introspect] {issue.get('title')}\n\n"
                     f"_Auto-surfaced by `parallax introspect` running against a (private) target; "
                     f"filed as a generalizable harness issue. No target details included._\n\n"
                     f"- severity: {issue.get('severity')} | complexity: {issue.get('complexity')} | "
                     f"fix: {issue.get('fix_kind')}\n\n"
                     f"## Symptom (harness behavior)\n{issue.get('evidence')}\n\n"
                     f"## Why / what / how\n{issue.get('explanation')}\n\n"
                     f"---\nFiled by a parallax deployment's self-introspection. Review and merge or close.\n")


def _write_local_proposal(chart, issue):
    d = chart.dir / "proposals"
    d.mkdir(exist_ok=True)
    slug = (re.sub(r"[^a-z0-9]+", "-", (issue.get("title") or "issue").lower()).strip("-")[:40] or "issue")
    path = d / f"{slug}.md"
    path.write_text(_local_proposal_md(chart, issue))
    return str(path.relative_to(chart.target))


def _open_issues():
    r = subprocess.run(["gh", "issue", "list", "--repo", REPO, "--state", "open",
                        "--limit", "60", "--json", "number,title,body"],
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout) if r.returncode == 0 else []
    except Exception:
        return []


def _engine_source(chart, budget=70000):
    pkg = chart.generic_atlas.parent / "parallax"
    parts, total = [], 0
    for f in sorted(pkg.glob("*.py")):
        try:
            t = f.read_text()[:9000]
        except Exception:
            continue
        if total + len(t) > budget:
            break
        parts.append(f"### FILE: parallax/{f.name}\n{t}")
        total += len(t)
    return "\n\n".join(parts)


def contribute_issue(chart, issue):
    """LLM-driven upstream contribution: dedup against open issues (+1 if a match),
    else open a new draft PR — including a patch when the model judges the fix
    concrete and non-obvious. Everything sanitized; everything a DRAFT for review."""
    issues = _open_issues()
    listing = "\n".join(f"#{i['number']}: {i['title']}" for i in issues) or "(none open)"
    safe = {k: _sanitize(chart, str(issue.get(k))) for k in ("title", "evidence", "explanation", "fix_kind")}
    prompt = (
        "### PARALLAX ROLE: CONTRIBUTE (file a generalizable harness issue upstream)\n"
        "A parallax deployment found a likely GENERIC engine issue. First, decide if it duplicates\n"
        "an OPEN issue below — if so, '+1' it with this deployment's corroborating evidence rather\n"
        "than opening a duplicate. Otherwise propose a NEW issue; and IF (and only if) you are\n"
        "confident in a concrete, non-obvious fix, include a patch as the FULL new contents of the\n"
        "file(s) to change. Never include the private target's name, paths, or scanned code.\n\n"
        "## The issue\n" + json.dumps(safe, indent=2) + "\n\n"
        "## Open issues on " + REPO + "\n" + listing + "\n\n"
        "## Engine source (only if drafting a patch)\n" + _engine_source(chart) + "\n\n"
        "Return ONE JSON object:\n"
        '{"decision":"match|new","match_number":<int or null>,'
        '"comment":"<+1 corroboration, if match>",'
        '"title":"<title, if new>","body":"<why/what/how, if new>",'
        '"changes":[{"path":"parallax/<file>.py","new_content":"<full file>"}]}\n'
        "Use changes=[] unless the fix is concrete and non-obvious. Only output the JSON object."
    )
    data, _eng = _diagnose_call(chart, prompt)
    if not data:
        return {"ok": False, "detail": "no engine available to draft contribution"}
    if data.get("decision") == "match" and data.get("match_number"):
        body = _sanitize(chart, data.get("comment") or "+1 — another parallax deployment hit this.")
        r = subprocess.run(["gh", "issue", "comment", str(data["match_number"]), "--repo", REPO,
                            "--body", body], capture_output=True, text=True)
        return {"ok": r.returncode == 0, "detail": f"+1 on #{data['match_number']}"}
    return _open_new(chart, issue, data)


def _open_new(chart, issue, data):
    root = chart.generic_atlas.parent

    def g(*a):
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True)

    if g("status", "--porcelain").stdout.strip():
        return {"ok": False, "detail": "engine repo dirty; skipped (commit/stash first)"}
    orig = g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    title = _sanitize(chart, data.get("title") or issue.get("title") or "issue")
    h = hashlib.sha1(title.encode()).hexdigest()[:8]
    slug = (re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "issue")
    branch = f"introspect/{slug}-{h}"
    if g("checkout", "-b", branch).returncode != 0:
        g("checkout", orig)
        return {"ok": False, "detail": f"branch {branch} already exists"}

    # Apply a proposed patch only if every changed file is in-package AND compiles.
    patched = []
    changes = data.get("changes") or []
    for ch in changes:
        p = root / ch.get("path", "")
        nc = ch.get("new_content") or ""
        if not ch.get("path", "").startswith("parallax/") or not p.exists() or not nc:
            patched = []
            break
        try:
            compile(nc, ch["path"], "exec")
        except Exception:
            patched = []  # bad/unsafe patch -> fall back to proposal-only
            break
        p.write_text(nc)
        patched.append(ch["path"])

    body = _sanitize(chart, data.get("body") or _contribution_md(chart, issue))
    if patched:
        body += "\n\n_Draft patch touches: " + ", ".join(patched) + " — review before merge._\n"
        g("add", "-A")
    else:
        g("checkout", "--", ".")  # discard any partial writes
        propdir = root / "proposals"
        propdir.mkdir(exist_ok=True)
        (propdir / f"{slug}-{h}.md").write_text(body)
        g("add", "proposals")
    g("commit", "-m", f"introspect: {title}")
    g("push", "-u", "origin", branch)
    pr = subprocess.run(["gh", "pr", "create", "--repo", REPO, "--head", branch, "--base", orig,
                         "--title", f"[introspect] {title}", "--body", body, "--draft"],
                        capture_output=True, text=True)
    g("checkout", orig)
    return {"ok": pr.returncode == 0, "detail": (pr.stdout or pr.stderr).strip(),
            "patched": patched}


def run(chart, act=False, contribute=False, yes=False):
    """Diagnose, triage, and (optionally) act. Defaults are SAFE:
      - report only unless --act.
      - --act stays LOCAL: auto-apply whitelisted config; write local proposals.
        Nothing leaves the machine.
      - --contribute files SANITIZED, engine-scope issues upstream (dedup-aware:
        +1 an open issue if it matches, else a draft PR with an optional patch).
        Gated by a one-time human consent (--yes) since telemetry can carry
        private target details."""
    evidence = gather(chart)
    issues, eng = diagnose(chart, evidence)
    consented = _has_consent()
    if contribute and not consented and yes:
        _grant_consent()
        consented = True
    can_contribute = contribute and consented
    actions = []
    for issue in issues:
        decision = triage(issue)
        scope = issue.get("scope", "local")
        results = []
        # LOCAL — safe, on this machine only
        if decision == "auto":
            if act:
                ok, msg = apply_config_fix(chart, issue.get("config_change"))
                results.append(f"auto-applied {msg}" if ok else f"auto-apply failed: {msg}")
            else:
                results.append(f"would auto-apply {issue.get('config_change')}")
        else:
            results.append(f"local proposal written: {_write_local_proposal(chart, issue)}"
                           if act else "would write local proposal")
        # UPSTREAM — opt-in + one-time consent; engine-scope only; sanitized; dedup-aware
        if scope == "engine":
            if can_contribute:
                r = contribute_issue(chart, issue)
                results.append(f"upstream {'OK' if r['ok'] else 'FAILED'}: {r['detail']}")
            elif contribute and not consented:
                results.append("upstream needs one-time consent: rerun with `--contribute --yes`")
            else:
                results.append("eligible to contribute upstream (rerun with --contribute)")
        actions.append({"issue": issue, "decision": decision, "scope": scope, "results": results})
    return {"evidence": evidence, "engine": eng, "issues": issues, "actions": actions,
            "acted": act, "contributed": can_contribute}


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
         f"acted: {res.get('acted')}; contributed: {res.get('contributed')}_", "",
         f"- surveys analyzed: {g['surveys']}  |  hypotheses: {g['total_hypotheses']} "
         f"({g['dedup_rate']*100:.0f}% deduped)  |  incubator: {g['incubator']}",
         "- engine attempts: " + (", ".join(
             f"{e}({sum(s.values())})" for e, s in g["engine_status"].items()) or "none yet"),
         "", f"## Issues ({len(res['issues'])})"]
    if not res["issues"]:
        L.append("_none — runs look healthy_")
    for a in res["actions"]:
        i = a["issue"]
        L.append("")
        L.append(f"### [{i.get('severity', '?').upper()}/{i.get('complexity', '?')}/"
                 f"{a.get('scope', 'local')}] {i.get('title')}")
        L.append(f"- triage: **{a['decision']}**")
        for r in a["results"]:
            L.append(f"  - {r}")
        L.append(f"- evidence: {i.get('evidence')}")
        L.append(f"- fix ({i.get('fix_kind')}): {i.get('explanation')}")
    return "\n".join(L) + "\n"
