"""Fixture appyaml_invalid: a snapshot whose app.yaml contract is broken in two ways.

The snapshot holds **both** ``app.yaml`` and ``app.yml``, and ``app.yml`` has an ``env`` entry,
``GREETING``, with both ``value`` and ``valueFrom``. Update must refuse it (design 5.4, spec US6):
no release is created and the refusal names the problem. Each fault can be seen alone by deleting
a file from a copy of the fixture:

    as it is                    refused: both app.yaml and app.yml are present.
    app.yaml deleted            refused: env entry ``GREETING`` has ``value`` and ``valueFrom``.
    app.yml deleted             valid: nothing to refuse; the app below starts normally.

The app itself is only there so the last case has something to run; with the contract broken it
never starts under appmux. It is a standalone Databricks-style app: ``python app.py`` and the port
in ``DATABRICKS_APP_PORT``.

HTTP paths
    GET /          200 ``{"fixture": "appyaml_invalid", "status": "ok"}``.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def index() -> dict[str, str]:
    return {"fixture": "appyaml_invalid", "status": "ok"}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
