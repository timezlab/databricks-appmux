"""Offline-install fixture wheels_only: its extra dependencies exist only as wheel files.

``requirements.txt`` pins two packages the App environment does not hold,
``appmux-fixture-alpha`` and ``appmux-fixture-beta`` (beta needs alpha), each with the sha256 of a
wheel that ``make_wheels.py`` builds offline. Build them into the ``wheels/`` folder of a copy of
this fixture; with the package index unreachable the release can only start if the Update finds the
wheels in that folder (design 5.3, spec US7). It is a standalone Databricks-style app:
``python app.py`` and the port in ``DATABRICKS_APP_PORT``.

HTTP paths
    GET /          200 ``{"fixture": "wheels_only", "status": "ok"}``.
    GET /report    200 JSON ``{"fixture": "wheels_only", "python": "<x.y.z>",
                   "pythonpath": "<PYTHONPATH or empty>", "packages": {...}}``.

``packages`` has one entry per module, keyed by distribution name. An entry is
``{"version": "<installed version>", "file": "<path of the module>", "value": "<VALUE of the
module>"}`` when the import works and ``{"error": "<exception class>"}`` when it does not. The
expected ``value`` is ``"appmux-fixture-alpha 1.0.0"`` and
``"appmux-fixture-beta 1.0.0 on appmux-fixture-alpha 1.0.0"``; ``file`` lies in the release's
overlay, which ``pythonpath`` names.
"""

from __future__ import annotations

import importlib
import os
import sys
from importlib import metadata

import uvicorn
from fastapi import FastAPI

# distribution name -> module it installs
REPORTED_MODULES = {
    "appmux-fixture-alpha": "appmux_fixture_alpha",
    "appmux-fixture-beta": "appmux_fixture_beta",
}

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
    return {"fixture": "wheels_only", "status": "ok"}


@app.get("/report")
def report() -> dict[str, object]:
    return {
        "fixture": "wheels_only",
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
