"""The request catalogue: what a person may ask a platform admin for, and what approving does.

Design 9.4 and data model 4.3. A request is a row in ``permission_requests`` that grants nothing
(FR-045): ``authorize()`` and the reconciler read ``apps`` only, so the catalogue is the one place
that knows what a request means. Three kinds, each an entry here:

* ``register``: a new app, asked by any visitor, all four platform fields, always a gate;
* ``delegation``: ``obo`` and/or ``shared_sp`` for an existing app, each a gate;
* ``settings``: any non-empty subset of ``slug``, ``display_name``, ``source_path`` and
  ``memory_budget_mb`` for an existing app; ``source_path`` is the only gate.

Everything here is a pure function of a ``RegistrySnapshot`` and a request row, so the rules are
tested without a server. The field checks are the ones of a direct edit: the payload models are
``RegisterPayload``, ``DelegationPayload`` and ``SettingsPayload`` of ``admin/schemas.py``, built
from the same ``Slug``, ``WorkspacePath`` and ``MemoryBudgetMb`` types as ``RegisterAppRequest``.
A refusal is an ``AdminError`` with a ``code`` the UI may branch on:

| code | status | when |
|---|---|---|
| ``bad_request`` | 400 | the payload fails its model |
| ``unknown_kind`` | 400 | the kind is not in the catalogue (a forged row; nothing approves it) |
| ``no_change`` | 400 | a field already holds the value asked for |
| ``reason_required`` | 400 | approving a request with a gate field, with no reason |
| ``request_invalid`` | 400 | a stored payload no longer passes its model |
| ``slug_taken`` | 409 | an app has the slug |
| ``request_pending`` | 409 | one pending request per (app, kind), or per proposed slug |
| ``too_many_pending`` | 409 | a person already has 3 pending ``register`` requests |
| ``shared_sp_not_allowed`` | 409 | the master switch is off |
| ``not_pending`` | 409 | the request is already closed |
| ``not_the_asker`` | 403 | someone else tries to withdraw |
| ``app_missing`` | 409 | the app the request is about is gone |
| ``requester_not_admin`` | 409 | the asker no longer manages the app |

What this module does not own, and the task text (T143) suggested it might: the audit actions and
their detail keys live in ``registry/audit.py``, the exactly-one-of rule of an ``EnvEntry`` in
``admin/schemas.py``, and the setting keys in ``admin/platform.py`` (lane M). They already have one
definition each; moving them here would add a second.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel, JsonValue, ValidationError

from appmux.admin.errors import BadRequest, Conflict, Forbidden, NotFound, validation_error
from appmux.admin.schemas import DelegationPayload, RegisterPayload, SettingsPayload
from appmux.registry.models import App, PermissionRequest
from appmux.registry.requests import ApprovalPlan, Outcome
from appmux.registry.snapshot import RegistrySnapshot
from appmux.settings import PlatformSettings

KINDS: Final = ("register", "delegation", "settings")
MAX_PENDING_REGISTER_PER_PERSON: Final = 3  # design 9.4: one account cannot fill the inbox

_PLATFORM_FIELDS: Final = frozenset({"slug", "display_name", "source_path", "memory_budget_mb"})
GATE_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    "register": _PLATFORM_FIELDS,
    "delegation": frozenset({"obo", "shared_sp"}),
    "settings": frozenset({"source_path"}),
}
_PAYLOAD_MODELS: Final[Mapping[str, type[BaseModel]]] = {
    "register": RegisterPayload,
    "delegation": DelegationPayload,
    "settings": SettingsPayload,
}
# The ``apps`` column each delegation key sets.
DELEGATION_COLUMNS: Final = {"obo": "obo_delegated", "shared_sp": "shared_sp_delegated"}
_SHARED_SP_SWITCH_KEY: Final = "shared_sp_delegation_allowed"


def asks_without_app(kind: str) -> bool:
    """Whether the kind has no app yet (only ``register``): its ``app`` is the proposed slug."""
    return kind == "register"


def shared_sp_allowed(snapshot: RegistrySnapshot) -> bool:
    """The master switch for the shared service principal (data model 4.1)."""
    row = snapshot.setting(_SHARED_SP_SWITCH_KEY)
    if row is not None and isinstance(row.value, bool):
        return row.value
    return PlatformSettings().shared_sp_delegation_allowed


def requires_reason(kind: str, payload: Mapping[str, object]) -> bool:
    """Whether approving needs a reason: the payload holds a gate field (design 9.4)."""
    gates = GATE_FIELDS.get(kind, frozenset())
    return any(key in gates and value is not None for key, value in payload.items())


def require_known_kind(row: PermissionRequest) -> None:
    """Refuse to answer a row whose kind this version of the code has no entry for.

    Such a row loads (the registry keeps what the SQL allows) and grants nothing, but the contract's
    ``RequestRow.kind`` cannot carry it, so the inbox hides it and no route answers it. It stays as
    it is until a version that knows the kind runs.
    """
    if row.kind not in KINDS:
        raise BadRequest(
            "this request is of a kind this version of appmux does not know, so it cannot be "
            "answered here; it stays as it is until a version that knows it runs",
            code="unknown_kind",
        )


def _parsed(kind: str, payload: Mapping[str, object]) -> BaseModel:
    model = _PAYLOAD_MODELS.get(kind)
    if model is None:
        raise BadRequest(
            "that kind of request does not exist; ask for a new app, a delegation or a settings "
            "change",
            code="unknown_kind",
        )
    try:
        return model.model_validate(dict(payload))
    except ValidationError as exc:
        raise validation_error(exc) from exc


def _asked(parsed: BaseModel) -> dict[str, object]:
    """The fields the payload sets to a value; an explicit ``null`` asks for nothing."""
    values = {name: getattr(parsed, name) for name in parsed.model_fields_set}
    return {name: value for name, value in values.items() if value is not None}


def _pending(snapshot: RegistrySnapshot) -> list[PermissionRequest]:
    return [row for row in snapshot.permission_requests if row.status == "pending"]


def _slot(row: PermissionRequest) -> str | None:
    """``coalesce(app_slug, payload->>'slug')``: the slot ``one_pending_request`` is unique on."""
    if row.app_slug is not None:
        return row.app_slug
    slug = row.payload.slug
    return slug if isinstance(slug, str) else None


def check_new_request(
    snapshot: RegistrySnapshot,
    *,
    kind: str,
    app: str,
    payload: Mapping[str, object],
    asker: str,
) -> None:
    """Raise the refusal a new request meets, or return when it may be inserted.

    The registry's unique index backs the pending rule against two requests arriving together; the
    check here is what gives the person the reason. Who may ask (any visitor for ``register``,
    ``CAN_MANAGE`` for the others) is ``authorize()``'s answer, taken before this is called.
    """
    parsed = _parsed(kind, payload)
    asked = _asked(parsed)
    if kind == "register":
        slug = asked["slug"]
        if slug != app:
            raise BadRequest("app must equal payload.slug")
        _require_slug_free(snapshot, str(slug))
        _require_no_pending(snapshot, kind, str(slug))
        _require_under_the_cap(snapshot, asker)
        return
    stored = snapshot.app(app)
    if stored is None:
        raise NotFound("no such app")
    if kind == "delegation":
        _require_a_change_in_delegation(stored, asked)
        if asked.get("shared_sp") is True and not shared_sp_allowed(snapshot):
            raise Conflict(
                "the shared service principal cannot be delegated while a platform admin has it "
                "switched off; ask for OBO instead, or ask a platform admin to allow it",
                code="shared_sp_not_allowed",
            )
    else:
        _require_a_change_in_settings(stored, asked)
        new_slug = asked.get("slug")
        if isinstance(new_slug, str):
            _require_slug_free(snapshot, new_slug)
    _require_no_pending(snapshot, kind, app)


def _require_slug_free(snapshot: RegistrySnapshot, slug: str) -> None:
    if snapshot.app(slug) is not None:
        raise Conflict(f"slug '{slug}' is already taken", code="slug_taken")


def pending_request_conflict() -> Conflict:
    """The refusal of a second pending request for the same (app, kind) or proposed slug.

    Also what the route answers when the registry's unique index refuses a request that arrived
    together with another one.
    """
    return Conflict(
        "a request of this kind is already waiting for a platform admin; withdraw it to ask "
        "again with different values",
        code="request_pending",
    )


def _require_no_pending(snapshot: RegistrySnapshot, kind: str, slot: str) -> None:
    if any(row.kind == kind and _slot(row) == slot for row in _pending(snapshot)):
        raise pending_request_conflict()


def _require_under_the_cap(snapshot: RegistrySnapshot, asker: str) -> None:
    mine = [
        row
        for row in _pending(snapshot)
        if row.kind == "register" and row.requested_by.lower() == asker.lower()
    ]
    if len(mine) >= MAX_PENDING_REGISTER_PER_PERSON:
        raise Conflict(
            f"you already have {MAX_PENDING_REGISTER_PER_PERSON} requests for a new app waiting; "
            "withdraw one or wait for an answer",
            code="too_many_pending",
        )


def _require_a_change_in_settings(app: App, asked: Mapping[str, object]) -> None:
    same = sorted(name for name, value in asked.items() if getattr(app, name) == value)
    if same:
        raise BadRequest(
            f"{', '.join(same)} already holds that value; leave it out of the request",
            code="no_change",
        )


def _require_a_change_in_delegation(app: App, asked: Mapping[str, object]) -> None:
    held = sorted(name for name in asked if getattr(app, DELEGATION_COLUMNS[name]) is True)
    if held:
        raise BadRequest(
            f"{', '.join(held)} is already delegated to this app; leave it out of the request",
            code="no_change",
        )


def require_pending(row: PermissionRequest) -> None:
    """Refuse to answer a request that is already closed."""
    if row.status != "pending":
        raise Conflict(
            f"this request is already {row.status}; reload the inbox", code="not_pending"
        )


def check_can_withdraw(row: PermissionRequest, actor_email: str) -> None:
    """Only the asker withdraws, and only while the request is pending (FR-041)."""
    if row.requested_by.lower() != actor_email.lower():
        raise Forbidden("only the person who asked can withdraw a request", code="not_the_asker")
    if row.status != "pending":
        raise Conflict(
            f"this request is already {row.status}; there is nothing to withdraw",
            code="not_pending",
        )


def current_values(
    snapshot: RegistrySnapshot, row: PermissionRequest
) -> dict[str, JsonValue] | None:
    """The app's current value of each key in the payload, for the inbox; ``null`` for register."""
    if row.kind not in ("settings", "delegation") or row.app_slug is None:
        return None
    app = snapshot.app(row.app_slug)
    if app is None:
        return None
    keys = [key for key, value in row.payload.model_dump().items() if value is not None]
    if row.kind == "delegation":
        return {
            key: getattr(app, DELEGATION_COLUMNS[key]) for key in keys if key in DELEGATION_COLUMNS
        }
    return {key: getattr(app, key) for key in keys if key in _PLATFORM_FIELDS}


