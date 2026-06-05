"""Static executors — the deterministic floor. Two paths:

  * external tools (slither / semgrep / gitleaks / trivy), run best-effort
  * a dependency-free built-in secret scan (so the floor has teeth on a bare box)

SECURITY: only whitelisted binaries run, and a static executor is *refused* if it
belongs to a machine-origin sightline. A self-evolving system must never be able
to spawn-and-run a new shell command.
"""

import re
import shutil
import subprocess
from pathlib import Path

from . import sense
from .sightline import STATIC_WHITELIST

SECRET_FILENAMES = re.compile(
    r"(^|/)(\.env(\.[\w-]+)?$|id_rsa|id_ed25519|.*\.pem|.*\.p12|.*\.keystore|"
    r"credentials\.json|service-account.*\.json)$")
SECRET_EXEMPT = re.compile(r"\.(example|sample|template|dist)$|\.env\.example$")
CONTENT_PATTERNS = [
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("generic-secret", re.compile(
        r"(?i)(api[_-]?key|secret|password|passwd|token|private[_-]?key)\s*[:=]\s*['\"][^'\"]{12,}['\"]")),
    ("hex-private-key", re.compile(r"(?i)(priv(ate)?[_-]?key|secret)\s*[:=]\s*['\"]?0x?[0-9a-f]{64}")),
]


def _builtin_secret_scan(chart):
    findings = []
    for rel in sense.tracked_files_all(chart):
        if SECRET_EXEMPT.search(rel):
            continue
        if SECRET_FILENAMES.search(rel):
            findings.append({"severity": "high", "file": rel,
                             "detail": "secret-bearing filename tracked in git; confirm it holds no live credentials"})
        path = chart.target / rel
        try:
            if path.stat().st_size > 1_500_000:
                continue
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        for name, pat in CONTENT_PATTERNS:
            if pat.search(text):
                findings.append({"severity": "high", "file": rel, "detail": f"matched {name} pattern"})
                break
    return findings


def run_executor(chart, sightline, executor):
    """Run one static executor. Returns {sightline, status, findings|summary}."""
    if sightline.origin == "machine":
        return {"sightline": sightline.id, "status": "refused",
                "detail": "machine-origin sightline may not run static commands"}
    cmd = executor.get("command") or []
    fallback = executor.get("fallback")
    binary = Path(str(cmd[0])).name if cmd else fallback
    if binary not in STATIC_WHITELIST:
        return {"sightline": sightline.id, "status": "refused",
                "detail": f"binary {binary!r} not in whitelist"}

    if cmd and shutil.which(cmd[0]):
        try:
            r = subprocess.run(cmd, cwd=str(chart.target), capture_output=True,
                               text=True, timeout=600)
            return {"sightline": sightline.id, "status": "ran", "tool": binary,
                    "returncode": r.returncode, "summary": (r.stdout or r.stderr)[-3000:]}
        except subprocess.TimeoutExpired:
            return {"sightline": sightline.id, "status": "timeout", "tool": binary}
        except Exception as e:
            return {"sightline": sightline.id, "status": f"error: {e}", "tool": binary}

    # tool missing -> use built-in fallback if declared
    if fallback == "builtin-secret-scan":
        f = _builtin_secret_scan(chart)
        return {"sightline": sightline.id, "status": "ran", "tool": "builtin-secret-scan",
                "findings": f}
    return {"sightline": sightline.id, "status": "tool-not-installed", "tool": binary}


def run_floor(chart, floor_atoms):
    return [run_executor(chart, sl, ex) for sl in floor_atoms for ex in sl.static_executors()]
