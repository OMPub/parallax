# parallax

A continuous, self-evolving, LLM-driven vulnerability scanner. It hunts for *new*
classes of exploits run after run — never re-walking the same checks, always
probing fresh angles — while a deterministic floor guarantees the basics never
slip. It remembers what it has learned about each repo, rotates across multiple
models for genuine diversity of attack ideas, and adversarially verifies every
finding so you get signal, not noise.

It also improves *itself*: each run reflects on its own operation, auto-tunes what
it safely can, and — with your consent — contributes generalizable fixes back to
the shared engine, so every deployment hardens the tool for everyone.

> Stdlib-only Python 3.9+. [CC0](LICENSE) — public domain.

## How it works: rail + chart

- The **engine** (this repo) is reusable and repo-agnostic — *the rail*.
- Each target repo carries a **`.parallax/` chart** that adapts the engine to that
  codebase: its lenses, threat-model context, and the memory it accumulates.

So the machinery improves once for everyone; the understanding deepens per repo.

## The survey loop

```
SENSE (what changed + blast radius) → FLOOR (always-run deterministic checks)
  → SELECT  (a bandit picks which probes to run, biased to changed/neglected code)
  → IDEATE  (generate novel hypotheses, told "do not repeat" what's been tried)
  → FILTER  (drop hypotheses too similar to anything in memory)
  → INVESTIGATE (read the real code; a "confirmed" verdict MUST cite file:line)
  → VERIFY  (a *different* model tries to refute it; only survivors are reported)
  → RECORD  (memory, coverage, per-probe yield)
  → REFLECT (propose brand-new probes for next time)
```

**Why it doesn't repeat itself**
- A **bandit** rotates probes by yield + a novelty bonus, with a temporary
  refractory penalty — recently-walked paths are suppressed, never abandoned.
- **Memory-conditioned prompts**: every ideation step is shown what's already been
  tried and told to find materially different angles.
- An **embedding novelty filter** rejects hypotheses too similar to past ones —
  catching same-bug paraphrases that keyword matching misses.
- **Engine diversity**: ideation rotates across the models you configure, because
  different model families surface different bug classes.

**Why the basics stay covered**
A deterministic **floor** runs every survey, immune to the bandit: a built-in
secret scan plus any of `slither` / `semgrep` / `gitleaks` / `trivy` you have
installed. The creative LLM work sits *on top* of that floor, never instead of it.

**Why findings are trustworthy**
Every confirmed finding must point at a concrete `file:line` and survive an
adversarial verify pass by a *different* model. Ungrounded or refuted claims are
dropped, not reported.

## Sightlines: the probes

Each probe is one small declarative YAML file (Atomic-Red-Team style). `tier: floor`
probes always run; `tier: lens` probes are bandit-selected. Executors are either
`kind: static` (a deterministic command — the floor) or `kind: llm` (a prompt).

```yaml
id: SOL-SWC115-TXORIGIN
title: tx.origin authentication
taxonomy: { swc: [SWC-115] }
tier: lens
maturity: active                 # candidate | active | proven | dormant
applies_when: { path_globs: ["**/*.sol"], contains_any: ["tx.origin"] }
executors:
  - kind: llm
    engine: ideate
    prompt: |
      Find authorization checks that use tx.origin instead of msg.sender ...
```

Coverage is tracked against real taxonomies (SWC, DASP, CWE, OWASP), so you can
see "probes cover 18 of 37 SWC classes" rather than guessing.

## Two cadences

- **diff** (default): only files changed since the last survey, plus their blast
  radius. Wire it to a git hook or CI.
- **archaeology**: the least-recently-examined code first. Run it on a timer to
  keep re-examining old code with fresh angles.

## Self-evolution

Each survey's **reflect** step proposes new `candidate` probes into
`.parallax/incubator/`. `parallax promote` graduates ones that pass a novelty +
schema gate into the active atlas, where the bandit starts scheduling them; yield
later promotes them to `proven` or retires them to `dormant` (never deleted).

## The meta loop: it reviews itself

