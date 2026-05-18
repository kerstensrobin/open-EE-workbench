#!/usr/bin/env python3
# nacho.works — VISA instrument discovery & diagnostics

import argparse
import importlib.util
import ipaddress
import itertools
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Iterable, List, Sequence, Tuple

REQUIRED_PACKAGES = [
    ("pyvisa",    "pyvisa",    "PyVISA — VISA resource manager"),
    ("pyvisa_py", "pyvisa-py", "PyVISA-py — pure-Python VISA backend"),
    ("usb",       "pyusb",     "PyUSB — low-level USB device access"),
    ("zeroconf",  "zeroconf",  "zeroconf — mDNS/LAN instrument discovery"),
]

_debug = False

_SPINNER_FRAMES = ["|", "/", "-", "\\"]

# Logo lines — padded to _LOGO_PAD chars so right-side text lines up cleanly.
_LOGO_LINES = [
    "                ####",
    "              #######",
    "             #########",
    "           ############",
    "          ##############",
    "         ################",
    "       ###################",
    "      #######     ####",
    "    ##########   ######  ##",
    "   ###############   #######",
    "  ###############     #######",
    "##############################",
    "    ##########################",
    "                  #############",
]
_LOGO_PAD    = 36   # width each logo line is padded to before right-side text
_BOX_INNER   = _LOGO_PAD + 42  # inner width of the surrounding box (logo + text + padding)
_TITLE_ROW   = 4    # "nacho.works" goes here
_SUB_ROW     = 5    # subtitle goes here
_SPINNER_ROW = 7    # rotating arrow + status goes here
# Lines to move up from after the full box to reach the spinner row.
# +2 accounts for the top and bottom border lines.
_ROWS_TO_SPINNER = len(_LOGO_LINES) - _SPINNER_ROW + 1  # +1 for bottom border


def _logo_line(idx: int, frame: str = " ", msg: str = "") -> str:
    logo = _LOGO_LINES[idx].ljust(_LOGO_PAD)
    if idx == _TITLE_ROW:
        right = "nacho.works"
    elif idx == _SUB_ROW:
        right = "VISA instrument discovery & diagnostics"
    elif idx == _SPINNER_ROW:
        right = f"{frame}  {msg}" if msg else ""
    else:
        right = ""
    inner = (logo + right).ljust(_BOX_INNER)
    return f"│{inner}│"


def _print_logo(frame: str = " ", msg: str = ""):
    print("┌" + "─" * _BOX_INNER + "┐")
    for i in range(len(_LOGO_LINES)):
        print(_logo_line(i, frame, msg))
    print("└" + "─" * _BOX_INNER + "┘")


class Spinner:
    def __init__(self, message: str = "Scanning"):
        self._message = message
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _write_spinner_row(self, frame: str, msg: str):
        line = _logo_line(_SPINNER_ROW, frame, msg)
        n = _ROWS_TO_SPINNER
        sys.stdout.write(f"\033[{n}A\r{line}\033[{n}B\r")
        sys.stdout.flush()

    def _spin(self):
        for frame in itertools.cycle(_SPINNER_FRAMES):
            if self._stop.is_set():
                break
            with self._lock:
                msg = self._message
            self._write_spinner_row(frame, msg)
            time.sleep(0.15)
        self._write_spinner_row(" ", "")

    def update(self, message: str):
        with self._lock:
            self._message = message

    def __enter__(self):
        _print_logo(frame=_SPINNER_FRAMES[0], msg=self._message)
        if not _debug:
            self._thread.start()
        return self

    def __exit__(self, *args):
        if not _debug:
            self._stop.set()
            self._thread.join()
        print()


