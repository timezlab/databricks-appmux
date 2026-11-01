"""The health clock (T077, design 5.5): poll once a second from process start, below 500 is up.

The clock and the probe are fakes, so a 60-second deadline takes no time. The default probe is
tried once against a real unix socket served in this process, because "connection refused" and
"answered 503" are what the child's relay does, and a fake cannot prove the mapping.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from reconciler_fakes import FakeClock, FakeProbe

from appmux.reconciler.health import (
    HEALTH_DEADLINE_MAX_S,
    HEALTH_DEADLINE_MIN_S,
    HEALTH_POLL_INTERVAL_S,
    PROBE_TIMEOUT_S,
    ProbeFailed,
    UnixSocketHealthProbe,
    wait_until_healthy,
)
from appmux.sandbox.relay import bind_relay_socket, serve_relay

pytestmark = pytest.mark.anyio

SOCKET = Path("/run/appmux-example/sales-1.sock")


async def wait(
    probe: FakeProbe, clock: FakeClock, *, deadline_s: int = 60, health_path: str = "/"
) -> tuple[bool, str | None, float]:
    result = await wait_until_healthy(
        probe, socket_path=SOCKET, health_path=health_path, deadline_s=deadline_s, clock=clock
    )
    return result.is_healthy, result.reason, result.elapsed_s


# --- the schedule -------------------------------------------------------------------------------


async def test_it_polls_once_a_second_from_the_start_and_is_healthy_at_the_first_good_answer() -> (
    None
):
    clock = FakeClock()
    probe = FakeProbe(clock, lambda t: 200 if t >= 23 else "connection refused")

    is_healthy, reason, elapsed_s = await wait(probe, clock)

    assert (is_healthy, reason) == (True, None)
    assert elapsed_s == 23
    assert probe.times == [float(second) for second in range(24)]
    assert HEALTH_POLL_INTERVAL_S == 1.0


async def test_the_first_poll_is_at_the_moment_the_process_started() -> None:
    clock = FakeClock(now=1000.0)
    probe = FakeProbe(clock, 200)

    await wait(probe, clock)

    assert probe.times == [1000.0]
    assert clock.sleeps == []


async def test_a_slow_probe_does_not_push_the_next_poll_a_whole_second_later() -> None:
    clock = FakeClock()
    probe = FakeProbe(clock, "timed out", cost_s=2.5)

    await wait(probe, clock, deadline_s=10)

    # Polls stay on the second: a probe that took 2.5 s is followed by the poll due at t=3.
    assert probe.times[:4] == [0.0, 3.0, 6.0, 9.0]


async def test_the_probe_gets_the_health_path_and_the_childs_socket() -> None:
    clock = FakeClock()
    probe = FakeProbe(clock, 200)

    await wait(probe, clock, health_path="/healthz")

    assert probe.targets == [(SOCKET, "/healthz")]
    assert probe.timeouts == [PROBE_TIMEOUT_S]


# --- what counts as healthy -----------------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 204, 302, 401, 404, 499])
async def test_any_status_below_500_is_healthy(status: int) -> None:
    clock = FakeClock()

    is_healthy, _, elapsed_s = await wait(FakeProbe(clock, status), clock)

    assert is_healthy
    assert elapsed_s == 0


@pytest.mark.parametrize("status", [500, 502, 503, 599])
async def test_a_status_of_500_or_more_is_not_healthy(status: int) -> None:
    clock = FakeClock()

    is_healthy, reason, _ = await wait(FakeProbe(clock, status), clock, deadline_s=5)

    assert not is_healthy
    assert reason == f"health check did not pass within 5 s (last error: status {status})"


# --- the deadline and the reason ------------------------------------------------------------------


async def test_the_reason_after_sixty_seconds_of_refusals_is_the_one_the_design_quotes() -> None:
    clock = FakeClock()
    probe = FakeProbe(clock, "connection refused")

    is_healthy, reason, elapsed_s = await wait(probe, clock, deadline_s=60)

    assert not is_healthy
    assert reason == "health check did not pass within 60 s (last error: connection refused)"
    assert elapsed_s == 60
    # t=0 .. t=60: the poll at the deadline is the last one.
    assert probe.times == [float(second) for second in range(61)]


async def test_a_child_that_turns_healthy_at_the_deadline_still_passes() -> None:
    clock = FakeClock()
    probe = FakeProbe(clock, lambda t: 200 if t >= 60 else "connection refused")

    is_healthy, _, elapsed_s = await wait(probe, clock, deadline_s=60)

    assert is_healthy
    assert elapsed_s == 60


async def test_the_reason_names_the_latest_error_not_the_first() -> None:
    clock = FakeClock()
    probe = FakeProbe(clock, lambda t: "connection refused" if t < 3 else 503)

    _, reason, _ = await wait(probe, clock, deadline_s=5)

    assert reason == "health check did not pass within 5 s (last error: status 503)"


@pytest.mark.parametrize("deadline_s", [HEALTH_DEADLINE_MIN_S, HEALTH_DEADLINE_MAX_S, 60])
async def test_the_deadline_bounds_are_five_and_six_hundred_seconds(deadline_s: int) -> None:
    clock = FakeClock()

    _, _, elapsed_s = await wait(
        FakeProbe(clock, "connection refused"), clock, deadline_s=deadline_s
    )

    assert (HEALTH_DEADLINE_MIN_S, HEALTH_DEADLINE_MAX_S) == (5, 600)
    assert elapsed_s == deadline_s


@pytest.mark.parametrize("deadline_s", [4, 601, 0, -1])
async def test_a_deadline_outside_the_bounds_is_refused_before_any_poll(deadline_s: int) -> None:
    clock = FakeClock()
    probe = FakeProbe(clock, 200)

    with pytest.raises(ValueError, match="between 5 and 600"):
        await wait(probe, clock, deadline_s=deadline_s)

    assert probe.times == []


async def test_cancelling_the_wait_stops_it_without_a_result() -> None:
    clock = FakeClock()
    probe = FakeProbe(clock, "connection refused")
    task = asyncio.create_task(wait(probe, clock, deadline_s=600))
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- the default probe, on a real unix socket -----------------------------------------------------


@contextlib.asynccontextmanager
async def serve(path: Path, reply: bytes | None) -> AsyncIterator[None]:
    """A unix-socket HTTP server that answers ``reply`` to a request, or never answers if None."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            if reply is None:
                await asyncio.sleep(3600)
            else:
                writer.write(reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle, path=str(path))
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()


