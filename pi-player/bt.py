"""Bluetooth speakers via bluetoothctl (slow - keep off the UI thread)."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

_DEVICE_LINE = re.compile(r"Device ([0-9A-F]{2}(?::[0-9A-F]{2}){5}) (.+)", re.I)


@dataclass(frozen=True)
class Device:
    address: str
    name: str
    paired: bool = False
    connected: bool = False


def available() -> bool:
    return shutil.which("bluetoothctl") is not None


def _ctl(*args: str, timeout: float = 15.0) -> str | None:
    try:
        result = subprocess.run(
            ["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("bluetoothctl %s: %s", " ".join(args), exc)
        return None
    return result.stdout


def _parse_devices(text: str | None) -> dict[str, str]:
    found = {}
    for line in (text or "").splitlines():
        match = _DEVICE_LINE.search(line)
        if match:
            found[match.group(1).upper()] = match.group(2).strip()
    return found


def _info(address: str) -> str:
    return _ctl("info", address) or ""


def devices() -> list[Device]:
    """Known devices, connected first, then paired, then the rest."""
    everything = _parse_devices(_ctl("devices"))
    paired = _parse_devices(_ctl("devices", "Paired"))
    if not paired:  # BlueZ before 5.65
        paired = _parse_devices(_ctl("paired-devices"))
    listed = []
    for address, name in everything.items():
        is_paired = address in paired
        connected = is_paired and "Connected: yes" in _info(address)
        # Unnamed devices show their address as the name - not useful.
        if not is_paired and name.replace("-", ":").upper() == address:
            continue
        listed.append(Device(address, name, is_paired, connected))
    return sorted(listed, key=lambda d: (not d.connected, not d.paired, d.name.lower()))


def scan(seconds: int = 10) -> None:
    _ctl("power", "on")
    _ctl("--timeout", str(seconds), "scan", "on", timeout=seconds + 10)


def connect(address: str) -> bool:
    _ctl("power", "on")
    if "Paired: yes" not in _info(address):
        _ctl("pair", address, timeout=30)
    _ctl("trust", address)
    _ctl("connect", address, timeout=20)
    return is_connected(address)


def disconnect(address: str) -> None:
    _ctl("disconnect", address)


def is_connected(address: str) -> bool:
    return "Connected: yes" in _info(address)


def sink_name(address: str) -> str:
    """The name WirePlumber gives a connected speaker's sink (prefix)."""
    return "bluez_output." + address.replace(":", "_")