def plan_approval(
    snapshot: RegistrySnapshot,
    row: PermissionRequest,
    *,
    reason: str | None,
    requester_can_manage: bool,
) -> ApprovalPlan:
    """Re-check ``row`` as it stands now and say what approving it does (design 9.4, step 3).

    Raises when approving cannot proceed and nothing should be written; returns ``SLUG_TAKEN`` for
    the one failure that closes the request at once, because a taken slug cannot become free by
    waiting. ``requester_can_manage`` is the caller's answer for the asker (the catalogue has no
    authorizer); it is not read for ``register``.
    """
    require_pending(row)
    payload = {key: value for key, value in row.payload.model_dump().items() if value is not None}
    require_known_kind(row)
    try:
        asked = _asked(_parsed(row.kind, payload))
    except BadRequest as exc:
        raise BadRequest(
            "the request no longer passes the checks of a new one; deny it and ask the person "
            "to ask again",
            code="request_invalid",
        ) from exc
    if row.kind == "register":
        return _plan_register(snapshot, row, asked, reason)
    app = snapshot.app(row.app_slug) if row.app_slug is not None else None
    if app is None:
        raise Conflict(
            "the app this request is about no longer exists; deny the request", code="app_missing"
        )
    if not requester_can_manage:
        raise Conflict(
            "the person who asked cannot manage this app any more (a group grant is not "
            "re-checked here); deny the request, or make the change directly",
            code="requester_not_admin",
        )
    new_slug = asked.get("slug")
    if isinstance(new_slug, str) and new_slug != app.slug and snapshot.app(new_slug) is not None:
        return ApprovalPlan(Outcome.SLUG_TAKEN, row, app.slug, taken_slug=new_slug)
    _require_reason(row.kind, payload, reason)
    if row.kind == "delegation":
        return _plan_delegation(snapshot, row, app, asked)
    return _plan_settings(row, app, asked)


