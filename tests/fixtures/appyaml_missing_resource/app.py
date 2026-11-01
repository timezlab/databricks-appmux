"""Fixture appyaml_missing_resource: ``app.yaml`` asks for resources the app does not declare.

``app.yaml`` has two ``valueFrom`` keys, ``openai-api-key`` and ``sql-warehouse``, and the app
declares no resource for either (design 5.4, spec US6). The Update succeeds, because the file is
valid, but a release of the snapshot must fail in the reconcile **before any build**, naming every
missing key at once::

    missing resources: openai-api-key, sql-warehouse

A standalone deploy would only log an error and start without the variables (spike 17); appmux
refuses to start a child that would run without a secret it asked for. After the two resources are
attached and the release is activated, the child starts and ``/report`` shows both variables set.
It is a standalone Databricks-style app: ``python app.py`` and the port in ``DATABRICKS_APP_PORT``.

HTTP paths
    GET /          200 ``{"fixture": "appyaml_missing_resource", "status": "ok"}``.
    GET /report    200 JSON ``{"fixture": "appyaml_missing_resource",
                   "env": {"LOG_LEVEL": "<value or unset>", "OPENAI_API_KEY": "set|unset",
                   "WAREHOUSE_ID": "set|unset"}}``.

``/report`` never returns a resolved ``valueFrom`` value, because one of them is a secret: it says
only whether the variable reached the child (``"set"``) or not (``"unset"``). ``LOG_LEVEL`` is a
literal ``value`` in ``app.yaml`` and is returned as it is.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

LITERAL_ENV_NAMES = ("LOG_LEVEL",)
RESOURCE_ENV_NAMES = ("OPENAI_API_KEY", "WAREHOUSE_ID")
UNSET = "unset"
SET = "set"

app = FastAPI()


@app.get("/")
def index() -> dict[str, str]:
    return {"fixture": "appyaml_missing_resource", "status": "ok"}


@app.get("/report")
def report() -> dict[str, object]:
    env: dict[str, str] = {name: os.environ.get(name, UNSET) for name in LITERAL_ENV_NAMES}
    env.update({name: SET if name in os.environ else UNSET for name in RESOURCE_ENV_NAMES})
    return {"fixture": "appyaml_missing_resource", "env": env}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
