"""A paced HTTP/1.1 load generator for the edge's capacity run (T086, SC-001). Not part of appmux.

A port of ``specs/001-appmux-v1/spikes/scripts/spike-07/http_loadgen.py``, kept to the one mode the
capacity run needs. ``connections`` keep-alive connections each send ``GET <path>``; requests fall
due at ``rate_per_s`` in total, spread over the connections, and the latency of a request is counted
from when it fell due, so a stall delays every request that was waiting behind it (a generator that
simply waited for each answer would hide the stall). The first requests are spread over one
interval, so the connections do not all fire at the same instant.

The run prints one JSON line (``LoadReport``): requests, the rate reached, the error rate, the
latency percentiles including p95, and the CPU of this generator. With ``--edge-pid`` it also
reports the CPU the edge process used during the run, read from ``/proc/<pid>/stat``, which works
only on the machine the edge runs on: the capacity run starts this generator inside the container.

Inside the container the generator plays the Databricks proxy by sending ``X-Forwarded-Email``
itself, so it can reach the edge on the container's own port. From another machine it goes through
the real proxy of the deployed App instead: ``run_load`` is then given a ``tls`` context and a
``bearer_token`` (the token of the person the proxy lets in), which it sends as ``Authorization``.
It sends no body, and the report holds neither an e-mail nor a token. The command line has only the
first form, because a token on a command line is visible to every process on the machine.

usage: python http_load.py --host H --port P --path /slug/ --email E
           [--connections 10] [--rate 500] [--duration 30] [--edge-pid PID]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import ssl
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Final

HEAD_END: Final = b"\r\n\r\n"
DEFAULT_CONNECTIONS: Final = 10
DEFAULT_RATE_PER_S: Final = 500.0
DEFAULT_DURATION_S: Final = 30.0
_MS_PER_S: Final = 1000
_PERCENT: Final = 100
# `/proc/<pid>/stat`: after the command name (field 2, in parentheses), field 3 is the state, so
# field n sits at index n - 3 of the text that follows. Fields 14 and 15 are user and system time.
_STAT_FIRST_FIELD_AFTER_COMMAND: Final = 3
_STAT_USER_TIME_FIELD: Final = 14
_STAT_SYSTEM_TIME_FIELD: Final = 15
# A path or e-mail goes into the raw request line and a header, so a control character in either
# would let the caller add a header or a second request.
_UNSAFE_REQUEST_TEXT: Final = re.compile(r"[\x00-\x20\x7f]")
_UNSAFE_HEADER_TEXT: Final = re.compile(r"[\x00-\x1f\x7f]")
_DEFAULT_HTTPS_PORT: Final = 443


class UnframedResponse(Exception):
    """A response with neither ``Content-Length`` nor chunked framing: where it ends is unknown."""


@dataclass(frozen=True, slots=True)
class LatencyMs:
    p50: float
    p95: float
    p99: float
    max: float


@dataclass(frozen=True, slots=True)
class LoadReport:
    """What one run measured. ``error_rate`` counts every request that was not a complete ``200``.

    ``requests`` are the exchanges that finished with a full response (whatever its status);
    ``failures`` are the ones among them that were not ``200``, plus the exchanges cut short by a
    connection error. A run in which nothing was attempted has an error rate of ``1.0``: no answer
    is not a pass.
    """

    connections: int
    target_rate_per_s: float
    seconds: float
    requests: int
    per_second: float
    failures: int
    failure_kinds: list[str]
    error_rate: float
    cpu_seconds: float
    latency_ms: LatencyMs | None
    edge_cpu_seconds: float | None = None
    edge_cpu_percent_of_one_core: float | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class _Tally:
    latencies_s: list[float] = field(default_factory=list)
    failure_kinds: list[str] = field(default_factory=list)
    interrupted: int = 0


def percentile(sorted_values: list[float], fraction: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * fraction))]


def cpu_seconds_from_stat(stat_text: str, ticks_per_second: int) -> float:
    """User plus system CPU seconds of the process a ``/proc/<pid>/stat`` line describes."""
    # The command name is in parentheses and may hold spaces or parentheses, so split after the
    # last closing one.
    after_command = stat_text.rpartition(")")[2].split()
    user = int(after_command[_STAT_USER_TIME_FIELD - _STAT_FIRST_FIELD_AFTER_COMMAND])
    system = int(after_command[_STAT_SYSTEM_TIME_FIELD - _STAT_FIRST_FIELD_AFTER_COMMAND])
    return (user + system) / ticks_per_second


def process_cpu_seconds(pid: int) -> float:
    """The CPU seconds the process ``pid`` has used so far; raises if there is no such process."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError as exc:
        raise ProcessLookupError(
            f"cannot read the CPU of process {pid}; give the pid of the edge, and run this "
            "on the machine the edge runs on"
        ) from exc
    return cpu_seconds_from_stat(text, os.sysconf("SC_CLK_TCK"))


