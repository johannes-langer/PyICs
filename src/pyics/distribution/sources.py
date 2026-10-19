"""
The course libraries ("sources") the user has added, and their indexes.

A source is the URL of an index: a JSON file, served by any web server (or a local
file), that lists courses by their slugs, the names they are pulled by::

    {
        "pyics_index": 1,
        "name": "Uni Bamberg AI courses",
        "courses": {
            "search-algorithms": {
                "title": "Search Algorithms",
                "description": "DFS, BFS, ...",
                "version": "1.2.0",
                "requires_pyics": ">=0.1",
                "requires": ["numpy>=1.26"],
                "url": "courses/search-algorithms-1.2.0.zip",
                "sha256": "...",
                "tags": ["algorithms", "graphs"]
            }
        }
    }

Each course is a zip archive (see ``archive``), whose ``url`` is relative to the index.
``build_index`` writes an index for a directory of archives.

The sources are stored in ``sources.json`` in pyics' configuration directory (see
``config_dir``): in a virtual environment, one of its own, so that each environment has
its own sources, else the user's. The sources an administrator configured for all users
of a computer are in the site configuration directories (see ``site_config_dirs``). The
last fetched index of each source is cached (see ``cache_dir``), so that the courses it
lists are known without a connection.

Sources used to be stored for the user in every environment. In a virtual environment,
those are moved to the environment's own sources once (see ``migrate_legacy_sources``).
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs

from pyics.distribution import archive
from pyics.distribution.dependencies import is_newer
from pyics.distribution.fetch import FetchError, check_url, fetch, to_url
from pyics.distribution.slugs import check_slug, slugify

# overrides the user's configuration directory; only its sources are used then
CONFIG_DIR_ENV = "PYICS_CONFIG_DIR"
SOURCES_FILE = "sources.json"
INDEX_FILE = "index.json"
INDEX_VERSION = 1  # the version of the index format pyics reads and writes
MAX_INDEX_BYTES = 10 * 2**20


class SourceError(Exception):
    """A source can't be added, removed or used, or a course can't be found in them."""


def in_virtual_env() -> bool:
    """Whether pyics runs in a virtual environment (venv, virtualenv, uv or conda)."""
    return sys.prefix != sys.base_prefix or (Path(sys.prefix) / "conda-meta").is_dir()


def user_config_dir() -> Path:
    """The user's pyics configuration directory, e.g. ``~/.config/pyics`` on Linux."""
    return Path(platformdirs.user_config_dir("pyics", appauthor=False))


def user_cache_dir() -> Path:
    """The user's pyics cache directory, e.g. ``~/.cache/pyics`` on Linux."""
    return Path(platformdirs.user_cache_dir("pyics", appauthor=False))


def config_dir() -> Path:
    """
    The directory the sources are stored in: in a virtual environment, its
    ``etc/pyics``, else the user's configuration directory (see ``user_config_dir``),
    unless overridden by the ``PYICS_CONFIG_DIR`` environment variable.
    """
    if override := os.environ.get(CONFIG_DIR_ENV):
        return Path(override)
    if in_virtual_env():
        return Path(sys.prefix) / "etc" / "pyics"
    return user_config_dir()


def site_config_dirs() -> list[Path]:
    """The directories in which administrators configure sources for all users of the
    computer, e.g. ``/etc/xdg/pyics`` on Linux. None if ``PYICS_CONFIG_DIR`` is set."""
    if os.environ.get(CONFIG_DIR_ENV):
        return []
    dirs = platformdirs.site_config_dir("pyics", appauthor=False, multipath=True)
    return [Path(d) for d in dirs.split(os.pathsep) if d]


def cache_dir() -> Path:
    """Where the last fetched index of each source is kept: in a virtual environment,
    its ``var/cache/pyics``, else the user's cache directory."""
    if override := os.environ.get(CONFIG_DIR_ENV):
        return Path(override) / "cache"
    if in_virtual_env():
        return Path(sys.prefix) / "var" / "cache" / "pyics"
    return user_cache_dir()


@dataclass(frozen=True)
class Source:
    """A course library: the URL of its index, and the name it is known by."""

    name: str
    url: str
    # whether its index and archives may be fetched over plain http
    allow_http: bool = False
    # the site configuration file it is configured in for all users, None if it is one
    # of the sources in ``config_dir``
    site: Path | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "url": self.url}
        if self.allow_http:
            data["allow_http"] = True
        return data


