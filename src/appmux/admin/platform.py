"""The platform routes: ``POST /platform/self-check`` (T065), ``PUT /platform/egress`` (T066),
``PUT /platform/admins`` (T154), ``GET /platform`` and ``PUT /platform/settings/{key}`` (T169).

``POST /platform/self-check`` (``platform.view``, FR-013) needs ``CAN_MANAGE`` on the platform. It
runs the canary again through the shared ``SelfCheckState`` and answers the per-check result, in
the contract's ``SelfCheckResult`` shape. A run that fails is not an error of the request: the
answer is ``200`` with ``passed: false``, and the same result decides whether the edge serves apps
(a pass reopens them, a fail closes them again, without a restart).

A self-check reads and writes no registry row, so the route declares ``skip_audit`` instead of
recording an entry. The ``detail`` of each check is text a program inside the sandbox produced
(cleaned to one printable line by ``sandbox/self_check.py``); it is shown here, to a platform
admin, and never on the status page a visitor sees.

``PUT /platform/egress`` (``egress.edit``, design 6.3) replaces the global rule list. The
``EgressEditor`` below holds what this route and ``PUT /apps/{app}/egress`` share: the check of a
list against the container's real address, and the call that tells the running children to read
the saved list. The order is fixed so that a failure leaves a clear state:

1. the list is checked; a refusal is ``400`` and nothing is written;
2. the list is stored with its ``egress.edit`` audit row in one transaction;
3. only then are the pumps told, so a child never enforces a list that is not on record. A failure
   here is ``503 egress_not_applied``: the list is saved and audited, and saving it again retries
   the signal (a save is never skipped as a no-op for that reason).

There is no ``version`` for the global list (``platform_settings`` has no such column), so two
admins saving at once end as the later one.

``PUT /platform/admins`` (``platform_admin.add`` and ``platform_admin.remove``, FR-026) replaces the
``platform_admins`` setting. Only a platform admin reaches it (the same check as the platform
page, made before the body is read). The list the change starts from is the one in force: the
setting, or the bootstrap list while the setting is unset. Each entry that is new gets a
``platform_admin.add`` audit row and each entry that is gone a ``platform_admin.remove`` row, all
with the request's ``reason``, in the transaction of the write. The last-admin guard (rule 2: a
``user`` entry must remain, because appmux cannot count the members of a group) is the body model's
(``PutAdminsRequest``), the same rule as ``authz.check_admin_change``, so a list without a ``user``
is a ``400`` before anything is asked of the registry. A list that adds and removes nothing
changes and records nothing (``skip_audit``) and the answer is the list in force, so a request
cannot reorder the setting unrecorded.

``GET /platform`` (``platform.view``, ``CAN_MANAGE`` on the platform) answers the platform page in
one response, from the snapshot and two seams: the shared ``SelfCheckState`` (read, never run) and
the memory ledger of the reconciler. Nothing is invented when a part is unknown: before the first
self-check finished it is ``503 self_check_pending``, with no memory source ``503 memory_unknown``,
and a stored setting that no longer fits its key is ``503 settings_invalid`` naming the key (the
admin API cannot store one; a row edited in the database can). Saving a valid value for that key
is the way out.

``PUT /platform/settings/{key}`` (``settings.edit``) stores one of the keys of ``SETTABLE_KEYS``.
The value is checked as the whole set of settings would be after the write, so the type, the range
and the rule between ``max_locked_releases`` and ``max_releases_per_app`` are the model's own and
a refusal is a ``400`` that names the key and writes nothing. ``null`` is refused too: a key's
JSON type is its own and no setting is stored as ``null``. There is no ``version`` (TD-013): the
later of two saves wins, and every save is audited. The audit detail names the key and never the
value.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Final, Literal

from pydantic import JsonValue, ValidationError
from pydantic_core import ErrorDetails
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext, parse_body
from appmux.admin.errors import BadRequest, Unavailable
from appmux.admin.schemas import (
    AdminList,
    EgressRules,
    MemoryView,
    PlatformApp,
    PlatformSetting,
    PlatformSettingsView,
    PlatformView,
    Principal,
    PutAdminsRequest,
    PutEgressBody,
    PutSettingRequest,
    SelfCheckEntry,
    SelfCheckResult,
    egress_list_problems,
)
from appmux.authz.actions import Action
from appmux.authz.policy import AdminEntry
from appmux.reconciler.admission import MemoryLedger
from appmux.registry.audit import AuditDetail, AuditEntry
from appmux.registry.grants import PLATFORM_ADMINS_KEY, build_authz_data
from appmux.registry.models import PlatformSetting as StoredSetting
from appmux.registry.platform import (
    faulty_keys,
    platform_setting_change,
    settings_in_force,
    stored_settings,
)
from appmux.registry.snapshot import RegistrySnapshot
from appmux.sandbox.rules import EgressRule
from appmux.sandbox.self_check import SelfCheckReport, SelfCheckState
from appmux.settings import RELEASES_KEPT_FREE, PlatformSettings

logger = logging.getLogger(__name__)

EGRESS_GLOBAL_KEY: Final = "egress_global"


@dataclass(frozen=True, slots=True)
class ClosedTargets:
    """What ``@container_ip`` and ``@appmux_port`` stand for in this running App."""

    container_ip: IPv4Address
    appmux_port: int


# Both are answers that depend on the running container: the first is ``None`` until the
# container's address is known; the second signals the pump of one child (its slug) or of every
# child (``None``) to read the saved list again.
ClosedTargetsSource = Callable[[], ClosedTargets | None]
EgressReloader = Callable[[str | None], Awaitable[None]]
# The reconciler's books of the slot's memory, read when the platform page is built; ``None`` while
# there are none to read (the page is then ``503``, not a figure made up).
MemorySource = Callable[[], MemoryLedger | None]

# The keys ``PUT /platform/settings/{key}`` takes: those the platform page shows as ``settings``.
# ``platform_admins`` and ``egress_global`` have routes of their own.
SETTABLE_KEYS: Final = tuple(PlatformSettingsView.model_fields)
_ROUTE_OF_OWN_KEY: Final = {
    PLATFORM_ADMINS_KEY: "PUT /platform/admins",
    EGRESS_GLOBAL_KEY: "PUT /platform/egress",
}
_LOCK_RULE_MESSAGE: Final = (
    f"max_locked_releases must be at most max_releases_per_app - {RELEASES_KEPT_FREE}, "
    "so a release can always be cleaned up"
)
_EXPECTED_BY_ERROR_TYPE: Final = {
    "int_type": "a whole number",
    "bool_type": "true or false",
    "string_type": "text",
    "string_too_short": "text that is not empty",
    "list_type": "a list of texts",
}


async def _reload_nothing(app_slug: str | None) -> None:
    """The reloader of a process that has no pump to signal."""


class EgressEditor:
    """What the two egress routes share (see the module docstring for the order they follow).

    ``closed_targets`` is asked on every save, not once, so a list is never checked against an
    address that has gone stale. Without a source, or while it answers ``None``, no list can be
    checked, and a save is refused with ``503``: an unchecked list could open a closed target.
    """

    def __init__(
        self,
        *,
        closed_targets: ClosedTargetsSource | None = None,
        reloader: EgressReloader | None = None,
    ) -> None:
        self._closed_targets = closed_targets
        self._reloader = reloader if reloader is not None else _reload_nothing

    def checked(self, rules: Sequence[EgressRule]) -> list[dict[str, str]]:
        """The list as JSON values to store; ``503`` if it cannot be checked, ``400`` if refused."""
        targets = self._closed_targets() if self._closed_targets is not None else None
        if targets is None:
            raise Unavailable(
                "appmux does not know this container's address yet, so it cannot check an egress "
                "list; try again in a moment, or ask a platform admin to read the App's log",
                code="egress_targets_unknown",
            )
        problems = egress_list_problems(
            rules, container_ip=targets.container_ip, appmux_port=targets.appmux_port
        )
        if problems:
            raise BadRequest(
                f"the egress list is refused: {'; '.join(problems)}", code="egress_rules_refused"
            )
        return [rule.model_dump(mode="json") for rule in rules]

    async def apply(self, app_slug: str | None) -> None:
        """Tell the children's pumps to read the saved list (``None``: the global list)."""
        try:
            await self._reloader(app_slug)
        except Exception as exc:
            # Broad on purpose: the reloader is injected and signals other processes, and whatever
            # it raises must end as this answer, not as a 500 for a list that is already saved.
            # The detail goes to the log only; the client gets the fixed sentence.
            logger.exception("the egress list was saved but the pumps were not told")
            raise Unavailable(
                "the egress list is saved, but running apps have not read it yet; "
                "save it again to retry",
                code="egress_not_applied",
            ) from exc


