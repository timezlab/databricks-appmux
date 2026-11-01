"""The read-only source viewer of a release (T151, FR-029, rule 17).

``GET /apps/{app}/releases/{id}/source`` lists the files of a release's snapshot and
``GET .../source/{path}`` returns one text file. The routes run behind the real guard and
authorizer on the in-memory registry, with a fake content store that serves tar archives built here
(the layout the snapshot source writes: relative member names, regular files). What is pinned:

* who may look: ``CAN_MANAGE`` (owner, grant holder, or a platform admin with no grant); ``CAN_USE``
  alone is ``403``, a person who cannot open the app gets the ``404`` of a missing one, and nothing
  is read from the store for either;
* what comes back: text files only (UTF-8, no NUL, at most ``MAX_TEXT_BYTES``); a binary file is
  listed with its kind and refused on request;
* every look writes ``release.view_source`` (release, and path for a file) before the answer
  carries any content, so a registry that cannot record it shows nothing;
* path containment: the requested path and the archive's member names are both checked, nothing is
  extracted to disk, and the scratch copy of the snapshot is removed on every outcome.

Starting data: ``edge_harness.seeded_registry`` plus releases of ``sales``. All emails are
placeholders.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import httpx
import pytest
from edge_harness import ALICE, BOB, CAROL, DANA, FRANK, NOW, AdminApi, seeded_registry
from stub_registry import StubRegistry

from appmux.admin.source_viewer import MAX_TEXT_BYTES, SourceViewerRoutes
from appmux.packages.store import ContentStoreError, DigestMismatch, MissingFromStore

DIGEST: Final = "sha256:" + "a" * 64
OTHER_DIGEST: Final = "sha256:" + "b" * 64
RELEASE: Final = 7
LISTING_URL: Final = f"/apps/sales/releases/{RELEASE}/source"
SECRET_MARKERS: Final = ("Traceback", "SELECT", "appmux-source-", "sha256:")

APP_PY: Final = b"from fastapi import FastAPI\r\napp = FastAPI()\n\tprint('caf\xc3\xa9')\n"
FILES: Final[Mapping[str, bytes]] = {
    "app.py": APP_PY,
    "static/app.js": b"console.log('hi');\n",
    "static/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
    "data/latin1.csv": b"name\ncaf\xe9\n",
    "empty.txt": b"",
    "README.md": "# Sales\n\nnaïve, ünïcode\n".encode(),
}


def tar_of(
    files: Mapping[str, bytes], *, extra: tuple[tuple[tarfile.TarInfo, bytes], ...] = ()
) -> bytes:
    """An uncompressed PAX archive laid out as the snapshot source writes it.

    ``extra`` are members the snapshot source never writes (a hostile or damaged archive).
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(content))
        for member, content in extra:
            archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def odd_member(
    name: str, kind: bytes, *, target: str = "", content: bytes = b""
) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.type = kind
    member.linkname = target
    member.size = len(content)
    return member, content


class FakeStore:
    """A content store that serves archives by digest and records every read."""

    def __init__(self, archives: Mapping[str, bytes]) -> None:
        self.archives = dict(archives)
        self.reads: list[Path] = []
        self.digests: list[str] = []
        self.failure: Exception | None = None

    async def get(self, digest: str, destination: Path) -> None:
        self.reads.append(destination)
        self.digests.append(digest)
        if self.failure is not None:
            raise self.failure
        await asyncio.to_thread(destination.write_bytes, self.archives[digest])


class Viewer:
    """The admin API with the source viewer mounted, over a registry that holds releases."""

    def __init__(
        self, archive: bytes | None = None, *, files: Mapping[str, bytes] | None = None
    ) -> None:
        self.registry: StubRegistry = seeded_registry()
        seed_release(self.registry, "sales", RELEASE, DIGEST)
        seed_release(self.registry, "docs", 3, OTHER_DIGEST)
        body = archive if archive is not None else tar_of(FILES if files is None else files)
        self.store = FakeStore({DIGEST: body, OTHER_DIGEST: tar_of({"docs.py": b"x = 1\n"})})
        self.api = AdminApi(self.registry, lambda authorizer: [SourceViewerRoutes(self.store)])

    def get(self, path: str, email: str | None = CAROL) -> httpx.Response:
        return self.api.call("GET", path, email)

    def file(self, path: str, email: str | None = CAROL) -> httpx.Response:
        return self.get(f"{LISTING_URL}/{path}", email)

    @property
    def audit_actions(self) -> list[tuple[str, str, dict[str, object]]]:
        return [
            (row.actor, row.action, dict(row.detail))
            for row in self.registry.audit_rows
            if row.action == "release.view_source"
        ]

    def is_untouched(self) -> bool:
        """Nothing was read from the store and nothing was recorded."""
        return self.store.reads == [] and self.registry.audit_rows == ()


