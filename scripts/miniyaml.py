"""
Minimal YAML reader - just enough of the spec for Ansible inventories and
small config files, so the project runs on a base Python install without
PyYAML.

Supported:
- block mappings and block sequences ("- item"), nested by indentation
- flow mappings / sequences on one line: {x: 400, y: 100}, [a, b], {}
- plain, 'single' and "double" quoted scalars
- int / float / true / false / yes / no / null / ~ coercion
- literal / folded block scalars (|, |-, |+, >, >-, >+)
- comments (full-line and trailing " # ..."), "---" / "..." markers

Not supported (raises YamlError if it notices): anchors/aliases, tags,
explicit block-scalar indentation indicators, multiple documents,
complex keys.
That's deliberate - an inventory that needs those should be simplified
rather than this growing into a full YAML implementation.

Usage mirrors yaml.safe_load: miniyaml.safe_load(stream_or_text).
"""
from __future__ import annotations

import re

__all__ = ["safe_load", "YamlError"]


class YamlError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"^[-+]?(0|[1-9][0-9_]*)$")
_FLOAT_RE = re.compile(r"^[-+]?([0-9][0-9_]*)?\.[0-9_]+([eE][-+][0-9]+)?$")
_TRUE = {"true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"}
_FALSE = {"false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"}
_NULL = {"", "~", "null", "Null", "NULL"}

_DQ_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/",
    " ": " ", "b": "\b", "f": "\f", "a": "\a", "e": "\x1b",
}


def _unquote_double(body: str, lineno: int) -> str:
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= len(body):
            raise YamlError(f"line {lineno}: dangling backslash in quoted string")
        nxt = body[i + 1]
        if nxt in _DQ_ESCAPES:
            out.append(_DQ_ESCAPES[nxt])
            i += 2
        elif nxt in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[nxt]
            out.append(chr(int(body[i + 2:i + 2 + width], 16)))
            i += 2 + width
        else:
            raise YamlError(f"line {lineno}: unknown escape \\{nxt}")
    return "".join(out)


def _scalar(text: str, lineno: int):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return _unquote_double(text[1:-1], lineno)
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if text[:1] in ("&", "*", "!", "|", ">"):
        raise YamlError(f"line {lineno}: unsupported YAML feature in {text!r}")
    if text in _NULL:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    if _INT_RE.match(text):
        return int(text.replace("_", ""))
    if _FLOAT_RE.match(text):
        return float(text.replace("_", ""))
    return text


# ---------------------------------------------------------------------------
# Line-level helpers
# ---------------------------------------------------------------------------

def _strip_comment(line: str) -> str:
    """Drop a trailing comment, respecting quotes. A '#' only starts a
    comment at the start of the line or after whitespace."""
    quote = None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1  # skip the escaped character
            elif c == quote:
                # '' inside single quotes is an escaped quote, not a close
                if quote == "'" and i + 1 < len(line) and line[i + 1] == "'":
                    i += 1
                else:
                    quote = None
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
        i += 1
    return line.rstrip()


def _split_key(text: str, lineno: int):
    """Split 'key: value' at the first unquoted ': ' (or trailing ':').
    Returns (key, rest) or None if the text isn't a mapping entry."""
    quote = None
    depth = 0
    for i, c in enumerate(text):
        if quote:
            if c == quote:
                quote = None
            continue
        if c in "'\"" and (i == 0 or text[i - 1] in " \t{[,"):
            quote = c
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        elif c == ":" and depth == 0 and (i + 1 == len(text) or text[i + 1] in " \t"):
            key = _scalar(text[:i], lineno)
            if isinstance(key, (dict, list)):
                raise YamlError(f"line {lineno}: complex keys aren't supported")
            return key, text[i + 1:].strip()
    return None


# ---------------------------------------------------------------------------
# Flow collections: {a: 1, b: [x, y]}
# ---------------------------------------------------------------------------

def _parse_flow(text: str, lineno: int):
    value, pos = _flow_value(text, 0, lineno)
    if text[pos:].strip():
        raise YamlError(f"line {lineno}: trailing characters after flow collection: {text[pos:]!r}")
    return value


def _skip_ws(text, pos):
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    return pos


