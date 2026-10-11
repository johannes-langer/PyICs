"""
Notebooks for tests, as the plain data Jupyter writes to ``.ipynb`` files (nbformat
4.5), built from short cell sources::

    nb = make_notebook(["import math", markdown("# Exercise 1"), "def f(): ..."])
    path = write_notebook(tmp_path / "ex.ipynb", nb)

Imported by test modules as ``from notebook_helpers import ...``: pytest puts this
directory on ``sys.path``, and doesn't collect this module, as its name doesn't end with
``_test``.
"""

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

type CellData = dict[str, Any]


def _cell(
    cell_type: str,
    source: str,
    tags: Sequence[str],
    cell_id: str | None,
    metadata: dict[str, Any] | None,
) -> CellData:
    cell: CellData = {
        "cell_type": cell_type,
        "metadata": {**(metadata or {}), **({"tags": list(tags)} if tags else {})},
        # stored as lines, like Jupyter stores it
        "source": source.splitlines(keepends=True),
    }
    if cell_id is not None:
        cell["id"] = cell_id
    return cell


def code(
    source: str,
    *,
    tags: Sequence[str] = (),
    id: str | None = None,
    outputs: list[dict[str, Any]] | None = None,
    execution_count: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> CellData:
    """A code cell of ``source``, with ``tags`` (its ``metadata.tags``), and the
    ``outputs`` and ``execution_count`` of a run, if given."""
    cell = _cell("code", source, tags, id, metadata)
    cell["outputs"] = list(outputs or [])
    cell["execution_count"] = execution_count
    return cell


def markdown(
    source: str,
    *,
    tags: Sequence[str] = (),
    id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CellData:
    """A markdown cell of ``source``, with ``tags`` (its ``metadata.tags``)."""
    return _cell("markdown", source, tags, id, metadata)


def stream(text: str) -> dict[str, Any]:
    """The output of a cell that printed ``text``, e.g. for ``code(outputs=...)``."""
    return {"name": "stdout", "output_type": "stream", "text": [text]}


def make_notebook(
    cells: Iterable[str | CellData],
    *,
    nbformat: int = 4,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    A notebook of ``cells``: each one a code cell's source, or a cell made with
    ``code`` or ``markdown``. Cells get ids ``cell-1``, ``cell-2``, ... by their
    position, unless they have one.
    """
    built = []
    for number, cell in enumerate(cells, 1):
        cell = code(cell) if isinstance(cell, str) else dict(cell)
        cell.setdefault("id", f"cell-{number}")
        built.append(cell)
    return {
        "cells": built,
        "metadata": metadata or {},
        "nbformat": nbformat,
        "nbformat_minor": 5,
    }


def notebook_text(nb: dict[str, Any] | Iterable[str | CellData]) -> str:
    """The notebook ``nb`` (or a notebook of these cells, see ``make_notebook``) as
    the text of an ``.ipynb`` file, written like Jupyter writes it."""
    if not isinstance(nb, dict):
        nb = make_notebook(nb)
    return json.dumps(nb, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def write_notebook(path: Path, nb: dict[str, Any] | Iterable[str | CellData]) -> Path:
    """Write the notebook ``nb`` (or a notebook of these cells, see
    ``make_notebook``) to ``path``, and return ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(notebook_text(nb), encoding="utf-8")
    return path
