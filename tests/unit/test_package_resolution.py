"""Resolving a child's requirements: environment, then wheels, then an index (FR-031, design 5.3).

The environment is built from real ``.dist-info`` folders and read with ``importlib.metadata``, the
store is the real ``ContentStore`` over an in-memory workspace, and the wheel sources are either
recording fakes (when a test counts or orders fetches) or the real ``WorkspaceWheelSource`` over an
in-memory workspace holding the wheels the fixtures build. No test opens a socket or starts pip.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
from collections.abc import Mapping, Sequence
from importlib.metadata import distributions
from pathlib import Path
from types import ModuleType

import pytest
from fake_workspace import FakeWorkspace
from packaging.tags import Tag

from appmux.packages.environment import Environment, read_environment
from appmux.packages.modes import PackageMode
from appmux.packages.resolve import (
    Recheck,
    Resolution,
    recheck_environment,
    resolve_requirements,
)
from appmux.packages.store import ContentStore
from appmux.packages.wheels import (
    STORE_SOURCE,
    ResolvedWheel,
    WheelSource,
    WheelStore,
    WorkspaceWheelSource,
)

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
STORE_FOLDER = "/Workspace/Users/00000000-example/appmux-store"
WHEEL_FOLDER = "/Workspace/Users/dana@example.com/wheels/sales"
SP_TOKEN = "sp-token-example"  # noqa: S105 - an obviously fake label
PRESSER_TOKEN = "tok-test"  # noqa: S105 - an obviously fake label
TAGS = (Tag("cp311", "cp311", "manylinux2014_x86_64"), Tag("py3", "none", "any"))

ENVIRONMENT_PACKAGES = {
    "fastapi": "0.115.0",
    "uvicorn": "0.30.6",
    "starlette": "0.38.6",
    "pydantic": "2.13.4",
    "appdirs": "1.4.4",
    "local-pkg": "1.2.3",
}


def make_environment(root: Path, packages: Mapping[str, str]) -> Environment:
    root.mkdir(parents=True, exist_ok=True)
    for name, version in packages.items():
        folder = root / f"{name.replace('-', '_')}-{version}.dist-info"
        folder.mkdir()
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        (folder / "METADATA").write_text(metadata, encoding="utf-8")
    return read_environment(distributions(path=[str(root)]))


@pytest.fixture
def environment(tmp_path: Path) -> Environment:
    return make_environment(tmp_path / "site", ENVIRONMENT_PACKAGES)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hashed(name: str, version: str, *wheels: bytes) -> str:
    hashes = " ".join(f"--hash=sha256:{sha256_of(data)}" for data in wheels)
    return f"{name}=={version} {hashes}"


def wheel_name(name: str, version: str) -> str:
    return f"{name.replace('-', '_')}-{version}-py3-none-any.whl"


class FakeSource:
    """A wheel source with fixed files that records the order of what is listed and fetched."""

    def __init__(self, label: str, files: Mapping[str, bytes]) -> None:
        self.label = label
        self._files = dict(files)
        self.log: list[str] = []

    async def list_filenames(self) -> list[str]:
        self.log.append("list")
        return sorted(self._files)

    async def fetch(self, filename: str, destination: Path) -> None:
        self.log.append(f"fetch {filename}")
        await asyncio.to_thread(destination.write_bytes, self._files[filename])

    @property
    def fetched(self) -> list[str]:
        return [entry.removeprefix("fetch ") for entry in self.log if entry.startswith("fetch ")]


def new_store() -> tuple[WheelStore, FakeWorkspace]:
    workspace = FakeWorkspace(required_token=SP_TOKEN)
    content = ContentStore(workspace, folder=STORE_FOLDER, token_source=lambda: SP_TOKEN)
    return WheelStore(content), workspace


async def resolve(
    text: str,
    environment: Environment,
    tmp_path: Path,
    *,
    mode: PackageMode = PackageMode.WHEELHOUSE,
    sources: Sequence[WheelSource] = (),
    store: WheelStore | None = None,
    stored_filenames: Mapping[str, str] | None = None,
) -> Resolution:
    return await resolve_requirements(
        text,
        mode=mode,
        environment=environment,
        store=store or new_store()[0],
        sources=sources,
        stored_filenames=stored_filenames or {},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )


# --- 1. the environment first -------------------------------------------------------------------


async def test_a_lock_the_environment_satisfies_fetches_nothing_and_builds_no_overlay(
    environment: Environment, tmp_path: Path
) -> None:
    source = FakeSource("src", {wheel_name("unneeded", "1.0"): b"decoy"})
    text = "fastapi==0.115.0\nstarlette>=0.37\npydantic>=2\n"

    result = await resolve(text, environment, tmp_path, sources=[source])

    assert result.failure_reason() is None
    assert (result.total_count, result.from_environment) == (3, 3)
    assert result.wheels == ()
    assert result.index_requirements == ()
    assert not result.needs_overlay
    assert result.summary() == "3 of 3 from the environment, 0 from wheels"
    assert source.log == []  # a wheel that is present and not named is not even listed


async def test_a_hashed_pin_the_environment_already_meets_uses_the_environments_copy(
    environment: Environment, tmp_path: Path
) -> None:
    uvicorn = b"a wheel of uvicorn 0.30.6"
    source = FakeSource("src", {wheel_name("uvicorn", "0.30.6"): uvicorn})
    text = hashed("uvicorn", "0.30.6", uvicorn) + "\n"

    result = await resolve(text, environment, tmp_path, sources=[source])

    assert result.from_environment == 1
    assert result.wheels == ()
    assert source.log == []


# --- 2. otherwise a hashed wheel, which overwrites the environment's copy -----------------------


async def test_a_pin_the_environment_lacks_is_taken_from_a_hashed_wheel(
    environment: Environment, tmp_path: Path
) -> None:
    alpha = b"alpha wheel"
    source = FakeSource("wheels/", {wheel_name("appmux-fixture-alpha", "1.0.0"): alpha})
    text = f"fastapi==0.115.0\n{hashed('appmux-fixture-alpha', '1.0.0', alpha)}\n"

    result = await resolve(text, environment, tmp_path, sources=[source])

    assert result.failure_reason() is None
    assert result.wheels == (
        ResolvedWheel(sha256_of(alpha), wheel_name("appmux-fixture-alpha", "1.0.0"), "wheels/"),
    )
    assert (result.from_environment, result.from_wheels, result.total_count) == (1, 1, 2)
    assert result.needs_overlay
    assert result.summary() == "1 of 2 from the environment, 1 from wheels"
    assert result.replaced == ()  # the environment had no appmux-fixture-alpha to replace


async def test_a_wheel_overwrites_a_differing_environment_version_for_this_release_only(
    environment: Environment, tmp_path: Path
) -> None:
    newer = b"appdirs 99.0.0 stand-in"
    source = FakeSource("src", {wheel_name("appdirs", "99.0.0"): newer})

    result = await resolve(
        hashed("appdirs", "99.0.0", newer) + "\n", environment, tmp_path, sources=[source]
    )

    assert [wheel.filename for wheel in result.wheels] == [wheel_name("appdirs", "99.0.0")]
    assert result.replaced == (
        "appdirs 99.0.0 from a wheel replaces the environment's appdirs 1.4.4 "
        "for this release only",
    )
    # the environment object is untouched: another release resolved against it still sees 1.4.4
    assert str(environment.version_of("appdirs")) == "1.4.4"


async def test_a_local_version_pin_forces_an_own_build_of_a_version_the_environment_has(
    environment: Environment, tmp_path: Path
) -> None:
    own = b"local-pkg 1.2.3+acme1"
    source = FakeSource("src", {wheel_name("local-pkg", "1.2.3+acme1"): own})

    result = await resolve(
        hashed("local-pkg", "1.2.3+acme1", own) + "\n", environment, tmp_path, sources=[source]
    )

    assert [wheel.filename for wheel in result.wheels] == [wheel_name("local-pkg", "1.2.3+acme1")]
    assert result.replaced == (
        "local-pkg 1.2.3+acme1 from a wheel replaces the environment's local-pkg 1.2.3 "
        "for this release only",
    )


async def test_a_wheel_for_the_version_the_environment_has_is_not_called_a_replacement(
    tmp_path: Path,
) -> None:
    # The environment has extra-pkg 1.0 but not what its "web" extra needs, so the pin with the
    # extra is not met and a wheel is taken; it replaces nothing, the version is the same.
    site = tmp_path / "site"
    site.mkdir()
    folder = site / "extra_pkg-1.0.dist-info"
    folder.mkdir()
    (folder / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: extra-pkg\nVersion: 1.0\n"
        'Provides-Extra: web\nRequires-Dist: web-dep>=1; extra == "web"\n',
        encoding="utf-8",
    )
    environment = read_environment(distributions(path=[str(site)]))
    own = b"extra-pkg 1.0 stand-in"
    source = FakeSource("src", {wheel_name("extra-pkg", "1.0"): own})

    result = await resolve(
        hashed("extra-pkg[web]", "1.0", own) + "\n", environment, tmp_path, sources=[source]
    )

    assert [wheel.filename for wheel in result.wheels] == [wheel_name("extra-pkg", "1.0")]
    assert result.replaced == ()


@pytest.mark.parametrize("specifier", [">=2.0", "~=2.0", "==2.*", "===2.0", "==2.0,!=2.0.1"])
async def test_a_hash_does_not_make_a_range_a_pin(
    specifier: str, environment: Environment, tmp_path: Path
) -> None:
    wheel = b"foo 2.0 stand-in"
    source = FakeSource("src", {wheel_name("foo", "2.0"): wheel})
    line = f"foo{specifier} --hash=sha256:{sha256_of(wheel)}\n"

    result = await resolve(line, environment, tmp_path, sources=[source])

    assert source.fetched == []
    assert result.wheels == ()
    assert len(result.unresolved) == 1
    assert "pinned with ==" in result.unresolved[0]


async def test_wheels_are_fetched_one_at_a_time_in_the_order_of_the_lock(
    environment: Environment, tmp_path: Path
) -> None:
    gamma, alpha, beta = b"gamma", b"alpha", b"beta"
    source = FakeSource(
        "src",
        {
            wheel_name("zeta-gamma", "1.0"): gamma,
            wheel_name("alpha", "1.0"): alpha,
            wheel_name("beta", "1.0"): beta,
        },
    )
    text = "\n".join(
        [
            hashed("zeta-gamma", "1.0", gamma),
            hashed("alpha", "1.0", alpha),
            hashed("beta", "1.0", beta),
        ]
    )

    result = await resolve(text, environment, tmp_path, sources=[source])

    assert source.fetched == [
        wheel_name("zeta-gamma", "1.0"),
        wheel_name("alpha", "1.0"),
        wheel_name("beta", "1.0"),
    ]
    assert [wheel.filename for wheel in result.wheels] == source.fetched  # the release's wheel list


async def test_a_store_hit_by_sha256_fetches_nothing(
    environment: Environment, tmp_path: Path
) -> None:
    alpha = b"alpha wheel"
    store, workspace = new_store()
    local = tmp_path / "alpha.whl"
    local.write_bytes(alpha)
    await store.add(sha256_of(alpha), local)  # put there by an earlier release
    workspace.calls.clear()
    source = FakeSource("src", {wheel_name("appmux-fixture-alpha", "1.0.0"): alpha})

    result = await resolve(
        hashed("appmux-fixture-alpha", "1.0.0", alpha) + "\n",
        environment,
        tmp_path,
        sources=[source],
        store=store,
        stored_filenames={sha256_of(alpha): wheel_name("appmux-fixture-alpha", "1.0.0")},
    )

    assert result.wheels == (
        ResolvedWheel(sha256_of(alpha), wheel_name("appmux-fixture-alpha", "1.0.0"), STORE_SOURCE),
    )
    assert source.log == []
    assert [op for op, _, _ in workspace.calls if op in {"read", "write"}] == []


async def test_two_releases_keep_their_own_wheel_lists_and_share_the_stored_file(
    environment: Environment, tmp_path: Path
) -> None:
    alpha, beta = b"alpha wheel", b"beta wheel"
    source = FakeSource(
        "src",
        {
            wheel_name("appmux-fixture-alpha", "1.0.0"): alpha,
            wheel_name("appmux-fixture-beta", "1.0.0"): beta,
        },
    )
    store, workspace = new_store()
    first = await resolve(
        hashed("appmux-fixture-alpha", "1.0.0", alpha) + "\n",
        environment,
        tmp_path,
        sources=[source],
        store=store,
    )
    second = await resolve(
        hashed("appmux-fixture-beta", "1.0.0", beta) + "\n",
        environment,
        tmp_path,
        sources=[source],
        store=store,
    )

    # a wheel in one app's source never lands in another app's list
    assert [wheel.sha256 for wheel in first.wheels] == [sha256_of(alpha)]
    assert [wheel.sha256 for wheel in second.wheels] == [sha256_of(beta)]
    assert sorted(path.rsplit("/", 1)[1] for path in workspace.files) == sorted(
        [sha256_of(alpha), sha256_of(beta)]
    )  # one stored file per distinct digest


# --- 3. no wheel has it: one list of everything unresolved --------------------------------------


async def test_every_requirement_that_cannot_be_resolved_is_listed_at_once_in_line_order(
    environment: Environment, tmp_path: Path
) -> None:
    lost = b"a wheel nobody has"
    text = "\n".join(
        [
            hashed("foo", "2.0", lost),  # line 1: hashed, in no source
            "bar>=1",  # line 2: unpinned and the environment lacks it
            "-r other.txt",  # line 3: names another file
            "uvicorn==0.31.0",  # line 4: pinned, but no hash
            "fastapi==0.115.0",  # line 5: fine
        ]
    )
    source = FakeSource("src", {})

    result = await resolve(text, environment, tmp_path, sources=[source])

    hint = "a wheel needs the requirement pinned with == and a --hash=sha256:..."
    assert result.unresolved == (
        f"no wheel for 'foo==2.0' (sha256 {sha256_of(lost)[:4]}…) in the wheel sources",
        f"no wheel for 'bar>=1' (the environment has no bar); {hint}",
        "no wheel for line 3 (option -r names another file or a source tree, which the "
        "environment cannot satisfy)",
        f"no wheel for 'uvicorn==0.31.0' (the environment has uvicorn 0.30.6); {hint}",
    )
    assert result.failure_reason() == "; ".join(result.unresolved)
    assert (result.from_environment, result.total_count) == (1, 5)
    assert result.wheels == ()


async def test_a_source_that_cannot_be_read_is_named_in_the_reason(
    environment: Environment, tmp_path: Path
) -> None:
    gone = FakeWorkspace(required_token=PRESSER_TOKEN)  # the wheel folder does not exist
    source = WorkspaceWheelSource(gone, folder=WHEEL_FOLDER, token=PRESSER_TOKEN)
    text = hashed("foo", "2.0", b"x") + "\n"

    result = await resolve(text, environment, tmp_path, sources=[source])

    assert f"'{WHEEL_FOLDER}' could not be read" in (result.failure_reason() or "")


async def test_the_failure_names_a_wheel_built_for_another_python(
    environment: Environment, tmp_path: Path
) -> None:
    data = b"cp312 only"
    source = FakeSource("src", {"foo-2.0-cp312-cp312-manylinux2014_x86_64.whl": data})

    result = await resolve(
        hashed("foo", "2.0", data) + "\n", environment, tmp_path, sources=[source]
    )

    assert "only for another Python or platform" in (result.failure_reason() or "")


async def test_a_hash_of_another_algorithm_is_not_a_wheel_hash(
    environment: Environment, tmp_path: Path
) -> None:
    text = f"foo==2.0 --hash=sha512:{'a' * 128}\n"
    result = await resolve(text, environment, tmp_path, sources=[FakeSource("src", {})])
    assert result.unresolved == (
        "no wheel for 'foo==2.0' (the environment has no foo); "
        "a wheel needs the requirement pinned with == and a --hash=sha256:...",
    )


async def test_a_direct_url_is_never_fetched(environment: Environment, tmp_path: Path) -> None:
    text = "foo @ https://example.com/foo-2.0-py3-none-any.whl\n"
    result = await resolve(text, environment, tmp_path, sources=[FakeSource("src", {})])
    assert result.unresolved == (
        "no wheel for 'foo' (a direct URL is not read from the environment)",
    )


# --- the modes ----------------------------------------------------------------------------------


async def test_index_mode_hands_the_unmet_requirements_to_the_index_and_ignores_wheels(
    environment: Environment, tmp_path: Path
) -> None:
    alpha = b"alpha wheel"
    source = FakeSource("src", {wheel_name("appmux-fixture-alpha", "1.0.0"): alpha})
    text = "\n".join(
        [
            "fastapi==0.115.0",
            hashed("appmux-fixture-alpha", "1.0.0", alpha),
            "requests[socks]>=2.31",
        ]
    )

    result = await resolve(text, environment, tmp_path, mode=PackageMode.INDEX, sources=[source])

    assert result.failure_reason() is None
    assert result.wheels == ()
    assert result.index_requirements == ("appmux-fixture-alpha==1.0.0", "requests[socks]>=2.31")
    assert (result.from_environment, result.from_wheels, result.from_index) == (1, 0, 2)
    assert result.summary() == "1 of 3 from the environment, 0 from wheels, 2 from the index"
    assert result.needs_overlay
    assert source.log == []


async def test_wheelhouse_plus_index_takes_wheels_first_and_the_index_for_the_rest(
    environment: Environment, tmp_path: Path
) -> None:
    alpha, lost = b"alpha wheel", b"a wheel nobody has"
    source = FakeSource("src", {wheel_name("appmux-fixture-alpha", "1.0.0"): alpha})
    text = "\n".join(
        [
            hashed("appmux-fixture-alpha", "1.0.0", alpha),
            hashed("foo", "2.0", lost),  # hashed, no wheel anywhere -> the index
            "requests>=2.31",  # not pinned -> the index
        ]
    )

    result = await resolve(
        text, environment, tmp_path, mode=PackageMode.WHEELHOUSE_INDEX, sources=[source]
    )

    assert result.failure_reason() is None
    assert [wheel.filename for wheel in result.wheels] == [
        wheel_name("appmux-fixture-alpha", "1.0.0")
    ]
    assert result.index_requirements == ("foo==2.0", "requests>=2.31")
    assert (result.from_wheels, result.from_index) == (1, 2)


async def test_wheelhouse_never_uses_an_index_and_says_what_to_add(
    environment: Environment, tmp_path: Path
) -> None:
    result = await resolve("requests>=2.31\n", environment, tmp_path)
    reason = result.failure_reason()
    assert result.index_requirements == ()
    assert reason is not None
    assert "requests>=2.31" in reason


async def test_unsupported_lines_fail_in_every_mode(
    environment: Environment, tmp_path: Path
) -> None:
    for mode in PackageMode:
        result = await resolve("-e ./local-package\n", environment, tmp_path, mode=mode)
        assert result.failure_reason() is not None, mode


# --- reading the lock ---------------------------------------------------------------------------


async def test_the_lock_is_read_like_pip_reads_it(environment: Environment, tmp_path: Path) -> None:
    alpha, beta = b"alpha wheel", b"beta wheel"
    source = FakeSource(
        "src",
        {
            wheel_name("appmux-fixture-alpha", "1.0.0"): alpha,
            wheel_name("appmux-fixture-beta", "1.0.0"): beta,
        },
    )
    text = (
        "# a comment\n"
        "fastapi==0.115.0  # inline comment\n"
        "appmux-fixture-alpha==1.0.0 \\\n"
        f"    --hash=sha256:{sha256_of(alpha)}\n"
        f"appmux-fixture-beta==1.0.0 --hash=sha256:{'0' * 64} \\\n"
        f"    --hash=sha256:{sha256_of(beta)}\n"
        'windows-only==1.0 ; sys_platform == "win32"\n'
    )

    result = await resolve(text, environment, tmp_path, sources=[source])

    assert result.failure_reason() is None
    assert [wheel.sha256 for wheel in result.wheels] == [sha256_of(alpha), sha256_of(beta)]
    # a requirement whose marker is false does not apply here and is not counted
    assert result.total_count == 3


async def test_options_are_noted_and_never_reach_the_build(
    environment: Environment, tmp_path: Path
) -> None:
    text = "--index-url https://index.example.com/simple\n--no-binary :all:\nfastapi==0.115.0\n"
    result = await resolve(text, environment, tmp_path)
    assert result.failure_reason() is None
    assert result.notes == ("ignored option: --index-url", "ignored option: --no-binary")
    assert result.total_count == 1


# --- the re-check at every start ----------------------------------------------------------------


def test_a_requirement_the_environment_still_meets_passes_the_recheck(
    environment: Environment,
) -> None:
    check = recheck_environment(
        "fastapi==0.115.0\nstarlette>=0.37\n",
        mode=PackageMode.WHEELHOUSE,
        environment=environment,
        wheel_sha256s=(),
    )
    assert check == Recheck(unmet=(), index_requirements=())
    assert check.failure_reason() is None


def test_a_requirement_the_environment_no_longer_meets_fails_the_release_before_it_starts(
    tmp_path: Path,
) -> None:
    redeployed = make_environment(tmp_path / "site", {"uvicorn": "0.31.0", "fastapi": "0.115.0"})
    check = recheck_environment(
        "fastapi==0.115.0\nuvicorn==0.30.6\n",
        mode=PackageMode.WHEELHOUSE,
        environment=redeployed,
        wheel_sha256s=(),
    )
    assert check.failure_reason() == (
        "the environment no longer provides 'uvicorn==0.30.6' (it has 0.31.0); "
        "add a wheel and Update"
    )


def test_the_recheck_lists_every_requirement_that_no_longer_holds(tmp_path: Path) -> None:
    redeployed = make_environment(tmp_path / "site", {"uvicorn": "0.31.0"})
    check = recheck_environment(
        "uvicorn==0.30.6\npolars>=1.0\n",
        mode=PackageMode.WHEELHOUSE,
        environment=redeployed,
        wheel_sha256s=(),
    )
    assert check.failure_reason() == (
        "the environment no longer provides 'uvicorn==0.30.6' (it has 0.31.0), "
        "'polars>=1.0' (it has no polars); add a wheel and Update"
    )


def test_a_requirement_a_wheel_of_the_release_backs_is_not_rechecked(tmp_path: Path) -> None:
    newer = b"appdirs 99.0.0 stand-in"
    old = make_environment(tmp_path / "site", {"appdirs": "1.4.4"})
    text = hashed("appdirs", "99.0.0", newer) + "\n"

    backed = recheck_environment(
        text, mode=PackageMode.WHEELHOUSE, environment=old, wheel_sha256s=[sha256_of(newer)]
    )
    unbacked = recheck_environment(
        text, mode=PackageMode.WHEELHOUSE, environment=old, wheel_sha256s=[]
    )

    assert backed.failure_reason() is None
    assert unbacked.failure_reason() is not None


def test_in_an_index_mode_a_drifted_requirement_goes_to_the_index_instead_of_failing(
    tmp_path: Path,
) -> None:
    redeployed = make_environment(tmp_path / "site", {"uvicorn": "0.31.0"})
    for mode in (PackageMode.INDEX, PackageMode.WHEELHOUSE_INDEX):
        check = recheck_environment(
            "uvicorn==0.30.6\n", mode=mode, environment=redeployed, wheel_sha256s=()
        )
        assert check.failure_reason() is None
        assert check.index_requirements == ("uvicorn==0.30.6",)


# --- the fixtures' own locks --------------------------------------------------------------------


def load_make_wheels(fixture: str) -> ModuleType:
    path = FIXTURES / fixture / "make_wheels.py"
    spec = importlib.util.spec_from_file_location(f"make_wheels_{fixture}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_workspace(fixture: str, tmp_path: Path) -> FakeWorkspace:
    """The wheels the fixture builds, in a workspace folder the presser may read."""
    built = tmp_path / f"built-{fixture}"
    built.mkdir()
    assert (
        load_make_wheels(fixture).main(["make_wheels.py", str(built)]) == 0
    )  # also checks the lock's digests
    workspace = FakeWorkspace(required_token=PRESSER_TOKEN)
    workspace.add_folder(WHEEL_FOLDER)
    for wheel in sorted(built.glob("*.whl")):
        workspace.add_file(f"{WHEEL_FOLDER}/{wheel.name}", wheel.read_bytes())
    return workspace


def fixture_lock(fixture: str) -> str:
    return (FIXTURES / fixture / "requirements.txt").read_text(encoding="utf-8")


async def test_wheels_only_resolves_alpha_and_beta_and_takes_fastapi_from_the_environment(
    environment: Environment, tmp_path: Path
) -> None:
    workspace = fixture_workspace("wheels_only", tmp_path)
    source = WorkspaceWheelSource(workspace, folder=WHEEL_FOLDER, token=PRESSER_TOKEN)

    result = await resolve(fixture_lock("wheels_only"), environment, tmp_path, sources=[source])

    assert result.failure_reason() is None
    assert [wheel.filename for wheel in result.wheels] == [
        "appmux_fixture_alpha-1.0.0-py3-none-any.whl",
        "appmux_fixture_beta-1.0.0-py3-none-any.whl",
    ]
    assert result.summary() == "2 of 4 from the environment, 2 from wheels"


async def test_wheel_reuse_needs_no_fetch_once_wheels_only_has_stored_the_wheel(
    environment: Environment, tmp_path: Path
) -> None:
    store, _ = new_store()
    first_workspace = fixture_workspace("wheels_only", tmp_path)
    first_source = WorkspaceWheelSource(first_workspace, folder=WHEEL_FOLDER, token=PRESSER_TOKEN)
    first = await resolve(
        fixture_lock("wheels_only"), environment, tmp_path, sources=[first_source], store=store
    )
    stored = {wheel.sha256: wheel.filename for wheel in first.wheels}

    # the second app's own wheel folder is gone: only the store can supply the wheel
    unreachable = FakeWorkspace(required_token=PRESSER_TOKEN)
    second_source = WorkspaceWheelSource(unreachable, folder=WHEEL_FOLDER, token=PRESSER_TOKEN)
    second = await resolve(
        fixture_lock("wheel_reuse"),
        environment,
        tmp_path,
        sources=[second_source],
        store=store,
        stored_filenames=stored,
    )

    assert second.failure_reason() is None
    assert [(wheel.filename, wheel.source) for wheel in second.wheels] == [
        ("appmux_fixture_alpha-1.0.0-py3-none-any.whl", STORE_SOURCE)
    ]
    assert unreachable.calls == []


async def test_newer_wheel_replaces_the_environments_appdirs(
    environment: Environment, tmp_path: Path
) -> None:
    workspace = fixture_workspace("newer_wheel", tmp_path)
    source = WorkspaceWheelSource(workspace, folder=WHEEL_FOLDER, token=PRESSER_TOKEN)

    result = await resolve(fixture_lock("newer_wheel"), environment, tmp_path, sources=[source])

    assert [wheel.filename for wheel in result.wheels] == ["appdirs-99.0.0-py3-none-any.whl"]
    assert result.replaced == (
        "appdirs 99.0.0 from a wheel replaces the environment's appdirs 1.4.4 "
        "for this release only",
    )


async def test_env_covered_fetches_no_wheel_even_though_a_wheel_folder_is_offered(
    environment: Environment, tmp_path: Path
) -> None:
    workspace = fixture_workspace("env_covered", tmp_path)
    source = WorkspaceWheelSource(workspace, folder=WHEEL_FOLDER, token=PRESSER_TOKEN)

    result = await resolve(fixture_lock("env_covered"), environment, tmp_path, sources=[source])

    assert result.failure_reason() is None
    assert result.wheels == ()
    assert not result.needs_overlay
    assert result.summary() == "5 of 5 from the environment, 0 from wheels"
    assert workspace.calls == []  # the folder was not listed, let alone read