def _flow_value(text: str, pos: int, lineno: int):
    pos = _skip_ws(text, pos)
    if pos >= len(text):
        return None, pos
    c = text[pos]
    if c == "{":
        result = {}
        pos += 1
        while True:
            pos = _skip_ws(text, pos)
            if pos < len(text) and text[pos] == "}":
                return result, pos + 1
            key, pos = _flow_scalar(text, pos, lineno, stop=":,}")
            pos = _skip_ws(text, pos)
            if pos < len(text) and text[pos] == ":":
                value, pos = _flow_value(text, pos + 1, lineno)
            else:
                value = None
            result[key] = value
            pos = _skip_ws(text, pos)
            if pos < len(text) and text[pos] == ",":
                pos += 1
            elif pos < len(text) and text[pos] == "}":
                continue
            else:
                raise YamlError(f"line {lineno}: unterminated flow mapping")
    if c == "[":
        result = []
        pos += 1
        while True:
            pos = _skip_ws(text, pos)
            if pos < len(text) and text[pos] == "]":
                return result, pos + 1
            value, pos = _flow_value(text, pos, lineno)
            result.append(value)
            pos = _skip_ws(text, pos)
            if pos < len(text) and text[pos] == ",":
                pos += 1
            elif pos < len(text) and text[pos] == "]":
                continue
            else:
                raise YamlError(f"line {lineno}: unterminated flow sequence")
    return _flow_scalar(text, pos, lineno, stop=",]}")


def _flow_scalar(text: str, pos: int, lineno: int, stop: str):
    pos = _skip_ws(text, pos)
    if pos < len(text) and text[pos] in "'\"":
        q = text[pos]
        end = pos + 1
        while end < len(text):
            if text[end] == q:
                if q == "'" and end + 1 < len(text) and text[end + 1] == "'":
                    end += 2
                    continue
                break
            if q == '"' and text[end] == "\\":
                end += 1
            end += 1
        if end >= len(text):
            raise YamlError(f"line {lineno}: unterminated quoted string")
        return _scalar(text[pos:end + 1], lineno), end + 1
    start = pos
    while pos < len(text) and text[pos] not in stop:
        # ':' only separates when followed by whitespace (so 'a:b' stays a scalar)
        if text[pos] == ":" and ":" in stop and not (pos + 1 == len(text) or text[pos + 1] in " \t,}]"):
            pos += 1
            continue
        if text[pos] == ":" and ":" in stop:
            break
        pos += 1
    return _scalar(text[start:pos], lineno), pos


# ---------------------------------------------------------------------------
# Block structure
# ---------------------------------------------------------------------------

def _value(text: str, lineno: int):
    if text.startswith(("{", "[")):
        return _parse_flow(text, lineno)
    return _scalar(text, lineno)


