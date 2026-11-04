"""appmux's configuration: the environment read once at start and the ``platform_settings`` schema.

``EnvironmentSettings`` is a Pydantic ``BaseModel`` validated from ``os.environ``;
``pydantic-settings`` is not used because the Databricks Apps environment lacks it
(ADR 0014). ``PlatformSettings`` is the code's list of ``platform_settings`` keys, their
types, defaults and cross-key rules (data model 4.1, rules 10 and 14); the registry layer
feeds it the stored rows as one mapping.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.config import JsonDict

from appmux.sandbox.rules import EgressRule

# One email: a local part, one "@", a domain of dot-separated labels. No white space, comma or
# semicolon anywhere, so a pasted list ("a@b.c;d@e.f") is not one address. Rule 5 of the data
# model adds that emails are lower case; the data model fixes no other shape.
_EMAIL_SHAPE: Final = re.compile(r"[^@\s,;]+@[^@\s,;.]+(?:\.[^@\s,;.]+)+")
_LIST_SEPARATORS: Final = re.compile(r"[,\s]+")
_PLAIN_DECIMAL: Final = re.compile(r"[0-9]+", re.ASCII)

DEFAULT_UI_DIR: Final = Path("web/out")

# The active release, the target, one release in progress and one to clean up must always fit
# beside the locked ones (data model 4.1, rule between keys; rule 10 of section 7).
RELEASES_KEPT_FREE: Final = 4
MAX_REQUEST_TIMEOUT_S: Final = 290  # below the platform proxy's 300 s
# The bounds of one app's memory budget (data model 4.2); the platform default budget shares them.
# The floor is what a person may type now: below it a runtime that reserves address space at start
# (the sandbox caps it at twice the budget) dies or hangs, so 64 MB "worked" only for a bare
# script. The registry row and its database check keep 64 (``registry/models.py``), so an app
# saved under the old floor still loads and runs until someone changes its budget.
MEMORY_BUDGET_MIN_MB: Final = 256
MEMORY_BUDGET_MAX_MB: Final = 16384


class SettingsError(ValueError):
    """The start-up environment is unusable; the message names every variable at fault."""

    def __init__(self, problems: Mapping[str, str]) -> None:
        self.variables: tuple[str, ...] = tuple(problems)
        details = "; ".join(f"{name}: {reason}" for name, reason in problems.items())
        super().__init__(
            f"cannot start, environment variables unusable ({details}); "
            "set them in the App's app.yaml env section and start again"
        )


def normalise_email(value: str) -> str:
    """Return the trimmed lower-case email, or raise ``ValueError`` without echoing the value."""
    email = value.strip().lower()
    if not _EMAIL_SHAPE.fullmatch(email):
        raise ValueError("an entry is not an email address (expected name@domain)")
    return email


_BLANK_MESSAGE: Final = "set but blank"


def _reason(error_type: str, message: str) -> str:
    if error_type == "missing":
        return "not set"
    if error_type == "string_too_short":
        return "set but empty"
    if error_type == "value_error" and message.endswith(_BLANK_MESSAGE):
        return _BLANK_MESSAGE
    return message.removeprefix("Value error, ")


class EnvironmentSettings(BaseModel):
    """Variables the platform sets, plus the two appmux's own ``app.yaml`` declares.

    Fields are named in lower case; each reads the upper-case variable in its alias. Only the
    variables named here are read from the environment. Nothing here has a default that
    resembles a credential: ``DATABRICKS_CLIENT_SECRET`` is required and held as ``SecretStr``
    so it cannot reach a log through ``repr`` or an f-string.
    """

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    databricks_host: str = Field(alias="DATABRICKS_HOST", min_length=1)
    databricks_client_id: str = Field(alias="DATABRICKS_CLIENT_ID", min_length=1)
    databricks_client_secret: SecretStr = Field(alias="DATABRICKS_CLIENT_SECRET", min_length=1)
    databricks_app_port: int = Field(alias="DATABRICKS_APP_PORT", ge=1, le=65535)
    # Both are set for every App (spike 17); the children are told them (design 5.3, research D16).
    databricks_workspace_id: str = Field(alias="DATABRICKS_WORKSPACE_ID", min_length=1)
    databricks_app_url: str = Field(alias="DATABRICKS_APP_URL", min_length=1)
    pghost: str = Field(alias="PGHOST", min_length=1)
    appmux_lakebase_database: str = Field(alias="APPMUX_LAKEBASE_DATABASE", min_length=1)
    # Honoured only while the registry's platform_admins is unset (design 13.1 item 4).
    appmux_bootstrap_admins: list[str] = Field(
        default_factory=list, alias="APPMUX_BOOTSTRAP_ADMINS"
    )
    # Where the admin UI export is served from; relative to the working directory.
    ui_dir: Path = Field(default=DEFAULT_UI_DIR, alias="APPMUX_UI_DIR")

    @field_validator(
        "databricks_host",
        "databricks_client_id",
        "databricks_client_secret",
        "databricks_workspace_id",
        "databricks_app_url",
        "pghost",
        "appmux_lakebase_database",
        mode="before",
    )
    @classmethod
    def white_space_only_is_unusable(cls, value: object) -> object:
        # The empty string is left to min_length so it reads "set but empty". Nothing is trimmed:
        # a secret is kept exactly as given.
        if isinstance(value, str) and value and not value.strip():
            raise ValueError(_BLANK_MESSAGE)
        return value

    @field_validator("databricks_app_port", mode="before")
    @classmethod
    def port_is_plain_decimal_digits(cls, value: object) -> object:
        # Python's int() also takes "8_000", "+80", " 80 " and non-ASCII digits; the platform
        # sets a plain number, so anything else is a mistake worth naming.
        if isinstance(value, str) and _PLAIN_DECIMAL.fullmatch(value) is None:
            raise ValueError("not a plain decimal number")
        return value

    @field_validator("appmux_bootstrap_admins", mode="before")
    @classmethod
    def split_bootstrap_admins(cls, value: object) -> object:
        if isinstance(value, str):
            return [entry for entry in _LIST_SEPARATORS.split(value) if entry]
        return value

    @field_validator("appmux_bootstrap_admins", mode="after")
    @classmethod
    def lower_case_bootstrap_admins(cls, value: list[str]) -> list[str]:
        return [normalise_email(entry) for entry in value]

    @field_validator("ui_dir", mode="before")
    @classmethod
    def blank_ui_dir_means_unset(cls, value: object) -> object:
        # An empty path would silently mean the working directory.
        if isinstance(value, str) and not value.strip():
            return DEFAULT_UI_DIR
        return value

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Validate ``environ`` (``os.environ`` by default); ``SettingsError`` names each fault."""
        source = os.environ if environ is None else environ
        wanted = {
            field.alias: source[field.alias]
            for field in cls.model_fields.values()
            if field.alias is not None and field.alias in source
        }
        try:
            return cls.model_validate(wanted)
        except ValidationError as exc:
            problems: dict[str, str] = {}
            for error in exc.errors(include_input=False, include_url=False):
                variable = str(error["loc"][0]) if error["loc"] else "environment"
                problems.setdefault(variable, _reason(error["type"], error["msg"]))
            raise SettingsError(problems) from exc


