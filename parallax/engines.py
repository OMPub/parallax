"""LLM engine adapters (claude / codex / local OpenAI-compatible / ollama).

Each engine is a pure text-in/text-out completion. Source is passed *in the
prompt* (read by Python), so engines stay deterministic and need no permission
grants unattended. Engine diversity is an anti-repetition lever: different model
families have different priors -> different findings. ``resolve()`` walks a
role's ordered preference list and returns the first available engine.
"""

import json
import shutil
import subprocess
import urllib.error
import urllib.request

DRY_RUN = False
TIMEOUT = 240            # CLI engines
LOCAL_TIMEOUT = 1500     # local reasoning models are slow (~2 tok/s on a 27B)

_OLLAMA_CACHE = {}
_LOCAL_CACHE = {}


def available(engine, chart=None):
    if engine == "claude":
        return shutil.which("claude") is not None
    if engine == "codex":
        return shutil.which("codex") is not None
    if engine == "ollama":
        return shutil.which("ollama") is not None and ollama_model(chart) is not None
    if engine == "local":
        return _local_probe(chart)["ok"]
    return False


def resolve(chart, role, exclude=None):
    chain = chart.engines.get(role, [])
    avail = [e for e in chain if available(e, chart)]
    if not avail:
        return None
    if exclude:
        alt = [e for e in avail if e != exclude]
        if alt:
            return alt[0]
    return avail[0]


def call_parsed(chart, role, prompt, *, want, temperature=None, max_tokens=None, exclude=None):
    """Try each available engine in the role's chain until one returns parseable
    JSON of the wanted shape ("list" or "dict"). This is the resilience layer: if
    the primary (e.g. a local reasoning model) truncates or emits no JSON, the
    next engine (e.g. claude) takes over automatically. Returns (parsed, engine)
    or (None, first_engine). ``exclude`` deprioritizes (but doesn't ban) an
    engine, used so verify prefers a different model than investigate."""
    chain = [e for e in chart.engines.get(role, []) if available(e, chart)]
    if exclude:
        others = [e for e in chain if e != exclude]
        if others:
            chain = others + [e for e in chain if e == exclude]
    for eng in chain:
        try:
            raw = call(eng, prompt, chart, temperature=temperature, max_tokens=max_tokens)
        except Exception:
            continue
        data = extract_json(raw)
        if want == "list" and isinstance(data, list) and data:
            return data, eng
        if want == "dict" and isinstance(data, dict) and data:
            return data, eng
    return None, (chain[0] if chain else None)


def call(engine, prompt, chart=None, temperature=None, max_tokens=None):
    if DRY_RUN:
        return _dry(prompt)
    if engine == "claude":
        return _claude(prompt, chart)
    if engine == "codex":
        return _codex(prompt)
    if engine == "ollama":
        return _ollama(prompt, chart)
    if engine == "local":
        return _local(prompt, chart, temperature, max_tokens)
    raise RuntimeError(f"unknown engine: {engine}")


# --- CLI engines ------------------------------------------------------------
def _run(cmd, stdin=None, timeout=TIMEOUT):
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{cmd[0]} timed out after {timeout}s")
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {r.returncode}: {r.stderr.strip()[:300]}")
    return r.stdout


def _claude(prompt, chart):
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if chart is not None and getattr(chart, "claude_model", None):
        cmd += ["--model", chart.claude_model]
    out = _run(cmd)
    try:
        return json.loads(out).get("result", out)
    except Exception:
        return out


def _codex(prompt):
    return _run(["codex", "exec", prompt])


def _ollama(prompt, chart):
    model = ollama_model(chart)
    if not model:
        raise RuntimeError("no suitable ollama model installed")
    return _run(["ollama", "run", model, prompt])


def ollama_model(chart=None):
    if chart is not None and getattr(chart, "ollama_model", None):
        return chart.ollama_model
    if "list" not in _OLLAMA_CACHE:
        try:
            out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=20).stdout
        except Exception:
            out = ""
        _OLLAMA_CACHE["list"] = [l.split()[0] for l in out.splitlines()[1:] if l.split()]
    for token in ("coder", "qwen", "deepseek", "codestral", "codellama", "llama3", "mistral"):
        for m in _OLLAMA_CACHE["list"]:
            if token in m.lower():
                return m
    return None


