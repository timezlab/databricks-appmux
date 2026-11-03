"""``GET /apps/{app}/environment``: what an app's child is started with, for its app admins (T191).

It describes the app's active release and resolves nothing: no secret is read, no token is minted
and no command is run in the child's sandbox, so the answer cannot hold a value appmux keeps from
the person. What it shows:

* ``runtime``: the release's runtime, the platform's Python (every child runs the parent's) and
  the platform's Node.js (``node --version``, run once per process without a shell);
* ``variables``: the layering of ``sandbox/environment.py`` read back, without its credential
  layer. The ``env`` entries of the release's ``app.yaml``, then those of its config, each with
  its value, or, for a ``valueFrom``, its resource key and the resource's target (a ``secret``
  shows no value at all); then the variables appmux sets for every child (``appmux_variables``),
  which win over both. The credential variables of the app's Databricks mode (design 8.5) are
  left out: their values are credentials and their names depend on a secret's state. At start
  they also replace an ``env`` entry of the same name, so such an entry is shown here with its
  own value although the child gets the credential in its place;
* ``packages``: what the release declares in its built directory (``requirements.txt``, the
  ``dependencies`` and ``devDependencies`` of ``package.json``), each in declaration order, the
  wheels it recorded, and, for a Python app, the distributions of the platform's environment.

A declared line or dependency that is not a plain requirement or version (a URL, a git or path
specifier, an option, another file, an npm alias or workspace) is left out without being echoed,
because a URL can hold a credential. A release that this container has not built (an app not
started since appmux started) has no directory, so it lists no declared package. Nothing is
audited: this is a read of what an app admin can already see on the release.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Final, Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext
from appmux.admin.schemas import (
    SECRET_TYPE,
    AppEnvironment,
    EnvironmentPackage,
    EnvironmentRuntime,
    EnvironmentVariable,
)
from appmux.authz.actions import Action
from appmux.reconciler.build import REQUIREMENTS_FILE
from appmux.reconciler.child_env import CHILD_HOME, PlatformIdentity
from appmux.reconciler.children import DEFAULT_CHILD_PORT
from appmux.registry.models import AppResource, EnvEntry, Release
from appmux.registry.snapshot import RegistrySnapshot
from appmux.sandbox.environment import ChildIdentity, appmux_variables

logger = logging.getLogger(__name__)

PACKAGE_JSON_FILE: Final = "package.json"
# A larger manifest is not read: the page lists packages, it does not parse a dump.
MAX_MANIFEST_BYTES: Final = 256 * 1024
NODE_VERSION_TIMEOUT_S: Final = 5.0
_PACKAGE_JSON_SECTIONS: Final = ("dependencies", "devDependencies")
# `pip` ends a comment at a '#' that starts the line or follows whitespace.
_INLINE_COMMENT: Final = re.compile(r"(^|\s)#.*$")
_NODE_VERSION: Final = re.compile(r"v(\d+\.\d+\.\d+[0-9A-Za-z.+-]*)")
# A semver range (``^1.2.0``, ``>=1 <2 || 3.x``, ``*``) or a dist-tag (``latest``); npm reads a
# specifier that starts with a dot as a directory.
_PLAIN_NPM_VERSION: Final = re.compile(r"(?!\.)[0-9A-Za-z.+*^~<>=| _-]*")

NodeVersionSource = Callable[[], Awaitable[str | None]]
RuntimeKind = Literal["python", "node", "unknown"]


@dataclass(frozen=True, slots=True)
class ChildSetup:
    """What a child is started with that the registry does not hold."""

    platform: PlatformIdentity
    releases_dir: Path  # where the builder unpacks ``<slug>/<release_no>``
    port: int = DEFAULT_CHILD_PORT


class NodeVersion:
    """The platform's ``node --version``, asked once and remembered (``None`` when not found)."""

    def __init__(
        self, *, executable: str | None = None, timeout_s: float = NODE_VERSION_TIMEOUT_S
    ) -> None:
        self._executable = executable
        self._timeout_s = timeout_s
        self._lock = asyncio.Lock()
        self._is_asked = False
        self._version: str | None = None

    async def __call__(self) -> str | None:
        async with self._lock:
            if not self._is_asked:
                self._version = await self._ask()
                self._is_asked = True
        return self._version

    async def _ask(self) -> str | None:
        executable = self._executable or shutil.which("node")
        if executable is None:
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "--version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.warning("node --version could not be run (%s); no Node.js version shown", exc)
            return None
        try:
            output, _ = await asyncio.wait_for(process.communicate(), self._timeout_s)
        except TimeoutError:
            process.kill()
            await process.wait()
            logger.warning("node --version did not answer in %s s", self._timeout_s)
            return None
        found = _NODE_VERSION.fullmatch(output.decode("ascii", "replace").strip())
        if process.returncode != 0 or found is None:
            logger.warning("node --version gave no version (exit status %s)", process.returncode)
            return None
        return found.group(1)