def seed_release(
    registry: StubRegistry, slug: str, release_no: int, digest: str | None, **columns: object
) -> None:
    registry.seed(
        "releases",
        {
            "app_slug": slug,
            "release_no": release_no,
            "snapshot_digest": digest,
            "detected": {},
            "status": "active",
            "status_at": NOW,
            "created_by": CAROL,
            "created_at": NOW,
            **columns,
        },
    )


@pytest.fixture
def viewer() -> Viewer:
    return Viewer()


def assert_no_internals(response: httpx.Response) -> None:
    for marker in SECRET_MARKERS:
        assert marker not in response.text


# --- the listing ----------------------------------------------------------------------------------


def test_the_listing_gives_every_file_with_its_size_and_kind_ordered_by_path(
    viewer: Viewer,
) -> None:
    response = viewer.get(LISTING_URL)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "release_no": RELEASE,
        "items": [
            {"path": "README.md", "size_bytes": len(FILES["README.md"]), "kind": "text"},
            {"path": "app.py", "size_bytes": len(APP_PY), "kind": "text"},
            {"path": "data/latin1.csv", "size_bytes": 10, "kind": "binary"},
            {"path": "empty.txt", "size_bytes": 0, "kind": "text"},
            {"path": "static/app.js", "size_bytes": 19, "kind": "text"},
            {
                "path": "static/logo.png",
                "size_bytes": len(FILES["static/logo.png"]),
                "kind": "binary",
            },
        ],
    }


def test_a_text_file_at_the_limit_is_text_and_one_byte_more_is_listed_as_binary() -> None:
    viewer = Viewer(
        files={"at_limit.txt": b"a" * MAX_TEXT_BYTES, "over.txt": b"a" * (MAX_TEXT_BYTES + 1)}
    )

    items = {item["path"]: item["kind"] for item in viewer.get(LISTING_URL).json()["items"]}

    assert items == {"at_limit.txt": "text", "over.txt": "binary"}


def test_only_regular_files_with_names_inside_the_snapshot_are_listed() -> None:
    extra = (
        odd_member("../escape.txt", tarfile.REGTYPE, content=b"outside"),
        odd_member("/etc/absolute.txt", tarfile.REGTYPE, content=b"outside"),
        odd_member("a/../b.txt", tarfile.REGTYPE, content=b"outside"),
        odd_member("a//c.txt", tarfile.REGTYPE, content=b"outside"),
        odd_member("./dot.txt", tarfile.REGTYPE, content=b"outside"),
        odd_member("link.txt", tarfile.SYMTYPE, target="/etc/passwd"),
        odd_member("hard.txt", tarfile.LNKTYPE, target="app.py"),
        odd_member("folder", tarfile.DIRTYPE),
        odd_member("fifo", tarfile.FIFOTYPE),
    )
    viewer = Viewer(archive=tar_of({"app.py": b"x = 1\n"}, extra=extra))

    response = viewer.get(LISTING_URL)

    assert response.status_code == 200, response.text
    assert [item["path"] for item in response.json()["items"]] == ["app.py"]


def test_a_snapshot_that_names_a_file_twice_lists_it_once() -> None:
    extra = (odd_member("app.py", tarfile.REGTYPE, content=b"y = 2\n"),)
    viewer = Viewer(archive=tar_of({"app.py": b"x = 1\n"}, extra=extra))

    items = viewer.get(LISTING_URL).json()["items"]

    assert [item["path"] for item in items] == ["app.py"]


def test_each_listing_writes_one_audit_row_naming_the_release_and_no_path(viewer: Viewer) -> None:
    assert viewer.get(LISTING_URL).status_code == 200
    assert viewer.get(LISTING_URL).status_code == 200

    assert viewer.audit_actions == [(CAROL, "release.view_source", {"release": RELEASE})] * 2
    assert {row.app_slug for row in viewer.registry.audit_rows} == {"sales"}
    assert viewer.registry.writes[-1].kinds == ()