class LevelOfEffect(StrEnum):
    """Who a setting reaches (data model rule 9)."""

    ALL_CHILDREN = "all children"
    ONE_CHILD = "one child"
    PER_VISITOR = "per visitor"


# Every platform_settings key reaches all children (data model 4.1); the schema carries it so
# the admin UI and the audit trail can state it without a second table.
_ALL_CHILDREN: Final[JsonDict] = {"level_of_effect": LevelOfEffect.ALL_CHILDREN.value}


class PlatformAdmin(BaseModel):
    """One entry of ``platform_admins``: a user's email or a Databricks group name."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["user", "group"]
    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def lower_case_user_email(cls, name: str, info: ValidationInfo) -> str:
        if info.data.get("type") == "user":
            return normalise_email(name)
        return name


def _default_egress_global() -> list[EgressRule]:
    return [
        EgressRule(
            action="deny",
            cidr="@container_ip/32",
            port="any",
            comment="the container's own address",
        ),
        EgressRule(
            action="deny", cidr="169.254.0.0/16", port="any", comment="metadata and link-local"
        ),
    ]


class PlatformSettings(BaseModel):
    """The keys of ``platform_settings``; an unknown key is rejected (data model rule 14).

    Values are not coerced between types: they come from ``jsonb``, where ``"120"`` is not
    ``120``. Defaults: those the plan fixes, those stated in data model 4.1, and for the three
    keys the documents give only a sample (``memory_reserve_mb``, ``package_mode``,
    ``egress_global``) the sample value; ``default_wheel_sources`` is empty (optional, design
    13.1 item 6) and ``platform_admins`` is unset until the bootstrap admin sets it.
    ``default_memory_budget_mb`` (512) is the budget of a new app registered by request or left
    blank in a direct registration; the bounds are those of one app's budget.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    platform_admins: list[PlatformAdmin] | None = Field(
        default=None, json_schema_extra=_ALL_CHILDREN
    )
    memory_reserve_mb: int = Field(default=1024, ge=1, json_schema_extra=_ALL_CHILDREN)
    default_memory_budget_mb: int = Field(
        default=512,
        ge=MEMORY_BUDGET_MIN_MB,
        le=MEMORY_BUDGET_MAX_MB,
        json_schema_extra=_ALL_CHILDREN,
    )
    package_mode: Literal["index", "wheelhouse", "wheelhouse+index"] = Field(
        default="wheelhouse", json_schema_extra=_ALL_CHILDREN
    )
    default_wheel_sources: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list, json_schema_extra=_ALL_CHILDREN
    )
    shared_sp_delegation_allowed: bool = Field(default=False, json_schema_extra=_ALL_CHILDREN)
    max_releases_per_app: int = Field(
        default=10, ge=RELEASES_KEPT_FREE, json_schema_extra=_ALL_CHILDREN
    )
    max_locked_releases: int = Field(default=3, ge=0, json_schema_extra=_ALL_CHILDREN)
    egress_global: list[EgressRule] = Field(
        default_factory=_default_egress_global, json_schema_extra=_ALL_CHILDREN
    )
    request_timeout_s: int = Field(
        default=120, ge=1, le=MAX_REQUEST_TIMEOUT_S, json_schema_extra=_ALL_CHILDREN
    )
    max_request_body_mb: int = Field(default=100, ge=1, json_schema_extra=_ALL_CHILDREN)
    max_concurrent_requests: int = Field(default=64, ge=1, json_schema_extra=_ALL_CHILDREN)
    build_timeout_s: int = Field(default=600, ge=1, json_schema_extra=_ALL_CHILDREN)
    max_processes_per_child: int = Field(default=256, ge=1, json_schema_extra=_ALL_CHILDREN)
    child_cpu_pin: bool = Field(default=True, json_schema_extra=_ALL_CHILDREN)
    attach_check_warehouse_id: str | None = Field(
        default=None, min_length=1, json_schema_extra=_ALL_CHILDREN
    )

    @field_validator("platform_admins")
    @classmethod
    def keep_at_least_one_user(
        cls, admins: list[PlatformAdmin] | None
    ) -> list[PlatformAdmin] | None:
        if admins is not None and not any(admin.type == "user" for admin in admins):
            raise ValueError("platform_admins must keep at least one entry of type 'user'")
        return admins

    @model_validator(mode="after")
    def locks_leave_room_to_clean_up(self) -> Self:
        ceiling = self.max_releases_per_app - RELEASES_KEPT_FREE
        if self.max_locked_releases > ceiling:
            raise ValueError(
                f"max_locked_releases ({self.max_locked_releases}) must be at most "
                f"max_releases_per_app - {RELEASES_KEPT_FREE} ({ceiling}), so a release "
                "can always be cleaned up"
            )
        return self


def level_of_effect(key: str) -> LevelOfEffect:
    """The level of effect of a ``platform_settings`` key; ``KeyError`` for an unknown key."""
    extra = PlatformSettings.model_fields[key].json_schema_extra
    if not isinstance(extra, dict) or "level_of_effect" not in extra:
        raise LookupError(f"platform setting {key!r} declares no level of effect")
    return LevelOfEffect(str(extra["level_of_effect"]))