def self_check_result(report: SelfCheckReport) -> SelfCheckResult:
    """The contract's ``SelfCheckResult`` for ``report`` (also held in ``PlatformView``)."""
    return SelfCheckResult(
        passed=report.passed,
        ran_at=report.ran_at,
        duration_ms=report.duration_ms,
        checks=[
            SelfCheckEntry(name=check.name, passed=check.passed, detail=check.detail)
            for check in report.checks
        ],
    )


def _entry_key(entry: AdminEntry) -> tuple[str, str]:
    # A user is stored in lower case; a group name is compared without case, as ``authorize`` does.
    return entry.type, entry.name.strip().lower()


def _expected(error: ErrorDetails) -> str:
    """What a setting's value should be, in words, from the model's own error (never the input)."""
    if len(error["loc"]) > 1:  # an item of a list
        return "a list of texts that are not empty"
    kind, context = error["type"], error.get("ctx", {})
    if kind == "greater_than_equal":
        return f"a whole number of at least {context['ge']}"
    if kind == "less_than_equal":
        return f"a whole number of at most {context['le']}"
    if kind == "literal_error":
        return f"one of {context['expected']}"
    return _EXPECTED_BY_ERROR_TYPE.get(kind, "a value of the type this setting has")


def _setting_refusal(key: str, error: ValidationError) -> BadRequest:
    """The ``400`` for ``key`` set to a value the settings model refuses.

    A problem in the value of ``key`` is said in words; the model's own rule between keys is said
    as a rule; a problem in another key is a stored value that no longer fits (a row edited in the
    database), which the admin must fix first. No submitted text is repeated.
    """
    problems: list[str] = []
    for detail in error.errors(include_input=False, include_url=False):
        field = str(detail["loc"][0]) if detail["loc"] else None
        if field == key:
            problem = f"{key} must be {_expected(detail)}"
        elif field is None:
            problem = _LOCK_RULE_MESSAGE
        else:
            problem = f"the stored value of {field} is not valid; save a valid value for it first"
        if problem not in problems:
            problems.append(problem)
    return BadRequest(f"the request is not valid: {'; '.join(problems)}")


