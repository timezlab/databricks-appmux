"""Spike 7, runs on the probe app: how much CPU time can this container actually use? Not part of appmux.

Runs N busy loops for a while and counts the CPU seconds they got from their own accounting, next to what /proc/stat says the
machine did. Prints one JSON line.

usage: cpu_ceiling.py <busy loops> [<seconds>]
"""
from __future__ import annotations

import json
import os
import sys
import time

DEFAULT_SECONDS = 6.0
STAT_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
CGROUP_CPU_FILES = ("cpu.max", "cpu.stat", "cpu.weight")


def busy(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass
    os._exit(0)


def machine_seconds() -> dict[str, float]:
    with open("/proc/stat") as handle:
        values = [int(v) for v in handle.readline().split()[1:9]]
    return {name: value / os.sysconf("SC_CLK_TCK") for name, value in zip(STAT_FIELDS, values)}


def pressure_some_avg10() -> float | None:
    try:
        with open("/proc/pressure/cpu") as handle:
            return float(handle.readline().split()[1].split("=")[1])
    except (OSError, IndexError, ValueError):
        return None  # no pressure-stall information on this kernel


def main() -> None:
    loops = int(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_SECONDS
    before = machine_seconds()
    started = time.monotonic()
    pids = []
    for _ in range(loops):
        pid = os.fork()
        if pid == 0:
            busy(seconds)
        pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)
    wall_s = time.monotonic() - started
    times = os.times()
    after = machine_seconds()
    cpu_s = times.children_user + times.children_system
    delta = {name: after[name] - before[name] for name in STAT_FIELDS}
    print(json.dumps({
        "busy_loops": loops, "seconds": round(wall_s, 2), "visible_cpus": os.cpu_count(),
        "cpu_seconds_the_loops_got": round(cpu_s, 2),
        "cpu_percent_of_one_cpu_in_total": round(cpu_s / wall_s * 100),
        "cpu_percent_of_one_cpu_per_loop": round(cpu_s / wall_s / loops * 100),
        "proc_stat_idle_percent_of_all_cpus": round(delta["idle"] / (wall_s * (os.cpu_count() or 1)) * 100),
        "proc_stat_steal_seconds": round(delta["steal"], 2),
        "cpu_pressure_some_avg10_after": pressure_some_avg10(),
        "cgroup_cpu_files_visible": [name for name in CGROUP_CPU_FILES if os.path.exists(f"/sys/fs/cgroup/{name}")],
    }))


if __name__ == "__main__":
    main()