def check_dependencies() -> bool:
    missing = [
        (import_name, pip_name, label)
        for import_name, pip_name, label in REQUIRED_PACKAGES
        if importlib.util.find_spec(import_name) is None
    ]
    if not missing:
        return True

    print("Missing packages:")
    for _, pip_name, label in missing:
        print(f"  - {pip_name}  ({label})")
    print()

    try:
        answer = input("Install them now? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if answer != "y":
        return False

    pip_names = [pip_name for _, pip_name, _ in missing]
    print()
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", *pip_names],
        check=False,
        stderr=subprocess.PIPE,
    )
    print()
    if result.returncode != 0:
        stderr_text = result.stderr.decode(errors="replace")
        if "externally-managed-environment" in stderr_text or "externally managed" in stderr_text.lower():
            print("Your system Python is externally managed (PEP 668) and does not allow")
            print("pip to install packages directly. Run the script inside a virtual environment:")
            print()
            print("  python3 -m venv venv")
            print("  source venv/bin/activate   # on Linux/Mac")
            print("  python3 nachoVisa.py")
            print()
        else:
            print("Installation failed. Please install the packages manually and re-run.")
            if stderr_text.strip():
                print(stderr_text.strip())
        return False

    g = globals()
    for import_name, _, _ in missing:
        try:
            g[import_name] = importlib.import_module(import_name)
        except ImportError:
            pass

    return True


try:
    import pyvisa
except ImportError:
    pyvisa = None

try:
    import psutil
except ImportError:
    psutil = None

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

try:
    from instruments import classify as _db_classify
except ImportError:
    _db_classify = None


LAN_PROBE_PORTS = (5025, 4880, 111)

WORKBENCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workbenches")

# instruments.json uses "awg" as the type; map it to the conventional role name
_TYPE_TO_ROLE = {"awg": "generator"}


def status(message: str):
    if _debug:
        print(f"[scan] {message}", flush=True)


def print_dependency_notice(message: str):
    print("Dependency notice:")
    print(f"  - {message}")
    print("  - Install PyVISA with `pip install pyvisa`.")
    print(
        "  - Then either install the pure Python backend with `pip install pyvisa-py`"
    )
    print("    or install a system VISA implementation such as NI-VISA.")
    print()


def connection_type(resource_name: str) -> str:
    r = resource_name.upper()
    if r.startswith("USB"):
        return "USB"
    if r.startswith("TCPIP"):
        return "Ethernet"
    if r.startswith("ASRL"):
        return "Serial"
    return "Other"


def parse_idn(idn: str):
    parts = [p.strip() for p in idn.split(",")]
    while len(parts) < 4:
        parts.append("")
    return parts[0], parts[1], parts[2], parts[3]


def serial_device_from_resource(resource_name: str) -> str | None:
    if not resource_name.upper().startswith("ASRL"):
        return None

    body = resource_name[4:]
    if body.endswith("::INSTR"):
        body = body[:-7]
    return body or None


def serial_port_metadata() -> dict:
    if list_ports is None:
        return {}

    metadata = {}
    try:
        ports = list(list_ports.comports())
    except Exception:
        return metadata

    for port in ports:
        metadata[port.device] = {
            "description": port.description,
            "hwid": port.hwid,
            "vid": port.vid,
            "pid": port.pid,
            "serial_number": port.serial_number,
            "manufacturer": port.manufacturer,
            "product": port.product,
        }

    return metadata


