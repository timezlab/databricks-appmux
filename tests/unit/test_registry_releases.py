"""The release writes of the reconciler: the insert of an Update, and every status step (T092/3).

The builders only make ``Change`` values; applying them to the in-memory registry shows the rows
they produce. The state table itself is tested in ``test_release_states.py``; here it is proven to
be consulted, so a status step the table forbids cannot reach the registry.
"""

from __future__ import annotations

import pytest
from reconciler_fakes import NOW, digest_of, seed_app, seed_release, stub_registry_class

from appmux.reconciler.states import IllegalTransition
from appmux.registry.models import DetectedManifest, Release, ReleaseConfig
from appmux.registry.releases import (
    LOG_TAIL_LINES,
    MAX_LOG_TAIL_CHARS,
    MAX_REASON_CHARS,
    START_LOG_TAIL_LINES,
    activation_changes,
    clean_log_tail,
    clean_reason,
    insert_release_change,
    next_release_no,
    release_status_change,
)
from appmux.registry.writers import WriteKind

pytestmark = pytest.mark.anyio


def registry_with_app() -> object:
    registry = stub_registry_class()()
    seed_app(registry)
    return registry


async def test_the_first_release_of_an_app_is_number_one() -> None:
    registry = registry_with_app()

    assert next_release_no(registry.snapshot, "sales") == 1  # type: ignore[attr-defined]


async def test_a_release_number_is_one_past_the_highest_ever_used_deleted_ones_included() -> None:
    registry = registry_with_app()
    seed_release(registry, "sales", 1, "superseded")
    seed_release(registry, "sales", 4, "superseded", deleted=True)
    seed_release(registry, "sales", 2, "active")

    assert next_release_no(registry.snapshot, "sales") == 5  # type: ignore[attr-defined]


async def test_the_insert_writes_a_pending_release_with_its_manifest_and_config() -> None:
    registry = registry_with_app()
    change = insert_release_change(
        app_slug="sales",
        release_no=1,
        snapshot_digest=digest_of(7),
        detected=DetectedManifest(runtime="python", command=["python", "app.py"]),
        config=ReleaseConfig(),
        created_by="alice@example.com",
    )

    assert change.kind is WriteKind.RELEASE_INSERT
    (row,) = await registry.write([change])  # type: ignore[attr-defined]

    assert isinstance(row, Release)
    assert (row.app_slug, row.release_no, row.status) == ("sales", 1, "pending")
    assert row.snapshot_digest == digest_of(7)
    assert row.detected.command == ["python", "app.py"]
    assert row.created_by == "alice@example.com"


async def test_a_status_step_moves_the_release_and_the_registry_stamps_the_time() -> None:
    registry = registry_with_app()
    seed_release(registry, "sales", 1, "pending")
    release = registry.snapshot.release("sales", 1)  # type: ignore[attr-defined]

    change = release_status_change(release, "building")
    assert change.kind is WriteKind.RELEASE_UPDATE
    assert dict(change.key) == {"app_slug": "sales", "release_no": 1}
    (row,) = await registry.write([change])  # type: ignore[attr-defined]

    assert isinstance(row, Release)
    assert row.status == "building"
    assert row.status_at != NOW


async def test_a_failed_step_stores_the_reason_and_the_log_tail() -> None:
    registry = registry_with_app()
    seed_release(registry, "sales", 1, "starting")
    release = registry.snapshot.release("sales", 1)  # type: ignore[attr-defined]

    change = release_status_change(
        release,
        "failed",
        reason="health check did not pass within 60 s (last error: connection refused)",
        log_tail="Traceback ...\nImportError: no module named 'x'",
    )
    (row,) = await registry.write([change])  # type: ignore[attr-defined]

    assert isinstance(row, Release)
    assert row.status == "failed"
    assert row.status_reason == (
        "health check did not pass within 60 s (last error: connection refused)"
    )
    assert row.build_log_tail == "Traceback ...\nImportError: no module named 'x'"


async def test_a_later_step_clears_the_reason_of_an_earlier_one() -> None:
    registry = registry_with_app()
    seed_release(registry, "sales", 1, "failed", status_reason="old reason")
    release = registry.snapshot.release("sales", 1)  # type: ignore[attr-defined]

    (row,) = await registry.write(  # type: ignore[attr-defined]
        [release_status_change(release, "pending", by_hand=True)]
    )

    assert isinstance(row, Release)
    assert (row.status, row.status_reason, row.build_log_tail) == ("pending", None, None)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("failed", "building"),
        ("superseded", "active"),
        ("pending", "active"),
        ("active", "pending"),
    ],
)
def test_a_step_the_state_table_forbids_never_becomes_a_change(old: str, new: str) -> None:
    release = Release.model_validate(
        {
            "app_slug": "sales",
            "release_no": 1,
            "snapshot_digest": digest_of(1),
            "detected": {},
            "status": old,
            "status_at": NOW,
            "created_by": "alice@example.com",
            "created_at": NOW,
        }
    )

    with pytest.raises(IllegalTransition):
        release_status_change(release, new)