def _read_sources(fp: Path, site: bool) -> list[Source]:
    if not fp.is_file():
        return []
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return [
            Source(
                check_slug(s["name"], "source name"),
                str(s["url"]),
                bool(s.get("allow_http", False)),
                fp if site else None,
            )
            for s in data["sources"]
        ]
    except Exception as err:  # noqa: BLE001
        raise SourceError(f"The sources in {fp} can't be read: {err}") from err


def load_sources() -> list[Source]:
    """The sources: those in ``config_dir`` first, then those configured for all users (unless
    the user has one of the same name), each in the order they were added. Courses are
    looked for in this order."""
    sources = _read_sources(config_dir() / SOURCES_FILE, site=False)
    names = {s.name for s in sources}
    for directory in site_config_dirs():
        for source in _read_sources(directory / SOURCES_FILE, site=True):
            if source.name not in names:
                names.add(source.name)
                sources.append(source)
    return sources


def _write_json(fp: Path, data: Any) -> None:
    """Write ``data`` to ``fp`` as a whole, so that it is never left half written."""
    fp.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=fp.parent, prefix=f".{fp.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.write("\n")
        os.replace(tmp, fp)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _migrated_from(fp: Path) -> list[str]:
    """The legacy sources files whose sources were moved to the sources file ``fp``
    (see ``migrate_legacy_sources``)."""
    try:
        migrated = json.loads(fp.read_text(encoding="utf-8")).get("migrated_from")
    except (OSError, ValueError, AttributeError):
        return []
    return [str(m) for m in migrated] if isinstance(migrated, list) else []


def _write_user_sources(
    sources: Iterable[Source], migrated_from: list[str] | None = None
) -> None:
    """Write the sources in ``config_dir``, keeping the legacy sources files they were
    moved from, unless given."""
    fp = config_dir() / SOURCES_FILE
    if migrated_from is None:
        migrated_from = _migrated_from(fp)
    data: dict[str, Any] = {
        "version": 1,
        "sources": [s.to_json() for s in sources if s.site is None],
    }
    if migrated_from:
        data["migrated_from"] = migrated_from
    _write_json(fp, data)


# --- sources stored for the user in all environments, by older versions ---


def legacy_sources_file() -> Path | None:
    """
    The sources file of the user, if there is one that should be moved to this
    environment: only in a virtual environment, and unless ``PYICS_CONFIG_DIR`` is set.
    """
    if os.environ.get(CONFIG_DIR_ENV) or not in_virtual_env():
        return None
    fp = user_config_dir() / SOURCES_FILE
    if not fp.is_file() or fp.absolute() == (config_dir() / SOURCES_FILE).absolute():
        return None
    return fp


def migrate_legacy_sources() -> Path | None:
    """
    Move the sources of the user's sources file (see ``legacy_sources_file``) after the
    environment's own, with their cached indexes, except those of a name or URL the
    environment has already. This is done once per file: the environment's sources
    file remembers it, so that a source removed afterwards doesn't come back.

    Returns
    -------
    Path | None
        The user's sources file, if there is one, whether its sources were moved now
        or before.
    """
    legacy = legacy_sources_file()
    if legacy is None:
        return None
    own = config_dir() / SOURCES_FILE
    migrated_from = _migrated_from(own)
    if str(legacy.absolute()) in migrated_from:
        return legacy

    current = _read_sources(own, site=False)
    names, urls = {s.name for s in current}, {s.url for s in current}
    moved = []
    for source in _read_sources(legacy, site=False):
        if source.name in names or source.url in urls:
            continue
        names.add(source.name)
        urls.add(source.url)
        moved.append(source)
    _write_user_sources([*current, *moved], [*migrated_from, str(legacy.absolute())])

    for source in moved:
        target = _cache_file(source)
        try:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_cache_file(source, user_cache_dir()), target)
        except OSError:
            pass  # the cache is only a convenience
    return legacy


def delete_legacy_sources(fp: Path) -> None:
    """Delete the user's sources file ``fp`` (see ``legacy_sources_file``), and the
    cached indexes of its sources."""
    try:
        legacy = _read_sources(fp, site=False)
    except SourceError:
        legacy = []
    for source in legacy:
        _cache_file(source, user_cache_dir()).unlink(missing_ok=True)
    fp.unlink(missing_ok=True)


