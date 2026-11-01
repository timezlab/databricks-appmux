"""Offline-install fixture env_covered: every requirement is met by the App environment.

``requirements.txt`` names only packages the Free Edition App environment already holds at a version
that satisfies the line, so the Update must fetch no wheel, build no overlay and start the child on
the environment alone (design 5.3, spec US7 scenario 1). ``make_wheels.py`` builds one decoy wheel
that no requirement names, so a test can offer a non-empty wheel source and assert nothing was
fetched from it. It is a standalone Databricks-style app: ``python app.py`` and the port in
``DATABRICKS_APP_PORT``.

HTTP paths
    GET /          200 ``{"fixture": "env_covered", "status": "ok"}``.
    GET /report    200 JSON ``{"fixture": "env_covered", "python": "<x.y.z>",
                   "pythonpath": "<PYTHONPATH or empty>", "packages": {...}, "decoy": {...}}``.

``packages`` has one entry per requirement, keyed by distribution name:
``{"version": "<installed version>", "file": "<path of the module>"}`` when the import works and
``{"error": "<exception class>"}`` when it does not. With no overlay every ``file`` lies in the App
environment and ``pythonpath`` is empty.

``decoy`` is the result of importing ``appmux_fixture_unneeded``, the decoy wheel's module, in the
same shape. It must be ``{"error": "ModuleNotFoundError"}``: the wheel was offered but no
requirement asked for it.
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
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "starlette": "starlette",
    "pydantic": "pydantic",
    "appdirs": "appdirs",
}
DECOY_DISTRIBUTION = "appmux-fixture-unneeded"
DECOY_MODULE = "appmux_fixture_unneeded"

app = FastAPI()


def describe_module(distribution: str, module_name: str) -> dict[str, str | None]:
    try:
        module = importlib.import_module(module_name)
        return {"version": metadata.version(distribution), "file": module.__file__}
    except (ImportError, metadata.PackageNotFoundError) as exc:
        return {"error": type(exc).__name__}


@app.get("/")
def index() -> dict[str, str]:
    return {"fixture": "env_covered", "status": "ok"}


@app.get("/report")
def report() -> dict[str, object]:
    return {
        "fixture": "env_covered",
        "python": sys.version.split()[0],
        "pythonpath": os.environ.get("PYTHONPATH", ""),
        "packages": {
            distribution: describe_module(distribution, module_name)
            for distribution, module_name in REPORTED_MODULES.items()
        },
        "decoy": describe_module(DECOY_DISTRIBUTION, DECOY_MODULE),
    }


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
