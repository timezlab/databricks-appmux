"""FR-054: the repository holds the Apache-2.0 licence and no slirp4netns binary or source.

The release zip carries the upstream binary with its source (ADR 0015), built
into the git-ignored ``build/`` folder; the repository never does.
"""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# Folders that are not the repository's own tree: VCS data, environments, generated output.
IGNORED_FOLDERS = frozenset({".git", ".venv", "node_modules", "build", "__pycache__"})
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar", ".zip", ".gz", ".xz")
FORBIDDEN_PREFIXES = ("slirp4netns", "libslirp")


def tracked_candidates() -> list[Path]:
    return [
        path
        for path in REPOSITORY_ROOT.rglob("*")
        if path.is_file()
        and not IGNORED_FOLDERS.intersection(path.relative_to(REPOSITORY_ROOT).parts)
    ]


def is_forbidden(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith(FORBIDDEN_PREFIXES):
        return True
    return name.endswith(ARCHIVE_SUFFIXES) and "slirp" in name


def test_licence_is_apache_2_0() -> None:
    text = (REPOSITORY_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text


def test_no_slirp4netns_binary_or_source_in_the_repository() -> None:
    offenders = [
        str(p.relative_to(REPOSITORY_ROOT)) for p in tracked_candidates() if is_forbidden(p)
    ]
    assert offenders == [], (
        f"slirp4netns binary or source must stay out of the repository: {offenders}"
    )


def test_detector_flags_the_files_it_is_meant_to_catch(tmp_path: Path) -> None:
    flagged = ["slirp4netns", "slirp4netns-v1.3.6", "libslirp-4.9.1.tar.gz", "src-slirp4netns.zip"]
    allowed = ["0015-bundle-slirp4netns-in-the-release-zip.md", "app.py", "notes.tar.gz"]
    assert all(is_forbidden(tmp_path / name) for name in flagged)
    assert not any(is_forbidden(tmp_path / name) for name in allowed)
