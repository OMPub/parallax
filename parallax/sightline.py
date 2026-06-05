"""Sightlines — atomic, declarative probe files (the Atomic-Red-Team analog).

One YAML per probe: taxonomy mapping, tier (floor|lens), maturity
(candidate|active|proven|dormant), applicability rules, and one or more
executors (kind: static = deterministic command = the "basics" floor; kind: llm
= a prompt run through an engine).
"""

import re
from pathlib import Path

from . import yaml_lite

TIERS = {"floor", "lens"}
MATURITIES = {"candidate", "active", "proven", "dormant"}
EXECUTOR_KINDS = {"static", "llm"}
# Static executors may only invoke these binaries (basename). A machine-spawned
# sightline can never add to this set — see validate().
STATIC_WHITELIST = {"slither", "semgrep", "gitleaks", "trivy", "grep", "builtin-secret-scan"}


def _glob_to_re(glob):
    # minimal glob with ** support
    out, i = "", 0
    while i < len(glob):
        if glob[i:i + 3] == "**/":
            out += "(?:.*/)?"
            i += 3
        elif glob[i:i + 2] == "**":
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        elif glob[i] == "?":
            out += "."
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile("^" + out + "$")


def glob_match(path, glob):
    return _glob_to_re(glob).match(path) is not None


class Sightline:
    def __init__(self, data, source_path=None):
        self.data = data or {}
        self.path = Path(source_path) if source_path else None
        self.id = self.data.get("id")
        self.title = self.data.get("title", self.id)
        self.taxonomy = self.data.get("taxonomy", {}) or {}
        self.tier = self.data.get("tier", "lens")
        self.maturity = self.data.get("maturity", "active")
        self.applies_when = self.data.get("applies_when", {}) or {}
        self.executors = self.data.get("executors", []) or []
        self.lineage = self.data.get("lineage", {}) or {}
        self.seed_yield = self.data.get("yield", {}) or {}

    @property
    def origin(self):
        return self.lineage.get("origin", "human")

    def llm_executors(self):
        return [e for e in self.executors if e.get("kind") == "llm"]

    def static_executors(self):
        return [e for e in self.executors if e.get("kind") == "static"]

    def taxonomy_ids(self):
        ids = []
        for scheme, vals in self.taxonomy.items():
            for v in (vals or []):
                ids.append(f"{scheme}:{v}")
        return ids

    def applies_to(self, rel_path, get_text):
        globs = self.applies_when.get("path_globs")
        if globs and not any(glob_match(rel_path, g) for g in globs):
            return False
        contains = self.applies_when.get("contains_any")
        if contains:
            text = get_text() or ""
            if not any(token in text for token in contains):
                return False
        return True


def validate(sl):
    """Return a list of error strings. Enforces the schema invariants *and* the
    security rule that machine-spawned sightlines may not ship static commands."""
    errs = []
    if not sl.id:
        errs.append("missing id")
    if sl.tier not in TIERS:
        errs.append(f"bad tier {sl.tier!r}")
    if sl.maturity not in MATURITIES:
        errs.append(f"bad maturity {sl.maturity!r}")
    if not sl.executors:
        errs.append("no executors")
    for e in sl.executors:
        k = e.get("kind")
        if k not in EXECUTOR_KINDS:
            errs.append(f"bad executor kind {k!r}")
        if k == "static":
            cmd = e.get("command") or []
            binary = (e.get("fallback") if not cmd else Path(str(cmd[0])).name)
            if binary not in STATIC_WHITELIST:
                errs.append(f"static command {binary!r} not in whitelist")
            if sl.origin == "machine":
                errs.append("SECURITY: machine-origin sightline may not declare a static executor")
        if k == "llm" and not e.get("prompt"):
            errs.append("llm executor missing prompt")
    return errs


def load_dir(directory):
    out = []
    p = Path(directory)
    if not p.exists():
        return out
    for f in sorted(p.rglob("*.yaml")):
        try:
            data = yaml_lite.load(f.read_text())
        except Exception:
            continue
        if isinstance(data, dict) and data.get("id"):
            out.append(Sightline(data, f))
    return out


def load_atlas(chart, include_maturities=("active", "proven")):
    """All applicable sightlines for this chart: the engine's generic atlas
    (GENERIC + the chart's language dir) plus the repo-local atlas. Filtered to
    the given maturities; floor-tier atoms are always kept."""
    dirs = [chart.generic_atlas / "GENERIC"]
    if chart.lang_dir:
        dirs.append(chart.generic_atlas / chart.lang_dir)
    dirs.append(chart.atlas_dir)
    seen, atoms = set(), []
    for d in dirs:
        for sl in load_dir(d):
            if sl.id in seen:
                continue
            if sl.tier == "floor" or sl.maturity in include_maturities:
                seen.add(sl.id)
                atoms.append(sl)
    return atoms
