#!/usr/bin/env python3
"""Workbench utilities shared across nacho.works scripts.

Typical usage in a test script:
    import pyvisa
    from workbench import load_workbench, open_by_role

    wb    = load_workbench()
    rm    = pyvisa.ResourceManager("@py")
    psu   = open_by_role(rm, wb, "psu")
    dmm   = open_by_role(rm, wb, "dmm")

    psu.dispatch("set_voltage", ch=1, value="5.0")
    psu.dispatch("output_on", ch=1)
    reading = dmm.dispatch("measure_vdc")
"""

import json
import os
import re

from eewBackbone import get_command, get_family_by_id

WORKBENCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workbenches")


class Instrument:
    """A PyVISA resource bundled with its eewBackbone family.

    Attribute access is proxied to the underlying PyVISA resource, so
    .write(), .query(), .read_raw(), .timeout etc. all work as normal.
    Use .dispatch() to send eewBackbone operations instead of building
    the get_command / loop manually.
    """

    def __init__(self, resource, family):
        object.__setattr__(self, "_resource", resource)
        object.__setattr__(self, "family", family)

    def dispatch(self, operation: str, **kwargs):
        """Run one eewBackbone operation; return the last query response or None."""
        result = None
        for action, scpi in get_command(self.family, operation, **kwargs):
            if action == "write":
                self._resource.write(scpi)
            elif action == "query":
                result = self._resource.query(scpi).strip()
            elif action == "raw_query":
                self._resource.write(scpi)
                result = self._resource.read_raw()
        return result

    def __getattr__(self, name):
        return getattr(self._resource, name)

    def __setattr__(self, name, value):
        if name in ("_resource", "family"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._resource, name, value)


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


def open_by_role(rm, wb: dict, role: str) -> Instrument:
    """Open the instrument with the given role and return it as an Instrument wrapper."""
    entry = by_role(wb, role)
    resource = rm.open_resource(entry["resource"])
    resource.timeout = 10000
    family = get_family_by_id(entry.get("family_id") or "")
    return Instrument(resource, family)


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
