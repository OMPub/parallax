"""Taxonomy anchors for coverage maps and dedupe keys.

Coverage is reported as "atoms covering N of M classes" per scheme — a legible,
honest measure of how much of the known bug-class space the atlas reaches.
"""

SCHEMES = {
    "swc": [f"SWC-{n}" for n in range(100, 137)],          # 37 Smart Contract Weakness classes
    "dasp": [f"DASP-{n}" for n in range(1, 11)],           # DASP Top 10
    "owasp": [f"A{n:02d}" for n in range(1, 11)],          # OWASP Top 10 (2021)
    "cwe": ["CWE-79", "CWE-89", "CWE-20", "CWE-125", "CWE-78", "CWE-416", "CWE-22",
            "CWE-352", "CWE-434", "CWE-862", "CWE-476", "CWE-287", "CWE-190", "CWE-502",
            "CWE-77", "CWE-119", "CWE-798", "CWE-918", "CWE-306", "CWE-362", "CWE-269",
            "CWE-94", "CWE-863", "CWE-400", "CWE-732"],     # CWE Top 25 (representative)
}


def coverage(scheme, atoms):
    universe = set(SCHEMES.get(scheme, []))
    covered = set()
    for sl in atoms:
        for v in (sl.taxonomy.get(scheme) or []):
            covered.add(v)
    return covered & universe, universe


def summary(chart, atoms):
    out = {}
    for scheme in chart.taxonomies:
        cov, uni = coverage(scheme, atoms)
        out[scheme] = {"covered": len(cov), "total": len(uni),
                       "gaps": sorted(uni - cov)}
    return out
