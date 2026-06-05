"""The survey loop.

  SENSE → FLOOR (always) → SELECT (bandit over applicable lens atoms)
        → IDEATE (hot, local) → novelty-FILTER → INVESTIGATE (claude, must
          ground at file:line) → VERIFY (different engine, refute)
        → RECORD → REFLECT (spawn candidate sightlines)
"""

from pathlib import Path

from . import engines, novelty, selector, sense, spawn, static_exec, taxonomy
from .memory import Logbook
from .sightline import load_atlas


def _read_file(chart, rel):
    try:
        return (chart.target / rel).read_text(errors="ignore")[: chart.max_file_chars]
    except Exception:
        return ""


def _candidate_files(chart, lb, mode):
    changed = sense.changed_files(chart, lb.state().get("last_commit"))
    if mode == "diff" and changed:
        return changed, "changed"
    allsrc = sense.tracked_files(chart)
    if mode == "diff":
        return allsrc[: chart.max_files], "all (no diff)"
    return lb.least_covered(allsrc)[: chart.max_files], "neglected-first"


def _applies(chart, atom, files, cache):
    out = []
    for f in files:
        if atom.applies_to(f, lambda f=f: cache.setdefault(f, _read_file(chart, f))):
            out.append(f)
    return out


def _build_checker(chart, prior):
    prior_vecs = None
    if chart.novelty_embeddings and not engines.DRY_RUN and engines.available("local", chart):
        prior_vecs = engines.embed(prior, chart) if prior else []
    return novelty.Checker(prior, prior_vecs,
                           jaccard_threshold=chart.jaccard_threshold,
                           cos_threshold=chart.embed_threshold)


def _ideate(chart, atom, executor, files, prior, n):
    listing = "\n".join(f"- {f}" for f in files[:40]) or "(no applicable files)"
    digest = "\n".join(f"- {c}" for c in prior[:60]) or "(none yet)"
    prompt = f"""### PARALLAX ROLE: IDEATE
You are probing a {chart.language} codebase through one sightline.

## Sightline: {atom.title}  [{', '.join(atom.taxonomy_ids()) or 'untagged'}]
{executor.get('prompt', '')}

## Applicable files
{listing}

## Hypotheses ALREADY TRIED (do NOT repeat or lightly reword)
{digest}

Return a JSON array of {n} objects with genuinely new angles:
  {{"claim": "<concrete, testable hypothesis>", "file": "<path>",
    "rationale": "<why this sightline surfaces it>",
    "severity_guess": "low|medium|high|critical"}}
Only output the JSON array."""
    temp = executor.get("temperature", 0.9)
    ideas, eng = engines.call_parsed(chart, "ideate", prompt, want="list",
                                     temperature=temp,
                                     max_tokens=getattr(chart, "local_max_tokens", 3500))
    return (ideas or []), eng


def _investigate(chart, hyp):
    rel = hyp.get("file", "")
    code = _read_file(chart, rel)
    numbered = "\n".join(f"{i+1}: {ln}" for i, ln in enumerate(code.splitlines()[:600]))
    prompt = f"""### PARALLAX ROLE: INVESTIGATE
Decide whether this hypothesis is a REAL vulnerability in the code below. Be
rigorous; do not invent issues. A "confirmed" verdict MUST cite a concrete line.

## Hypothesis
{hyp.get('claim')}

## File: {rel} (line-numbered)
{numbered}

Return ONE JSON object:
  {{"verdict": "confirmed|refuted|inconclusive", "severity": "low|medium|high|critical",
    "file": "{rel}", "line": <int line number of the sink>,
    "evidence": "<specific code reference>", "fix": "<concrete remediation>"}}
Only output the JSON object."""
    data, eng = engines.call_parsed(chart, "investigate", prompt, want="dict", temperature=0.1)
    return data, eng


def _grounded(finding):
    """FP control: a confirmed finding must point at a concrete line with evidence."""
    try:
        line = int(finding.get("line", 0))
    except Exception:
        line = 0
    return line > 0 and bool(str(finding.get("evidence", "")).strip())


def _verify(chart, hyp, finding, investigate_engine):
    rel = hyp.get("file", "")
    code = _read_file(chart, rel)
    prompt = f"""### PARALLAX ROLE: VERIFY
Another analyst claims the finding below is real. Your job is to REFUTE it: look
for why it is a false positive, mitigated elsewhere, or unreachable. Default to
skepticism.

## Claimed finding ({finding.get('severity')}) at {rel}:{finding.get('line')}
{finding.get('evidence')}

## File
{code}

Return ONE JSON object: {{"still_holds": true|false, "reason": "<why>"}}
Only output the JSON object."""
    data, eng = engines.call_parsed(chart, "verify", prompt, want="dict",
                                    temperature=0.2, exclude=investigate_engine)
    if not data:
        return {"still_holds": True, "reason": "no verifier verdict"}, eng
    return data, eng