def is_usb_serial_resource(resource_name: str, serial_metadata: dict) -> bool:
    device = serial_device_from_resource(resource_name)
    if not device:
        return False

    metadata = serial_metadata.get(device)
    if metadata and (metadata.get("vid") is not None or metadata.get("pid") is not None):
        return True

    basename = os.path.basename(device).lower()
    return basename.startswith(("ttyusb", "ttyacm", "cu.usb", "tty.usb"))


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Scan VISA instruments reachable via PyVISA/PyVISA-py."
    )
    parser.add_argument(
        "--backend",
        default="@py",
        help=(
            "PyVISA backend to use. Defaults to '@py'. "
            "Use an empty string to let PyVISA choose automatically."
        ),
    )
    parser.add_argument(
        "--usb-only",
        action="store_true",
        help="Only print USB VISA resources and USB diagnostics.",
    )
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="Probe a specific instrument IP address directly. Can be passed multiple times.",
    )
    parser.add_argument(
        "--subnet",
        action="append",
        default=[],
        help=(
            "Scan a specific IPv4 subnet in CIDR notation, for example 192.168.1.0/24. "
            "Can be passed multiple times."
        ),
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=0.35,
        help="TCP connect timeout in seconds used while discovering LAN hosts.",
    )
    parser.add_argument(
        "--max-hosts",
        type=int,
        default=256,
        help="Maximum number of IPs to probe per subnet during LAN discovery.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=64,
        help="Number of concurrent workers to use while probing LAN hosts.",
    )
    parser.add_argument(
        "--all-resources",
        action="store_true",
        help="Include serial and other non-USB/non-TCPIP VISA resources in the output.",
    )
    parser.add_argument(
        "--fix-udev",
        action="store_true",
        help="(Linux only) Write udev rules for detected USBTMC devices and reload udev.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print verbose scan progress instead of the animated spinner.",
    )
    parser.add_argument(
        "--save",
        metavar="NAME",
        help="Save the scanned workbench under NAME without prompting.",
    )
    return parser


def open_resource_manager(backend: str):
    if pyvisa is None:
        raise RuntimeError("PyVISA is not installed.")

    if backend:
        try:
            return pyvisa.ResourceManager(backend)
        except Exception as exc:
            backend_name = backend.strip()
            if backend_name == "@py":
                raise RuntimeError(
                    "PyVISA is installed, but the `pyvisa-py` backend is not available."
                ) from exc
            raise RuntimeError(
                f"Could not open VISA backend {backend!r}. A system VISA implementation such as NI-VISA may be missing."
            ) from exc

    try:
        return pyvisa.ResourceManager()
    except Exception as exc:
        raise RuntimeError(
            "PyVISA is installed, but no usable VISA backend was found. Install `pyvisa-py` or a system VISA implementation such as NI-VISA."
        ) from exc


def list_standard_resources(rm) -> Tuple[List[str], List[str]]:
    errors = []

    try:
        return list(rm.list_resources()), errors
    except Exception as exc:
        errors.append(f"Standard VISA discovery failed: {exc}")
        return [], errors


def list_usb_fallback_resources() -> Tuple[List[str], List[str]]:
    errors = []
    resources = set()

    try:
        from pyvisa_py.usb import USBInstrSession, USBRawSession
    except Exception as exc:
        errors.append(f"USB fallback unavailable: {exc}")
        return [], errors

    for label, session_cls in (
        ("USB INSTR", USBInstrSession),
        ("USB RAW", USBRawSession),
    ):
        try:
            resources.update(session_cls.list_resources())
        except Exception as exc:
            errors.append(f"{label} discovery failed: {exc}")

    return sorted(resources), errors


def discover_resources(rm, spinner: Spinner = None) -> Tuple[List[str], List[str]]:
    resources = set()
    errors = []

    status("Querying standard VISA resources")
    if spinner:
        spinner.update("Querying VISA resources")
    standard_resources, standard_errors = list_standard_resources(rm)
    resources.update(standard_resources)
    errors.extend(standard_errors)

    status("Querying direct USB VISA resources")
    if spinner:
        spinner.update("Querying USB resources")
    usb_resources, usb_errors = list_usb_fallback_resources()
    resources.update(usb_resources)
    errors.extend(usb_errors)

    return sorted(resources), errors


def candidate_lan_resources(host: str) -> Tuple[str, ...]:
    return (
        f"TCPIP0::{host}::inst0::INSTR",
        f"TCPIP0::{host}::hislip0::INSTR",
        f"TCPIP0::{host}::5025::SOCKET",
    )


