"""What the Pi itself is doing: addresses, memory, temperature, storage.

Everything here is read from /proc, /sys or a short command, so a missing file or a machine
that isn't a Pi leaves a row out rather than breaking the page."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import time

log = logging.getLogger(__name__)

# Bits of vcgencmd get_throttled, newest first: the lower half is now, the upper half is since boot.
THROTTLE_BITS = ((0, "under-voltage"), (1, "frequency capped"), (2, "throttled"),
                 (3, "temperature limit"))
SLOW_TTL = 20.0   # addresses and throttling change rarely and cost a process each
_slow: tuple[float, dict] = (0.0, {})


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip("\x00 \n")
    except OSError:
        return ""


def _run(args: list[str], timeout: float = 2.0) -> str:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return done.stdout.strip() if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _bytes_text(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return f"{count:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{count:.1f} {unit}"
        count /= 1024
    return ""


def _uptime_text(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def model() -> str:
    name = _read("/proc/device-tree/model") or _read("/sys/firmware/devicetree/base/model")
    return name or f"{platform.system()} {platform.machine()}"


def temperature() -> float | None:
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return int(raw) / 1000
    except ValueError:
        return None


def memory() -> tuple[int, int] | None:
    """(used, total) in bytes, counting cache as free like free(1) does."""
    values = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    values[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    if len(values) != 2:
        return None
    return values["MemTotal"] - values["MemAvailable"], values["MemTotal"]


def addresses() -> list[str]:
    """Every address this Pi answers on, so the screen can be checked against the router."""
    found = []
    for line in _run(["ip", "-o", "-4", "addr", "show", "scope", "global"]).splitlines():
        parts = line.split()
        if len(parts) > 3:
            found.append(f"{parts[3].split('/')[0]} ({parts[1]})")
    if not found:  # no ip(8): ask the routing table which address would be used
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))  # a reserved address; nothing is sent
            found.append(probe.getsockname()[0])
        except OSError:
            pass
        finally:
            probe.close()
    return found


def throttling() -> tuple[str, bool]:
    """What the firmware says about power and heat, as a phrase plus whether it's happening now."""
    raw = _run(["vcgencmd", "get_throttled"]).partition("=")[2]
    try:
        flags = int(raw, 16)
    except ValueError:
        return "", False
    now = [name for bit, name in THROTTLE_BITS if flags & (1 << bit)]
    before = [name for bit, name in THROTTLE_BITS if flags & (1 << (bit + 16))]
    if now:
        return ", ".join(now).capitalize() + " now", True
    if before:
        return ", ".join(before).capitalize() + " since boot", False
    return "None", False


def _slow_bits() -> dict:
    global _slow
    if time.monotonic() - _slow[0] < SLOW_TTL:
        return _slow[1]
    phrase, throttled = throttling()
    bits = {"addresses": addresses(), "throttling": phrase, "throttled_now": throttled}
    _slow = (time.monotonic(), bits)
    return bits


def snapshot(music_dir: str = "") -> dict:
    """One reading of everything the Health page shows."""
    out: dict = {"model": model(), "host": socket.gethostname(),
                 "os": f"{platform.system()} {platform.release()}"}
    out.update(_slow_bits())
    temp = temperature()
    if temp is not None:
        out["temperature"] = temp
    used = memory()
    if used is not None:
        out["memory"] = f"{_bytes_text(used[0])} of {_bytes_text(used[1])} used"
        out["memory_share"] = used[0] / used[1] if used[1] else 0.0
    try:
        out["load"] = ", ".join(f"{v:.2f}" for v in os.getloadavg())
    except (OSError, AttributeError):  # getloadavg is Unix only
        pass
    uptime = _read("/proc/uptime").split(" ")[0]
    if uptime:
        try:
            out["uptime"] = _uptime_text(float(uptime))
        except ValueError:
            pass
    freq = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if freq.isdigit():
        out["cpu"] = f"{int(freq) / 1000:.0f} MHz x{os.cpu_count() or 1}"
    for label, path in (("storage", "/"), ("music", music_dir)):
        if not path:
            continue
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        out[label] = f"{_bytes_text(usage.free)} free of {_bytes_text(usage.total)}"
    return out
