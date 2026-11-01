"""Hostile fixture pkg_env_writer: tries to write into the shared Python package environment.

Run it only inside an appmux sandbox, through the launcher. The app environment and every overlay
are mounted read-only into a child (design 5.3 and 6.4), so each attempt must fail with ``EROFS``.
It is a standalone Databricks-style app: ``python app.py`` and the port in ``DATABRICKS_APP_PORT``.

HTTP paths
    GET /          200 ``{"fixture": "pkg_env_writer"}``, so the health check passes.
    GET /report    200 JSON, computed on every call (see below).

What it tries: to create one new, empty file in
    * the parent's site-packages (``purelib`` and ``platlib`` of the running interpreter), and
    * every directory named in ``PYTHONPATH``, which is how an overlay reaches a child
      (design 5.3: ``PYTHONPATH=<overlay>``). With no overlay there is nothing to probe there and
      ``overlay_probed`` is false.
The file name is not a ``.pth`` file, so a write that succeeds executes nothing; the fixture removes
the file again at once.

``/report`` keys
    attempts                 list of ``{"kind": "site-packages" | "overlay", "directory",
                             "result"}``; ``result`` is ``"created"`` (isolation failed) or an
                             errno name.
    overlay_probed           true when ``PYTHONPATH`` named at least one directory.
    created                  the directories where the file could be created (must be []).
    site_packages_all_erofs  true when at least one site-packages directory was probed and every
                             one answered ``"EROFS"``: the site-packages half (design 6.4).
    overlay_all_erofs        true when at least one overlay directory was probed and every one
                             answered ``"EROFS"``: the overlay half. False when no overlay was
                             probed, so "not probed" can never read as "read-only".
    all_erofs                both halves above. A run with no overlay therefore reports false; a
                             test of such a run reads ``site_packages_all_erofs`` and says so.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sysconfig

import uvicorn
from fastapi import FastAPI

PROBE_FILE_NAME = "appmux-fixture-write-probe.txt"
EXPECTED_RESULT = "EROFS"
CREATED = "created"

app = FastAPI()


# Fixtures are standalone apps and cannot share code, so the hostile ones repeat this helper.
def errno_name(exc: OSError) -> str:
    """The errno name of a failed system call, or the exception type when it carries no errno."""
    if exc.errno is None:
        return type(exc).__name__
    return errno.errorcode.get(exc.errno, type(exc).__name__)


def try_create_file(directory: str) -> str:
    """``"created"`` when a new file could be made in the directory, otherwise the errno name."""
    path = os.path.join(directory, PROBE_FILE_NAME)
    try:
        file_descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        return errno_name(exc)
    os.close(file_descriptor)
    # Best-effort tidy-up: the result already says the write succeeded, which is the finding.
    with contextlib.suppress(OSError):
        os.unlink(path)
    return CREATED


def list_targets() -> list[tuple[str, str]]:
    """The ``(kind, directory)`` pairs to probe, without repeats."""
    targets: list[tuple[str, str]] = []
    for key in ("purelib", "platlib"):
        directory = sysconfig.get_path(key)
        if ("site-packages", directory) not in targets:
            targets.append(("site-packages", directory))
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if entry and ("overlay", entry) not in targets:
            targets.append(("overlay", entry))
    return targets


def kind_is_all_erofs(attempts: list[dict[str, str]], kind: str) -> bool:
    """True when at least one directory of this kind was probed and every one said ``EROFS``."""
    results = [a["result"] for a in attempts if a["kind"] == kind]
    return bool(results) and all(result == EXPECTED_RESULT for result in results)


def build_report() -> dict[str, object]:
    targets = list_targets()
    attempts = [
        {"kind": kind, "directory": directory, "result": try_create_file(directory)}
        for kind, directory in targets
    ]
    site_packages_erofs = kind_is_all_erofs(attempts, "site-packages")
    overlay_erofs = kind_is_all_erofs(attempts, "overlay")
    return {
        "fixture": "pkg_env_writer",
        "attempts": attempts,
        "overlay_probed": any(kind == "overlay" for kind, _ in targets),
        "created": [a["directory"] for a in attempts if a["result"] == CREATED],
        "site_packages_all_erofs": site_packages_erofs,
        "overlay_all_erofs": overlay_erofs,
        "all_erofs": site_packages_erofs and overlay_erofs,
    }


@app.get("/")
def health() -> dict[str, str]:
    return {"fixture": "pkg_env_writer"}


@app.get("/report")
def report() -> dict[str, object]:
    return build_report()


if __name__ == "__main__":
    uvicorn.run(
        app,
        # appmux and the platform both set UVICORN_HOST=0.0.0.0 (design 5.4). Run without it, the
        # hostile /report must not be open on every interface, so the default is loopback.
        host=os.environ.get("UVICORN_HOST", "127.0.0.1"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