def iter_network_hosts(network: ipaddress.IPv4Network, max_hosts: int) -> Iterable[str]:
    count = 0
    for host in network.hosts():
        yield str(host)
        count += 1
        if count >= max_hosts:
            break


def get_local_ipv4_addresses() -> List[str]:
    addresses = set()

    if psutil is not None:
        try:
            for interface_addrs in psutil.net_if_addrs().values():
                for addr in interface_addrs:
                    if addr.family != socket.AF_INET:
                        continue
                    try:
                        ip = ipaddress.ip_address(addr.address)
                    except ValueError:
                        continue
                    if not ip.is_loopback:
                        addresses.add(str(ip))
        except Exception:
            pass

    for name in {socket.gethostname(), socket.getfqdn()}:
        if not name:
            continue
        try:
            _, _, host_ips = socket.gethostbyname_ex(name)
        except OSError:
            continue
        for host_ip in host_ips:
            try:
                ip = ipaddress.ip_address(host_ip)
            except ValueError:
                continue
            if isinstance(ip, ipaddress.IPv4Address) and not ip.is_loopback:
                addresses.add(str(ip))

    for target in ("8.8.8.8", "1.1.1.1", "192.0.2.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target, 80))
                host_ip = sock.getsockname()[0]
        except OSError:
            continue

        try:
            ip = ipaddress.ip_address(host_ip)
        except ValueError:
            continue

        if isinstance(ip, ipaddress.IPv4Address) and not ip.is_loopback:
            addresses.add(str(ip))

    return sorted(addresses)


def get_local_ipv4_subnets() -> Tuple[List[ipaddress.IPv4Network], List[str]]:
    notes = []

    networks = []
    seen = set()
    local_ips = get_local_ipv4_addresses()

    if not local_ips:
        return [], ["Could not determine any non-loopback local IPv4 addresses automatically."]

    for host_ip in local_ips:
        try:
            interface = ipaddress.ip_interface(f"{host_ip}/24")
        except ValueError:
            continue

        network = interface.network
        notes.append(
            f"Subnet note: auto-discovery is probing {network} around local address {host_ip}."
        )

        if network not in seen:
            networks.append(network)
            seen.add(network)

    return networks, notes


def port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover_lan_hosts(
    hosts: Sequence[str],
    subnets: Sequence[str],
    timeout: float,
    max_hosts: int,
    workers: int,
    spinner: Spinner = None,
) -> Tuple[List[str], List[str]]:
    notes = []
    discovered_hosts = set(hosts)

    networks = []
    status("Collecting local IPv4 subnets")
    if spinner:
        spinner.update("Collecting local subnets")
    auto_networks, auto_notes = get_local_ipv4_subnets()
    notes.extend(auto_notes)
    networks.extend(auto_networks)

    for subnet in subnets:
        try:
            networks.append(ipaddress.ip_network(subnet, strict=False))
        except ValueError as exc:
            notes.append(f"Ignoring invalid subnet {subnet!r}: {exc}")

    candidate_hosts = set(discovered_hosts)
    for network in networks:
        for host in iter_network_hosts(network, max_hosts):
            candidate_hosts.add(host)

    if not candidate_hosts:
        status("No LAN hosts to probe")
        return [], notes

    status(f"Probing {len(candidate_hosts)} LAN hosts for instrument ports")
    if spinner:
        spinner.update(f"Probing {len(candidate_hosts)} LAN hosts")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                lambda h: h
                if any(port_open(h, port, timeout) for port in LAN_PROBE_PORTS)
                else None,
                host,
            ): host
            for host in sorted(candidate_hosts)
        }
        for future in as_completed(futures):
            host = future.result()
            if host:
                discovered_hosts.add(host)
                status(f"Host responded on an instrument port: {host}")

    return sorted(discovered_hosts), notes


