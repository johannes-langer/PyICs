"""
Pulling courses: downloading a course archive, from one of the sources (see
``sources``) or from a URL or path, checking it (see ``archive``), and extracting it to
a directory of its own, e.g. ``./search-algorithms``. The course's manifest and task
files are kept in its ``pyics_files/`` (see ``archive.relocate``), so that the
directory itself holds the exercise and solution files, and whatever else the student
works with.

``pyics_files/`` also holds an origin file, ``.pyics-origin.json``, which records where
the course came from and which files were pulled, so that the course can be updated (see
``update``): an update replaces the files that were pulled, but never the exercise
files that hold the student's work, nor any other file the student created.
"""

import hashlib
import json
import os
import shutil
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyics.distribution import archive, sources
from pyics.distribution.archive import (
    ORIGIN_FILE,
    CourseInfo,
    origin_path,
    remove_empty_dirs,
)
from pyics.distribution.dependencies import Problem, check, describe, install_hint
from pyics.distribution.fetch import FetchError, fetch, is_url, scheme, to_url
from pyics.distribution.slugs import slugify
from pyics.distribution.sources import CourseEntry, Source, SourceError
from pyics.util import paths
from pyics.util.json_handling import load_plain

ORIGIN_VERSION = 1


class PullError(Exception):
    """A course can't be pulled or updated."""


@dataclass(frozen=True)
class Pulled:
    """A course that was pulled (see ``pull``) or updated (see ``update``)."""

    # where it was pulled to
    directory: Path
    manifest_path: Path
    info: CourseInfo
    # the requirements of the course that aren't met
    problems: list[Problem] = field(default_factory=list)
    # problems with the sources that were looked in
    warnings: list[str] = field(default_factory=list)
    # for an update: the version it was updated from
    old_version: str = ""
    # for an update: whether it was up to date already, so nothing was changed
    up_to_date: bool = False
    # for an update: the files of the new version that weren't written, since they
    # would have replaced the student's files
    kept: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Download:
    data: bytes
    url: str
    sha256: str
    source: Source | None = None
    entry: CourseEntry | None = None
    warnings: list[str] = field(default_factory=list)


def is_direct(spec: str) -> bool:
    """Whether ``spec`` names a course archive (a URL, or the path of a file) rather than
    a course in the sources."""
    return is_url(spec) or spec.endswith(".zip") or Path(spec).is_file()


def _requirement_error(title: str, problems: list[Problem]) -> PullError:
    return PullError(f"{title} needs {describe(problems)}. {install_hint(problems)}")


def _fetch_archive(url: str, *, allow_http: bool) -> _Download:
    data = fetch(url, max_bytes=archive.MAX_ARCHIVE_BYTES, allow_http=allow_http)
    return _Download(data, url, hashlib.sha256(data).hexdigest())


def _download_entry(
    source: Source, entry: CourseEntry, warnings: list[str] | None = None
) -> _Download:
    """Download the archive of ``entry`` of ``source``, checking it against its hash."""
    if problems := check(requires_pyics=entry.requires_pyics):
        raise _requirement_error(entry.title, problems)
    data = fetch(
        entry.url,
        max_bytes=archive.MAX_ARCHIVE_BYTES,
        allow_http=source.allow_http,
        allow_file=scheme(source.url) == "file",
    )
    digest = hashlib.sha256(data).hexdigest()
    if digest != entry.sha256:
        raise PullError(
            f"The archive of {entry.slug!r} doesn't match its hash in the index of "
            f"{source.name!r}: it was changed or damaged on the way. Try again, or ask "
            "whoever maintains the source to rebuild its index."
        )
    return _Download(data, entry.url, digest, source, entry, warnings or [])


