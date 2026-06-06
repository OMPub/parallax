"""REFLECT — emit candidate sightlines into the incubator.

After a survey, a meta-agent proposes new probes aimed at uncovered taxonomy
classes and angles not yet tried. Candidates are:
  * gated for novelty against the existing atlas + the logbook (no near-dupes)
  * always kind: llm (machines never author runnable shell commands — security)
  * written as maturity: candidate, lineage.origin: machine

Promotion (candidate -> active) is a separate, gated step (see __main__ promote).
"""

from . import engines, novelty, yaml_lite
from .sightline import load_dir


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
        sl = {
            "id": sid,
            "title": title,
            "taxonomy": cand.get("taxonomy", {}) or {},
            "tier": "lens",
            "maturity": "candidate",
            "applies_when": {"path_globs": _lang_globs(chart)},
            "executors": [{"kind": "llm", "engine": "ideate", "temperature": 0.9, "prompt": frame}],
            "lineage": {"origin": "machine", "parent": [a.id for a in chosen],
                        "survey": survey_id, "rationale": cand.get("rationale", ""),
                        "novelty": cand.get("novelty")},
            "yield": {"runs": 0, "confirmed": 0, "refuted": 0},
        }
        path = chart.incubator_dir / f"{sid}.yaml"
        path.write_text(yaml_lite.dump(sl) + "\n")
        spawned.append({"id": sid, "title": title,
                        "path": str(path.relative_to(chart.target))})
    return spawned
