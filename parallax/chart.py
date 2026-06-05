"""The chart — a target repo's `.parallax/` profile.

`<target>/.parallax/chart.yaml` selects language, engine routing, novelty, and
spawn behavior. The chart also resolves the on-disk layout: the repo-local atlas/
incubator/logbook/surveys/findings, plus the generic atlas shipped with the
parallax engine itself.
"""

import os
from pathlib import Path

from . import yaml_lite

PARALLAX_ROOT = Path(__file__).resolve().parent.parent  # the standalone repo root

DEFAULT_EXTENSIONS = {
    "solidity": [".sol"],
    "python": [".py"],
    "javascript": [".js", ".mjs", ".cjs", ".ts", ".tsx"],
    "generic": [".py", ".js", ".ts", ".sol", ".go", ".rs", ".rb"],
}
LANG_DIR = {"solidity": "SOL", "python": "PY", "javascript": "JS"}
DEFAULT_EXCLUDES = ["node_modules", "vendor", "out", "build", "cache", "dist",
                    ".git", "broadcast", "deployments", "assets", "fixtures"]
DEFAULT_ENGINES = {
    "ideate": ["local", "claude"],
    "investigate": ["claude", "local"],
    "verify": ["local", "claude"],
    "reflect": ["claude", "local"],
}


class Chart:
    def __init__(self, target, data):
        self.target = Path(target).resolve()
        self.data = data or {}
        d = self.data
        self.name = d.get("name", self.target.name)
        self.language = d.get("language", "generic")
        self.extensions = d.get("extensions", DEFAULT_EXTENSIONS.get(self.language, DEFAULT_EXTENSIONS["generic"]))
        self.excludes = d.get("excludes", DEFAULT_EXCLUDES)
        self.engines = d.get("engines", DEFAULT_ENGINES)
        self.max_files = int(d.get("max_files", 40))
        self.max_file_chars = int(d.get("max_file_chars", 16000))

        self.claude_model = d.get("claude_model")
        self.ollama_model = d.get("ollama_model")
        local = d.get("local") or {}
        self.local_endpoint = local.get("endpoint", "http://127.0.0.1:1234/v1")
        self.local_model = local.get("model")
        self.local_max_tokens = int(local.get("max_tokens", 3500))
        self.local_temperature = float(local.get("temperature", 0.7))

        nov = d.get("novelty") or {}
        self.embed_model = nov.get("embed_model", "text-embedding-nomic-embed-text-v1.5")
        self.novelty_embeddings = bool(nov.get("embeddings", True))
        self.embed_threshold = float(nov.get("cos_threshold", 0.72))
        self.jaccard_threshold = float(nov.get("jaccard_threshold", 0.6))

        spawn = d.get("spawn") or {}
        self.spawn_per_survey = int(spawn.get("per_survey", 3))
        self.spawn_auto_promote = bool(spawn.get("auto_promote", False))

        self.taxonomies = d.get("taxonomies", ["cwe", "owasp"])

        # layout
        self.dir = self.target / ".parallax"
        self.atlas_dir = self.dir / "atlas"
        self.incubator_dir = self.dir / "incubator"
        self.logbook_dir = self.dir / "logbook"
        self.surveys_dir = self.dir / "surveys"
        self.findings_dir = self.dir / "findings"
        for p in (self.atlas_dir, self.incubator_dir, self.logbook_dir,
                  self.surveys_dir, self.findings_dir):
            p.mkdir(parents=True, exist_ok=True)
        self.generic_atlas = PARALLAX_ROOT / "atlas"
        self.lang_dir = LANG_DIR.get(self.language)


def load_chart(path):
    target = Path(os.path.expanduser(path)).resolve()
    cf = target / ".parallax" / "chart.yaml"
    data = yaml_lite.load(cf.read_text()) if cf.exists() else {}
    return Chart(target, data)
