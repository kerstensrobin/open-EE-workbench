"""
core/winusb.py — Windows USB driver setup for USBTMC instruments.

Windows has no built-in USBTMC driver, so without a vendor VISA, USB
instruments are driverless and pyvisa-py cannot open them. install_driver()
binds the inbox WinUSB driver to every USBTMC device (see usbtmc_winusb.ps1
for how), after a single UAC prompt.

Importable both as `core.winusb` (install.py) and as `winusb` (nachoVisa.py).
"""
import base64
import os
import shutil
import subprocess
import sys
import tempfile

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usbtmc_winusb.ps1")


def add_libusb_to_path():
    """Make libusb-1.0.dll findable for pyvisa-py. Call before pyvisa is imported.

    pyvisa-py locates libusb through ctypes.util.find_library(), which on
    Windows only searches PATH — and it probes exactly once, when pyvisa_py.usb
    is first imported; a miss disables USB for the rest of the process.
    install.py stages the DLL next to the venv's python.exe, and the `libusb`
    package ships its own copy; put both folders on PATH.
    """
    if sys.platform != "win32":
        return
    dirs = [os.path.dirname(sys.executable)]
    try:
        import importlib.util
        spec = importlib.util.find_spec("libusb")
        if spec and spec.origin:
            arch = "x86_64" if sys.maxsize > 2**32 else "x86"
            dirs.append(os.path.join(os.path.dirname(spec.origin), "_platform", "windows", arch))
    except Exception:
        pass
    os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])


def _powershell(command: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=timeout,
    )


def vendor_visa_installed() -> bool:
    """True if a system VISA (Keysight IO Libraries, NI-VISA, R&S VISA …) is present.

    A vendor VISA brings its own USBTMC driver and PyVISA prefers it, so the
    WinUSB driver isn't needed there.
    """
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    return any(os.path.exists(os.path.join(system32, dll)) for dll in ("visa64.dll", "visa32.dll"))


def driverless_usbtmc() -> list:
    """Return names of connected USBTMC devices with no driver bound.

    Usually Device Manager code 28, but a device whose driver was just removed
    sits at code 0 with no service until it is replugged — so test the service.
    """
    if sys.platform != "win32":
        return []
    script = (
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { -not $_.Service -and $_.CompatibleID -like 'USB\\Class_FE&SubClass_03*' } | "
        "ForEach-Object { \"$($_.Name)  [$($_.PNPDeviceID)]\" }"
    )
    try:
        out = _powershell(script, timeout=15).stdout
    except Exception:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _run_elevated(action: str) -> tuple:
    """Run usbtmc_winusb.ps1 elevated (one UAC prompt). Returns (ok, log_text)."""
    # Not mkdtemp(): on Python 3.12+ Windows it applies an owner-only ACL, and
    # files the elevated process writes there become unreadable to us.
    work_dir = os.path.join(tempfile.gettempdir(), f"open-eew-usb-{os.getpid()}")
    os.makedirs(work_dir, exist_ok=True)
    try:
        with open(_SCRIPT, encoding="utf-8-sig") as f:
            body = f.read()
        quoted_dir = work_dir.replace("'", "''")
        script = f"$Action = '{action}'\n$WorkDir = '{quoted_dir}'\n{body}"
        # -EncodedCommand is not subject to execution policy, which school PCs
        # often lock down for .ps1 files.
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        launcher = (
            "try { $p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
            f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{encoded}'; "
            "exit $p.ExitCode } catch { Write-Output $_.Exception.Message; exit 2 }"
        )
        result = _powershell(launcher, timeout=300)
        log_path = os.path.join(work_dir, "driver.log")
        log = ""
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8-sig", errors="replace") as f:
                log = "\n".join(line for line in f.read().splitlines() if line.strip())
        if result.returncode == 2:
            log = result.stdout.strip() or "Elevation was cancelled."
        return result.returncode == 0, log
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _run_and_report(action: str, start_msg: str, ok_msg: str, fail_msg: str) -> bool:
    if sys.platform != "win32":
        print("The USB instrument driver is only needed on Windows.")
        return False
    print(start_msg)
    ok, log = _run_elevated(action)
    if log:
        print("  " + log.replace("\n", "\n  "))
    print(ok_msg if ok else fail_msg)
    return ok


def install_driver() -> bool:
    """Install the WinUSB driver package for USBTMC instruments. Returns True on success."""
    return _run_and_report(
        "install",
        "Installing the USB instrument driver (WinUSB) — please accept the Windows admin prompt…",
        "USB instrument driver installed. Unplug and replug instruments if they still aren't found.",
        "USB instrument driver installation failed.",
    )


def uninstall_driver() -> bool:
    """Remove the WinUSB driver package and its certificate. Returns True on success."""
    return _run_and_report(
        "uninstall",
        "Removing the USB instrument driver — please accept the Windows admin prompt…",
        "USB instrument driver removed.",
        "USB instrument driver removal failed.",
    )
