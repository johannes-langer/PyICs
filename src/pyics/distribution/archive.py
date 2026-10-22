"""
Course archives: zip files that hold one course each, as written by ``pack``. An
archive holds the course manifest and its task files (with the exercise and solution
code they store), at the same places relative to each other as where they were packed,
and any other files the course needs, e.g. data its exercises read. Exercise and
solution files aren't included: they are created from the task files once the course is
started.

Once extracted, ``relocate`` moves the manifest and task files into ``pyics_files/``,
so that the student's files aren't mixed with pyics's own.

Archives are checked before any of their contents is used: ``extract`` refuses entries
that would end up outside the directory they are extracted to, and ``check_course``
refuses courses whose task files would write files outside it, or make json_tricks
import modules the course doesn't declare.
"""

import io
import os
import stat
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from pyics.distribution.dependencies import (
    ModulePolicy,
    manifest_requirements,
    pip_install_lines,
)
from pyics.distribution.slugs import check_slug, slugify
from pyics.notebook import cells
from pyics.util import json_handling as json
from pyics.util import paths
from pyics.util.discovery import COURSE_SUFFIX, SKIP_DIRS, TASK_SUFFIX, find_files

# where a pulled course keeps its manifest, task files and origin file, so that its
# directory holds the student's files only (see ``relocate``)
PYICS_DIR = "pyics_files"
# written to a pulled course's ``PYICS_DIR`` (see ``pyics.distribution.pull``, and
# ``origin_path``), or, by older versions, to its directory
ORIGIN_FILE = ".pyics-origin.json"
MAX_ARCHIVE_BYTES = 100 * 2**20
MAX_UNPACKED_BYTES = 500 * 2**20
MAX_MEMBERS = 10_000
# how deep below the root of an archive its manifest may be
MANIFEST_DEPTH = 2
# never packed when including directories
_SKIP_FILES = {ORIGIN_FILE, ".pyics.log", ".DS_Store"}
_SKIP_SUFFIXES = (".pyc", ".pyo")
# fixed, so that packing the same course twice gives the same archive (and hash)
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


class ArchiveError(Exception):
    """A course can't be packed, or an archive isn't a valid course."""


def origin_path(directory: Path) -> Path:
    """Where the origin file of the course pulled to ``directory`` is written."""
    return directory / PYICS_DIR / ORIGIN_FILE


@dataclass(frozen=True)
class CourseInfo:
    """What a course manifest says about the course (see ``course_info``)."""

    title: str
    slug: str
    version: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    requires_pyics: str | None = None


def course_info(manifest: Mapping[str, Any], fallback_slug: str = "") -> CourseInfo:
    """
    The title, slug and other metadata of a course ``manifest``. Its slug is its
    ``slug`` field, or else derived from its name, or else ``fallback_slug``.

    Raises
    ------
    ArchiveError
        If a field is invalid.
    """
    name = manifest.get("name")
    title = name if isinstance(name, str) and name else fallback_slug
    try:
        slug = manifest.get("slug") or slugify(title) or fallback_slug
        check_slug(slug, "course slug")
        requires, requires_pyics = manifest_requirements(manifest)
    except ValueError as err:
        raise ArchiveError(f"The course manifest is invalid: {err}") from err
    tags = manifest.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ArchiveError("The course manifest is invalid: its tags must be strings")
    description = manifest.get("description") or ""
    if not isinstance(description, str):
        raise ArchiveError("The course manifest is invalid: its description isn't text")
    return CourseInfo(
        title=title,
        slug=slug,
        version=str(manifest.get("version") or ""),
        description=description,
        tags=tags,
        requires=requires,
        requires_pyics=requires_pyics,
    )


def _map_paths(data: Any, fn: Callable[[PurePath | str], Any]) -> Any:
    """
    A copy of a task file's ``data`` (as read by ``json_handling.loads_plain``), with
    ``fn`` applied to each path it refers to: its ``PurePath`` objects (e.g. its
    exercise and solution files), the strings stored as ``file_path`` (e.g. by a test)
    or listed in ``ex_files`` and ``sol_files``, and the notebook paths its ``sections``
    are keyed by.
    """

    def walk(obj: Any, key: Any = None) -> Any:
        if isinstance(obj, PurePath):
            return fn(obj)
        if key == "file_path" and isinstance(obj, str):
            return fn(obj)
        if key in ("ex_files", "sol_files") and isinstance(obj, list):
            return [fn(f) if isinstance(f, (str, PurePath)) else walk(f) for f in obj]
        if isinstance(obj, dict):
            mapped = {k: walk(v, k) for k, v in obj.items()}
            if obj is data and isinstance(mapped.get("sections"), dict):
                mapped["sections"] = {
                    fn(k) if isinstance(k, str) else k: v
                    for k, v in mapped["sections"].items()
                }
            return mapped
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(walk(v) for v in obj)
        if isinstance(obj, (set, frozenset)):
            return type(obj)(walk(v) for v in obj)
        return obj

    return walk(data)


