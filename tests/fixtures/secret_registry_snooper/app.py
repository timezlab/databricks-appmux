"""Hostile fixture secret_registry_snooper: tries other apps' secrets and the registry.

Run it only inside an appmux sandbox, through the launcher, against a scratch workspace: with the
shared-SP token delivered it makes real calls (design 8.3, 8.4 and 4.2; spec FR-011, US6, US8). It
is a standalone Databricks-style app: ``python app.py`` and the port in ``DATABRICKS_APP_PORT``.
It reads its targets from environment entries that the release adds (all optional, all
non-secret). Without them an attempt is reported ``"not_attempted"``, which proves nothing: the
containment flags below then read false.

    SNOOP_SECRET_REFS        comma-separated ``scope/key`` references of OTHER apps' secrets.
    SNOOP_REGISTRY_HOST      host of the registry database.
    SNOOP_REGISTRY_PORT      port, default 5432.
    SNOOP_REGISTRY_DATABASE  database name, default ``databricks_postgres``.
    SNOOP_REGISTRY_USER      the role to try without a token (a token's own identity is used with
                             one, falling back to this value).

Every attempt is made twice. **Without a token** it sends no credential: no Authorization header,
an empty registry password. **With the token** it uses the credential appmux delivered to this
child, through the unmodified Databricks SDK (``DATABRICKS_AUTH_TYPE=databricks-cli`` and
``DATABRICKS_CLI_PATH``, or a static ``DATABRICKS_TOKEN``); the second case runs only when such a
credential exists, which is the case for a shared-SP or own-SP child and not for a plain one.

The attempts, per case (their names in the report)
    list_scopes        ``GET /api/2.0/secrets/scopes/list``.
    read <scope/key>   ``GET /api/2.0/secrets/get`` for each reference in SNOOP_SECRET_REFS; one
                       ``read`` entry, ``"not_attempted"``, when the variable is empty.
    registry login     a PostgreSQL login to the registry over TLS (SSLRequest, startup message,
                       cleartext password), written with the standard library so no driver has to
                       be in the environment. Any other authentication method the server asks for
                       is reported ``"inconclusive"``.

HTTP paths
    GET /          200 ``{"fixture": "secret_registry_snooper", "status": "ok"}``.
    GET /report    200 JSON, computed on every call (see below).

``/report`` never contains a value that was read. A secret response body is not read at all, and
the token is held in memory only: it goes into the Authorization header and the registry password
and nowhere else. Each attempt is ``{"outcome": ..., "detail": ...}`` where ``outcome`` is one of

    "refused"        the target said no (HTTP status, SQLSTATE) or was unreachable (errno name).
    "succeeded"      the target let the call through: the isolation failed for that attempt.
    "inconclusive"   the probe could not tell (a TLS or certificate failure, on the workspace API
                     and on the registry alike, or an unsupported authentication method). It
                     counts against the isolation: a probe that cannot see must not pass.
    "not_attempted"  the attempt's configuration is missing.

``/report`` keys
    token_delivered            true when the SDK could build a credential from the environment.
    token_identity             the user name the token belongs to, or null (a service principal's
                               application id; not a secret).
    attempted                  how many attempts ran (both cases together).
    without_token              ``{"<attempt name>": result}`` for the attempts above.
    with_token                 the same shape, or null when no token was delivered.
    succeeded                  the attempts that succeeded, as ``"<case>: <attempt>"`` strings.
    inconclusive               the attempts that were inconclusive, same form.
    not_attempted              the attempts that were skipped for missing configuration, same form.
    without_token_contained    true only when every attempt without a token ran and was refused;
                               false when any one succeeded, was inconclusive or was not attempted,
                               so a run that skipped an attempt cannot pass. It must be true for
                               every child, so the test sets the release env entries above first.
    with_token_contained       the same with the token, or null when no token was delivered. With a
                               shared-SP token a ``"succeeded"`` is the accepted residual of design
                               8.4 (the appmux SP's reach) and TD-002, to be judged by the test, not
                               hidden here.
"""

