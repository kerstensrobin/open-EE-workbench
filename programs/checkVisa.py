#!/usr/bin/env python3

import argparse
import importlib.util
import ipaddress
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, List, Sequence, Tuple

REQUIRED_PACKAGES = [
    ("pyvisa",    "pyvisa",    "PyVISA — VISA resource manager"),
    ("pyvisa_py", "pyvisa-py", "PyVISA-py — pure-Python VISA backend"),
    ("usb",       "pyusb",     "PyUSB — low-level USB device access"),
    ("zeroconf",  "zeroconf",  "zeroconf — mDNS/LAN instrument discovery"),
]


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
            print("  python3 checkVisa.py")
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


LAN_PROBE_PORTS = (5025, 4880, 111)


def status(message: str):
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
    return "Other"


def parse_idn(idn: str):
    parts = [p.strip() for p in idn.split(",")]
    while len(parts) < 4:
        parts.append("")
    return parts[0], parts[1], parts[2], parts[3]


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


def discover_resources(rm) -> Tuple[List[str], List[str]]:
    resources = set()
    errors = []

    status("Querying standard VISA resources")
    standard_resources, standard_errors = list_standard_resources(rm)
    resources.update(standard_resources)
    errors.extend(standard_errors)

    if not any(name.upper().startswith("USB") for name in resources):
        status("No USB VISA resources reported by backend, trying USB fallback")
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
) -> Tuple[List[str], List[str]]:
    notes = []
    discovered_hosts = set(hosts)

    networks = []
    status("Collecting local IPv4 subnets")
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


def probe_lan_resources(rm, hosts: Sequence[str]) -> Tuple[List[str], List[str]]:
    resources = []
    notes = []

    for host in hosts:
        found = False
        last_error = None
        status(f"Probing VISA resources on {host}")

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


def query_identity(inst, resource_name: str) -> str:
    configure_instrument(inst, resource_name)
    return inst.query("*IDN?").strip()


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


def main():
    if not check_dependencies():
        return

    args = build_arg_parser().parse_args()

    if args.fix_udev:
        fix_udev()
        return

    if pyvisa is None:
        print_dependency_notice("PyVISA is not installed.")
        return

    status("Opening VISA resource manager")
    try:
        rm = open_resource_manager(args.backend)
    except RuntimeError as exc:
        print_dependency_notice(str(exc))
        return

    resources, discovery_notes = discover_resources(rm)
    lan_hosts, lan_scan_notes = discover_lan_hosts(
        hosts=args.host,
        subnets=args.subnet,
        timeout=args.scan_timeout,
        max_hosts=args.max_hosts,
        workers=args.workers,
    )
    discovery_notes.extend(lan_scan_notes)
    if lan_hosts:
        status(f"Found {len(lan_hosts)} LAN host(s) worth probing via VISA")
    lan_resources, lan_notes = probe_lan_resources(rm, lan_hosts)
    resources = sorted(set(resources) | set(lan_resources))
    discovery_notes.extend(lan_notes)

    if not args.all_resources:
        resources = [
            name
            for name in resources
            if name.upper().startswith(("TCPIP", "USB"))
        ]

    if args.usb_only:
        resources = [name for name in resources if name.upper().startswith("USB")]

    if not resources:
        print("No VISA instruments found.")
        print_discovery_notes(discovery_notes)
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
        return

    status("Collecting identity information for discovered resources")
    instrument_reports = []

    for resource_name in resources:
        conn = connection_type(resource_name)

        try:
            status(f"Querying identity for {resource_name}")
            inst = rm.open_resource(resource_name)
            idn = query_identity(inst, resource_name)
            manufacturer, model, serial, firmware = parse_idn(idn)
            instrument_reports.append(
                (
                    f"Resource string : {resource_name}\n"
                    f"Connection      : {conn}\n"
                    f"Manufacturer    : {manufacturer or 'Unknown'}\n"
                    f"Model           : {model or 'Unknown'}\n"
                    f"Serial          : {serial or 'Unknown'}\n"
                    f"Firmware        : {firmware or 'Unknown'}\n"
                )
            )

            inst.close()

        except Exception as e:
            instrument_reports.append(
                (
                    f"Resource string : {resource_name}\n"
                    f"Connection      : {conn}\n"
                    f"Error           : {e}\n"
                )
            )

    status("Scan complete")
    print()
    print_discovery_notes(discovery_notes)
    print("Detected VISA instruments:\n")
    if args.usb_only:
        print_usb_diagnostics()
    for report in instrument_reports:
        print(report)


if __name__ == "__main__":
    main()
