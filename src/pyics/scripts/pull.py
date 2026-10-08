from os import PathLike
from pathlib import Path

from pyics.distribution.archive import ArchiveError
from pyics.distribution.fetch import is_url
from pyics.distribution.pull import Pulled, PullError, is_direct, pull, update
from pyics.scripts.console import (
    TRUST_WARNING,
    confirm,
    fail,
    install_requirements,
    report_problems,
)
from pyics.util import paths

# how many directories below the working directory the course selector looks
_SELECTOR_DEPTH = 2


def _where_to_start(pulled: Pulled) -> str:
    """How to start the course ``pulled`` from the working directory."""
    try:
        rel = pulled.manifest_path.relative_to(paths.absolute(Path.cwd()))
    except ValueError:
        rel = None
    if rel is not None and len(rel.parent.parts) <= _SELECTOR_DEPTH:
        return "Start it with: pyics"
    return f"Start it by running pyics in {pulled.directory}"


def _finish(pulled: Pulled, *, install_deps: bool, yes: bool) -> None:
    """Report the course's unmet requirements, installing them if asked to."""
    problems = pulled.problems
    if problems and install_deps:
        problems = install_requirements(problems, yes=yes)
    report_problems(pulled.info.title, problems)


def _pull(
    course: str,
    dest: PathLike | None = None,
    install_deps: bool = False,
    allow_http: bool = False,
    yes: bool = False,
) -> None:
    """
    Console script to pull a course into a directory of its own, ``./<name>`` by
    default: a course from the course libraries (see ``add_source``) by its name, or
    ``<source>/<name>`` if several have one of that name, or a course archive by its
    URL or path.

    Parameters
    ----------
    course: str
        The course.
    dest: PathLike | None
        The directory to pull it into, which must be new or empty.
    install_deps: bool
        Install the packages the course needs that aren't installed (after asking).
    allow_http: bool
        Allow downloading a course archive by URL over plain http, which isn't secure.
    yes: bool
        Don't ask for confirmation.
    """
    if is_direct(course) and is_url(course):
        print(TRUST_WARNING)
        if not yes and not confirm(f"Pull the course at {course}?"):
            print("Cancelled, no course was pulled.")
            return
    try:
        pulled = pull(course, Path(dest) if dest else None, allow_http=allow_http)
    except (PullError, ArchiveError, OSError) as err:
        fail(err)
    for warning in pulled.warnings:
        print(f"warning: {warning}")
    version = f" {pulled.info.version}" if pulled.info.version else ""
    print(f"Pulled {pulled.info.title}{version} into {pulled.directory}.")
    _finish(pulled, install_deps=install_deps, yes=yes)
    print(_where_to_start(pulled))


def _update(
    directory: PathLike = Path("."),
    install_deps: bool = False,
    allow_http: bool = False,
    force: bool = False,
    yes: bool = False,
) -> None:
    """
    Console script to update a pulled course to its latest version. Your exercise files
    are kept as they are, and so is your progress.

    Parameters
    ----------
    directory: PathLike
        The course's directory, or a directory in it. The working directory by default.
    install_deps: bool
        Install the packages the course needs that aren't installed (after asking).
    allow_http: bool
        Allow downloading a course pulled by URL over plain http, which isn't secure.
    force: bool
        Update the course even if it is up to date, e.g. to restore its files.
    yes: bool
        Don't ask for confirmation.
    """
    try:
        updated = update(Path(directory), allow_http=allow_http, force=force)
    except (PullError, ArchiveError, OSError) as err:
        fail(err)
    title = updated.info.title
    if updated.up_to_date:
        version = f" ({updated.info.version})" if updated.info.version else ""
        print(f"{title} is up to date{version}.")
    else:
        old, new = updated.old_version, updated.info.version
        versions = f" from {old} to {new}" if old and new and old != new else ""
        print(f"Updated {title}{versions}. Your exercise files were kept.")
        if updated.kept:
            print(
                "These files of the new version weren't written, to keep yours: "
                + ", ".join(updated.kept)
            )
    _finish(updated, install_deps=install_deps, yes=yes)