from __future__ import annotations

import errno
import os
import socket
import ssl
import struct
import urllib.error
import urllib.parse
import urllib.request

import uvicorn
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from fastapi import FastAPI

REFUSED = "refused"
SUCCEEDED = "succeeded"
INCONCLUSIVE = "inconclusive"
NOT_ATTEMPTED = "not_attempted"

SECRET_REFS_ENV = "SNOOP_SECRET_REFS"
REGISTRY_HOST_ENV = "SNOOP_REGISTRY_HOST"
REGISTRY_PORT_ENV = "SNOOP_REGISTRY_PORT"
REGISTRY_DATABASE_ENV = "SNOOP_REGISTRY_DATABASE"
REGISTRY_USER_ENV = "SNOOP_REGISTRY_USER"
DEFAULT_REGISTRY_PORT = "5432"
DEFAULT_REGISTRY_DATABASE = "databricks_postgres"

CONNECT_TIMEOUT_SECONDS = 5.0
BEARER_PREFIX = "Bearer "
SECRET_SCOPES_PATH = "/api/2.0/secrets/scopes/list"
SECRET_GET_PATH = "/api/2.0/secrets/get"

# PostgreSQL frontend/backend protocol, version 3.
POSTGRES_SSL_REQUEST_CODE = 80877103
POSTGRES_PROTOCOL_VERSION = 196608
POSTGRES_SSL_ACCEPTED = b"S"
POSTGRES_AUTHENTICATION = b"R"
POSTGRES_ERROR = b"E"
POSTGRES_PASSWORD_MESSAGE = b"p"
POSTGRES_AUTH_OK = 0
POSTGRES_AUTH_CLEARTEXT_PASSWORD = 3
POSTGRES_SQLSTATE_FIELD = b"C"
MAX_POSTGRES_MESSAGE_BYTES = 65536

app = FastAPI()


def outcome(result: str, detail: str) -> dict[str, str]:
    return {"outcome": result, "detail": detail}


# Fixtures are standalone apps and cannot share code, so the hostile ones repeat this helper.
def errno_name(exc: OSError) -> str:
    """The errno name of a failed system call, or the exception type when it carries no errno."""
    if exc.errno is None:
        return type(exc).__name__
    return errno.errorcode.get(exc.errno, type(exc).__name__)


def workspace_url() -> str | None:
    host = os.environ.get("DATABRICKS_HOST", "").strip().rstrip("/")
    if not host:
        return None
    return host if host.startswith("https://") else f"https://{host.removeprefix('http://')}"


def secret_references() -> list[str]:
    """The ``scope/key`` references from the environment, ignoring malformed entries."""
    references = []
    for raw in os.environ.get(SECRET_REFS_ENV, "").split(","):
        scope, _, key = raw.strip().partition("/")
        if scope and key:
            references.append(f"{scope}/{key}")
    return references


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Report a redirect (to a login page, say) as the refusal it is instead of following it."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def tls_failed(exc: ssl.SSLError) -> dict[str, str]:
    """A TLS or certificate failure means the probe could not see the target: inconclusive."""
    return outcome(INCONCLUSIVE, f"TLS failed: {type(exc).__name__}")


def call_workspace_api(url: str, authorization: str | None) -> dict[str, str]:
    """One GET against the workspace; the response body is never read."""
    request = urllib.request.Request(url)
    if authorization is not None:
        request.add_header("Authorization", authorization)
    opener = urllib.request.build_opener(NoRedirectHandler)
    try:
        with opener.open(request, timeout=CONNECT_TIMEOUT_SECONDS) as response:
            return outcome(SUCCEEDED, f"http {response.status}")
    except urllib.error.HTTPError as exc:
        return outcome(REFUSED, f"http {exc.code}")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            return tls_failed(reason)
        return outcome(REFUSED, errno_name(reason) if isinstance(reason, OSError) else str(reason))
    except ssl.SSLError as exc:
        # urllib wraps a handshake failure in URLError, but one raised while the response is read
        # reaches here unwrapped.
        return tls_failed(exc)
    except OSError as exc:
        return outcome(REFUSED, errno_name(exc))