def probe_lan_resources(rm, hosts: Sequence[str], spinner: Spinner = None) -> Tuple[List[str], List[str]]:
    resources = []
    notes = []

    for host in hosts:
        found = False
        last_error = None
        status(f"Probing VISA resources on {host}")
        if spinner:
            spinner.update(f"Probing {host}")

        for resource_name in candidate_lan_resources(host):
            try:
                status(f"Trying {resource_name}")
                inst = rm.open_resource(resource_name)
                configure_instrument(inst, resource_name)
                inst.query("*IDN?")
                inst.close()
                resources.append(resource_name)
                found = True
                status(f"Connected to {resource_name}")
                break
            except Exception as exc:
                last_error = exc

        if not found and last_error is not None:
            notes.append(f"LAN probe failed for {host}: {last_error}")

    return resources, notes


def configure_instrument(inst, resource_name: str):
    inst.timeout = 2000

    # USB RAW resources often do not expose line-based terminations.
    if resource_name.upper().endswith("::RAW"):
        return

    inst.read_termination = "\n"
    inst.write_termination = "\n"


def query_serial_identity_raw_once(inst) -> str:
    inst.flush(pyvisa.constants.BufferOperation.discard_read_buffer)

    original_timeout = inst.timeout
    try:
        inst.timeout = 1000
        inst.baud_rate = 9600
        inst.data_bits = 8
        inst.parity = pyvisa.constants.Parity.none
        inst.stop_bits = pyvisa.constants.StopBits.one
        inst.read_termination = None
        inst.write_termination = None

        inst.write_raw(b"*IDN?")
        time.sleep(0.05)
        inst.timeout = 200

        data = bytearray()
        while True:
            try:
                chunk = inst.read_bytes(1, break_on_termchar=False)
                if not chunk:
                    break
                data.extend(chunk)
            except pyvisa.errors.VisaIOError:
                break

        text = bytes(data).replace(b"\x00", b"").decode("ascii", errors="replace")
        return "".join(ch for ch in text if ch.isprintable()).strip()
    finally:
        inst.timeout = original_timeout


def query_serial_identity_raw(inst) -> str:
    responses = []
    for _ in range(3):
        response = query_serial_identity_raw_once(inst)
        if response:
            responses.append(response)
        time.sleep(0.1)

    if not responses:
        return ""
    return max(responses, key=len)


def query_identity(inst, resource_name: str) -> str:
    configure_instrument(inst, resource_name)
    try:
        return inst.query("*IDN?").strip()
    except Exception:
        if resource_name.upper().startswith("ASRL"):
            identity = query_serial_identity_raw(inst)
            if identity:
                return identity
        raise


def probe_usb_devices() -> Tuple[List[dict], List[str]]:
    errors = []

    try:
        import usb.core
        import usb.util
    except Exception as exc:
        return [], [f"PyUSB import failed: {exc}"]

    try:
        devices = list(usb.core.find(find_all=True))
    except Exception as exc:
        return [], [f"PyUSB enumeration failed: {exc}"]

    results = []
    for dev in devices:
        device_path = None
        if sys.platform.startswith("linux"):
            device_path = (
                f"/dev/bus/usb/{getattr(dev, 'bus', 0):03d}/{getattr(dev, 'address', 0):03d}"
            )
        entry = {
            "vendor_id": f"0x{dev.idVendor:04x}",
            "product_id": f"0x{dev.idProduct:04x}",
            "bus": getattr(dev, "bus", "?"),
            "address": getattr(dev, "address", "?"),
            "device_path": device_path,
            "manufacturer": None,
            "product": None,
            "serial": None,
            "is_usbtmc": False,
            "can_read": os.access(device_path, os.R_OK) if device_path else None,
            "can_write": os.access(device_path, os.W_OK) if device_path else None,
        }

        try:
            for cfg in dev:
                for intf in cfg:
                    if intf.bInterfaceClass == 0xFE and intf.bInterfaceSubClass == 0x03:
                        entry["is_usbtmc"] = True
                        break
                if entry["is_usbtmc"]:
                    break
        except Exception:
            pass

        for field, index in (
            ("manufacturer", dev.iManufacturer),
            ("product", dev.iProduct),
            ("serial", dev.iSerialNumber),
        ):
            if not index:
                continue
            try:
                entry[field] = usb.util.get_string(dev, index)
            except Exception:
                entry[field] = None

        results.append(entry)

    return results, errors