# --- local OpenAI-compatible server (LM Studio / llama.cpp / ollama /v1) -----
def _local_endpoint(chart):
    return getattr(chart, "local_endpoint", None) or "http://127.0.0.1:1234/v1"


def _local_probe(chart):
    ep = _local_endpoint(chart)
    if ep in _LOCAL_CACHE:
        return _LOCAL_CACHE[ep]
    info = {"ok": False, "models": []}
    try:
        with urllib.request.urlopen(ep + "/models", timeout=4) as r:
            data = json.loads(r.read().decode())
        info["models"] = [m.get("id") for m in data.get("data", []) if m.get("id")]
        info["ok"] = True
    except Exception:
        pass
    _LOCAL_CACHE[ep] = info
    return info


def local_chat_model(chart):
    if chart is not None and getattr(chart, "local_model", None):
        return chart.local_model
    for m in _local_probe(chart)["models"]:
        if "embed" not in m.lower():
            return m
    return None


def _post_json(url, payload, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _local(prompt, chart, temperature, max_tokens):
    ep = _local_endpoint(chart)
    model = local_chat_model(chart)
    if not model:
        raise RuntimeError("no local chat model available")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature if temperature is not None else getattr(chart, "local_temperature", 0.7),
        "max_tokens": max_tokens or getattr(chart, "local_max_tokens", 3500),
    }
    try:
        d = _post_json(ep + "/chat/completions", payload, LOCAL_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"local HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        raise RuntimeError(f"local call failed: {e}")
    choice = d["choices"][0]
    msg = choice["message"]
    content = (msg.get("content") or "").strip()
    # If the model was truncated mid-reasoning (finish_reason == "length") and
    # produced no answer, return EMPTY rather than its chain-of-thought. The raw
    # reasoning is not an answer, and substituting it would make a failed call
    # look successful — defeating the caller's engine fallback (e.g. -> claude).
    # Only fall back to reasoning_content when the model actually stopped.
    if not content and choice.get("finish_reason") != "length":
        content = (msg.get("reasoning_content") or "").strip()
    return content


def embed(texts, chart):
    """Embedding vectors for texts via the local server, or None if unavailable."""
    if not _local_probe(chart)["ok"]:
        return None
    ep = _local_endpoint(chart)
    model = getattr(chart, "embed_model", None) or "text-embedding-nomic-embed-text-v1.5"
    try:
        d = _post_json(ep + "/embeddings", {"model": model, "input": texts}, 120)
        return [row["embedding"] for row in d["data"]]
    except Exception:
        return None


# --- helpers ----------------------------------------------------------------
def _dry(prompt):
    if "ROLE: IDEATE" in prompt:
        return json.dumps([
            {"claim": "append() trusts caller-supplied parentHash without verifying the parent exists",
             "file": "contracts/DocChain.sol", "line": 0, "rationale": "dry", "severity_guess": "high"},
            {"claim": "EIP-1271 isValidSignature compared by truthiness, not the bytes4 magic value",
             "file": "contracts/DocChain.sol", "line": 0, "rationale": "dry", "severity_guess": "medium"},
        ])
    if "ROLE: INVESTIGATE" in prompt:
        return json.dumps({"verdict": "confirmed", "severity": "high", "file": "contracts/DocChain.sol",
                           "line": 42, "evidence": "dry: no parent-existence check", "fix": "require(blocks[parentHash].exists)"})
    if "ROLE: VERIFY" in prompt:
        return json.dumps({"still_holds": True, "reason": "dry: refutation failed"})
    if "ROLE: REFLECT" in prompt:
        return json.dumps([{"title": "Storage-collision in proxy init", "taxonomy": {"swc": ["SWC-119"]},
                            "rationale": "dry candidate", "prompt": "Look for storage slot collisions...",
                            "novelty": 0.8, "severity_guess": "high"}])
    return "{}"


def extract_json(text):
    if text is None:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
    dec = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch in "[{":
            try:
                obj, _ = dec.raw_decode(t[i:])
                return obj
            except Exception:
                continue
    return None
