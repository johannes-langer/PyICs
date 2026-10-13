"""
Sections of a notebook, which link a task to the part of a notebook it is about, e.g.
when several tasks share one (lecture-style) notebook.

A section starts at its **anchor** cell, and runs up to just before the next section's
anchor (or to the end of the notebook). A cell is the anchor of the section ``<name>``

- if it is tagged ``pyics:<name>`` (in its ``metadata.tags``), or
- if its first line is the marker ``# pyics: <name>`` (a code cell) or
  ``<!-- pyics: <name> -->`` (a markdown cell).

Tags are Jupyter's own way to mark cells, but are hidden in most frontends. Markers are
visible everywhere, and students can restore one they deleted. Authors usually anchor a
section on the exercise's description (a markdown cell), which students have no reason
to change: the cells they add below it belong to the section.

Section names consist of letters, digits, ``_``, ``.`` and ``-`` (see ``NAME_PATTERN``),
e.g. ``ex2``. If several cells start a section of the same name (e.g. after an anchor
cell was copied), the first of them does, and the others are ordinary cells of the
section they are in. A cell can start several sections, which then have the same cells.
Cells above the first anchor belong to no section.
"""

import re
from dataclasses import dataclass
from typing import Any

from pyics.notebook import cells
from pyics.notebook.cells import Cell

# the prefix of the tags that start a section: ``pyics:<name>`` in ``metadata.tags``
TAG_PREFIX = "pyics:"
# what a section's name may consist of: letters, digits, "_", "." and "-"
NAME_PATTERN = r"[\w.-]+"

_NAME = re.compile(NAME_PATTERN)
# the marker on the first line of a code cell, and of a markdown cell
_CODE_MARKER = re.compile(rf"#\s*pyics:\s*({NAME_PATTERN})\s*")
_MARKDOWN_MARKER = re.compile(rf"<!--\s*pyics:\s*({NAME_PATTERN})\s*-->\s*")
# markdown headings (CommonMark): an ATX heading ("## Title", with an optional closing
# sequence of "#"), the underline of a setext heading ("Title" above "=====" or
# "-----"), and the fence of a code block, whose lines aren't headings
_ATX_HEADING = re.compile(r" {0,3}#{1,6}(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*")
_SETEXT_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)[ \t]*")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")


class SectionNotFound(LookupError):
    """A notebook has no section of a name, e.g. because the cell that started it was
    removed, or its tag changed."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(
            f"Section '{name}' not found: the cell that starts it (tagged "
            f"'{TAG_PREFIX}{name}', or starting with the marker '# pyics: {name}') was "
            "removed or its tag changed."
        )


@dataclass(frozen=True)
class Section:
    """A section of a notebook (see the module's docstring)."""

    name: str
    # the first markdown heading of its anchor cell (as written, without the "#"
    # markers), or its name if that cell has no heading
    title: str
    # the cell that starts it
    anchor: Cell
    # its cells, from its anchor to just before the next section's anchor, in order
    cells: list[Cell]

    @property
    def code_cells(self) -> list[Cell]:
        """Its code cells, in order."""
        return [cell for cell in self.cells if cell.cell_type == "code"]


def is_name(name: object) -> bool:
    """Whether ``name`` can be the name of a section (see ``NAME_PATTERN``)."""
    return isinstance(name, str) and _NAME.fullmatch(name) is not None


def check_name(name: object) -> str:
    """
    Return ``name`` if it can be the name of a section, raise otherwise.

    Raises
    ------
    ValueError
        If ``name`` isn't a string of letters, digits, "_", "." and "-".
    """
    if not is_name(name):
        raise ValueError(
            f"{name!r} can't be the name of a section: it may only consist of letters, "
            "digits, '_', '.' and '-'"
        )
    assert isinstance(name, str)
    return name


def anchor_names(cell: Cell) -> list[str]:
    """The names of the sections ``cell`` starts by its tags and its marker (see the
    module's docstring), in this order, whether or not an earlier cell starts them."""
    names = [
        tag.removeprefix(TAG_PREFIX)
        for tag in cell.tags
        if tag.startswith(TAG_PREFIX) and is_name(tag.removeprefix(TAG_PREFIX))
    ]
    first_line = cell.source.split("\n", 1)[0].strip()
    marker = {"code": _CODE_MARKER, "markdown": _MARKDOWN_MARKER}.get(cell.cell_type)
    if marker is not None and (match := marker.fullmatch(first_line)):
        names.append(match[1])
    return list(dict.fromkeys(names))


def heading(cell: Cell) -> str | None:
    """The text of the first heading of the markdown cell ``cell``, as written but
    without the "#" markers, or None if it has none (or isn't a markdown cell)."""
    if cell.cell_type != "markdown":
        return None
    fence: str | None = None
    previous = ""
    for line in cell.source.splitlines():
        if (opening := _FENCE.match(line)) is not None:
            if fence is None:
                fence = opening[1][0] * 3
            elif opening[1].startswith(fence):
                fence = None
            previous = ""
            continue
        if fence is not None:
            continue
        if (atx := _ATX_HEADING.fullmatch(line)) is not None:
            if text := (atx[1] or "").strip():
                return text
        elif _SETEXT_UNDERLINE.fullmatch(line) and _paragraph(previous):
            return previous.strip()
        previous = line
    return None


def _paragraph(line: str) -> bool:
    """Whether ``line`` can be the text of a setext heading, rather than e.g. blank, a
    list item, or a marker."""
    text = line.strip()
    return bool(text) and not text.startswith(("#", "<", "-", "*", "+", ">", "|"))


def _find(all_cells: list[Cell]) -> dict[str, Section]:
    """The sections of a notebook of ``all_cells`` (see ``find_sections``)."""
    # the position of each anchor among all cells, and the sections it starts
    anchors: list[tuple[int, list[str]]] = []
    seen: set[str] = set()
    for position, cell in enumerate(all_cells):
        # a name taken by an earlier cell doesn't make this one an anchor
        if names := [name for name in anchor_names(cell) if name not in seen]:
            seen.update(names)
            anchors.append((position, names))

    found: dict[str, Section] = {}
    for i, (start, names) in enumerate(anchors):
        end = anchors[i + 1][0] if i + 1 < len(anchors) else len(all_cells)
        anchor = all_cells[start]
        title = heading(anchor)
        for name in names:
            found[name] = Section(name, title or name, anchor, all_cells[start:end])
    return found


def find_sections(nb: dict[str, Any]) -> dict[str, Section]:
    """The sections of the notebook ``nb`` (as read by ``cells.parse``), by name, in the
    order they start in."""
    return _find(cells.cells(nb))


def get_section(nb: dict[str, Any], name: str) -> Section:
    """
    The section ``name`` of the notebook ``nb`` (as read by ``cells.parse``).

    Raises
    ------
    SectionNotFound
        If the notebook has no section ``name``.
    """
    if (section := find_sections(nb).get(name)) is None:
        raise SectionNotFound(name)
    return section
