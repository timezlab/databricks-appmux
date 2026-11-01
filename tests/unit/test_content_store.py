"""The content store: files named by digest in appmux's own workspace folder (T088).

``ContentStore`` is tested against an in-memory workspace. ``WorkspaceHttpApi`` is the part that
speaks the real workspace API; it is tested with ``httpx.MockTransport``, which replays the URL
shapes and status codes the spike 4 and spike 15 probes measured. No test opens a socket.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fake_workspace import FakeWorkspace

from appmux.packages.store import (
    ContentStore,
    ContentStoreError,
    DigestMismatch,
    FileTooLarge,
    MissingFromStore,
    WorkspaceError,
    WorkspaceFileTooLarge,
    WorkspaceHttpApi,
    WorkspaceNotFound,
    WorkspaceSessionExpired,
    file_digest,
)

pytestmark = pytest.mark.anyio

FOLDER = "/Workspace/Users/00000000-example/appmux-store"
SP_TOKEN = "sp-token-example"  # noqa: S105 - an obviously fake label the fake workspace compares


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def store_over(workspace: FakeWorkspace, **options: int) -> ContentStore:
    return ContentStore(workspace, folder=FOLDER, token_source=lambda: SP_TOKEN, **options)


def local_file(tmp_path: Path, data: bytes, name: str = "snapshot.tar") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- the store ----------------------------------------------------------------------------------


async def test_a_file_put_in_the_store_can_be_got_back_byte_for_byte(tmp_path: Path) -> None:
    store = store_over(FakeWorkspace(required_token=SP_TOKEN))
    data = bytes(range(256)) * 40
    await store.put(digest_of(data), local_file(tmp_path, data))
    destination = tmp_path / "back.tar"
    await store.get(digest_of(data), destination)
    assert destination.read_bytes() == data


async def test_the_file_is_named_by_the_hex_digest_in_the_store_folder(tmp_path: Path) -> None:
    workspace = FakeWorkspace(required_token=SP_TOKEN)
    data = b"snapshot bytes"
    await store_over(workspace).put(digest_of(data), local_file(tmp_path, data))
    assert list(workspace.files) == [f"{FOLDER}/{hashlib.sha256(data).hexdigest()}"]


async def test_exists_tells_a_stored_digest_from_a_missing_one(tmp_path: Path) -> None:
    store = store_over(FakeWorkspace(required_token=SP_TOKEN))
    data = b"one"
    assert not await store.exists(digest_of(data))
    await store.put(digest_of(data), local_file(tmp_path, data))
    assert await store.exists(digest_of(data))
    assert not await store.exists(digest_of(b"two"))


async def test_putting_the_same_digest_twice_keeps_one_file(tmp_path: Path) -> None:
    workspace = FakeWorkspace(required_token=SP_TOKEN)
    store = store_over(workspace)
    data = b"same"
    await store.put(digest_of(data), local_file(tmp_path, data))
    await store.put(digest_of(data), local_file(tmp_path, data, "again.tar"))
    assert len(workspace.files) == 1


async def test_every_call_uses_the_service_principals_token_read_fresh(tmp_path: Path) -> None:
    workspace = FakeWorkspace()
    tokens = iter(["first", "second", "third", "fourth"])
    store = ContentStore(workspace, folder=FOLDER, token_source=lambda: next(tokens))
    data = b"x"
    await store.exists(digest_of(data))
    await store.put(digest_of(data), local_file(tmp_path, data))
    await store.exists(digest_of(data))
    await store.get(digest_of(data), tmp_path / "out")
    assert [token for _, _, token in workspace.calls] == ["first", "second", "third", "fourth"]


async def test_a_file_over_the_limit_is_refused_naming_the_file_and_nothing_is_sent(
    tmp_path: Path,
) -> None:
    workspace = FakeWorkspace(required_token=SP_TOKEN)
    store = store_over(workspace, max_file_bytes=10)
    data = b"x" * 11
    with pytest.raises(FileTooLarge) as caught:
        await store.put(digest_of(data), local_file(tmp_path, data))
    assert digest_of(data) in str(caught.value)
    assert "10 bytes" in str(caught.value)
    assert workspace.calls == []


async def test_a_file_exactly_at_the_limit_is_stored(tmp_path: Path) -> None:
    store = store_over(FakeWorkspace(required_token=SP_TOKEN), max_file_bytes=10)
    data = b"x" * 10
    await store.put(digest_of(data), local_file(tmp_path, data))
    assert await store.exists(digest_of(data))


async def test_getting_what_was_never_stored_says_so(tmp_path: Path) -> None:
    store = store_over(FakeWorkspace(required_token=SP_TOKEN))
    destination = tmp_path / "out"
    with pytest.raises(MissingFromStore) as caught:
        await store.get(digest_of(b"never stored"), destination)
    assert digest_of(b"never stored") in str(caught.value)
    assert not destination.exists()


async def test_bytes_that_do_not_match_their_name_are_refused_and_not_kept(
    tmp_path: Path,
) -> None:
    workspace = FakeWorkspace(required_token=SP_TOKEN)
    store = store_over(workspace)
    name = digest_of(b"what the name promises")
    workspace.add_file(f"{FOLDER}/{name.removeprefix('sha256:')}", b"what is really there")
    destination = tmp_path / "out"
    with pytest.raises(DigestMismatch) as caught:
        await store.get(name, destination)
    assert name in str(caught.value)
    assert not destination.exists()


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "sha256:",
        "sha256:abc",
        "sha256:" + "G" * 64,
        "sha256:" + "A" * 64,  # upper case is another spelling of the same digest: not accepted
        "sha1:" + "a" * 40,
        "a" * 64,
        "sha256:" + "a" * 63 + "/",
        "../" + "a" * 64,
        "sha256:../../" + "a" * 57,
    ],
)
async def test_a_name_that_is_not_a_sha256_digest_never_reaches_the_workspace(
    tmp_path: Path, digest: str
) -> None:
    workspace = FakeWorkspace(required_token=SP_TOKEN)
    store = store_over(workspace)
    with pytest.raises(ValueError, match="sha256"):
        await store.exists(digest)
    with pytest.raises(ValueError, match="sha256"):
        await store.put(digest, local_file(tmp_path, b"x"))
    with pytest.raises(ValueError, match="sha256"):
        await store.get(digest, tmp_path / "out")
    assert workspace.calls == []


async def test_a_workspace_failure_is_a_store_error_that_keeps_its_cause(tmp_path: Path) -> None:
    workspace = FakeWorkspace()  # no required token: the store's own token is fine
    data = b"x"
    workspace.failing_status[f"{FOLDER}/{hashlib.sha256(data).hexdigest()}"] = 500
    store = store_over(workspace)
    with pytest.raises(ContentStoreError) as caught:
        await store.put(digest_of(data), local_file(tmp_path, data))
    assert isinstance(caught.value.__cause__, WorkspaceError)
    assert "500" in str(caught.value)


async def test_ensure_folder_creates_the_store_folder_with_the_store_token() -> None:
    workspace = FakeWorkspace(required_token=SP_TOKEN)
    await store_over(workspace).ensure_folder()
    assert workspace.calls == [("mkdirs", FOLDER, SP_TOKEN)]
    await store_over(workspace).ensure_folder()  # creating a folder that exists is not an error


async def test_ensure_folder_reports_a_workspace_failure_as_a_store_error() -> None:
    workspace = FakeWorkspace()
    workspace.failing_status[FOLDER] = 500
    with pytest.raises(ContentStoreError, match="500"):
        await store_over(workspace).ensure_folder()


async def test_the_store_refuses_a_folder_outside_the_workspace_tree() -> None:
    with pytest.raises(ValueError, match="/Workspace/"):
        ContentStore(FakeWorkspace(), folder="/var/store", token_source=lambda: "t")


async def test_file_digest_is_the_sha256_of_the_bytes(tmp_path: Path) -> None:
    path = local_file(tmp_path, b"abc")
    assert file_digest(path) == (
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


# --- the HTTP adapter ---------------------------------------------------------------------------

BASE_URL = "https://workspace.example.com"


def adapter(transport: httpx.MockTransport) -> WorkspaceHttpApi:
    return WorkspaceHttpApi(httpx.AsyncClient(base_url=BASE_URL, transport=transport))


class Recorder:
    """A handler that records each request and answers with the next scripted response."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self._responses = list(responses)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(await request.aread())
        return self._responses.pop(0)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


