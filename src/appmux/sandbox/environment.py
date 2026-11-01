"""The environment a child receives (design 5.4): built from what it is given, nothing else.

Pure: no I/O and no read of ``os.environ``, so no variable of appmux's own environment can reach
a child. Later wins: the ``app.yaml`` entries, then the release ``env`` entries, then appmux's
variables, which nothing can override.

This module is the only place a secret value is placed in an environment. The caller resolves
each ``valueFrom`` resource first (a ``secret`` to its value, any other type to its target string)
and passes the result as ``resource_values``. No error message, log line or ``repr`` here holds a
value, only the variable name or the resource key; the module does not log or print at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# The value the platform gives the framework host variables; the child still listens only on
# loopback inside its own network namespace.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - an environment value for the child, not a bind address
LOCALE = "C.UTF-8"


class ChildEnvironmentError(ValueError):
    """The environment cannot be built; the message names a variable or resource, never a value."""


class ChildVariables(dict[str, str]):
    """Variable names to values, as handed to ``exec``; its ``repr`` lists the names only.

    The values can be credentials or resolved secrets, so a log line that formats this object
    (``%s``, ``%r``, an f-string, a traceback frame) must not print them. The mapping itself is a
    plain ``dict``: it compares equal to one and can be passed wherever the launcher takes an
    environment. A copy made with ``dict(...)`` or ``{**...}`` is a plain ``dict`` again, so the
    protection ends where the caller makes one.
    """

    def __repr__(self) -> str:
        return f"{type(self).__name__}(names={sorted(self)})"


@dataclass(frozen=True, slots=True)
class EnvEntry:
    """One ``env`` entry of ``app.yaml`` or of a release: a plain value or a resource key."""

    name: str
    value: str | None = None
    value_from: str | None = None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.value_from is None):
            raise ChildEnvironmentError(
                f"env entry {self.name!r} needs exactly one of value and value_from"
            )


@dataclass(frozen=True, slots=True)
class ChildIdentity:
    """What appmux knows about one child when it starts it.

    ``credentials`` are the variables of the app's mode (design 8.5), passed in by the caller;
    they come before appmux's own variables, so they cannot replace one of those. They are left
    out of ``repr`` and ``str`` because their values are credentials.
    """

    slug: str
    port: int
    workspace_host: str
    workspace_id: str
    parent_url: str
    home_directory: str  # the child's private writable temp directory
    credentials: Mapping[str, str] = field(default_factory=dict, repr=False)


def appmux_variables(child: ChildIdentity) -> ChildVariables:
    """The variables appmux sets for a child, which neither ``app.yaml`` nor a release may set.

    Not set on purpose: ``UV_PYTHON_INSTALL_DIR`` (v1 supports only the parent's Python),
    ``OTEL_*`` (no telemetry in v1), the client id and secret of appmux's own service principal,
    and every Streamlit base-path variable (it breaks Streamlit when the prefix is stripped).
    """
    port = str(child.port)
    base_path = f"/{child.slug}"
    own = {
        "DATABRICKS_APP_PORT": port,
        "DATABRICKS_APP_NAME": child.slug,
        "DATABRICKS_HOST": child.workspace_host,
        "DATABRICKS_WORKSPACE_ID": child.workspace_id,
        "DATABRICKS_APP_URL": f"{child.parent_url.rstrip('/')}/{child.slug}",
        "APPMUX_BASE_PATH": base_path,
        # The platform's own values, handled one by one (research D16).
        "LC_CTYPE": LOCALE,
        "PYTHONUNBUFFERED": "1",
        "HOME": child.home_directory,
        # The framework variables of a standalone App, all pointing at the child's port.
        "PORT": port,
        "UVICORN_PORT": port,
        "UVICORN_HOST": ALL_INTERFACES,
        "FLASK_RUN_PORT": port,
        "FLASK_RUN_HOST": ALL_INTERFACES,
        "GRADIO_SERVER_PORT": port,
        "GRADIO_SERVER_NAME": ALL_INTERFACES,
        "GRADIO_ANALYTICS_ENABLED": "false",
        "STREAMLIT_SERVER_PORT": port,
        "STREAMLIT_SERVER_ADDRESS": ALL_INTERFACES,
        "STREAMLIT_SERVER_HEADLESS": "true",
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
        "STREAMLIT_SERVER_ENABLE_CORS": "false",
        "STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION": "false",
        # The base path, so unmodified code generates prefixed links (spike 5).
        "UVICORN_ROOT_PATH": base_path,
        "GRADIO_ROOT_PATH": base_path,
        "DASH_REQUESTS_PATHNAME_PREFIX": f"{base_path}/",
        "DASH_ROUTES_PATHNAME_PREFIX": "/",
    }
    return ChildVariables({**child.credentials, **own})


def build_child_environment(
    *,
    app_yaml_env: Sequence[EnvEntry],
    release_env: Sequence[EnvEntry],
    appmux_variables: Mapping[str, str],
    resource_values: Mapping[str, str] | None = None,
) -> ChildVariables:
    """The complete environment of a child; nothing else is inherited.

    ``resource_values`` maps a resource key to its resolved value. Every ``value_from`` of both
    layers must be in it, even one a later layer overrides: a release that names a resource it
    cannot read is refused, never started without the variable.
    """
    resolved = resource_values or {}
    environment = ChildVariables()
    for entry in (*app_yaml_env, *release_env):
        environment[entry.name] = _entry_value(entry, resolved)
    environment.update(appmux_variables)
    for name, value in environment.items():
        _check_exportable(name, value)
    return environment


def _entry_value(entry: EnvEntry, resource_values: Mapping[str, str]) -> str:
    if entry.value is not None:
        return entry.value
    # __post_init__ guarantees value_from is set whenever value is not.
    key = entry.value_from or ""
    if key not in resource_values:
        raise ChildEnvironmentError(
            f"env {entry.name} refers to resource {key!r}, which has no resolved value; "
            "attach the resource and start the release again"
        )
    return resource_values[key]


def _check_exportable(name: str, value: str) -> None:
    if not name or "=" in name or "\x00" in name:
        raise ChildEnvironmentError(
            f"environment variable name {name!r} cannot be exported: "
            "it is empty or holds '=' or a NUL byte"
        )
    if "\x00" in value:
        raise ChildEnvironmentError(
            f"environment variable {name} cannot be exported: its value holds a NUL byte"
        )
