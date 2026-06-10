"""REFLECT + the auto-trial lifecycle.

After a survey, a meta-agent proposes new probes (lenses). Each is:
  * gated for novelty against the existing atlas + the logbook (no near-dupes)
  * always kind: llm (machines never author runnable shell commands — security)
  * lineage.origin: machine

Lifecycle (repo-local, no human in the loop):
  * If the chart sets spawn.auto_promote, a novel + schema-valid candidate is
    promoted straight to `active` (entering the bandit on the next survey) up to a
    bounded trial population; otherwise it waits in the incubator for `promote`.
  * retire_dead_weight() later demotes machine lenses that proved unproductive
    (many runs, 0 confirmed) to `dormant`, freeing trial slots.
The human gate is reserved for UPSTREAM contribution, not local exploration.
"""

from . import engines, novelty, yaml_lite
from .sightline import Sightline, load_dir, validate

# Default bound on auto-promoted (machine-origin) active lenses per repo (chart
# may override via spawn.max_active); retirement frees slots as duds go dormant.
MAX_ACTIVE_MACHINE = 24


def _active_machine_count(chart):
    return sum(1 for sl in load_dir(chart.atlas_dir)
               if sl.origin == "machine" and sl.maturity == "active")


def _lang_globs(chart):
    return [f"**/*{ext}" for ext in chart.extensions] or ["**/*"]


def _existing_texts(chart):
    texts = []
    dirs = [chart.generic_atlas / "GENERIC", chart.atlas_dir, chart.incubator_dir]
    if chart.lang_dir:
        dirs.append(chart.generic_atlas / chart.lang_dir)
    for d in dirs:
        for sl in load_dir(d):
            texts.append(f"{sl.title} {sl.data.get('lineage', {}).get('rationale', '')}")
    return texts


def _candidate_checker(chart, texts):
    vecs = None
    if chart.novelty_embeddings and not engines.DRY_RUN and engines.available("local", chart):
        vecs = engines.embed(texts, chart) if texts else []
    return novelty.Checker(texts, vecs, jaccard_threshold=chart.jaccard_threshold,
                           cos_threshold=chart.embed_threshold)


def reflect(chart, record, chosen, prior, survey_id):
    if not [e for e in chart.engines.get("reflect", []) if engines.available(e, chart)]:
        return []
    gaps = []
    for scheme, info in record.get("taxonomy_coverage", {}).items():
        for g in info.get("gaps", [])[:8]:
            gaps.append(f"{scheme}:{g}")
    tried = "\n".join(f"- [{h['outcome']}] {h['claim']}" for h in record["hypotheses"][:40]) or "(none)"
    digest = "\n".join(f"- {c}" for c in prior[:50]) or "(none)"
    n = chart.spawn_per_survey
    prompt = f"""### PARALLAX ROLE: REFLECT
You just surveyed a {chart.language} repo. Propose up to {n} NEW sightlines
(probes) to run next — angles materially different from what was tried, aimed at
UNCOVERED bug classes. Each is an LLM reasoning probe (a lens); never a shell command.

## Sightlines run this survey
{', '.join(a.title for a in chosen) or '(none)'}

## Hypotheses tried this survey (with outcomes)
{tried}

## Uncovered taxonomy classes (prioritize)
{', '.join(gaps) or '(none listed)'}

## Already explored over time (do NOT reword these)
{digest}

Return a JSON array of up to {n}:
  {{"title": "<short probe name>",
    "taxonomy": {{"swc": ["SWC-XXX"]}} ,
    "prompt": "<the lens framing an ideation agent will use>",
    "rationale": "<why novel and valuable>",
    "severity_guess": "low|medium|high|critical", "novelty": 0.0}}
Only output the JSON array."""
    data, _eng, _tr = engines.call_parsed(chart, "reflect", prompt, want="list", temperature=0.8,
                                          max_tokens=getattr(chart, "local_max_tokens", 3500))
    if not data:
        return []

    checker = _candidate_checker(chart, _existing_texts(chart) + prior)
    auto = getattr(chart, "spawn_auto_promote", False)
    cap = getattr(chart, "spawn_max_active", MAX_ACTIVE_MACHINE)
    slots = max(0, cap - _active_machine_count(chart)) if auto else 0
    spawned = []
    for i, cand in enumerate(data[:n]):
        title = (cand.get("title") or "").strip()
        frame = (cand.get("prompt") or "").strip()
        if not title or not frame:
            continue
        key = f"{title} {cand.get('rationale', '')}"
        if not checker.is_novel(key):
            continue
        checker.add(key)
        sid = f"{chart.lang_dir or 'GEN'}-CAND-{survey_id:03d}-{i:02d}"
        # Auto-trial: promote straight to active (repo-local) when enabled and a
        # trial slot is free; else it waits in the incubator for `promote`.
        promote = auto and slots > 0
        sl = {
            "id": sid,
            "title": title,
            "taxonomy": cand.get("taxonomy", {}) or {},
            "tier": "lens",
            "maturity": "active" if promote else "candidate",
            "applies_when": {"path_globs": _lang_globs(chart)},
            "executors": [{"kind": "llm", "engine": "ideate", "temperature": 0.9, "prompt": frame}],
            "lineage": {"origin": "machine", "parent": [a.id for a in chosen],
                        "survey": survey_id, "rationale": cand.get("rationale", ""),
                        "novelty": cand.get("novelty")},
            "yield": {"runs": 0, "confirmed": 0, "refuted": 0},
        }
        # Never promote something that fails schema or the machine-llm-only rule.
        if promote and validate(Sightline(sl)):
            promote = False
            sl["maturity"] = "candidate"
        path = (chart.atlas_dir if promote else chart.incubator_dir) / f"{sid}.yaml"
        path.write_text(yaml_lite.dump(sl) + "\n")
        if promote:
            slots -= 1
        spawned.append({"id": sid, "title": title, "maturity": sl["maturity"],
                        "path": str(path.relative_to(chart.target))})
    return spawned


def retire_dead_weight(chart, logbook):
    """Demote machine-origin active lenses that have proven unproductive
    (>= spawn.retire_after runs with 0 confirmed and 0 operational aborts) to
    'dormant', freeing trial slots. Human-authored baseline atoms are never
    auto-retired — the bandit down-weights those via yield. Dormant atoms stay on
    disk, never deleted."""
    min_runs = getattr(chart, "spawn_retire_after", 6)
    y = logbook.yields()
    retired = []
    for sl in load_dir(chart.atlas_dir):
        if sl.origin != "machine" or sl.maturity != "active":
            continue
        s = y.get(sl.id, {})
        if s.get("runs", 0) >= min_runs and s.get("confirmed", 0) == 0 \
                and s.get("operational_aborts", 0) == 0:
            sl.data["maturity"] = "dormant"
            sl.path.write_text(yaml_lite.dump(sl.data) + "\n")
            retired.append(sl.id)
    return retired