async def test_write_posts_the_bytes_to_import_file_with_overwrite(tmp_path: Path) -> None:
    recorder = Recorder(httpx.Response(200, json={}))
    path = "/Workspace/Users/00000000-example/appmux-store/ab12"
    await adapter(recorder.transport).write_file("sp-token", path, local_file(tmp_path, b"DATA"))
    (request,) = recorder.requests
    assert request.method == "POST"
    assert request.url.path == "/api/2.0/workspace-files/import-file" + path
    assert parse_qs(urlsplit(str(request.url)).query) == {"overwrite": ["true"]}
    assert request.headers["authorization"] == "Bearer sp-token"
    assert request.headers["content-length"] == "4"
    assert recorder.bodies == [b"DATA"]


async def test_write_accepts_200_and_204(tmp_path: Path) -> None:
    for status in (200, 204):
        recorder = Recorder(httpx.Response(status))
        await adapter(recorder.transport).write_file("t", f"{FOLDER}/a", local_file(tmp_path, b"x"))


async def test_a_path_with_spaces_is_percent_encoded_in_the_url(tmp_path: Path) -> None:
    recorder = Recorder(httpx.Response(200), httpx.Response(200, content=b"x"))
    api = adapter(recorder.transport)
    await api.write_file("t", "/Workspace/Users/a b/store/f", local_file(tmp_path, b"x"))
    await api.read_file("t", "/Workspace/Users/a b/store/f", tmp_path / "o", max_bytes=10)
    assert recorder.requests[0].url.raw_path.startswith(
        b"/api/2.0/workspace-files/import-file/Workspace/Users/a%20b/store/f"
    )
    assert parse_qs(recorder.requests[1].url.query.decode())["path"] == [
        "/Workspace/Users/a b/store/f"
    ]


