"""The bodies of the admin JSON API: one Pydantic model per schema of contracts/admin-api.md.

The route tasks import these models instead of defining their own; ``tests/contract`` holds them
in step with the contract's tables and examples, and ``web/src/lib/api/schemas/`` is the same
contract in Zod. Every model is frozen, strict (no coercion between JSON types) and refuses a
field the schema does not list. A response model is built by the route with the exact types
(``datetime`` for a ``time``); a request model validates the parsed JSON body.

Wire names: a field is ``snake_case`` as in the contract. The two windows of ``AuditList`` and
``MetricsResponse`` are called ``from`` on the wire, a Python keyword, so the attribute is
``from_`` and the model reads and writes the alias; dump with ``model_dump(mode="json")`` or
``model_dump_json()``, which use the alias, and a ``time`` leaves as ``2026-10-30T09:14:00Z``.

What is checked here is what a request body alone decides: shapes, limits, closed lists, the
rules between fields and a NUL character (Postgres text and jsonb cannot hold one, so a request
carrying it is refused here instead of failing at the database). A rule that needs state (the
app's owner, the resolved container address, a taken slug, the resource catalogue, a stale
``version``) belongs to the route, which uses the two owner helpers and
``refuse_overlapping_allows`` below. A response model is more permissive about a value the
registry has already validated, such as the ``value_from`` key of an env entry read from the
child's ``app.yaml``, which appmux does not control. Secrets never appear in a schema: a secret
is a ``scope/key`` reference.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from ipaddress import IPv4Address
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    ValidationError,
    field_validator,
    model_validator,
)

from appmux.registry.models import LATENCY_BUCKET_COUNT, SecretKey, Slug, WheelEntry
from appmux.sandbox.rules import (
    APPMUX_PORT_PLACEHOLDER,
    CONTAINER_IP_PLACEHOLDER,
    EgressRule,
    EgressRulesRefused,
    refuse_overlapping_allows,
)
from appmux.settings import normalise_email

# --- limits ---------------------------------------------------------------------------------

MEMORY_BUDGET_MIN_MB: Final = 64
MEMORY_BUDGET_MAX_MB: Final = 16384
REASON_MAX_LENGTH: Final = 500
RELEASE_NAME_MAX_LENGTH: Final = 80
RELEASE_TAG_MAX_LENGTH: Final = 32
RELEASE_TAGS_MAX: Final = 10
HEALTH_DEADLINE_MIN_S: Final = 5
HEALTH_DEADLINE_MAX_S: Final = 600
WORKSPACE_PATH_PREFIX: Final = "/Workspace/"
SECRET_TYPE: Final = "secret"  # noqa: S105 - the resource type's name, not a credential
SECRET_PERMISSION: Final = "READ"  # noqa: S105 - a permission level, not a credential
EVERYONE_NAME: Final = "*"
# A response returns the ``value_from`` key the child's app.yaml names, in whatever form; the cap
# only keeps an absurd value out of a page.
ENV_VALUE_FROM_RESPONSE_MAX_LENGTH: Final = 255
NUL_CHARACTER: Final = "\x00"

# The address and port the schema checks an egress list against. The real container address is
# known only to the route, which calls ``refuse_overlapping_allows`` again with it. With this
# probe the schema refuses what holds without knowing the address: an ``allow`` of the
# placeholders and of the link-local range (``0.0.0.0/0`` overlaps that range), exactly what the
# UI refuses. A plain CIDR that covers the container's real address is left to the route.
_PROBE_CONTAINER_IP: Final = IPv4Address("0.0.0.0")  # noqa: S104 - a probe address, never bound
_PROBE_APPMUX_PORT: Final = 1

_SECRET_REFERENCE: Final = re.compile(r"[^/]+/[^/]+")

# --- value checks, each with the message the UI shows ----------------------------------------


def _require_not_blank(text: str) -> str:
    if not text.strip():
        raise ValueError("must not be blank")
    return text


def _require_lower_case_email(text: str) -> str:
    try:
        email = normalise_email(text)
    except ValueError as exc:
        raise ValueError("must be an email address in lower case (name@domain)") from exc
    if email != text:
        raise ValueError("must be an email address in lower case (name@domain)")
    return text


def _require_workspace_path(text: str) -> str:
    if not text.startswith(WORKSPACE_PATH_PREFIX):
        raise ValueError(f"must start with {WORKSPACE_PATH_PREFIX}")
    return text


def _require_leading_slash(text: str) -> str:
    if not text.startswith("/"):
        raise ValueError("must start with /")
    return text


def _require_lower_case(text: str) -> str:
    if text != text.lower():
        raise ValueError("must be lower case")
    return text


def _require_true(flag: bool) -> bool:
    if flag is not True:
        raise ValueError("only true can be asked for")
    return flag


def _refuse_nul(value: object) -> object:
    """Refuse a NUL in a string, at any depth, in a list, or in a key of an object.

    Walked with a stack, not recursion, so a deeply nested body cannot exhaust the interpreter.
    The message never repeats the text.
    """
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if NUL_CHARACTER in item:
                raise ValueError("must not contain a NUL character")
        elif isinstance(item, Mapping):
            for key, child in item.items():
                pending.append(key)
                pending.append(child)
        elif isinstance(item, list | tuple):
            pending.extend(item)
    return value


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


NotBlank = Annotated[str, AfterValidator(_require_not_blank)]
# A strict ``bool`` (``1`` is not ``true``), then the value must be ``true``.
AskedFor = Annotated[bool, AfterValidator(_require_true)]
Reason = Annotated[str, Field(max_length=REASON_MAX_LENGTH), AfterValidator(_require_not_blank)]
LowerCaseEmail = Annotated[str, AfterValidator(_require_lower_case_email)]
WorkspacePath = Annotated[str, AfterValidator(_require_workspace_path)]
MemoryBudgetMb = Annotated[int, Field(ge=MEMORY_BUDGET_MIN_MB, le=MEMORY_BUDGET_MAX_MB)]
# The key of an app resource: the registry's form (``app_resources.key``), defined once there.
ResourceKey = SecretKey
# ISO 8601 UTC with whole seconds and a trailing Z (contract, "Reading the schemas").
UtcTime = Annotated[AwareDatetime, PlainSerializer(_format_time, return_type=str)]

AppState = Literal["stopped", "starting", "running", "crashed"]
DesiredState = Literal["running", "stopped"]
AppPermission = Literal["CAN_USE", "CAN_MANAGE"]
OpenMode = Literal["as_user", "without_token"]
Runtime = Literal["python", "node"]
ReleaseStatus = Literal[
    "pending", "building", "starting", "healthy", "active", "failed", "superseded"
]
AccessAs = Literal["obo", "own_sp", "shared_sp"]
GrantType = Literal["user", "group", "everyone"]
RequestKind = Literal["register", "delegation", "settings"]
RequestStatus = Literal["pending", "approved", "denied", "withdrawn"]
PackageMode = Literal["index", "wheelhouse", "wheelhouse+index"]


class OwnerGrantRefused(ValueError):
    """A grant or removal that would lower or remove the owner; the route answers ``400``."""


class _Body(BaseModel):
    """Base of every schema: frozen, no coercion, no unlisted field, the wire name of a field."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        serialize_by_alias=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