def _checked_setting(snapshot: RegistrySnapshot, key: str, value: JsonValue) -> JsonValue:
    """``value`` if the settings stay valid with ``key`` set to it; ``BadRequest`` otherwise."""
    if value is None:
        raise BadRequest(
            f"{key} needs a value: no setting is stored as null, so send the value it should have"
        )
    try:
        PlatformSettings.model_validate({**stored_settings(snapshot), key: value})
    except ValidationError as exc:
        raise _setting_refusal(key, exc) from exc
    return value


def platform_view(
    snapshot: RegistrySnapshot,
    *,
    settings: PlatformSettings,
    admins: Sequence[AdminEntry],
    report: SelfCheckReport,
    memory: MemoryLedger,
) -> PlatformView:
    """The contract's ``PlatformView`` from one snapshot and the two figures it does not hold."""
    return PlatformView(
        settings=PlatformSettingsView.model_validate(
            {key: getattr(settings, key) for key in SETTABLE_KEYS}
        ),
        admins=[Principal(type=entry.type, name=entry.name) for entry in admins],
        egress_global=list(settings.egress_global),
        self_check=self_check_result(report),
        memory=MemoryView(
            limit_mb=memory.limit_mb,
            reserve_mb=memory.reserve_mb,
            committed_mb=memory.committed_mb,
            free_mb=memory.free_mb,
        ),
        apps=[
            PlatformApp(
                slug=app.slug,
                display_name=app.display_name,
                source_path=app.source_path,
                memory_budget_mb=app.memory_budget_mb,
                owner=app.owner,
                obo_delegated=app.obo_delegated,
                shared_sp_delegated=app.shared_sp_delegated,
                egress=list(app.egress),
                version=app.version,
            )
            for app in sorted(snapshot.apps, key=lambda app: (app.display_name, app.slug))
        ],
    )