class EnvironmentRoutes:
    """The route provider of ``GET /apps/{app}/environment``."""

    def __init__(self, setup: ChildSetup, *, node_version: NodeVersionSource | None = None) -> None:
        self._setup = setup
        self._node_version = node_version if node_version is not None else NodeVersion()
        # The platform's environment does not change while appmux runs: read it once.
        self._platform_packages: tuple[EnvironmentPackage, ...] | None = None

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def read_environment(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.APP_VIEW_ENVIRONMENT, slug)
            snapshot = context.registry.snapshot
            release = _active_release(snapshot, slug)
            runtime = EnvironmentRuntime(
                kind=_runtime_kind(release),
                python_version=platform.python_version(),
                node_version=await self._node_version(),
            )
            if release is None:
                body = AppEnvironment(release=None, runtime=runtime, variables=[], packages=[])
            else:
                body = AppEnvironment(
                    release=release.release_no,
                    runtime=runtime,
                    variables=self._variables(release, snapshot.resources_for(slug)),
                    packages=await self._packages(release),
                )
            return JSONResponse(body.model_dump(mode="json"))

        return [Route("/apps/{app}/environment", read_environment, methods=["GET"])]

    def _variables(
        self, release: Release, resources: Sequence[AppResource]
    ) -> list[EnvironmentVariable]:
        """The child's variables by layer, except the credentials of the app's mode.

        The identity is built without credentials on purpose: listing them would name variables
        whose values are credentials, and their names depend on a secret's state. So an ``env``
        entry that one of them replaces at start is shown here with its own value.
        """
        by_key = {resource.key: resource for resource in resources}
        rows: dict[str, EnvironmentVariable] = {}
        # Later wins, as in ``build_child_environment``: app.yaml, the release's config, appmux.
        for entry in (*release.detected.env, *release.config.env):
            rows[entry.name] = _entry_row(entry, by_key)
        identity = ChildIdentity(
            slug=release.app_slug,
            port=self._setup.port,
            workspace_host=self._setup.platform.workspace_host,
            workspace_id=self._setup.platform.workspace_id,
            parent_url=self._setup.platform.parent_url,
            home_directory=CHILD_HOME,
        )
        for name, value in appmux_variables(identity).items():
            rows[name] = EnvironmentVariable(
                name=name, value=value, source="platform", resource_key=None, is_secret=False
            )
        return list(rows.values())

    async def _packages(self, release: Release) -> list[EnvironmentPackage]:
        built = self._setup.releases_dir / release.app_slug / str(release.release_no)
        declared = await asyncio.to_thread(_declared_packages, built)
        wheels = [_wheel_package(wheel.filename) for wheel in release.wheels]
        provided: Sequence[EnvironmentPackage] = ()
        if _runtime_kind(release) == "python":
            if self._platform_packages is None:
                self._platform_packages = await asyncio.to_thread(_platform_packages)
            provided = self._platform_packages
        return [*declared, *wheels, *provided]


def _active_release(snapshot: RegistrySnapshot, slug: str) -> Release | None:
    return next(
        (
            release
            for release in snapshot.releases_for(slug)
            if release.status == "active" and release.deleted_at is None
        ),
        None,
    )


def _runtime_kind(release: Release | None) -> RuntimeKind:
    runtime = release.detected.runtime if release is not None else None
    if runtime == "python":
        return "python"
    if runtime == "node":
        return "node"
    return "unknown"