def try_secrets(host_url: str | None, authorization: str | None) -> dict[str, dict[str, str]]:
    if host_url is None:
        return {"list_scopes": outcome(NOT_ATTEMPTED, "DATABRICKS_HOST is not set")}
    attempts = {"list_scopes": call_workspace_api(host_url + SECRET_SCOPES_PATH, authorization)}
    references = secret_references()
    if not references:
        attempts["read"] = outcome(NOT_ATTEMPTED, f"{SECRET_REFS_ENV} is empty")
    for reference in references:
        scope, _, key = reference.partition("/")
        query = urllib.parse.urlencode({"scope": scope, "key": key})
        attempts[f"read {reference}"] = call_workspace_api(
            f"{host_url}{SECRET_GET_PATH}?{query}", authorization
        )
    return attempts


def read_exactly(connection: socket.socket, size: int) -> bytes:
    received = bytearray()
    while len(received) < size:
        chunk = connection.recv(size - len(received))
        if not chunk:
            raise ConnectionError("the registry closed the connection")
        received.extend(chunk)
    return bytes(received)


def read_postgres_message(connection: socket.socket) -> tuple[bytes, bytes]:
    message_type = read_exactly(connection, 1)
    (length,) = struct.unpack("!i", read_exactly(connection, 4))
    if not 4 <= length <= MAX_POSTGRES_MESSAGE_BYTES:
        raise ConnectionError("the registry sent a message of an unexpected length")
    return message_type, read_exactly(connection, length - 4)


def authentication_code(payload: bytes) -> int | None:
    return struct.unpack_from("!i", payload)[0] if len(payload) >= 4 else None


def error_sqlstate(payload: bytes) -> str:
    for field in payload.split(b"\0"):
        if field[:1] == POSTGRES_SQLSTATE_FIELD:
            return field[1:].decode("ascii", errors="replace")
    return "unknown"


def negotiate_login(
    connection: socket.socket, user: str, database: str, password: str
) -> dict[str, str]:
    parameters = f"user\0{user}\0database\0{database}\0\0".encode()
    body = struct.pack("!i", POSTGRES_PROTOCOL_VERSION) + parameters
    connection.sendall(struct.pack("!i", 4 + len(body)) + body)
    message_type, payload = read_postgres_message(connection)
    if message_type == POSTGRES_AUTHENTICATION:
        if authentication_code(payload) == POSTGRES_AUTH_CLEARTEXT_PASSWORD:
            secret = password.encode() + b"\0"
            connection.sendall(
                POSTGRES_PASSWORD_MESSAGE + struct.pack("!i", 4 + len(secret)) + secret
            )
            message_type, payload = read_postgres_message(connection)
        if message_type == POSTGRES_AUTHENTICATION:
            code = authentication_code(payload)
            if code == POSTGRES_AUTH_OK:
                return outcome(SUCCEEDED, "the registry accepted the login")
            return outcome(INCONCLUSIVE, f"the registry asked for authentication method {code}")
    if message_type == POSTGRES_ERROR:
        return outcome(REFUSED, f"registry error {error_sqlstate(payload)}")
    return outcome(INCONCLUSIVE, f"unexpected registry message {message_type!r}")


