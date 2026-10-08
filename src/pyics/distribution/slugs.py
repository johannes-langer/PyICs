"""
Slugs: the names courses are pulled by and sources are known by, e.g.
``search-algorithms``.
"""

import re

SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def slugify(text: str) -> str:
    """``text`` as a slug, e.g. ``search-algorithms`` for "Search Algorithms". Empty if
    ``text`` has no letters or digits."""
    return re.sub(r"[^a-z0-9._]+", "-", text.lower()).strip("-._")


def check_slug(slug: str, what: str = "slug") -> str:
    """Return ``slug`` if it is a valid slug, raise ``ValueError`` otherwise."""
    if not isinstance(slug, str) or not SLUG.match(slug):
        raise ValueError(
            f"{slug!r} is not a valid {what}: use lowercase letters, digits, '.', '_' "
            "and '-', starting with a letter or digit"
        )
    return slug
