"""parallax — a continuous, self-evolving, LLM-driven vulnerability scanner.

The engine is the reusable rail; per-repo `.parallax/` charts adapt it. Stdlib
only (the local engine speaks the OpenAI-compatible API over urllib; YAML via the
vendored yaml_lite, or PyYAML if present).
"""

__version__ = "0.0.1"