class _RequestBody(_Body):
    """Base of every schema a client sends: a ``_Body`` that also refuses a NUL character."""

    @field_validator("*", mode="before")
    @classmethod
    def _no_nul_character(cls, value: object) -> object:
        return _refuse_nul(value)


# --- shared types ---------------------------------------------------------------------------


class Principal(_Body):
    """Who a list names, as a response returns it (``AdminList``, ``PlatformView.admins``)."""

    type: GrantType
    name: str


def _require_exactly_one_env_source(value: str | None, value_from: str | None) -> None:
    if (value is None) == (value_from is None):
        raise ValueError("exactly one of value and value_from must be set")


class EnvEntry(_Body):
    """One environment entry of a response: exactly one of ``value`` and a ``value_from`` key.

    ``value_from`` is any non-empty string up to a cap: ``detected.env`` is copied from the
    child's ``app.yaml``, which appmux must not change (constitution I), so its keys are not
    held to the form of an app resource key. A client sends ``EnvEntryRequest`` instead.
    """

    name: str = Field(min_length=1)
    value: str | None = None
    value_from: (
        Annotated[str, Field(min_length=1, max_length=ENV_VALUE_FROM_RESPONSE_MAX_LENGTH)] | None
    ) = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        _require_exactly_one_env_source(self.value, self.value_from)
        return self


