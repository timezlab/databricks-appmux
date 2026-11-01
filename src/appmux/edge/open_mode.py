"""Open as a user: the edge cookie and the decision whether a child receives the visitor's token.

A platform admin holds ``CAN_MANAGE`` on every app with no grant. When they open an OBO-delegated
app on which they are neither the owner nor named by a grant, the edge removes
``X-Forwarded-Access-Token`` (the app still sees who is there from ``X-Forwarded-Email``), because
that token can write platform data. The admin may choose **Open as a user** for one app: the token
then passes exactly as for a visitor, until they switch back (design 9.2, FR-028, rule 17). The
choice lives in the ``__Host-appmux_open`` cookie, which this module writes and reads and
``rewrite/request.py`` removes before the request reaches a child.

Two pieces, built together at start-up and sharing one signing key:

* ``OpenModeCookies``: the cookie. The value is the list of apps chosen "as a user", joined with
  ``.``, then ``~`` and a signature: ``sales.team~<hmac>``. It is a session cookie (no ``Max-Age``)
  with the ``__Host-`` attributes (``Secure``, ``Path=/``, no ``Domain``) plus ``HttpOnly`` and
  ``SameSite=Lax``.
* ``OpenModeTokenPolicy``: the edge's ``ForwardTokenPolicy``. It answers whether to pass the
  actor's token to the child, from the registry's snapshot (through ``authz``) and the cookie.

Why the cookie is signed. Every child runs on the same origin, so a child's page script can write a
cookie of any name the browser lets a script write, and ``HttpOnly`` only protects a cookie that
already exists. An unsigned cookie would let a child switch an admin to **Open as a user** for
itself and receive their token. The signature covers the person and the list, so a value that a
script writes, that another person holds, or that was edited afterwards means "no choice", which is
the default (no token). The key is random per process and never stored: a restart forgets every
choice, and each admin is back to the default until they choose again.

The decision fails closed. Whatever goes wrong while deciding (a snapshot that is not loaded, a
group lookup that broke) means "do not forward the token", and the log line names the app, never
the person, the token or the cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from collections.abc import Mapping
from typing import Final

from appmux.admin.identity import AccessSource
from appmux.admin.schemas import OpenMode
from appmux.authz.actions import Action
from appmux.authz.policy import Reason, authorize
from appmux.identity.actor import Actor, HeaderLines
from appmux.registry.models import SLUG_PATTERN
from appmux.rewrite.response import OPEN_MODE_COOKIE_NAME

logger = logging.getLogger(__name__)

# Keeps the cookie far below the size a browser or proxy accepts; the oldest choice goes first.
MAX_CHOICES: Final = 16

_MIN_KEY_BYTES: Final = 32
_SIGNATURE_SEPARATOR: Final = "~"
_SLUG_SEPARATOR: Final = "."
_SIGNED_CONTEXT: Final = b"appmux.open_mode.v1"
_SLUG: Final = re.compile(SLUG_PATTERN)
# The attributes of a ``__Host-`` cookie (Secure, Path=/, no Domain) plus the contract's two.
_ATTRIBUTES: Final = "Path=/; Secure; HttpOnly; SameSite=Lax"
_DELETION_ATTRIBUTES: Final = "Max-Age=0; " + _ATTRIBUTES
# Reasons ``app.view_as_user`` is refused for a person who needs no choice: they are an ordinary
# visitor (no platform standing) or already hold access of their own, so the token passes as usual.
_VISITOR_REASONS: Final = frozenset({Reason.NOT_PLATFORM_ADMIN, Reason.ALREADY_HAS_ACCESS})


def _email_of(actor: Actor) -> str:
    return actor.email.strip().lower()


class OpenModeCookies:
    """Reads and writes the ``__Host-appmux_open`` cookie of one process."""

    def __init__(self, *, key: bytes | None = None) -> None:
        """``key`` is for tests; by default a fresh random key, so a restart forgets all choices."""
        if key is None:
            key = secrets.token_bytes(_MIN_KEY_BYTES)
        if len(key) < _MIN_KEY_BYTES:
            raise ValueError(f"the open-mode key must be at least {_MIN_KEY_BYTES} bytes")
        self._key = key

    def chosen_as_user(self, headers: HeaderLines, actor: Actor) -> frozenset[str]:
        """The apps this person chose to open as a user; empty when there is no valid choice.

        Empty means the default, so every doubt lands there: the cookie is absent or sent twice, or
        its value is not in the exact form this module writes, or its signature does not match
        this person and this list.
        """
        values = _cookie_values(headers)
        if len(values) != 1:
            return frozenset()
        slugs = self._verified_slugs(values[0], actor)
        return frozenset(slugs) if slugs is not None else frozenset()

    def switched(self, headers: HeaderLines, actor: Actor, app_slug: str, mode: OpenMode) -> str:
        """The ``Set-Cookie`` value that records the person's new choice for ``app_slug``.

        The list starts from the valid cookie the request carried (an invalid one counts as
        empty). ``as_user`` moves the app to the newest place, ``without_token`` removes it; the
        oldest choice goes first when there are more than ``MAX_CHOICES``. With nothing left the
        answer deletes the cookie.
        """
        values = _cookie_values(headers)
        current = self._verified_slugs(values[0], actor) if len(values) == 1 else None
        slugs = [slug for slug in current or [] if slug != app_slug]
        if mode == "as_user":
            slugs.append(app_slug)
        slugs = slugs[-MAX_CHOICES:]
        if not slugs:
            return f"{OPEN_MODE_COOKIE_NAME}=; {_DELETION_ATTRIBUTES}"
        joined = _SLUG_SEPARATOR.join(slugs)
        value = f"{joined}{_SIGNATURE_SEPARATOR}{self._signature(actor, joined)}"
        return f"{OPEN_MODE_COOKIE_NAME}={value}; {_ATTRIBUTES}"

    def _verified_slugs(self, value: str, actor: Actor) -> list[str] | None:
        joined, separator, signature = value.partition(_SIGNATURE_SEPARATOR)
        if not separator or not joined:
            return None
        slugs = joined.split(_SLUG_SEPARATOR)
        if (
            len(slugs) > MAX_CHOICES
            or len(set(slugs)) != len(slugs)
            or not all(_SLUG.fullmatch(slug) for slug in slugs)
        ):
            return None
        expected = self._signature(actor, joined)
        if not hmac.compare_digest(expected.encode(), signature.encode(errors="replace")):
            return None
        return slugs

    def _signature(self, actor: Actor, joined_slugs: str) -> str:
        message = b"\0".join([_SIGNED_CONTEXT, _email_of(actor).encode(), joined_slugs.encode()])
        digest = hmac.digest(self._key, message, hashlib.sha256)
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _cookie_values(headers: HeaderLines) -> list[str]:
    """The value of every pair named exactly ``__Host-appmux_open``, over all ``Cookie`` lines."""
    lines = headers.items() if isinstance(headers, Mapping) else headers
    values: list[str] = []
    for name, line in lines:
        if name.lower() != "cookie":
            continue
        for pair in line.split(";"):
            pair_name, equals, value = pair.partition("=")
            if equals and pair_name.strip() == OPEN_MODE_COOKIE_NAME:
                values.append(value.strip())
    return values


class OpenModeTokenPolicy:
    """The edge's ``ForwardTokenPolicy``: whether the child receives the actor's own token.

    Called after the edge found that the actor may open the app. The rules, in order:

    1. an app that is not OBO-delegated, or unknown, never receives a token;
    2. a person who cannot open the app receives none (the edge refuses them before this, so this
       is a second lock);
    3. a platform admin who is neither the owner nor named by a grant (``authz``'s
       ``app.view_as_user`` is allowed) receives it only if their valid cookie names this app;
    4. any other visitor (the owner, a grant holder, an ordinary user) receives it, as always;
    5. everything else, including "groups unknown" where a group grant could hide, receives none.
    """

    def __init__(self, access: AccessSource, cookies: OpenModeCookies) -> None:
        self._access = access
        self._cookies = cookies

    async def __call__(self, actor: Actor, app_slug: str, headers: HeaderLines) -> bool:
        try:
            return await self._decide(actor, app_slug, headers)
        except Exception:
            # An operational boundary of an auth decision: whatever went wrong must end as "no
            # token", not as a failed request and not as a default of "pass".
            logger.exception(
                "the open-mode decision failed; the access token is not forwarded (app %r)",
                _printable(app_slug),
            )
            return False

    async def _decide(self, actor: Actor, app_slug: str, headers: HeaderLines) -> bool:
        access = await self._access.access_for(actor)
        app = access.data.apps.get(app_slug)
        if app is None or not app.obo_delegated:
            return False
        opened = authorize(actor, Action.APP_OPEN, app_slug, access.data, groups=access.groups)
        if not opened.allowed:
            return False
        as_user = authorize(
            actor, Action.APP_VIEW_AS_USER, app_slug, access.data, groups=access.groups
        )
        if as_user.allowed:
            return app_slug in self._cookies.chosen_as_user(headers, actor)
        return as_user.reason in _VISITOR_REASONS


def _printable(value: str) -> str:
    # The slug comes from a request path; keep a log line short and free of control characters.
    return "".join(c if c.isprintable() else "?" for c in value)[:64]