def run_survey(chart, mode="diff", k=2, n_ideate=4, n_investigate=2, lens_override=None):
    lb = Logbook(chart)
    survey_id = lb.next_survey_id()
    commit = sense.current_commit(chart.target)

    atoms = load_atlas(chart)
    floor_atoms = [a for a in atoms if a.tier == "floor"]
    lens_atoms = [a for a in atoms if a.tier == "lens"]

    files, basis = _candidate_files(chart, lb, mode)
    cache = {}

    # FLOOR — always, no bandit discretion
    floor_results = static_exec.run_floor(chart, floor_atoms)

    # applicability + selection over lens atoms
    applicable = [(a, m) for a in lens_atoms if (m := _applies(chart, a, files, cache))]
    if lens_override:
        want = set(lens_override)
        chosen = [a for a, _ in applicable if a.id in want]
        if not chosen:
            chosen = selector.select([a for a, _ in applicable], lb, survey_id, k)
    else:
        chosen = selector.select([a for a, _ in applicable], lb, survey_id, k)
    applies_map = {a.id: m for a, m in applicable}

    prior = lb.prior_claims()
    checker = _build_checker(chart, prior)
    record = {
        "survey_id": survey_id, "mode": mode, "basis": basis, "commit": commit,
        "files_considered": len(files), "novelty_mode": checker.mode,
        "chosen_sightlines": [a.id for a in chosen],
        "taxonomy_coverage": taxonomy.summary(chart, atoms),
        "floor": floor_results, "hypotheses": [], "findings": [],
        "skipped_duplicates": [], "spawned": [],
    }

    for atom in chosen:
        atom_files = applies_map.get(atom.id, files)
        confirmed = refuted = explored = 0
        for executor in atom.llm_executors():
            ideas, ideate_eng = _ideate(chart, atom, executor, atom_files, prior, n_ideate)
            valid = [(h, (h.get("claim") or "").strip()) for h in ideas]
            valid = [(h, c) for h, c in valid if c]
            vecs = None
            if checker.mode == "embeddings" and valid:
                vecs = engines.embed([c for _, c in valid], chart)
                if not vecs or len(vecs) != len(valid):
                    vecs = None
            for idx, (hyp, claim) in enumerate(valid):
                vec = vecs[idx] if vecs else None
                if not checker.is_novel(claim, vec):
                    record["skipped_duplicates"].append(claim)
                    continue
                checker.add(claim, vec)
                explored += 1
                outcome = "explored"
                if explored <= n_investigate:
                    finding, inv_eng = _investigate(chart, hyp)
                    if finding and finding.get("verdict") == "confirmed" and _grounded(finding):
                        vres, ver_eng = _verify(chart, hyp, finding, inv_eng)
                        if vres.get("still_holds"):
                            outcome = "confirmed"
                            confirmed += 1
                            record["findings"].append({
                                "sightline": atom.id, "taxonomy": atom.taxonomy_ids(),
                                "claim": claim, "file": finding.get("file"),
                                "line": finding.get("line"), "severity": finding.get("severity"),
                                "evidence": finding.get("evidence"), "fix": finding.get("fix"),
                                "verified_by": ver_eng, "verify_reason": vres.get("reason")})
                        else:
                            outcome = "refuted-by-verify"
                            refuted += 1
                    elif finding and finding.get("verdict") == "confirmed":
                        outcome = "ungrounded"  # claimed but no concrete line -> dropped
                    elif finding:
                        outcome = finding.get("verdict", "inconclusive")
                lb.append_hypothesis({
                    "survey_id": survey_id, "sightline": atom.id, "claim": claim,
                    "file": hyp.get("file"), "outcome": outcome, "commit": commit,
                    "ideate_engine": ideate_eng})
                record["hypotheses"].append({"claim": claim, "outcome": outcome, "sightline": atom.id})
                if hyp.get("file"):
                    lb.update_coverage(hyp["file"], survey_id, commit, atom.id)
        lb.record_yield(atom.id, survey_id, confirmed=confirmed, refuted=refuted, explored=explored)

    # REFLECT — spawn candidate sightlines into the incubator
    record["spawned"] = spawn.reflect(chart, record, chosen, prior, survey_id)

    lb.set_last_commit(commit)
    lb.write_survey(survey_id, record)
    return record
