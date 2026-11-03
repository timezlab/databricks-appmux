"""The release routes: Update, read, name and tag, lock, unlock and delete (T094, T115).

``POST /apps/{app}/releases`` is the Update button (``app.update``, design 5.2). The route
authorizes, makes sure the presser's own access token arrived, hands the flow
(``ReleaseUpdater``, implemented by the reconciler) that token and a ``write`` it may call once,
and maps what comes back. The flow reads the app's folder with the token; the release itself is
written through ``write`` by the appmux service principal, never with the presser's token. The
rules a request meets, in order:

1. identity and same-origin: the guard of ``api.py``;
2. ``app.update`` on the app (``CAN_MANAGE``): a person who cannot open the app gets the same
   ``404`` as for a missing one, a person who can open it but not manage it gets ``403``;
3. the presser's token: without it the folder cannot be read as them (``403 user_token_missing``)
   and the flow never runs;
4. the flow. ``UpdateRefused`` becomes the error of its ``kind`` with the flow's own message;
5. the answer: ``202`` with the new release (status ``pending``) after the reconciler was told, or
   ``200`` ``up_to_date`` when the folder equals a live release (nothing was written, so the
   call declares that no audit row is due).

``write`` is where the route's audit rule lives: it records one ``release.create`` row naming the
presser, the release number and the snapshot digest, in the same transaction as the flow's
changes. A flow that breaks its side of the seam (it never writes a release it reports, writes one
and reports "up to date", writes twice, or writes with no release row) is a defect of the server,
so the answer is ``500`` and the reconciler is not woken.

``GET /apps/{app}/releases/{id}`` (``app.view``) is the release page: status, the reason and the
tail of the build log for a failed one, what appmux detected, the app admin's additions and the
wheels. A deleted release has no snapshot, so it reads like a missing one.

Four routes manage a release (all ``CAN_MANAGE``; the appmux service principal writes, never the
caller's token; each answers a ``ReleaseDetail``, except the delete, which answers ``204``):

* ``PUT /apps/{app}/releases/{id}`` replaces the name and the tags together. One audit row per
  kind of change (``release.label``, ``release.tag``, ``release.untag``) names the release; the
  audit detail has no place for a name or a tag, so the rows say *that* it changed, not to what.
  A request that changes nothing writes and records nothing.
* ``PUT`` and ``DELETE /apps/{app}/releases/{id}/lock`` lock and unlock by hand. A lock is refused
  ``409 lock_limit_reached`` at ``max_locked_releases``; locking a locked release or unlocking an
  unlocked one is a ``200`` that changes nothing.
* ``DELETE /apps/{app}/releases/{id}`` deletes a release that nothing protects. The active, the
  target, an in-progress and a locked release answer ``409 release_protected`` with the reason.

The rules and the writes are ``reconciler/retention.py``'s. A lock, a delete and the cap's own
clean-up all start by writing the app's settings row at the version they read, so two of them made
from the same snapshot cannot both succeed: the second answers ``409 changed_by_others`` and the
page reads again. They answer ``503`` while the registry is down, even where they would change
nothing, because "nothing to change" is judged on a snapshot that may be old.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Final

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext, parse_body
from appmux.admin.apps import env_entries, release_summary
from appmux.admin.errors import (
    AdminError,
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    Unavailable,
)
from appmux.admin.schemas import (
    BundleResource,
    DetectedManifest,
    ReleaseConfigView,
    ReleaseCreated,
    ReleaseDetail,
    ReleaseUnchanged,
    Runtime,
    UpdateReleaseRequest,
)
from appmux.authz.actions import Action
from appmux.reconciler.ports import RefusalKind, ReleaseUpdater, UpdateRefused
from appmux.reconciler.retention import (
    RetentionRefused,
    delete_changes,
    lock_changes,
    unlock_changes,
)
from appmux.reconciler.start_failure import failure_hint
from appmux.registry.audit import AuditAction, AuditDetail, AuditEntry
from appmux.registry.errors import RegistryUnavailable
from appmux.registry.models import Release, RowModel
from appmux.registry.protocol import Change
from appmux.registry.releases import label_and_tags_change
from appmux.registry.writers import WriteKind

# What each refusal of the flow answers. ``UpdateRefused`` keeps its own message for all four.
_REFUSAL_ERRORS: Final[dict[RefusalKind, type[AdminError]]] = {
    "not_found": NotFound,
    "refused": BadRequest,
    "conflict": Conflict,
    "unavailable": Unavailable,
}
# Release numbers are small positive integers; anything else is no release, whatever it is.
_RELEASE_NO_PATTERN: Final = re.compile(r"[1-9][0-9]{0,9}")
_FALLBACK_RUNTIME: Final[Runtime] = "python"


def _no_wake_up(app_slug: str) -> None:
    """The default for ``on_release_created``: the reconciler finds the release on its next pass."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _runtime_of(release: Release) -> Runtime:
    """The runtime the snapshot was detected as.

    The registry allows an unset runtime (a snapshot whose detection failed) while the contract
    says ``python`` or ``node``. Such a release is exactly the one whose reason a person needs to
    read, so it answers ``python`` rather than a ``500`` that would hide the reason.
    """
    if release.detected.runtime == "node":
        return "node"
    return _FALLBACK_RUNTIME


