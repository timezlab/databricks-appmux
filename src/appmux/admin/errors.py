"""The errors an admin route raises, and the one function that turns any exception into a response.

Every error of the admin API has the body of contracts/admin-api.md (``ErrorBody``)::

    {"error": {"code": "slug_taken", "message": "slug 'reports' is already taken"}}

``error_response`` is the only place that builds it. What it puts in the body:

* an ``AdminError``: its status, its code and the message the route author wrote. Route authors
  write messages for people, so a message never holds an exception's text, a submitted value, an
  SQL statement or a path;
* a ``RegistryError``: the status of its class and the class's fixed message, never the raised
  text (a driver message must not reach a person);
* a Starlette ``HTTPException`` (an unknown route, a wrong method): a fixed message per status;
* anything else: ``internal_error`` and a fixed sentence. The exception, with its traceback, is
  logged once on the server and nowhere else.

``validation_error`` turns a Pydantic ``ValidationError`` into a ``BadRequest`` that names fields
and never repeats the submitted value.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from http import HTTPStatus
from typing import ClassVar, Final

from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from appmux.registry.errors import (
    ChangedByOthers,
    InvalidRow,
    RegistryError,
    RegistryRoleMissing,
    RegistryUnavailable,
    RowAlreadyExists,
    RowMissing,
)

logger = logging.getLogger(__name__)

INTERNAL_ERROR_CODE: Final = "internal_error"
INTERNAL_ERROR_MESSAGE: Final = (
    "something went wrong on the server; try again, and ask a platform admin to check the "
    "server log if it keeps happening"
)

_CODE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_FIELDS_NAMED: Final = 10
_MAX_FIELD_NAME_LENGTH: Final = 64


class AdminError(Exception):
    """An error a route raises on purpose; ``status``, ``code`` and the message reach the client.

    ``code`` is a short snake_case word the UI may branch on; a route may pass its own (the
    contract's example is ``slug_taken``). The message says what failed and what to do next, and it
    is always safe to show: write it from the route's own knowledge, never from an exception.
    """

    status: ClassVar[int] = 500
    code: ClassVar[str] = INTERNAL_ERROR_CODE
    default_message: ClassVar[str] = INTERNAL_ERROR_MESSAGE

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.message = message if message is not None else self.default_message
        self.error_code = code if code is not None else self.code
        if _CODE_PATTERN.fullmatch(self.error_code) is None:
            raise ValueError("an error code is lower-case words joined by '_', up to 64 characters")
        super().__init__(self.message)


class BadRequest(AdminError):
    """The body fails its schema, or a design rule refuses it; the message names the field."""

    status = 400
    code = "bad_request"
    default_message = "the request is not valid"


class Unauthorized(AdminError):
    """No usable forwarded identity."""

    status = 401
    code = "unauthorized"
    default_message = "no forwarded identity; open this page through the Databricks App URL"


class Forbidden(AdminError):
    """``authorize()`` denied; the caller already sees the screen, so the denial is stated."""

    status = 403
    code = "forbidden"
    default_message = "you are not allowed to do that"


class NotFound(AdminError):
    """No such app, release or request (also: one the caller may not even open)."""

    status = 404
    code = "not_found"
    default_message = "not found"


class Conflict(AdminError):
    """A stale ``version``, or a state that forbids the action."""

    status = 409
    code = "conflict"
    default_message = "the current state does not allow that"


class Unavailable(AdminError):
    """The registry is down and the route needs it."""

    status = 503
    code = "unavailable"
    default_message = RegistryUnavailable.default_message


# Registry errors by class, with the status each answers. ``RegistryError`` itself and any class
# not listed is a defect of the registry, not of the request, and answers as ``internal_error``.
_REGISTRY_STATUS: Final[Mapping[type[RegistryError], int]] = {
    RegistryUnavailable: 503,
    RegistryRoleMissing: 403,
    ChangedByOthers: 409,
    RowAlreadyExists: 409,
    RowMissing: 404,
    InvalidRow: 400,
}

_HTTP_CODES: Final[Mapping[int, str]] = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
}
_HTTP_MESSAGES: Final[Mapping[int, str]] = {
    400: "the request is not valid",
    404: "not found",
    405: "that method is not allowed on this route",
}

# What a field's problem is called in a message: the Pydantic error types a body can produce, in
# words that carry no value. Every other type is "is not valid".
_FIELD_PROBLEMS: Final[Mapping[str, str]] = {
    "missing": "is required",
    "extra_forbidden": "is not allowed",
}


def validation_error(exc: ValidationError) -> BadRequest:
    """A ``BadRequest`` naming the fields ``exc`` rejects and never a value.

    A field name comes from the submitted body (an unknown key), so it is cut to printable ASCII
    and a short length. Pydantic's own message is not used: it can repeat the input.
    """
    problems: list[str] = []
    for detail in exc.errors(include_input=False, include_url=False, include_context=False):
        if detail["type"] in ("json_invalid", "model_attributes_type"):
            return BadRequest("the body is not valid JSON of the expected shape")
        name = ".".join(str(part) for part in detail["loc"]) or "body"
        name = "".join(ch if ch.isascii() and ch.isprintable() else "?" for ch in name)
        name = name[:_MAX_FIELD_NAME_LENGTH]
        problem = f"{name} {_FIELD_PROBLEMS.get(detail['type'], 'is not valid')}"
        if problem not in problems:
            problems.append(problem)
    if not problems:
        return BadRequest()
    shown = problems[:_MAX_FIELDS_NAMED]
    extra = len(problems) - len(shown)
    suffix = f"; and {extra} more" if extra > 0 else ""
    return BadRequest(f"the request is not valid: {'; '.join(shown)}{suffix}")


def _http_phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase.lower()
    except ValueError:
        return "error"


def _describe(exc: BaseException) -> tuple[int, str, str, Mapping[str, str] | None]:
    """Status, code, message and extra headers of the response for ``exc``."""
    if isinstance(exc, AdminError):
        return exc.status, exc.error_code, exc.message, None
    if isinstance(exc, RegistryError):
        for error_type, status in _REGISTRY_STATUS.items():
            if isinstance(exc, error_type):
                return status, exc.code, error_type.default_message, None
    elif isinstance(exc, HTTPException):
        status = exc.status_code
        code = _HTTP_CODES.get(status, "http_error")
        return status, code, _HTTP_MESSAGES.get(status, _http_phrase(status)), exc.headers
    # Not an error a route chose: the server's defect. The traceback goes to the log once and the
    # client gets a sentence that says nothing about the cause.
    logger.error("unhandled exception in the admin API", exc_info=exc)
    return 500, INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE, None


def error_response(exc: BaseException) -> JSONResponse:
    """The contract's error response for any exception."""
    status, code, message, headers = _describe(exc)
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status,
        headers=dict(headers) if headers is not None else None,
    )
