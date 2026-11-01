"""Shared pytest configuration: the integration gate and the anyio backend."""

from __future__ import annotations

import os

import pytest

CONTAINER_ENVIRONMENT_VARIABLE = "APPMUX_TEST_CONTAINER"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every ``integration`` test unless it runs on a Databricks container."""
    if os.environ.get(CONTAINER_ENVIRONMENT_VARIABLE) == "1":
        return
    skip_integration = pytest.mark.skip(
        reason=f"needs a Databricks container; set {CONTAINER_ENVIRONMENT_VARIABLE}=1 there"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


@pytest.fixture
def anyio_backend() -> str:
    """The anyio pytest plugin (brought by Starlette) runs async tests on asyncio only."""
    return "asyncio"