UDEV_RULES_PATH = "/etc/udev/rules.d/99-usbtmc.rules"

# Groups used for USB device access, in order of preference across distros.
# plugdev  — Debian/Ubuntu/Mint
# usbusers — some RPM-based distros
# usb      — openSUSE and others
# users    — common fallback on Arch-based and other distros
_USB_GROUP_CANDIDATES = ["plugdev", "usbusers", "usb", "users"]


def detect_usb_group() -> tuple[str | None, bool]:
    """Return (group, user_is_member) for the best USB-access group on this system.

    Prefers a group the current user already belongs to.  Falls back to a group
    that exists but the user is not yet in (caller should warn).  Returns
    (None, False) when no candidate group exists at all.
    """
    try:
        import grp
        import os
        current_gids = set(os.getgroups())
        current_groups = {grp.getgrgid(g).gr_name for g in current_gids if g in {g2.gr_gid for g2 in grp.getgrall()}}
        existing = {g.gr_name: g for g in grp.getgrall()}

        # Prefer a candidate the user is already in
        for name in _USB_GROUP_CANDIDATES:
            if name in existing and name in current_groups:
                return name, True

        # Fall back to a candidate that exists (user needs to be added)
        for name in _USB_GROUP_CANDIDATES:
            if name in existing:
                return name, False
    except Exception:
        pass
    return None, False


def build_udev_rule(vendor_id: str, product_id: str, group: str | None, world_writable: bool = False) -> str:
    mode = "0666" if world_writable else "0664"
    rule = (
        f'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{vendor_id}", '
        f'ATTRS{{idProduct}}=="{product_id}", MODE="{mode}"'
    )
    if group:
        rule += f', GROUP="{group}"'
    rule += ', TAG+="uaccess"'
    return rule


def _has_usb_permission_issue() -> bool:
    devices, _ = probe_usb_devices()
    return any(d["is_usbtmc"] and not d["can_write"] for d in devices)


def suggest_udev_fix(devices: List[dict]):
    if not sys.platform.startswith("linux"):
        return
    blocked = [d for d in devices if d["is_usbtmc"] and not d["can_write"]]
    if not blocked:
        return
    group, member = detect_usb_group()
    world_writable = group is None
    print("udev fix suggestion:")
    print("  One or more USBTMC instruments are not writable by your user.")
    print("  Run with --fix-udev to apply the fix automatically, or manually create")
    print(f"  {UDEV_RULES_PATH} with:")
    print()
    for dev in blocked:
        print(f"    {build_udev_rule(dev['vendor_id'][2:], dev['product_id'][2:], group, world_writable)}")
    print()
    if group and not member:
        print(f"  Then add yourself to the '{group}' group:")
        print(f"    sudo usermod -aG {group} $USER")
        print("  (Log out and back in for the group change to take effect.)")
        print()
    print("  Then reload udev and replug the device:")
    print("    sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb")
    print()


def print_usb_diagnostics():
    devices, errors = probe_usb_devices()
    if errors:
        print("USB diagnostics:")
        for error in errors:
            print(f"  - {error}")
        print()
        return

    if not devices:
        print("USB diagnostics:")
        print("  - PyUSB sees no USB devices.")
        print()
        return

    print("USB diagnostics:")
    for dev in devices:
        summary = (
            f"  - {dev['vendor_id']}:{dev['product_id']} "
            f"bus={dev['bus']} addr={dev['address']}"
        )
        labels = [dev["manufacturer"], dev["product"], dev["serial"]]
        labels = [label for label in labels if label]
        if labels:
            summary += " " + " | ".join(labels)
        if dev["is_usbtmc"]:
            summary += " [USBTMC]"
        if dev["device_path"] and not (dev["can_read"] and dev["can_write"]):
            perms = []
            if not dev["can_read"]:
                perms.append("read")
            if not dev["can_write"]:
                perms.append("write")
            summary += f" [no {'/'.join(perms)} access to {dev['device_path']}]"
        print(summary)
    print()
    suggest_udev_fix(devices)