def _unpack(
    download: _Download, parent: Path, fallback_slug: str
) -> tuple[Path, list[str], archive.CheckedCourse]:
    """Extract ``download`` to a new temporary directory in ``parent``, check it, and
    move its manifest and task files into ``pyics_files/`` (see ``archive.relocate``).
    The directory is removed again if the course doesn't pass."""
    parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".pyics-pull-", dir=parent))
    try:
        files = archive.extract(download.data, tmp)
        # checked before anything is moved, so that all paths stay in ``tmp``
        archive.check_course(tmp, fallback_slug)
        moved = archive.relocate(tmp)
        files = sorted(moved.get(f, f) for f in files)
        checked = archive.check_course(tmp, fallback_slug)
        if problems := check(requires_pyics=checked.info.requires_pyics):
            raise _requirement_error(checked.info.title, problems)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp, files, checked


def _write_origin(
    directory: Path, download: _Download, info: CourseInfo, files: list[str]
) -> None:
    origin: dict[str, Any] = {
        "version": ORIGIN_VERSION,
        "source": download.source.name if download.source else None,
        "index": download.source.url if download.source else None,
        "slug": download.entry.slug if download.entry else info.slug,
        "url": download.url,
        "course_version": info.version,
        "sha256": download.sha256,
        "files": files,
    }
    text = json.dumps(origin, indent=4) + "\n"
    fp = origin_path(directory)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(text, encoding="utf-8")


def _check_dest(dest: Path) -> None:
    """Raise if a course can't be pulled to ``dest``: only to a new or empty
    directory."""
    if not dest.exists() or (dest.is_dir() and not any(dest.iterdir())):
        return
    if origin_path(dest).is_file() or (dest / ORIGIN_FILE).is_file():
        raise PullError(
            f"The course was pulled to {dest} already: update it with: pyics update "
            f"{dest}"
        )
    raise PullError(f"{dest} exists already: pull the course elsewhere with --dest")


def pull(
    spec: str,
    dest: Path | None = None,
    *,
    cwd: Path | None = None,
    allow_http: bool = False,
) -> Pulled:
    """
    Pull the course ``spec``: a course in the sources (its slug, or
    ``<source>/<slug>``, see ``sources.resolve``), or the URL or path of a course
    archive.

    Parameters
    ----------
    spec: str
        The course.
    dest: Path | None
        The directory to pull it to, which must be new or empty. By default, the
        course's slug in ``cwd``.
    cwd: Path | None
        The directory to pull it to by default. The working directory by default.
    allow_http: bool
        Allow downloading an archive given by URL over plain http.

    Raises
    ------
    PullError, ArchiveError
        If it can't be pulled.
    """
    cwd = paths.absolute(cwd if cwd is not None else Path.cwd())
    dest = paths.absolute(dest) if dest is not None else None
    try:
        if is_direct(spec):
            if dest is not None:
                _check_dest(dest)
            url = to_url(spec)
            download = _fetch_archive(url, allow_http=allow_http)
            name = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).stem
            fallback_slug = slugify(name)
        else:
            resolved = sources.resolve(spec)
            fallback_slug = resolved.entry.slug
            dest = dest or cwd / fallback_slug
            _check_dest(dest)
            download = _download_entry(
                resolved.source, resolved.entry, resolved.warnings
            )
    except (FetchError, SourceError) as err:
        raise PullError(str(err)) from err

    tmp, files, checked = _unpack(download, dest.parent if dest else cwd, fallback_slug)
    try:
        if dest is None:
            dest = cwd / checked.info.slug
            _check_dest(dest)
        _write_origin(tmp, download, checked.info, files)
        if dest.exists():
            dest.rmdir()  # empty, see _check_dest
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return Pulled(
        directory=dest,
        manifest_path=dest / checked.manifest_path.relative_to(tmp),
        info=checked.info,
        problems=check(checked.info.requires),
        warnings=download.warnings,
    )


def find_origin(directory: Path) -> tuple[Path, dict[str, Any]]:
    """
    The pulled course that ``directory`` is in: the directory it was pulled to, and
    its origin file's contents.

    Raises
    ------
    PullError
        If ``directory`` isn't in a pulled course.
    """
    directory = paths.absolute(directory)
    candidates = [directory, *directory.parents]
    # where it is written, before where older versions wrote it, so that the course
    # is found from within its ``pyics_files/`` too
    found = [(c, origin_path(c)) for c in candidates]
    found += [(c, c / ORIGIN_FILE) for c in candidates]
    for candidate, fp in found:
        if not fp.is_file():
            continue
        try:
            origin = json.loads(fp.read_text(encoding="utf-8"))
            if not isinstance(origin, dict) or not isinstance(
                origin.get("files"), list
            ):
                raise ValueError("it has no files")
        except ValueError as err:
            raise PullError(f"{fp} can't be read: {err}") from err
        return candidate, origin
    raise PullError(
        f"{directory} isn't a pulled course: there is no {ORIGIN_FILE} in it, nor "
        "above it"
    )