def test_an_empty_snapshot_lists_no_files() -> None:
    viewer = Viewer(files={})

    response = viewer.get(LISTING_URL)

    assert response.status_code == 200
    assert response.json() == {"release_no": RELEASE, "items": []}


# --- one file -------------------------------------------------------------------------------------


def test_a_text_file_comes_back_exactly_as_stored(viewer: Viewer) -> None:
    response = viewer.file("app.py")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "release_no": RELEASE,
        "path": "app.py",
        "size_bytes": len(APP_PY),
        "content": APP_PY.decode(),
    }


def test_a_file_in_a_folder_and_an_empty_file_are_served(viewer: Viewer) -> None:
    nested = viewer.file("static/app.js")
    empty = viewer.file("empty.txt")

    assert nested.json()["content"] == "console.log('hi');\n"
    assert (empty.status_code, empty.json()["content"], empty.json()["size_bytes"]) == (200, "", 0)


def test_a_file_name_with_unicode_or_spaces_is_served() -> None:
    viewer = Viewer(files={"docs/user guide/ghi chú.md": b"hello\n"})

    response = viewer.file("docs/user%20guide/ghi%20ch%C3%BA.md")

    assert response.status_code == 200, response.text
    assert response.json()["path"] == "docs/user guide/ghi chú.md"


def test_each_file_view_writes_one_audit_row_with_the_release_and_the_path(
    viewer: Viewer,
) -> None:
    viewer.file("app.py")
    viewer.file("static/app.js")

    assert viewer.audit_actions == [
        (CAROL, "release.view_source", {"release": RELEASE, "path": "app.py"}),
        (CAROL, "release.view_source", {"release": RELEASE, "path": "static/app.js"}),
    ]


@pytest.mark.parametrize("path", ["static/logo.png", "data/latin1.csv"])
def test_a_binary_file_is_refused_and_its_bytes_are_not_returned(viewer: Viewer, path: str) -> None:
    response = viewer.file(path)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "not_text"
    assert "PNG" not in response.text
    assert_no_internals(response)


def test_a_file_over_the_size_limit_is_refused_like_a_binary_one() -> None:
    viewer = Viewer(files={"big.txt": b"a" * (MAX_TEXT_BYTES + 1)})

    response = viewer.file("big.txt")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "not_text"
    assert "aaaa" not in response.text


def test_a_file_at_the_size_limit_is_served() -> None:
    viewer = Viewer(files={"edge.txt": b"a" * MAX_TEXT_BYTES})

    response = viewer.file("edge.txt")

    assert response.status_code == 200
    assert response.json()["size_bytes"] == MAX_TEXT_BYTES


def test_a_missing_file_is_not_found(viewer: Viewer) -> None:
    response = viewer.file("nope.py")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_folder_name_is_not_a_file(viewer: Viewer) -> None:
    assert viewer.file("static").status_code == 404


def test_a_symbolic_link_in_the_archive_is_not_a_file() -> None:
    link = odd_member("link.txt", tarfile.SYMTYPE, target="/etc/passwd")
    viewer = Viewer(archive=tar_of({"app.py": b"x = 1\n"}, extra=(link,)))

    response = viewer.file("link.txt")

    assert response.status_code == 404
    assert "root:" not in response.text


def test_a_look_that_finds_nothing_or_a_binary_file_is_still_recorded(viewer: Viewer) -> None:
    viewer.file("nope.py")
    viewer.file("static/logo.png")

    assert [(action, detail["path"]) for _, action, detail in viewer.audit_actions] == [
        ("release.view_source", "nope.py"),
        ("release.view_source", "static/logo.png"),
    ]