def fix_udev():
    if not sys.platform.startswith("linux"):
        print("--fix-udev is only supported on Linux.")
        return

    devices, errors = probe_usb_devices()
    if errors:
        for e in errors:
            print(f"Error: {e}")
        return

    blocked = [d for d in devices if d["is_usbtmc"] and not d["can_write"]]
    if not blocked:
        print("No USBTMC devices with permission issues found — nothing to fix.")
        return

    group, member = detect_usb_group()
    world_writable = group is None
    if group and member:
        print(f"Detected USB access group on this system: {group!r} (you are a member).")
    elif group:
        print(f"Detected USB access group on this system: {group!r} (you are NOT a member).")
        print(f"After applying the rule, also run:  sudo usermod -aG {group} $USER")
        print("Then log out and back in for the group change to take effect.")
    else:
        print("No known USB access group found; using MODE=\"0666\" (world-writable).")
    print()

    lines = [build_udev_rule(d["vendor_id"][2:], d["product_id"][2:], group, world_writable) for d in blocked]
    content = "\n".join(lines) + "\n"

    print(f"Will write {UDEV_RULES_PATH} with:")
    for line in lines:
        print(f"  {line}")
    print()

    try:
        answer = input("Proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if answer != "y":
        return

    result = subprocess.run(
        ["sudo", "tee", UDEV_RULES_PATH],
        input=content.encode(),
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"Failed to write udev rules: {result.stderr.decode().strip()}")
        return

    for cmd in (
        ["sudo", "udevadm", "control", "--reload-rules"],
        ["sudo", "udevadm", "trigger", "--subsystem-match=usb"],
    ):
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            print(f"Command failed: {' '.join(cmd)}")
            print(r.stderr.decode().strip())
            return

    print("udev rules applied. Replug the device if it is still not accessible.")


def print_discovery_notes(notes: Sequence[str]):
    if not notes:
        return

    print("Discovery notes:")
    for note in notes:
        print(f"  - {note}")
    print()


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", name.strip()).strip("_") or "workbench"


def suggest_tests(instruments: List[dict]) -> List[str]:
    roles = {i["role"] for i in instruments if i.get("role")}
    tests = []
    if {"scope", "generator"}.issubset(roles):
        tests.append("ac_frequency_sweep")
    if {"scope", "psu"}.issubset(roles):
        tests.append("psu_ramp_capture")
    if "dmm" in roles:
        tests.append("dmm_logger")
    return tests


def save_workbench(name: str, instruments: List[dict]) -> str:
    os.makedirs(WORKBENCH_DIR, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "instruments": instruments,
        "suggested_tests": suggest_tests(instruments),
    }
    path = os.path.join(WORKBENCH_DIR, f"{_safe_name(name)}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def main():
    global _debug

    if not check_dependencies():
        return

    args = build_arg_parser().parse_args()
    _debug = args.debug

    if args.fix_udev:
        fix_udev()
        return

    if pyvisa is None:
        print_dependency_notice("PyVISA is not installed.")
        return

    with Spinner("Opening VISA resource manager") as spinner:
        status("Opening VISA resource manager")
        try:
            rm = open_resource_manager(args.backend)
        except RuntimeError as exc:
            print_dependency_notice(str(exc))
            return

        resources, discovery_notes = discover_resources(rm, spinner)
        serial_metadata = serial_port_metadata()

        if not args.usb_only:
            lan_hosts, lan_scan_notes = discover_lan_hosts(
                hosts=args.host,
                subnets=args.subnet,
                timeout=args.scan_timeout,
                max_hosts=args.max_hosts,
                workers=args.workers,
                spinner=spinner,
            )
            discovery_notes.extend(lan_scan_notes)

            if lan_hosts:
                status(f"Found {len(lan_hosts)} LAN host(s) worth probing via VISA")
            lan_resources, lan_notes = probe_lan_resources(rm, lan_hosts, spinner)
            resources = sorted(set(resources) | set(lan_resources))
            discovery_notes.extend(lan_notes)

        if not args.all_resources:
            resources = [
                name
                for name in resources
                if name.upper().startswith(("TCPIP", "USB"))
                or is_usb_serial_resource(name, serial_metadata)
            ]

        if args.usb_only:
            resources = [name for name in resources if name.upper().startswith("USB")]

        instrument_reports = []
        instrument_data = []

        if resources:
            status("Collecting identity information for discovered resources")
            if spinner:
                spinner.update("Identifying instruments")

            for resource_name in resources:
                conn = connection_type(resource_name)
                status(f"Querying identity for {resource_name}")
                inst = None

                try:
                    inst = rm.open_resource(resource_name)
                    idn = query_identity(inst, resource_name)
                    manufacturer, model, serial, firmware = parse_idn(idn)
                    family = _db_classify(idn) if _db_classify else None
                    if family:
                        type_str = f"{family['type']}  ({family['vendor']} {family['series']})"
                        role = _TYPE_TO_ROLE.get(family["type"], family["type"])
                    else:
                        type_str = "unknown"
                        role = None
                    instrument_reports.append(
                        (
                            f"Resource string : {resource_name}\n"
                            f"Connection      : {conn}\n"
                            f"Manufacturer    : {manufacturer or 'Unknown'}\n"
                            f"Model           : {model or 'Unknown'}\n"
                            f"Type            : {type_str}\n"
                            f"Serial          : {serial or 'Unknown'}\n"
                            f"Firmware        : {firmware or 'Unknown'}\n"
                        )
                    )
                    instrument_data.append({
                        "resource": resource_name,
                        "connection": conn,
                        "manufacturer": manufacturer,
                        "model": model,
                        "serial": serial,
                        "firmware": firmware,
                        "type": family["type"] if family else None,
                        "role": role,
                        "family_id": family["id"] if family else None,
                    })

                except Exception as e:
                    if inst is not None:
                        error_text = f"Device found but limited VISA support (*IDN? failed: {e})"
                    else:
                        error_text = str(e)
                    instrument_reports.append(
                        (
                            f"Resource string : {resource_name}\n"
                            f"Connection      : {conn}\n"
                            f"Error           : {error_text}\n"
                        )
                    )
                finally:
                    if inst is not None:
                        inst.close()

        status("Scan complete")

    # Spinner has exited — terminal line is clear, print results.
    print_discovery_notes(discovery_notes)

    if not instrument_reports:
        print_usb_diagnostics()
        if sys.platform.startswith("linux") and _has_usb_permission_issue():
            print("USB permission issues detected. Run with --fix-udev to fix automatically.")
            try:
                answer = input("Apply udev fix now? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                answer = ""
            if answer == "y":
                fix_udev()
        print("No VISA instruments found.")
        return

    print("Detected VISA instruments:\n")
    if args.usb_only:
        print_usb_diagnostics()
    for report in instrument_reports:
        print(report)

    if instrument_data:
        save_name = args.save
        if save_name is None:
            try:
                answer = input("Save this workbench? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                answer = ""
            if answer == "y":
                try:
                    save_name = input("Workbench name: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    save_name = ""
        if save_name:
            path = save_workbench(save_name, instrument_data)
            tests = suggest_tests(instrument_data)
            print(f"Workbench saved: {path}")
            if tests:
                print(f"Suggested tests: {', '.join(tests)}")


if __name__ == "__main__":
    main()