class EnvEntryRequest(_RequestBody):
    """One environment entry a client sends: ``value_from`` names a resource key of the app."""

    name: str = Field(min_length=1)
    value: str | None = None
    value_from: ResourceKey | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        _require_exactly_one_env_source(self.value, self.value_from)
        return self


def egress_list_problems(
    rules: Sequence[EgressRule], *, container_ip: IPv4Address, appmux_port: int
) -> list[str]:
    """What the editor refuses in a rule list, one sentence per offending rule (empty: accepted).

    The sentences number the rules from 1 and name the placeholders (``@container_ip``,
    ``@appmux_port``) and the link-local range, never the container's address. The schema checks
    with a probe address (``_check_egress_list``); a route calls this with the real one.
    """
    problems = [
        f"rule {number} is an allow on {APPMUX_PORT_PLACEHOLDER}, which stays closed because "
        "self_check() assumes it; make it a deny"
        for number, rule in enumerate(rules, start=1)
        if rule.action == "allow" and rule.port == APPMUX_PORT_PLACEHOLDER
    ]
    problems += [
        f"rule {number}: cidr '{rule.cidr}' needs a prefix length "
        f"(write {rule.cidr}/32 for one address)"
        for number, rule in enumerate(rules, start=1)
        if "/" not in rule.cidr and not rule.cidr.startswith(CONTAINER_IP_PLACEHOLDER)
    ]
    try:
        refuse_overlapping_allows(rules, container_ip=container_ip, appmux_port=appmux_port)
    except EgressRulesRefused as exc:
        problems.extend(exc.problems)
    return problems


def _check_egress_list(rules: list[EgressRule]) -> list[EgressRule]:
    problems = egress_list_problems(
        rules, container_ip=_PROBE_CONTAINER_IP, appmux_port=_PROBE_APPMUX_PORT
    )
    if problems:
        raise EgressRulesRefused(problems)
    return rules


# Each item is an ``EgressRule`` (its own CIDR and port checks), then the list as a whole: a
# CIDR written without a prefix length (``EgressRule`` reads it as a /32; the contract and the UI
# ask for the prefix), and an ``allow`` of a closed target.
EgressRuleRequestList = Annotated[list[EgressRule], AfterValidator(_check_egress_list)]


# --- request bodies -------------------------------------------------------------------------


class OpenModeRequest(_RequestBody):
    mode: OpenMode


class RegisterAppRequest(_RequestBody):
    slug: Slug
    display_name: NotBlank
    source_path: WorkspacePath
    memory_budget_mb: MemoryBudgetMb
    owner: LowerCaseEmail


class UpdateAppRequest(_RequestBody):
    version: int
    slug: Slug | None = None
    display_name: NotBlank | None = None
    source_path: WorkspacePath | None = None
    memory_budget_mb: MemoryBudgetMb | None = None

    @model_validator(mode="after")
    def _a_field_besides_version(self) -> Self:
        changes = (self.slug, self.display_name, self.source_path, self.memory_budget_mb)
        if all(change is None for change in changes):
            raise ValueError("at least one field besides version must be present")
        return self


class SetOwnerRequest(_RequestBody):
    version: int
    owner: LowerCaseEmail


class UpdateReleaseRequest(_RequestBody):
    name: Annotated[str, Field(min_length=1, max_length=RELEASE_NAME_MAX_LENGTH)] | None
    tags: Annotated[
        list[
            Annotated[
                str,
                Field(max_length=RELEASE_TAG_MAX_LENGTH),
                AfterValidator(_require_lower_case),
            ]
        ],
        Field(max_length=RELEASE_TAGS_MAX),
    ]


class UpdateConfigRequest(_RequestBody):
    version: int
    env: list[EnvEntryRequest]
    wheel_sources: list[str]
    health_path: Annotated[str, AfterValidator(_require_leading_slash)]
    health_deadline_s: Annotated[int, Field(ge=HEALTH_DEADLINE_MIN_S, le=HEALTH_DEADLINE_MAX_S)]
    own_sp_client_id_key: ResourceKey | None
    own_sp_client_secret_key: ResourceKey | None

    @model_validator(mode="after")
    def _own_service_principal_keys_come_as_a_pair(self) -> Self:
        if (self.own_sp_client_id_key is None) != (self.own_sp_client_secret_key is None):
            raise ValueError("set both own_sp_*_key fields, or neither")
        return self


