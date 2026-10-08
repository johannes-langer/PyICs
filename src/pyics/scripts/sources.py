from pyics.distribution import dependencies, sources
from pyics.distribution.sources import SourceError
from pyics.scripts.console import TRUST_WARNING, confirm, fail


def _add_source(
    url: str, name: str | None = None, allow_http: bool = False, yes: bool = False
) -> None:
    """
    Console script to add a course library (a source) by the URL of its index, so that
    its courses can be pulled by name (see ``pull``).

    Parameters
    ----------
    url: str
        The URL of the index, or its path, e.g. on a network share.
    name: str | None
        The name to know the source by. By default, the index's name.
    allow_http: bool
        Allow fetching the index and the courses over plain http, which isn't secure.
    yes: bool
        Don't ask for confirmation.
    """
    try:
        source, index, data = sources.prepare_source(url, name, allow_http=allow_http)
    except SourceError as err:
        fail(err)
    count = f"{len(index.courses)} course" + ("" if len(index.courses) == 1 else "s")
    courses = f": {', '.join(index.courses)}" if index.courses else ""
    print(f"{index.name or source.url} lists {count}{courses}")
    print(TRUST_WARNING)
    if not yes and not confirm(f"Add it as {source.name!r}?"):
        print("Cancelled, no source was added.")
        return
    try:
        sources.save_source(source, data)
    except (SourceError, OSError) as err:
        fail(err)
    print(f"Added the source {source.name!r}. Find its courses with: pyics search")


def _remove_source(name: str) -> None:
    """
    Console script to remove a course library (see ``add_source``). Courses pulled from
    it stay where they are.

    Parameters
    ----------
    name: str
        The name of the source.
    """
    try:
        sources.remove_source(name)
    except (SourceError, OSError) as err:
        fail(err)
    print(f"Removed the source {name!r}.")


def _sources() -> None:
    """Console script to list the course libraries (see ``add_source``)."""
    try:
        found = sources.load_sources()
    except SourceError as err:
        fail(err)
    if not found:
        print("You haven't added any sources. Add one with: pyics add_source <url>")
        return
    for source in found:
        line = f"{source.name}  {source.url}"
        if source.allow_http:
            line += "  (plain http allowed)"
        if source.site is not None:
            line += f"  (for all users, in {source.site})"
        print(line)


def _search(term: str | None = None, offline: bool = False) -> None:
    """
    Console script to list the courses in the course libraries (see ``add_source``)
    that match ``term`` in their name, title, description or tags, or all of them.

    Parameters
    ----------
    term: str | None
        What to look for.
    offline: bool
        Only look in the indexes fetched last, without fetching them anew.
    """
    try:
        if not sources.load_sources():
            print("You haven't added any sources. Add one with: pyics add_source <url>")
            return
        courses, warnings = sources.search(term, refresh=not offline)
    except SourceError as err:
        fail(err)
    for warning in warnings:
        print(f"warning: {warning}")
    if not courses:
        print("No course found." if term is None else f"No course matches {term!r}.")
        return

    slugs = [entry.slug for _, entry in courses]
    for source, entry in courses:
        # the source is needed to pull a course whose slug several sources have
        name = (
            entry.slug
            if slugs.count(entry.slug) == 1
            else f"{source.name}/{entry.slug}"
        )
        version = f" {entry.version}" if entry.version else ""
        print(f"{name}{version}: {entry.title} (from {source.name})")
        if entry.description:
            print(f"    {entry.description}")
        if entry.tags:
            print(f"    tags: {', '.join(entry.tags)}")
        if problems := dependencies.check(entry.requires, entry.requires_pyics):
            print(f"    needs {dependencies.describe(problems)}")
    print("Pull a course with: pyics pull <name>")
