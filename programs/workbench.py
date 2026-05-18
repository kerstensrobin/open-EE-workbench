#!/usr/bin/env python3
"""Workbench utilities shared across nacho.works scripts.

Typical usage in a test script:
    import pyvisa
    from workbench import load_workbench, open_by_role

    wb = load_workbench()               # loads active workbench
    rm = pyvisa.ResourceManager("@py")
    scope = open_by_role(rm, wb, "scope")
    gen   = open_by_role(rm, wb, "generator")
"""

import json
import os
import re

WORKBENCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workbenches")


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", name.strip()).strip("_") or "workbench"


def load_workbench(name: str | None = None) -> dict:
    """Load a workbench by name, or the active workbench if name is None."""
    if name is None:
        path = os.path.join(WORKBENCH_DIR, "active.json")
        if not os.path.exists(path):
            raise FileNotFoundError(
                "No active workbench set. Run: python3 nachoVisa.py"
            )
    else:
        path = os.path.join(WORKBENCH_DIR, f"{_safe_name(name)}.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"Workbench {name!r} not found at {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def by_role(wb: dict, role: str) -> dict:
    """Return the instrument entry with the given role, or raise RuntimeError."""
    matches = [i for i in wb["instruments"] if i.get("role") == role]
    if not matches:
        roles = [i.get("role") for i in wb["instruments"]]
        raise RuntimeError(
            f"No {role!r} in workbench {wb['name']!r}. Available roles: {roles}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple {role!r} instruments in workbench {wb['name']!r}. "
            "Edit the workbench JSON to assign unique roles."
        )
    return matches[0]


def open_by_role(rm, wb: dict, role: str):
    """Open and return a pyvisa resource for the instrument with the given role."""
    instrument = by_role(wb, role)
    res = rm.open_resource(instrument["resource"])
    res.timeout = 10000
    return res


def set_active(name: str) -> str:
    """Point workbenches/active.json at the named workbench. Returns the link path."""
    target = f"{_safe_name(name)}.json"
    if not os.path.exists(os.path.join(WORKBENCH_DIR, target)):
        raise FileNotFoundError(
            f"Workbench {name!r} not found. Save it first with nachoVisa.py."
        )
    link = os.path.join(WORKBENCH_DIR, "active.json")
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(target, link)
    return link


def active_name() -> str | None:
    """Return the name of the active workbench, or None if not set."""
    link = os.path.join(WORKBENCH_DIR, "active.json")
    if not os.path.lexists(link):
        return None
    try:
        target = os.readlink(link)
        return target.removesuffix(".json")
    except OSError:
        return None
