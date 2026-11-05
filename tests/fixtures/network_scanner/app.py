"""Hostile fixture network_scanner: tries to reach what a sandboxed child must not reach.

Run it only inside an appmux sandbox, through the launcher (design 6.2 to 6.4, spec FR-011). It is a
standalone Databricks-style app: ``python app.py`` and the port in ``DATABRICKS_APP_PORT``.

HTTP paths
    GET /          200 ``{"fixture": "network_scanner"}``, so the health check passes.
    GET /report    200 JSON, computed on every call; it takes up to ``CONNECT_TIMEOUT_S`` seconds
                   because a connect that a rule drops is a silent timeout.

A child cannot learn these addresses from inside its sandbox (it has no ``/proc/net/tcp`` of the
container), so the test that registers the fixture passes them in the release's ``env`` entries.
A target whose variable is unset is reported as ``not_configured`` and the test must treat that as
a failure of its own set-up, not as a pass.

Variables
    SCANNER_APPMUX_PORT          appmux's own port, tried on 127.0.0.1 (loopback).
    SCANNER_CONTAINER_IP         the container's IPv4 address.
    SCANNER_CONTAINER_PORTS      comma-separated TCP ports listening on that address (28 on Free
                                 Edition); each one is tried.
    SCANNER_DENIED_DESTINATION   ``host:port`` that an egress rule denies in the test.
The metadata address ``169.254.169.254:80`` is always tried.

``/report`` keys
    attempts         list of ``{"label", "host", "port", "result", "elapsed_ms"}``; ``label`` is
                     ``appmux_loopback``, ``container_ip``, ``metadata`` or ``denied_destination``;
                     ``result`` is ``"connected"`` (isolation failed), ``"timeout"`` or an errno
                     name.
    connected        ``"host:port"`` of every attempt that connected (must be []).
    not_configured   labels whose variable is unset (must be []).
    process_id,      the identity of this process: two reports with the same pair came from the
    started_at       same child, so a rule that took effect between them was reloaded, not read
                     by a new child.
"""

from __future__ import annotations

import errno
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI

CONNECT_TIMEOUT_S = 3.0
MAX_PARALLEL_CONNECTS = 32
METADATA_HOST = "169.254.169.254"
METADATA_PORT = 80
CONNECTED = "connected"
STARTED_AT = time.time()

app = FastAPI()


@dataclass(frozen=True, slots=True)
class Target:
    label: str
    host: str
    port: int


# Fixtures are standalone apps and cannot share code, so the hostile ones repeat this helper.
def errno_name(exc: OSError) -> str:
    """The errno name of a failed system call, or the exception type when it carries no errno."""
    if exc.errno is None:
        return type(exc).__name__
    return errno.errorcode.get(exc.errno, type(exc).__name__)


def parse_ports(text: str) -> list[int]:
    """``"22, 80"`` becomes ``[22, 80]``; a bad entry raises ValueError at start-up."""
    return [int(entry) for entry in text.split(",") if entry.strip()]


def parse_destination(text: str) -> tuple[str, int]:
    """``"host:port"`` becomes ``("host", port)``; a bad value raises ValueError at start-up."""
    host, separator, port = text.rpartition(":")
    if not separator or not host:
        raise ValueError(f"SCANNER_DENIED_DESTINATION must look like host:port, got {text!r}")
    return host, int(port)


def load_targets() -> tuple[list[Target], list[str]]:
    """The targets to try and the labels that could not be configured."""
    targets = [Target("metadata", METADATA_HOST, METADATA_PORT)]
    not_configured: list[str] = []

    appmux_port = os.environ.get("SCANNER_APPMUX_PORT")
    if appmux_port:
        targets.append(Target("appmux_loopback", "127.0.0.1", int(appmux_port)))
    else:
        not_configured.append("appmux_loopback")

    container_ip = os.environ.get("SCANNER_CONTAINER_IP")
    container_ports = os.environ.get("SCANNER_CONTAINER_PORTS")
    if container_ip and container_ports:
        targets.extend(
            Target("container_ip", container_ip, port) for port in parse_ports(container_ports)
        )
    else:
        not_configured.append("container_ip")

    denied = os.environ.get("SCANNER_DENIED_DESTINATION")
    if denied:
        denied_host, denied_port = parse_destination(denied)
        targets.append(Target("denied_destination", denied_host, denied_port))
    else:
        not_configured.append("denied_destination")
    return targets, not_configured


TARGETS, NOT_CONFIGURED = load_targets()


def try_connect(target: Target) -> dict[str, object]:
    started = time.monotonic()
    try:
        with socket.create_connection((target.host, target.port), timeout=CONNECT_TIMEOUT_S):
            result = CONNECTED
    except TimeoutError:
        result = "timeout"
    except OSError as exc:
        result = errno_name(exc)
    return {
        "label": target.label,
        "host": target.host,
        "port": target.port,
        "result": result,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def build_report() -> dict[str, object]:
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_CONNECTS) as pool:
        attempts = list(pool.map(try_connect, TARGETS))
    return {
        "fixture": "network_scanner",
        "attempts": attempts,
        "connected": [f"{a['host']}:{a['port']}" for a in attempts if a["result"] == CONNECTED],
        "not_configured": NOT_CONFIGURED,
        "process_id": os.getpid(),
        "started_at": STARTED_AT,
    }


@app.get("/")
def health() -> dict[str, str]:
    return {"fixture": "network_scanner"}


@app.get("/report")
def report() -> dict[str, object]:
    return build_report()


if __name__ == "__main__":
    uvicorn.run(
        app,
        # appmux and the platform both set UVICORN_HOST=0.0.0.0 (design 5.4). Run without it, the
        # hostile /report must not be open on every interface, so the default is loopback.
        host=os.environ.get("UVICORN_HOST", "127.0.0.1"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
