"""
routes/system.py — update check, URL/folder helpers, report issue, plot save, SCPI ref.
"""
import json as _json
import os
import subprocess
import sys
import urllib.request as _urlreq
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

import core.shared as _sh

bp = Blueprint("system", __name__)

_ROOT = Path(__file__).parent.parent


# ── Update check ──────────────────────────────────────────────────────────────

@bp.route("/api/check-update", methods=["GET"])
def api_check_update():
    try:
        current = subprocess.check_output(
            ["git", "describe", "--tags", "--always"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL, text=True,
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
        save_dir  = Path(os.path.expanduser(out_path)).resolve() if out_path else _ROOT / "results"
        save_dir.mkdir(parents=True, exist_ok=True)
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
        save_dir = Path(os.path.expanduser(out_path)).resolve() if out_path else _ROOT / "results"
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / filename
        path.write_text(_json.dumps(data, indent=2), encoding="utf-8")
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