async def test_read_gets_the_export_as_a_direct_download(tmp_path: Path) -> None:
    recorder = Recorder(httpx.Response(200, content=b"FILE BYTES"))
    destination = tmp_path / "out"
    await adapter(recorder.transport).read_file(
        "tok", f"{FOLDER}/ab12", destination, max_bytes=1000
    )
    (request,) = recorder.requests
    assert request.method == "GET"
    assert request.url.path == "/api/2.0/workspace/export"
    assert parse_qs(urlsplit(str(request.url)).query) == {
        "path": [f"{FOLDER}/ab12"],
        "direct_download": ["true"],
    }
    assert destination.read_bytes() == b"FILE BYTES"


async def test_read_stops_and_removes_the_file_when_it_grows_past_the_limit(
    tmp_path: Path,
) -> None:
    recorder = Recorder(httpx.Response(200, content=b"x" * 100))
    destination = tmp_path / "out"
    with pytest.raises(WorkspaceFileTooLarge):
        await adapter(recorder.transport).read_file("t", f"{FOLDER}/a", destination, max_bytes=10)
    assert not destination.exists()


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (404, WorkspaceNotFound),
        (401, WorkspaceSessionExpired),
        (403, WorkspaceError),
        (500, WorkspaceError),
    ],
)
async def test_statuses_become_the_errors_the_callers_handle(
    tmp_path: Path, status: int, error: type[WorkspaceError]
) -> None:
    body = {"error_code": "X", "message": "the workspace says no"}
    recorder = Recorder(
        httpx.Response(status, json=body),
        httpx.Response(status, json=body),
        httpx.Response(status, json=body),
    )
    api = adapter(recorder.transport)
    with pytest.raises(error) as caught:
        await api.read_file("t", f"{FOLDER}/a", tmp_path / "o", max_bytes=10)
    assert caught.value.status == status
    assert "the workspace says no" in str(caught.value)
    with pytest.raises(error):
        await api.write_file("t", f"{FOLDER}/a", local_file(tmp_path, b"x"))
    with pytest.raises(error):
        await api.list_folder("t", FOLDER)
    assert not (tmp_path / "o").exists()


async def test_an_error_never_carries_the_token(tmp_path: Path) -> None:
    recorder = Recorder(httpx.Response(500, text="boom"))
    with pytest.raises(WorkspaceError) as caught:
        await adapter(recorder.transport).read_file(
            "secret-token-value", f"{FOLDER}/a", tmp_path / "o", max_bytes=10
        )
    assert "secret-token-value" not in str(caught.value)
    assert "secret-token-value" not in repr(caught.value)


