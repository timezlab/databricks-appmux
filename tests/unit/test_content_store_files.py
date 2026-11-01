"""The content store's files as the daily clean-up sees them (T116, the part left open by T112).

``clean_content_store`` works on two seams: ``StoredFiles`` (list the store folder with modification
times, delete one file) and a reader for the digests live releases reference. The first is
``ContentStore.list_files`` / ``delete_file`` over the workspace REST calls, tested here with
``httpx.MockTransport``; the second is ``registry_reference_reader``, tested over an in-memory
registry. ``run_cleanup_once`` ties them for the daily schedule.

Whenever the answer is unclear the store keeps the file: a listing entry that is not a plain file,
an unreadable registry and a quarantined release all end in "remove nothing".
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from reconciler_fakes import NOW, digest_of, seed_app, stub_registry_class
from retention_world import seed_numbered_release

from appmux.packages.cleanup import (
    CleanupSkipped,
    StoredFile,
    clean_content_store,
    registry_reference_reader,
    run_cleanup_once,
)
from appmux.packages.store import (
    ContentStore,
    ContentStoreError,
    WorkspaceError,
    WorkspaceHttpApi,
)
from appmux.registry.snapshot import QuarantinedRow, build_snapshot

pytestmark = pytest.mark.anyio

BASE_URL = "https://workspace.example.com"
FOLDER = "/Workspace/Users/00000000-example/appmux-store"
SP_TOKEN = "sp-token-example"  # noqa: S105 - an obviously fake label
OLD_MS = int((NOW - timedelta(days=3)).timestamp() * 1000)
YOUNG_MS = int((NOW - timedelta(hours=2)).timestamp() * 1000)
KEPT = "1" * 64  # referenced by a release
LOOSE = "3" * 64  # nothing references it
YOUNG = "4" * 64


class Workspace:
    """A handler that records the requests and answers each from a script or a function."""

    def __init__(self, answer: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self._answer = answer

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(await request.aread())
        return self._answer(request)

    def store(self) -> ContentStore:
        client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(self))
        return ContentStore(WorkspaceHttpApi(client), folder=FOLDER, token_source=lambda: SP_TOKEN)

    def deleted_paths(self) -> list[str]:
        return [
            json.loads(body)["path"]
            for request, body in zip(self.requests, self.bodies, strict=True)
            if request.url.path == "/api/2.0/workspace/delete"
        ]


def listing_of(*objects: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"objects": list(objects)})


def entry(name: str, modified_ms: object = OLD_MS, object_type: str = "FILE") -> dict[str, Any]:
    item: dict[str, Any] = {"path": f"{FOLDER}/{name}", "object_type": object_type, "size": 10}
    if modified_ms is not None:
        item["modified_at"] = modified_ms
    return item


def answering(response: httpx.Response) -> Workspace:
    return Workspace(lambda request: response)


# --- the listing --------------------------------------------------------------------------------


async def test_the_listing_carries_each_files_modification_time() -> None:
    workspace = answering(listing_of(entry(LOOSE, OLD_MS), entry(YOUNG, YOUNG_MS)))
    files = await workspace.store().list_files()
    assert files == [
        StoredFile(LOOSE, NOW - timedelta(days=3)),
        StoredFile(YOUNG, NOW - timedelta(hours=2)),
    ]
    assert all(file.modified_at is not None and file.modified_at.tzinfo is UTC for file in files)
    (request,) = workspace.requests
    assert request.url.path == "/api/2.0/workspace/list"
    assert request.url.params["path"] == FOLDER
    assert request.headers["authorization"] == f"Bearer {SP_TOKEN}"


@pytest.mark.parametrize(
    "modified_at",
    [None, "yesterday", True, -1, 10**30, 1.5e30],
    ids=["missing", "text", "boolean", "negative", "far future", "float too large"],
)
async def test_a_modification_time_the_workspace_did_not_give_is_unknown_not_an_error(
    modified_at: object,
) -> None:
    # The same listing call reads the user's source folder; a changed field must not break it, and
    # "unknown" makes the clean-up keep the file.
    workspace = answering(listing_of(entry(LOOSE, modified_at)))
    assert await workspace.store().list_files() == [StoredFile(LOOSE, None)]


async def test_only_plain_files_are_listed() -> None:
    # A folder or notebook someone put in the store folder is not a store file: it is never a
    # candidate for removal, so it does not appear at all.
    workspace = answering(
        listing_of(
            entry(KEPT),
            entry(LOOSE, object_type="DIRECTORY"),
            entry(YOUNG, object_type="NOTEBOOK"),
            entry("notes.txt"),
        )
    )
    names = [file.name for file in await workspace.store().list_files()]
    assert names == [KEPT, "notes.txt"]


async def test_an_entry_outside_the_store_folder_is_left_out() -> None:
    elsewhere = {"path": "/Workspace/Users/someone-else/" + LOOSE, "object_type": "FILE"}
    workspace = answering(listing_of(entry(KEPT), elsewhere))
    assert [file.name for file in await workspace.store().list_files()] == [KEPT]


async def test_an_empty_store_lists_no_files() -> None:
    assert await answering(httpx.Response(200, json={})).store().list_files() == []


async def test_a_failed_listing_is_a_store_error_naming_the_status_only() -> None:
    workspace = answering(httpx.Response(500, json={"message": "internal detail"}))
    with pytest.raises(ContentStoreError, match=r"list.*status 500") as caught:
        await workspace.store().list_files()
    assert "internal detail" not in str(caught.value)
    assert SP_TOKEN not in str(caught.value)


async def test_the_token_is_read_again_for_every_call() -> None:
    tokens = iter(["tok-one", "tok-two"])
    workspace = answering(httpx.Response(200, json={}))
    client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(workspace))
    store = ContentStore(WorkspaceHttpApi(client), folder=FOLDER, token_source=lambda: next(tokens))
    await store.list_files()
    await store.list_files()
    assert [request.headers["authorization"] for request in workspace.requests] == [
        "Bearer tok-one",
        "Bearer tok-two",
    ]


# --- delete -------------------------------------------------------------------------------------


async def test_delete_posts_the_path_without_recursion() -> None:
    workspace = answering(httpx.Response(200, json={}))
    await workspace.store().delete_file(LOOSE)
    (request,) = workspace.requests
    assert request.method == "POST"
    assert request.url.path == "/api/2.0/workspace/delete"
    assert request.headers["authorization"] == f"Bearer {SP_TOKEN}"
    # recursive=false so that a folder that bears a digest's name is refused, never emptied
    assert json.loads(workspace.bodies[0]) == {"path": f"{FOLDER}/{LOOSE}", "recursive": False}


@pytest.mark.parametrize(
    "name",
    ["../" + "a" * 61, "sha256:" + "a" * 64, "A" * 64, "a" * 63, "a" * 64 + "/x", "", "notes.txt"],
)
async def test_only_a_bare_digest_name_can_be_deleted(name: str) -> None:
    workspace = answering(httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="64 lower-case hex digits"):
        await workspace.store().delete_file(name)
    assert workspace.requests == []


async def test_a_file_that_is_already_gone_counts_as_deleted() -> None:
    # Two runs overlapping, or someone removing it by hand: the wanted end state is reached.
    workspace = answering(httpx.Response(404, json={"message": "RESOURCE_DOES_NOT_EXIST"}))
    await workspace.store().delete_file(LOOSE)
    assert len(workspace.requests) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_any_other_failure_to_delete_is_a_store_error(status: int) -> None:
    workspace = answering(httpx.Response(status, json={"message": "internal detail"}))
    with pytest.raises(ContentStoreError, match=rf"could not remove.*status {status}") as caught:
        await workspace.store().delete_file(LOOSE)
    assert LOOSE in str(caught.value)
    assert "internal detail" not in str(caught.value)


async def test_a_delete_with_no_answer_is_a_store_error() -> None:
    def closed(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ContentStoreError, match="no answer"):
        await Workspace(closed).store().delete_file(LOOSE)


async def test_the_http_adapter_raises_workspace_errors_for_a_delete() -> None:
    workspace = answering(httpx.Response(500, json={"message": "no"}))
    api = WorkspaceHttpApi(
        httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(workspace))
    )
    with pytest.raises(WorkspaceError) as caught:
        await api.delete_path("tok", f"{FOLDER}/{LOOSE}")
    assert caught.value.status == 500


# --- the clean-up over the store ----------------------------------------------------------------


async def test_the_clean_up_removes_only_the_old_unreferenced_file_through_the_store() -> None:
    workspace = Workspace(
        lambda request: (
            listing_of(entry(KEPT), entry(LOOSE), entry(YOUNG, YOUNG_MS), entry("notes.txt"))
            if request.url.path == "/api/2.0/workspace/list"
            else httpx.Response(200, json={})
        )
    )

    async def references() -> list[str]:
        return [KEPT]

    report = await clean_content_store(workspace.store(), references, now=lambda: NOW)
    assert report.removed == (LOOSE,)
    assert workspace.deleted_paths() == [f"{FOLDER}/{LOOSE}"]
    assert (report.kept_referenced, report.kept_recent, report.ignored) == (1, 1, 1)


async def test_one_file_that_cannot_go_does_not_keep_the_others() -> None:
    other = "5" * 64

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/2.0/workspace/list":
            return listing_of(entry(LOOSE), entry(other))
        body = json.loads(request.content)
        return httpx.Response(500 if body["path"].endswith(LOOSE) else 200, json={})

    async def references() -> list[str]:
        return []

    report = await clean_content_store(Workspace(answer).store(), references, now=lambda: NOW)
    assert report.removed == (other,)
    assert [failure.name for failure in report.failed] == [LOOSE]


# --- the registry as the reference reader -------------------------------------------------------


def registry_with_releases() -> Any:
    registry = stub_registry_class()()
    seed_app(registry)
    seed_numbered_release(registry, 1, "superseded")  # a snapshot digest and one wheel
    seed_numbered_release(registry, 2, "active")
    seed_numbered_release(registry, 3, "superseded", deleted=True)
    return registry


async def test_live_releases_name_their_snapshot_and_wheels_and_deleted_ones_name_nothing() -> None:
    registry = registry_with_releases()
    read = registry_reference_reader(registry)
    names = set(await read())
    assert names == {
        digest_of(1).removeprefix("sha256:"),
        digest_of(2).removeprefix("sha256:"),
        "c" * 64,  # the wheel both live releases use
    }


async def test_the_reader_refreshes_the_registry_before_it_reads() -> None:
    registry = registry_with_releases()
    seen: list[str] = []
    original = registry.refresh

    async def refresh() -> None:
        seen.append("refresh")
        await original()

    registry.refresh = refresh
    await registry_reference_reader(registry)()
    assert seen == ["refresh"]


async def test_a_registry_that_is_down_is_never_read_as_no_references() -> None:
    registry = registry_with_releases()
    registry.go_down()
    with pytest.raises(CleanupSkipped, match="registry could not be read"):
        await registry_reference_reader(registry)()


class QuarantineRegistry:
    """A registry whose snapshot has a release row that failed its model."""

    def __init__(self, quarantined: tuple[QuarantinedRow, ...]) -> None:
        self.snapshot = build_snapshot({}, loaded_at=NOW, quarantined=quarantined)
        self.last_error: str | None = None

    async def refresh(self) -> None:
        return None


async def test_a_quarantined_release_row_stops_the_run() -> None:
    # The row is left out of the snapshot but may still reference a file; without it every file it
    # needs looks unreferenced.
    registry = QuarantineRegistry((QuarantinedRow("releases", "sales/4"),))
    with pytest.raises(CleanupSkipped, match="release row"):
        await registry_reference_reader(registry)()


async def test_a_quarantined_row_of_another_table_does_not_stop_the_run() -> None:
    registry = QuarantineRegistry((QuarantinedRow("app_grants", "sales/x"),))
    assert set(await registry_reference_reader(registry)()) == set()


# --- the daily run ------------------------------------------------------------------------------


def store_with_files(deleted: list[str], *names_and_ages: tuple[str, datetime]) -> ContentStore:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/2.0/workspace/list":
            return listing_of(
                *[entry(name, int(at.timestamp() * 1000)) for name, at in names_and_ages]
            )
        deleted.append(json.loads(request.content)["path"])
        return httpx.Response(200, json={})

    return Workspace(answer).store()


async def test_a_run_removes_what_no_live_release_needs() -> None:
    registry = registry_with_releases()
    deleted: list[str] = []
    store = store_with_files(
        deleted,
        (digest_of(1).removeprefix("sha256:"), NOW - timedelta(days=9)),  # a live snapshot
        (LOOSE, NOW - timedelta(days=9)),  # nothing needs it
        (YOUNG, NOW - timedelta(hours=1)),  # too young
    )
    report = await run_cleanup_once(store, registry, now=lambda: NOW)
    assert report is not None
    assert report.removed == (LOOSE,)
    assert deleted == [f"{FOLDER}/{LOOSE}"]


async def test_a_run_with_an_unreadable_registry_removes_nothing_and_says_so() -> None:
    registry = registry_with_releases()
    registry.go_down()
    deleted: list[str] = []
    store = store_with_files(deleted, (LOOSE, NOW - timedelta(days=9)))
    assert await run_cleanup_once(store, registry, now=lambda: NOW) is None
    assert deleted == []


async def test_a_run_with_a_failing_store_removes_nothing_and_says_so() -> None:
    registry = registry_with_releases()
    store = answering(httpx.Response(500, json={})).store()
    assert await run_cleanup_once(store, registry, now=lambda: NOW) is None


async def test_a_run_logs_why_it_was_skipped(caplog: pytest.LogCaptureFixture) -> None:
    registry = registry_with_releases()
    registry.go_down()
    store = store_with_files([])
    with caplog.at_level("WARNING", logger="appmux.packages.cleanup"):
        await run_cleanup_once(store, registry, now=lambda: NOW)
    assert "content-store clean-up skipped" in caplog.text
    assert SP_TOKEN not in caplog.text
