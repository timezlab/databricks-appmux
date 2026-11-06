"""The audit entry and the INSERT that records it (data model 4.9).

Rule 15 of the data model: a secret value never appears in ``audit_log.detail``. Two things make
that hold here. ``AuditDetail`` lists every key it accepts and none of them is named for a value
(there is no ``value``, ``secret``, ``token`` or ``env``), so an unlisted key is refused. And every
free-text field (the actor, the owner, the app slug, the reason and each name or path in the detail)
has a length limit and a pattern for its form, and refuses a string shaped like a credential, so a
value pasted into a name, a path or a reason does not get through either. The fields with a closed
vocabulary (``kind``, ``type``, ``permission``, ``by``, ``setting``, ``digest``, ``locks``,
``access_as``, ``check`` and the action) are not run through that check: each holds a short word, a
count or a digest, and its pattern refuses anything else. The credential check knows the shapes of
common credentials, not all of them: the field list is the guarantee, the check a net under it.

This module opens no connection. ``insert_statement`` returns the SQL and its parameters, and the
registry client runs them in the transaction of the change that is being recorded.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Final, Literal, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from appmux.registry.models import SCOPE_KEY_PATTERN, SLUG_PATTERN

AuditAction = Literal[
    "app.register",
    "app.delete",
    "app.rename",
    "app.view_as_user",
    "app.set_owner",
    "app.edit_platform_fields",
    "app.start",
    "app.stop",
    "app.restart",
    "request.create",
    "request.withdraw",
    "request.approve",
    "request.deny",
    "delegation.grant",
    "delegation.revoke",
    "platform_admin.add",
    "platform_admin.remove",
    "grant.app",
    "egress.edit",
    "settings.edit",
    "resource.attach",
    "resource.detach",
    "config.edit",
    "release.create",
    "release.activate",
    "release.tag",
    "release.untag",
    "release.label",
    "release.lock",
    "release.unlock",
    "release.delete",
    "release.view_source",
    "app.view_logs",
    "platform.service_principal",
]
AUDIT_ACTIONS: Final[tuple[str, ...]] = get_args(AuditAction)

# Data model 4.9 and 4.3: these actions pass a risk through a gate and carry a mandatory reason.
# Approving a request needs one only when the payload holds a gate field, which the request
# catalogue knows and this module does not, so request.approve is checked there.
ACTIONS_REQUIRING_REASON: Final[frozenset[str]] = frozenset(
    {"delegation.grant", "delegation.revoke", "platform_admin.add", "platform_admin.remove"}
)

# An email in lower case, as the data model stores them (``apps.owner = lower(apps.owner)``). The
# local part is limited to the characters of a real address so that "https://user:pw@host" is not
# an email, and the domain needs a dot so that "token@host" is not one either.
EMAIL_PATTERN: Final = r"[a-z0-9._%+'-]{1,64}@[a-z0-9][a-z0-9.-]*\.[a-z]{2,}"
# An email, "db:<role>" for a manual change, or "appmux" for automatic clean-up.
ACTOR_PATTERN: Final = rf"^(appmux|db:[a-z_][a-z0-9_]{{0,62}}|{EMAIL_PATTERN})$"
# ``audit_log.app_slug`` has no foreign key, so ``SLUG_PATTERN`` (imported from the row models,
# where ``apps.slug`` is defined) is all that keeps a stray value out of the column.

# A principal is "user:<email>", "everyone:*", "group:<name>" or "service_principal:<application
# id>" (ADR 0020). A Databricks group name may hold spaces and any other printable character
# ("Data Platform"), so only a control character, a newline (including U+0085, U+2028 and U+2029)
# or a length outside 1 to 255 is refused there; a name shaped like a credential is refused by the
# check that every principal goes through. The user, everyone and service principal forms keep
# their no-whitespace shape.
_PRINTABLE_GROUP_NAME: Final = r"[^\x00-\x1f\x7f-\x9f\u2028\u2029]{1,255}"
PRINCIPAL_PATTERN: Final = (
    rf"^((user|everyone|service_principal):\S{{1,255}}|group:{_PRINTABLE_GROUP_NAME})$"
)

_MAX_EMAIL_LENGTH: Final = 254  # the longest address SMTP carries
_MAX_TEXT_LENGTH: Final = 512
_MAX_REASON_LENGTH: Final = 500

# A match must not start inside a longer word, but "\b" is the wrong test for that: "_" is a word
# character, so "\b" does not fire in "env_dapi..." or "client_secret". A letter or a digit before
# the match is the only thing that makes it part of another word.
_NOT_AFTER_ALNUM: Final = r"(?<![A-Za-z0-9])"
# A name that says its value is a secret, in any of the spellings it has in an environment
# variable, a config key or a header: client_secret, DATABRICKS_CLIENT_SECRET, access_token, ...
_CREDENTIAL_NAME: Final = (
    r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|credential|authorization)"
)

# Every shape fails closed: a longer or odder spelling of a credential is refused too. A plain word
# like "secret" or "token" with no "=" or ":" and a value after it is not a shape, so an audit
# reason can still say that a secret was rotated.
#
# A child's output is masked in the event loop every app shares, so each shape runs in time linear
# in the text (review T205 R1). The two shapes that can start many times inside one run of
# characters (a JSON web token's "eyJ", a credential name) start only where that run starts: the
# match then takes the first "eyJ" or name in the run, ``kept`` is the text before it, and each
# later part takes its run whole and never gives any of it back. Each start in one run reaches the
# same end of the run, so it has the same outcome: these shapes match exactly what the plain forms
# ("eyJ[...]{8,}\.[...]{8,}\." and NAME[\w.-]*...) match, but scan a run once instead of once for
# each start in it.
_BASE64URL: Final = "[A-Za-z0-9_-]"
_NAME_CHARACTER: Final = r"[\w.-]"
_DATABRICKS_TOKEN_SHAPES: Final = (
    re.compile(r"dapi[0-9a-fA-F]{16,}"),  # Databricks personal access token
    re.compile(r"dose[0-9a-fA-F]{16,}"),  # Databricks OAuth client secret
)
_CREDENTIAL_VALUE_SHAPES: Final = (
    *_DATABRICKS_TOKEN_SHAPES,
    re.compile(  # JSON web token: its header and payload
        rf"(?<!{_BASE64URL})(?>(?P<kept>{_BASE64URL}*?)eyJ)"
        rf"{_BASE64URL}{{8,}}+\.{_BASE64URL}{{8,}}+\."
    ),
    re.compile(_NOT_AFTER_ALNUM + r"bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    # Vendor API keys.
    re.compile(_NOT_AFTER_ALNUM + r"(?:sk|pk|ghp|gho|ghs|xox[abps])[-_][A-Za-z0-9_-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),  # a password in a URL
)
# NAME=value and NAME: value, also quoted ("NAME": "value") and with a longer name around it.
# ``kept`` is the whole name and its separator, which masking keeps.
_NAMED_CREDENTIAL: Final = re.compile(
    rf"(?<!{_NAME_CHARACTER})"
    rf"(?P<kept>(?>{_NAME_CHARACTER}*?{_CREDENTIAL_NAME}){_NAME_CHARACTER}*+"
    r"""["']?\s*[=:]\s*["']?)\S+""",
    re.IGNORECASE,
)
_CREDENTIAL_SHAPES: Final = (*_CREDENTIAL_VALUE_SHAPES, _NAMED_CREDENTIAL)
_MASK: Final = "***"