def test_failed_goes_back_to_pending_only_by_hand() -> None:
    release = Release.model_validate(
        {
            "app_slug": "sales",
            "release_no": 1,
            "snapshot_digest": digest_of(1),
            "detected": {},
            "status": "failed",
            "status_at": NOW,
            "created_by": "alice@example.com",
            "created_at": NOW,
        }
    )

    with pytest.raises(IllegalTransition, match="only by hand"):
        release_status_change(release, "pending")
    assert release_status_change(release, "pending", by_hand=True).values["status"] == "pending"


# --- the swap's transaction -----------------------------------------------------------------------


async def test_activation_supersedes_the_old_release_before_it_activates_the_new_one() -> None:
    registry = registry_with_app()
    seed_release(registry, "sales", 1, "active")
    seed_release(registry, "sales", 2, "healthy")
    snapshot = registry.snapshot  # type: ignore[attr-defined]

    changes = activation_changes(snapshot.release("sales", 2), snapshot.release("sales", 1))

    # The database allows one active release per app at every statement, so the old one goes first.
    assert [(c.key["release_no"], c.values["status"]) for c in changes] == [
        (1, "superseded"),
        (2, "active"),
    ]
    await registry.write(changes)  # type: ignore[attr-defined]
    assert [r.status for r in registry.snapshot.releases_for("sales")] == [  # type: ignore[attr-defined]
        "superseded",
        "active",
    ]


async def test_the_first_activation_of_an_app_is_one_change() -> None:
    registry = registry_with_app()
    seed_release(registry, "sales", 1, "healthy")
    release = registry.snapshot.release("sales", 1)  # type: ignore[attr-defined]

    changes = activation_changes(release, None)

    assert [(c.key["release_no"], c.values["status"]) for c in changes] == [(1, "active")]


# --- text that reaches the table ------------------------------------------------------------------


def test_a_reason_loses_nul_bytes_and_is_cut_to_its_limit() -> None:
    text = clean_reason("bad\x00 thing " + "x" * 2000)

    assert "\x00" not in text
    assert len(text) == MAX_REASON_CHARS
    assert text.endswith("...")


def test_a_short_reason_is_kept_as_written() -> None:
    assert clean_reason("missing resources: openai-api-key") == "missing resources: openai-api-key"


def test_a_log_tail_keeps_the_last_forty_lines_and_drops_nul_bytes() -> None:
    lines = [f"line {number}\x00" for number in range(100)]

    tail = clean_log_tail("\n".join(lines))

    assert tail.splitlines()[0] == "line 60"
    assert tail.splitlines()[-1] == "line 99"
    assert len(tail.splitlines()) == LOG_TAIL_LINES
    assert "\x00" not in tail


def test_a_log_tail_is_cut_to_its_character_limit_from_the_front() -> None:
    tail = clean_log_tail("a" * (MAX_LOG_TAIL_CHARS * 2) + "END")

    assert len(tail) == MAX_LOG_TAIL_CHARS
    assert tail.endswith("END")


def test_an_empty_log_tail_is_stored_as_nothing() -> None:
    assert clean_log_tail("  \n") is None


def test_the_tail_of_a_failed_start_keeps_fifty_lines() -> None:
    # Contract addendum: a failed start keeps the child's last 50 lines; a build keeps 40.
    lines = [f"line {number}" for number in range(100)]

    tail = clean_log_tail("\n".join(lines), lines=START_LOG_TAIL_LINES)

    assert START_LOG_TAIL_LINES == 50
    assert tail is not None
    assert tail.splitlines()[0] == "line 50"


async def test_a_failed_step_keeps_as_many_lines_as_it_is_given() -> None:
    registry = registry_with_app()
    seed_release(registry, "sales", 1, "starting")
    release = registry.snapshot.release("sales", 1)  # type: ignore[attr-defined]
    log = "\n".join(f"line {number}" for number in range(100))

    change = release_status_change(
        release, "failed", reason="x", log_tail=log, log_tail_lines=START_LOG_TAIL_LINES
    )

    assert len(change.values["build_log_tail"].splitlines()) == START_LOG_TAIL_LINES


def test_a_credential_shape_in_a_log_tail_is_masked_before_it_is_stored() -> None:
    token = "dapi" + "0a1b2c3d" * 4  # built at run time: no credential-shaped literal here

    tail = clean_log_tail(f"connecting with {token}\nDATABRICKS_CLIENT_SECRET=abc123def")

    assert tail is not None
    assert token not in tail
    assert "abc123def" not in tail
    assert tail.splitlines() == ["connecting with ***", "DATABRICKS_CLIENT_SECRET=***"]
