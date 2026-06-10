"""parallax CLI.

  python -m parallax survey   <path> [--mode diff|archaeology] [--dry-run]
  python -m parallax floor    <path>      # deterministic floor only
  python -m parallax status   <path>      # logbook + coverage + yield
  python -m parallax atlas     list|validate <path>
  python -m parallax promote  <path> [--id ID]   # candidate -> active (gated)
"""

import argparse
import json
import sys

from . import engines, taxonomy, yaml_lite
from .chart import load_chart
from .memory import Logbook
from .sightline import load_atlas, load_dir, validate


def _banner(chart):
    avail = [e for e in ("claude", "codex", "ollama", "local") if engines.available(e, chart)]
    print(f"[parallax] engines: {', '.join(avail) or 'NONE (use --dry-run)'}")
    if engines.available("local", chart):
        active = engines._active_local(chart).get("endpoint")
        print(f"[parallax] local: {active} model={engines.local_chat_model(chart)}")
    elif any("local" in chain for chain in chart.engines.values()):
        # local is configured but unreachable — say so loudly, don't drop it silently.
        print(f"[parallax] WARNING: local endpoint {chart.local_endpoint} unreachable "
              f"({engines.local_probe_error(chart) or 'unknown'}); "
              f"ideate rotation + fallback will use claude/codex only this run")


def cmd_survey(args):
    engines.DRY_RUN = args.dry_run
    chart = load_chart(args.path)
    if args.engine:  # force ALL roles to one engine (e.g. fully local)
        chart.engines = {r: [args.engine] for r in ("ideate", "investigate", "verify", "reflect")}
    if args.max_tokens:
        chart.local_max_tokens = args.max_tokens
    if args.local_endpoint:
        chart.local_endpoint = args.local_endpoint
    if args.local_model:
        chart.local_model = args.local_model
    if args.embed_model:
        chart.embed_model = args.embed_model
    from . import report, survey
    print(f"[parallax] survey {chart.name} ({chart.target}) mode={args.mode} "
          f"dry_run={args.dry_run}" + (f" engine={args.engine}" if args.engine else ""))
    _banner(chart)
    rec = survey.run_survey(chart, mode=args.mode, k=args.sightlines,
                            n_ideate=args.ideate, n_investigate=args.investigate,
                            lens_override=args.lens)
    path = report.write(chart, rec)
    promoted = sum(1 for s in rec.get("spawned", []) if s.get("maturity") == "active")
    print(f"\n[parallax] survey {rec['survey_id']:05d}: {len(rec['findings'])} confirmed, "
          f"{len(rec['hypotheses'])} hypotheses, {len(rec['skipped_duplicates'])} dupes, "
          f"{len(rec['spawned'])} new lenses ({promoted} auto-trialed), "
          f"{len(rec.get('retired', []))} retired")
    print(f"[parallax] report: {path}")


def cmd_floor(args):
    chart = load_chart(args.path)
    from . import static_exec
    atoms = [a for a in load_atlas(chart) if a.tier == "floor"]
    print(json.dumps(static_exec.run_floor(chart, atoms), indent=2))


def cmd_status(args):
    chart = load_chart(args.path)
    lb = Logbook(chart)
    st, hyp, y = lb.state(), lb.hypotheses(), lb.yields()
    atoms = load_atlas(chart)
    print(f"chart:        {chart.name}")
    print(f"target:       {chart.target}")
    print(f"surveys:      {st.get('survey_count', 0)}")
    print(f"last commit:  {st.get('last_commit')}")
    print(f"atlas atoms:  {len(atoms)} active/proven/floor "
          f"(+{len(load_dir(chart.incubator_dir))} in incubator)")
    print(f"hypotheses:   {len(hyp)} tried "
          f"({sum(1 for h in hyp if h.get('outcome') == 'confirmed')} confirmed)")
    for scheme, info in taxonomy.summary(chart, atoms).items():
        print(f"  {scheme} coverage: {info['covered']}/{info['total']}")
    if y:
        print("sightline yield:")
        for sid, s in sorted(y.items(), key=lambda kv: -kv[1].get("runs", 0)):
            print(f"  {sid:30s} runs={s.get('runs',0):3d} "
                  f"confirmed={s.get('confirmed',0):2d} last={s.get('last_survey',0)}")


