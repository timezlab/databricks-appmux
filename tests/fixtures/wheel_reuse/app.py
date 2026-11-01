"""Offline-install fixture wheel_reuse: needs a wheel that another app already put in the store.

``requirements.txt`` pins ``appmux-fixture-alpha`` with the same sha256 as the wheels_only fixture.
After wheels_only has been registered, the content store already holds that wheel by digest, so
updating this fixture must reuse it instead of fetching it again (design 5.3, spec US7 scenario 4).
``make_wheels.py`` builds the wheel, for a test that also offers it in a wheel source. It is a
standalone Databricks-style app: ``python app.py`` and the port in ``DATABRICKS_APP_PORT``.

HTTP paths
    GET /          200 ``{"fixture": "wheel_reuse", "status": "ok"}``.
    GET /report    200 JSON ``{"fixture": "wheel_reuse", "python": "<x.y.z>",
                   "pythonpath": "<PYTHONPATH or empty>", "packages": {...}}``.

``packages`` has one entry, keyed ``appmux-fixture-alpha``: ``{"version": "<installed version>",
"file": "<path of the module>", "value": "<VALUE of the module>"}`` when the import works and
``{"error": "<exception class>"}`` when it does not. The expected ``value`` is
``"appmux-fixture-alpha 1.0.0"``; ``file`` lies in the release's overlay, which ``pythonpath``
names.
"""

from __future__ import annotations

import importlib
import os
import sys
from importlib import metadata

import uvicorn
from fastapi import FastAPI

# distribution name -> module it installs
REPORTED_MODULES = {"appmux-fixture-alpha": "appmux_fixture_alpha"}

app = FastAPI()


def describe_module(distribution: str, module_name: str) -> dict[str, str | None]:
    try:
        module = importlib.import_module(module_name)
        return {
            "version": metadata.version(distribution),
            "file": module.__file__,
            "value": module.VALUE,
        }
    except (ImportError, AttributeError, metadata.PackageNotFoundError) as exc:
        return {"error": type(exc).__name__}


@app.get("/")
def index() -> dict[str, str]:
    return {"fixture": "wheel_reuse", "status": "ok"}


@app.get("/report")
def report() -> dict[str, object]:
    return {
        "fixture": "wheel_reuse",
        "python": sys.version.split()[0],
        "pythonpath": os.environ.get("PYTHONPATH", ""),
        "packages": {
            distribution: describe_module(distribution, module_name)
            for distribution, module_name in REPORTED_MODULES.items()
        },
    }


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
