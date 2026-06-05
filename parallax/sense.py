"""Git sensing — what changed (diff cadence) and the full source set
(archaeology cadence)."""

import subprocess
from pathlib import Path


def _git(target, *args):
    try:
        r = subprocess.run(["git", "-C", str(target), *args],
                           capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def current_commit(target):
    return _git(target, "rev-parse", "HEAD").strip() or None


def tracked_files_all(chart):
    out = _git(chart.target, "ls-files")
    files = []
    for line in out.splitlines():
        line = line.strip()
        if not line or any(p in chart.excludes for p in Path(line).parts):
            continue
        files.append(line)
    return files


def tracked_files(chart):
    return [f for f in tracked_files_all(chart)
            if not chart.extensions or Path(f).suffix in chart.extensions]


def changed_files(chart, since_commit):
    if not since_commit:
        return []
    out = _git(chart.target, "diff", "--name-only", f"{since_commit}..HEAD")
    raw = [l.strip() for l in out.splitlines() if l.strip()]
    tracked = set(tracked_files(chart))
    changed = [f for f in raw if f in tracked]
    dirs = {str(Path(f).parent) for f in changed}
    siblings = [f for f in tracked if str(Path(f).parent) in dirs]
    return sorted(set(changed) | set(siblings))