def cmd_atlas(args):
    chart = load_chart(args.path)
    if args.action == "validate":
        ok = True
        for d in (chart.generic_atlas, chart.atlas_dir, chart.incubator_dir):
            for sl in load_dir(d):
                errs = validate(sl)
                if errs:
                    ok = False
                    print(f"INVALID {sl.id} ({sl.path}):")
                    for e in errs:
                        print(f"   - {e}")
        print("all sightlines valid" if ok else "validation FAILED")
        sys.exit(0 if ok else 1)
    for sl in load_atlas(chart):
        print(f"[{sl.tier:5s}/{sl.maturity:9s}] {sl.id:28s} {sl.title}")
    cand = load_dir(chart.incubator_dir)
    if cand:
        print("-- incubator --")
        for sl in cand:
            print(f"[candidate     ] {sl.id:28s} {sl.title}")


def cmd_introspect(args):
    chart = load_chart(args.path)
    from . import introspect
    res = introspect.run(chart, act=args.act, contribute=args.contribute, yes=args.yes)
    print(introspect.render(chart, res))


def cmd_promote(args):
    chart = load_chart(args.path)
    from . import spawn
    free = max(0, chart.spawn_max_active - spawn._active_machine_count(chart))
    limit = args.limit if args.limit is not None else free  # default: fill to the trial cap
    promoted = []
    for sl in load_dir(chart.incubator_dir):
        if args.id and sl.id != args.id:
            continue
        if not args.id and len(promoted) >= limit:
            break
        errs = validate(sl)
        if errs:
            print(f"skip {sl.id}: {errs}")
            continue
        sl.data["maturity"] = "active"
        (chart.atlas_dir / f"{sl.id}.yaml").write_text(yaml_lite.dump(sl.data) + "\n")
        sl.path.unlink()
        promoted.append(sl.id)
    print(f"promoted {len(promoted)} (trial cap {chart.spawn_max_active}, {free} slots were free): "
          f"{', '.join(promoted) or '(none)'}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="parallax")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("survey")
    s.add_argument("path")
    s.add_argument("--mode", choices=["diff", "archaeology"], default="diff")
    s.add_argument("--sightlines", type=int, default=2)
    s.add_argument("--ideate", type=int, default=4)
    s.add_argument("--investigate", type=int, default=2)
    s.add_argument("--lens", action="append", default=None,
                   help="force a specific sightline id (repeatable)")
    s.add_argument("--engine", choices=["claude", "codex", "local", "ollama"], default=None,
                   help="force ALL roles to one engine (e.g. --engine local for fully local)")
    s.add_argument("--max-tokens", type=int, default=None,
                   help="override the local engine token budget for this run")
    s.add_argument("--local-endpoint", default=None,
                   help="override the local OpenAI-compatible endpoint (e.g. http://192.168.0.7:1234/v1)")
    s.add_argument("--local-model", default=None, help="override the local chat model id")
    s.add_argument("--embed-model", default=None, help="override the embedding model id")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_survey)

    f = sub.add_parser("floor")
    f.add_argument("path")
    f.set_defaults(func=cmd_floor)

    st = sub.add_parser("status")
    st.add_argument("path")
    st.set_defaults(func=cmd_status)

    al = sub.add_parser("atlas")
    al.add_argument("action", choices=["list", "validate"])
    al.add_argument("path")
    al.set_defaults(func=cmd_atlas)

    intr = sub.add_parser("introspect",
                          help="LLM meta-review of parallax's own runs; triage + (with --act) fix/PR")
    intr.add_argument("path")
    intr.add_argument("--act", action="store_true",
                      help="LOCAL only: auto-apply whitelisted config fixes + write local proposals")
    intr.add_argument("--contribute", action="store_true",
                      help="opt-in: file SANITIZED, engine-scope issues upstream (dedup-aware, draft PRs)")
    intr.add_argument("--yes", action="store_true",
                      help="grant the one-time consent to contribute upstream")
    intr.set_defaults(func=cmd_introspect)

    pr = sub.add_parser("promote")
    pr.add_argument("path")
    pr.add_argument("--id", default=None)
    pr.add_argument("--limit", type=int, default=None,
                    help="max candidates to promote (default: fill to the trial cap)")
    pr.set_defaults(func=cmd_promote)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
