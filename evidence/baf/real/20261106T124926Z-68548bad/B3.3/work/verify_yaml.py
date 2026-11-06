"""Minimal YAML-subset reader with YAML 1.1 implicit typing rules.

Supports exactly the subset used by our files:
  * block mappings (key: value)
  * block sequences (- item)
  * double-quoted scalars, single-quoted scalars, plain scalars
  * nested indentation
Plain scalars are resolved with YAML 1.1 rules (bool / int / float / null),
quoted scalars are ALWAYS strings -- this is what makes the verification
meaningful for codes like NO, ON and postal codes with leading zeros.
"""
import re

BOOL_TRUE = {"y", "Y", "yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON"}
BOOL_FALSE = {"n", "N", "no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF"}
NULLS = {"", "~", "null", "Null", "NULL"}


def resolve_plain(text):
    """YAML 1.1 implicit resolution of a plain (unquoted) scalar."""
    if text in NULLS:
        return None
    if text in BOOL_TRUE:
        return True
    if text in BOOL_FALSE:
        return False
    if re.fullmatch(r"[-+]?0[0-7_]+", text):            # YAML 1.1 octal, e.g. 0123
        return int(text.replace("_", ""), 8)
    if re.fullmatch(r"[-+]?(0|[1-9][0-9_]*)", text):     # decimal
        return int(text.replace("_", ""))
    if re.fullmatch(r"[-+]?0x[0-9a-fA-F_]+", text):      # hex
        return int(text.replace("_", ""), 16)
    if re.fullmatch(r"[-+]?([0-9][0-9_]*)?\.[0-9_]+([eE][-+]?[0-9]+)?", text):
        return float(text.replace("_", ""))
    return text


def parse_scalar(token):
    token = token.strip()
    if token.startswith('"') and token.endswith('"') and len(token) >= 2:
        return token[1:-1]          # double-quoted -> string
    if token.startswith("'") and token.endswith("'") and len(token) >= 2:
        return token[1:-1].replace("''", "'")   # single-quoted -> string
    return resolve_plain(token)


def _lines(text):
    out = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        out.append((len(raw) - len(raw.lstrip(" ")), raw.strip()))
    return out


def load(text):
    lines = _lines(text)

    def parse_block(i, indent):
        if i >= len(lines):
            return None, i
        if lines[i][1].startswith("- "):
            seq = []
            while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
                seq.append(parse_scalar(lines[i][1][2:]))
                i += 1
            return seq, i
        mapping = {}
        while i < len(lines) and lines[i][0] == indent:
            key, _, rest = lines[i][1].partition(":")
            key = parse_scalar(key)
            rest = rest.strip()
            i += 1
            if rest:
                mapping[key] = parse_scalar(rest)
            else:
                if i < len(lines) and lines[i][0] > indent:
                    val, i = parse_block(i, lines[i][0])
                else:
                    val = None
                mapping[key] = val
        return mapping, i

    doc, _ = parse_block(0, lines[0][0] if lines else 0)
    return doc