# --- path containment -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "%2e%2e/escape.txt",
        "static/%2e%2e/app.py",
        "%2e/app.py",
        "static/%2e/app.js",
        "static//app.js",
        "static/app.js/",
        "%2Fetc%2Fpasswd",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "app.py%00.txt",
        "",
    ],
    ids=lambda path: path or "empty",
)
def test_a_path_that_leaves_or_bends_the_snapshot_is_refused_before_anything_is_read(
    path: str,
) -> None:
    extra = (
        odd_member("../escape.txt", tarfile.REGTYPE, content=b"outside"),
        odd_member("/etc/passwd", tarfile.REGTYPE, content=b"outside"),
    )
    viewer = Viewer(archive=tar_of(FILES, extra=extra))

    response = viewer.file(path)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "bad_request"
    assert "outside" not in response.text
    assert "X-Injected" not in response.headers
    assert viewer.is_untouched()


def test_a_path_with_a_line_break_never_reaches_the_viewer() -> None:
    viewer = Viewer()

    response = viewer.file("app.py%0d%0aX-Injected:%20yes")

    # The route's path pattern does not match a line break, so the router answers before the
    # viewer runs; either way nothing is read, recorded or echoed into a header.
    assert response.status_code == 404
    assert "X-Injected" not in response.headers
    assert viewer.is_untouched()


def test_a_path_longer_than_an_audit_entry_can_hold_is_refused_before_anything_is_read() -> None:
    viewer = Viewer()

    response = viewer.file("a/" * 300 + "b.txt")

    assert response.status_code == 400
    assert viewer.is_untouched()


def test_a_member_that_climbs_out_is_never_returned_even_when_asked_by_its_exact_name() -> None:
    extra = (odd_member("../escape.txt", tarfile.REGTYPE, content=b"outside"),)
    viewer = Viewer(archive=tar_of({"app.py": b"x = 1\n"}, extra=extra))

    assert viewer.file("%2e%2e/escape.txt").status_code == 400
    assert viewer.file("escape.txt").status_code == 404


def test_a_name_that_looks_like_a_credential_cannot_be_viewed_and_is_not_echoed() -> None:
    name = "config/api_key=abcdef123456.txt"
    viewer = Viewer(files={name: b"x = 1\n", "ok.txt": b"fine\n"})

    listed = [item["path"] for item in viewer.get(LISTING_URL).json()["items"]]
    response = viewer.file(name)

    assert listed == [name, "ok.txt"]
    assert response.status_code == 400
    assert "abcdef123456" not in response.text
    assert viewer.audit_actions == [(CAROL, "release.view_source", {"release": RELEASE})]


# --- who may look ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email", [ALICE, CAROL, DANA], ids=["owner", "can_manage", "platform_admin"]
)
def test_an_app_admin_and_a_platform_admin_may_look_and_it_is_recorded_under_their_name(
    viewer: Viewer, email: str
) -> None:
    listing = viewer.get(LISTING_URL, email)
    one_file = viewer.file("app.py", email)

    assert (listing.status_code, one_file.status_code) == (200, 200)
    assert [actor for actor, _, _ in viewer.audit_actions] == [email, email]


def test_a_platform_admin_looks_without_a_grant_and_gets_none(viewer: Viewer) -> None:
    grants_before = viewer.registry.snapshot.grants_for("sales")

    assert viewer.file("app.py", DANA).status_code == 200

    assert viewer.registry.snapshot.grants_for("sales") == grants_before
    assert all(grant.principal != DANA for grant in viewer.registry.snapshot.grants_for("sales"))


@pytest.mark.parametrize("url", [LISTING_URL, f"{LISTING_URL}/app.py"], ids=["listing", "file"])
def test_can_use_alone_is_denied_and_nothing_is_read_or_recorded(viewer: Viewer, url: str) -> None:
    response = viewer.get(url, BOB)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert "fastapi" not in response.text
    assert viewer.is_untouched()


@pytest.mark.parametrize("url", [LISTING_URL, f"{LISTING_URL}/app.py"], ids=["listing", "file"])
def test_a_stranger_gets_the_not_found_of_a_missing_app(viewer: Viewer, url: str) -> None:
    stranger = viewer.get(url, FRANK)
    missing = viewer.get(url.replace("/sales/", "/nothing/"), CAROL)

    assert stranger.status_code == missing.status_code == 404
    assert stranger.json() == missing.json()
    assert viewer.is_untouched()


def test_a_request_without_an_identity_is_unauthorized(viewer: Viewer) -> None:
    response = viewer.get(LISTING_URL, None)

    assert response.status_code == 401
    assert viewer.is_untouched()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_the_viewer_is_read_only(viewer: Viewer, method: str) -> None:
    for url in (LISTING_URL, f"{LISTING_URL}/app.py"):
        response = viewer.api.call(method, url, CAROL, json={})

        assert response.status_code == 405
    assert viewer.is_untouched()