class AttachResourceRequest(_RequestBody):
    type: NotBlank
    target: NotBlank
    permission: NotBlank
    access_as: AccessAs | None = None
    from_bundle: bool = False

    @field_validator("from_bundle", mode="before")
    @classmethod
    def _null_is_the_default(cls, value: object) -> object:
        return False if value is None else value

    @model_validator(mode="after")
    def _access_as_follows_the_type(self) -> Self:
        if self.type != SECRET_TYPE:
            if self.access_as is None:
                raise ValueError("access_as is required for every type but secret")
            return self
        if self.access_as is not None:
            raise ValueError("a secret has no access_as")
        if self.permission != SECRET_PERMISSION:
            raise ValueError(f"a secret takes the level {SECRET_PERMISSION}")
        if _SECRET_REFERENCE.fullmatch(self.target) is None:
            raise ValueError("a secret target is a scope/key reference")
        return self


class GrantRequest(_RequestBody):
    type: GrantType
    name: str
    permission: AppPermission

    @model_validator(mode="after")
    def _the_principal_fits_its_type(self) -> Self:
        if self.type == "everyone":
            if self.name != EVERYONE_NAME:
                raise ValueError(f"everyone is named '{EVERYONE_NAME}'")
            if self.permission != "CAN_USE":
                raise ValueError("everyone is CAN_USE only")
        elif self.type == "user":
            _require_lower_case_email(self.name)
        else:
            _require_not_blank(self.name)
            if self.name == EVERYONE_NAME:
                raise ValueError(f"'{EVERYONE_NAME}' names everyone, not a group")
        return self


def refuse_lowering_owner(grant: GrantRequest, *, owner: str) -> None:
    """Refuse a grant that names the app's owner with a level below ``CAN_MANAGE``.

    The owner is always ``CAN_MANAGE`` and is not a grant row (data model rule 1), so a lower
    level for them would contradict ``apps.owner``. Naming them with ``CAN_MANAGE`` changes
    nothing and is not refused. ``owner`` is ``apps.owner``; the two emails are compared in lower
    case, as ``refuse_removing_owner`` does, so a mixed-case argument cannot let the owner go.
    """
    if (
        grant.type == "user"
        and grant.name.lower() == owner.lower()
        and grant.permission != "CAN_MANAGE"
    ):
        raise OwnerGrantRefused(
            "the owner is always CAN_MANAGE and cannot be named with a lower level"
        )


def refuse_removing_owner(principal_type: str, name: str, *, owner: str) -> None:
    """Refuse ``DELETE /apps/{app}/grants/{type}/{name}`` naming the owner (``400``)."""
    if principal_type == "user" and name.lower() == owner.lower():
        raise OwnerGrantRefused("the owner is not a grant row and cannot be removed")


class RegisterPayload(_RequestBody):
    """The payload of a ``register`` request: all four platform fields."""

    slug: Slug
    display_name: NotBlank
    source_path: WorkspacePath
    memory_budget_mb: MemoryBudgetMb


class DelegationPayload(_RequestBody):
    """The payload of a ``delegation`` request: ``obo``, ``shared_sp`` or both; only ``true``."""

    obo: AskedFor | None = None
    shared_sp: AskedFor | None = None

    @model_validator(mode="after")
    def _asks_for_something(self) -> Self:
        if self.obo is None and self.shared_sp is None:
            raise ValueError("ask for obo, shared_sp or both")
        return self


class SettingsPayload(_RequestBody):
    """The payload of a ``settings`` request: any non-empty subset of the platform fields."""

    slug: Slug | None = None
    display_name: NotBlank | None = None
    source_path: WorkspacePath | None = None
    memory_budget_mb: MemoryBudgetMb | None = None

    @model_validator(mode="after")
    def _asks_for_something(self) -> Self:
        if all(value is None for value in self.__dict__.values()):
            raise ValueError("ask for at least one field")
        return self


_PAYLOAD_MODELS: Final[dict[str, type[_RequestBody]]] = {
    "register": RegisterPayload,
    "delegation": DelegationPayload,
    "settings": SettingsPayload,
}


