# parallax

A continuous, self-evolving, LLM-driven vulnerability scanner. It hunts for
novel exploits without repeating itself, keeps a persistent memory of what it has
learned, and guarantees the basics stay covered.

**Rail + chart** (mirrors doc-chain's own architecture): this standalone repo is
the reusable engine ("the rail"); each target repo carries a `.parallax/`
directory ("the chart") that selects, weights, and extends the engine's behavior.

## Vocabulary
| term | meaning |
|---|---|
| **sightline** | one atomic probe (a YAML file): applicability + executor(s) |
| **atlas** | the curated catalog of sightlines |
| **survey** | one scan run |
| **candidate** | a machine-spawned, unconfirmed sightline awaiting promotion |
| **logbook** | persistent memory over time |
| **chart** | a target's per-repo profile (`.parallax/chart.yaml`) |

## A sightline (the Atomic-Red-Team analog)
One YAML per probe. `tier: floor` atoms always run; `tier: lens` atoms are
selected by the bandit. Executors are `kind: static` (a deterministic command —
this *is* the basics floor) or `kind: llm` (a prompt + engine + temperature). The
floor and the creative lenses are just two executor kinds on one format.

```yaml
id: SOL-SWC115-TXORIGIN
title: tx.origin authentication
taxonomy: { swc: [SWC-115] }
tier: lens
maturity: active           # candidate | active | proven | dormant
applies_when: { path_globs: ["**/*.sol"], contains_any: ["tx.origin"] }
executors:
  - kind: llm
    engine: ideate
    temperature: 0.9
    prompt: |
      Find auth checks that use tx.origin instead of msg.sender ...
lineage: { origin: human }
yield: { runs: 0, confirmed: 0 }
```

## The survey loop
```
SENSE (git diff + blast radius) → FLOOR (always-run static atoms)
  → SELECT (bandit over applicable lens atoms; baseline re-arm on cadence)
  → IDEATE (hot, local model; memory-conditioned "do not repeat")
  → novelty-FILTER (embedding cosine; Jaccard fallback)
  → INVESTIGATE (claude; a "confirmed" verdict MUST cite file:line)
  → VERIFY (a *different* engine tries to refute; only survivors reported)
  → RECORD (logbook, coverage, per-sightline yield)
  → REFLECT (spawn ≤ N candidate sightlines into the incubator)
```

### How it doesn't repeat itself
1. **Bandit** over sightlines: yield + novelty bonus − refractory penalty, so
   recently-walked paths are suppressed (not banned) and basics never lapse.
2. **Memory-conditioned prompts**: every IDEATE gets the digest of past
   hypotheses with "propose materially different angles."
3. **Embedding novelty filter**: rejects hypotheses too similar (cosine) to the
   logbook — catches same-vuln paraphrases lexical matching misses.
4. **Engine + temperature diversity**: hot local ideation, cold claude verify.

### How the basics stay covered
`tier: floor` atoms (secrets, slither/semgrep/gitleaks/trivy) run every survey,
immune to the bandit and the maturity lifecycle.

## Two cadences
- **diff** (default): files changed since the last survey + blast radius — wire to a hook/CI.
- **archaeology**: least-recently/least-thoroughly examined files first — creatively re-examine OLD code on a cron.

## Self-evolution & promotion
REFLECT spawns `candidate` sightlines into `.parallax/incubator/`. Promotion gate
= schema-valid + novelty vs the atlas + (optional) human review. Promoted atoms
move to the atlas as `active` and enter the bandit. Yield later promotes
active→proven or demotes→dormant (never deleted).

> **Security invariants (enforced in code, not convention):**
> - Machine-spawned sightlines may only be `kind: llm`. A self-evolving system
>   must never author runnable shell commands. (`sightline.validate`)
> - Static executors run only whitelisted binaries. (`static_exec`)
> - Repo content is untrusted **data**, never instructions — targets like RSO
>   ingest external feeds and operator PRs.
> - Live/unpatched finding detail is gitignored; only cleared knowledge is committed.

## Usage
```bash
python -m parallax survey   ../doc-chain --mode diff
python -m parallax survey   ../doc-chain --mode diff --dry-run   # no API calls
python -m parallax floor    ../doc-chain                          # basics only
python -m parallax status   ../doc-chain
python -m parallax atlas    list ../doc-chain
python -m parallax atlas    validate ../doc-chain
python -m parallax promote  ../doc-chain [--id CAND-ID]
python tests/test_basics.py
```

Stdlib-only Python 3.9+. Engines: `claude`/`codex` CLIs + a local
OpenAI-compatible server (LM Studio / llama.cpp / ollama `/v1`); the floor uses
`slither`/`gitleaks`/`semgrep`/`trivy` if installed, else a built-in secret scan.
YAML via the vendored `yaml_lite` (auto-upgrades to PyYAML if present).

> Local note: a 27B reasoning model runs ~2 tok/s and can't disable its thinking,
> so ideate/verify on local take minutes — fine for cron, route to `claude` for
> fast interactive runs. Embeddings (novelty) are instant.

## Layout
```
parallax/        engine: __main__, survey, selector, novelty, spawn, memory,
                 engines, static_exec, sightline, taxonomy, chart, yaml_lite, report
atlas/           curated generic sightlines: GENERIC/ SOL/ PY/ JS/
incubator/       candidate landing (standalone experiments)
schema/          sightline.schema.json
tests/
```
Per target repo: `.parallax/{chart.yaml, atlas/, incubator/, logbook/, findings/, surveys/}`.

## License

Released under [CC0 1.0 Universal](LICENSE) — a public-domain dedication. No
rights reserved; use, modify, and redistribute freely.
