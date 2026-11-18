"""MLflow experiments in the person's home folder are offered beside the search (owner's report).

An App's token cannot hold ``mlflow``, so the experiment search is read with appmux's own token and
showed only what appmux may read (``/Shared``), not an experiment the person made from the
Experiments tab, which lands in ``/Users/<email>`` (2026-11-18). The person's own token can list
that folder (``GET /api/2.0/workspace/list``, the call the Deploy reads a folder with), so its
``MLFLOW_EXPERIMENT`` objects are added to whatever the search gave.

All names and ids are placeholders; both tokens are fake values.
"""

from __future__ import annotations

from typing import Final

import httpx
from edge_harness import ALICE, VISITOR_TOKEN
from suggestion_harness import (
    PLATFORM_TOKEN,
    Answer,
    PlatformTokenProvider,
    Suggester,
    Workspace,
    labels_of,
    targets_of,
)

SEARCH: Final = ("POST", "/api/2.0/mlflow/experiments/search")
WORKSPACE_LIST: Final = ("GET", "/api/2.0/workspace/list")
SCOPE_REFUSAL: Final = {"error_code": "X", "message": "Invalid scope, required scopes: mlflow"}
HOME: Final = f"/Users/{ALICE}"
SHARED_ONLY: Final = {"experiments": [{"experiment_id": "7", "name": "/Shared/forecast"}]}
HOME_LISTING: Final = {
    "objects": [
        {
            "path": f"{HOME}/churn",
            "object_type": "MLFLOW_EXPERIMENT",
            "object_id": 4242,
            "resource_id": "4242",
        },
        {"path": f"{HOME}/legacy", "object_type": "MLFLOW_EXPERIMENT", "object_id": 99},
        {"path": f"{HOME}/analysis", "object_type": "NOTEBOOK", "object_id": 5},
        {"path": f"{HOME}/sales", "object_type": "DIRECTORY", "object_id": 6},
    ]
}


def search_for_platform_only(request: httpx.Request) -> httpx.Response:
    if request.headers["Authorization"] == f"Bearer {PLATFORM_TOKEN}":
        return httpx.Response(200, json=SHARED_ONLY)
    return httpx.Response(403, json=SCOPE_REFUSAL)


def home_for_the_person(request: httpx.Request) -> httpx.Response:
    if request.headers["Authorization"] != f"Bearer {VISITOR_TOKEN}":
        return httpx.Response(403, json={"error_code": "PERMISSION_DENIED"})
    if request.url.params.get("path") != HOME:
        return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})
    return httpx.Response(200, json=HOME_LISTING)


def suggester(
    search: Answer = search_for_platform_only, home: Answer = home_for_the_person
) -> Suggester:
    workspace = Workspace({SEARCH: search, WORKSPACE_LIST: home})
    return Suggester(workspace, platform_token=PlatformTokenProvider())


def test_the_persons_own_experiments_are_listed_beside_what_appmux_can_read() -> None:
    response = suggester().suggest("mlflow_experiment")

    body = response.json()
    assert targets_of(response) == ["7", "4242", "99"]
    assert labels_of(response) == ["/Shared/forecast", f"{HOME}/churn", f"{HOME}/legacy"]
    assert body["source"] == "platform_token"
    assert "home folder" in body["note"]


def test_the_home_folder_is_read_with_the_persons_token_and_names_only_their_home() -> None:
    made = suggester()

    made.suggest("mlflow_experiment")

    [listing] = [r for r in made.workspace.requests if r.url.path == WORKSPACE_LIST[1]]
    assert listing.headers["Authorization"] == f"Bearer {VISITOR_TOKEN}"
    assert dict(listing.url.params) == {"path": HOME}


def test_the_query_filters_the_home_experiments_too() -> None:
    response = suggester().suggest("mlflow_experiment", "CHURN")

    assert targets_of(response) == ["4242"]


def test_an_experiment_both_lists_hold_is_offered_once() -> None:
    both = {"experiments": [{"experiment_id": "4242", "name": f"{HOME}/churn"}]}
    response = suggester(search=(200, both)).suggest("mlflow_experiment")

    assert targets_of(response) == ["4242", "99"]


def test_when_the_search_cannot_be_read_at_all_the_home_experiments_are_still_offered() -> None:
    nobody = (403, SCOPE_REFUSAL)
    response = suggester(search=nobody).suggest("mlflow_experiment")

    body = response.json()
    assert targets_of(response) == ["4242", "99"]
    assert body["source"] == "caller_token"
    assert "home folder" in body["note"]


def test_a_home_folder_that_cannot_be_read_leaves_the_search_as_it_was() -> None:
    response = suggester(home=(403, {"error_code": "PERMISSION_DENIED"})).suggest(
        "mlflow_experiment"
    )

    body = response.json()
    assert targets_of(response) == ["7"]
    assert body["source"] == "platform_token"
    assert body["note"] is None


def test_without_the_persons_token_the_home_folder_is_not_read() -> None:
    made = suggester()

    made.suggest("mlflow_experiment", headers={"X-Forwarded-Access-Token": ""})

    assert WORKSPACE_LIST not in made.workspace.calls


def test_no_other_type_reads_the_home_folder() -> None:
    made = Suggester(Workspace({("GET", "/api/2.2/jobs/list"): (200, {"jobs": []})}))

    made.suggest("job")

    assert WORKSPACE_LIST not in made.workspace.calls