def build_release_detail(
    release: Release, *, is_target: bool, memory_budget_mb: int
) -> ReleaseDetail:
    """The contract's ``ReleaseDetail`` for a live ``release`` of an app with that budget now."""
    if release.snapshot_digest is None:
        raise ValueError("a deleted release has no detail to show")
    detected = release.detected
    return ReleaseDetail(
        **dict(release_summary(release, is_target=is_target)),
        snapshot_digest=release.snapshot_digest,
        build_log_tail=release.build_log_tail,
        failure_hint=failure_hint(
            reason=release.status_reason if release.status == "failed" else None,
            log_tail=release.build_log_tail,
            runtime=detected.runtime,
            memory_budget_mb=memory_budget_mb,
        ),
        detected=DetectedManifest(
            runtime=_runtime_of(release),
            framework=detected.framework,
            command=list(detected.command),
            env=env_entries(detected.env),
            bundle_resources=[
                BundleResource(
                    key=resource.key,
                    type=resource.type,
                    target=resource.target,
                    permission=resource.permission,
                )
                for resource in detected.bundle_resources
            ],
            warnings=list(detected.warnings),
        ),
        config=ReleaseConfigView(
            env=env_entries(release.config.env),
            wheel_sources=list(release.config.wheel_sources),
        ),
        wheels=list(release.wheels),
    )


def _release_create_detail(changes: Iterable[Change], release_no: int) -> AuditDetail:
    """The ``release.create`` audit detail from the flow's ``releases`` insert.

    The digest comes from the row the flow is about to store, so the audit row names the snapshot
    that was really written. A flow that sends no release row, a row for another number or a
    malformed digest is a defect of the server, not a bad request: ``ValueError`` (the guard
    answers ``500``) and nothing is written.
    """
    inserts = [change for change in changes if change.kind is WriteKind.RELEASE_INSERT]
    if len(inserts) != 1 or inserts[0].values.get("release_no") != release_no:
        raise ValueError("an Update must write exactly one release row, for the number it reports")
    digest = inserts[0].values.get("snapshot_digest")
    if not isinstance(digest, str):
        raise ValueError("the release row of an Update has no snapshot digest")
    try:
        return AuditDetail(release=release_no, digest=digest)
    except ValidationError as exc:
        raise ValueError("the release row of an Update has a malformed snapshot digest") from exc


def _live_release(context: AdminContext, request: Request, slug: str) -> Release:
    """The release named in the path; ``NotFound`` for a bad number, a missing or a deleted one."""
    raw_release_no = request.path_params["release_id"]
    if _RELEASE_NO_PATTERN.fullmatch(raw_release_no) is None:
        raise NotFound("no such release")
    release = context.registry.snapshot.release(slug, int(raw_release_no))
    if release is None or release.deleted_at is not None:
        raise NotFound("no such release")
    return release


