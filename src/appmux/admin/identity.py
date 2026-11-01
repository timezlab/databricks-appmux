"""``GET /me``, ``GET /apps`` and ``POST /apps/{app}/open-mode`` (T041, T153).

Both answer from the registry's last snapshot and the caller's groups, through the same
``Access`` the authorizer uses, so the launcher lists exactly what ``app.open`` would allow. They
need only an identity ("any visitor"), so neither calls ``AdminContext.require``. A registry that is
down does not stop them: the last snapshot keeps answering, and a snapshot never loaded is ``503``.

The ``state`` of an app belongs to the reconciler, which is not built yet. Until a reader is
injected, the state is read from the app's desired state: ``stopped`` when it is to be stopped and
``starting`` when it is to be running, which says nothing the data does not support (it never
claims ``running`` or ``crashed``).

``POST /apps/{app}/open-mode`` (``app.view_as_user``, design 9.2) records how a platform admin who
is neither owner nor named by a grant opens an OBO-delegated app: ``without_token`` (the default)
or ``as_user``, where the app receives the admin's token until they switch back. The choice is a
cookie that only the edge writes; this route asks the edge's ``OpenModeCookieWriter`` for the
``Set-Cookie`` value (``admin`` may not import ``edge``, so the writer is injected) and answers
``204`` with it. ``as_user`` first writes the ``app.view_as_user`` audit row, so a registry that
cannot record the choice sets no cookie. Switching back reduces access, changes no row and writes
nothing. Without a writer the route answers ``503`` and records nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Final, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext, parse_body
from appmux.admin.authorizer import Access
from appmux.admin.errors import Unavailable
from appmux.admin.schemas import (
    AppList,
    AppState,
    AppSummary,
    DesiredState,
    MeResponse,
    OpenMode,
    OpenModeRequest,
)
from appmux.authz.actions import Action
from appmux.authz.policy import authorize
from appmux.identity.actor import Actor, HeaderLines

# The order the contract gives: the default first.
_ADMIN_OPEN_MODES: Final[tuple[OpenMode, ...]] = ("without_token", "as_user")

AppStateReader = Callable[[str, DesiredState], AppState]


class AccessSource(Protocol):
    """Gives a route the snapshot, the authorization data and the groups of one actor."""

    async def access_for(self, actor: Actor) -> Access: ...


class OpenModeCookieWriter(Protocol):
    """The edge's side of Open as a user: the cookie that records the admin's choice.

    ``edge.open_mode.OpenModeCookies`` implements it. ``headers`` are the request's, so the
    choices already made for other apps are kept.
    """

    def switched(
        self, headers: HeaderLines, actor: Actor, app_slug: str, mode: OpenMode
    ) -> str: ...


def state_from_desired(slug: str, desired_state: DesiredState) -> AppState:
    """The state the registry alone supports: what is wanted, never what is observed."""
    return "stopped" if desired_state == "stopped" else "starting"


class IdentityRoutes:
    """The route provider of ``GET /me``, ``GET /apps`` and ``POST /apps/{app}/open-mode``.

    ``open_mode_cookies`` is the edge's cookie writer; without it the open-mode route answers
    ``503`` (code that builds the admin API alone has no edge to set a cookie).
    """

    def __init__(
        self,
        access: AccessSource,
        *,
        app_state: AppStateReader = state_from_desired,
        open_mode_cookies: OpenModeCookieWriter | None = None,
    ) -> None:
        self._access = access
        self._app_state = app_state
        self._open_mode_cookies = open_mode_cookies

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def me(request: Request) -> Response:
            actor = context.actor(request)
            access = await self._access.access_for(actor)
            is_admin = authorize(
                actor, Action.PLATFORM_VIEW, None, access.data, groups=access.groups
            ).allowed
            body = MeResponse(
                email=actor.email.strip().lower(),
                groups=sorted(access.groups or ()),
                is_platform_admin=is_admin,
            )
            return JSONResponse(body.model_dump(mode="json"))

        async def apps(request: Request) -> Response:
            actor = context.actor(request)
            access = await self._access.access_for(actor)
            items: list[AppSummary] = []
            for app in access.snapshot.apps:
                opened = authorize(
                    actor, Action.APP_OPEN, app.slug, access.data, groups=access.groups
                )
                if not opened.allowed or opened.level is None:
                    continue
                as_user = authorize(
                    actor, Action.APP_VIEW_AS_USER, app.slug, access.data, groups=access.groups
                )
                config = access.snapshot.app_config(app.slug)
                items.append(
                    AppSummary(
                        slug=app.slug,
                        display_name=app.display_name,
                        state=self._app_state(
                            app.slug, config.desired_state if config is not None else "stopped"
                        ),
                        permission=opened.level.value,
                        open_modes=list(_ADMIN_OPEN_MODES) if as_user.allowed else [],
                    )
                )
            items.sort(key=lambda item: (item.display_name.casefold(), item.slug))
            return JSONResponse(AppList(items=items).model_dump(mode="json"))

        async def set_open_mode(request: Request) -> Response:
            app_slug = request.path_params["app"]
            await context.require(request, Action.APP_VIEW_AS_USER, app_slug)
            body = await parse_body(request, OpenModeRequest)
            if self._open_mode_cookies is None:
                raise Unavailable(
                    "this process has no edge to remember the choice; restart the App",
                    code="open_mode_unavailable",
                )
            set_cookie = self._open_mode_cookies.switched(
                request.headers, context.actor(request), app_slug, body.mode
            )
            if body.mode == "as_user":
                # Recorded before the cookie exists: a registry that cannot keep the row sets none.
                entry = context.audit_entry(request, "app.view_as_user", app_slug=app_slug)
                await context.write(request, [], [entry])
            else:
                context.skip_audit(
                    request, "switching back to the default reduces access and changes no row"
                )
            return Response(status_code=204, headers={"Set-Cookie": set_cookie})

        return [
            Route("/me", me, methods=["GET"]),
            Route("/apps", apps, methods=["GET"]),
            Route("/apps/{app}/open-mode", set_open_mode, methods=["POST"]),
        ]
