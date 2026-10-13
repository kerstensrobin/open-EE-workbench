"""
routes/system.py — update check, URL/folder helpers, report issue, plot save, SCPI ref.
"""
import io
import json as _json
import math as _math
import os
import subprocess
import sys
import time as _time
import urllib.request as _urlreq
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

import core.shared as _sh
from core.paths import today_output_dir

bp = Blueprint("system", __name__)


# ── Update check ──────────────────────────────────────────────────────────────

@bp.route("/api/check-update", methods=["GET"])
def api_check_update():
    try:
        current = subprocess.check_output(
            ["git", "describe", "--tags", "--always"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).strip()
    except Exception:
        current = "unknown"
    try:
        req = _urlreq.Request(
            "https://api.github.com/repos/kerstensrobin/open-EE-workbench/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "open-EE-workbench"},
        )
        with _urlreq.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
        latest      = data.get("tag_name", "unknown")
        release_url = data.get("html_url",
                                "https://github.com/kerstensrobin/open-EE-workbench/releases")
        up_to_date  = (current == latest) or current.startswith(latest.lstrip("v"))
        return jsonify({"current": current, "latest": latest,
                        "up_to_date": up_to_date, "release_url": release_url})
    except Exception as exc:
        return jsonify({"current": current, "error": str(exc)})


# ── Open URL / folder ─────────────────────────────────────────────────────────

@bp.route("/api/open-url", methods=["POST"])
def api_open_url():
    """Open a URL in the system default browser."""
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400
    try:
        import webbrowser
        webbrowser.open(url)
        return jsonify({"status": "ok"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    """Open a folder in the system file manager (Linux: xdg-open)."""
    path = (request.json or {}).get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    p = Path(path)
    if not p.exists():
        return jsonify({"error": f"Path does not exist: {path}"}), 404
    try:
        subprocess.Popen(["xdg-open", str(p)], close_fds=True)
        return jsonify({"status": "ok"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Report issue ──────────────────────────────────────────────────────────────

@bp.route("/api/report-issue", methods=["POST"])
def api_report_issue():
    """Build a pre-filled GitHub issue URL from current session state and open it."""
    import platform
    import urllib.parse

    instruments = []
    wb = _sh._state.get("workbench")
    if wb:
        for instr in wb.get("instruments", []):
            instruments.append({
                "manufacturer": instr.get("manufacturer", ""),
                "model":        instr.get("model", ""),
                "serial":       instr.get("serial", ""),
                "firmware":     instr.get("firmware", ""),
                "connection":   instr.get("connection", ""),
                "resource":     instr.get("resource", ""),
                "family_id":    instr.get("family_id", "unknown"),
                "type":         instr.get("type", ""),
            })

    try:
        pyvisa_ver = _sh.pyvisa.__version__ if _sh.PYVISA_OK else "not installed"
    except Exception:
        pyvisa_ver = "unknown"

    py_ver    = sys.version.split()[0]
    os_info   = platform.platform()
    wb_name   = _sh._state.get("wb_name") or "none"
    connected = _sh._state.get("connected", False)

    if instruments:
        rows = ["| # | Manufacturer | Model | Serial | Firmware | Connection | Family ID |",
                "|---|---|---|---|---|---|---|"]
        for i, ins in enumerate(instruments, 1):
            rows.append(
                f"| {i} | {ins['manufacturer']} | {ins['model']} | {ins['serial']} "
                f"| {ins['firmware']} | {ins['connection']} | {ins['family_id']} |"
            )
        instr_section = "\n".join(rows)
    else:
        instr_section = "_No instruments connected at time of report._"

    # SCPI history (core/shared.py: _scpi_log) is kept regardless of the UI's
    # verboseScpi/verbosePoll display filters, so it reflects real traffic
    # even if the console pane was filtering it out when the bug happened.
    scpi_entries = list(_sh._scpi_log)
    if scpi_entries:
        def _fmt_entry(e):
            ts = datetime.fromtimestamp(e["t"]).strftime("%H:%M:%S")
            r  = f"[{e['role']}] " if e.get("role") else ""
            tail = f"  ←  {e['result']}" if e.get("result") is not None else ""
            return f"{ts}  {r}{e['cmd']}{tail}"

        tail_n = 40
        tail_lines = "\n".join(_fmt_entry(e) for e in scpi_entries[-tail_n:])
        shown = min(tail_n, len(scpi_entries))
        log_path = None
        try:
            log_path = today_output_dir() / f"scpi_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            log_path.write_text(
                "\n".join(_fmt_entry(e) for e in scpi_entries), encoding="utf-8"
            )
        except Exception:
            pass
        attach_note = (
            f"\n📎 Full log ({len(scpi_entries)} entries) saved to `{log_path}` "
            "— drag it into this issue if it's relevant.\n" if log_path else ""
        )
        scpi_section = f"""\
<details>
<summary>SCPI log (last {shown} of {len(scpi_entries)})</summary>

```
{tail_lines}
```
</details>
{attach_note}"""
    else:
        scpi_section = "_No SCPI traffic recorded this session._"

    body = f"""\
## Description

<!-- Describe what you were trying to do and what went wrong, or which instrument you'd like supported. -->

## Steps to reproduce (if reporting a bug)

1.
2.
3.

## Expected behaviour

## Actual behaviour

---

## Session snapshot

**Workbench:** `{wb_name}`
**Connected:** {connected}

### Instruments

{instr_section}

### SCPI log

{scpi_section}

### System

| | |
|---|---|
| OS | `{os_info}` |
| Python | `{py_ver}` |
| PyVISA | `{pyvisa_ver}` |
"""

    first_model = instruments[0]["model"] if instruments else "Instrument"
    title = f"[bug] {first_model} — <short description>"
    url = (
        "https://github.com/kerstensrobin/open-EE-workbench/issues/new"
        f"?title={urllib.parse.quote(title)}"
        f"&body={urllib.parse.quote(body)}"
        "&labels=bug"
    )

    try:
        import webbrowser
        webbrowser.open(url)
        return jsonify({"status": "ok"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Folder picker ─────────────────────────────────────────────────────────────

@bp.route("/api/pick-folder", methods=["POST"])
def api_pick_folder():
    """Open a native OS folder-picker dialog and return the chosen path."""
    try:
        import tkinter as _tk
        from tkinter import filedialog as _fd

        initial = (request.json or {}).get("initial", str(Path.home()))
        result  = {"path": None}

        root = _tk.Tk()
        root.withdraw()

        dlg = _tk.Toplevel(root)
        dlg.title("Select output folder")
        dlg.resizable(True, False)
        dlg.attributes("-topmost", True)

        folder_var = _tk.StringVar(value=initial)

        # ── Row 1: folder path + Browse ───────────────────────────────
        r1 = _tk.Frame(dlg, padx=10, pady=8)
        r1.pack(fill="x")
        _tk.Label(r1, text="Folder:", width=12, anchor="w").pack(side="left")
        _tk.Entry(r1, textvariable=folder_var, width=44).pack(
            side="left", fill="x", expand=True, padx=(0, 6))

        def _browse():
            p = _fd.askdirectory(parent=dlg, title="Select folder",
                                  initialdir=folder_var.get() or initial)
            if p:
                folder_var.set(p)

        _tk.Button(r1, text="Browse…", command=_browse).pack(side="left")

        # ── Row 2: new subfolder + Create ─────────────────────────────
        r2 = _tk.Frame(dlg, padx=10, pady=2)
        r2.pack(fill="x")
        _tk.Label(r2, text="New subfolder:", width=12, anchor="w").pack(side="left")
        sub_var   = _tk.StringVar()
        sub_entry = _tk.Entry(r2, textvariable=sub_var, width=44)
        sub_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        def _create():
            name = sub_var.get().strip()
            if not name:
                return
            base     = folder_var.get().strip() or str(Path.home())
            new_path = Path(base) / name
            try:
                new_path.mkdir(parents=True, exist_ok=True)
                folder_var.set(str(new_path))
                sub_var.set("")
            except Exception as exc:
                _tk.messagebox.showerror("Error", str(exc), parent=dlg)

        sub_entry.bind("<Return>", lambda _: _create())
        _tk.Button(r2, text="Create", command=_create).pack(side="left")

        # ── Row 3: OK / Cancel ────────────────────────────────────────
        r3 = _tk.Frame(dlg, padx=10, pady=10)
        r3.pack(fill="x")

        def _ok():
            result["path"] = folder_var.get().strip() or None
            dlg.destroy()

        def _cancel():
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", _cancel)
        _tk.Button(r3, text="Cancel", command=_cancel, width=8).pack(side="right")
        _tk.Button(r3, text="OK",     command=_ok,     width=8).pack(
            side="right", padx=(0, 6))

        dlg.update_idletasks()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        w,  h  = dlg.winfo_reqwidth(),    dlg.winfo_reqheight()
        dlg.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")

        dlg.grab_set()
        root.wait_window(dlg)
        root.destroy()

        return jsonify({"path": result["path"]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Python console ───────────────────────────────────────────────────────────

import ctypes
import threading
import uuid

_py_running = False
_py_thread: threading.Thread | None = None

_pending_confirms: dict[str, threading.Event] = {}
_confirm_answers: dict[str, bool] = {}


def _py_emit(msg, cls="py-out"):
    _sh.sio.emit("python_output", {"msg": msg, "cls": cls})


def _confirm_resource_takeover(resource_name: str) -> bool:
    """Ask the UI whether to disconnect the Workbench tab so a Python-console
    script can open its own raw session to an instrument the Workbench
    already holds. Blocks the console's exec thread until the UI answers (or
    120s pass, treated as "no").

    USB instruments allow only one open session — if the Workbench tab is
    connected and a script calls pyvisa.ResourceManager().open_resource() on
    the same resource in-process, the two sessions fight over the device
    (seen as outright connection failures or SCPI "Query INTERRUPTED" errors
    from traffic neither session knows about). This can't be solved by the
    existing per-resource lock (core/helpers.py: LockedResource) because that
    only serialises I/O *within* the app's own session — a script-opened
    session is invisible to it.
    """
    confirm_id = uuid.uuid4().hex
    event = threading.Event()
    _pending_confirms[confirm_id] = event
    _sh.sio.emit("python_confirm", {
        "id": confirm_id,
        "resource": resource_name,
        "message": (
            f"The Workbench tab is currently using {resource_name}. "
            "Disconnect it and open the instrument from this script instead?"
        ),
    })
    try:
        # Short slices, not one long wait: the console's Stop button works by
        # raising KeyboardInterrupt in this thread, which only lands between
        # Python bytecodes — a single blocking wait would hold it off.
        deadline = _time.monotonic() + 120
        while not event.wait(0.25):
            if _time.monotonic() > deadline:
                return False
        return _confirm_answers.pop(confirm_id, False)
    finally:
        _pending_confirms.pop(confirm_id, None)
        _confirm_answers.pop(confirm_id, None)


@bp.route("/api/python/confirm", methods=["POST"])
def api_python_confirm():
    d = request.json or {}
    confirm_id = d.get("id", "")
    event = _pending_confirms.get(confirm_id)
    if event is None:
        return jsonify({"error": "unknown or expired confirmation"}), 404
    _confirm_answers[confirm_id] = bool(d.get("proceed"))
    event.set()
    return jsonify({"status": "ok"})


def _resource_key(resource_name: str):
    """Canonical form of a VISA resource name for equality checks.

    The same instrument can be spelled many ways: pyvisa-py lists USB IDs in
    decimal ("USB0::10893::36609::…") while scripts and datasheets use hex
    ("USB0::0x2A8D::0x8F01::…"), and the board number, "::0" interface
    number and case are all optional/insensitive.
    """
    try:
        from pyvisa import rname
        parsed = rname.parse_resource_name(resource_name)
    except Exception:
        return resource_name.strip().upper()
    fields = []
    for f in parsed._fields:
        v = str(getattr(parsed, f, "") or "")
        try:
            fields.append(int(v, 0))
        except ValueError:
            fields.append(v.upper())
    return (type(parsed).__name__, tuple(fields))


def _held_by_workbench(resource_name: str) -> str | None:
    """Return the Workbench's own spelling of resource_name if it holds it."""
    key = _resource_key(resource_name)
    for held in list(_sh._state.get("resources", {})):
        if _resource_key(held) == key:
            return held
    return None


_console = threading.local()   # .active is True on the console's exec thread


def _install_takeover_guard():
    """Make open_resource() on an address the Workbench tab already holds
    pause for user confirmation instead of silently fighting over the device.
    See _confirm_resource_takeover().

    Patched on pyvisa's ResourceManager class rather than handed to scripts
    as a wrapped module: scripts start with their own `import pyvisa` (which
    would replace any wrapper in their namespace), and libraries such as
    pymeasure open sessions through pyvisa too. Only calls made on the
    console's exec thread are checked, so the Workbench's own connects are
    unaffected.
    """
    from pyvisa.highlevel import ResourceManager
    real_open = ResourceManager.open_resource
    if getattr(real_open, "_eew_guard", False):
        return

    def open_resource(self, resource_name, *a, **kw):
        if getattr(_console, "active", False) and _held_by_workbench(resource_name):
            if not _confirm_resource_takeover(resource_name):
                raise RuntimeError(
                    f"Cancelled: the Workbench tab is connected to {resource_name!r}. "
                    "Disconnect it first (or accept the takeover prompt) before this "
                    "script can open it."
                )
            from core.routes.connection import _disconnect_all
            # pyvisa hands out one shared ResourceManager per backend, so `self`
            # is usually the Workbench's own: release its instruments but keep
            # the session open, or this very call fails with InvalidSession.
            _disconnect_all(close_rm=False)
        return real_open(self, resource_name, *a, **kw)

    open_resource._eew_guard = True
    ResourceManager.open_resource = open_resource


@bp.route("/api/python/run", methods=["POST"])
def api_python_run():
    global _py_running, _py_thread
    if _py_running:
        return jsonify({"error": "Already running"}), 409

    body = request.json or {}
    code = body.get("code", "").strip()
    path = (body.get("path") or "").strip() or None
    if not code:
        return jsonify({"error": "No code"}), 400

    _py_running = True

    def _run():
        global _py_running, _py_thread

        class _OutStream(io.TextIOBase):
            def __init__(self, cls):
                self._cls = cls
            def write(self, s):
                for line in s.splitlines():
                    if line.rstrip():
                        _py_emit(line.rstrip(), self._cls)
                return len(s)

        def _print(*args, sep=" ", end="\n", **_kw):
            text = sep.join(str(a) for a in args)
            for line in (text + end).splitlines():
                if line:
                    _py_emit(line, "py-out")

        try:
            from core.backbone import get_command
            from core.helpers import _op, _log, _run_steps, _find_instrument
        except Exception:
            get_command = _op = _log = _run_steps = _find_instrument = None

        ns = {
            "__builtins__": __builtins__,
            "print":      _print,
            "time":       _time,
            "json":       _json,
            "math":       _math,
            "Path":       Path,
            "resources":  _sh._state.get("resources", {}),
            "families":   _sh._state.get("families",  {}),
            "wb":         _sh._state.get("workbench"),
            "sio":        _sh.sio,
        }
        if path:
            ns["__file__"] = path
        if _sh.PYVISA_OK:
            _install_takeover_guard()
            ns["pyvisa"] = _sh.pyvisa
        _console.active = True
        try:
            import pymeasure as _pymeasure
            ns["pymeasure"] = _pymeasure
        except ImportError:
            pass
        if get_command:
            ns.update(get_command=get_command, _op=_op, _log=_log,
                      _run_steps=_run_steps, _find_instrument=_find_instrument)

        try:
            import contextlib
            with contextlib.redirect_stdout(_OutStream("py-out")), \
                 contextlib.redirect_stderr(_OutStream("py-err")):
                exec(compile(code, "<console>", "exec"), ns)  # noqa: S102
            _py_emit("✓ Done", "py-ok")
            _sh.sio.emit("python_done", {"error": None})
        except KeyboardInterrupt:
            _py_emit("■ Stopped", "py-err")
            _sh.sio.emit("python_done", {"error": "stopped"})
        except Exception as exc:
            import traceback
            exc_type, exc_val, exc_tb = sys.exc_info()
            te = traceback.TracebackException(exc_type, exc_val, exc_tb)
            te.stack = traceback.StackSummary.from_list(
                [f for f in te.stack if f.filename == "<console>"]
            )
            for line in "".join(te.format()).splitlines():
                if line:
                    _py_emit(line, "py-err")
            _sh.sio.emit("python_done", {"error": str(exc)})
        finally:
            _console.active = False
            _py_running = False
            _py_thread = None

    _py_thread = threading.Thread(target=_run, daemon=True)
    _py_thread.start()
    return jsonify({"status": "running"})


@bp.route("/api/python/stop", methods=["POST"])
def api_python_stop():
    global _py_thread
    t = _py_thread
    if t is None or not t.is_alive():
        return jsonify({"status": "not_running"})
    tid = t.ident
    if tid:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(tid),
            ctypes.py_object(KeyboardInterrupt),
        )
    return jsonify({"status": "stopping"})


@bp.route("/api/save-text", methods=["POST"])
def api_save_text():
    d        = request.json or {}
    filename = d.get("filename", "script.py")
    content  = d.get("content", "")
    out_path = (d.get("output_path") or "").strip()
    try:
        save_dir = today_output_dir(out_path)
        path = save_dir / filename
        path.write_text(content, encoding="utf-8")
        return jsonify({"path": str(path)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Script file explorer ──────────────────────────────────────────────────────

_SCRIPTS_DIR   = Path(__file__).resolve().parents[2] / "scripts"
_FS_SKIP_NAMES = {"__pycache__", ".git", ".venv", "node_modules"}


@bp.route("/api/fs/list", methods=["GET"])
def api_fs_list():
    """List the immediate contents of a directory (defaults to the scripts/ folder)."""
    raw = (request.args.get("path") or "").strip()
    try:
        target = Path(os.path.expanduser(raw)).resolve() if raw else _SCRIPTS_DIR.resolve()
        if not target.is_dir():
            return jsonify({"error": f"Not a directory: {target}"}), 400
        entries = []
        for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name.startswith(".") or p.name in _FS_SKIP_NAMES:
                continue
            entries.append({"name": p.name, "path": str(p), "is_dir": p.is_dir()})
        return jsonify({"path": str(target), "entries": entries})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@bp.route("/api/fs/read", methods=["GET"])
def api_fs_read():
    """Read a text file's contents for the script editor."""
    raw = (request.args.get("path") or "").strip()
    if not raw:
        return jsonify({"error": "path is required"}), 400
    try:
        p = Path(os.path.expanduser(raw)).resolve()
        if not p.is_file():
            return jsonify({"error": f"Not a file: {p}"}), 400
        content = p.read_text(encoding="utf-8", errors="replace")
        return jsonify({"path": str(p), "name": p.name, "content": content})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@bp.route("/api/fs/write", methods=["POST"])
def api_fs_write():
    """Write the script editor's contents back to a known file path."""
    d   = request.json or {}
    raw = (d.get("path") or "").strip()
    if not raw:
        return jsonify({"error": "path is required"}), 400
    try:
        p = Path(os.path.expanduser(raw)).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(d.get("content", ""), encoding="utf-8")
        return jsonify({"path": str(p)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Plot image save ───────────────────────────────────────────────────────────

@bp.route("/api/plot/save-image", methods=["POST"])
def api_plot_save_image():
    """Save a base64-encoded PNG from the plot canvas to the results folder."""
    import base64

    d        = request.json or {}
    filename = d.get("filename", "plot_export")
    data_url = d.get("data", "")
    out_path = (d.get("output_path") or "").strip()
    try:
        header, b64 = data_url.split(",", 1) if "," in data_url else ("", data_url)
        img_bytes = base64.b64decode(b64)
        save_dir  = today_output_dir(out_path)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = save_dir / f"{filename}_{ts}.png"
        path.write_bytes(img_bytes)
        return jsonify({"path": str(path)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Generic JSON save ────────────────────────────────────────────────────────

@bp.route("/api/save-json", methods=["POST"])
def api_save_json():
    d        = request.json or {}
    filename = d.get("filename", "export")
    data     = d.get("data")
    out_path = (d.get("output_path") or "").strip()
    try:
        save_dir = today_output_dir(out_path)
        path = save_dir / filename
        path.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        return jsonify({"path": str(path)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── HDF5 export (measurement data + run metadata) ─────────────────────────────

@bp.route("/api/export-hdf5", methods=["POST"])
def api_export_hdf5():
    """Write automation/sandbox results to an HDF5 file.

    Layout: a `data` dataset (rows x columns, NaN for missing values, with
    a `columns` attribute) and a `metadata` group holding the run's metadata
    as both a JSON blob and flattened top-level attributes.
    """
    try:
        import h5py
        import numpy as np
    except ImportError:
        return jsonify({"error": "h5py is not installed — re-run the installer (install-windows.bat / install-linux-mac.sh) to add it"}), 500

    d        = request.json or {}
    filename = d.get("filename", "export")
    columns  = d.get("columns") or []
    rows     = d.get("rows") or []
    metadata = d.get("metadata") or {}
    out_path = (d.get("output_path") or "").strip()

    if not columns or not rows:
        return jsonify({"error": "No data to export"}), 400

    try:
        save_dir = today_output_dir(out_path)
        path = save_dir / (filename if filename.endswith((".h5", ".hdf5")) else f"{filename}.hdf5")

        data = np.array(
            [[np.nan if v is None else v for v in row] for row in rows],
            dtype=float,
        )

        with h5py.File(path, "w") as hf:
            dset = hf.create_dataset("data", data=data)
            dset.attrs["columns"] = np.array(columns, dtype=h5py.string_dtype())

            meta_grp = hf.create_group("metadata")
            meta_grp.attrs["json"] = _json.dumps(metadata)
            for k, v in metadata.items():
                if v is None or isinstance(v, (str, int, float, bool)):
                    meta_grp.attrs[k] = v if v is not None else ""

        return jsonify({"path": str(path)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── SCPI command reference ────────────────────────────────────────────────────

@bp.route("/api/scpi-commands", methods=["GET"])
def api_scpi_commands():
    """Return the resolved command table for a given family_id."""
    family_id = request.args.get("family_id", "").strip()
    if not family_id:
        return jsonify({"error": "family_id required"}), 400
    try:
        from core.backbone import _family_index, _resolve_family, HELPERS_OK
        if not HELPERS_OK:
            return jsonify({"error": "backbone unavailable"}), 503
        idx = _family_index()
        if family_id not in idx:
            return jsonify({"error": f"Unknown family: {family_id}"}), 404
        fam = _resolve_family(idx[family_id])
        return jsonify({
            "family_id": fam["id"],
            "vendor":    fam.get("vendor", ""),
            "series":    fam.get("series", ""),
            "type":      fam.get("type", ""),
            "commands":  fam.get("commands", {}),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
