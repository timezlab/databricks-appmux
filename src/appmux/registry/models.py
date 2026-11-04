"""One frozen row model per registry table (docs/product/v1-data-model.md, section 4).

Field names equal column names, so a row read with ``dict_row`` validates with
``Model.model_validate(row)``. Each class is named after its table in the singular:
``PlatformSetting``, ``App``, ``PermissionRequest``, ``AppConfig``, ``AppGrant``, ``Release``,
``AppResource``, ``MetricRow`` and ``AuditLogRow``; ``ROW_MODELS`` maps the table name to its class.

Every ``jsonb`` column is a typed model. A row model repeats each ``CHECK`` constraint of the
baseline migration that concerns one row, and ``tests/unit/test_baseline_migration.py`` holds the
two in step with a refusal table and an acceptance table. The lists the code owns (setting keys,
request kinds and their payload keys, audit actions and their detail keys, the keys of a detected
manifest, the rules for tags, which permission a resource type allows) are not checked here: the
catalogues of later tasks and the write-side models (``appmux.registry.audit.AuditEntry``) check
them before a row is written, so a row with an unknown kind or key still loads and nothing treats
it as approved.

The database cannot check the *shape* of a ``jsonb`` value, so a row written by a holder of the
shared-SP token (data model section 6) can still be refused here, for example a ``releases`` row
whose ``detected`` has a ``command`` that is not a list. That is intended: the model is stricter
than the SQL for a typed value, and the reader that loads a table (the snapshot, T019) must
quarantine a row that fails its model, report it and carry on, never fail the whole table.

Nothing here opens a connection.
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    StringConstraints,
    model_validator,
)

from appmux.sandbox.rules import EgressRule
from appmux.settings import PlatformAdmin

# The forms of ``apps.slug`` and of a secret scope key (data model 4.2 and 4.8). The write-side
# models in ``audit.py`` import them, so each form is defined once.
SLUG_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,30}$"
SCOPE_KEY_PATTERN: Final = r"^[a-z0-9][a-z0-9._-]{0,62}$"
PLATFORM_METRICS_SLUG: Final = "_platform"
LATENCY_BUCKET_COUNT: Final = 11
# How a service principal is named in ``app_grants``: its application id, a UUID in lower case
# (ADR 0020). Migration 0003 repeats it as a CHECK; the admin schema imports it from here.
APPLICATION_ID_PATTERN: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _require_lower_case(text: str) -> str:
    if text != text.lower():
        raise ValueError("must be lower case")
    return text


LowerText = Annotated[str, AfterValidator(_require_lower_case)]
Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]
# `btrim(x) <> ''` in SQL: btrim trims spaces only, so a tab alone is not blank to the database.
NonBlankText = Annotated[str, StringConstraints(pattern=r"[^ ]")]
SecretKey = Annotated[str, StringConstraints(pattern=SCOPE_KEY_PATTERN)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Tolerant(BaseModel):
    """A jsonb value whose keys the code's catalogues own: an unknown key is not an error."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class RowModel(_Frozen):
    """Base of the nine row models: frozen, and a column that does not exist is an error."""


# --- typed jsonb values -------------------------------------------------------------------------


# The JSON shapes the settings of data model 4.1 take. Which shape belongs to which key is the
# settings catalogue's rule, not this model's. ``PlatformAdmin`` is defined in ``appmux.settings``
# and ``EgressRule`` in ``appmux.sandbox.rules``: one definition of each stored shape.
SettingValue = bool | int | str | list[str] | list[PlatformAdmin] | list[EgressRule]


class RequestPayload(BaseModel):
    """What a request asks for. The SQL asks only that it is a JSON object.

    The keys of the three kinds of data model 4.3 are named for convenience and hold whatever JSON
    was stored; any other key is kept too. Which keys a kind requires, and their types, belong to
    the request catalogue, which validates a payload before it is acted on. A request of a kind
    the code does not know therefore loads, and nothing approves it.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    slug: JsonValue = None
    display_name: JsonValue = None
    source_path: JsonValue = None
    memory_budget_mb: JsonValue = None
    obo: JsonValue = None
    shared_sp: JsonValue = None


class EnvEntry(_Tolerant):
    """An environment entry: exactly one of a literal ``value`` and a ``value_from`` key."""

    name: Annotated[str, StringConstraints(min_length=1)]
    value: str | None = None
    value_from: str | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> EnvEntry:
        if (self.value is None) == (self.value_from is None):
            raise ValueError(f"env entry '{self.name}' needs exactly one of value and value_from")
        return self


class BundleResource(_Tolerant):
    """A resource the snapshot's ``databricks.yml`` declares: an offer, never a grant."""

    key: str
    type: str
    target: str | None
    permission: str


