"""scripts/vendor-wheels.sh: which wheels it vendors and the refusals that guard them.

The script is a supply-chain guard (python/security rules): it reads every wheel's version,
URL, SHA-256 and upload time from ``uv.lock`` and downloads nothing it cannot check. Each
refusal is therefore proved here, offline:

* a wheel published in the last 24 hours, or one with no upload time, is refused before any
  download unless ``--allow-recent`` is given;
* a download whose SHA-256 differs from ``uv.lock`` is deleted and fails the run, and the
  previous outputs stay;
* a URL outside ``https://files.pythonhosted.org`` is refused before any request.

The script reads ``uv.lock`` and the wheel files from a fake repository in a temporary
directory; wheels are served by a throwaway HTTP server on 127.0.0.1 that the script accepts
only through ``APPMUX_VENDOR_ALLOW_LOOPBACK_HTTP_FOR_TESTS``. Nothing here reaches the network.
"""

from __future__ import annotations

import hashlib
import http.server
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

VENDOR_WHEELS = Path(__file__).resolve().parents[2] / "scripts" / "vendor-wheels.sh"
ENVIRONMENT_LIST = "specs/001-appmux-v1/spikes/evidence/spike-18-environment-packages.txt"
LOOPBACK_OVERRIDE = "APPMUX_VENDOR_ALLOW_LOOPBACK_HTTP_FOR_TESTS"

LONG_AGO = timedelta(days=30)

# The wheel the script must pick for gamma-binary among the five it is offered.
GAMMA_BINARY_CHOSEN = "gamma_binary-1.0-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
GAMMA_BINARY_OTHER_WHEELS = [
    "gamma_binary-1.0-cp311-cp311-macosx_11_0_arm64.whl",
    "gamma_binary-1.0-cp311-cp311-musllinux_1_2_x86_64.whl",
    "gamma_binary-1.0-cp311-cp311-manylinux2014_aarch64.manylinux_2_17_aarch64.whl",
    "gamma_binary-1.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
]
DELTA_WHEEL = "delta-2.0-py3-none-any.whl"
# The wheel file name is the last part of the URL path, so these end in a valid one.
NOT_AN_HTTP_REPLY_PATH = f"/not-an-http-reply/{DELTA_WHEEL}"
MISSING_PATH = f"/missing/{DELTA_WHEEL}"
GAMMA_WHEEL = "gamma-1.0-py3-none-any.whl"
ALPHA_WHEEL = "alpha-5.0-py3-none-any.whl"


