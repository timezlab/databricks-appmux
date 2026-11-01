"""Fixture bundle_offer: a snapshot whose ``databricks.yml`` declares resources.

``databricks.yml`` lists six resources under ``resources.apps.bundle_offer.resources``. At Update
appmux reads that list and stores one **offer** per entry (design 5.4, spec US6); reading the file
attaches nothing. The expected offers and warnings are written at the top of ``databricks.yml``.
What a test checks with this fixture:

    after Update      six offers in the manifest (``bundle_resources``), zero rows in
                      ``app_resources``; the unresolved ``${var.endpoint}`` is flagged and the
                      ``IS_OWNER`` permission raises a warning; ``variables``, ``targets``,
                      ``config``, ``permissions`` and ``user_api_scopes`` raise ignore warnings.
    before attaching  the release fails before any build with ``missing resources:`` followed by
                      the three keys ``experiment``, ``openai-api-key`` and ``sql-warehouse``; the
                      alphabetical order written in ``app.yaml`` is this fixture's own choice, not
                      a design rule, so a test compares the keys as a set.
    after attaching   the three resources ``app.yaml`` names and pressing Activate, the child
                      starts and ``/report`` shows them set.

It is a standalone Databricks-style app: ``python app.py`` and the port in ``DATABRICKS_APP_PORT``.

HTTP paths
    GET /          200 ``{"fixture": "bundle_offer", "status": "ok"}``.
    GET /report    200 JSON ``{"fixture": "bundle_offer", "env": {"LOG_LEVEL": "<value or unset>",
                   "OPENAI_API_KEY": "set|unset", "WAREHOUSE_ID": "set|unset",
                   "MLFLOW_EXPERIMENT_ID": "set|unset"}}``.

``/report`` never returns a resolved ``valueFrom`` value, because one of them is a secret: it says
only whether the variable reached the child. ``LOG_LEVEL`` is a literal in ``app.yaml`` (the
``debug`` in the bundle's ``config`` is ignored) and is returned as it is, so it reads ``info``.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

LITERAL_ENV_NAMES = ("LOG_LEVEL",)
RESOURCE_ENV_NAMES = ("OPENAI_API_KEY", "WAREHOUSE_ID", "MLFLOW_EXPERIMENT_ID")
UNSET = "unset"
SET = "set"

app = FastAPI()


@app.get("/")
def index() -> dict[str, str]:
    return {"fixture": "bundle_offer", "status": "ok"}


@app.get("/report")
def report() -> dict[str, object]:
    env: dict[str, str] = {name: os.environ.get(name, UNSET) for name in LITERAL_ENV_NAMES}
    env.update({name: SET if name in os.environ else UNSET for name in RESOURCE_ENV_NAMES})
    return {"fixture": "bundle_offer", "env": env}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
