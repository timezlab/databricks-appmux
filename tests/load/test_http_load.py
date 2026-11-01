"""The load generator of the capacity run (T086), against a fake HTTP server on a local port.

These tests check the generator itself: that it paces, counts latency from the moment a request fell
due, reads every kind of response to its end, counts failures, refuses text that would let a caller
add a header, and reads a process's CPU from ``/proc``. They say nothing about the edge's capacity;
that is ``tests/integration/test_edge_capacity.py`` on a container. They are not part of the default
gate (``tests/unit`` and ``tests/contract``): run them with ``uv run pytest tests/load``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest
from http_load import (
    HEAD_END,
    _request_bytes,
    cpu_seconds_from_stat,
    percentile,
    process_cpu_seconds,
    run_load,
)

pytestmark = pytest.mark.anyio

HOST: Final = "127.0.0.1"
PATH: Final = "/sales/"
EMAIL: Final = "dana@example.com"
# A made-up token: the tests check that it reaches the wire and never the report.
TOKEN: Final = "fake-token-for-tests-0123456789"  # noqa: S105 - a placeholder, not a credential
OK_RESPONSE: Final = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
CHUNKED_RESPONSE: Final = (
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
)
SCRIPT: Final = Path(__file__).with_name("http_load.py")

Responder = Callable[[bytes], Awaitable[bytes]]


@dataclass
class FakeServer:
    port: int
    heads: list[bytes] = field(default_factory=list)
    connections: int = 0


async def answer_ok(head: bytes) -> bytes:
    return OK_RESPONSE


@asynccontextmanager
async def serve(
    responder: Responder = answer_ok, *, tls: ssl.SSLContext | None = None
) -> AsyncIterator[FakeServer]:
    server_state = FakeServer(port=0)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        server_state.connections += 1
        try:
            while True:
                head = await reader.readuntil(HEAD_END)
                server_state.heads.append(head)
                writer.write(await responder(head))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # the generator closed its end: nothing more to answer on this connection
        finally:
            writer.close()

    server = await asyncio.start_server(handle, HOST, 0, ssl=tls)
    server_state.port = server.sockets[0].getsockname()[1]
    try:
        yield server_state
    finally:
        server.close()
        await server.wait_closed()


def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


# --- pacing and the report -------------------------------------------------------------------


async def test_a_paced_run_sends_exactly_the_requests_that_fall_due_and_reports_them() -> None:
    async with serve() as server:
        report = await run_load(
            host=HOST,
            port=server.port,
            path=PATH,
            email=EMAIL,
            connections=4,
            rate_per_s=200,
            duration_s=1.0,
        )

    # 4 connections at 200 requests a second: one request every 20 ms on each, so 50 per connection
    # fall due inside one second, whatever the machine does with them.
    assert report.requests == 200 and len(server.heads) == 200
    assert server.connections == 4
    assert report.failures == 0 and report.error_rate == 0.0 and report.failure_kinds == []
    assert report.latency_ms is not None and report.latency_ms.p95 >= report.latency_ms.p50 >= 0
    assert report.per_second > 0 and report.cpu_seconds >= 0
    assert report.edge_cpu_seconds is None


async def test_the_request_names_the_person_and_nothing_secret() -> None:
    async with serve() as server:
        report = await run_load(
            host=HOST,
            port=server.port,
            path="/sales/a?b=1",
            email=EMAIL,
            connections=1,
            rate_per_s=20,
            duration_s=0.2,
        )

    head = server.heads[0].decode("latin-1")
    assert head.startswith("GET /sales/a?b=1 HTTP/1.1\r\n")
    assert f"X-Forwarded-Email: {EMAIL}\r\n" in head
    assert "token" not in head.lower() and "cookie" not in head.lower()
    assert EMAIL not in report.to_json()  # the report holds numbers, not people


async def test_latency_counts_from_when_the_request_fell_due_so_a_stall_shows() -> None:
    stalled = False

    async def stall_once(head: bytes) -> bytes:
        nonlocal stalled
        if not stalled:
            stalled = True
            await asyncio.sleep(0.5)
        return OK_RESPONSE

    async with serve(stall_once) as server:
        report = await run_load(
            host=HOST,
            port=server.port,
            path=PATH,
            email=EMAIL,
            connections=1,
            rate_per_s=100,
            duration_s=1.0,
        )

    # 100 requests fall due in the second; the 50 that fell due during the half-second stall were
    # sent late, and each is counted late (from about 500 ms down to about 10 ms). A generator
    # that timed each request from its own send would see one slow request out of 100, and a p95
    # near zero.
    assert report.requests == 100 and report.failures == 0 and report.latency_ms is not None
    assert report.latency_ms.p95 >= 300


async def test_a_chunked_answer_is_read_to_its_end_so_the_connection_is_reused() -> None:
    async def chunked(head: bytes) -> bytes:
        return CHUNKED_RESPONSE

    async with serve(chunked) as server:
        report = await run_load(
            host=HOST,
            port=server.port,
            path=PATH,
            email=EMAIL,
            connections=1,
            rate_per_s=50,
            duration_s=0.5,
        )

    assert report.requests == 25 and report.failures == 0
    assert server.connections == 1  # all 25 went over the one connection


# --- failures ---------------------------------------------------------------------------------


async def test_an_answer_that_is_not_200_is_a_failure_and_the_error_rate_says_so() -> None:
    async def broken(head: bytes) -> bytes:
        return b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n"

    async with serve(broken) as server:
        report = await run_load(
            host=HOST,
            port=server.port,
            path=PATH,
            email=EMAIL,
            connections=2,
            rate_per_s=40,
            duration_s=0.5,
        )

    assert report.requests == 20
    assert report.failures == 20 and report.error_rate == 1.0
    assert report.failure_kinds == ["status 500"]


async def test_a_server_that_hangs_up_mid_run_ends_that_connection_and_counts_one_failure() -> None:
    answered = 0

    async def answer_three_then_hang_up(head: bytes) -> bytes:
        nonlocal answered
        answered += 1
        if answered > 3:
            raise ConnectionResetError  # the fake server closes the connection without a reply
        return OK_RESPONSE

    async with serve(answer_three_then_hang_up) as server:
        report = await run_load(
            host=HOST,
            port=server.port,
            path=PATH,
            email=EMAIL,
            connections=1,
            rate_per_s=50,
            duration_s=0.5,
        )

    assert report.requests == 3 and report.failures == 1
    assert report.error_rate == pytest.approx(1 / 4)
    assert report.failure_kinds == ["IncompleteReadError"]


async def test_nothing_listening_is_a_total_failure_not_an_empty_pass() -> None:
    report = await run_load(
        host=HOST,
        port=unused_port(),
        path=PATH,
        email=EMAIL,
        connections=2,
        rate_per_s=20,
        duration_s=0.2,
    )

    assert report.requests == 0 and report.latency_ms is None
    assert report.failures == 2 and report.error_rate == 1.0
    assert report.failure_kinds == ["ConnectionRefusedError"]


# --- what the generator refuses ---------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"path": "/x\r\nX-Injected: 1"},
        {"path": "/has space"},
        {"path": "no-leading-slash"},
        {"email": "dana@example.com\r\nX-Injected: 1"},
        {"email": ""},
        {"connections": 0},
        {"rate_per_s": 0},
        {"duration_s": 0},
    ],
)
async def test_input_that_could_forge_a_request_or_make_no_run_is_refused(
    overrides: dict[str, Any],
) -> None:
    options: dict[str, Any] = {
        "host": HOST,
        "port": unused_port(),
        "path": PATH,
        "email": EMAIL,
        "duration_s": 0.2,
    }
    options.update(overrides)

    with pytest.raises(ValueError, match="must"):
        await run_load(**options)


# --- a bearer token over TLS (the deployed App's proxy) ----------------------------------------


async def test_a_bearer_token_replaces_the_forwarded_email_and_stays_out_of_the_report() -> None:
    async with serve() as server:
        report = await run_load(
            host=HOST,
            port=server.port,
            path=PATH,
            bearer_token=TOKEN,
            connections=1,
            rate_per_s=20,
            duration_s=0.2,
        )

    head = server.heads[0].decode("latin-1")
    assert f"Authorization: Bearer {TOKEN}\r\n" in head
    assert "x-forwarded-email" not in head.lower()
    assert report.requests > 0 and report.failures == 0
    assert TOKEN not in report.to_json()


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": None, "bearer_token": None},
        {"email": EMAIL, "bearer_token": TOKEN},
        {"email": None, "bearer_token": f"{TOKEN}\r\nX-Injected: 1"},
        {"email": None, "bearer_token": ""},
    ],
)
async def test_the_caller_names_itself_by_exactly_one_clean_credential(
    overrides: dict[str, Any],
) -> None:
    options: dict[str, Any] = {
        "host": HOST,
        "port": unused_port(),
        "path": PATH,
        "duration_s": 0.2,
    }
    options.update(overrides)

    with pytest.raises(ValueError, match="must"):
        await run_load(**options)


@pytest.fixture
def tls_pair(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """A server context and a client context that trusts its self-signed certificate."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("needs the openssl command to make a throwaway certificate")
    certificate, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={HOST}",
            "-addext",
            f"subjectAltName=IP:{HOST}",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    server_side = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_side.load_cert_chain(certificate, key)
    client_side = ssl.create_default_context(cafile=str(certificate))
    return server_side, client_side


