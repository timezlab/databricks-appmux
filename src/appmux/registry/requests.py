"""The writes of the request inbox: ``Change`` values and audit rows (T144; data model 4.3 and 6).

What is written, by whom, in one place:

* **create** and **withdraw** are the appmux service principal's (``REQUEST_CREATE``,
  ``REQUEST_WITHDRAW``): the asker acts through appmux and the table can only record a wish, so the
  low-privilege role may write it (a request grants nothing, FR-045);
* **approve** and **deny** are the platform admin's own role (``REQUEST_ANSWER``, and for an
  approval the ``apps`` and ``app_config`` writes it grants). They travel in one transaction with
  one identity, so every field applies or none does (FR-042); the route passes the admin's token.

Each answer states ``expected_status="pending"``: the registry re-reads the row inside the
transaction, so two admins answering together close it once and the second gets ``ChangedByOthers``.
``closed_at`` is stated by the caller because the table requires it together with ``closed_by``.

Nothing here reads the database. ``plan_approval`` (``admin/request_catalogue.py``) decides what an
approval does from the snapshot; these functions only spell it as writes. An approval that creates
an app reuses ``register_app_changes`` of ``registry/apps.py``, so a registered app and an approved
one start the same way: owned by the asker, stopped, no release.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from appmux.registry.apps import register_app_changes
from appmux.registry.audit import AuditAction, AuditDetail, AuditEntry, ChangedFields
from appmux.registry.models import App, PermissionRequest
from appmux.registry.protocol import Change
from appmux.registry.writers import WriteKind


class Outcome(StrEnum):
    APPLY = "apply"  # change the app (or create it) and close the request as approved
    CLOSE_ONLY = "close_only"  # every value already holds: close the request as approved
    SLUG_TAKEN = "slug_taken"  # close the request as denied, automatically (FR-043)


@dataclass(frozen=True, slots=True)
class ApprovalPlan:
    """What approving a request does, decided from the snapshot as it stands now.

    ``app_values`` are the ``apps`` columns to set (``settings`` and ``delegation``); ``new_app``
    is the four fields of a ``register``; ``before`` and ``after`` use the payload's own keys (the
    audit model's ``ChangedFields``). ``audit_slug`` is the slug the audit rows are written under:
    the new one after a rename, the created app's for a ``register``.
    """

    outcome: Outcome
    request: PermissionRequest
    audit_slug: str | None
    new_app: Mapping[str, object] | None = None
    app_values: Mapping[str, object] = field(default_factory=dict)
    rename: tuple[str, str] | None = None
    before: Mapping[str, object] = field(default_factory=dict)
    after: Mapping[str, object] = field(default_factory=dict)
    taken_slug: str | None = None


def taken_slug_note(slug: str) -> str:
    """The note of the automatic denial (design 9.4 step 3), in the request and in the audit row."""
    return f"slug '{slug}' is already taken (automatic)"


def create_request_change(
    *,
    kind: str,
    app_slug: str | None,
    payload: Mapping[str, object],
    reason: str,
    requested_by: str,
) -> Change:
    """The insert of a ``pending`` request; ``app_slug`` is ``None`` for a ``register``."""
    return Change(
        WriteKind.REQUEST_CREATE,
        values={
            "app_slug": app_slug,
            "kind": kind,
            "payload": dict(payload),
            "reason": reason,
            "requested_by": requested_by,
        },
    )


def withdraw_change(row: PermissionRequest, *, closed_by: str, closed_at: datetime) -> Change:
    return Change(
        WriteKind.REQUEST_WITHDRAW,
        key={"request_id": row.request_id},
        values={"status": "withdrawn", "closed_by": closed_by, "closed_at": closed_at},
        expected_status="pending",
    )


def _answer(
    row: PermissionRequest,
    *,
    status: str,
    closed_by: str,
    closed_at: datetime,
    note: str | None,
    app_slug: str | None = None,
) -> Change:
    values: dict[str, object] = {"status": status, "closed_by": closed_by, "closed_at": closed_at}
    if note is not None:
        values["closed_note"] = note
    if app_slug is not None:
        values["app_slug"] = app_slug
    return Change(
        WriteKind.REQUEST_ANSWER,
        key={"request_id": row.request_id},
        values=values,
        expected_status="pending",
    )


def approval_changes(
    plan: ApprovalPlan,
    *,
    app: App | None,
    approver: str,
    closed_at: datetime,
    note: str | None = None,
) -> list[Change]:
    """The writes of one approval, in order: what it grants, then the answer.

    ``app`` is the app row the plan was made from (its ``version`` is what the update expects);
    a ``register`` has none. A plan that only closes the request writes the answer alone.
    """
    if plan.outcome is Outcome.SLUG_TAKEN:
        raise ValueError("a taken slug is denied, not approved; use deny_changes")
    row = plan.request
    changes: list[Change] = []
    linked_slug: str | None = None
    if plan.new_app is not None:
        linked_slug = str(plan.new_app["slug"])
        changes.extend(
            register_app_changes(
                slug=linked_slug,
                display_name=str(plan.new_app["display_name"]),
                owner=row.requested_by.lower(),
                source_path=str(plan.new_app["source_path"]),
                memory_budget_mb=int(str(plan.new_app["memory_budget_mb"])),
                registered_by=approver,
            )
        )
    elif plan.outcome is Outcome.APPLY:
        if app is None:
            raise ValueError("approving a change to an app needs the app row the plan came from")
        changes.append(
            Change(
                WriteKind.APP_UPDATE,
                key={"slug": app.slug},
                values={**plan.app_values, "updated_by": approver},
                expected_version=app.version,
            )
        )
    changes.append(
        _answer(
            row,
            status="approved",
            closed_by=approver,
            closed_at=closed_at,
            note=note,
            app_slug=linked_slug,
        )
    )
    return changes


def deny_changes(
    row: PermissionRequest, *, closed_by: str, closed_at: datetime, note: str | None
) -> list[Change]:
    """The answer of a denial: by an admin, or automatically when the slug is taken (FR-043)."""
    return [_answer(row, status="denied", closed_by=closed_by, closed_at=closed_at, note=note)]


def _changed_fields(values: Mapping[str, object]) -> ChangedFields | None:
    return ChangedFields.model_validate(dict(values)) if values else None


def _audit(
    action: AuditAction,
    row: PermissionRequest,
    *,
    actor: str,
    app_slug: str | None,
    reason: str | None,
    detail: AuditDetail,
    automatic: bool = False,
) -> AuditEntry:
    return AuditEntry(
        actor=actor,
        action=action,
        app_slug=app_slug,
        detail=detail,
        reason=reason if reason is not None and reason.strip() else None,
        automatic=automatic,
    )


def answer_audit(
    plan: ApprovalPlan, row: PermissionRequest, *, approver: str, reason: str | None
) -> list[AuditEntry]:
    """The audit rows of pressing Approve: ``request.approve`` (+ ``app.rename``), or the
    automatic ``request.deny`` of a taken slug under the same admin.

    The asker is recorded as ``principal`` on every row, so a person who approves their own request
    shows both names (the actor and the principal).
    """
    asker = f"user:{row.requested_by}"
    if plan.outcome is Outcome.SLUG_TAKEN:
        note = taken_slug_note(str(plan.taken_slug))
        detail = AuditDetail(request=row.request_id, kind=row.kind, principal=asker)
        return [
            _audit(
                "request.deny",
                row,
                actor=approver,
                app_slug=row.app_slug,
                reason=note,
                detail=detail,
                automatic=True,
            )
        ]
    detail = AuditDetail(
        request=row.request_id,
        kind=row.kind,
        principal=asker,
        before=_changed_fields(plan.before),
        after=_changed_fields(plan.after),
        owner=row.requested_by.lower() if plan.new_app is not None else None,
        budget_mb=(
            int(str(plan.new_app["memory_budget_mb"])) if plan.new_app is not None else None
        ),
    )
    entries = [
        _audit(
            "request.approve",
            row,
            actor=approver,
            app_slug=plan.audit_slug,
            reason=reason,
            detail=detail,
        )
    ]
    if plan.rename is not None:
        old, new = plan.rename
        entries.append(
            _audit(
                "app.rename",
                row,
                actor=approver,
                app_slug=new,
                reason=None,
                detail=AuditDetail(from_=old, to=new),
            )
        )
    return entries


def deny_audit(row: PermissionRequest, *, denier: str, reason: str | None) -> list[AuditEntry]:
    detail = AuditDetail(
        request=row.request_id, kind=row.kind, principal=f"user:{row.requested_by}"
    )
    return [
        _audit(
            "request.deny",
            row,
            actor=denier,
            app_slug=row.app_slug,
            reason=reason,
            detail=detail,
        )
    ]