class DetectedManifest(_Tolerant):
    """What appmux found in a snapshot (data model 4.6, ``releases.detected``).

    The SQL asks only for a JSON value, so no key is required here: the detection code of a later
    task decides which keys it writes, and a row with ``{}`` loads.
    """

    runtime: str | None = None
    framework: str | None = None
    command: list[str] = Field(default_factory=list)
    env: list[EnvEntry] = Field(default_factory=list)
    bundle_resources: list[BundleResource] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ReleaseConfig(_Tolerant):
    """What the app admin adds without touching the child's source (``releases.config``)."""

    env: list[EnvEntry] = Field(default_factory=list)
    wheel_sources: list[str] = Field(default_factory=list)


class WheelEntry(_Tolerant):
    """A wheel the install used (``releases.wheels``)."""

    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    filename: Annotated[str, StringConstraints(min_length=1)]
    source: Annotated[str, StringConstraints(min_length=1)]


class DroppedByRule(RootModel[dict[str, int]]):
    """Packets dropped per rule id: ``app:2`` is the app's second rule, ``global:1`` the first."""

    model_config = ConfigDict(frozen=True)


# --- the nine tables ----------------------------------------------------------------------------


class PlatformSetting(RowModel):
    key: str
    value: SettingValue
    updated_by: str
    updated_at: AwareDatetime


class App(RowModel):
    slug: Slug
    display_name: NonBlankText
    owner: LowerText
    source_path: Annotated[str, StringConstraints(pattern=r"^/Workspace/")]
    # The bounds the database checks (``migrations/0001_baseline.sql``), wider than the floor of a
    # new budget (``settings.MEMORY_BUDGET_MIN_MB``) so an app stored below it keeps loading.
    memory_budget_mb: Annotated[int, Field(ge=64, le=16384)]
    obo_delegated: bool = False
    shared_sp_delegated: bool = False
    egress: list[EgressRule] = Field(default_factory=list)
    version: int = 1
    created_by: str
    created_at: AwareDatetime
    updated_by: str
    updated_at: AwareDatetime


class PermissionRequest(RowModel):
    request_id: int
    app_slug: str | None
    kind: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,31}$")]
    payload: RequestPayload = Field(default_factory=RequestPayload)
    reason: Annotated[str, StringConstraints(pattern=r"[^ ]", max_length=500)]
    requested_by: str
    requested_at: AwareDatetime
    status: Literal["pending", "approved", "denied", "withdrawn"] = "pending"
    closed_by: str | None = None
    closed_at: AwareDatetime | None = None
    closed_note: str | None = None

    @model_validator(mode="after")
    def _check_constraints(self) -> PermissionRequest:
        if self.app_slug is None and "slug" not in self.payload.model_fields_set:
            raise ValueError("a request to register names its slug in the payload")
        if (self.status == "pending") != (self.closed_at is None):
            raise ValueError("closed_at is null exactly while the request is pending")
        if (self.closed_at is None) != (self.closed_by is None):
            raise ValueError("closed_at and closed_by are set together")
        return self


class AppConfig(RowModel):
    app_slug: str
    desired_state: Literal["running", "stopped"] = "stopped"
    target_release_no: int | None = None
    health_path: Annotated[str, StringConstraints(pattern=r"^/")] = "/"
    health_deadline_s: Annotated[int, Field(ge=5, le=600)] = 60
    own_sp_client_id_key: str | None = None
    own_sp_client_secret_key: str | None = None
    version: int = 1
    updated_by: str
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _own_service_principal_keys_come_as_a_pair(self) -> AppConfig:
        if (self.own_sp_client_id_key is None) != (self.own_sp_client_secret_key is None):
            raise ValueError("own_sp_client_id_key and own_sp_client_secret_key are set together")
        return self


class AppGrant(RowModel):
    app_slug: str
    principal_type: Literal["user", "group", "everyone", "service_principal"]
    principal: str
    permission: Literal["CAN_USE", "CAN_MANAGE"]
    granted_by: str
    granted_at: AwareDatetime

    @model_validator(mode="after")
    def _check_principal(self) -> AppGrant:
        if (self.principal_type == "everyone") != (self.principal == "*"):
            raise ValueError("the principal is '*' exactly for the type 'everyone'")
        if self.principal_type == "user" and self.principal != self.principal.lower():
            raise ValueError("a user principal is an email in lower case")
        # A stored CAN_MANAGE is not refused here: like `everyone`, the policy reads it as CAN_USE.
        if (
            self.principal_type == "service_principal"
            and re.fullmatch(APPLICATION_ID_PATTERN, self.principal) is None
        ):
            raise ValueError(
                "a service principal is named by its application id, a UUID in lower case"
            )
        return self


