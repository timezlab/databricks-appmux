"""The resource catalogue, the attach rules, and the ``Change`` values the resource routes write.

The catalogue lives in code (data model 4.7), so a new Databricks type is a code change; the table
``app_resources`` does not constrain ``type``. Each ``ResourceType`` states the levels the type
allows (a resource's ``permission`` is one of them, data model rule 5), whether an attach check
exists for it (spike 16: eight of the fourteen types have one, the other six are stored
``unverified``), and whether that check runs a statement on a SQL warehouse.

Everything here is pure: the rules take plain values and the builders return ``Change`` values.
The calls to Databricks and the routes are ``admin/resources.py``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal, Protocol

from pydantic import SecretStr

from appmux.registry.models import SCOPE_KEY_PATTERN, App, AppConfig, AppResource
from appmux.registry.protocol import Change
from appmux.registry.snapshot import RegistrySnapshot
from appmux.registry.writers import WriteKind
from appmux.settings import PlatformSettings

SECRET_TYPE: Final = "secret"  # noqa: S105 - the resource type's name, not a credential
SECRET_LEVEL: Final = "READ"  # noqa: S105 - a permission level, not a credential

_RESOURCE_KEY: Final = re.compile(SCOPE_KEY_PATTERN)


def is_resource_key(text: str) -> bool:
    """Whether ``text`` has the form of ``app_resources.key``: what ``valueFrom`` can name."""
    return _RESOURCE_KEY.fullmatch(text) is not None


@dataclass(frozen=True, slots=True)
class ResourceType:
    """One row of the catalogue of data model 4.7.

    ``levels`` are appmux's spelling of the Databricks level names, lowest first where Databricks
    orders them (a serving endpoint: view, query, manage). ``is_verifiable`` is true for the eight
    types with an attach check; ``needs_check_warehouse`` marks the checks that run a statement on
    the warehouse of the ``attach_check_warehouse_id`` setting (the ``sql_warehouse`` check runs on
    the resource's own warehouse and needs no setting). ``label`` is the noun a refusal uses.
    """

    name: str
    label: str
    levels: tuple[str, ...]
    is_verifiable: bool
    needs_check_warehouse: bool = False


def _types(*types: ResourceType) -> Mapping[str, ResourceType]:
    return MappingProxyType({resource_type.name: resource_type for resource_type in types})


RESOURCE_TYPES: Final[Mapping[str, ResourceType]] = _types(
    ResourceType(
        SECRET_TYPE, "secret", (SECRET_LEVEL,), is_verifiable=True, needs_check_warehouse=True
    ),
    ResourceType("sql_warehouse", "SQL warehouse", ("CAN_USE", "CAN_MANAGE"), is_verifiable=True),
    ResourceType(
        "mlflow_experiment",
        "MLflow experiment",
        ("CAN_READ", "CAN_EDIT", "CAN_MANAGE"),
        is_verifiable=False,
    ),
    ResourceType(
        "serving_endpoint",
        "serving endpoint",
        ("CAN_VIEW", "CAN_QUERY", "CAN_MANAGE"),
        is_verifiable=True,
    ),
    ResourceType(
        "lakebase_database",
        "Lakebase database",
        ("CAN_CONNECT_AND_CREATE",),
        is_verifiable=False,
    ),
    ResourceType("job", "job", ("CAN_VIEW", "CAN_MANAGE_RUN", "CAN_MANAGE"), is_verifiable=False),
    ResourceType("uc_table", "table", ("SELECT", "MODIFY"), is_verifiable=True),
    ResourceType("uc_volume", "volume", ("READ_VOLUME", "WRITE_VOLUME"), is_verifiable=True),
    ResourceType(
        "uc_function", "function", ("EXECUTE",), is_verifiable=True, needs_check_warehouse=True
    ),
    ResourceType("uc_connection", "connection", ("USE_CONNECTION",), is_verifiable=True),
    ResourceType(
        "genie_space",
        "Genie space",
        ("CAN_VIEW", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"),
        is_verifiable=True,
    ),
    ResourceType("vector_search_index", "AI Search index", ("SELECT",), is_verifiable=False),
    ResourceType("databricks_app", "Databricks app", ("CAN_USE",), is_verifiable=False),
    ResourceType("gateway_model_service", "model service", ("EXECUTE",), is_verifiable=False),
)

VERIFIABLE_TYPES: Final = frozenset(
    name for name, resource_type in RESOURCE_TYPES.items() if resource_type.is_verifiable
)


def levels_of(type_name: str) -> tuple[str, ...] | None:
    """The levels of a catalogue type, or ``None`` for a name the catalogue does not hold."""
    resource_type = RESOURCE_TYPES.get(type_name)
    return None if resource_type is None else resource_type.levels


# --- the attach check: verdicts, the seam to Databricks, and the rules ---------------------------

CheckResult = Literal["ok", "unverified"]

ACCESS_AS_VALUES: Final = ("obo", "own_sp", "shared_sp")
CHECK_WAREHOUSE_SETTING: Final = "attach_check_warehouse_id"
SHARED_SP_SWITCH_SETTING: Final = "shared_sp_delegation_allowed"
UNVERIFIED_SHARED_SP_MESSAGE: Final = (
    "cannot verify access to this resource type; shared_sp requires a verified check"
)

_MAX_TARGET_LENGTH: Final = 512
_NAME_PART: Final = r"[A-Za-z0-9_-]{1,255}"

# The forms below are what the check calls can carry safely: a target is put into a SQL statement
# or a URL path, so only these characters get there (no quote, backtick, space, slash or dot-dot).
_TARGET_FORMS: Final[Mapping[str, tuple[re.Pattern[str], str]]] = MappingProxyType(
    {
        SECRET_TYPE: (
            re.compile(r"[A-Za-z0-9_.@-]{1,128}/[A-Za-z0-9_.-]{1,128}"),
            "a secret target is <scope>/<key>: a secret scope and a key joined by one slash, "
            "using letters, digits and the characters . _ -",
        ),
        "sql_warehouse": (
            re.compile(r"[A-Za-z0-9_-]{1,64}"),
            "a SQL warehouse target is its id: letters, digits, _ and -, at most 64 characters",
        ),
        "serving_endpoint": (
            re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"),
            "a serving endpoint target is its name: letters, digits and . _ -, "
            "starting with a letter or digit",
        ),
        "uc_table": (
            re.compile(rf"{_NAME_PART}\.{_NAME_PART}\.{_NAME_PART}"),
            "a table target is <catalog>.<schema>.<table>, using letters, digits, _ and -",
        ),
        "uc_function": (
            re.compile(rf"{_NAME_PART}\.{_NAME_PART}\.{_NAME_PART}"),
            "a function target is <catalog>.<schema>.<function>, using letters, digits, _ and -",
        ),
        "uc_volume": (
            re.compile(rf"/Volumes/{_NAME_PART}/{_NAME_PART}/{_NAME_PART}"),
            "a volume target looks like /Volumes/<catalog>/<schema>/<volume>, "
            "using letters, digits, _ and -",
        ),
        "uc_connection": (
            re.compile(_NAME_PART),
            "a connection target is its name: letters, digits, _ and -",
        ),
        "genie_space": (
            re.compile(r"[A-Za-z0-9_-]{1,64}"),
            "a Genie space target is its id: letters, digits, _ and -, at most 64 characters",
        ),
    }
)


class CheckVerdict(StrEnum):
    """What the attacher's own token could do: the answer of one attach check."""

    OK = "ok"
    UNVERIFIED = "unverified"
    NO_ACCESS = "no_access"


class AttachRefused(Exception):
    """The attach is refused and nothing is written. ``code`` is stable; ``message`` is for people.

    The message names what failed and what to do next. It never repeats a submitted value, a
    token or a response body from Databricks.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AttachCheckUnavailable(Exception):
    """The check could not run (Databricks did not answer, or the answer says nothing on access).

    It is never read as a pass: the attach does not happen and the person may try again.
    """


@dataclass(frozen=True, slots=True)
class AttachProbe:
    """One check to run, with the attacher's token.

    ``warehouse_id`` is the ``attach_check_warehouse_id`` setting, given only to the types that
    run a statement on it. The token is a ``SecretStr``, so a repr or a log line shows no value.
    """

    resource_type: str
    target: str
    permission: str
    token: SecretStr
    warehouse_id: str | None


class AttachChecker(Protocol):
    """Asks Databricks whether the holder of the token can use the resource.

    The seam of the attach check: ``admin/resources.py`` has the HTTP adapter, tests have a stub.
    It answers ``OK``, ``UNVERIFIED`` (the check cannot tell, for example because the App does not
    declare the scope it needs) or ``NO_ACCESS``; it raises ``AttachCheckUnavailable`` when it
    could not ask.
    """

    async def check(self, probe: AttachProbe) -> CheckVerdict: ...


def no_access_message(type_name: str, target: str) -> str:
    """The refusal for a person whose token cannot use the resource (design 8.3)."""
    if type_name == SECRET_TYPE:
        scope = target.split("/", 1)[0]
        return f"you do not have {SECRET_LEVEL} on secret scope '{scope}'"
    resource_type = RESOURCE_TYPES.get(type_name)
    noun = resource_type.label if resource_type is not None else "resource"
    return f"you do not have access to this {noun}"


def own_sp_key_in_use(config: AppConfig, key: str) -> bool:
    """Whether the app's own-service-principal pair names this resource key."""
    return key in (config.own_sp_client_id_key, config.own_sp_client_secret_key)


def own_sp_pair_problem(
    id_key: str | None, secret_key: str | None, resources: Iterable[AppResource]
) -> str | None:
    """Why a pair of own-SP keys cannot stand, or ``None`` (data model rule 6).

    Both unset is fine (the app has no own service principal). A key that is set must be the key
    of a ``secret`` resource of the app. The message names the field, never the key.
    """
    by_key = {resource.key: resource for resource in resources}
    for field_name, key in (
        ("own_sp_client_id_key", id_key),
        ("own_sp_client_secret_key", secret_key),
    ):
        if key is None:
            continue
        resource = by_key.get(key)
        if resource is None:
            return f"{field_name} names no resource of this app; attach the secret first"
        if resource.type != SECRET_TYPE:
            return f"{field_name} must name a secret resource, not a {_label_of(resource.type)}"
    return None


def own_sp_keys_problem(config: AppConfig, resources: Iterable[AppResource]) -> str | None:
    """``own_sp_pair_problem`` over the keys of a stored config."""
    return own_sp_pair_problem(
        config.own_sp_client_id_key, config.own_sp_client_secret_key, resources
    )


def check_warehouse_id(snapshot: RegistrySnapshot) -> str | None:
    """The SQL warehouse the secret and function checks run on, or ``None`` when unset.

    A setting whose value is not usable text counts as unset: the check then asks the platform
    admin to set it instead of sending a statement to a warehouse id nobody chose.
    """
    row = snapshot.setting(CHECK_WAREHOUSE_SETTING)
    value: object = PlatformSettings().attach_check_warehouse_id if row is None else row.value
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def shared_sp_delegation_allowed(snapshot: RegistrySnapshot) -> bool:
    """The platform switch for ``shared_sp`` (off until a platform admin turns it on)."""
    row = snapshot.setting(SHARED_SP_SWITCH_SETTING)
    if row is not None and isinstance(row.value, bool):
        return row.value
    return PlatformSettings().shared_sp_delegation_allowed


async def verify_attach(
    *,
    resource_type: str,
    target: str,
    permission: str,
    access_as: str | None,
    app: App,
    config: AppConfig,
    resources: Iterable[AppResource],
    shared_sp_allowed: bool,
    warehouse_id: str | None,
    token: SecretStr | None,
    checker: AttachChecker,
) -> CheckResult:
    """Apply the attach rules and run the check with the attacher's token (FR-037).

    Returns the ``check_result`` to store. Raises ``AttachRefused`` (nothing is written) for a
    rule of the design or for a token without access, and ``AttachCheckUnavailable`` when the
    check could not run. Every rule that needs no call is applied before the first call.
    """
    catalogue = RESOURCE_TYPES.get(resource_type)
    if catalogue is None:
        raise AttachRefused(
            "unknown_type",
            f"unknown resource type; use one of: {', '.join(sorted(RESOURCE_TYPES))}",
        )
    if permission not in catalogue.levels:
        raise AttachRefused(
            "bad_permission",
            f"the permission of a {catalogue.label} is one of: {', '.join(catalogue.levels)}",
        )
    _require_target_form(catalogue, target)
    _require_access_as(catalogue, access_as, app, config, tuple(resources), shared_sp_allowed)

    if not catalogue.is_verifiable:
        if access_as == "shared_sp":
            raise AttachRefused("unverified_shared_sp", UNVERIFIED_SHARED_SP_MESSAGE)
        return "unverified"

    check_warehouse = (warehouse_id or "").strip() or None
    if catalogue.needs_check_warehouse and check_warehouse is None:
        raise AttachRefused(
            "check_warehouse_unset",
            f"the {CHECK_WAREHOUSE_SETTING} setting is not set, so access to a "
            f"{catalogue.label} cannot be checked; ask a platform admin to set it",
        )
    if token is None:
        raise AttachRefused(
            "token_missing",
            "appmux did not receive your Databricks token, so it cannot check your access; "
            "open appmux from the Databricks Apps list and try again",
        )

    verdict = await checker.check(
        AttachProbe(
            resource_type=resource_type,
            target=target,
            permission=permission,
            token=token,
            warehouse_id=check_warehouse if catalogue.needs_check_warehouse else None,
        )
    )
    if verdict is CheckVerdict.NO_ACCESS:
        raise AttachRefused("no_access", no_access_message(resource_type, target))
    if verdict is CheckVerdict.UNVERIFIED:
        if access_as == "shared_sp":
            raise AttachRefused("unverified_shared_sp", UNVERIFIED_SHARED_SP_MESSAGE)
        return "unverified"
    return "ok"


def is_valid_target(type_name: str, target: str) -> bool:
    """Whether ``target`` has the form the attach check of a verifiable type can carry safely.

    A type with no check has no form beyond printable text (``verify_attach`` states that rule);
    this answers ``True`` only for a checked type whose pattern the target matches.
    """
    form = _TARGET_FORMS.get(type_name)
    return form is not None and form[0].fullmatch(target) is not None


def _require_target_form(catalogue: ResourceType, target: str) -> None:
    form = _TARGET_FORMS.get(catalogue.name)
    if form is not None:
        pattern, hint = form
        if pattern.fullmatch(target) is None:
            raise AttachRefused("bad_target", hint)
        return
    if not target.strip() or not target.isprintable() or len(target) > _MAX_TARGET_LENGTH:
        raise AttachRefused(
            "bad_target",
            f"the target of a {catalogue.label} is printable text of at most "
            f"{_MAX_TARGET_LENGTH} characters",
        )


def _require_access_as(
    catalogue: ResourceType,
    access_as: str | None,
    app: App,
    config: AppConfig,
    resources: tuple[AppResource, ...],
    shared_sp_allowed: bool,
) -> None:
    if catalogue.name == SECRET_TYPE:
        if access_as is not None:
            raise AttachRefused("access_as_invalid", "a secret has no access_as")
        return
    if access_as not in ACCESS_AS_VALUES:
        raise AttachRefused(
            "access_as_invalid", f"access_as is one of: {', '.join(ACCESS_AS_VALUES)}"
        )
    # No gate on obo: declaring it is setting, and the app's obo delegation only decides, at
    # request time, whether a visitor's token reaches the child (edge/open_mode.py and
    # authz/policy.py). The attach check still proves the attacher's own token reaches the object.
    if access_as == "shared_sp":
        if not app.shared_sp_delegated:
            raise AttachRefused(
                "delegation_missing",
                "access_as shared_sp needs the shared_sp delegation of this app; request it first",
            )
        if not shared_sp_allowed:
            raise AttachRefused(
                "delegation_missing",
                f"the platform does not allow shared_sp (setting {SHARED_SP_SWITCH_SETTING} "
                "is off); ask a platform admin",
            )
    if access_as == "own_sp":
        if config.own_sp_client_id_key is None or config.own_sp_client_secret_key is None:
            raise AttachRefused(
                "own_sp_keys_missing",
                "access_as own_sp needs own_sp_client_id_key and own_sp_client_secret_key in "
                "the app configuration, each naming a secret resource of the app",
            )
        problem = own_sp_keys_problem(config, resources)
        if problem is not None:
            raise AttachRefused("own_sp_keys_invalid", problem)


def _label_of(type_name: str) -> str:
    resource_type = RESOURCE_TYPES.get(type_name)
    return resource_type.label if resource_type is not None else "resource of another type"


# --- the writes of the resource routes ---------------------------------------------------------


def put_resource_change(
    existing: AppResource | None,
    *,
    app_slug: str,
    key: str,
    type_name: str,
    target: str,
    permission: str,
    access_as: str | None,
    check_result: str,
    attached_by: str,
    attached_at: datetime,
) -> Change:
    """The write that attaches a resource: an insert, or an update of the row under that key.

    Attaching again under a key replaces the row, because the check is run again by the person who
    attaches (``attached_by`` is the person whose token made the check). It always writes.
    """
    columns: dict[str, object] = {
        "type": type_name,
        "target": target,
        "permission": permission,
        "access_as": access_as,
        "check_result": check_result,
        "attached_by": attached_by,
        "attached_at": attached_at,
    }
    if existing is None:
        return Change(
            WriteKind.APP_RESOURCE_INSERT, values={"app_slug": app_slug, "key": key, **columns}
        )
    return Change(
        WriteKind.APP_RESOURCE_UPDATE,
        key={"app_slug": existing.app_slug, "key": existing.key},
        values=columns,
    )


def delete_resource_change(existing: AppResource) -> Change:
    """The write that removes a stored resource."""
    return Change(
        WriteKind.APP_RESOURCE_DELETE, key={"app_slug": existing.app_slug, "key": existing.key}
    )
