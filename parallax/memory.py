"""The logbook — persistent, git-trackable memory under `.parallax/logbook/`.

  state.json        last surveyed commit + monotonic survey counter
  hypotheses.jsonl  every hypothesis ever tried + outcome  (anti-repeat ledger)
  yield.json        per-sightline dynamic stats feeding the bandit (NOT written
                    back into curated atlas files, so the catalog history stays clean)
  coverage.json     per-file: last survey/commit examined, sightlines applied
"""

import json
import time
from pathlib import Path


def _read(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _write(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


class Logbook:
    def __init__(self, chart):
        self.c = chart
        self.state_path = chart.logbook_dir / "state.json"
        self.hyp_path = chart.logbook_dir / "hypotheses.jsonl"
        self.yield_path = chart.logbook_dir / "yield.json"
        self.coverage_path = chart.logbook_dir / "coverage.json"

    # state
    def state(self):
        return _read(self.state_path, {"last_commit": None, "survey_count": 0})

    def next_survey_id(self):
        st = self.state()
        st["survey_count"] = int(st.get("survey_count", 0)) + 1
        _write(self.state_path, st)
        return st["survey_count"]

    def set_last_commit(self, commit):
        st = self.state()
        st["last_commit"] = commit
        _write(self.state_path, st)

    def next_rotation(self, key="ideate"):
        """Persistent round-robin counter (advances across surveys/restarts) so
        the ideate engine rotates evenly over time."""
        st = self.state()
        field = f"{key}_rotation"
        i = int(st.get(field, 0))
        st[field] = i + 1
        _write(self.state_path, st)
        return i

    # hypotheses
    def hypotheses(self):
        if not self.hyp_path.exists():
            return []
        out = []
        for line in self.hyp_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out

    def prior_claims(self, limit=300):
        claims = [h.get("claim", "") for h in self.hypotheses() if h.get("claim")]
        return list(reversed(claims))[:limit]

    def append_hypothesis(self, rec):
        rec.setdefault("ts", int(time.time()))
        with self.hyp_path.open("a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    # per-sightline yield (bandit input)
    def yields(self):
        return _read(self.yield_path, {})

    def sightline_stat(self, sid):
        return self.yields().get(sid, {"runs": 0, "confirmed": 0, "refuted": 0, "last_survey": 0})

    def record_yield(self, sid, survey_id, confirmed=0, refuted=0, explored=0):
        y = self.yields()
        s = y.setdefault(sid, {"runs": 0, "confirmed": 0, "refuted": 0, "explored": 0, "last_survey": 0})
        s["runs"] += 1
        s["confirmed"] += confirmed
        s["refuted"] += refuted
        s["explored"] += explored
        s["last_survey"] = survey_id
        _write(self.yield_path, y)

    def record_operational(self, sid, survey_id, reasons):
        """An operational abort (engine error/truncation), NOT a low-yield run.
        Deliberately does NOT touch runs/confirmed/last_survey — so the bandit's
        quality signal stays clean and the sightline remains re-armed for retry.
        Tracked separately so introspection can see (and heal) the failure."""
        y = self.yields()
        s = y.setdefault(sid, {"runs": 0, "confirmed": 0, "refuted": 0, "explored": 0, "last_survey": 0})
        s["operational_aborts"] = s.get("operational_aborts", 0) + 1
        s["last_operational"] = {"survey": survey_id, "reasons": reasons}
        _write(self.yield_path, y)

    # coverage
    def coverage(self):
        return _read(self.coverage_path, {})

    def update_coverage(self, file, survey_id, commit, sid):
        c = self.coverage()
        e = c.setdefault(file, {"last_survey": 0, "last_commit": None, "sightlines": []})
        e["last_survey"] = survey_id
        e["last_commit"] = commit
        if sid and sid not in e["sightlines"]:
            e["sightlines"].append(sid)
        _write(self.coverage_path, c)

    def least_covered(self, files):
        c = self.coverage()
        return sorted(files, key=lambda f: c.get(f, {}).get("last_survey", 0))

    # survey record
    def write_survey(self, survey_id, record):
        _write(self.c.surveys_dir / f"survey-{survey_id:05d}.json", record)
