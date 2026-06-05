"""Render a survey record into a markdown report under `.parallax/surveys/`."""

SEV = {"critical": 0, "high": 1, "medium": 2, "low": 3, None: 4}


def render(chart, rec):
    L = []
    a = L.append
    a(f"# parallax survey {rec['survey_id']:05d} — {chart.name}")
    a("")
    a(f"- target: `{chart.target}`")
    a(f"- commit: `{rec.get('commit')}`")
    a(f"- mode: **{rec['mode']}** ({rec['basis']}), {rec['files_considered']} files considered")
    a(f"- sightlines run: {', '.join(rec['chosen_sightlines']) or '(none applicable)'}")
    a(f"- novelty filter: **{rec.get('novelty_mode', 'jaccard')}**")
    a("")

    findings = sorted(rec["findings"], key=lambda f: SEV.get(f.get("severity"), 4))
    a(f"## Confirmed findings ({len(findings)})")
    if not findings:
        a("_None survived adversarial verification this survey._")
    for f in findings:
        a("")
        a(f"### [{(f.get('severity') or '?').upper()}] {f.get('file')}:{f.get('line')}")
        a(f"- **sightline:** {f.get('sightline')}  ({', '.join(f.get('taxonomy', [])) or 'untagged'})")
        a(f"- **claim:** {f.get('claim')}")
        a(f"- **evidence:** {f.get('evidence')}")
        a(f"- **fix:** {f.get('fix')}")
        a(f"- **verified by:** {f.get('verified_by')} — {f.get('verify_reason')}")
    a("")

    a("## Deterministic floor")
    for r in rec["floor"]:
        line = f"- `{r['sightline']}` → {r['status']}"
        if r.get("tool"):
            line += f" ({r['tool']})"
        a(line)
        for bf in r.get("findings", []):
            a(f"    - [{bf['severity']}] `{bf['file']}` — {bf['detail']}")
    if not rec["floor"]:
        a("_no floor-tier sightlines applied_")
    a("")

    a("## Taxonomy coverage")
    for scheme, info in rec.get("taxonomy_coverage", {}).items():
        a(f"- **{scheme}**: atoms cover {info['covered']} of {info['total']} classes")
    a("")

    a("## Exploration this survey")
    a(f"- hypotheses tried: {len(rec['hypotheses'])}")
    a(f"- skipped as duplicates: {len(rec['skipped_duplicates'])}")
    for h in rec["hypotheses"]:
        a(f"  - [{h['outcome']}] ({h['sightline']}) {h['claim']}")
    a("")

    a(f"## Spawned candidate sightlines ({len(rec['spawned'])})")
    if not rec["spawned"]:
        a("_none spawned_")
    for s in rec["spawned"]:
        a(f"- `{s['id']}` — {s['title']}  →  `{s['path']}`")
    a("")
    return "\n".join(L)


def write(chart, rec):
    md = render(chart, rec)
    path = chart.surveys_dir / f"survey-{rec['survey_id']:05d}.md"
    path.write_text(md + "\n")
    return path