def _refuse_credential_shape(text: str) -> str:
    # The message names no part of the value: a refusal must not leak what it refused.
    if any(shape.search(text) for shape in _CREDENTIAL_SHAPES):
        raise ValueError(
            "looks like a credential; an audit entry holds names and references, never a value"
        )
    return text


def mask_credentials(text: str) -> str:
    """``text`` with every credential shape above replaced by ``***``, for output kept to be read.

    A child's output and a build's log are shown to the people who manage the app, so what this
    module refuses in an audit entry is masked there. The value shapes go first, so that in
    ``Authorization: Bearer <token>`` the token is gone before the name rule masks one word; a
    ``NAME=value`` keeps its name. Like the refusal, it knows common shapes, not all of them.
    """
    for shape in _CREDENTIAL_SHAPES:
        text = shape.sub(_masked_match, text)
    return text


def _masked_match(found: re.Match[str]) -> str:
    return (found.groupdict().get("kept") or "") + _MASK


def _refuse_token_shape(text: str) -> str:
    # A slug is a name the platform admin picked and shows in a URL, so a vendor prefix such as
    # "sk-" or "pk-" is a legal start ("sk-learn-demo"). Only the Databricks token shapes are
    # refused there; they are the credentials this system holds.
    if any(shape.search(text) for shape in _DATABRICKS_TOKEN_SHAPES):
        raise ValueError("looks like a token; an audit entry holds names, never a value")
    return text


SafeText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=_MAX_TEXT_LENGTH),
    AfterValidator(_refuse_credential_shape),
]
SafeWord = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^\S+$"),
    AfterValidator(_refuse_credential_shape),
]
SafeEmail = Annotated[
    str,
    StringConstraints(max_length=_MAX_EMAIL_LENGTH, pattern=f"^{EMAIL_PATTERN}$"),
    AfterValidator(_refuse_credential_shape),
]
SafeActor = Annotated[
    str,
    StringConstraints(max_length=_MAX_EMAIL_LENGTH, pattern=ACTOR_PATTERN),
    AfterValidator(_refuse_credential_shape),
]
SafeSlug = Annotated[
    str,
    StringConstraints(pattern=SLUG_PATTERN),
    AfterValidator(_refuse_token_shape),
]
Reason = Annotated[
    str,
    StringConstraints(min_length=1, max_length=_MAX_REASON_LENGTH, pattern=r"\S"),
    AfterValidator(_refuse_credential_shape),
]
# The application id of a service principal (a UUID on Databricks). The pattern is wider than a
# UUID so that a test environment with another id still starts; it has no space, no slash and no
# way to hold a credential, and the credential check runs on top (ADR 0022).
ClientId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,128}$"),
    AfterValidator(_refuse_credential_shape),
]