class Release(RowModel):
    app_slug: str
    release_no: Annotated[int, Field(gt=0)]
    snapshot_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    detected: DetectedManifest
    config: ReleaseConfig = Field(default_factory=ReleaseConfig)
    wheels: list[WheelEntry] = Field(default_factory=list)
    status: Literal[
        "pending", "building", "starting", "healthy", "active", "failed", "superseded"
    ] = "pending"
    status_reason: str | None = None
    build_log_tail: str | None = None
    status_at: AwareDatetime
    label: Annotated[str, StringConstraints(min_length=1, max_length=80)] | None = None
    tags: list[str] = Field(default_factory=list)  # the rules for a tag are the tag catalogue's
    locked_by: str | None = None
    locked_at: AwareDatetime | None = None
    created_by: str
    created_at: AwareDatetime
    deleted_by: str | None = None
    deleted_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check_constraints(self) -> Release:
        if (self.deleted_at is None) != (self.snapshot_digest is not None):
            raise ValueError("a live release has a snapshot and a deleted one has none")
        if (self.deleted_at is None) != (self.deleted_by is None):
            raise ValueError("deleted_at and deleted_by are set together")
        if (self.locked_at is None) != (self.locked_by is None):
            raise ValueError("locked_at and locked_by are set together")
        if self.deleted_at is not None and self.locked_at is not None:
            raise ValueError("a release is unlocked before it is deleted")
        return self


class AppResource(RowModel):
    app_slug: str
    key: SecretKey
    type: str
    target: NonBlankText
    permission: str
    access_as: Literal["obo", "own_sp", "shared_sp"] | None
    check_result: Literal["ok", "unverified"]
    attached_by: str
    attached_at: AwareDatetime

    @model_validator(mode="after")
    def _check_constraints(self) -> AppResource:
        is_secret = self.type == "secret"
        if is_secret != (self.access_as is None):
            raise ValueError("access_as is empty exactly for a secret")
        if is_secret and re.fullmatch(r"[^/]+/[^/]+", self.target) is None:
            raise ValueError("the target of a secret is a 'scope/key' reference")
        if self.access_as == "shared_sp" and self.check_result != "ok":
            raise ValueError("shared_sp needs a verified check")
        return self


class MetricRow(RowModel):
    app_slug: str
    minute: AwareDatetime
    requests: int = 0
    r2xx: int = 0
    r3xx: int = 0
    r4xx: int = 0
    r5xx: int = 0
    upstream_errors: int = 0
    limited: int = 0
    lat_hist: Annotated[
        list[int], Field(min_length=LATENCY_BUCKET_COUNT, max_length=LATENCY_BUCKET_COUNT)
    ]
    bytes_in: int = 0
    bytes_out: int = 0
    in_flight_max: int = 0
    ws_opened: int = 0
    ws_open: int = 0
    rss_mb_max: int = 0
    restarts: int = 0
    egress_bytes_out: int = 0
    egress_bytes_in: int = 0
    egress_dropped: int = 0
    egress_dropped_by_rule: DroppedByRule = Field(default_factory=lambda: DroppedByRule({}))
    # The next eight are filled on the '_platform' row only.
    mem_used_mb: int | None = None
    mem_limit_mb: int | None = None
    children_running: int | None = None
    children_registered: int | None = None
    admission_rejections: int | None = None
    registry_errors: int | None = None
    registry_ms_max: int | None = None
    edge_rss_mb: int | None = None

    @model_validator(mode="after")
    def _platform_columns_belong_to_the_platform_row(self) -> MetricRow:
        if (self.app_slug == PLATFORM_METRICS_SLUG) != (self.mem_used_mb is not None):
            raise ValueError(
                "the platform columns are filled on the '_platform' row and only there"
            )
        return self


class AuditLogRow(RowModel):
    id: int
    ts: AwareDatetime
    actor: str
    action: str  # one of the names of data model 9, checked by AuditEntry when a row is written
    app_slug: str | None = None
    detail: dict[str, JsonValue] = Field(default_factory=dict)
    reason: str | None = None


ROW_MODELS: Final[dict[str, type[RowModel]]] = {
    "platform_settings": PlatformSetting,
    "apps": App,
    "permission_requests": PermissionRequest,
    "app_config": AppConfig,
    "app_grants": AppGrant,
    "releases": Release,
    "app_resources": AppResource,
    "metrics": MetricRow,
    "audit_log": AuditLogRow,
}