def _payload_problems(error: ValidationError) -> str:
    # Built from loc and msg only: pydantic's own text repeats the input values.
    return "; ".join(
        f"payload{''.join(f'.{part}' for part in item['loc'])}: "
        f"{item['msg'].removeprefix('Value error, ')}"
        for item in error.errors()
    )


class CreatePermissionRequest(_RequestBody):
    kind: RequestKind
    app: Slug
    payload: dict[str, JsonValue]
    reason: Reason

    @model_validator(mode="after")
    def _payload_follows_the_kind(self) -> Self:
        try:
            parsed = _PAYLOAD_MODELS[self.kind].model_validate(self.payload)
        except ValidationError as exc:
            raise ValueError(_payload_problems(exc)) from exc
        if isinstance(parsed, RegisterPayload) and parsed.slug != self.app:
            raise ValueError("app must equal payload.slug")
        return self


class DecidePermissionRequest(_RequestBody):
    reason: str | None = None
    note: str | None = None


class DelegationReasonRequest(_RequestBody):
    """The body of a delegation ``PUT``, and the query parameters of its ``DELETE``."""

    version: int
    reason: Reason

    @classmethod
    def from_query(cls, params: Mapping[str, str]) -> Self:
        """Read the ``DELETE``'s percent-decoded query parameters; ``version`` arrives as text."""
        return cls.model_validate(dict(params), strict=False)


class PutSettingRequest(_RequestBody):
    value: JsonValue


class AdminEntry(_RequestBody):
    """One entry a client sends in the platform admins list: a user or a group, never everyone."""

    type: Literal["user", "group"]
    name: NotBlank

    @model_validator(mode="after")
    def _a_user_is_a_lower_case_email(self) -> Self:
        if self.type == "user":
            _require_lower_case_email(self.name)
        return self


class PutAdminsRequest(_RequestBody):
    admins: list[AdminEntry]
    reason: NotBlank

    @model_validator(mode="after")
    def _the_list_keeps_a_user_and_no_duplicate(self) -> Self:
        seen: set[tuple[str, str]] = set()
        for admin in self.admins:
            key = (admin.type, admin.name.lower())
            if key in seen:
                raise ValueError(f"{admin.type} '{admin.name}' is listed twice")
            seen.add(key)
        if not any(admin.type == "user" for admin in self.admins):
            raise ValueError("at least one user must remain")
        return self


class PutEgressRequest(_RequestBody):
    rules: EgressRuleRequestList


class PutAppEgressRequest(_RequestBody):
    version: int
    rules: EgressRuleRequestList


class PutEgressBody(_RequestBody):
    """``PutEgressRequest`` with the items checked and the list left to the route.

    ``PutEgressRequest`` also checks the list against probe addresses, and a refusal inside a
    model reaches the client as a field path only. The egress routes read these two bodies instead
    and check the list themselves (``egress_list_problems``, with the container's real address), so
    a refusal can name the rule and the closed target.
    """

    rules: list[EgressRule]


class PutAppEgressBody(PutEgressBody):
    """``PutAppEgressRequest`` for the same reason as ``PutEgressBody``."""

    version: int


# --- response bodies ------------------------------------------------------------------------


class ErrorDetail(_Body):
    code: str
    message: str


class ErrorBody(_Body):
    error: ErrorDetail


class MeResponse(_Body):
    email: str
    groups: list[str]
    is_platform_admin: bool


class AppSummary(_Body):
    slug: str
    display_name: str
    state: AppState
    permission: AppPermission
    open_modes: list[OpenMode]


class AppList(_Body):
    items: list[AppSummary]


class AppStateResponse(_Body):
    state: AppState
    desired_state: DesiredState


class ReleaseSummary(_Body):
    release_no: int
    status: ReleaseStatus
    status_reason: str | None
    status_at: UtcTime
    name: str | None
    tags: list[str]
    is_target: bool
    locked_by: str | None
    locked_at: UtcTime | None
    created_by: str
    created_at: UtcTime

    @model_validator(mode="after")
    def _locked_at_goes_with_locked_by(self) -> Self:
        if (self.locked_by is None) != (self.locked_at is None):
            raise ValueError("locked_at is null exactly when locked_by is")
        return self