def _task_files_paths(data: Mapping[str, Any], base: Path) -> tuple[list[Path], ...]:
    """
    The paths a task file's ``data`` (as read by ``json_handling.loads_plain``) refers
    to (see ``_map_paths``), resolved relative to its directory ``base``.

    Returns
    -------
    tuple[list[Path], list[Path], list[Path]]
        Its exercise files, its solution files, and all paths it refers to (e.g. the
        files its tests test).
    """

    def resolved(key: str) -> list[Path]:
        files = data.get(key) or []
        if not isinstance(files, list) or not all(
            isinstance(f, (str, PurePath)) for f in files
        ):
            raise ArchiveError(f"The {key} of a task file aren't paths")
        return [paths.resolve(f, base) for f in files]

    exercises, solutions = resolved("ex_files"), resolved("sol_files")
    referenced: list[Path] = []

    def collect(path: PurePath | str) -> PurePath | str:
        referenced.append(paths.resolve(path, base))
        return path

    _map_paths(data, collect)
    return exercises, solutions, referenced


# --- packing ---


@dataclass
class CourseFiles:
    """A course to be packed, as read by ``read_course``. Paths are absolute."""

    manifest_path: Path
    # as read by ``json_handling.loads_plain``
    manifest: dict[str, Any]
    # the task files, mapped to their data as read by ``json_handling.loads_plain``
    tasks: dict[Path, dict[str, Any]]
    exercise_files: set[Path]
    solution_files: set[Path]
    # all paths the task files refer to, including the exercise and solution files
    referenced: set[Path]

    @property
    def info(self) -> CourseInfo:
        return course_info(self.manifest, slugify(self.manifest_path.name))


def read_course(manifest_path: Path) -> CourseFiles:
    """
    Read the course of the manifest at ``manifest_path`` and its task files, as plain
    data (see ``json_handling.loads_plain``).

    Raises
    ------
    ArchiveError
        If the manifest or one of its task files can't be read.
    """
    manifest_path = paths.absolute(manifest_path)
    if not manifest_path.name.endswith(COURSE_SUFFIX):
        raise ArchiveError(
            f"{manifest_path} is not a course manifest (*{COURSE_SUFFIX})"
        )
    try:
        manifest = json.load_plain(manifest_path)
    except Exception as err:  # noqa: BLE001
        raise ArchiveError(
            f"Can't read the course manifest {manifest_path}: {err}"
        ) from err
    tasks_map = manifest.get("tasks") if isinstance(manifest, dict) else None
    if not isinstance(tasks_map, dict) or not tasks_map:
        raise ArchiveError(f"The course manifest {manifest_path} has no tasks")

    course = CourseFiles(manifest_path, manifest, {}, set(), set(), set())
    for name, rel in tasks_map.items():
        path = paths.absolute(paths.resolve(rel, manifest_path.parent))
        try:
            data = json.load_plain(path)
            if not isinstance(data, dict):
                raise TypeError("it isn't a task")
        except Exception as err:  # noqa: BLE001
            raise ArchiveError(f"Can't read the task file of {name!r}: {err}") from err
        course.tasks[path] = data
        exercises, solutions, referenced = _task_files_paths(data, path.parent)
        course.exercise_files.update(map(paths.absolute, exercises))
        course.solution_files.update(map(paths.absolute, solutions))
        course.referenced.update(map(paths.absolute, referenced))
    return course


