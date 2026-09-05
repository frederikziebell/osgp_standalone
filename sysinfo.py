"""Host system health stats (CPU load, memory, temperature) for the dashboard's small
status readout - stdlib only, no psutil. Linux-only (reads /proc and /sys), which is
fine since the deployment target is Raspberry Pi OS; any field that can't be read
(missing file, running on a non-Linux dev machine, etc.) comes back as None rather
than raising, so the dashboard can just omit it.
"""

import glob
import os


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


def get_system_stats():
    return {
        "cpu_load_1m": _read_load_average(),
        "cpu_count": os.cpu_count(),
        "mem_percent": _read_memory_percent(),
        "temp_c": _read_temp_c(),
    }