class WheelServer:
    """Serves bytes by path on 127.0.0.1 and records every path requested."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []
        files, requests = self.files, self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append(self.path)
                if self.path == NOT_AN_HTTP_REPLY_PATH:
                    self.wfile.write(b"this is not an http status line\r\n\r\n")
                elif self.path in files:
                    body = files[self.path]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def log_message(self, message_format: str, *args: object) -> None:
                """Keep the test output quiet."""

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}{path}"

    def serve(self, filename: str, content: bytes | None = None) -> tuple[str, bytes]:
        """Serve a wheel under /packages/ and return its URL and bytes."""
        path = f"/packages/{filename}"
        body = content if content is not None else f"bytes of {filename}\n".encode()
        self.files[path] = body
        return self.url(path), body


@pytest.fixture
def server() -> Iterator[WheelServer]:
    wheel_server = WheelServer()
    wheel_server.start()
    yield wheel_server
    wheel_server.stop()


@dataclass
class FakeRepo:
    """A repository root with a uv.lock and an environment list, and its build folder."""

    root: Path
    build: Path
    environment_packages: list[str] = field(default_factory=lambda: ["alpha==5.0"])

    def write_environment_list(self) -> None:
        path = self.root / ENVIRONMENT_LIST
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fake list\n" + "\n".join(self.environment_packages) + "\n")

    def write_lock(self, text: str) -> None:
        (self.root / "uv.lock").write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> FakeRepo:
    fake = FakeRepo(root=tmp_path / "repo", build=tmp_path / "repo" / "build")
    fake.root.mkdir()
    fake.write_environment_list()
    return fake


def timestamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def wheel_toml(url: str, content: bytes, uploaded_at: datetime | None) -> str:
    upload_time = f', upload-time = "{timestamp(uploaded_at)}"' if uploaded_at else ""
    digest = hashlib.sha256(content).hexdigest()
    return f'    {{ url = "{url}", hash = "sha256:{digest}"{upload_time} }},\n'


def package_toml(
    name: str,
    version: str,
    *,
    wheels: list[str] | None = None,
    dependencies: list[str] | None = None,
    optional_dependencies: dict[str, list[str]] | None = None,
    dev_dependencies: list[str] | None = None,
    editable: bool = False,
) -> str:
    source = '{ editable = "." }' if editable else '{ registry = "https://pypi.org/simple" }'
    text = f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {source}\n'
    if dependencies:
        text += "dependencies = [\n" + "".join(f"    {item},\n" for item in dependencies) + "]\n"
    if wheels:
        text += "wheels = [\n" + "".join(wheels) + "]\n"
    if optional_dependencies:
        text += "\n[package.optional-dependencies]\n"
        for extra, items in optional_dependencies.items():
            text += f"{extra} = [\n" + "".join(f"    {item},\n" for item in items) + "]\n"
    if dev_dependencies:
        text += "\n[package.dev-dependencies]\ndev = [\n"
        text += "".join(f"    {item},\n" for item in dev_dependencies) + "]\n"
    return text + "\n"


@dataclass(frozen=True)
class Scenario:
    """The fake lock of the happy path and what the script must make of it."""

    chosen_wheels: dict[str, bytes]
    chosen_paths: list[str]
    requirements_text: str


def write_standard_scenario(
    repo: FakeRepo,
    server: WheelServer,
    *,
    delta_age: timedelta | None = LONG_AGO,
    delta_url: str | None = None,
    delta_content_in_lock: bytes | None = None,
) -> Scenario:
    """Write a uv.lock whose runtime closure needs delta, gamma and gamma-binary.

    appmux depends on alpha (already in the App environment), on windows-only-helper (a
    win32 marker), and on gamma with its ``binary`` extra. gamma needs delta; its other
    extra and the dev group name packages that must not be vendored.
    """
    now = datetime.now(UTC)
    old = now - LONG_AGO
    # None leaves delta's lock entry without an upload time.
    delta_time = None if delta_age is None else now - delta_age

    delta_served_url, delta_bytes = server.serve(DELTA_WHEEL)
    delta_locked_bytes = delta_content_in_lock if delta_content_in_lock is not None else delta_bytes
    gamma_url, gamma_bytes = server.serve(GAMMA_WHEEL)
    alpha_url, alpha_bytes = server.serve(ALPHA_WHEEL)
    chosen_url, chosen_bytes = server.serve(GAMMA_BINARY_CHOSEN)
    other_binary_wheels = []
    for filename in GAMMA_BINARY_OTHER_WHEELS:
        other_url, other_bytes = server.serve(filename)
        other_binary_wheels.append(wheel_toml(other_url, other_bytes, old))
    # Listed in the middle on purpose: the pick must not depend on the order.
    binary_wheels = [
        *other_binary_wheels[:2],
        wheel_toml(chosen_url, chosen_bytes, old),
        *other_binary_wheels[2:],
    ]

    repo.write_lock(
        'version = 1\nrequires-python = ">=3.11"\n\n'
        + package_toml(
            "appmux",
            "0.0.0",
            editable=True,
            dependencies=[
                '{ name = "alpha" }',
                '{ name = "windows-only-helper", marker = "sys_platform == \'win32\'" }',
                '{ name = "gamma", extra = ["binary"] }',
            ],
            dev_dependencies=['{ name = "dev-tool" }'],
        )
        + package_toml("alpha", "5.0", wheels=[wheel_toml(alpha_url, alpha_bytes, old)])
        + package_toml("windows-only-helper", "1.0")
        + package_toml("dev-tool", "1.0")
        + package_toml("unwanted-extra", "1.0")
        + package_toml(
            "gamma",
            "1.0",
            wheels=[wheel_toml(gamma_url, gamma_bytes, old)],
            dependencies=['{ name = "delta" }'],
            optional_dependencies={
                "binary": ['{ name = "gamma-binary" }'],
                "other": ['{ name = "unwanted-extra" }'],
            },
        )
        + package_toml("gamma-binary", "1.0", wheels=binary_wheels)
        + package_toml(
            "delta",
            "2.0",
            wheels=[wheel_toml(delta_url or delta_served_url, delta_locked_bytes, delta_time)],
        )
    )
    chosen = {
        DELTA_WHEEL: delta_bytes,
        GAMMA_WHEEL: gamma_bytes,
        GAMMA_BINARY_CHOSEN: chosen_bytes,
    }
    requirements = "--no-index\n--find-links ./wheels\n"
    for requirement, filename in (
        ("delta==2.0", DELTA_WHEEL),
        ("gamma==1.0", GAMMA_WHEEL),
        ("gamma-binary==1.0", GAMMA_BINARY_CHOSEN),
    ):
        requirements += f"{requirement} \\\n    --hash=sha256:{sha256(chosen[filename])}\n"
    return Scenario(
        chosen_wheels=chosen,
        chosen_paths=[f"/packages/{filename}" for filename in chosen],
        requirements_text=requirements,
    )


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def vendor(
    repo: FakeRepo, *args: str, loopback_override: bool = True
) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(repo.root.parent / "home"),
        "APPMUX_REPO_ROOT": str(repo.root),
        "APPMUX_BUILD_DIR": str(repo.build),
        "APPMUX_PYTHON": sys.executable,
    }
    if loopback_override:
        environment[LOOPBACK_OVERRIDE] = "1"
    return subprocess.run(
        ["bash", str(VENDOR_WHEELS), *args],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def wheel_files(repo: FakeRepo) -> dict[str, bytes]:
    folder = repo.build / "wheels"
    return {path.name: path.read_bytes() for path in folder.iterdir()} if folder.is_dir() else {}


def assert_nothing_was_built(repo: FakeRepo) -> None:
    assert not (repo.build / "wheels").exists()
    assert not (repo.build / "requirements.txt").exists()
    assert list(repo.build.glob(".wheels-*")) == [], "a staging folder was left behind"


def test_vendors_the_runtime_closure_minus_the_app_environment(
    repo: FakeRepo, server: WheelServer
) -> None:
    scenario = write_standard_scenario(repo, server)

    result = vendor(repo)

    assert result.returncode == 0, result.stderr
    # delta (needed by gamma) and gamma-binary (the `binary` extra) are in; alpha is in the
    # App environment, windows-only-helper has a win32 marker, dev-tool is a dev group and
    # unwanted-extra belongs to an extra nobody asked for.
    assert wheel_files(repo) == scenario.chosen_wheels
    assert (repo.build / "requirements.txt").read_text() == scenario.requirements_text
    assert "in the App environment, not vendored: alpha" in result.stdout
    assert sorted(server.requests) == sorted(scenario.chosen_paths)


def test_only_the_manylinux2014_cp311_wheel_is_chosen_among_the_locked_ones(
    repo: FakeRepo, server: WheelServer
) -> None:
    write_standard_scenario(repo, server)

    result = vendor(repo)

    assert result.returncode == 0, result.stderr
    binary_wheels = [name for name in wheel_files(repo) if name.startswith("gamma_binary")]
    assert binary_wheels == [GAMMA_BINARY_CHOSEN]


@pytest.mark.parametrize(
    "available_wheel",
    [
        pytest.param("lonely-1.0-cp311-cp311-macosx_11_0_arm64.whl", id="other-platform"),
        pytest.param("lonely-1.0-cp312-cp312-manylinux2014_x86_64.whl", id="other-python"),
        pytest.param("lonely-1.0-cp311-cp311-musllinux_1_2_x86_64.whl", id="musl"),
    ],
)
def test_a_package_with_no_wheel_for_the_target_is_refused_not_built_from_source(
    repo: FakeRepo, server: WheelServer, available_wheel: str
) -> None:
    url, content = server.serve(available_wheel)
    repo.write_lock(
        "version = 1\n\n"
        + package_toml("appmux", "0.0.0", editable=True, dependencies=['{ name = "lonely" }'])
        + package_toml(
            "lonely", "1.0", wheels=[wheel_toml(url, content, datetime.now(UTC) - LONG_AGO)]
        )
    )

    result = vendor(repo)

    assert result.returncode != 0
    assert "no wheel for 'lonely' 1.0" in result.stderr
    assert server.requests == []
    assert_nothing_was_built(repo)


@pytest.mark.parametrize(
    "age",
    [
        pytest.param(timedelta(minutes=5), id="five-minutes-old"),
        pytest.param(timedelta(hours=23), id="twenty-three-hours-old"),
    ],
)
def test_refuses_a_wheel_published_in_the_last_24_hours_before_any_download(
    repo: FakeRepo, server: WheelServer, age: timedelta
) -> None:
    write_standard_scenario(repo, server, delta_age=age)

    result = vendor(repo)

    assert result.returncode != 0
    assert "last 24 hours" in result.stderr
    assert "--allow-recent" in result.stderr
    assert DELTA_WHEEL in result.stderr
    assert server.requests == [], "the age check must run before the first download"
    assert_nothing_was_built(repo)


def test_accepts_a_wheel_published_more_than_24_hours_ago(
    repo: FakeRepo, server: WheelServer
) -> None:
    scenario = write_standard_scenario(repo, server, delta_age=timedelta(hours=25))

    result = vendor(repo)

    assert result.returncode == 0, result.stderr
    assert wheel_files(repo) == scenario.chosen_wheels


def test_allow_recent_accepts_a_wheel_published_minutes_ago(
    repo: FakeRepo, server: WheelServer
) -> None:
    scenario = write_standard_scenario(repo, server, delta_age=timedelta(minutes=5))

    result = vendor(repo, "--allow-recent")

    assert result.returncode == 0, result.stderr
    assert wheel_files(repo) == scenario.chosen_wheels


def test_refuses_a_wheel_whose_lock_entry_has_no_upload_time(
    repo: FakeRepo, server: WheelServer
) -> None:
    write_standard_scenario(repo, server, delta_age=None)

    result = vendor(repo)

    assert result.returncode != 0
    assert "no upload time" in result.stderr
    assert DELTA_WHEEL in result.stderr
    assert server.requests == []
    assert_nothing_was_built(repo)


def test_a_download_that_differs_from_the_lock_hash_fails_and_keeps_the_previous_outputs(
    repo: FakeRepo, server: WheelServer
) -> None:
    write_standard_scenario(repo, server, delta_content_in_lock=b"the bytes uv.lock vouches for\n")
    previous_wheel = repo.build / "wheels" / "previous-1.0-py3-none-any.whl"
    previous_wheel.parent.mkdir(parents=True)
    previous_wheel.write_text("previous wheel\n")
    previous_requirements = repo.build / "requirements.txt"
    previous_requirements.write_text("previous requirements\n")

    result = vendor(repo)

    assert result.returncode != 0
    assert "does not match the SHA-256 in uv.lock" in result.stderr
    assert DELTA_WHEEL in result.stderr
    assert wheel_files(repo) == {previous_wheel.name: b"previous wheel\n"}
    assert previous_requirements.read_text() == "previous requirements\n"
    assert list(repo.build.glob(".wheels-*")) == [], "the staging folder, with the bad download"
    assert not list(repo.build.rglob("*.partial"))


@pytest.mark.parametrize(
    "wheel_url",
    [
        pytest.param(f"https://evil.example.invalid/packages/{DELTA_WHEEL}", id="other-host"),
        pytest.param(
            f"https://files.pythonhosted.org.evil.example.invalid/packages/{DELTA_WHEEL}",
            id="allowed-host-as-a-prefix",
        ),
        pytest.param(
            f"https://files.pythonhosted.org@evil.example.invalid/packages/{DELTA_WHEEL}",
            id="allowed-host-as-user-info",
        ),
        pytest.param(
            f"http://files.pythonhosted.org/packages/{DELTA_WHEEL}", id="allowed-host-over-http"
        ),
        pytest.param(f"file:///tmp/{DELTA_WHEEL}", id="file-scheme"),
    ],
)
def test_refuses_a_wheel_url_outside_https_files_pythonhosted_org(
    repo: FakeRepo, server: WheelServer, wheel_url: str
) -> None:
    write_standard_scenario(repo, server, delta_url=wheel_url)

    result = vendor(repo)

    assert result.returncode != 0
    assert "not allowed" in result.stderr
    assert DELTA_WHEEL in result.stderr
    assert server.requests == []
    assert_nothing_was_built(repo)


def test_a_loopback_url_is_refused_unless_the_test_override_is_set(
    repo: FakeRepo, server: WheelServer
) -> None:
    write_standard_scenario(repo, server)

    result = vendor(repo, loopback_override=False)

    assert result.returncode != 0
    assert "not allowed" in result.stderr
    assert server.requests == [], "the default build may reach files.pythonhosted.org only"
    assert_nothing_was_built(repo)


@pytest.mark.parametrize(
    "wheel_path",
    [
        pytest.param(NOT_AN_HTTP_REPLY_PATH, id="reply-is-not-http"),
        pytest.param(MISSING_PATH, id="http-404"),
    ],
)
def test_a_failed_download_says_what_to_do_and_leaves_nothing_behind(
    repo: FakeRepo, server: WheelServer, wheel_path: str
) -> None:
    write_standard_scenario(repo, server, delta_url=server.url(wheel_path))

    result = vendor(repo)

    assert result.returncode != 0
    assert "download failed" in result.stderr
    assert "run again" in result.stderr
    assert "Traceback" not in result.stderr
    assert_nothing_was_built(repo)


def test_a_missing_environment_list_says_what_to_do_instead_of_a_traceback(
    repo: FakeRepo, server: WheelServer
) -> None:
    write_standard_scenario(repo, server)
    (repo.root / ENVIRONMENT_LIST).unlink()

    result = vendor(repo)

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "spike-18-environment-packages.txt" in result.stderr
    assert "is missing" in result.stderr
    assert "git checkout" in result.stderr
    assert server.requests == []
    assert_nothing_was_built(repo)