def _entry_row(entry: EnvEntry, resources: Mapping[str, AppResource]) -> EnvironmentVariable:
    if entry.value_from is None:
        return EnvironmentVariable(
            name=entry.name, value=entry.value, source="config", resource_key=None, is_secret=False
        )
    resource = resources.get(entry.value_from)
    is_secret = resource is not None and resource.type == SECRET_TYPE
    # A non-secret resource gives the child its target (``secrets/resolver.py``); a secret gives
    # its value, which is never read here; a key the app does not have gives nothing.
    value = resource.target if resource is not None and not is_secret else None
    return EnvironmentVariable(
        name=entry.name,
        value=value,
        source="resource",
        resource_key=entry.value_from,
        is_secret=is_secret,
    )


def _declared_packages(built: Path) -> list[EnvironmentPackage]:
    """The packages the release's own files declare; none for a release not built here."""
    rows: list[EnvironmentPackage] = []
    requirements = _read_manifest(built / REQUIREMENTS_FILE)
    if requirements is not None:
        rows.extend(_requirement_rows(requirements))
    package_json = _read_manifest(built / PACKAGE_JSON_FILE)
    if package_json is not None:
        rows.extend(_package_json_rows(package_json))
    return rows


def _read_manifest(path: Path) -> str | None:
    # The builder unpacks regular files only (``reconciler/build.py``), so no link is followed.
    try:
        with path.open("rb") as manifest:
            raw = manifest.read(MAX_MANIFEST_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("%s of a release could not be read (%s)", path.name, exc.strerror)
        return None
    if len(raw) > MAX_MANIFEST_BYTES:
        logger.info("%s is larger than %d bytes; its packages are not listed", path.name, len(raw))
        return None
    return raw.decode("utf-8", "replace")


def _requirement_rows(text: str) -> list[EnvironmentPackage]:
    rows: list[EnvironmentPackage] = []
    for physical in text.splitlines():
        line = _INLINE_COMMENT.sub("", physical).strip().removesuffix("\\").strip()
        if not line or line.startswith("-"):
            continue  # an option, another file, or a `--hash` continuation
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            continue  # a path or a line pip alone understands; never echoed
        if requirement.url is not None:
            continue  # a URL can hold a credential
        specifier = str(requirement.specifier)
        rows.append(
            EnvironmentPackage(name=requirement.name, version=specifier or None, source="release")
        )
    return rows


def _package_json_rows(text: str) -> list[EnvironmentPackage]:
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(manifest, dict):
        return []
    rows: list[EnvironmentPackage] = []
    for section in _PACKAGE_JSON_SECTIONS:
        dependencies = manifest.get(section)
        if not isinstance(dependencies, dict):
            continue
        for name, version in dependencies.items():
            if isinstance(version, str) and not _is_plain_npm_version(version):
                continue  # a URL, git, path, alias or workspace specifier; never echoed
            rows.append(
                EnvironmentPackage(
                    name=name,
                    version=version if isinstance(version, str) else None,
                    source="release",
                )
            )
    return rows


def _is_plain_npm_version(specifier: str) -> bool:
    """Whether ``specifier`` is a version, a range or a dist-tag, which npm resolves by name.

    An allow-list, so a specifier shape not named here is left out rather than shown: every other
    kind (``git+https://``, ``file:``, ``link:``, ``workspace:``, ``npm:`` aliases, ``org/repo``,
    ``../path``) has a ``:``, a ``/``, a ``#``, an ``@`` or a leading ``.``, which this refuses.
    """
    return _PLAIN_NPM_VERSION.fullmatch(specifier) is not None


def _wheel_package(filename: str) -> EnvironmentPackage:
    try:
        name, version, _, _ = parse_wheel_filename(filename)
    except InvalidWheelFilename:
        # The registry holds what the Update stored; a name it cannot parse is shown as it is.
        return EnvironmentPackage(name=filename, version=None, source="wheel")
    return EnvironmentPackage(name=name, version=str(version), source="wheel")


def _platform_packages() -> tuple[EnvironmentPackage, ...]:
    """The distributions of this process's environment, which a Python child imports from."""
    seen: dict[str, EnvironmentPackage] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        version = distribution.metadata["Version"]
        if not name:
            continue
        # An earlier path entry shadows a later copy, as it does for `import`.
        seen.setdefault(
            canonicalize_name(name),
            EnvironmentPackage(name=name, version=version or None, source="platform"),
        )
    return tuple(sorted(seen.values(), key=lambda package: canonicalize_name(package.name)))