class BundleResource(_Body):
    key: str
    type: str
    target: str | None
    permission: str


class DetectedManifest(_Body):
    """``ReleaseDetail.detected``: what appmux found in the snapshot."""

    runtime: Runtime
    framework: str | None
    command: list[str]
    env: list[EnvEntry]
    bundle_resources: list[BundleResource]
    warnings: list[str]


class ReleaseConfigView(_Body):
    """``ReleaseDetail.config``: what the app admin added without touching the source."""

    env: list[EnvEntry]
    wheel_sources: list[str]


class ReleaseDetail(ReleaseSummary):
    # ``snapshot_digest`` is ``sha256:`` and 64 hex digits; the registry's ``Release`` row model
    # holds that form, and a response is built from a row it has already validated.
    snapshot_digest: str
    build_log_tail: str | None
    detected: DetectedManifest
    config: ReleaseConfigView
    wheels: list[WheelEntry]


class ReleaseCreated(_Body):
    release_no: int
    status: Literal["pending"]


class ReleaseUnchanged(_Body):
    status: Literal["up_to_date"]
    release_no: int


class SourceFileEntry(_Body):
    path: str
    size_bytes: int
    kind: Literal["text", "binary"]


class ReleaseSourceListing(_Body):
    release_no: int
    items: list[SourceFileEntry]


class ReleaseSourceFile(_Body):
    release_no: int
    path: str
    size_bytes: int
    content: str


class ConfigChangeResult(_Body):
    config_version: int
    release_no: int | None


class Requirement(_Body):
    key: str
    state: Literal["attached", "missing"]
    offer: BundleResource | None
    suggested_type: str | None


class RequirementsResponse(_Body):
    release_no: int | None
    items: list[Requirement]
    unreferenced_offers: list[BundleResource]


class ResourceView(_Body):
    key: str
    type: str
    target: str
    permission: str
    access_as: AccessAs | None
    check_result: Literal["ok", "unverified"]
    attached_by: str
    attached_at: UtcTime

    @model_validator(mode="after")
    def _access_as_is_null_exactly_for_a_secret(self) -> Self:
        if (self.access_as is None) != (self.type == SECRET_TYPE):
            raise ValueError("access_as is null exactly for a secret")
        return self


class AppGrant(_Body):
    type: GrantType
    name: str
    permission: AppPermission
    granted_by: str
    granted_at: UtcTime


class DelegationStatus(_Body):
    obo_delegated: bool
    shared_sp_delegated: bool
    shared_sp_delegation_allowed: bool


class AppConfigView(_Body):
    """``AppDetail.config``: what the app admin controls."""

    desired_state: DesiredState
    target_release_no: int | None
    env: list[EnvEntry]
    wheel_sources: list[str]
    health_path: str
    health_deadline_s: int
    own_sp_client_id_key: str | None
    own_sp_client_secret_key: str | None
    version: int
    updated_by: str
    updated_at: UtcTime


class AppDetail(_Body):
    slug: str
    display_name: str
    owner: str
    source_path: str
    memory_budget_mb: int
    state: AppState
    runtime: Runtime | None
    restart_to_apply: bool
    version: int
    created_by: str
    created_at: UtcTime
    updated_by: str
    updated_at: UtcTime
    releases: list[ReleaseSummary]
    requirements: RequirementsResponse
    resources: list[ResourceView]
    config: AppConfigView
    delegation: DelegationStatus
    egress: list[EgressRule]
    grants: list[AppGrant]


class RequestRow(_Body):
    request_id: int
    app_slug: str | None
    kind: RequestKind
    payload: dict[str, JsonValue]
    reason: str
    requested_by: str
    requested_at: UtcTime
    status: RequestStatus
    closed_by: str | None
    closed_at: UtcTime | None
    closed_note: str | None
    current: dict[str, JsonValue] | None
    requires_reason: bool
    app_version: int | None

    @model_validator(mode="after")
    def _closed_fields_follow_the_status(self) -> Self:
        if (self.status == "pending") != (self.closed_at is None):
            raise ValueError("closed_at is null exactly while the request is pending")
        if (self.closed_at is None) != (self.closed_by is None):
            raise ValueError("closed_by is null exactly while the request is pending")
        return self