class _Parser:
    def __init__(self, text: str):
        self.raw = text.splitlines()
        self.lines: list[tuple[int, int, str]] = []  # (lineno, indent, content)
        for lineno, raw in enumerate(text.splitlines(), 1):
            if "\t" in raw[:len(raw) - len(raw.lstrip())]:
                raise YamlError(f"line {lineno}: tabs aren't allowed for indentation")
            line = _strip_comment(raw)
            if not line.strip():
                continue
            if line.strip() in ("---", "..."):
                if self.lines and line.strip() == "---":
                    raise YamlError(f"line {lineno}: multiple documents aren't supported")
                continue
            indent = len(line) - len(line.lstrip(" "))
            self.lines.append((lineno, indent, line.strip()))
        self.pos = 0

    def parse(self):
        if not self.lines:
            return None
        value = self._block(self.lines[0][1])
        if self.pos < len(self.lines):
            lineno = self.lines[self.pos][0]
            raise YamlError(f"line {lineno}: unexpected indentation")
        return value

    def _block(self, indent: int):
        lineno, _, content = self.lines[self.pos]
        if content == "-" or content.startswith("- "):
            return self._sequence(indent)
        if _split_key(content, lineno) is not None:
            return self._mapping(indent)
        self.pos += 1
        return _value(content, lineno)

    def _child(self, parent_indent: int, inline: str, lineno: int, allow_same_indent_seq=False):
        """Value for a 'key:' / '- ' whose inline text is `inline`: either
        that inline text, or the nested block on the following lines."""
        if inline in ("|", "|-", "|+", ">", ">-", ">+"):
            return self._block_scalar(parent_indent, inline, lineno)
        if inline:
            return _value(inline, lineno)
        if self.pos < len(self.lines):
            _, nxt_indent, nxt = self.lines[self.pos]
            if nxt_indent > parent_indent:
                return self._block(nxt_indent)
            # YAML allows a sequence under a key at the same indent as the key
            if allow_same_indent_seq and nxt_indent == parent_indent and (nxt == "-" or nxt.startswith("- ")):
                return self._sequence(nxt_indent)
        return None

    def _block_scalar(self, parent_indent: int, header: str, lineno: int) -> str:
        """Collect the raw lines of a | or > scalar that starts after line
        `lineno` (1-based), then skip the pre-tokenized lines it covered."""
        body: list[str] = []
        last = lineno
        for idx in range(lineno, len(self.raw)):
            raw = self.raw[idx]
            if raw.strip() and len(raw) - len(raw.lstrip(" ")) <= parent_indent:
                break
            body.append(raw)
            last = idx + 1
        while body and not body[-1].strip():
            body.pop()
            last -= 1
        trailing_blank = sum(1 for idx in range(last, len(self.raw)) if not self.raw[idx].strip()
                             and all(not r.strip() for r in self.raw[last:idx + 1]))
        while self.pos < len(self.lines) and self.lines[self.pos][0] <= last:
            self.pos += 1

        content_lines = [l for l in body if l.strip()]
        block_indent = min((len(l) - len(l.lstrip(" ")) for l in content_lines), default=0)
        lines = [l[block_indent:] if l.strip() else "" for l in body]

        if header[0] == "|":
            text = "\n".join(lines)
        else:
            # Folding: single newlines become spaces, blank lines become
            # newlines, and more-indented lines keep their line breaks.
            parts: list[str] = []
            for i, l in enumerate(lines):
                if i == 0:
                    parts.append(l)
                elif l == "" or lines[i - 1] == "":
                    parts.append("\n" + l if l else "")
                elif l.startswith(" ") or lines[i - 1].startswith(" "):
                    parts.append("\n" + l)
                else:
                    parts.append(" " + l)
            text = "".join(parts)

        chomp = header[1:]
        if not text:
            return ""
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" * (1 + trailing_blank)
        return text + "\n"

    def _mapping(self, indent: int):
        result = {}
        while self.pos < len(self.lines):
            lineno, cur_indent, content = self.lines[self.pos]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise YamlError(f"line {lineno}: unexpected indentation")
            split = _split_key(content, lineno)
            if split is None:
                raise YamlError(f"line {lineno}: expected 'key: value', got {content!r}")
            key, rest = split
            self.pos += 1
            result[key] = self._child(indent, rest, lineno, allow_same_indent_seq=True)
        return result

    def _sequence(self, indent: int):
        result = []
        while self.pos < len(self.lines):
            lineno, cur_indent, content = self.lines[self.pos]
            if cur_indent != indent or not (content == "-" or content.startswith("- ")):
                if cur_indent > indent:
                    raise YamlError(f"line {lineno}: unexpected indentation")
                break
            rest = content[1:].strip()
            if rest == "-" or rest.startswith("- "):
                # "- - x": a nested sequence starting on the same line
                item_indent = indent + (len(content) - len(rest))
                self.lines[self.pos] = (lineno, item_indent, rest)
                result.append(self._sequence(item_indent))
            elif rest and not rest.startswith(("{", "[", "'", '"')) and _split_key(rest, lineno) is not None:
                # "- key: value" starts an inline mapping; re-enter it as a
                # mapping indented to where the key text begins.
                item_indent = indent + (len(content) - len(rest))
                self.lines[self.pos] = (lineno, item_indent, rest)
                result.append(self._mapping(item_indent))
            else:
                self.pos += 1
                result.append(self._child(indent, rest, lineno))
        return result


def safe_load(stream):
    """Parse a YAML document from a string or a file-like object."""
    text = stream.read() if hasattr(stream, "read") else stream
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    if text.startswith("﻿"):
        text = text[1:]
    return _Parser(text).parse()
