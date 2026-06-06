"""introspect — parallax looking back on its own runs (the meta loop).

Reads the survey history and surfaces OPERATIONAL health, separate from scan
quality: which engines are failing and why, which sightlines aborted for harness
reasons (and were therefore NOT penalized), whether novelty is saturating, and
whether the incubator is backing up. Emits heuristic recommendations — the
deterministic foundation the LLM self-healing agent will build on.
"""

import json
from pathlib import Path

from .memory import Logbook
from .sightline import load_dir


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
    engine_status = {}      # engine -> {status: count}
    truncations = 0
    total_hyp = total_dups = 0
    aborts = []             # {survey, sightline, reasons}
    for s in surveys:
        for t in s.get("engine_trace", []):
            es = engine_status.setdefault(t.get("engine"), {})
            st = t.get("status", "?")
            es[st] = es.get(st, 0) + 1
            if st == "error" and "truncat" in (t.get("detail") or "").lower():
                truncations += 1
        total_hyp += len(s.get("hypotheses", []))
        total_dups += len(s.get("skipped_duplicates", []))
        for a in s.get("operational_aborts", []):
            aborts.append({"survey": s.get("survey_id"), "sightline": a.get("sightline"),
                           "reasons": a.get("reasons")})
    return {
        "surveys": len(surveys),
        "engine_status": engine_status,
        "truncations": truncations,
        "total_hypotheses": total_hyp,
        "total_deduped": total_dups,
        "dedup_rate": (total_dups / (total_hyp + total_dups)) if (total_hyp + total_dups) else 0.0,
        "operational_aborts": aborts,
        "incubator": len(load_dir(chart.incubator_dir)),
        "yields": lb.yields(),
    }


def recommend(chart, g):
    recs = []
    if g["truncations"]:
        recs.append(f"Local model truncated {g['truncations']}x (finish_reason=length). "
                    f"Raise local.max_tokens (currently {getattr(chart, 'local_max_tokens', '?')}).")
    for eng, st in g["engine_status"].items():
        total = sum(st.values())
        errs = st.get("error", 0) + st.get("unparsable", 0)
        if total >= 2 and errs == total:
            recs.append(f"Engine '{eng}' failed every call ({errs}/{total}) — likely down, "
                        f"rate-limited, or misconfigured; check it or drop it from rotation.")
        elif total >= 4 and errs / total >= 0.5:
            recs.append(f"Engine '{eng}' failing {errs}/{total} calls — investigate before trusting it.")
    if g["operational_aborts"]:
        per = {}
        for a in g["operational_aborts"]:
            per[a["sightline"]] = per.get(a["sightline"], 0) + 1
        for sid, n in sorted(per.items(), key=lambda x: -x[1]):
            recs.append(f"Sightline '{sid}' aborted operationally {n}x (NOT penalized; re-armed). "
                        f"If it keeps aborting, the harness — not the lens — needs a fix.")
    if g["total_hypotheses"] + g["total_deduped"] >= 8 and g["dedup_rate"] >= 0.7:
        recs.append(f"{g['dedup_rate']*100:.0f}% of hypotheses were deduped — novelty is saturating on "
                    f"the lenses run; rotate to under-covered lenses or widen the search.")
    if g["incubator"] >= 5:
        recs.append(f"{g['incubator']} candidate sightlines are sitting in the incubator unreviewed — "
                    f"run `parallax promote` (or prune) so self-evolution actually lands.")
    if not recs:
        recs.append("No operational issues detected — runs look healthy.")
    return recs


def render(chart, g):
    L = [f"# parallax introspection — {chart.name}", "",
         f"- surveys analyzed: {g['surveys']}",
         f"- hypotheses: {g['total_hypotheses']} tried, {g['total_deduped']} deduped "
         f"({g['dedup_rate']*100:.0f}% dedup)",
         f"- incubator backlog: {g['incubator']} candidates", "",
         "## Engine health (ideate/investigate/verify/reflect attempts)"]
    if g["engine_status"]:
        for eng, st in sorted(g["engine_status"].items(), key=lambda x: str(x[0])):
            total = sum(st.values())
            parts = ", ".join(f"{k}={v}" for k, v in sorted(st.items()))
            L.append(f"- **{eng}**: {total} attempts ({parts})")
    else:
        L.append("- (no engine telemetry yet — run a survey)")
    L += ["", f"## Operational aborts ({len(g['operational_aborts'])})",
          "_Harness failures, not low-yield lenses — excluded from the bandit, left re-armed._"]
    for a in g["operational_aborts"]:
        L.append(f"- survey {a['survey']}: `{a['sightline']}` — {', '.join(a['reasons'] or [])}")
    L += ["", "## Recommendations"]
    for r in recommend(chart, g):
        L.append(f"- {r}")
    return "\n".join(L) + "\n"