def differing_notebooks(
    tasks: Mapping[Path, Mapping[str, Any]],
) -> dict[Path, list[Path]]:
    """
    The exercise notebooks that several of the ``tasks`` share, but store different
    contents for (compared normalized, see ``cells.normalize``). The first of them to
    be set up creates the notebook, so which contents students get depends on the task
    they start first.

    Parameters
    ----------
    tasks: Mapping[Path, Mapping[str, Any]]
        Task files by absolute path, mapped to their data as read by
        ``json_handling.loads_plain``. Those whose exercise files can't be read are
        left out.

    Returns
    -------
    dict[Path, list[Path]]
        The notebooks' absolute paths, mapped to the task files that share them.
    """
    stored: dict[Path, dict[Path, str]] = {}
    for task_file, data in tasks.items():
        try:
            exercises, _, _ = _task_files_paths(data, task_file.parent)
        except ArchiveError:
            continue
        codes = data.get("exercise_code")
        for path, text in zip(exercises, codes if isinstance(codes, list) else []):
            if cells.is_notebook(path) and isinstance(text, str):
                try:
                    text = cells.normalize(text)
                except cells.NotebookError:
                    pass  # compared as it is
                stored.setdefault(paths.absolute(path), {})[task_file] = text
    return {
        notebook: list(contents)
        for notebook, contents in stored.items()
        if len(set(contents.values())) > 1
    }


def pip_installs(
    tasks: Mapping[Path, Mapping[str, Any]],
) -> list[tuple[Path, int, str]]:
    """
    The lines that install packages (see ``dependencies.pip_install_lines``) in the
    exercise and solution notebooks the ``tasks`` store, once each, e.g. those of a
    notebook that several tasks share.

    Parameters
    ----------
    tasks: Mapping[Path, Mapping[str, Any]]
        Task files by absolute path, mapped to their data as read by
        ``json_handling.loads_plain``.

    Returns
    -------
    list[tuple[Path, int, str]]
        The notebook's absolute path, the number of the line's cell, and the line.
    """
    found: dict[tuple[Path, int, str], None] = {}  # ordered set
    for task_file, data in tasks.items():
        try:
            exercises, solutions, _ = _task_files_paths(data, task_file.parent)
        except ArchiveError:
            continue
        for files, key in ((exercises, "exercise_code"), (solutions, "solution_code")):
            codes = data.get(key)
            for path, text in zip(files, codes if isinstance(codes, list) else []):
                if cells.is_notebook(path) and isinstance(text, str):
                    for number, line in pip_install_lines(text):
                        found[(paths.absolute(path), number, line)] = None
    return list(found)


def _included_files(
    include: Iterable[Path], course: CourseFiles, exclude: set[Path]
) -> list[Path]:
    """The files of ``include`` (files, or directories with their files below), without
    the files of ``course`` and the files in ``exclude``, nor caches and the like."""
    course_files = {course.manifest_path, *course.tasks}
    student_files = course.exercise_files | course.solution_files
    found: dict[Path, None] = {}  # ordered set

    def add(path: Path) -> None:
        if path in course_files or path in exclude or path.name in _SKIP_FILES:
            return
        if path in student_files or path.suffix in _SKIP_SUFFIXES:
            return
        if path.name.endswith(COURSE_SUFFIX):
            raise ArchiveError(f"Can't include {path}: it is another course's manifest")
        found[path] = None

    for item in include:
        item = paths.absolute(item)
        if item.is_file():
            add(item)
        elif item.is_dir():
            for dirpath, dirnames, filenames in os.walk(item):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                for filename in sorted(filenames):
                    add(Path(dirpath) / filename)
        else:
            raise ArchiveError(f"Can't include {item}: it doesn't exist")
    return list(found)


def course_root(course: CourseFiles, included: Iterable[Path] = ()) -> Path:
    """The directory an archive of ``course`` is packed relative to: the innermost one
    that holds the manifest, the task files, the files they refer to, and the
    ``included`` files."""
    dirs = [
        course.manifest_path.parent,
        *(p.parent for p in course.tasks),
        *(p.parent for p in course.referenced),
        *(p.parent for p in included),
    ]
    try:
        return Path(os.path.commonpath(dirs))
    except ValueError as err:  # e.g. on different drives on Windows
        raise ArchiveError(
            f"The course's files have no common directory: {err}"
        ) from err


def local_modules(course: CourseFiles) -> set[str]:
    """The modules that are part of ``course`` itself: its exercise and solution files,
    and the modules next to them and in its root directory, which its code may import
    without them being installed."""
    modules = {p.stem for p in course.exercise_files | course.solution_files}
    dirs = {course_root(course), *(p.parent for p in course.exercise_files)}
    for directory in dirs:
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            if child.suffix == ".py" or (child / "__init__.py").is_file():
                modules.add(child.stem)
    return modules