# --- which release --------------------------------------------------------------------------------


@pytest.mark.parametrize("release_id", ["99", "abc", "0", "-1", "07", "1.5", "99999999999"])
def test_a_release_that_does_not_exist_is_not_found(viewer: Viewer, release_id: str) -> None:
    for url in (
        f"/apps/sales/releases/{release_id}/source",
        f"/apps/sales/releases/{release_id}/source/app.py",
    ):
        response = viewer.get(url)

        assert response.status_code == 404
        assert response.json()["error"]["message"] == "no such release"
    assert viewer.is_untouched()


def test_a_release_of_another_app_is_not_found_under_this_one(viewer: Viewer) -> None:
    assert viewer.get("/apps/sales/releases/3/source").status_code == 404
    assert viewer.get("/apps/docs/releases/3/source", FRANK).status_code == 200
    assert viewer.get("/apps/docs/releases/3/source", CAROL).status_code == 404


def test_a_deleted_release_has_no_snapshot_and_is_not_found() -> None:
    viewer = Viewer()
    seed_release(
        viewer.registry, "sales", 8, None, deleted_by=CAROL, deleted_at=NOW, status="superseded"
    )

    response = viewer.get("/apps/sales/releases/8/source")

    assert response.status_code == 404
    assert viewer.is_untouched()


# --- the audit comes first ------------------------------------------------------------------------


def test_a_registry_that_cannot_record_the_look_shows_nothing(viewer: Viewer) -> None:
    viewer.registry.go_down()

    listing = viewer.get(LISTING_URL)
    one_file = viewer.file("app.py")

    assert (listing.status_code, one_file.status_code) == (503, 503)
    assert "fastapi" not in one_file.text
    assert "items" not in listing.json()
    assert viewer.registry.audit_rows == ()


# --- the store ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (MissingFromStore("sha256:... is not in the content store"), 503, "snapshot_missing"),
        (DigestMismatch("the file holds other bytes"), 503, "content_store_unavailable"),
        (
            ContentStoreError("could not read (workspace status 500)"),
            503,
            "content_store_unavailable",
        ),
    ],
    ids=["missing", "digest_mismatch", "store_error"],
)
def test_a_store_that_cannot_give_the_snapshot_is_unavailable_and_nothing_is_recorded(
    viewer: Viewer, failure: Exception, status: int, code: str
) -> None:
    viewer.store.failure = failure

    response = viewer.get(LISTING_URL)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert viewer.registry.audit_rows == ()
    assert_no_internals(response)
    assert "workspace status" not in response.text


def test_a_snapshot_that_is_not_a_tar_archive_is_a_server_defect_and_nothing_is_recorded() -> None:
    viewer = Viewer(archive=b"this is not a tar archive" * 100)

    response = viewer.get(LISTING_URL)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "snapshot_unreadable"
    assert viewer.registry.audit_rows == ()


def test_a_compressed_archive_is_not_opened() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("app.py")
        member.size = 6
        archive.addfile(member, io.BytesIO(b"x = 1\n"))
    viewer = Viewer(archive=buffer.getvalue())

    response = viewer.get(LISTING_URL)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "snapshot_unreadable"


@pytest.mark.parametrize("outcome", ["listing", "file", "refused_binary", "store_error"])
def test_the_scratch_copy_of_the_snapshot_is_removed_on_every_outcome(
    viewer: Viewer, outcome: str
) -> None:
    if outcome == "store_error":
        viewer.store.failure = ContentStoreError("down")
    if outcome == "listing" or outcome == "store_error":
        viewer.get(LISTING_URL)
    elif outcome == "file":
        viewer.file("app.py")
    else:
        viewer.file("static/logo.png")

    (scratch,) = viewer.store.reads
    assert not scratch.exists()
    assert not scratch.parent.exists()


def test_the_store_is_asked_for_the_digest_of_the_release_named() -> None:
    viewer = Viewer()

    viewer.get(LISTING_URL)
    viewer.get("/apps/docs/releases/3/source", FRANK)

    assert viewer.store.digests == [DIGEST, OTHER_DIGEST]