class PlatformRoutes:
    """The route provider of the platform routes.

    ``self_check`` is the state the start-up and the edge share; ``None`` means this process has
    none, and the route says so instead of running anything. ``egress`` is shared with the app
    routes; the default has no closed targets, so ``PUT /platform/egress`` answers ``503``.
    ``bootstrap_admins`` is ``EnvironmentSettings.appmux_bootstrap_admins``, the admins in force
    while the ``platform_admins`` setting is unset (the list ``PUT /platform/admins`` replaces).
    ``memory`` reads the reconciler's ledger for ``GET /platform``; without one that route is
    ``503``.
    """

    def __init__(
        self,
        *,
        self_check: SelfCheckState | None = None,
        egress: EgressEditor | None = None,
        bootstrap_admins: Iterable[str] = (),
        memory: MemorySource | None = None,
    ) -> None:
        self._self_check = self_check
        self._egress = egress if egress is not None else EgressEditor()
        self._bootstrap_admins = tuple(bootstrap_admins)
        self._memory = memory

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def run_self_check(request: Request) -> Response:
            await context.require(request, Action.PLATFORM_VIEW)
            if self._self_check is None:
                raise Unavailable(
                    "this process has no self-check set up; restart the App",
                    code="self_check_unavailable",
                )
            report = await self._self_check.run()
            context.skip_audit(request, "a self-check runs the canary and changes no registry row")
            return JSONResponse(self_check_result(report).model_dump(mode="json"))

        async def put_egress(request: Request) -> Response:
            await context.require(request, Action.EGRESS_EDIT)
            body = await parse_body(request, PutEgressBody)
            stored = self._egress.checked(body.rules)

            actor = context.actor(request)
            change = platform_setting_change(
                context.registry.snapshot, EGRESS_GLOBAL_KEY, stored, updated_by=actor.email
            )
            # The audit detail has no field for a rule list; the list itself is in the setting.
            entry = context.audit_entry(
                request, "egress.edit", detail=AuditDetail(setting=EGRESS_GLOBAL_KEY)
            )
            await context.write(request, [change], [entry], admin_token=actor.visitor_token)
            await self._egress.apply(None)
            return JSONResponse(EgressRules(items=body.rules).model_dump(mode="json"))

        async def put_admins(request: Request) -> Response:
            # The platform check comes first so that a stranger learns nothing from a 400.
            await context.require(request, Action.PLATFORM_VIEW)
            body = await parse_body(request, PutAdminsRequest)

            snapshot = context.registry.snapshot
            current = build_authz_data(
                snapshot, bootstrap_admins=self._bootstrap_admins
            ).platform_admins
            wanted = tuple(AdminEntry(entry.type, entry.name) for entry in body.admins)
            kept = {_entry_key(entry) for entry in wanted}
            was = {_entry_key(entry) for entry in current}
            removed = [entry for entry in current if _entry_key(entry) not in kept]
            added = [entry for entry in wanted if _entry_key(entry) not in was]
            if not removed and not added:
                context.skip_audit(request, "the admin list in force is the one sent")
                return _admin_list(current)

            if removed:
                await context.require(request, Action.PLATFORM_ADMIN_REMOVE)
            if added:
                await context.require(request, Action.PLATFORM_ADMIN_ADD)
            actor = context.actor(request)
            entries = [
                *(
                    _admin_audit(context, request, "platform_admin.remove", e, body.reason)
                    for e in removed
                ),
                *(
                    _admin_audit(context, request, "platform_admin.add", e, body.reason)
                    for e in added
                ),
            ]
            change = platform_setting_change(
                snapshot,
                PLATFORM_ADMINS_KEY,
                [{"type": entry.type, "name": entry.name} for entry in wanted],
                updated_by=actor.email,
            )
            await context.write(request, [change], entries, admin_token=actor.visitor_token)
            return _admin_list(wanted)

        async def get_platform(request: Request) -> Response:
            await context.require(request, Action.PLATFORM_VIEW)
            if self._self_check is None:
                raise Unavailable(
                    "this process has no self-check set up; restart the App",
                    code="self_check_unavailable",
                )
            report = self._self_check.latest
            if report is None:
                raise Unavailable(
                    "the first self-check has not finished yet; open this page again in a moment",
                    code="self_check_pending",
                )
            ledger = self._memory() if self._memory is not None else None
            if ledger is None:
                raise Unavailable(
                    "appmux does not know the slot's memory yet; open this page again in a moment",
                    code="memory_unknown",
                )
            snapshot = context.registry.snapshot
            try:
                settings = settings_in_force(snapshot)
            except ValidationError as exc:
                raise Unavailable(
                    f"a stored setting is not valid ({', '.join(faulty_keys(exc))}); save a valid "
                    "value for each of those keys, then open this page again",
                    code="settings_invalid",
                ) from exc
            admins = build_authz_data(
                snapshot, bootstrap_admins=self._bootstrap_admins
            ).platform_admins
            view = platform_view(
                snapshot, settings=settings, admins=admins, report=report, memory=ledger
            )
            return JSONResponse(view.model_dump(mode="json"))

        async def put_setting(request: Request) -> Response:
            await context.require(request, Action.SETTINGS_EDIT)
            key = request.path_params["key"]
            if key in _ROUTE_OF_OWN_KEY:
                raise BadRequest(
                    f"{key} is changed through {_ROUTE_OF_OWN_KEY[key]}, not here",
                    code="setting_has_own_route",
                )
            if key not in SETTABLE_KEYS:
                raise BadRequest(
                    f"that is not a setting you can change here; the keys are "
                    f"{', '.join(SETTABLE_KEYS)}",
                    code="unknown_setting",
                )
            body = await parse_body(request, PutSettingRequest)
            snapshot = context.registry.snapshot
            value = _checked_setting(snapshot, key, body.value)

            actor = context.actor(request)
            change = platform_setting_change(snapshot, key, value, updated_by=actor.email)
            # The audit detail has no field for a value; the value is in the setting itself.
            entry = context.audit_entry(request, "settings.edit", detail=AuditDetail(setting=key))
            (row,) = await context.write(
                request, [change], [entry], admin_token=actor.visitor_token
            )
            if not isinstance(row, StoredSetting):
                raise RuntimeError("the registry did not return the stored setting")
            stored = PlatformSetting(
                key=key, value=value, updated_by=row.updated_by, updated_at=row.updated_at
            )
            return JSONResponse(stored.model_dump(mode="json"))

        return [
            Route("/platform", get_platform, methods=["GET"]),
            Route("/platform/self-check", run_self_check, methods=["POST"]),
            Route("/platform/egress", put_egress, methods=["PUT"]),
            Route("/platform/admins", put_admins, methods=["PUT"]),
            Route("/platform/settings/{key}", put_setting, methods=["PUT"]),
        ]


def _admin_list(entries: Sequence[AdminEntry]) -> Response:
    body = AdminList(items=[Principal(type=entry.type, name=entry.name) for entry in entries])
    return JSONResponse(body.model_dump(mode="json"))


def _admin_audit(
    context: AdminContext,
    request: Request,
    action: Literal["platform_admin.add", "platform_admin.remove"],
    entry: AdminEntry,
    reason: str,
) -> AuditEntry:
    """The audit row of one entry added or removed.

    The audit model refuses a principal with a control character, one over 255 characters and one
    shaped like a credential. That is reported against ``admins``, a field the caller sent, and
    never repeats the entry.
    """
    try:
        detail = AuditDetail(principal=f"{entry.type}:{entry.name}")
    except ValidationError as exc:
        raise BadRequest(
            "admins: an entry is not a name that can be recorded; it may hold a control "
            "character, be longer than 255 characters or look like a credential"
        ) from exc
    return context.audit_entry(request, action, detail=detail, reason=reason)
