"""Offline-install fixture newer_wheel: needs a newer version of a package the environment has.

The App environment holds ``appdirs`` 1.4.4. ``requirements.txt`` pins ``appdirs==99.0.0`` with the
sha256 of a stand-in wheel that ``make_wheels.py`` builds offline, so the Update must install that
wheel into the release's overlay and the child must import it instead of the environment's copy
(design 5.3, spec US7 scenario 2). The wheel is not the real ``appdirs``; its module holds only a
version and a marker. It is a standalone Databricks-style app: ``python app.py`` and the port in
``DATABRICKS_APP_PORT``.

HTTP paths
    GET /          200 ``{"fixture": "newer_wheel", "status": "ok"}``.
    GET /report    200 JSON ``{"fixture": "newer_wheel", "python": "<x.y.z>",
                   "pythonpath": "<PYTHONPATH or empty>", "imported": {...}, "copies": [...]}``.

``imported`` describes the copy the child actually imported:
``{"version": "<installed version>", "module_version": "<__version__>",
"marker": "<MARKER or null>", "file": "<path of the module>"}``, or
``{"error": "<exception class>"}`` when the import fails.
The wheel won when ``version`` is ``"99.0.0"``, ``marker`` is ``"appmux-fixture-newer-wheel"`` and
``file`` lies in the overlay named by ``pythonpath``.

``copies`` lists every installed ``appdirs`` found on ``sys.path`` in search order, each
``{"version": "<version>", "location": "<directory it sits in>"}``. With the overlay first it reads
99.0.0 then 1.4.4; a release without the wheel shows only the environment's copy.
"""

from __future__ import annotations

import importlib
import os
import sys
from importlib import metadata

import uvicorn
from fastapi import FastAPI

DISTRIBUTION = "appdirs"

app = FastAPI()


def describe_imported_copy() -> dict[str, str | None]:
    try:
        module = importlib.import_module(DISTRIBUTION)
        return {
            "version": metadata.version(DISTRIBUTION),
            "module_version": getattr(module, "__version__", None),
            "marker": getattr(module, "MARKER", None),
            "file": module.__file__,
        }
    except (ImportError, metadata.PackageNotFoundError) as exc:
        return {"error": type(exc).__name__}


def list_installed_copies() -> list[dict[str, str]]:
    return [
        {"version": copy.version, "location": str(copy.locate_file(""))}
        for copy in metadata.distributions(name=DISTRIBUTION)
    ]


@app.get("/")
def index() -> dict[str, str]:
    return {"fixture": "newer_wheel", "status": "ok"}


@app.get("/report")
def report() -> dict[str, object]:
    return {
        "fixture": "newer_wheel",
        "python": sys.version.split()[0],
        "pythonpath": os.environ.get("PYTHONPATH", ""),
        "imported": describe_imported_copy(),
        "copies": list_installed_copies(),
    }


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