def _detail_response(context: AdminContext, release: Release, *, reread: bool = False) -> Response:
    """The release as ``ReleaseDetail``; ``reread`` shows the row as the write left it."""
    snapshot = context.registry.snapshot
    if reread:
        stored = snapshot.release(release.app_slug, release.release_no)
        if stored is None:
            raise ValueError("the release written a moment ago is not in the registry")
        release = stored
    config = snapshot.app_config(release.app_slug)
    is_target = config is not None and config.target_release_no == release.release_no
    detail = build_release_detail(
        release, is_target=is_target, memory_budget_mb=_memory_budget_mb(context, release)
    )
    return JSONResponse(detail.model_dump(mode="json"))


def _memory_budget_mb(context: AdminContext, release: Release) -> int:
    """The budget of the release's app now: the failure hint is judged against it."""
    app = context.registry.snapshot.app(release.app_slug)
    if app is None:
        raise ValueError("a release whose app is not in the registry has no detail to show")
    return app.memory_budget_mb


def _require_fresh_registry(context: AdminContext) -> None:
    """Refuse (``503``) while the registry's latest read or write failed.

    A count, a protection and "nothing to change" are all judged on the snapshot; while the
    registry is down it may be old, and these routes must not answer from it.
    """
    if context.registry.last_error is not None:
        raise RegistryUnavailable


def _refuse_blank_texts(body: UpdateReleaseRequest) -> None:
    """``400`` for a name or a tag made of white space, which the schema's length rules admit."""
    problems: list[str] = []
    if body.name is not None and not body.name.strip():
        problems.append("name is not valid")
    problems.extend(
        f"tags.{index} is not valid" for index, tag in enumerate(body.tags) if not tag.strip()
    )
    if problems:
        raise BadRequest(f"the request is not valid: {'; '.join(problems)}")