async def test_a_tls_context_makes_the_run_speak_tls_and_check_the_certificate(
    tls_pair: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    server_side, client_side = tls_pair
    async with serve(tls=server_side) as server:
        report = await run_load(
            host=HOST,
            port=server.port,
            path=PATH,
            bearer_token=TOKEN,
            tls=client_side,
            connections=2,
            rate_per_s=40,
            duration_s=0.5,
        )
        untrusting = await run_load(
            host=HOST,
            port=server.port,
            path=PATH,
            bearer_token=TOKEN,
            tls=ssl.create_default_context(),  # does not know the throwaway certificate
            connections=1,
            rate_per_s=20,
            duration_s=0.2,
        )

    assert report.requests == 20 and report.failures == 0
    assert untrusting.requests == 0 and untrusting.error_rate == 1.0
    assert untrusting.failure_kinds == ["SSLCertVerificationError"]


def test_the_host_header_leaves_out_the_port_https_uses_by_default() -> None:
    # A browser or curl never writes ``Host: name:443``; the proxy in front of the deployed App
    # routes by that name, so the generator writes what they write.
    secure = _request_bytes("app.example.com", 443, PATH, None, TOKEN, secure=True).decode(
        "latin-1"
    )
    plain = _request_bytes("127.0.0.1", 8000, PATH, EMAIL, None, secure=False).decode("latin-1")

    assert "Host: app.example.com\r\n" in secure
    assert "Host: 127.0.0.1:8000\r\n" in plain


# --- CPU of the edge process ------------------------------------------------------------------


def test_cpu_is_read_from_a_stat_line_whose_command_holds_spaces_and_parentheses() -> None:
    # Fields: 1 pid, 2 (command), 3 state, 4..13 ten fields, 14 utime, 15 stime.
    line = "4242 (my (odd) cmd) S " + " ".join(["0"] * 10) + " 250 150 0 0 20 0 1 0 12345"

    assert cpu_seconds_from_stat(line, ticks_per_second=100) == pytest.approx(4.0)


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs a Linux /proc")
def test_a_running_process_has_a_cpu_reading_and_a_missing_one_says_what_to_do() -> None:
    assert process_cpu_seconds(os.getpid()) >= 0
    with pytest.raises(ProcessLookupError, match="give the pid of the edge"):
        process_cpu_seconds(2**31 - 1)


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs a Linux /proc")
async def test_the_run_reports_the_cpu_the_edge_process_used_while_it_lasted() -> None:
    async with serve() as server:
        report = await run_load(
            host=HOST,
            port=server.port,
            path=PATH,
            email=EMAIL,
            connections=2,
            rate_per_s=100,
            duration_s=0.5,
            edge_pid=os.getpid(),  # this process hosts the fake server
        )

    assert report.edge_cpu_seconds is not None and report.edge_cpu_seconds >= 0
    assert report.edge_cpu_percent_of_one_core is not None


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs a Linux /proc")
async def test_a_missing_edge_process_stops_the_run_before_any_load_is_sent() -> None:
    async with serve() as server:
        with pytest.raises(ProcessLookupError):
            await run_load(
                host=HOST,
                port=server.port,
                path=PATH,
                email=EMAIL,
                duration_s=0.2,
                edge_pid=2**31 - 1,
            )

    assert server.heads == []


# --- the command line -------------------------------------------------------------------------


def test_percentile_picks_the_value_at_the_fraction_of_a_sorted_list() -> None:
    values = [float(n) for n in range(1, 101)]

    assert percentile(values, 0.5) == 51.0
    assert percentile(values, 0.95) == 96.0
    assert percentile([7.0], 0.95) == 7.0


async def test_the_command_line_prints_one_json_line_with_the_numbers_the_run_needs() -> None:
    async with serve() as server:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(SCRIPT),
            "--host",
            HOST,
            "--port",
            str(server.port),
            "--path",
            PATH,
            "--email",
            EMAIL,
            "--connections",
            "2",
            "--rate",
            "40",
            "--duration",
            "0.5",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

    assert process.returncode == 0, stderr.decode()
    lines = stdout.decode().splitlines()
    assert len(lines) == 1
    report = json.loads(lines[0])
    assert report["requests"] == 20 and report["error_rate"] == 0.0
    assert report["latency_ms"]["p95"] >= 0
    assert report["target_rate_per_s"] == 40
    assert EMAIL not in lines[0]
