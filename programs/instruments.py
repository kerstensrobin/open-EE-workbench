#!/usr/bin/env python3
"""Shared instrument database for nacho.works VISA scripts.

Loads instruments.json from the same directory and provides:
  classify(idn)              → resolved family dict or None
  resolve_command(cmd, **kw) → list of (action, string) steps
  get_command(family, op, **kw)

JSON inheritance model (v2.0):
  Base families have a "commands" dict.
  Derived families declare "inherits" (parent id) + "overrides":
    - A key overriding an existing parent command replaces it.
    - A key set to null removes that command (unsupported on this device).
    - A key not present in the parent is added (vendor-specific extension).
  classify() always returns a fully resolved family (inheritance applied).

Command spec format (from instruments.json):
  "cmd string"           → single write
  ["a", "b", {...}]      → sequential steps
  {"write": "...", "query": "..."}   → settable+readable property
  {"query": "..."}       → read-only query
  {"raw_query": "..."}   → binary read (use inst.read_raw())
"""

import json
import os

_DB = None
_DB_PATH = os.path.join(os.path.dirname(__file__), "instruments.json")


def _load() -> dict:
    global _DB
    if _DB is None:
        with open(_DB_PATH) as f:
            _DB = json.load(f)
    return _DB


def _family_index() -> dict[str, dict]:
    return {f["id"]: f for f in _load()["families"]}


def _resolve_family(family: dict) -> dict:
    """Return a copy of family with commands fully merged from its inheritance chain."""
    if "inherits" not in family:
        return family

    index = _family_index()
    parent_id = family["inherits"]
    if parent_id not in index:
        raise KeyError(f"Parent family {parent_id!r} not found in instruments.json")

    parent = _resolve_family(index[parent_id])
    commands = dict(parent.get("commands", {}))

    for key, val in family.get("overrides", {}).items():
        if val is None:
            commands.pop(key, None)
        else:
            commands[key] = val

    result = {k: v for k, v in family.items() if k not in ("inherits", "overrides")}
    result["commands"] = commands
    return result


def classify(idn: str) -> dict | None:
    """Return the resolved family dict for this IDN string, or None if unknown."""
    u = idn.upper()
    for family in _load()["families"]:
        if any(p.upper() in u for p in family["patterns"]):
            return _resolve_family(family)
    return None


def resolve_command(cmd, **kwargs) -> list[tuple[str, str]]:
    """Resolve a command spec to a list of (action, scpi_string) tuples.

    action is one of: 'write', 'query', 'raw_query'
    """
    if isinstance(cmd, str):
        return [("write", cmd.format(**kwargs))]
    if isinstance(cmd, dict):
        steps = []
        for action in ("write", "query", "raw_query", "note"):
            if action in cmd:
                steps.append((action, cmd[action].format(**kwargs)))
        return steps
    if isinstance(cmd, list):
        steps = []
        for item in cmd:
            steps.extend(resolve_command(item, **kwargs))
        return steps
    return []


def get_command(family: dict, operation: str, **kwargs) -> list[tuple[str, str]]:
    """Return resolved (action, scpi_string) steps for an operation.

    Raises KeyError if the operation is not defined for this family.
    """
    cmd = family.get("commands", {}).get(operation)
    if cmd is None:
        raise KeyError(
            f"Operation {operation!r} not defined for family {family['id']!r}"
        )
    return resolve_command(cmd, **kwargs)
