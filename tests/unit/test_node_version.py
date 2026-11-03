"""``NodeVersion``: the platform's ``node --version``, asked once, without a shell (T191).

Each test points it at a small script in ``tmp_path`` standing in for ``node``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from appmux.admin.environment import NodeVersion

pytestmark = pytest.mark.anyio


def fake_node(tmp_path: Path, body: str) -> str:
    script = tmp_path / "node"
    script.write_text(f"#!/bin/sh\necho asked >> {tmp_path / 'calls'}\n{body}\n")
    script.chmod(0o755)
    return str(script)


def calls(tmp_path: Path) -> int:
    path = tmp_path / "calls"
    return len(path.read_text().splitlines()) if path.exists() else 0


async def test_the_version_is_read_without_its_v_and_asked_only_once(tmp_path: Path) -> None:
    node = NodeVersion(executable=fake_node(tmp_path, 'echo "$1" >&2; echo v22.22.1'))

    answers = await asyncio.gather(node(), node(), node())

    assert answers == ["22.22.1"] * 3
    assert calls(tmp_path) == 1


async def test_a_node_that_fails_or_prints_no_version_gives_none(tmp_path: Path) -> None:
    failing = NodeVersion(executable=fake_node(tmp_path, "echo v22.0.0; exit 1"))
    chatty = NodeVersion(executable=fake_node(tmp_path, "echo 'hello; rm -rf /'"))

    assert await failing() is None
    assert await chatty() is None


async def test_a_node_that_does_not_answer_in_time_gives_none(tmp_path: Path) -> None:
    node = NodeVersion(executable=fake_node(tmp_path, "exec sleep 5"), timeout_s=0.2)

    assert await node() is None


async def test_no_node_gives_none(tmp_path: Path) -> None:
    node = NodeVersion(executable=str(tmp_path / "no-such-node"))

    assert await node() is None
