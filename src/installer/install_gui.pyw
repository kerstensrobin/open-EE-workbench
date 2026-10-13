#!/usr/bin/env python3
"""
install_gui.pyw — installer window, a GUI wrapper around install.py.

install-windows.bat in the project root finds (or installs) Python and opens
this window with pythonw.exe, so students never see a terminal or have to
pick between installer files. It also still works double-clicked directly.

This is just a thin driver: it shells out to install.py with its prompts
pre-answered via CLI flags (--zeroconf, --usb-driver, --desktop-entry) and
streams its output into a log box instead of a terminal.
"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent   # src/installer/
ROOT = HERE.parents[1]                   # project root


def _console_python() -> str:
    """The console interpreter next to this one (python.exe, not pythonw.exe).

    Double-clicking a .pyw runs it under pythonw.exe, which has no console.
    Running install.py under it as well means every console program it starts
    (pip, the venv's python.exe, powershell) has no console to inherit, so
    Windows opens a fresh cmd window for each one. Under python.exe started
    with CREATE_NO_WINDOW they all share one hidden console instead.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return sys.executable


def _launcher() -> Path:
    """The start file install.py writes into the project root."""
    return ROOT / ("open-eew.bat" if sys.platform == "win32" else "open-eew")


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
        # Shown once an install succeeds — install.py has written the launcher.
        self.start_btn = ttk.Button(btn_row, text="Start open-EE-workbench",
                                    command=self.start_app)

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

        # -u: unbuffered, so each line reaches the log as it's printed rather
        # than in one block at exit (the admin prompt used to appear before
        # the text announcing it).
        args = [_console_python(), "-u", str(HERE / "install.py"),
                "--zeroconf", "y" if self.zeroconf_var.get() else "n"]
        if sys.platform == "win32":
            args += ["--usb-driver", "y" if self.usb_driver_var.get() else "n"]
        elif sys.platform == "linux":
            args += ["--desktop-entry", "y" if self.desktop_var.get() else "n"]

        def _run():
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            # install.py and the pip/python processes it starts all inherit
            # this env: unbuffered UTF-8 output, decoded as UTF-8 here. A pipe
            # otherwise defaults to the ANSI codepage (cp1252) on one end and
            # UTF-8 on the other, which turns "—" into "â€”".
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            try:
                proc = subprocess.Popen(
                    args, cwd=str(ROOT),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    bufsize=1, creationflags=creationflags, env=env,
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
                        self._append(f"\n✓ Install finished. From now on, start the app with "
                                     f"{_launcher().name} in {ROOT}\n")
                        self.start_btn.pack(side="left", padx=(8, 0))
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

    def start_app(self):
        if sys.platform == "win32":
            # The .bat hands off to pythonw.exe via `start` and exits, so the
            # app outlives this window; CREATE_NO_WINDOW hides the brief cmd.
            subprocess.Popen([str(_launcher())], cwd=str(ROOT),
                             creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            subprocess.Popen([str(_launcher())], cwd=str(ROOT), start_new_session=True)
        self.root.destroy()


def main():
    root = tk.Tk()
    InstallerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