def _require_reason(kind: str, payload: Mapping[str, object], reason: str | None) -> None:
    if requires_reason(kind, payload) and (reason is None or not reason.strip()):
        raise BadRequest(
            "this request holds a field that passes a risk to the platform; enter the reason "
            "for approving it",
            code="reason_required",
        )


def _plan_register(
    snapshot: RegistrySnapshot,
    row: PermissionRequest,
    asked: Mapping[str, object],
    reason: str | None,
) -> ApprovalPlan:
    slug = str(asked["slug"])
    if snapshot.app(slug) is not None:
        return ApprovalPlan(Outcome.SLUG_TAKEN, row, None, taken_slug=slug)
    _require_reason(row.kind, asked, reason)
    return ApprovalPlan(Outcome.APPLY, row, slug, new_app=dict(asked), after=dict(asked))


def _plan_delegation(
    snapshot: RegistrySnapshot, row: PermissionRequest, app: App, asked: Mapping[str, object]
) -> ApprovalPlan:
    if asked.get("shared_sp") is True and not shared_sp_allowed(snapshot):
        raise Conflict(
            "the shared service principal is switched off for the platform; turn it on in the "
            "platform settings first, or deny the request",
            code="shared_sp_not_allowed",
        )
    changing = {
        key: value for key, value in asked.items() if getattr(app, DELEGATION_COLUMNS[key]) != value
    }
    outcome = Outcome.APPLY if changing else Outcome.CLOSE_ONLY
    return ApprovalPlan(
        outcome,
        row,
        app.slug,
        app_values={DELEGATION_COLUMNS[key]: value for key, value in changing.items()},
        before={key: getattr(app, DELEGATION_COLUMNS[key]) for key in changing},
        after=changing,
    )


def _plan_settings(row: PermissionRequest, app: App, asked: Mapping[str, object]) -> ApprovalPlan:
    changing = {name: value for name, value in asked.items() if getattr(app, name) != value}
    new_slug = changing.get("slug")
    return ApprovalPlan(
        Outcome.APPLY if changing else Outcome.CLOSE_ONLY,
        row,
        str(new_slug) if isinstance(new_slug, str) else app.slug,
        app_values=changing,
        rename=(app.slug, new_slug) if isinstance(new_slug, str) else None,
        before={name: getattr(app, name) for name in changing},
        after=changing,
    )