# --- indexes ---


@dataclass(frozen=True)
class CourseEntry:
    """A course listed in an index."""

    slug: str
    title: str
    # the absolute URL of its archive
    url: str
    sha256: str
    description: str = ""
    version: str = ""
    requires: list[str] = field(default_factory=list)
    requires_pyics: str | None = None
    tags: list[str] = field(default_factory=list)

    def matches(self, term: str) -> bool:
        """Whether ``term`` occurs in its slug, title, description or tags, ignoring
        case."""
        term = term.lower()
        texts = [self.slug, self.title, self.description, *self.tags]
        return any(term in text.lower() for text in texts)


@dataclass(frozen=True)
class Index:
    """The index of a source."""

    name: str
    courses: dict[str, CourseEntry]


def _str_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"its {what} must be a list of strings")
    return value


def _optional_str(value: Any, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"its {what} must be a string")
    return value


def _parse_entry(slug: str, data: Any, index_url: str) -> CourseEntry:
    check_slug(slug, "course slug")
    if not isinstance(data, dict):
        raise ValueError("it must be an object")
    url, sha256 = data.get("url"), data.get("sha256")
    if not isinstance(url, str) or not url:
        raise ValueError("it has no url")
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise ValueError("it has no sha256 hash")
    try:
        int(sha256, 16)
    except ValueError:
        raise ValueError("its sha256 hash isn't hexadecimal") from None
    requires_pyics = data.get("requires_pyics")
    if requires_pyics is not None and not isinstance(requires_pyics, str):
        raise ValueError("its requires_pyics must be a version specifier")
    return CourseEntry(
        slug=slug,
        title=_optional_str(data.get("title"), "title") or slug,
        url=urllib.parse.urljoin(index_url, url),
        sha256=sha256.lower(),
        description=_optional_str(data.get("description"), "description"),
        version=_optional_str(data.get("version"), "version"),
        requires=_str_list(data.get("requires"), "requires"),
        requires_pyics=requires_pyics or None,
        tags=_str_list(data.get("tags"), "tags"),
    )


def parse_index(data: Any, url: str) -> Index:
    """
    Parse the index ``data`` (decoded JSON) fetched from ``url``.

    Raises
    ------
    SourceError
        If it isn't a valid index, or needs a newer version of pyics.
    """
    if not isinstance(data, dict) or "pyics_index" not in data:
        raise SourceError(f"{url} isn't a pyics index")
    version = data["pyics_index"]
    if not isinstance(version, int) or version < 1:
        raise SourceError(f"{url} has an invalid index version {version!r}")
    if version > INDEX_VERSION:
        raise SourceError(
            f"{url} is an index of version {version}, which needs a newer pyics: "
            "upgrade it with: pip install -U pyics"
        )
    name = data.get("name")
    courses = data.get("courses")
    if not isinstance(courses, dict):
        raise SourceError(f"The index {url} has no courses")
    entries: dict[str, CourseEntry] = {}
    for slug, entry in courses.items():
        try:
            entries[slug] = _parse_entry(slug, entry, url)
        except ValueError as err:
            raise SourceError(f"The course {slug!r} of the index {url}: {err}") from err
    return Index(name if isinstance(name, str) else "", entries)


def fetch_index(url: str, *, allow_http: bool = False) -> tuple[Index, Any]:
    """
    Fetch and parse the index at ``url``.

    Returns
    -------
    tuple[Index, Any]
        The index, and its decoded JSON, e.g. to cache it.
    """
    try:
        raw = fetch(url, max_bytes=MAX_INDEX_BYTES, allow_http=allow_http)
    except FetchError as err:
        raise SourceError(str(err)) from err
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as err:  # including UnicodeDecodeError
        raise SourceError(f"{url} isn't a pyics index: {err}") from err
    return parse_index(data, url), data


def _cache_file(source: Source, directory: Path | None = None) -> Path:
    """Where the index of ``source`` is cached, in ``directory`` (``cache_dir`` by
    default)."""
    # the URL is hashed into the name, so the index of a source that is removed and
    # added again with another URL isn't mistaken for its old one
    digest = hashlib.sha256(source.url.encode()).hexdigest()[:16]
    directory = cache_dir() if directory is None else directory
    return directory / "indexes" / f"{source.name}-{digest}.json"


