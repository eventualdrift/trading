"""`tradebot config-set`: change settings in config.yaml without hand-editing it.

Edits only the lines for the given keys (comments and everything else stay as they are),
checks the result means exactly what was asked, validates it like `tradebot run` would,
keeps a timestamped backup, then replaces the file atomically. If the file's layout can't be
edited line by line (e.g. a section written as ``costs: {fee_rate: 0.001}``), it is rewritten
from the parsed settings instead - same values, but comments are lost (the backup keeps them).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import yaml

from .config import _merge, load_config


def parse_assignments(items: list[str]) -> dict[str, Any]:
    """["costs.fee_rate=0.00075", "core.fraction=0.65"] -> {"costs.fee_rate": 0.00075, ...}"""
    out = {}
    for item in items:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key or not re.fullmatch(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_/]+)*", key):
            raise ValueError(f"expected key=value (e.g. costs.fee_rate=0.00075), got {item!r}")
        out[key] = yaml.safe_load(raw) if raw.strip() else None
    return out


def _nested(updates: dict[str, Any]) -> dict:
    out: dict = {}
    for key, value in updates.items():
        node = out
        *parents, last = key.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[last] = value
    return out


def _scalar(value: Any) -> str:
    text = yaml.safe_dump({"k": value}, default_flow_style=True, width=10_000).strip()
    return text[len("{k: "):-1]


_SKIP = re.compile(r"^\s*(#.*)?$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _key_line(line: str, indent: int, key: str) -> tuple[str, str] | None:
    """If ``line`` is ``key:`` at this indentation -> (inline value, trailing comment incl. spacing)."""
    m = re.match(rf"^ {{{indent}}}{re.escape(key)}:(.*)$", line.rstrip("\n"))
    if not m or (m.group(1) and not m.group(1)[0].isspace()):
        return None
    rest = m.group(1)
    if not rest.strip() or rest.lstrip().startswith("#"):
        return "", rest.rstrip()  # a section header (maybe with a comment)
    v = re.match(r"^\s*(.*?)(\s+#.*)?\s*$", rest)
    return v.group(1), v.group(2) or ""


def _set_line(lines: list[str], path: list[str], value: Any) -> None:
    """Set one dotted key in block-style YAML lines (in place). Raises ValueError if it can't."""
    start, end, indent = 0, len(lines), 0  # the block being searched and its key indentation
    for depth, key in enumerate(path):
        last = depth == len(path) - 1
        found = None
        for i in range(start, end):
            if _SKIP.match(lines[i]) or _indent(lines[i]) != indent:
                continue
            m = _key_line(lines[i], indent, key)
            if m is not None:
                found = (i, m)
                break
        if found is None:  # add the missing key (and any missing sections below it)
            text = "".join(f"{' ' * (indent + 2 * k)}{p}:\n" for k, p in enumerate(path[depth:-1]))
            text += f"{' ' * (indent + 2 * (len(path) - 1 - depth))}{path[-1]}: {_scalar(value)}\n"
            at = end
            while at > start and _SKIP.match(lines[at - 1]):  # before trailing blank/comment lines
                at -= 1
            if end == len(lines) and depth == 0:
                at = len(lines)
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
            lines[at:at] = [text]
            return
        i, (inline, comment) = found
        if last:
            if lines[i + 1:end] and _child_block(lines, i, end):
                raise ValueError(f"{'.'.join(path)} is a section, not a single value")
            lines[i] = f"{' ' * indent}{key}: {_scalar(value)}{comment}\n"
            return
        if inline:  # inline value such as {a: 1}: not editable line by line
            raise ValueError(f"{'.'.join(path[:depth + 1])} is written inline")
        block_end = i + 1
        while block_end < end and (_SKIP.match(lines[block_end]) or _indent(lines[block_end]) > indent):
            block_end += 1
        children = [ln for ln in lines[i + 1:block_end] if not _SKIP.match(ln)]
        child_indent = _indent(children[0]) if children else indent + 2
        start, end, indent = i + 1, block_end, child_indent


def _child_block(lines: list[str], i: int, end: int) -> bool:
    base = _indent(lines[i])
    for ln in lines[i + 1:end]:
        if _SKIP.match(ln):
            continue
        return _indent(ln) > base
    return False


def edit_text(text: str, updates: dict[str, Any]) -> tuple[str, bool]:
    """Apply updates -> (new text, comments_kept). The result always parses to exactly the
    old settings merged with the updates."""
    old = yaml.safe_load(text) or {} if text.strip() else {}
    if not isinstance(old, dict):
        raise ValueError("the config file must be a mapping of settings")
    want = _merge(old, _nested(updates))
    lines = text.splitlines(keepends=True)
    try:
        for key, value in updates.items():
            _set_line(lines, key.split("."), value)
        new = "".join(lines)
        if (yaml.safe_load(new) or {}) == want:
            return new, True
    except (ValueError, yaml.YAMLError):
        pass
    return yaml.safe_dump(want, sort_keys=False, default_flow_style=False), False


def _lookup(cfg, key: str):
    node = cfg
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else getattr(node, part, None)
    return node


def config_set(path: str | Path, assignments: list[str]) -> list[str]:
    """Change settings in ``path``; returns a report. Raises ValueError (file untouched) if the
    result would not be a valid config."""
    path = Path(path)
    updates = parse_assignments(assignments)
    text = path.read_text() if path.exists() else ""
    before = load_config(path, env_file=None) if path.exists() else None
    new_text, kept = edit_text(text, updates)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(new_text)
    try:
        after = load_config(tmp, env_file=None)  # same checks as `tradebot run`
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    report = []
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(text)
        report.append(f"backup: {backup}")
    os.replace(tmp, path)
    for key in updates:
        old = _lookup(before, key) if before is not None else None
        report.append(f"{key}: {old!r} -> {_lookup(after, key)!r}")
    if not kept:
        report.append("note: the file was rewritten from its settings, so its comments were dropped "
                      "(they are still in the backup)")
    return report
