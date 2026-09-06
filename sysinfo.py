"""Host system health stats (CPU load, memory, temperature) for the dashboard's small
status readout - stdlib only, no psutil. Linux-only (reads /proc and /sys), which is
fine since the deployment target is Raspberry Pi OS; any field that can't be read
(missing file, running on a non-Linux dev machine, etc.) comes back as None rather
than raising, so the dashboard can just omit it.
"""

import glob
import os
import subprocess


def _read_temp_c():
    # Thermal zone numbering varies across Pi models/kernel versions (the CPU isn't
    # always thermal_zone0), so scan for the first zone file that actually parses
    # rather than hardcoding one path.
    for path in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
        try:
            with open(path) as f:
                millidegrees = int(f.read().strip())
        except (OSError, ValueError):
            continue
        return millidegrees / 1000.0
    return None


def _read_memory_percent():
    try:
        fields = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                fields[key] = int(rest.strip().split()[0])  # kB
    except (OSError, ValueError):
        return None
    total = fields.get("MemTotal")
    available = fields.get("MemAvailable")
    if not total or available is None:
        return None
    return (total - available) / total * 100.0


def _read_load_average():
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


def _read_db_size_bytes(db_path):
    if not db_path:
        return None
    try:
        return os.path.getsize(db_path)
    except OSError:
        return None


# Bit layout of `vcgencmd get_throttled`'s hex value (documented by the Raspberry Pi
# firmware): the low 3 bits are "right now", the same conditions shifted up by 16 are
# "has happened since boot". The Pi only ever detects *under*-voltage, not over-voltage
# or current - there's no such signal to read.
_THROTTLED_UNDERVOLTAGE_NOW = 0x1
_THROTTLED_THROTTLED_NOW = 0x4
_THROTTLED_UNDERVOLTAGE_EVER = 0x1 << 16
_THROTTLED_THROTTLED_EVER = 0x4 << 16


def _read_power_status():
    # Prefer vcgencmd: needs the binary (ships by default on Raspberry Pi OS) and the
    # 'video' group (or root), but tells us whether undervoltage/throttling has *ever*
    # happened since boot, not just whether it's happening at this exact instant - a
    # brief power blip between polls would otherwise go unnoticed.
    try:
        result = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                                 text=True, timeout=2.0)
        if result.returncode == 0:
            _, _, hex_value = result.stdout.strip().partition("=")
            bits = int(hex_value, 16)
            return {
                "source": "vcgencmd",
                "undervoltage_now": bool(bits & _THROTTLED_UNDERVOLTAGE_NOW),
                "undervoltage_ever": bool(bits & _THROTTLED_UNDERVOLTAGE_EVER),
                "throttled_now": bool(bits & _THROTTLED_THROTTLED_NOW),
                "throttled_ever": bool(bits & _THROTTLED_THROTTLED_EVER),
            }
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    # Fallback: the kernel's own undervoltage alarm via sysfs - no extra binary or
    # group needed, but only reports the current instantaneous state, not history.
    for name_path in glob.glob("/sys/class/hwmon/hwmon*/name"):
        try:
            with open(name_path) as f:
                if f.read().strip() != "rpi_volt":
                    continue
            alarm_path = os.path.join(os.path.dirname(name_path), "in0_lcrit_alarm")
            with open(alarm_path) as f:
                alarm = int(f.read().strip())
        except (OSError, ValueError):
            continue
        return {
            "source": "sysfs",
            "undervoltage_now": bool(alarm),
            "undervoltage_ever": None,
            "throttled_now": None,
            "throttled_ever": None,
        }
    return None


def get_system_stats(db_path=None):
    return {
        "cpu_load_1m": _read_load_average(),
        "cpu_count": os.cpu_count(),
        "mem_percent": _read_memory_percent(),
        "temp_c": _read_temp_c(),
        "db_bytes": _read_db_size_bytes(db_path),
        "power": _read_power_status(),
    }
