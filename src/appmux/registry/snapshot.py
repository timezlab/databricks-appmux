"""The registry's in-memory copy: an immutable snapshot and the loader that keeps it fresh.

Design 4.3: the small tables are re-read every few seconds and after each write; with Lakebase down
the last snapshot keeps serving and the error is reported; with Lakebase down at startup the first
load raises ``RegistryUnavailable`` so the caller shows the status page (fail closed, nothing is
served from an empty copy).

Seven tables are in the snapshot. ``metrics`` and ``audit_log`` are not: metrics are write-heavy
and read by window, and the audit log is kept forever, so neither belongs in a copy re-read every
few seconds. The registry reads them through its own methods (``appmux.registry.protocol``).

The tables are read one after the other, not in one transaction, so a snapshot can show a change to
one table and not yet the change to another that came with it (a renamed app and the old slug in
``app_grants``); the next read repairs it. Nothing that decides access needs two tables to agree: a
grant whose app is missing grants nothing.

A row that fails its model in ``registry/models.py`` is quarantined: left out of the snapshot,
counted and reported with its table and primary key, never its content, because the database cannot
check the shape of a ``jsonb`` value and a holder of the shared-SP token can write a malformed one
(data model 6). Leaving a row out fails closed: a grant that does not load grants nothing, and an
app that does not load has no route.

This module reads rows through ``RowSource`` and opens no connection; ``psycopg`` is the registry
client's business.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property
from types import MappingProxyType
from typing import Any, Final, NoReturn, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError

from appmux.registry.errors import RegistryUnavailable
from appmux.registry.models import (
    ROW_MODELS,
    App,
    AppConfig,
    AppGrant,
    AppResource,
    PermissionRequest,
    PlatformSetting,
    Release,
    RowModel,
)

logger = logging.getLogger(__name__)

# The seven tables of the snapshot, in the order they are read.
SNAPSHOT_TABLES: Final[tuple[str, ...]] = (
    "platform_settings",
    "apps",
    "permission_requests",
    "app_config",
    "app_grants",
    "releases",
    "app_resources",
)

# The primary key of each table (migrations/0001_baseline.sql): it names a row without showing it.
PRIMARY_KEYS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "platform_settings": ("key",),
        "apps": ("slug",),
        "permission_requests": ("request_id",),
        "app_config": ("app_slug",),
        "app_grants": ("app_slug", "principal_type", "principal"),
        "releases": ("app_slug", "release_no"),
        "app_resources": ("app_slug", "key"),
        "metrics": ("app_slug", "minute"),
        "audit_log": ("id",),
    }
)

DEFAULT_POLL_INTERVAL_S: Final = 5.0
_MAX_KEY_LENGTH: Final = 64
_UNKNOWN_KEY_PART: Final = "?"
_UNEXPECTED_ERROR_MESSAGE: Final = "the registry could not be read; see the log"

_Row = TypeVar("_Row", bound=RowModel)
_Frozen = TypeVar("_Frozen")


def _read_only(*_args: object, **_kwargs: object) -> NoReturn:
    raise TypeError(
        "the registry snapshot is read-only; take a copy with copy.deepcopy() to edit one"
    )


class FrozenList(list[Any]):
    """A ``list`` that refuses every change, so ``isinstance(x, list)`` still holds for readers.

    A frozen pydantic model only blocks assignment to its fields; a ``list`` field can still be
    appended to. The snapshot is shared by the edge, the admin API and the reconciler, so one
    reader editing ``release.detected.command`` in place would change the start command every
    other reader sees. ``copy`` and ``deepcopy`` return a plain, editable ``list``.
    """

    __slots__ = ()

    append = extend = insert = remove = pop = clear = sort = reverse = _read_only
    __setitem__ = __delitem__ = __iadd__ = __imul__ = _read_only

    def __reduce__(self) -> tuple[Any, ...]:
        return (list, (list(self),))


class FrozenDict(dict[Any, Any]):
    """A ``dict`` that refuses every change; the counterpart of ``FrozenList``."""

    __slots__ = ()

    update = setdefault = pop = popitem = clear = _read_only
    __setitem__ = __delitem__ = __ior__ = _read_only

    def __reduce__(self) -> tuple[Any, ...]:
        return (dict, (dict(self),))


def deep_freeze(value: _Frozen) -> _Frozen:
    """A copy of ``value`` in which no list, dict, set or model field can be changed in place.

    Lists, dicts and sets inside it (and inside pydantic models, including a model's extra keys)
    become ``FrozenList``, ``FrozenDict`` and ``frozenset``; models are copied, not edited. The
    caller's own objects stay as they were. The return type is the argument's type: a frozen list
    is still a ``list`` to a type checker, and refuses a change when the program runs.
    """
    frozen: Any
    if isinstance(value, BaseModel):
        frozen = value.model_copy()
        for name, field_value in list(frozen.__dict__.items()):
            frozen.__dict__[name] = deep_freeze(field_value)
        if frozen.__pydantic_extra__ is not None:
            object.__setattr__(frozen, "__pydantic_extra__", deep_freeze(frozen.__pydantic_extra__))
    elif isinstance(value, list):
        frozen = FrozenList(deep_freeze(item) for item in value)
    elif isinstance(value, dict):
        frozen = FrozenDict((key, deep_freeze(item)) for key, item in value.items())
    elif isinstance(value, tuple):
        frozen = tuple(deep_freeze(item) for item in value)
    elif isinstance(value, set | frozenset):
        frozen = frozenset(deep_freeze(item) for item in value)
    else:
        frozen = value
    return cast(_Frozen, frozen)


class RowSource(Protocol):
    """Where rows come from; the psycopg client implements it.

    ``fetch_rows`` returns every row of one table as a mapping of column name to value, and raises
    ``RegistryUnavailable`` when the database cannot be reached.
    """

    async def fetch_rows(self, table: str) -> Sequence[Mapping[str, object]]: ...


@dataclass(frozen=True, slots=True)
class QuarantinedRow:
    """A row left out of the snapshot. It names the row (table and key) and shows nothing of it."""

    table: str
    key: str


def _key_part(value: object) -> str:
    text = _UNKNOWN_KEY_PART if value is None else str(value)
    printable = "".join(character if character.isprintable() else "?" for character in text)
    return printable[:_MAX_KEY_LENGTH]


def _quarantine_key(table: str, row: Mapping[str, object]) -> str:
    parts = "/".join(_key_part(row.get(column)) for column in PRIMARY_KEYS[table])
    return parts[:_MAX_KEY_LENGTH]


@dataclass(frozen=True)
class RegistrySnapshot:
    """Every row of the seven tables as frozen models, at one point in time (``loaded_at``).

    The fields are tuples; the lookups below answer from indexes built on first use. Nothing here
    can be changed, so a reader that keeps a reference keeps a consistent view while a newer
    snapshot is already being served.
    """

    loaded_at: datetime
    platform_settings: tuple[PlatformSetting, ...] = ()
    apps: tuple[App, ...] = ()
    permission_requests: tuple[PermissionRequest, ...] = ()
    app_configs: tuple[AppConfig, ...] = ()
    app_grants: tuple[AppGrant, ...] = ()
    releases: tuple[Release, ...] = ()
    app_resources: tuple[AppResource, ...] = ()
    quarantined: tuple[QuarantinedRow, ...] = field(default=())

    @cached_property
    def _settings_by_key(self) -> Mapping[str, PlatformSetting]:
        return MappingProxyType({row.key: row for row in self.platform_settings})

    @cached_property
    def _apps_by_slug(self) -> Mapping[str, App]:
        return MappingProxyType({row.slug: row for row in self.apps})

    @cached_property
    def _configs_by_slug(self) -> Mapping[str, AppConfig]:
        return MappingProxyType({row.app_slug: row for row in self.app_configs})

    @cached_property
    def _requests_by_id(self) -> Mapping[int, PermissionRequest]:
        return MappingProxyType({row.request_id: row for row in self.permission_requests})

    @cached_property
    def _grants_by_slug(self) -> Mapping[str, tuple[AppGrant, ...]]:
        grouped: defaultdict[str, list[AppGrant]] = defaultdict(list)
        for grant in self.app_grants:
            grouped[grant.app_slug].append(grant)
        return MappingProxyType(
            {
                slug: tuple(sorted(rows, key=lambda g: (g.principal_type, g.principal)))
                for slug, rows in grouped.items()
            }
        )

    @cached_property
    def _releases_by_slug(self) -> Mapping[str, tuple[Release, ...]]:
        grouped: defaultdict[str, list[Release]] = defaultdict(list)
        for release in self.releases:
            grouped[release.app_slug].append(release)
        return MappingProxyType(
            {
                slug: tuple(sorted(rows, key=lambda r: r.release_no))
                for slug, rows in grouped.items()
            }
        )

    @cached_property
    def _resources_by_slug(self) -> Mapping[str, tuple[AppResource, ...]]:
        grouped: defaultdict[str, list[AppResource]] = defaultdict(list)
        for resource in self.app_resources:
            grouped[resource.app_slug].append(resource)
        return MappingProxyType(
            {slug: tuple(sorted(rows, key=lambda r: r.key)) for slug, rows in grouped.items()}
        )

    def setting(self, key: str) -> PlatformSetting | None:
        return self._settings_by_key.get(key)

    def app(self, slug: str) -> App | None:
        return self._apps_by_slug.get(slug)

    def app_config(self, slug: str) -> AppConfig | None:
        return self._configs_by_slug.get(slug)

    def request(self, request_id: int) -> PermissionRequest | None:
        return self._requests_by_id.get(request_id)

    def grants_for(self, slug: str) -> tuple[AppGrant, ...]:
        """The app's grants ordered by principal type and principal; empty when there are none."""
        return self._grants_by_slug.get(slug, ())

    def releases_for(self, slug: str) -> tuple[Release, ...]:
        """The app's releases, deleted ones included, oldest first."""
        return self._releases_by_slug.get(slug, ())

    def release(self, slug: str, release_no: int) -> Release | None:
        for release in self.releases_for(slug):
            if release.release_no == release_no:
                return release
        return None

    def resources_for(self, slug: str) -> tuple[AppResource, ...]:
        """The app's resources ordered by key."""
        return self._resources_by_slug.get(slug, ())

    def resource(self, slug: str, key: str) -> AppResource | None:
        for resource in self.resources_for(slug):
            if resource.key == key:
                return resource
        return None


def _typed(rows: Sequence[RowModel], model: type[_Row]) -> tuple[_Row, ...]:
    # Frozen copies, so a reader cannot edit a list or dict inside a row (see ``deep_freeze``).
    return tuple(deep_freeze(row) for row in rows if isinstance(row, model))


def build_snapshot(
    rows_by_table: Mapping[str, Sequence[RowModel]],
    *,
    loaded_at: datetime,
    quarantined: tuple[QuarantinedRow, ...] = (),
) -> RegistrySnapshot:
    """Assemble a snapshot from validated rows; shared by the loader and by test doubles."""
    return RegistrySnapshot(
        loaded_at=loaded_at,
        platform_settings=_typed(rows_by_table.get("platform_settings", ()), PlatformSetting),
        apps=tuple(sorted(_typed(rows_by_table.get("apps", ()), App), key=lambda a: a.slug)),
        permission_requests=tuple(
            sorted(
                _typed(rows_by_table.get("permission_requests", ()), PermissionRequest),
                key=lambda r: r.request_id,
            )
        ),
        app_configs=_typed(rows_by_table.get("app_config", ()), AppConfig),
        app_grants=_typed(rows_by_table.get("app_grants", ()), AppGrant),
        releases=_typed(rows_by_table.get("releases", ()), Release),
        app_resources=_typed(rows_by_table.get("app_resources", ()), AppResource),
        quarantined=quarantined,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SnapshotLoader:
    """Reads the seven tables through a ``RowSource`` and serves the latest good snapshot.

    * ``refresh()`` reads all tables, validates each row with its model, quarantines the ones that
      fail and swaps the snapshot in whole. Call it once at startup and after every write.
    * The first ``refresh()`` raises ``RegistryUnavailable`` when the source fails: there is no
      snapshot to serve, and the caller shows the status page. A later failure keeps the last good
      snapshot, sets ``last_error`` and does not raise; the next success clears it.
    * ``poll()`` runs forever (cancel it to stop): sleep the interval, then ``refresh()``.
    """

    def __init__(
        self,
        source: RowSource,
        *,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._source = source
        self._poll_interval_s = poll_interval_s
        self._sleep = sleep
        self._clock = clock
        self._snapshot: RegistrySnapshot | None = None
        self._last_error: str | None = None
        self._reported: frozenset[QuarantinedRow] = frozenset()
        # One reload at a time, so a slow poll cannot install its older read over a newer one.
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> RegistrySnapshot:
        """The latest good snapshot; raises ``RegistryUnavailable`` until one has been loaded."""
        if self._snapshot is None:
            raise RegistryUnavailable("the registry has not been read yet; try again in a moment")
        return self._snapshot

    @property
    def last_error(self) -> str | None:
        """Why the latest reload failed, in words fit for a status page; ``None`` when it worked."""
        return self._last_error

    async def refresh(self) -> None:
        async with self._lock:
            try:
                snapshot = await self._read()
            except RegistryUnavailable as exc:
                self._last_error = exc.message
                if self._snapshot is None:
                    raise
                logger.warning("registry reload failed; serving the last snapshot: %s", exc.message)
                return
            self._snapshot = snapshot
            self._last_error = None

    async def poll(self) -> None:
        """Reload every interval until cancelled; a failed reload never ends the loop."""
        while True:
            await self._sleep(self._poll_interval_s)
            try:
                await self.refresh()
            except RegistryUnavailable:
                # Only reachable before the first load; the caller of poll() loads first, and a
                # later poll will try again, so there is nothing more to do than keep last_error.
                continue
            except Exception:
                # A broad catch is safe here: this is the last-resort guard of a background loop,
                # the failure is logged with its traceback, and the last good snapshot still serves.
                logger.exception("unexpected error while reloading the registry")
                self._last_error = _UNEXPECTED_ERROR_MESSAGE

    async def _read(self) -> RegistrySnapshot:
        rows_by_table: dict[str, list[RowModel]] = {}
        quarantined: list[QuarantinedRow] = []
        for table in SNAPSHOT_TABLES:
            model = ROW_MODELS[table]
            valid: list[RowModel] = []
            for raw in await self._source.fetch_rows(table):
                try:
                    valid.append(model.model_validate(raw))
                except ValidationError:
                    # Only the table and key are kept: the error text repeats the rejected input.
                    quarantined.append(QuarantinedRow(table, _quarantine_key(table, raw)))
            rows_by_table[table] = valid
        self._report(quarantined)
        return build_snapshot(
            rows_by_table, loaded_at=self._clock(), quarantined=tuple(quarantined)
        )

    def _report(self, quarantined: list[QuarantinedRow]) -> None:
        """Log each newly quarantined row once, not at every poll."""
        current = frozenset(quarantined)
        for row in sorted(current - self._reported, key=lambda r: (r.table, r.key)):
            logger.warning(
                "registry row quarantined (fails its model): table=%s key=%s", row.table, row.key
            )
        self._reported = current
