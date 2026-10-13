"""
Jupyter notebooks (``.ipynb`` files), read as plain JSON (without ``nbformat``): their
cells, and their contents normalized for storing them in task files.

Only nbformat 4 is supported, which Jupyter has written since 2015. Older notebooks are
converted by opening and saving them in Jupyter.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyics.util.paths import StrPath

NOTEBOOK_SUFFIX = ".ipynb"

# metadata that running a notebook (or looking at its outputs) changes, and that the
# frontend fills in again: of the notebook, and of its cells. That includes the kernel
# its author chose (``kernelspec``), which names an environment on their machine
_RUN_METADATA = ("kernelspec", "language_info", "widgets")
_RUN_CELL_METADATA = ("execution", "ExecuteTime", "collapsed", "scrolled")


class NotebookError(ValueError):
    """A file can't be read as a notebook: it isn't valid JSON, isn't a notebook, or is
    in an nbformat older than 4."""


def is_notebook(path: StrPath) -> bool:
    """Whether the file at ``path`` is a notebook, by its suffix (in any case)."""
    return Path(path).suffix.lower() == NOTEBOOK_SUFFIX


@dataclass(frozen=True)
class Cell:
    """A cell of a notebook."""

    # its position among all cells of the notebook, from 0
    index: int
    # "code", "markdown" or "raw"
    cell_type: str
    # its id (nbformat 4.5 and later). Frontends keep it while the cell exists, but it
    # changes e.g. when the cell is copied or split.
    id: str | None
    # its ``metadata.tags``
    tags: tuple[str, ...]
    source: str

    @property
    def number(self) -> int:
        """Its position among all cells, from 1, as messages show it ("cell 3")."""
        return self.index + 1


def parse(text: str, *, name: str | None = None) -> dict[str, Any]:
    """
    The notebook ``text`` as plain data. ``name``, e.g. the notebook's file name, is
    used in messages.

    Raises
    ------
    NotebookError
        If ``text`` isn't a notebook in nbformat 4.
    """
    what = name or "the notebook"
    try:
        nb = json.loads(text)
    except json.JSONDecodeError as err:
        raise NotebookError(
            f"Can't read {what}: it isn't valid JSON ({err})."
        ) from None
    version = nb.get("nbformat") if isinstance(nb, dict) else None
    if not isinstance(version, int) or isinstance(version, bool):
        raise NotebookError(f"Can't read {what}: it isn't a Jupyter notebook.")
    if version < 4:
        raise NotebookError(
            f"Can't read {what}: nbformat {version} isn't supported. Open and save it "
            "in Jupyter to convert it."
        )
    cells = nb.get("cells")
    if not isinstance(cells, list) or not all(isinstance(c, dict) for c in cells):
        raise NotebookError(f"Can't read {what}: its cells aren't a list of cells.")
    return nb


def read(path: StrPath) -> dict[str, Any]:
    """
    The notebook at ``path`` as plain data (see ``parse``).

    Raises
    ------
    NotebookError
        If the file isn't a notebook in nbformat 4.
    OSError
        If the file can't be read.
    """
    return parse(Path(path).read_text(encoding="utf-8"), name=Path(path).name)


def _source(cell: dict[str, Any]) -> str:
    """The source of ``cell``, which nbformat stores as a string or a list of lines."""
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def cells(nb: dict[str, Any]) -> list[Cell]:
    """All cells of the notebook ``nb`` (see ``parse``), in order."""
    result = []
    for index, cell in enumerate(nb["cells"]):
        metadata = cell.get("metadata")
        tags = metadata.get("tags") if isinstance(metadata, dict) else None
        cell_id = cell.get("id")
        result.append(
            Cell(
                index=index,
                cell_type=str(cell.get("cell_type", "")),
                id=cell_id if isinstance(cell_id, str) else None,
                tags=tuple(t for t in tags or () if isinstance(t, str)),
                source=_source(cell),
            )
        )
    return result


def code_cells(nb: dict[str, Any]) -> list[Cell]:
    """The code cells of the notebook ``nb`` (see ``parse``), in order."""
    return [cell for cell in cells(nb) if cell.cell_type == "code"]


def normalize(text: str, *, name: str | None = None) -> str:
    """
    The notebook ``text`` as task files store it: without what running it changes, i.e.
    its outputs, execution counts and the metadata of the run (e.g. execution times),
    and without its kernel (``kernelspec``), so that students choose their own. Sources are stored as lists of lines, and the JSON is written like Jupyter writes
    it, so that a notebook that was merely run, or saved by another frontend, is stored
    the same.

    ``name``, e.g. the notebook's file name, is used in messages.

    Raises
    ------
    NotebookError
        If ``text`` isn't a notebook in nbformat 4.
    """
    nb = parse(text, name=name)
    for cell in nb["cells"]:
        if cell.get("cell_type") == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
        if isinstance(cell.get("source"), str):
            cell["source"] = cell["source"].splitlines(keepends=True)
        if isinstance(metadata := cell.get("metadata"), dict):
            for key in _RUN_CELL_METADATA:
                metadata.pop(key, None)

    if not isinstance(metadata := nb.get("metadata"), dict):
        metadata = nb["metadata"] = {}
    for key in _RUN_METADATA:
        metadata.pop(key, None)
    return json.dumps(nb, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def as_cell_text(text: str) -> str:
    """
    The cells of the notebook ``text`` as text, e.g. to show how two notebooks differ:
    a ``# %% cell N`` block of each code cell's source, and a ``# %% [markdown] cell N``
    block of each markdown (or raw) cell's lines, commented out. Outputs and metadata
    are left out.

    Raises
    ------
    NotebookError
        If ``text`` isn't a notebook in nbformat 4.
    """
    blocks = []
    for cell in cells(parse(text)):
        if cell.cell_type == "code":
            header, body = f"# %% cell {cell.number}", cell.source
        else:
            header = f"# %% [{cell.cell_type}] cell {cell.number}"
            body = "\n".join(f"# {line}".rstrip() for line in cell.source.splitlines())
        blocks.append(f"{header}\n{body}".rstrip("\n") + "\n")
    return "\n".join(blocks)
