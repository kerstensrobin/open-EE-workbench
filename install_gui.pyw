#!/usr/bin/env python3
"""
install_gui.pyw — double-clickable GUI wrapper around install.py.

Windows users without `python`/`py` on PATH can't run `python install.py`
from a terminal — but double-clicking a .pyw file in Explorer goes through
the file association the python.org installer registers regardless of the
"Add to PATH" checkbox, so this works even then. It also dodges IDEs that
hijack the plain .py extension as their own "open in editor" default
(PyCharm, VS Code, …) — those normally leave .pyw alone.

This is just a thin driver: it shells out to install.py with its prompts
pre-answered via CLI flags (--zeroconf, --usb-driver, --desktop-entry) and
streams its output into a log box instead of a terminal.
"""
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

ROOT = Path(__file__).parent.resolve()


class InstallerGUI:
    def __init__(self, root):
        self.root = root
        root.title("open-EE-workbench installer")
        root.geometry("640x480")
        root.minsize(520, 380)

        try:
            icon_path = ROOT / "src" / "ui" / "gui_assets" / "icon.png"
            self._icon_img = tk.PhotoImage(file=str(icon_path))
            root.iconphoto(True, self._icon_img)
        except Exception:
            pass

        pad = {"padx": 14, "pady": 6}

        ttk.Label(root, text="open-EE-workbench installer", font=("", 14, "bold")) \
            .pack(anchor="w", **pad)
        ttk.Label(root, text=f"Project root: {ROOT}", foreground="#777") \
            .pack(anchor="w", padx=14)

        opts = ttk.LabelFrame(root, text="Options")
        opts.pack(fill="x", padx=14, pady=10)

        self.zeroconf_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opts, text="Enable mDNS / LAN instrument discovery (zeroconf)",
            variable=self.zeroconf_var,
        ).pack(anchor="w", padx=10, pady=4)

        self.usb_driver_var = tk.BooleanVar(value=True)
        self.desktop_var = tk.BooleanVar(value=False)
        if sys.platform == "win32":
            ttk.Checkbutton(
                opts, text="Install the USB instrument driver (WinUSB) — one admin prompt",
                variable=self.usb_driver_var,
            ).pack(anchor="w", padx=10, pady=4)
        elif sys.platform == "linux":
            ttk.Checkbutton(
                opts, text="Add open-EE-workbench to the application menu",
                variable=self.desktop_var,
            ).pack(anchor="w", padx=10, pady=4)

        btn_row = ttk.Frame(root)
        btn_row.pack(fill="x", padx=14, pady=(0, 6))
        self.install_btn = ttk.Button(btn_row, text="Install", command=self.start_install)
        self.install_btn.pack(side="left")
        ttk.Button(btn_row, text="Close", command=root.destroy).pack(side="right")

        self.progress = ttk.Progressbar(root, mode="indeterminate")
        self.progress.pack(fill="x", padx=14, pady=(0, 8))

        log_frame = ttk.Frame(root)
        log_frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        mono = "Consolas" if sys.platform == "win32" else "Menlo"
        self.log = tk.Text(
            log_frame, wrap="word", state="disabled",
            background="#111", foreground="#ddd", insertbackground="#ddd",
            font=(mono, 10),
        )
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._queue = queue.Queue()

    def _append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def start_install(self):
        self.install_btn.configure(state="disabled")
        self.progress.start(12)
        self._append("Starting install…\n\n")

        args = [sys.executable, str(ROOT / "install.py"),
                "--zeroconf", "y" if self.zeroconf_var.get() else "n"]
        if sys.platform == "win32":
            args += ["--usb-driver", "y" if self.usb_driver_var.get() else "n"]
        elif sys.platform == "linux":
            args += ["--desktop-entry", "y" if self.desktop_var.get() else "n"]

        def _run():
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            try:
                proc = subprocess.Popen(
                    args, cwd=str(ROOT),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, creationflags=creationflags,
                )
            except Exception as exc:
                self._queue.put(("line", f"Could not start install.py: {exc}\n"))
                self._queue.put(("done", 1))
                return
            for line in proc.stdout:
                self._queue.put(("line", line))
            proc.wait()
            self._queue.put(("done", proc.returncode))

        threading.Thread(target=_run, daemon=True).start()
        self.root.after(80, self._poll_queue)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "line":
                    self._append(payload)
                elif kind == "done":
                    self.progress.stop()
                    self.install_btn.configure(state="normal")
                    if payload == 0:
                        self._append("\n✓ Install finished. Close this window, then run open-eew.bat.\n")
                    else:
                        self._append(f"\n✗ Install failed (exit code {payload}). See the log above.\n")
                        messagebox.showerror(
                            "Install failed",
                            "The installer hit an error — scroll up in the log for details.",
                        )
                    return
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)


def main():
    root = tk.Tk()
    InstallerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