def cache_index(source: Source, data: Any) -> None:
    """Keep the index ``data`` of ``source``, as fetched last."""
    try:
        _write_json(_cache_file(source), data)
    except OSError:
        pass  # the cache is only a convenience


def cached_index(source: Source) -> Index | None:
    """The index of ``source`` as it was fetched last, if it is cached."""
    try:
        data = json.loads(_cache_file(source).read_text(encoding="utf-8"))
        return parse_index(data, source.url)
    except (OSError, ValueError, SourceError):
        return None


@dataclass(frozen=True)
class SourceIndex:
    """The index of a source, as far as it is known (see ``load_indexes``)."""

    source: Source
    index: Index | None
    # why it couldn't be fetched, if it couldn't
    error: str | None = None
    # whether it is the cached index, since it couldn't be fetched
    cached: bool = False


def load_indexes(
    sources: Sequence[Source] | None = None, *, refresh: bool = True
) -> list[SourceIndex]:
    """
    The indexes of ``sources`` (all sources by default): fetched anew if ``refresh``,
    falling back to the cached ones of the sources that can't be fetched, or only the
    cached ones otherwise.
    """
    results: list[SourceIndex] = []
    for source in load_sources() if sources is None else sources:
        if not refresh:
            results.append(SourceIndex(source, cached_index(source), cached=True))
            continue
        try:
            index, data = fetch_index(source.url, allow_http=source.allow_http)
        except SourceError as err:
            cached = cached_index(source)
            results.append(SourceIndex(source, cached, str(err), cached is not None))
            continue
        cache_index(source, data)
        results.append(SourceIndex(source, index))
    return results


def prepare_source(
    location: str, name: str | None = None, *, allow_http: bool = False
) -> tuple[Source, Index, Any]:
    """
    Fetch and check the index at ``location`` (a URL or a path) to add it as a source,
    named ``name``, or else after the index's name. Nothing is stored yet (see
    ``save_source``).

    Returns
    -------
    tuple[Source, Index, Any]
        The source, its index, and the index's decoded JSON.
    """
    url = to_url(location)
    try:
        check_url(url, allow_http=allow_http)
    except FetchError as err:
        raise SourceError(str(err)) from err
    index, data = fetch_index(url, allow_http=allow_http)

    if name is None:
        name = slugify(index.name) or slugify(urllib.parse.urlsplit(url).hostname or "")
        if not name:
            raise SourceError("The index has no name: name the source with --name")
    try:
        check_slug(name, "source name")
    except ValueError as err:
        raise SourceError(str(err)) from err
    for source in load_sources():
        if source.url == url:
            raise SourceError(f"{url} is added already, as {source.name!r}")
        if source.name == name:
            raise SourceError(
                f"There is a source named {name!r} already ({source.url}): name this "
                "one differently with --name"
            )
    return Source(name, url, allow_http), index, data


def save_source(source: Source, data: Any) -> None:
    """Add ``source`` (see ``prepare_source``) after the user's other sources, and cache
    its index ``data``."""
    _write_user_sources([*load_sources(), source])
    cache_index(source, data)


def remove_source(name: str) -> Source:
    """Remove the user's source ``name``, and its cached index."""
    sources = load_sources()
    for source in sources:
        if source.name != name:
            continue
        if source.site is not None:
            raise SourceError(
                f"{name!r} is configured for all users of this computer, in "
                f"{source.site}, so it can only be removed there"
            )
        _write_user_sources(s for s in sources if s is not source)
        _cache_file(source).unlink(missing_ok=True)
        return source
    raise SourceError(f"There is no source named {name!r}")


@dataclass(frozen=True)
class Resolved:
    """A course found in the sources (see ``resolve``)."""

    source: Source
    entry: CourseEntry
    # problems with the sources that were looked in
    warnings: list[str]


def _warnings(indexes: Iterable[SourceIndex]) -> list[str]:
    warnings = []
    for result in indexes:
        if result.error is None:
            continue
        note = "using its cached index" if result.cached else "skipping it"
        warnings.append(f"{result.source.name}: {result.error} ({note})")
    return warnings