async def _read_response(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """The head and the body of one response, read to its end so the connection can be reused."""
    head = await reader.readuntil(HEAD_END)
    lowered = head.lower()
    if b"\r\ntransfer-encoding: chunked" in lowered:
        pieces: list[bytes] = []
        while True:
            size = int((await reader.readline()).strip(), 16)
            if size == 0:
                await reader.readline()  # the empty line that ends the (absent) trailers
                break
            pieces.append(await reader.readexactly(size))
            await reader.readexactly(2)  # the CRLF after each chunk
        return head, b"".join(pieces)
    for line in head.split(b"\r\n"):
        if line[:15].lower() == b"content-length:":
            return head, await reader.readexactly(int(line[15:]))
    if head.startswith((b"HTTP/1.1 204", b"HTTP/1.1 304")):
        return head, b""
    raise UnframedResponse


async def _worker(
    host: str,
    port: int,
    request: bytes,
    tally: _Tally,
    *,
    tls: ssl.SSLContext | None,
    run_started: float,
    rate_per_s: float,
    duration_s: float,
    first_request: int,
    request_step: int,
) -> None:
    """Send requests ``first_request``, ``first_request + request_step``, ... of the run.

    Request number ``k`` falls due ``k / rate_per_s`` seconds after the run started, and it belongs
    to the run when that is under ``duration_s``. Both are seconds since the start, not readings of
    the monotonic clock: that clock reads the machine's uptime, and a sum or a running total of
    uptime and interval rounds differently at different readings, which let the request due exactly
    at the end of the run in on one machine and keep it out on another (GitHub's runner sent 201
    where 200 fell due).
    """
    request_number = first_request
    try:
        reader, writer = await asyncio.open_connection(host, port, ssl=tls)
    except OSError as exc:
        tally.failure_kinds.append(type(exc).__name__)
        tally.interrupted += 1
        return
    try:
        while (due_after_s := request_number / rate_per_s) < duration_s:
            fell_due = run_started + due_after_s  # latency counts from when the request fell due
            wait_s = fell_due - time.monotonic()
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            request_number += request_step
            writer.write(request)
            head, _ = await _read_response(reader)
            if not head.startswith(b"HTTP/1.1 200"):
                tally.failure_kinds.append("status " + head[9:12].decode(errors="replace"))
            tally.latencies_s.append(time.monotonic() - fell_due)
    except (OSError, asyncio.IncompleteReadError, UnframedResponse, ValueError) as exc:
        # ValueError: a chunk size that is not hexadecimal. Either way the connection is unusable.
        tally.failure_kinds.append(type(exc).__name__)
        tally.interrupted += 1
    finally:
        writer.close()
        with contextlib.suppress(OSError):  # the connection may already be broken
            await writer.wait_closed()


def _identity_header(email: str | None, bearer_token: str | None) -> str:
    """The one header that says who is calling; exactly one of the two forms must be given."""
    if (email is None) == (bearer_token is None):
        raise ValueError("exactly one of email and bearer_token must be set")
    if email is not None:
        if not email or _UNSAFE_HEADER_TEXT.search(email):
            raise ValueError("email must be set and hold no control character")
        return f"X-Forwarded-Email: {email}"
    if not bearer_token or _UNSAFE_HEADER_TEXT.search(bearer_token):
        raise ValueError("bearer_token must be set and hold no control character")
    return f"Authorization: Bearer {bearer_token}"


def _request_bytes(
    host: str,
    port: int,
    path: str,
    email: str | None,
    bearer_token: str | None,
    *,
    secure: bool,
) -> bytes:
    if not path.startswith("/") or _UNSAFE_REQUEST_TEXT.search(path):
        raise ValueError("path must start with '/' and hold no space or control character")
    identity = _identity_header(email, bearer_token)
    # A browser or curl never writes ``Host: name:443``, and the proxy of the deployed App routes
    # by that name, so the generator writes what they write.
    authority = host if secure and port == _DEFAULT_HTTPS_PORT else f"{host}:{port}"
    return (
        f"GET {path} HTTP/1.1\r\nHost: {authority}\r\nUser-Agent: appmux-load\r\n"
        f"Accept: */*\r\n{identity}\r\n\r\n"
    ).encode("latin-1")


async def run_load(
    *,
    host: str,
    port: int,
    path: str,
    email: str | None = None,
    bearer_token: str | None = None,
    tls: ssl.SSLContext | None = None,
    connections: int = DEFAULT_CONNECTIONS,
    rate_per_s: float = DEFAULT_RATE_PER_S,
    duration_s: float = DEFAULT_DURATION_S,
    edge_pid: int | None = None,
) -> LoadReport:
    if connections < 1 or rate_per_s <= 0 or duration_s <= 0:
        raise ValueError("connections, rate and duration must all be positive")
    request = _request_bytes(host, port, path, email, bearer_token, secure=tls is not None)
    tally = _Tally()
    edge_cpu_before = process_cpu_seconds(edge_pid) if edge_pid is not None else None
    started = time.monotonic()
    await asyncio.gather(
        *(
            _worker(
                host,
                port,
                request,
                tally,
                tls=tls,
                run_started=started,
                rate_per_s=rate_per_s,
                duration_s=duration_s,
                first_request=index,
                request_step=connections,
            )
            for index in range(connections)
        )
    )
    seconds = time.monotonic() - started
    edge_cpu_seconds = (
        process_cpu_seconds(edge_pid) - edge_cpu_before
        if edge_pid is not None and edge_cpu_before is not None
        else None
    )
    return _report(tally, connections, rate_per_s, seconds, edge_cpu_seconds)


def _report(
    tally: _Tally,
    connections: int,
    rate_per_s: float,
    seconds: float,
    edge_cpu_seconds: float | None,
) -> LoadReport:
    latencies = sorted(tally.latencies_s)
    attempts = len(latencies) + tally.interrupted
    failures = len(tally.failure_kinds)
    latency = (
        LatencyMs(
            p50=round(percentile(latencies, 0.50) * _MS_PER_S, 2),
            p95=round(percentile(latencies, 0.95) * _MS_PER_S, 2),
            p99=round(percentile(latencies, 0.99) * _MS_PER_S, 2),
            max=round(latencies[-1] * _MS_PER_S, 2),
        )
        if latencies
        else None
    )
    return LoadReport(
        connections=connections,
        target_rate_per_s=rate_per_s,
        seconds=round(seconds, 2),
        requests=len(latencies),
        per_second=round(len(latencies) / seconds, 1),
        failures=failures,
        failure_kinds=sorted(set(tally.failure_kinds)),
        error_rate=round(failures / attempts, 6) if attempts else 1.0,
        cpu_seconds=round(time.process_time(), 3),
        latency_ms=latency,
        edge_cpu_seconds=None if edge_cpu_seconds is None else round(edge_cpu_seconds, 3),
        edge_cpu_percent_of_one_core=(
            None if edge_cpu_seconds is None else round(edge_cpu_seconds / seconds * _PERCENT, 1)
        ),
    )


def _parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paced HTTP load against the appmux edge.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--path", required=True, help="for example /sales/")
    parser.add_argument("--email", required=True, help="who the generator says it is")
    parser.add_argument("--connections", type=int, default=DEFAULT_CONNECTIONS)
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE_PER_S, help="requests a second")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S, help="seconds")
    parser.add_argument("--edge-pid", type=int, default=None, help="report this process's CPU")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(sys.argv[1:] if argv is None else argv)
    report = asyncio.run(
        run_load(
            host=arguments.host,
            port=arguments.port,
            path=arguments.path,
            email=arguments.email,
            connections=arguments.connections,
            rate_per_s=arguments.rate,
            duration_s=arguments.duration,
            edge_pid=arguments.edge_pid,
        )
    )
    print(report.to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
