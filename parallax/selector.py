"""The bandit — explore/exploit selection over applicable `lens`-tier sightlines.

  exploit    sightlines whose llm executors have historically confirmed bugs
  explore    UCB bonus for rarely-run sightlines
  novelty    bonus that grows the longer a sightline has gone unused
  refractory temporary penalty on sightlines run in the last few surveys
             (suppressed, never banned -> pathways cycle, basics never abandoned)

Floor-tier sightlines are NOT subject to this; they always run (enforced in the
survey loop). Every BASELINE_EVERY surveys a proven sightline is force-armed so
canonical high-value checks can't silently lapse.
"""

import math

EXPLORE_C = 0.7
NOVELTY_W = 0.3
REFRACTORY_WINDOW = 2
REFRACTORY_PENALTY = 0.35
BASELINE_EVERY = 4


def _score(stat, total_runs, survey_id):
    runs = stat.get("runs", 0)
    exploit = stat.get("confirmed", 0) / (runs + 1)
    explore = EXPLORE_C * math.sqrt(math.log(total_runs + 1) / (runs + 1))
    last = stat.get("last_survey", 0)
    idle = survey_id - last if last else survey_id
    novelty = NOVELTY_W * min(idle, 10)
    val = exploit + explore + novelty
    if last and (survey_id - last) <= REFRACTORY_WINDOW:
        val *= REFRACTORY_PENALTY
    return val


def select(lens_atoms, logbook, survey_id, k):
    """Pick up to k lens-tier sightlines. On the baseline cadence, force-arm the
    most-neglected proven one first."""
    y = logbook.yields()
    total = sum(v.get("runs", 0) for v in y.values()) + 1
    ranked = sorted(
        lens_atoms,
        key=lambda sl: _score(y.get(sl.id, {}), total, survey_id),
        reverse=True)
    chosen = []
    if survey_id % BASELINE_EVERY == 0:
        proven = [sl for sl in lens_atoms if sl.maturity == "proven"]
        proven.sort(key=lambda sl: y.get(sl.id, {}).get("last_survey", 0))
        if proven:
            chosen.append(proven[0])
    for sl in ranked:
        if sl not in chosen:
            chosen.append(sl)
        if len(chosen) >= k:
            break
    return chosen[:k]
