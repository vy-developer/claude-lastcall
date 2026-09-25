"""Just enough TOML to read a few keys out of ~/.codex/config.toml. Read-only.

tomllib only exists on Python 3.11+, and Last Call supports 3.9, so this reads
the file line by line: table headers (`[a."b.c".d]`), `key = value` lines and
one-line inline tables (`"k" = { trust_level = "trusted" }`). Everything is
flattened to {(path, parts, ...): value}. Anything it does not understand
(multi-line strings and arrays, arrays of tables) is skipped rather than
guessed at; the callers only ever ask "is this one key set?".
"""

import json
import re

_KEY = r"""(?:"(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_-]+)"""
_DOTTED = r"%s(?:\s*\.\s*%s)*" % (_KEY, _KEY)
_ONE_KEY = re.compile(r"\s*(%s)\s*" % _KEY)
_HEADER = re.compile(r"^\[\s*(%s)\s*\]\s*(?:#.*)?$" % _DOTTED)
_ASSIGN = re.compile(r"^(%s)\s*=\s*(.*)$" % _DOTTED)
_INLINE_PAIR = re.compile(
    r"""(%s)\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*'|[^,}]+)""" % _DOTTED)


def _unquote(token):
    if token.startswith('"'):
        try:
            return json.loads(token)
        except ValueError:
            return token[1:-1]
    if token.startswith("'"):
        return token[1:-1]
    return token


def split_key(text):
    """'a."b.c".d' -> ['a', 'b.c', 'd']; None when it is not a key."""
    parts, pos = [], 0
    while True:
        match = _ONE_KEY.match(text, pos)
        if not match:
            return None
        parts.append(_unquote(match.group(1)))
        pos = match.end()
        if pos == len(text):
            return parts
        if text[pos] != ".":
            return None
        pos += 1


def _scalar(raw):
    raw = raw.strip()
    match = re.match(r'"((?:[^"\\]|\\.)*)"', raw)
    if match:
        return _unquote('"%s"' % match.group(1))
    match = re.match(r"'([^']*)'", raw)
    if match:
        return match.group(1)
    raw = raw.split("#", 1)[0].strip()
    if raw in ("true", "false"):
        return raw == "true"
    if re.match(r"^[+-]?\d+$", raw):
        return int(raw)
    return raw


def parse(text):
    """{tuple of key parts: value} for every simple key in ``text``."""
    flat, section = {}, ()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            header = None if line.startswith("[[") else _HEADER.match(line)
            parts = split_key(header.group(1)) if header else None
            section = tuple(parts) if parts else None
            continue
        if section is None:
            continue  # inside an array of tables, or after a header we cannot read
        assign = _ASSIGN.match(line)
        if not assign:
            continue
        key = split_key(assign.group(1))
        if not key:
            continue
        path = section + tuple(key)
        value = assign.group(2).strip()
        if value.startswith("{"):
            body = value[1:value.rfind("}")] if "}" in value else value[1:]
            for pair in _INLINE_PAIR.finditer(body):
                inner = split_key(pair.group(1))
                if inner:
                    flat[path + tuple(inner)] = _scalar(pair.group(2))
            continue
        flat[path] = _scalar(value)
    return flat


def load(path):
    """parse() of the file at ``path``; {} when it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            return parse(handle.read())
    except (OSError, UnicodeDecodeError):
        return {}