def http_reply(status: int) -> bytes:
    return f"HTTP/1.1 {status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()


async def test_the_default_probe_reads_the_status_the_child_answers(tmp_path: Path) -> None:
    socket_path = tmp_path / "child.sock"
    probe = UnixSocketHealthProbe()
    try:
        async with serve(socket_path, http_reply(404)):
            assert await probe.probe(socket_path, "/missing", timeout_s=2.0) == 404
        async with serve(socket_path, http_reply(503)):
            assert await probe.probe(socket_path, "/", timeout_s=2.0) == 503
    finally:
        await probe.aclose()


async def test_the_default_probe_reports_a_socket_nobody_listens_on_as_refused(
    tmp_path: Path,
) -> None:
    probe = UnixSocketHealthProbe()
    try:
        with pytest.raises(ProbeFailed) as missing:
            await probe.probe(tmp_path / "absent.sock", "/", timeout_s=1.0)
    finally:
        await probe.aclose()

    assert str(missing.value) == "connection refused"


async def test_the_default_probe_gives_up_on_a_child_that_never_answers(tmp_path: Path) -> None:
    socket_path = tmp_path / "child.sock"
    probe = UnixSocketHealthProbe()
    try:
        async with serve(socket_path, None):
            with pytest.raises(ProbeFailed) as hung:
                await probe.probe(socket_path, "/", timeout_s=0.2)
    finally:
        await probe.aclose()

    assert str(hung.value) == "timed out"


async def test_the_default_probe_reports_a_child_that_hangs_up_after_the_request_as_closed(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "child.sock"
    probe = UnixSocketHealthProbe()
    try:
        async with serve(socket_path, b""):
            with pytest.raises(ProbeFailed) as hung_up:
                await probe.probe(socket_path, "/", timeout_s=2.0)
    finally:
        await probe.aclose()

    assert str(hung_up.value) == "connection closed before an answer"


# --- the default probe through the child's real relay ---------------------------------------------
#
# In the container the child is never reached on a unix socket of its own: sandbox/relay.py binds
# the socket, and listens on it only once the child has bound its port, so the socket refuses until
# then. A relay that listened first and hung up while the port refused (the first integration run,
# T096, slow_start) made the reason read "connection closed before an answer".


def unused_loopback_port() -> int:
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        return int(probe_socket.getsockname()[1])


@contextlib.asynccontextmanager
async def relay_to(socket_path: Path, child_port: int) -> AsyncIterator[None]:
    # Bound like the launcher binds it: not listening until the child's port is up.
    relay = asyncio.create_task(
        serve_relay(bind_relay_socket(socket_path, is_listening=False), child_port)
    )
    try:
        yield
    finally:
        relay.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay


async def wait_until_relay_accepts(socket_path: Path) -> None:
    async with asyncio.timeout(5.0):
        while True:
            try:
                _, writer = await asyncio.open_unix_connection(str(socket_path))
            except ConnectionRefusedError:
                await asyncio.sleep(0.01)
                continue
            writer.close()
            return


async def test_the_default_probe_reports_a_port_nobody_listens_on_as_refused_through_the_relay(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "child.sock"
    probe = UnixSocketHealthProbe()
    try:
        async with relay_to(socket_path, unused_loopback_port()):
            with pytest.raises(ProbeFailed) as unbound:
                await probe.probe(socket_path, "/", timeout_s=2.0)
    finally:
        await probe.aclose()

    assert str(unbound.value) == "connection refused"


async def test_the_default_probe_reads_the_status_through_the_relay_once_the_child_listens(
    tmp_path: Path,
) -> None:
    async def child(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            writer.close()  # the relay's own try of the port: it connects and sends nothing
            return
        writer.write(http_reply(302))
        await writer.drain()
        writer.close()

    child_server = await asyncio.start_server(child, "127.0.0.1", 0)
    child_port = int(child_server.sockets[0].getsockname()[1])
    socket_path = tmp_path / "child.sock"
    probe = UnixSocketHealthProbe()
    try:
        async with child_server, relay_to(socket_path, child_port):
            await wait_until_relay_accepts(socket_path)
            assert await probe.probe(socket_path, "/", timeout_s=2.0) == 302
    finally:
        await probe.aclose()