def _origin_source(origin: dict[str, Any], allow_http: bool) -> Source | None:
    """The source the course of ``origin`` was pulled from: the one of that name, or,
    if it has been removed, its index as it was then."""
    name = origin.get("source")
    if not name:
        return None
    for source in sources.load_sources():
        if source.name == name:
            return source
    if isinstance(origin.get("index"), str):
        return Source(name, origin["index"], allow_http)
    raise PullError(f"The course was pulled from the source {name!r}, which is gone")


def update(
    directory: Path = Path("."), *, allow_http: bool = False, force: bool = False
) -> Pulled:
    """
    Update the pulled course that ``directory`` is in (see ``find_origin``) to the
    latest version in its source, or at the URL it was pulled from.

    The files that were pulled are replaced, and those the new version no longer has
    are removed, but the course's exercise files, which hold the student's work, are
    never touched, nor are other files that weren't pulled.

    Parameters
    ----------
    directory: Path
        The course, or a directory in it.
    allow_http: bool
        Allow downloading over plain http, for a course pulled by URL.
    force: bool
        Update even if the latest version is the one that was pulled, e.g. to restore
        a deleted task file.

    Raises
    ------
    PullError, ArchiveError
        If it can't be updated.
    """
    root, origin = find_origin(directory)
    slug = str(origin.get("slug") or root.name)
    old_version = str(origin.get("course_version") or "")
    try:
        source = _origin_source(origin, allow_http)
        if source is not None:
            index, data = sources.fetch_index(source.url, allow_http=source.allow_http)
            sources.cache_index(source, data)
            entry = index.courses.get(slug)
            if entry is None:
                raise PullError(f"The source {source.name!r} no longer has {slug!r}")
            latest = entry.sha256
        elif isinstance(origin.get("url"), str):
            entry = None
            download = _fetch_archive(origin["url"], allow_http=allow_http)
            latest = download.sha256
        else:
            raise PullError(f"The origin file of {root} doesn't say where it came from")

        if latest == origin.get("sha256") and not force:
            manifest_path = archive.find_manifest(root)
            info = archive.course_info(load_plain(manifest_path), slug)
            return Pulled(
                root,
                manifest_path,
                info,
                check(info.requires),
                old_version=old_version,
                up_to_date=True,
            )
        if source is not None and entry is not None:
            download = _download_entry(source, entry)
    except (FetchError, SourceError) as err:
        raise PullError(str(err)) from err

    tmp, files, checked = _unpack(download, root.parent, slug)
    try:
        # the student's work: the exercise files of the old and new version, and
        # whatever wasn't pulled
        protected = archive.exercise_files(root)
        protected |= {root / p.relative_to(tmp) for p in checked.exercise_files}
        old_files = {
            f
            for f in origin["files"]
            if isinstance(f, str) and paths.absolute(root / f).is_relative_to(root)
        }
        kept: list[str] = []
        for f in files:
            target = root / f
            if target in protected or (target.exists() and f not in old_files):
                kept.append(f)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp / f, target)
        for f in sorted(old_files - set(files)):
            target = paths.absolute(root / f)
            if target in protected or not target.is_file():
                continue
            target.unlink()
            remove_empty_dirs(target, root)
        _write_origin(root, download, checked.info, [f for f in files if f not in kept])
        # where older versions wrote it
        (root / ORIGIN_FILE).unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return Pulled(
        directory=root,
        manifest_path=root / checked.manifest_path.relative_to(tmp),
        info=checked.info,
        problems=check(checked.info.requires),
        old_version=old_version,
        kept=kept,
    )
