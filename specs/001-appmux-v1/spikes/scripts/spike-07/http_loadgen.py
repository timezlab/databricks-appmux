"""Spike 7, runs on the probe app: an HTTP/1.1 load generator. Not part of appmux.

`connections` keep-alive connections for `duration_s` seconds. By default each sends the next request when the previous response is
complete (closed loop, as fast as the path allows). With a `rate_per_s`, requests fall due at that total rate, spread over the
connections, and the latency of a request is counted from when it fell due, so a stall delays every request that was waiting
behind it. Prints one JSON line, including this process's own CPU seconds (the driver cannot read them after the process ends).

usage: http_loadgen.py <host> <port> <connections> <duration_s> [<path> [<rate_per_s>]]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

HEAD_END = b"\r\n\r\n"


def percentile(sorted_values: list[float], fraction: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * fraction))]


async def worker(host: str, port: int, request: bytes, deadline: float, latencies: list[float], failures: list[str],
                 interval_s: float, first_due: float) -> None:
    due = first_due
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as exc:
        failures.append(type(exc).__name__)
        return
    try:
        while time.monotonic() < deadline:
            if interval_s:
                wait_s = due - time.monotonic()
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                started = due
                due += interval_s
            else:
                started = time.monotonic()
            writer.write(request)
            head = await reader.readuntil(HEAD_END)
            if not head.startswith(b"HTTP/1.1 200"):
                failures.append("status " + head[9:12].decode(errors="replace"))
            for line in head.split(b"\r\n"):
                if line[:15].lower() == b"content-length:":
                    await reader.readexactly(int(line[15:]))
            latencies.append(time.monotonic() - started)
    except (OSError, asyncio.IncompleteReadError) as exc:
        failures.append(type(exc).__name__)
    finally:
        writer.close()


async def main() -> None:
    host, port, connections, duration_s = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
    path = sys.argv[5] if len(sys.argv) > 5 else "/"
    rate_per_s = float(sys.argv[6]) if len(sys.argv) > 6 else 0.0
    interval_s = connections / rate_per_s if rate_per_s else 0.0
    request = f"GET {path} HTTP/1.1\r\nHost: probe\r\nUser-Agent: spike-7\r\nAccept: */*\r\nX-Forwarded-Email: spoof@example.invalid\r\n\r\n".encode()
    latencies: list[float] = []
    failures: list[str] = []
    started = time.monotonic()
    deadline = started + duration_s
    # first requests are spread over one interval so the connections do not all fire at the same instant
    await asyncio.gather(*(worker(host, port, request, deadline, latencies, failures, interval_s, started + interval_s * index / connections)
                           for index in range(connections)))
    seconds = time.monotonic() - started
    latencies.sort()
    out = {"connections": connections, "requests": len(latencies), "seconds": round(seconds, 2), "per_second": round(len(latencies) / seconds),
           "failures": len(failures), "failure_kinds": sorted(set(failures)), "cpu_seconds": round(time.process_time(), 3)}
    if rate_per_s:
        out["rate_per_s"] = rate_per_s
    if latencies:
        out["latency_ms"] = {"p50": round(percentile(latencies, 0.5) * 1000, 2), "p95": round(percentile(latencies, 0.95) * 1000, 2),
                             "p99": round(percentile(latencies, 0.99) * 1000, 2), "max": round(latencies[-1] * 1000, 2)}
    print(json.dumps(out))


if __name__ == "__main__":
    asyncio.run(main())