class RequestList(_Body):
    items: list[RequestRow]


class SelfCheckEntry(_Body):
    name: str
    passed: bool
    detail: str | None


class SelfCheckResult(_Body):
    passed: bool
    ran_at: UtcTime
    duration_ms: int
    checks: list[SelfCheckEntry]

    @model_validator(mode="after")
    def _one_failed_check_fails_the_result(self) -> Self:
        if self.passed and not all(check.passed for check in self.checks):
            raise ValueError("passed is true only when every check passed")
        return self


class PlatformSettingsView(_Body):
    """``PlatformView.settings``: the effective value of each settable key."""

    memory_reserve_mb: int
    package_mode: PackageMode
    default_wheel_sources: list[str]
    shared_sp_delegation_allowed: bool
    max_releases_per_app: int
    max_locked_releases: int
    request_timeout_s: int
    max_request_body_mb: int
    max_concurrent_requests: int
    build_timeout_s: int
    max_processes_per_child: int
    child_cpu_pin: bool
    attach_check_warehouse_id: str | None


class MemoryView(_Body):
    """``PlatformView.memory``: the slot's memory."""

    limit_mb: int
    reserve_mb: int
    committed_mb: int
    free_mb: int


class PlatformApp(_Body):
    slug: str
    display_name: str
    source_path: str
    memory_budget_mb: int
    owner: str
    obo_delegated: bool
    shared_sp_delegated: bool
    egress: list[EgressRule]
    version: int


class PlatformView(_Body):
    settings: PlatformSettingsView
    admins: list[Principal]
    egress_global: list[EgressRule]
    self_check: SelfCheckResult
    memory: MemoryView
    apps: list[PlatformApp]


class PlatformSetting(_Body):
    key: str
    value: JsonValue
    updated_by: str
    updated_at: UtcTime


class AdminList(_Body):
    items: list[Principal]


class EgressRules(_Body):
    items: list[EgressRule]


class AuditRow(_Body):
    id: int
    ts: UtcTime
    actor: str
    action: str
    app_slug: str | None
    detail: dict[str, JsonValue]
    reason: str | None


class AuditList(_Body):
    from_: UtcTime = Field(alias="from")
    to: UtcTime
    items: list[AuditRow]


class MetricsRow(_Body):
    app_slug: str
    minute: UtcTime
    requests: int
    r2xx: int
    r3xx: int
    r4xx: int
    r5xx: int
    upstream_errors: int
    limited: int
    lat_hist: Annotated[
        list[int], Field(min_length=LATENCY_BUCKET_COUNT, max_length=LATENCY_BUCKET_COUNT)
    ]
    bytes_in: int
    bytes_out: int
    in_flight_max: int
    ws_opened: int
    ws_open: int
    rss_mb_max: int
    restarts: int
    egress_bytes_out: int
    egress_bytes_in: int
    egress_dropped: int
    egress_dropped_by_rule: dict[str, int]
    mem_used_mb: int | None
    mem_limit_mb: int | None
    children_running: int | None
    children_registered: int | None
    admission_rejections: int | None
    registry_errors: int | None
    registry_ms_max: int | None
    edge_rss_mb: int | None


class MetricsResponse(_Body):
    from_: UtcTime = Field(alias="from")
    to: UtcTime
    truncated: bool
    items: list[MetricsRow]


# The steps ``GET /metrics`` and ``GET /metrics/breakdown`` add minutes up to (T193).
MetricStep = Literal["1m", "5m", "15m", "1h"]


class MetricsPoint(_Body):
    """One step of a series of ``MetricsBreakdown``; ``minute`` is the start of the step."""

    minute: UtcTime
    requests: int
    r4xx: int
    r5xx: int
    rss_mb_max: int
    bytes_out: int
    egress_dropped: int
    lat_hist: Annotated[
        list[int], Field(min_length=LATENCY_BUCKET_COUNT, max_length=LATENCY_BUCKET_COUNT)
    ]


class MetricsSeries(_Body):
    app_slug: str
    display_name: str | None  # null for an app that is no longer registered
    points: list[MetricsPoint]


class MetricsBreakdown(_Body):
    from_: UtcTime = Field(alias="from")
    to: UtcTime
    step: MetricStep
    series: list[MetricsSeries]
    other: list[MetricsPoint] | None