def _write_zip(out: Path, members: Mapping[str, bytes]) -> None:
    """Write ``members`` (by archive path) to the zip file ``out``, reproducibly and as a
    whole."""
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(members):
                info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
                info.create_system = 3  # unix, so that the permissions are read
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, members[name])
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def pack(
    course: CourseFiles,
    out: Path,
    *,
    include: Iterable[Path] = (),
    no_solutions: bool = False,
) -> list[str]:
    """
    Pack ``course`` into the archive ``out``: its manifest, without the progress made
    on it, its task files, and the ``include``d files (or the files in the ``include``d
    directories, see ``_included_files``).

    Parameters
    ----------
    course: CourseFiles
        The course, as read by ``read_course``.
    out: Path
        Where to write the archive.
    include: Iterable[Path]
        Other files the course needs, e.g. data its exercises read, or directories of
        them. Exercise and solution files are never included.
    no_solutions: bool
        Leave the solutions out of the task files, e.g. for a course that is graded.

    Returns
    -------
    list[str]
        The paths of the files in the archive.
    """
    out = paths.absolute(out)
    included = _included_files(include, course, exclude={out})
    root = course_root(course, included)

    def name(path: Path) -> str:
        return path.relative_to(root).as_posix()

    members: dict[str, bytes] = {}
    manifest = dict(course.manifest)
    manifest["progress"] = [0] * len(course.tasks)
    members[name(course.manifest_path)] = json.dumps(manifest, indent=4).encode()
    for path, data in course.tasks.items():
        if no_solutions:
            data = {**data, "sol_files": [], "solution_code": []}
        members[name(path)] = json.dumps(data, indent=4).encode()
    for path in included:
        members[name(path)] = path.read_bytes()
    _write_zip(out, members)
    return sorted(members)


def update_manifest(manifest_path: Path, changes: Mapping[str, Any]) -> None:
    """Set the fields ``changes`` in the course manifest at ``manifest_path``, removing
    those set to None, and keep the rest as it is."""
    manifest = json.load_plain(manifest_path)
    for key, value in changes.items():
        if value is None:
            manifest.pop(key, None)
        else:
            manifest[key] = value
    text = json.dumps(manifest, indent=4)
    Path(manifest_path).write_text(text, encoding="utf-8")


# --- checking and extracting ---


def _check_member(info: zipfile.ZipInfo) -> list[str]:
    """The parts of the path of the archive entry ``info``, if it may be extracted:
    a relative path that stays in the directory it is extracted to, and no link."""
    name = info.filename
    parts = name.rstrip("/").split("/")
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or ":" in parts[0]  # a drive, e.g. C:
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ArchiveError(f"The archive has an entry with an unsafe path: {name!r}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ArchiveError(f"The archive has a link, which isn't supported: {name!r}")
    if info.flag_bits & 0x1:
        raise ArchiveError(f"The archive has an encrypted entry: {name!r}")
    return parts


def _open_zip(data: bytes | Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data) if isinstance(data, bytes) else data)
    except (zipfile.BadZipFile, OSError) as err:
        raise ArchiveError(f"It isn't a zip archive: {err}") from err


def _checked_members(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, list[str]]]:
    """The entries of ``zf`` with the parts of their paths, once all of them have been
    checked (see ``_check_member``), and their number and size are within limits."""
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ArchiveError(f"The archive has more than {MAX_MEMBERS} entries")
    members, seen, total = [], set(), 0
    for info in infos:
        parts = _check_member(info)
        key = "/".join(parts).lower()  # the same file on case-insensitive systems
        if key in seen:
            raise ArchiveError(f"The archive has {info.filename!r} twice")
        seen.add(key)
        total += info.file_size
        if total > MAX_UNPACKED_BYTES:
            raise ArchiveError(
                f"The archive unpacks to more than {MAX_UNPACKED_BYTES // 2**20} MB"
            )
        members.append((info, parts))
    return members