`parallax introspect` reviews parallax's **own** run history — not your code — to
catch anything degrading runs for reasons unrelated to scan quality (an engine
that's down, an endpoint that's unreachable, novelty saturating, and failure modes
no checklist anticipated, because the diagnosis is itself an open-ended LLM review).
Crucially, an *operational* failure is never mistaken for a low-value probe, so the
bandit's learning stays clean.

It then **triages** each issue:
- trivial + a safe, bounded config change → **applied locally** (`--act`)
- anything else → a local proposal, and (if it's a generalizable *engine* issue)
  eligible to contribute upstream.

## Contributing back (collective hardening)

Every deployment can harden the shared engine. When `introspect` finds a
generalizable engine issue, `--contribute` files it upstream — and it's built to be
safe for a *security* tool:

```bash
parallax introspect /path/to/repo --contribute --yes
```

- **Dedup**: it reads the engine repo's open issues and, if your finding matches
  one, **+1's it** with corroborating evidence instead of opening a duplicate.
- **Patches**: when the fix is concrete, the PR can include a draft patch
  (compile-checked, always a *draft* for human review).
- **Sanitized**: target names, paths, code, and findings are stripped — nothing
  about *your* repo leaves your machine; only the generalizable engine issue does.
- **Consent**: upstream contribution is off by default and requires a one-time
  `--yes`. Local fixes (`--act`) never leave the machine.

## Quick start

```bash
# 1. point parallax at a repo
mkdir -p /path/to/repo/.parallax/atlas
cat > /path/to/repo/.parallax/chart.yaml <<'YAML'
name: my-repo
language: python            # solidity | python | javascript | generic
taxonomies: [cwe, owasp]
engines:                    # any mix of: claude, codex, local
  ideate:      [claude]
  investigate: [claude]
  verify:      [claude]
  reflect:     [claude]
YAML

# 2. run
python -m parallax survey /path/to/repo
```

That's the simplest setup (one CLI engine). Add `codex` and/or a local
OpenAI-compatible server to the engine lists for cross-model diversity and lower
cost — `ideate` rotates across whatever you list.

### Commands

```bash
python -m parallax survey   <repo> [--mode diff|archaeology] [--dry-run]
python -m parallax floor    <repo>            # deterministic checks only
python -m parallax status   <repo>            # coverage, probe yields, history
python -m parallax atlas    list|validate <repo>
python -m parallax introspect <repo> [--act] [--contribute --yes]
python -m parallax promote  <repo> [--id ID]  # graduate an incubated probe
```

For unattended runs, `scripts/overnight.sh <hours> <repo...>` rotates surveys
across repos until a deadline, then runs local introspection.

## Engines & tools

- **LLM engines** (configure per chart): the `claude` CLI, the `codex` CLI, and/or
  any **local OpenAI-compatible server** (LM Studio, llama.cpp, Ollama's `/v1`,
  etc.). List several per role to rotate for diversity and resilience — if one is
  rate-limited or unreachable, parallax falls back to the next automatically.
- **Floor tools** (optional, auto-detected): `slither`, `semgrep`, `gitleaks`,
  `trivy`. Without them, a built-in dependency-free secret scan still runs.
- **YAML**: parsed by a vendored mini-loader, or PyYAML if installed.

## Security model

Enforced in code, not by convention:
- Machine-spawned probes may only be `kind: llm` — a self-evolving system never
  authors runnable shell commands.
- `static` executors run only whitelisted binaries.
- Scanned repo content is treated as untrusted **data**, never as instructions.
- Live/unpatched finding detail stays gitignored; only cleared knowledge is kept.
- Upstream contributions are sanitized and consent-gated.

## Layout

```
parallax/   engine (survey, selector, novelty, spawn, memory, engines,
            static_exec, sightline, taxonomy, introspect, chart, yaml_lite, report)
atlas/      curated generic probes: GENERIC/ SOL/ PY/ JS/
schema/     sightline.schema.json
scripts/    overnight.sh
```
Per target repo: `.parallax/{chart.yaml, atlas/, incubator/, logbook/, findings/, surveys/}`.

## License

[CC0 1.0 Universal](LICENSE) — public-domain dedication. No rights reserved.
