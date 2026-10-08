from os import PathLike
from pathlib import Path
from typing import Any

from pyics.distribution import archive, dependencies
from pyics.distribution.archive import ArchiveError
from pyics.distribution.slugs import check_slug
from pyics.distribution.sources import SourceError, build_index
from pyics.scripts.console import confirm, fail
from pyics.util import paths


def _text(value: Any) -> str:
    """A field of a manifest as text, e.g. a list as its items."""
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


def _declare_requirements(
    course: archive.CourseFiles, requires: list[str], *, yes: bool
) -> list[str] | None:
    """
    Find the packages ``course`` uses that its ``requires`` don't declare, and offer to
    add them (or add them if ``yes``).

    Returns
    -------
    list[str] | None
        Its requires with them added, or None if there are none, or they aren't added.
    """
    base = course.manifest_path.parent
    tasks = {
        paths.relative_to(path, base).as_posix(): data
        for path, data in course.tasks.items()
    }
    used = dependencies.used_modules(tasks, archive.local_modules(course))
    missing = dependencies.undeclared(used, requires)
    if not missing:
        return None
    print("The course uses packages that its requires don't declare:")
    for distribution, files in sorted(missing.items()):
        installed = dependencies.installed_version(distribution)
        note = "" if installed else ", not installed here"
        print(f"  {distribution} (in {', '.join(sorted(files))}{note})")
    if yes or confirm("Add them to its requires?", default=True):
        return [*requires, *sorted(missing)]
    print("Students will have to find out themselves which packages to install.")
    return None


def _pack(
    manifest: PathLike,
    out: PathLike | None = None,
    version: str | None = None,
    description: str | None = None,
    slug: str | None = None,
    tags: list[str] | None = None,
    include: list[str] | None = None,
    no_solutions: bool = False,
    yes: bool = False,
) -> None:
    """
    Console script to pack a course into an archive, which can be pulled by its path or
    URL, or published in an index (see ``build_index``). The archive holds the course
    manifest and its task files, but not the progress made on the course, nor the
    exercise and solution files, which are created when the course is started.

    The given version, description, slug and tags are stored in the manifest, so the
    next pack reuses them. So are the packages the course uses that its requires don't
    declare yet, once confirmed.

    Parameters
    ----------
    manifest: PathLike
        The course manifest (``.pyicscourse.json``).
    out: PathLike | None
        Where to write the archive, or the directory to write it to. By default,
        ``./<slug>-<version>.zip``.
    version: str | None
        The course's version, e.g. ``1.2.0``. Give each version you publish a new one,
        so that pulled courses can be updated.
    description: str | None
        A short description, shown when searching for courses.
    slug: str | None
        The name the course is pulled by, e.g. ``search-algorithms``. By default,
        derived from its name when it is first packed.
    tags: list[str] | None
        Tags to find the course by.
    include: list[str] | None
        Other files the course needs, e.g. data its exercises read, or directories of
        them.
    no_solutions: bool
        Leave the solutions out, e.g. for a course that is graded.
    yes: bool
        Don't ask for confirmation.
    """
    try:
        course = archive.read_course(Path(manifest))
        info = course.info
        changes: dict[str, Any] = {}
        if version is not None:
            changes["version"] = version
        if description is not None:
            changes["description"] = description
        if tags is not None:
            changes["tags"] = tags
        if slug is not None:
            changes["slug"] = check_slug(slug, "course slug")
        elif not course.manifest.get("slug"):
            # fixed once the course is published, so that renaming it doesn't change
            # the name it is pulled and updated by
            changes["slug"] = info.slug
        if not course.manifest.get("requires_pyics"):
            changes["requires_pyics"] = f">={dependencies.pyics_version()}"
        requires = _declare_requirements(course, info.requires, yes=yes)
        if requires is not None:
            changes["requires"] = requires

        if changes:
            archive.update_manifest(course.manifest_path, changes)
            course = archive.read_course(course.manifest_path)
            info = course.info
            print(
                f"Updated {course.manifest_path.name}: "
                + ", ".join(f"{key} {_text(value)}" for key, value in changes.items())
            )

        name = f"{info.slug}-{info.version}.zip" if info.version else f"{info.slug}.zip"
        out = Path(out) if out is not None else Path(name)
        if out.is_dir():
            out = out / name
        files = archive.pack(
            course,
            out,
            include=[Path(i) for i in include or []],
            no_solutions=no_solutions,
        )
    except (ArchiveError, ValueError, OSError) as err:
        fail(err)

    version_text = f" {info.version}" if info.version else ""
    print(f"Packed {info.title}{version_text} into {out} ({len(files)} files).")
    if not info.version:
        print("It has no version: give it one with --version, so it can be updated.")
    print("Publish it in an index with: pyics build_index <directory of archives>")


def _build_index(
    directory: PathLike = Path("."),
    out: PathLike | None = None,
    name: str | None = None,
) -> None:
    """
    Console script to write the index of a course library (a source): of the course
    archives (see ``pack``) in a directory and its subdirectories, the newest version
    of each course. Upload the directory to a web server, and add the index's URL
    with ``pyics add_source``.

    Parameters
    ----------
    directory: PathLike
        The directory of the archives. The working directory by default.
    out: PathLike | None
        Where to write the index. By default, ``index.json`` in ``directory``.
    name: str | None
        The library's name, e.g. "Uni Bamberg AI courses". By default, the name of the
        index it replaces, or else of ``directory``.
    """
    try:
        fp, index = build_index(
            Path(directory), Path(out) if out is not None else None, name=name
        )
    except (SourceError, OSError) as err:
        fail(err)
    courses = index["courses"]
    listed = ", ".join(
        f"{slug} {entry['version']}".rstrip() for slug, entry in courses.items()
    )
    count = f"{len(courses)} course" + ("" if len(courses) == 1 else "s")
    print(f"Wrote the index {index['name']!r} of {count} to {fp}.")
    if listed:
        print(f"Courses: {listed}")
    print(
        "Upload the directory to a web server, and add the index's URL with: "
        "pyics add_source <url>"
    )