class _AuditModel(BaseModel):
    # hide_input_in_errors: pydantic's own error text repeats the rejected input, which would put
    # the very value this module refuses into a log line.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class ChangedFields(_AuditModel):
    """The ``apps`` and ``app_config`` columns an approval or an edit changes, before or after."""

    slug: SafeWord | None = None
    display_name: SafeText | None = None
    source_path: SafeText | None = None
    memory_budget_mb: int | None = None
    obo: bool | None = None
    shared_sp: bool | None = None
    health_path: SafeText | None = None
    health_deadline_s: int | None = None


class AuditDetail(_AuditModel):
    """What changed. Each key is one the data model's examples use; a new key is a new field here.

    ``from_`` and ``automatic`` are stored under the names ``from`` and ``automatic``.
    """

    request: int | None = None
    kind: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,31}$")] | None = None
    release: int | None = None
    locks: Annotated[str, StringConstraints(pattern=r"^\d+ of \d+$")] | None = None
    owner: SafeEmail | None = None
    budget_mb: int | None = None
    principal: (
        Annotated[
            str,
            StringConstraints(pattern=PRINCIPAL_PATTERN),
            AfterValidator(_refuse_credential_shape),
        ]
        | None
    ) = None
    permission: Annotated[str, StringConstraints(pattern=r"^[A-Z_]{1,32}$")] | None = None
    key: (
        Annotated[
            str,
            StringConstraints(pattern=SCOPE_KEY_PATTERN),
            AfterValidator(_refuse_credential_shape),
        ]
        | None
    ) = None
    type: Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,40}$")] | None = None
    target: SafeWord | None = None
    access_as: Literal["obo", "own_sp", "shared_sp"] | None = None
    check: Literal["ok", "unverified"] | None = None
    from_: SafeWord | None = Field(default=None, alias="from")
    to: SafeWord | None = None
    by: Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,32}$")] | None = None
    cap: int | None = None
    path: SafeText | None = None
    digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{4,64}$")] | None = None
    setting: Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,64}$")] | None = None
    client_id: ClientId | None = None
    previous_client_id: ClientId | None = None
    before: ChangedFields | None = None
    after: ChangedFields | None = None
    automatic: bool | None = None

    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, populate_by_name=True
    )

    @model_validator(mode="after")
    def _secret_target_is_a_scope_and_key(self) -> AuditDetail:
        # Data model 4.7: a secret resource's target is "scope/key", a reference, never a value.
        if self.type != "secret" or self.target is None:
            return self
        if re.fullmatch(r"[^/\s]+/[^/\s]+", self.target) is None:
            raise ValueError("the target of a secret resource must be a 'scope/key' reference")
        return self


class AuditEntry(_AuditModel):
    """One administrative action, ready to be written in the transaction of the change itself."""

    actor: SafeActor
    action: AuditAction
    app_slug: SafeSlug | None = None
    detail: AuditDetail = Field(default_factory=AuditDetail)
    reason: Reason | None = None
    automatic: bool = False

    @model_validator(mode="after")
    def _check_reason_and_automatic_flag(self) -> AuditEntry:
        if self.action in ACTIONS_REQUIRING_REASON and self.reason is None:
            raise ValueError(f"action '{self.action}' needs a reason")
        if self.detail.automatic is True and not self.automatic:
            raise ValueError("detail says automatic but the entry's automatic flag is off")
        return self

    def row_detail(self) -> AuditDetail:
        """The detail as it is stored: the automatic flag of the entry is written into it."""
        if self.automatic:
            return self.detail.model_copy(update={"automatic": True})
        return self.detail


AUDIT_INSERT_SQL: Final = (
    "INSERT INTO audit_log (actor, action, app_slug, detail, reason) "
    "VALUES (%s, %s, %s, %s::jsonb, %s)"
)


def insert_statement(entry: AuditEntry) -> tuple[str, tuple[Any, ...]]:
    """The INSERT and its parameters. Either write identity holds INSERT on ``audit_log``."""
    detail_json = entry.row_detail().model_dump_json(by_alias=True, exclude_none=True)
    return AUDIT_INSERT_SQL, (entry.actor, entry.action, entry.app_slug, detail_json, entry.reason)