def read_archive(data: bytes | Path) -> CourseInfo:
    """
    What the manifest of the course archive ``data`` (its contents, or its path) says
    about the course, without extracting it. Its slug falls back to the archive's file
    name.

    Raises
    ------
    ArchiveError
        If it isn't a course archive.
    """
    with _open_zip(data) as zf:
        members = _checked_members(zf)
        manifests = [
            info
            for info, parts in members
            if parts[-1].endswith(COURSE_SUFFIX) and len(parts) <= MANIFEST_DEPTH + 1
        ]
        if len(manifests) != 1:
            raise ArchiveError(
                f"The archive has {len(manifests)} course manifests instead of one"
            )
        try:
            manifest = json.loads_plain(zf.read(manifests[0]).decode("utf-8"))
        except Exception as err:  # noqa: BLE001
            raise ArchiveError(f"Its course manifest can't be read: {err}") from err
    if not isinstance(manifest, dict):
        raise ArchiveError("Its course manifest isn't a course manifest")
    fallback = slugify(Path(data).stem) if isinstance(data, Path) else ""
    return course_info(manifest, fallback)


def extract(data: bytes, dest: Path) -> list[str]:
    """
    Extract the archive ``data`` to the directory ``dest``, once all its entries have
    been checked to stay in it (see ``_checked_members``).

    Returns
    -------
    list[str]
        The paths of the extracted files, relative to ``dest``.
    """
    files: list[str] = []
    with _open_zip(data) as zf:
        members = _checked_members(zf)
        try:
            for info, parts in members:
                target = dest.joinpath(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "xb") as dst:
                    # reads at most the size the entry declares
                    while chunk := src.read(2**20):
                        dst.write(chunk)
                files.append("/".join(parts))
        except (OSError, zipfile.BadZipFile, EOFError) as err:
            raise ArchiveError(f"The archive can't be extracted: {err}") from err
    return sorted(files)


@dataclass(frozen=True)
class CheckedCourse:
    """A course that passed ``check_course``."""

    manifest_path: Path
    info: CourseInfo
    # the exercise files its tasks create, which hold the student's work
    exercise_files: frozenset[Path]


def _inside(root: Path, path: Path, what: str) -> Path:
    path = paths.absolute(path)
    if not path.is_relative_to(paths.absolute(root)):
        raise ArchiveError(f"{what} is outside of the course's directory: {path}")
    return path


def _manifests(root: Path) -> list[Path]:
    """The course manifests in the directory ``root`` (see ``find_manifest``),
    relative to it."""
    return [
        rel
        for rel in find_files(root, MANIFEST_DEPTH + 1, COURSE_SUFFIX)
        if len(rel.parent.parts) <= MANIFEST_DEPTH or rel.parts[0] == PYICS_DIR
    ]


def find_manifest(root: Path) -> Path:
    """The single course manifest in the directory ``root``: at most
    ``MANIFEST_DEPTH`` directories below it, or one more in ``PYICS_DIR`` (see
    ``relocate``)."""
    manifests = _manifests(root)
    if len(manifests) != 1:
        raise ArchiveError(
            f"The course has {len(manifests)} course manifests instead of one"
        )
    return root / manifests[0]


def check_course(root: Path, fallback_slug: str = "") -> CheckedCourse:
    """
    Check the course in the directory ``root``, e.g. an extracted archive, before it
    is used:

    * it has a single course manifest, holding plain data only (see ``course_info``),
    * its task files are in ``root``, and all files they refer to (exercise and
      solution files, which are written when the course is started, and the files
      their tests test) are, too, and aren't the course's own files,
    * its task files name only modules for json_tricks to import that the course
      declares (see ``ModulePolicy``).

    Raises
    ------
    ArchiveError
        If it doesn't pass.
    """
    manifest_path = find_manifest(root)
    try:
        manifest = json.load_plain(manifest_path)
    except Exception as err:  # noqa: BLE001
        raise ArchiveError(f"The course manifest can't be read: {err}") from err
    if not isinstance(manifest, dict):
        raise ArchiveError("The course manifest isn't a course manifest")
    tags = json.scan_tags(manifest)
    if tags.modules or tags.packages:
        raise ArchiveError("The course manifest holds objects, rather than plain data")
    info = course_info(manifest, fallback_slug)
    tasks_map = manifest.get("tasks")
    if not isinstance(tasks_map, dict) or not tasks_map:
        raise ArchiveError("The course manifest has no tasks")

    task_paths: dict[str, Path] = {}
    for name, rel in tasks_map.items():
        if not isinstance(rel, (str, PurePath)):
            raise ArchiveError(f"The task {name!r} has no task file")
        path = _inside(
            root, paths.resolve(rel, manifest_path.parent), f"The task {name!r}"
        )
        if not path.name.endswith(TASK_SUFFIX) or not path.is_file():
            raise ArchiveError(f"The task file of {name!r} is missing: {path}")
        task_paths[name] = path
    own_files = {
        paths.absolute(manifest_path),
        paths.absolute(root / ORIGIN_FILE),
        paths.absolute(origin_path(root)),
    }
    own_files |= set(task_paths.values())

    policy = ModulePolicy(info.requires)
    exercise_files: set[Path] = set()
    for name, path in task_paths.items():
        try:
            data = json.load_plain(path)
            if not isinstance(data, dict):
                raise TypeError("it isn't a task")
        except Exception as err:  # noqa: BLE001
            raise ArchiveError(
                f"The task file of {name!r} can't be read: {err}"
            ) from err
        for module in sorted(json.scan_tags(data).modules, key=str):
            if not policy.allows(module):
                raise ArchiveError(
                    f"The task {name!r} uses the module {module!r}, which the course "
                    "doesn't declare in its requires"
                )
        exercises, solutions, referenced = _task_files_paths(data, path.parent)
        for file in referenced:
            _inside(root, file, f"A file of the task {name!r}")
        for file in exercises + solutions:
            if paths.absolute(file) in own_files:
                raise ArchiveError(
                    f"The task {name!r} would overwrite a file of the course: {file}"
                )
        exercise_files.update(paths.absolute(file) for file in exercises)
    return CheckedCourse(manifest_path, info, frozenset(exercise_files))