class ReleaseRoutes:
    """The route provider of the release routes.

    ``updater`` is the Update flow. ``on_release_created`` receives the app's slug after a release
    was written (the parent wires it to ``Reconciler.wake``); by default nothing is signalled and
    the reconciler finds the release on its next pass. ``now`` stamps locks and deletions.
    """

    def __init__(
        self,
        updater: ReleaseUpdater,
        *,
        on_release_created: Callable[[str], None] = _no_wake_up,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._updater = updater
        self._on_release_created = on_release_created
        self._now = now

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def update(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.APP_UPDATE, slug)
            actor = context.actor(request)
            if actor.visitor_token is None:
                raise Forbidden(
                    "appmux did not receive your Databricks access token, so it cannot read the "
                    "app's folder as you; ask a platform admin to turn on user authorization for "
                    "the appmux App, then press Update again",
                    code="user_token_missing",
                )

            written: list[int] = []

            async def write(
                changes: Sequence[Change], *, release_no: int
            ) -> tuple[RowModel | None, ...]:
                if written:
                    raise ValueError("an Update writes one release; the flow wrote twice")
                entry = context.audit_entry(
                    request,
                    "release.create",
                    app_slug=slug,
                    detail=_release_create_detail(changes, release_no),
                )
                rows = await context.write(request, changes, [entry])
                written.append(release_no)
                return rows

            try:
                result = await self._updater.update(
                    actor=actor, app_slug=slug, user_token=actor.visitor_token, write=write
                )
            except UpdateRefused as exc:
                raise _REFUSAL_ERRORS[exc.kind](exc.message) from exc

            if result.is_up_to_date:
                if written:
                    raise ValueError("the flow wrote a release and reported the folder up to date")
                context.skip_audit(
                    request,
                    f"the folder equals live release {result.release_no}; no release was written",
                )
                unchanged = ReleaseUnchanged(status="up_to_date", release_no=result.release_no)
                return JSONResponse(unchanged.model_dump(mode="json"))

            if written != [result.release_no]:
                raise ValueError("the flow reported a new release that it did not write")
            self._on_release_created(slug)
            created = ReleaseCreated(release_no=result.release_no, status="pending")
            return JSONResponse(created.model_dump(mode="json"), status_code=202)

        async def get_release(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.APP_VIEW, slug)
            release = _live_release(context, request, slug)
            return _detail_response(context, release)

        async def put_release(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.RELEASE_LABEL, slug)
            release = _live_release(context, request, slug)
            body = await parse_body(request, UpdateReleaseRequest)
            _refuse_blank_texts(body)
            _require_fresh_registry(context)

            tags = list(dict.fromkeys(body.tags))  # a repeated tag is the same tag
            change = label_and_tags_change(release, name=body.name, tags=tags)
            if change is None:
                context.skip_audit(request, f"release {release.release_no} already has that name")
                return _detail_response(context, release)

            # One audit row per kind of change, always in this order.
            kinds: list[tuple[Action, AuditAction]] = []
            if body.name != release.label:
                kinds.append((Action.RELEASE_LABEL, "release.label"))
            if any(tag not in release.tags for tag in tags):
                kinds.append((Action.RELEASE_TAG, "release.tag"))
            if any(tag not in tags for tag in release.tags):
                kinds.append((Action.RELEASE_UNTAG, "release.untag"))
            for action, _ in kinds:
                await context.require(request, action, slug)
            detail = AuditDetail(release=release.release_no)
            entries = [
                context.audit_entry(request, audit_action, app_slug=slug, detail=detail)
                for _, audit_action in kinds
            ]
            await context.write(request, [change], entries)
            return _detail_response(context, release, reread=True)

        async def lock_release(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.RELEASE_LOCK, slug)
            release = _live_release(context, request, slug)
            _require_fresh_registry(context)
            if release.locked_at is not None:
                context.skip_audit(request, f"release {release.release_no} is locked already")
                return _detail_response(context, release)
            actor = context.actor(request)
            try:
                plan = lock_changes(
                    context.registry.snapshot,
                    slug,
                    release.release_no,
                    locked_by=actor.email,
                    now=self._now(),
                )
            except RetentionRefused as exc:
                raise Conflict(str(exc), code=exc.code) from exc
            entry = context.audit_entry(
                request,
                "release.lock",
                app_slug=slug,
                detail=AuditDetail(release=release.release_no, locks=plan.locks_text()),
            )
            await context.write(request, plan.changes, [entry])
            return _detail_response(context, release, reread=True)

        async def unlock_release(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.RELEASE_UNLOCK, slug)
            release = _live_release(context, request, slug)
            _require_fresh_registry(context)
            if release.locked_at is None:
                context.skip_audit(request, f"release {release.release_no} is not locked")
                return _detail_response(context, release)
            try:
                plan = unlock_changes(context.registry.snapshot, slug, release.release_no)
            except RetentionRefused as exc:
                raise Conflict(str(exc), code=exc.code) from exc
            entry = context.audit_entry(
                request,
                "release.unlock",
                app_slug=slug,
                detail=AuditDetail(release=release.release_no, locks=plan.locks_text()),
            )
            await context.write(request, plan.changes, [entry])
            return _detail_response(context, release, reread=True)

        async def delete_release(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.RELEASE_DELETE, slug)
            release = _live_release(context, request, slug)
            _require_fresh_registry(context)
            actor = context.actor(request)
            try:
                changes = delete_changes(
                    context.registry.snapshot,
                    slug,
                    release.release_no,
                    deleted_by=actor.email,
                    now=self._now(),
                )
            except RetentionRefused as exc:
                raise Conflict(str(exc), code=exc.code) from exc
            entry: AuditEntry = context.audit_entry(
                request,
                "release.delete",
                app_slug=slug,
                detail=AuditDetail(release=release.release_no),
            )
            await context.write(request, changes, [entry])
            return Response(status_code=204)

        return [
            Route("/apps/{app}/releases", update, methods=["POST"]),
            Route("/apps/{app}/releases/{release_id}", get_release, methods=["GET"]),
            Route("/apps/{app}/releases/{release_id}", put_release, methods=["PUT"]),
            Route("/apps/{app}/releases/{release_id}", delete_release, methods=["DELETE"]),
            Route("/apps/{app}/releases/{release_id}/lock", lock_release, methods=["PUT"]),
            Route("/apps/{app}/releases/{release_id}/lock", unlock_release, methods=["DELETE"]),
        ]
