"""yaml_lite — a tiny, dependency-free YAML reader/writer for the sightline subset.

Resolves the "stdlib-only + YAML" tension: if PyYAML is importable we use it
(strictly better); otherwise we fall back to this loader, which handles exactly
the constructs sightlines and Atomic-Red-Team-style atoms use:

  - nested block maps (indentation)
  - block lists ("- item" and "- key: val" maps)
  - flow collections ({a: b}, [x, y])
  - block scalars ("|" keep-newline, "|-" strip)
  - quoted/plain scalars, ints, floats, bools, null, "#" comments

It is NOT a general YAML parser. We control the sightline schema and the machine
spawner round-trips through dump(), so the dialect stays within these bounds.
"""

import re

try:  # prefer the real thing when available
    import yaml as _pyyaml

    def load(text):
        return _pyyaml.safe_load(text)

    _HAVE_PYYAML = True
except Exception:  # pragma: no cover - exercised only without PyYAML
    _HAVE_PYYAML = False

_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def _scalar(s):
    s = s.strip()
    if s == "":
        return None
    if (s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'"):
        return s[1:-1]
    low = s.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if _INT.match(s):
        return int(s)
    if _FLOAT.match(s):
        return float(s)
    return s


# --- flow collections: {a: b, c: [x, y]} / [a, b] ---------------------------
def _skip_ws(s, i):
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


def _flow_scalar(s, i):
    i = _skip_ws(s, i)
    if i < len(s) and s[i] in "\"'":
        q = s[i]
        j = i + 1
        while j < len(s) and s[j] != q:
            j += 1
        return s[i + 1:j], j + 1
    j = i
    while j < len(s) and s[j] not in ",}]:":
        j += 1
    return _scalar(s[i:j]), j


def _flow_value(s, i):
    i = _skip_ws(s, i)
    if i >= len(s):
        return None, i
    if s[i] == "{":
        d = {}
        i += 1
        while True:
            i = _skip_ws(s, i)
            if i >= len(s) or s[i] == "}":
                return d, i + 1
            key, i = _flow_scalar(s, i)
            i = _skip_ws(s, i)
            if i < len(s) and s[i] == ":":
                i += 1
            v, i = _flow_value(s, i)
            d[str(key)] = v
            i = _skip_ws(s, i)
            if i < len(s) and s[i] == ",":
                i += 1
                continue
            return d, i + 1
    if s[i] == "[":
        a = []
        i += 1
        while True:
            i = _skip_ws(s, i)
            if i >= len(s) or s[i] == "]":
                return a, i + 1
            v, i = _flow_value(s, i)
            a.append(v)
            i = _skip_ws(s, i)
            if i < len(s) and s[i] == ",":
                i += 1
                continue
            return a, i + 1
    return _flow_scalar(s, i)


def _parse_flow(s):
    return _flow_value(s.strip(), 0)[0]


# --- block structure --------------------------------------------------------
class _Reader:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0

    def peek(self):
        """Return (indent, stripped, raw_index) of the next significant line, or
        None. Skips blank/comment/`---` lines, leaving self.i at the hit."""
        while self.i < len(self.lines):
            ln = self.lines[self.i]
            st = ln.strip()
            if st == "" or st.startswith("#") or st == "---":
                self.i += 1
                continue
            return len(ln) - len(ln.lstrip(" ")), st, self.i
        return None

    def block_scalar(self, parent_indent, strip_trailing):
        body, base = [], None
        while self.i < len(self.lines):
            ln = self.lines[self.i]
            if ln.strip() == "":
                body.append("")
                self.i += 1
                continue
            indent = len(ln) - len(ln.lstrip(" "))
            if indent <= parent_indent:
                break
            if base is None:
                base = indent
            body.append(ln[base:])
            self.i += 1
        while body and body[-1] == "":
            body.pop()
        text = "\n".join(body)
        return text if strip_trailing else text + "\n"

    def node(self, min_indent):
        p = self.peek()
        if p is None or p[0] < min_indent:
            return None
        indent, text, _ = p
        if text == "-" or text.startswith("- "):
            return self.parse_list(indent)
        return self.parse_map(indent)

    def _value_from_inline(self, rest, indent):
        rest = rest.strip()
        if rest in ("|", "|+"):
            return self.block_scalar(indent, strip_trailing=False)
        if rest == "|-":
            return self.block_scalar(indent, strip_trailing=True)
        if rest == "":
            return self.node(indent + 1)
        if rest[0] in "{[":
            return _parse_flow(rest)
        return _scalar(rest)

    def parse_map(self, indent):
        d = {}
        while True:
            p = self.peek()
            if p is None:
                break
            ind, text, _ = p
            if ind != indent or text == "-" or text.startswith("- "):
                break
            self.i += 1  # consume the peeked line
            key, _, rest = text.partition(":")
            d[str(_scalar(key))] = self._value_from_inline(rest, indent)
        return d

    def parse_list(self, indent):
        items = []
        while True:
            p = self.peek()
            if p is None:
                break
            ind, text, _ = p
            if ind != indent or not (text == "-" or text.startswith("- ")):
                break
            self.i += 1
            after = "" if text == "-" else text[1:].strip()
            content_indent = indent + 2
            if after == "":
                items.append(self.node(indent + 1))
            elif after in ("|", "|-", "|+"):
                items.append(self.block_scalar(indent, strip_trailing=(after == "|-")))
            elif after[0] in "{[":
                items.append(_parse_flow(after))
            elif ":" in after and after[0] not in "\"'":
                # "- key: val" — a map whose first key sits on the dash line.
                k, _, v = after.partition(":")
                m = {str(_scalar(k)): self._value_from_inline(v, content_indent)}
                m.update(self.parse_map(content_indent))
                items.append(m)
            else:
                items.append(_scalar(after))
        return items


if not _HAVE_PYYAML:
    def load(text):
        return _Reader(text.split("\n")).node(0)


# --- writer (used by the spawner to emit candidate sightlines) --------------
# Leading chars YAML treats as indicators (alias, anchor, tag, flow, etc.).
_NEED_QUOTE_START = set("!&*?|>%@`\"'#,[]{} -")
_YAML_KEYWORDS = {"null", "true", "false", "yes", "no", "on", "off", "~"}


def _dump_scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if (s == "" or s[0] in _NEED_QUOTE_START or s[-1] == " "
            or ": " in s or " #" in s or s.lower() in _YAML_KEYWORDS):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def dump(obj, indent=0):
    pad = "  " * indent
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict) and v:
                out.append(f"{pad}{k}:")
                out.append(dump(v, indent + 1))
            elif isinstance(v, list) and v:
                out.append(f"{pad}{k}:")
                out.append(_dump_list(v, indent + 1))
            elif isinstance(v, str) and "\n" in v:
                out.append(f"{pad}{k}: |")
                for bl in v.rstrip("\n").split("\n"):
                    out.append(f"{'  ' * (indent + 1)}{bl}")
            else:
                out.append(f"{pad}{k}: {_dump_scalar(v)}")
    elif isinstance(obj, list):
        out.append(_dump_list(obj, indent))
    else:
        out.append(f"{pad}{_dump_scalar(obj)}")
    return "\n".join(out)


def _dump_list(items, indent):
    pad = "  " * indent
    out = []
    for it in items:
        if isinstance(it, dict):
            body = dump(it, indent + 1).split("\n")
            out.append(f"{pad}- {body[0].lstrip()}")
            out.extend(body[1:])
        else:
            out.append(f"{pad}- {_dump_scalar(it)}")
    return "\n".join(out)
