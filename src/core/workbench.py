#!/usr/bin/env python3
"""Workbench utilities shared across nacho.works scripts.

Typical usage in a test script:
    from nachoVisa import open_resource_manager
    from workbench import load_workbench, open_by_role

    wb    = load_workbench()
    rm    = open_resource_manager()
    psu   = open_by_role(rm, wb, "psu")
    dmm   = open_by_role(rm, wb, "dmm")

    psu.scpi_dispatch("set_voltage", ch=1, value="5.0")
    psu.scpi_dispatch("output_on", ch=1)
    reading = dmm.scpi_dispatch("measure_vdc")
"""

import json
import os
import re

from eewBackbone import get_command, get_family_by_id

WORKBENCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workbenches")


class Instrument:
    """A connected instrument: PyVISA resource + its SCPI command set, kept together.

    open_by_role() returns one of these. The two things that previously had to be
    tracked separately — the open connection and the family dict that says which
    SCPI strings to send — are now one object.

    Use scpi_dispatch() to send named operations (e.g. "set_voltage", "output_on").
    The operation names and their SCPI expansions live in core/eewBackbone.json;
    scpi_dispatch() looks up the right strings, fills in any placeholders, and
    sends them over the connection.

    All other attribute access (e.g. .write(), .query(), .timeout) is forwarded
    transparently to the underlying PyVISA resource, so raw SCPI access still works
    if you need something not covered by eewBackbone.
    """

    def __init__(self, resource, family):
        # Use object.__setattr__ directly here because our own __setattr__ below
        # would try to forward these to self._resource before it exists.
        object.__setattr__(self, "_resource", resource)
        object.__setattr__(self, "family", family)

    def scpi_dispatch(self, operation: str, **kwargs):
        """Send a named SCPI operation and return the response string, or None.

        operation is a key in eewBackbone.json (e.g. "set_voltage", "measure_vdc").
        Keyword args fill in placeholders: ch=1, value="5.0", freq=1000, etc.

        Some operations are a single write; others are a write followed by a read,
        or a sequence of steps. scpi_dispatch handles all of those uniformly and
        returns whatever the last read produced (or None for write-only operations).
        """
        result = None
        for action, scpi in get_command(self.family, operation, **kwargs):
            if action == "write":
                self._resource.write(scpi)
            elif action == "query":
                # Send the query string and read back the instrument's response.
                result = self._resource.query(scpi).strip()
            elif action == "raw_query":
                # Binary read — used for things like oscilloscope screenshots
                # where the response is raw bytes rather than a text string.
                self._resource.write(scpi)
                result = self._resource.read_raw()
        return result

    def __getattr__(self, name):
        # Forward any attribute not defined on Instrument itself to the PyVISA resource.
        return getattr(self._resource, name)

    def __setattr__(self, name, value):
        # Keep _resource and family on this object; forward everything else
        # (e.g. .timeout, .chunk_size) to the underlying PyVISA resource.
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
    """Open the instrument with the given role and return it as an Instrument.

    Looks up the instrument entry by role in the workbench JSON, opens the
    PyVISA connection, and loads its SCPI command set from eewBackbone using
    the family_id stored in the workbench file. Returns both bundled together
    as an Instrument so scripts don't have to manage them separately.
    """
    entry = by_role(wb, role)
    resource = rm.open_resource(entry["resource"])
    resource.timeout = 10000
    # family_id is stored in the workbench JSON when the bench is scanned,
    # so we can load the right SCPI command set without querying *IDN? again.
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