def resolve(spec: str) -> Resolved:
    """
    Find the course ``spec`` in the sources: a slug, e.g. ``search-algorithms``, or a
    slug in a source, e.g. ``uni-bamberg/search-algorithms``. The sources' indexes are
    fetched anew (falling back to the cached ones).

    Raises
    ------
    SourceError
        If no source, or more than one, lists it.
    """
    source_name, _, slug = spec.rpartition("/")
    sources = load_sources()
    if not sources:
        raise SourceError(
            "You haven't added any sources: add one with: pyics add_source <url>"
        )
    if source_name:
        sources = [s for s in sources if s.name == source_name]
        if not sources:
            raise SourceError(f"There is no source named {source_name!r}")

    indexes = load_indexes(sources)
    found = [
        (result.source, result.index.courses[slug])
        for result in indexes
        if result.index is not None and slug in result.index.courses
    ]
    warnings = _warnings(indexes)
    if not found:
        where = f"the source {source_name!r}" if source_name else "your sources"
        message = f"There is no course {slug!r} in {where}"
        if warnings:
            message += " (" + "; ".join(warnings) + ")"
        raise SourceError(message + ". Find courses with: pyics search")
    if len(found) > 1:
        options = ", ".join(f"{s.name}/{e.slug}" for s, e in found)
        raise SourceError(
            f"Several sources have a course {slug!r}: pull one of {options}"
        )
    source, entry = found[0]
    return Resolved(source, entry, warnings)


def search(
    term: str | None = None, *, refresh: bool = True
) -> tuple[list[tuple[Source, CourseEntry]], list[str]]:
    """
    The courses in the sources that match ``term`` (see ``CourseEntry.matches``), or
    all of them.

    Returns
    -------
    tuple[list[tuple[Source, CourseEntry]], list[str]]
        The courses, with their sources, and problems with the sources.
    """
    indexes = load_indexes(refresh=refresh)
    courses = [
        (result.source, entry)
        for result in indexes
        if result.index is not None
        for entry in result.index.courses.values()
        if term is None or entry.matches(term)
    ]
    return courses, _warnings(indexes)


def cached_courses() -> list[tuple[Source, CourseEntry]]:
    """The courses in the cached indexes of the sources, without fetching anything, e.g.
    to offer them in the course selector."""
    try:
        courses, _ = search(refresh=False)
    except SourceError:  # e.g. an unreadable sources file
        return []
    return courses


# --- writing indexes ---


def build_index(
    directory: Path, out: Path | None = None, *, name: str | None = None
) -> tuple[Path, dict[str, Any]]:
    """
    Write an index of the course archives in ``directory`` and its subdirectories to
    ``out`` (``index.json`` in ``directory`` by default), linking to them relative to
    it. Of several versions of a course, the newest is listed. The index is named
    ``name``, or keeps the name of the index it replaces, or else is named after the
    directory.

    Returns
    -------
    tuple[Path, dict[str, Any]]
        The path the index was written to, and the index.
    """
    directory = Path(directory).absolute()
    out = Path(out).absolute() if out is not None else directory / INDEX_FILE
    if name is None and out.is_file():
        try:
            name = json.loads(out.read_text(encoding="utf-8")).get("name")
        except (OSError, ValueError, AttributeError):
            name = None
    name = name or directory.name

    courses: dict[str, dict[str, Any]] = {}
    for zip_path in sorted(directory.rglob("*.zip")):
        try:
            info = archive.read_archive(zip_path)
        except archive.ArchiveError as err:
            raise SourceError(f"{zip_path} isn't a course archive: {err}") from err
        current = courses.get(info.slug)
        if current is not None and not is_newer(info.version, current["version"]):
            continue
        rel = Path(os.path.relpath(zip_path, out.parent)).as_posix()
        entry: dict[str, Any] = {"title": info.title}
        if info.description:
            entry["description"] = info.description
        entry["version"] = info.version
        if info.requires_pyics:
            entry["requires_pyics"] = info.requires_pyics
        if info.requires:
            entry["requires"] = info.requires
        entry["url"] = urllib.parse.quote(rel)
        entry["sha256"] = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        if info.tags:
            entry["tags"] = info.tags
        courses[info.slug] = entry

    index = {"pyics_index": INDEX_VERSION, "name": name, "courses": courses}
    _write_json(out, index)
    return out, index
