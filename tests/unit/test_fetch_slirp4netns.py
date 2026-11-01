"""scripts/fetch-slirp4netns.sh refuses a download whose SHA-256 differs from the pinned one.

The script's three URLs can be overridden with local ``file://`` URLs (a test seam; the
pinned SHA-256 values cannot be overridden). The refusal tests run the real script. The
layout test runs a copy whose pinned values are replaced by the hashes of small fixture
files, so nothing here touches the network (one test talks to a server on 127.0.0.1) or
runs a downloaded program.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import os
import re
import stat
import subprocess
import tarfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "fetch-slirp4netns.sh"

FIXTURE_BINARY = b"\x7fELF fixture, not a program\n"
FIXTURE_COPYING = b"fixture GNU GENERAL PUBLIC LICENSE Version 2\n"
FIXTURE_PARSON_LICENSE = b"fixture MIT License, Krzysztof Gabis\n"
FIXTURE_LIBSLIRP_LICENSE = b"fixture BSD-3-Clause, Danny Gasparovski\n"
FIXTURE_LIBSLIRP_COPYRIGHT = b"fixture Slirp was written by Danny Gasparovski\n"
FIXTURE_LGPL_TEXT = b"fixture GNU LESSER GENERAL PUBLIC LICENSE Version 2.1\n"

HASH_CONSTANTS = (
    "SLIRP4NETNS_BINARY_SHA256",
    "SLIRP4NETNS_SOURCE_SHA256",
    "LIBSLIRP_SOURCE_SHA256",
    "LGPL21_TEXT_SHA256",
)
URL_CONSTANTS = (
    "SLIRP4NETNS_BINARY_URL",
    "SLIRP4NETNS_SOURCE_URL",
    "LIBSLIRP_SOURCE_URL",
    "LGPL21_TEXT_URL",
)
OUTPUT_MARKER = ".appmux-slirp4netns-output"
CONSTANT_LINE = re.compile(r'^readonly (?P<name>[A-Z0-9_]+)="(?P<value>[^"]*)"$')


@dataclass(frozen=True)
class Fixtures:
    binary: Path
    slirp_source: Path
    libslirp_source: Path
    lgpl_text: Path
    output: Path

    def environment(self) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items() if key != "BASH_ENV"}
        environment.update(
            APPMUX_SLIRP4NETNS_BINARY_URL=self.binary.as_uri(),
            APPMUX_SLIRP4NETNS_SOURCE_URL=self.slirp_source.as_uri(),
            APPMUX_LIBSLIRP_SOURCE_URL=self.libslirp_source.as_uri(),
            APPMUX_LGPL21_TEXT_URL=self.lgpl_text.as_uri(),
            APPMUX_SLIRP4NETNS_OUTPUT_DIR=str(self.output),
        )
        return environment


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_archive(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


@pytest.fixture
def fixtures(tmp_path: Path) -> Fixtures:
    download_folder = tmp_path / "upstream"
    download_folder.mkdir()
    binary = download_folder / "slirp4netns-x86_64"
    binary.write_bytes(FIXTURE_BINARY)
    slirp_source = download_folder / "v1.3.6.tar.gz"
    write_archive(
        slirp_source,
        {
            "slirp4netns-1.3.6/COPYING": FIXTURE_COPYING,
            "slirp4netns-1.3.6/vendor/parson/LICENSE": FIXTURE_PARSON_LICENSE,
        },
    )
    libslirp_source = download_folder / "v4.9.5.tar.gz"
    write_archive(
        libslirp_source,
        {
            "libslirp-4.9.5/LICENSE": FIXTURE_LIBSLIRP_LICENSE,
            "libslirp-4.9.5/COPYRIGHT": FIXTURE_LIBSLIRP_COPYRIGHT,
        },
    )
    lgpl_text = download_folder / "lgpl-2.1.txt"
    lgpl_text.write_bytes(FIXTURE_LGPL_TEXT)
    return Fixtures(
        binary, slirp_source, libslirp_source, lgpl_text, tmp_path / "work" / "slirp4netns"
    )


def script_pinned_to_fixtures(fixtures: Fixtures, destination: Path) -> Path:
    """Copy the script with each pinned SHA-256 replaced by the hash of the fixture file."""
    text = SCRIPT.read_text(encoding="utf-8")
    replacements = {
        "SLIRP4NETNS_BINARY_SHA256": sha256_of(fixtures.binary),
        "SLIRP4NETNS_SOURCE_SHA256": sha256_of(fixtures.slirp_source),
        "LIBSLIRP_SOURCE_SHA256": sha256_of(fixtures.libslirp_source),
        "LGPL21_TEXT_SHA256": sha256_of(fixtures.lgpl_text),
    }
    for name, digest in replacements.items():
        text, count = re.subn(
            rf'^readonly {name}="[0-9a-f]{{64}}"$', f'readonly {name}="{digest}"', text, flags=re.M
        )
        assert count == 1, f'{name} must be one `readonly {name}="<64 hex>"` line in the script'
    patched = destination / "fetch-slirp4netns.sh"
    patched.write_text(text, encoding="utf-8")
    return patched


def run(
    script: Path, fixtures: Fixtures, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        env=fixtures.environment() if environment is None else environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def leftovers_beside(output: Path) -> list[str]:
    parent = output.parent
    return sorted(p.name for p in parent.iterdir() if p != output) if parent.exists() else []


def script_code_lines(text: str) -> list[str]:
    """The script's lines that are neither blank nor comments, in order."""
    return [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


def pinned_constants(text: str) -> dict[str, str]:
    """The `readonly NAME="value"` lines that open the script, right after `set -euo pipefail`."""
    code = script_code_lines(text)
    start = code.index("set -euo pipefail") + 1
    constants: dict[str, str] = {}
    for line in code[start:]:
        match = CONSTANT_LINE.match(line)
        if match is None:
            break
        constants[match["name"]] = match["value"]
    return constants


def test_script_pins_its_constants_at_the_top() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    code = script_code_lines(text)
    assert code[0] == "set -euo pipefail", "strict mode is the first statement"

    top = pinned_constants(text)
    assert set(top) == {
        "SLIRP4NETNS_RELEASE",
        "LIBSLIRP_RELEASE",
        "SOURCE_OFFER_CONTACT_URL",
        *HASH_CONSTANTS,
        *URL_CONSTANTS,
    }, "the pinned block must hold exactly the release, URL and SHA-256 constants"
    readonly_lines = [line for line in code if line.startswith("readonly ")]
    assert len(readonly_lines) == len(top), "no pinned constant may sit below the opening block"

    assert top["SLIRP4NETNS_RELEASE"] == "v1.3.6"
    assert top["LIBSLIRP_RELEASE"] == "v4.9.5"
    for name in HASH_CONSTANTS:
        assert re.fullmatch(r"[0-9a-f]{64}", top[name]), name
    assert top["SLIRP4NETNS_BINARY_URL"] == (
        "https://github.com/rootless-containers/slirp4netns/releases/download/"
        f"{top['SLIRP4NETNS_RELEASE']}/slirp4netns-x86_64"
    )
    assert top["SLIRP4NETNS_SOURCE_URL"] == (
        "https://github.com/rootless-containers/slirp4netns/archive/refs/tags/"
        f"{top['SLIRP4NETNS_RELEASE']}.tar.gz"
    )
    assert top["LIBSLIRP_SOURCE_URL"] == (
        f"https://github.com/qemu/libslirp/archive/refs/tags/{top['LIBSLIRP_RELEASE']}.tar.gz"
    )
    assert top["LGPL21_TEXT_URL"] == "https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt"
    assert top["SOURCE_OFFER_CONTACT_URL"].startswith("https://"), "the offer names a place to ask"
    assert os.access(SCRIPT, os.X_OK), "scripts/fetch-slirp4netns.sh must be executable"


def test_refuses_a_binary_whose_hash_differs_from_the_pinned_one(fixtures: Fixtures) -> None:
    result = run(SCRIPT, fixtures)

    assert result.returncode != 0
    assert "SHA-256" in result.stderr
    assert "slirp4netns-x86_64" in result.stderr
    assert not fixtures.output.exists()
    assert leftovers_beside(fixtures.output) == [], "the rejected download must be deleted"


def test_refuses_a_source_archive_whose_hash_differs_and_writes_nothing(
    fixtures: Fixtures, tmp_path: Path
) -> None:
    script = script_pinned_to_fixtures(fixtures, tmp_path)
    write_archive(fixtures.slirp_source, {"slirp4netns-1.3.6/COPYING": b"tampered after pinning\n"})

    result = run(script, fixtures)

    assert result.returncode != 0
    assert "SHA-256" in result.stderr
    assert "v1.3.6.tar.gz" in result.stderr
    assert not fixtures.output.exists(), "a binary must not be left behind without its source"
    assert leftovers_beside(fixtures.output) == []


def test_refuses_a_libslirp_archive_whose_hash_differs_and_writes_nothing(
    fixtures: Fixtures, tmp_path: Path
) -> None:
    script = script_pinned_to_fixtures(fixtures, tmp_path)
    write_archive(fixtures.libslirp_source, {"libslirp-4.9.5/LICENSE": b"tampered after pinning\n"})

    result = run(script, fixtures)

    assert result.returncode != 0
    assert "SHA-256" in result.stderr
    assert "libslirp-4.9.5.tar.gz" in result.stderr
    assert "download deleted" in result.stderr
    assert not fixtures.output.exists(), (
        "a binary must not be left behind without its libslirp source"
    )
    assert leftovers_beside(fixtures.output) == []


def test_refuses_an_lgpl_text_whose_hash_differs_and_writes_nothing(
    fixtures: Fixtures, tmp_path: Path
) -> None:
    script = script_pinned_to_fixtures(fixtures, tmp_path)
    fixtures.lgpl_text.write_bytes(b"tampered after pinning\n")

    result = run(script, fixtures)

    assert result.returncode != 0
    assert "SHA-256" in result.stderr
    assert "lgpl-2.1.txt" in result.stderr
    assert not fixtures.output.exists(), "a binary must not be left behind without the LGPL text"
    assert leftovers_beside(fixtures.output) == []


def test_a_failed_run_keeps_the_previous_output(fixtures: Fixtures) -> None:
    fixtures.output.mkdir(parents=True)
    (fixtures.output / OUTPUT_MARKER).write_text("written by an earlier run\n", encoding="utf-8")
    sentinel = fixtures.output / "previous-run.txt"
    sentinel.write_text("kept", encoding="utf-8")

    result = run(SCRIPT, fixtures)

    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "kept"


def test_writes_the_layout_the_assembler_expects(fixtures: Fixtures, tmp_path: Path) -> None:
    script = script_pinned_to_fixtures(fixtures, tmp_path)

    result = run(script, fixtures)

    assert result.returncode == 0, result.stderr
    binary = fixtures.output / "bin" / "slirp4netns"
    assert binary.read_bytes() == FIXTURE_BINARY, "the binary must be stored unmodified"
    assert binary.stat().st_mode & stat.S_IXUSR

    licences = fixtures.output / "licenses" / "slirp4netns"
    assert (licences / "COPYING").read_bytes() == FIXTURE_COPYING
    assert (licences / "LICENSE-parson").read_bytes() == FIXTURE_PARSON_LICENSE
    assert (licences / "LICENSE-libslirp").read_bytes() == FIXTURE_LIBSLIRP_LICENSE
    assert (licences / "COPYRIGHT-libslirp").read_bytes() == FIXTURE_LIBSLIRP_COPYRIGHT
    assert (licences / "LICENSE-LGPL-2.1").read_bytes() == FIXTURE_LGPL_TEXT

    slirp_archives = sorted(p.name for p in licences.glob("slirp4netns-*.tar.gz"))
    assert slirp_archives == ["slirp4netns-1.3.6.tar.gz"]
    assert (licences / slirp_archives[0]).read_bytes() == fixtures.slirp_source.read_bytes()
    libslirp_archives = sorted(p.name for p in licences.glob("libslirp-*.tar.gz"))
    assert libslirp_archives == ["libslirp-4.9.5.tar.gz"]
    assert (licences / libslirp_archives[0]).read_bytes() == fixtures.libslirp_source.read_bytes()

    constants = pinned_constants(SCRIPT.read_text(encoding="utf-8"))
    source_note = (licences / "SOURCE.txt").read_text(encoding="utf-8")
    flat_note = " ".join(source_note.split())
    assert "v1.3.6" in source_note
    for name in URL_CONSTANTS:
        assert constants[name] in source_note, f"SOURCE.txt must name {name}"
    assert sha256_of(fixtures.binary) in source_note
    assert sha256_of(fixtures.slirp_source) in source_note
    assert sha256_of(fixtures.libslirp_source) in source_note, (
        "SOURCE.txt must carry the libslirp SHA-256"
    )
    assert constants["LGPL21_TEXT_URL"] in source_note
    assert sha256_of(fixtures.lgpl_text) in source_note, "SOURCE.txt must carry the LGPL SHA-256"
    assert "unmodified upstream build" in source_note
    assert (
        "The source of this release is the archive beside this file, slirp4netns-1.3.6.tar.gz."
        in flat_note
    )
    assert (fixtures.output / OUTPUT_MARKER).is_file(), (
        "the folder must carry the marker of its creator"
    )

    assert leftovers_beside(fixtures.output) == [], "staging files must not be left behind"


def test_fails_when_the_source_archive_has_no_copying(fixtures: Fixtures, tmp_path: Path) -> None:
    write_archive(fixtures.slirp_source, {"slirp4netns-1.3.6/README.md": b"no licence here\n"})
    script = script_pinned_to_fixtures(fixtures, tmp_path)

    result = run(script, fixtures)

    assert result.returncode != 0
    assert "COPYING" in result.stderr
    assert not fixtures.output.exists()


def test_source_note_states_what_the_folder_holds_and_what_it_does_not(
    fixtures: Fixtures, tmp_path: Path
) -> None:
    script = script_pinned_to_fixtures(fixtures, tmp_path)

    result = run(script, fixtures)

    assert result.returncode == 0, result.stderr
    note = (fixtures.output / "licenses" / "slirp4netns" / "SOURCE.txt").read_text(encoding="utf-8")
    flat = " ".join(note.split())

    # What the folder holds: every file the script writes next to SOURCE.txt is named.
    for name in (
        "COPYING",
        "LICENSE-parson",
        "LICENSE-libslirp",
        "COPYRIGHT-libslirp",
        "LICENSE-LGPL-2.1",
        "slirp4netns-1.3.6.tar.gz",
        "libslirp-4.9.5.tar.gz",
    ):
        assert name in note, f"SOURCE.txt must list {name}"

    # Libraries in the static binary, with their licences and the glibc version.
    assert "glibc 2.35" in flat
    assert "libseccomp 2.5.3" in flat
    assert "LGPL-2.1" in note
    assert "glib" in note
    assert "parson" in note and "MIT" in note
    assert "BSD-3-Clause" in note

    # libcap is not linked, and the evidence is on the page.
    assert "libcap is NOT linked" in flat
    assert "slirp4netns_LDADD" in note
    assert "LIBCAP_LIBS" in note

    # The archives hold the source of slirp4netns and libslirp only.
    assert "do NOT contain the source of glibc, glib or libseccomp" in flat
    for package in ("glibc", "glib2.0", "libseccomp"):
        assert f"`{package}`" in note, f"SOURCE.txt must name the Ubuntu source package {package}"
    assert "jammy" in note

    # The LGPL libraries' source is offered in writing (LGPL-2.1 section 6c), not shipped.
    assert "written offer" in flat.lower()
    assert "three years" in flat
    assert "LGPL-2.1 section 6" in flat
    contact = pinned_constants(SCRIPT.read_text(encoding="utf-8"))["SOURCE_OFFER_CONTACT_URL"]
    assert contact in note
    assert "snapshot jammy-20260509" in flat
    assert "open owner decision" not in flat
    assert "makes no offer" not in flat
    assert "NEEDS-OWNER-DECISION" not in note


def test_final_notice_reminds_the_operator_that_the_offer_must_stay_reachable(
    fixtures: Fixtures, tmp_path: Path
) -> None:
    script = script_pinned_to_fixtures(fixtures, tmp_path)

    result = run(script, fixtures)

    assert result.returncode == 0, result.stderr
    assert "NEEDS-OWNER-DECISION" not in result.stderr
    assert "written offer" in result.stderr
    assert "three years" in result.stderr
    contact = pinned_constants(SCRIPT.read_text(encoding="utf-8"))["SOURCE_OFFER_CONTACT_URL"]
    assert contact in result.stderr


def test_a_stray_output_override_over_an_unrelated_folder_is_refused(
    fixtures: Fixtures, tmp_path: Path
) -> None:
    script = script_pinned_to_fixtures(fixtures, tmp_path)
    fixtures.output.mkdir(parents=True)
    precious = fixtures.output / "precious.txt"
    precious.write_text("not made by the script", encoding="utf-8")

    result = run(script, fixtures)

    assert result.returncode != 0
    assert "APPMUX_SLIRP4NETNS_OUTPUT_DIR" in result.stderr
    assert precious.read_text(encoding="utf-8") == "not made by the script"
    assert sorted(p.name for p in fixtures.output.iterdir()) == ["precious.txt"]
    assert leftovers_beside(fixtures.output) == []


def test_an_output_folder_that_cannot_be_listed_is_refused_not_treated_as_empty(
    fixtures: Fixtures, tmp_path: Path
) -> None:
    script = script_pinned_to_fixtures(fixtures, tmp_path)
    fixtures.output.mkdir(parents=True)
    precious = fixtures.output / "precious.txt"
    precious.write_text("not made by the script", encoding="utf-8")
    failing_tools = tmp_path / "failing-tools"
    failing_tools.mkdir()
    failing_find = failing_tools / "find"
    failing_find.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    failing_find.chmod(failing_find.stat().st_mode | stat.S_IXUSR)
    environment = fixtures.environment()
    environment["PATH"] = f"{failing_tools}{os.pathsep}{environment['PATH']}"

    result = run(script, fixtures, environment)

    assert result.returncode != 0
    assert "could not list" in result.stderr
    assert precious.read_text(encoding="utf-8") == "not made by the script"
    assert leftovers_beside(fixtures.output) == []


@pytest.mark.parametrize("kind", ["symlink", "plain file"])
def test_an_output_path_that_is_not_a_plain_folder_is_refused(
    fixtures: Fixtures, tmp_path: Path, kind: str
) -> None:
    script = script_pinned_to_fixtures(fixtures, tmp_path)
    fixtures.output.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("outside the output path", encoding="utf-8")
    if kind == "symlink":
        fixtures.output.symlink_to(elsewhere, target_is_directory=True)
    else:
        fixtures.output.write_text("a file, not a folder", encoding="utf-8")

    result = run(script, fixtures)

    assert result.returncode != 0
    assert "not a plain folder" in result.stderr
    assert (elsewhere / "keep.txt").read_text(encoding="utf-8") == "outside the output path"
    assert fixtures.output.is_symlink() == (kind == "symlink")
    assert fixtures.output.exists()
    assert leftovers_beside(fixtures.output) == []


def test_a_second_run_replaces_the_folder_the_script_made_itself(
    fixtures: Fixtures, tmp_path: Path
) -> None:
    script = script_pinned_to_fixtures(fixtures, tmp_path)
    assert run(script, fixtures).returncode == 0
    stale = fixtures.output / "stale.txt"
    stale.write_text("left by the first run", encoding="utf-8")

    second = run(script, fixtures)

    assert second.returncode == 0, second.stderr
    assert not stale.exists()
    assert (fixtures.output / "bin" / "slirp4netns").read_bytes() == FIXTURE_BINARY


def test_an_empty_output_folder_is_replaced(fixtures: Fixtures, tmp_path: Path) -> None:
    script = script_pinned_to_fixtures(fixtures, tmp_path)
    fixtures.output.mkdir(parents=True)

    result = run(script, fixtures)

    assert result.returncode == 0, result.stderr
    assert (fixtures.output / "bin" / "slirp4netns").is_file()


def redirect_handler(target: str) -> type[http.server.BaseHTTPRequestHandler]:
    """A handler class that answers every GET with a 302 to ``target``."""

    class RedirectToTarget(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    return RedirectToTarget


@pytest.fixture
def redirecting_server(fixtures: Fixtures) -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), redirect_handler(fixtures.binary.as_uri()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/slirp4netns-x86_64"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_redirect_to_a_local_file_is_not_followed(
    fixtures: Fixtures, tmp_path: Path, redirecting_server: str
) -> None:
    # The script only speaks https; the copy speaks http so a loopback server can send the
    # redirect. If the script allowed "file" on redirects, the fixture binary (whose hash
    # is pinned in the copy) would be accepted and the run would succeed.
    text = script_pinned_to_fixtures(fixtures, tmp_path).read_text(encoding="utf-8")
    assert text.count("'=https") >= 2, "the script must restrict --proto and --proto-redir"
    loopback_script = tmp_path / "fetch-slirp4netns-loopback.sh"
    loopback_script.write_text(text.replace("'=https", "'=http"), encoding="utf-8")
    environment = fixtures.environment()
    environment["APPMUX_SLIRP4NETNS_BINARY_URL"] = redirecting_server

    result = run(loopback_script, fixtures, environment)

    assert result.returncode != 0, "a redirect to file:// must not be followed"
    assert not fixtures.output.exists()
