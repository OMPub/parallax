"""Stdlib unit tests (run: python tests/test_basics.py)."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from parallax import yaml_lite                       # noqa: E402
from parallax.sightline import (                     # noqa: E402
    Sightline, validate, load_dir, glob_match,
)


class YamlLite(unittest.TestCase):
    def test_roundtrip_block_scalar_and_flow(self):
        d = yaml_lite.load(
            "a: 1\nb: [x, y]\nt: { swc: [SWC-1] }\nc:\n  d: |\n    line1\n    # literal\n    line2\n")
        self.assertEqual(d["a"], 1)
        self.assertEqual(d["b"], ["x", "y"])
        self.assertEqual(d["t"]["swc"], ["SWC-1"])
        self.assertIn("line1", d["c"]["d"])
        self.assertIn("# literal", d["c"]["d"])
        self.assertEqual(yaml_lite.load(yaml_lite.dump(d)), d)


class Sightlines(unittest.TestCase):
    def test_all_shipped_atoms_validate(self):
        atoms = load_dir(ROOT / "atlas")
        self.assertTrue(atoms, "no shipped atoms found")
        for sl in atoms:
            self.assertEqual(validate(sl), [], f"{sl.id} invalid")

    def test_globs(self):
        self.assertTrue(glob_match("contracts/src/DocChain.sol", "**/*.sol"))
        self.assertFalse(glob_match("README.md", "**/*.sol"))

    def test_machine_static_executor_refused(self):
        sl = Sightline({
            "id": "X", "tier": "lens", "maturity": "candidate",
            "executors": [{"kind": "static", "command": ["gitleaks"]}],
            "lineage": {"origin": "machine"}})
        self.assertTrue(any("SECURITY" in e for e in validate(sl)))


if __name__ == "__main__":
    unittest.main()