def try_registry_login(user: str | None, password: str) -> dict[str, str]:
    host = os.environ.get(REGISTRY_HOST_ENV)
    if not host:
        return outcome(NOT_ATTEMPTED, f"{REGISTRY_HOST_ENV} is not set")
    if not user:
        return outcome(NOT_ATTEMPTED, f"{REGISTRY_USER_ENV} is not set and no token identity")
    port = int(os.environ.get(REGISTRY_PORT_ENV, DEFAULT_REGISTRY_PORT))
    database = os.environ.get(REGISTRY_DATABASE_ENV, DEFAULT_REGISTRY_DATABASE)
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS) as plain:
            plain.sendall(struct.pack("!ii", 8, POSTGRES_SSL_REQUEST_CODE))
            if plain.recv(1) != POSTGRES_SSL_ACCEPTED:
                return outcome(INCONCLUSIVE, "the registry did not offer TLS")
            context = ssl.create_default_context()
            with context.wrap_socket(plain, server_hostname=host) as secure:
                return negotiate_login(secure, user, database, password)
    except ssl.SSLError as exc:
        return tls_failed(exc)
    except OSError as exc:
        return outcome(REFUSED, errno_name(exc))


def delivered_credential() -> tuple[str, str | None] | None:
    """The bearer token the unmodified SDK builds from the environment, and its identity."""
    try:
        config = Config()
        authorization = config.authenticate().get("Authorization", "")
    except (ValueError, OSError):
        return None
    if not authorization.startswith(BEARER_PREFIX):
        return None
    try:
        identity = WorkspaceClient(config=config).current_user.me().user_name
    except (ValueError, OSError):
        # SDK errors derive from IOError; the identity is optional, the registry user can be set.
        identity = None
    return authorization.removeprefix(BEARER_PREFIX), identity


def attempts_with_outcome(cases: dict[str, dict[str, dict[str, str]]], result: str) -> list[str]:
    """``"<case>: <attempt>"`` for every attempt, in every case, that ended with ``result``."""
    return [
        f"{case}: {name}"
        for case, attempts in cases.items()
        for name, attempt in attempts.items()
        if attempt["outcome"] == result
    ]


def is_contained(attempts: dict[str, dict[str, str]]) -> bool:
    """True only when every attempt ran and was refused.

    A skipped attempt (missing configuration) and an inconclusive one both read false: a probe that
    did not try, or could not see, proves nothing, so it must not pass (fail closed).
    """
    return all(attempt["outcome"] == REFUSED for attempt in attempts.values())


@app.get("/")
def index() -> dict[str, str]:
    return {"fixture": "secret_registry_snooper", "status": "ok"}


@app.get("/report")
def report() -> dict[str, object]:
    host_url = workspace_url()
    configured_user = os.environ.get(REGISTRY_USER_ENV)
    without_token = {
        **try_secrets(host_url, None),
        "registry login": try_registry_login(configured_user, ""),
    }
    cases = {"without token": without_token}

    credential = delivered_credential()
    with_token: dict[str, dict[str, str]] | None = None
    identity = None
    if credential is not None:
        token, identity = credential
        with_token = {
            **try_secrets(host_url, BEARER_PREFIX + token),
            "registry login": try_registry_login(identity or configured_user, token),
        }
        cases["with token"] = with_token

    not_attempted = attempts_with_outcome(cases, NOT_ATTEMPTED)
    return {
        "fixture": "secret_registry_snooper",
        "token_delivered": credential is not None,
        "token_identity": identity,
        "attempted": sum(len(attempts) for attempts in cases.values()) - len(not_attempted),
        "without_token": without_token,
        "with_token": with_token,
        "succeeded": attempts_with_outcome(cases, SUCCEEDED),
        "inconclusive": attempts_with_outcome(cases, INCONCLUSIVE),
        "not_attempted": not_attempted,
        "without_token_contained": is_contained(without_token),
        "with_token_contained": None if with_token is None else is_contained(with_token),
    }


if __name__ == "__main__":
    uvicorn.run(
        app,
        # appmux and the platform both set UVICORN_HOST=0.0.0.0 (design 5.4). Run without it, the
        # hostile /report must not be open on every interface, so the default is loopback.
        host=os.environ.get("UVICORN_HOST", "127.0.0.1"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