def exercise_files(root: Path) -> set[Path]:
    """The exercise files of the courses in the directory ``root``, as far as their
    manifests and task files can be read."""
    found: set[Path] = set()
    for rel in _manifests(root):
        manifest_path = root / rel
        try:
            manifest = json.load_plain(manifest_path)
            task_paths = [
                paths.resolve(v, manifest_path.parent)
                for v in manifest["tasks"].values()
            ]
        except Exception:  # noqa: BLE001
            continue
        for path in task_paths:
            try:
                exercises, _, _ = _task_files_paths(json.load_plain(path), path.parent)
            except Exception:  # noqa: BLE001
                continue
            found.update(paths.absolute(file) for file in exercises)
    return found


def remove_empty_dirs(path: Path, root: Path) -> None:
    """Remove the directories of ``path`` below ``root`` that are left empty."""
    for parent in path.parents:
        if parent == root or not parent.is_relative_to(root):
            return
        try:
            parent.rmdir()
        except OSError:  # not empty
            return


def relocate(root: Path) -> dict[str, str]:
    """
    Move the manifest and task files of the course in the directory ``root``, which
    passed ``check_course``, into ``root / PYICS_DIR``, keeping their places relative
    to each other, so that ``root`` holds the student's files only. The paths the task
    files refer to (see ``_map_paths``) are rewritten to point to the same files as
    before.

    Nothing is moved if they are in ``PYICS_DIR`` already, or if a file there is in the
    way.

    Returns
    -------
    dict[str, str]
        The files that were moved, relative to ``root``, mapped to where they are now.
    """
    root = paths.absolute(root)
    target_dir = root / PYICS_DIR
    manifest_path = paths.absolute(find_manifest(root))
    if manifest_path.is_relative_to(target_dir):
        return {}
    manifest = json.load_plain(manifest_path)
    task_paths = {
        paths.absolute(paths.resolve(rel, manifest_path.parent))
        for rel in manifest["tasks"].values()
    }
    moves = {
        path: target_dir / path.relative_to(root)
        for path in [manifest_path, *sorted(task_paths)]
    }
    if any(target.exists() for target in moves.values()):
        return {}

    def rebase(old_dir: Path, new_dir: Path) -> Callable[[PurePath | str], Any]:
        def fn(path: PurePath | str) -> PurePath | str:
            if PurePath(path).is_absolute():
                return path
            target = paths.absolute(paths.resolve(path, old_dir))
            new = paths.relative_to(moves.get(target, target), new_dir)
            return new if isinstance(path, PurePath) else new.as_posix()

        return fn

    for old, new in moves.items():
        new.parent.mkdir(parents=True, exist_ok=True)
        if old in task_paths:
            data = _map_paths(json.load_plain(old), rebase(old.parent, new.parent))
            new.write_text(json.dumps(data, indent=4), encoding="utf-8")
            old.unlink()
        else:
            os.replace(old, new)
    for old in moves:
        remove_empty_dirs(old, root)
    return {
        old.relative_to(root).as_posix(): new.relative_to(root).as_posix()
        for old, new in moves.items()
    }