async def test_a_connection_closed_without_an_answer_is_a_workspace_error(
    tmp_path: Path,
) -> None:
    # Spike 4: the service closes an over-limit upload instead of answering.
    def closed(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    with pytest.raises(WorkspaceError) as caught:
        await adapter(httpx.MockTransport(closed)).write_file(
            "t", f"{FOLDER}/a", local_file(tmp_path, b"x")
        )
    assert caught.value.status is None
    assert isinstance(caught.value.__cause__, httpx.RemoteProtocolError)


async def test_make_folder_posts_mkdirs_with_the_path_as_json() -> None:
    recorder = Recorder(httpx.Response(200, json={}))
    await adapter(recorder.transport).make_folder("tok", FOLDER)
    (request,) = recorder.requests
    assert request.method == "POST"
    assert request.url.path == "/api/2.0/workspace/mkdirs"
    assert request.headers["authorization"] == "Bearer tok"
    assert json.loads(recorder.bodies[0]) == {"path": FOLDER}


async def test_exists_reads_get_status() -> None:
    recorder = Recorder(
        httpx.Response(200, json={"object_type": "FILE", "path": f"{FOLDER}/a"}),
        httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST", "message": "no"}),
    )
    api = adapter(recorder.transport)
    assert await api.file_exists("t", f"{FOLDER}/a") is True
    assert await api.file_exists("t", f"{FOLDER}/b") is False
    assert recorder.requests[0].url.path == "/api/2.0/workspace/get-status"
    assert parse_qs(urlsplit(str(recorder.requests[0].url)).query) == {"path": [f"{FOLDER}/a"]}


async def test_exists_does_not_hide_other_failures() -> None:
    recorder = Recorder(httpx.Response(500, json={"message": "down"}))
    with pytest.raises(WorkspaceError):
        await adapter(recorder.transport).file_exists("t", f"{FOLDER}/a")


async def test_list_returns_the_objects_with_their_types_and_sizes() -> None:
    listing = {
        "objects": [
            {"path": f"{FOLDER}/app.py", "object_type": "FILE", "size": 12, "object_id": 1},
            {"path": f"{FOLDER}/static", "object_type": "DIRECTORY", "object_id": 2},
            {"path": f"{FOLDER}/nb", "object_type": "NOTEBOOK", "language": "PYTHON"},
        ]
    }
    recorder = Recorder(httpx.Response(200, json=listing))
    objects = await adapter(recorder.transport).list_folder("tok", FOLDER)
    assert [
        (item.path.rsplit("/", 1)[1], item.object_type, item.size_bytes) for item in objects
    ] == [
        ("app.py", "FILE", 12),
        ("static", "DIRECTORY", None),
        ("nb", "NOTEBOOK", None),
    ]
    (request,) = recorder.requests
    assert request.url.path == "/api/2.0/workspace/list"
    assert parse_qs(urlsplit(str(request.url)).query) == {"path": [FOLDER]}
    assert request.headers["authorization"] == "Bearer tok"


async def test_an_empty_folder_lists_as_no_objects() -> None:
    # The API answers {} (no "objects" key) for an empty folder, and for a caller with no access.
    recorder = Recorder(httpx.Response(200, json={}))
    assert await adapter(recorder.transport).list_folder("t", FOLDER) == []


async def test_a_listing_that_is_not_the_documented_shape_is_an_error() -> None:
    recorder = Recorder(httpx.Response(200, json={"objects": [{"object_type": "FILE"}]}))
    with pytest.raises(WorkspaceError, match="listing"):
        await adapter(recorder.transport).list_folder("t", FOLDER)
    recorder = Recorder(httpx.Response(200, content=b"<html>proxy page</html>"))
    with pytest.raises(WorkspaceError, match="listing"):
        await adapter(recorder.transport).list_folder("t", FOLDER)


async def test_the_json_error_text_is_cut_to_a_short_length() -> None:
    recorder = Recorder(httpx.Response(500, content=json.dumps({"message": "m" * 5000}).encode()))
    with pytest.raises(WorkspaceError) as caught:
        await adapter(recorder.transport).list_folder("t", FOLDER)
    assert len(str(caught.value)) < 400
