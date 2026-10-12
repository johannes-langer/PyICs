"""
Helpers of the tests of pyics-jupyter: notebooks as the plain data Jupyter writes, tasks
and courses of them, and a clock that only moves when a test says so. Imported as
``from live_helpers import ...`` (pytest puts this directory on ``sys.path``).
"""

import io
import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from rich.console import Console, RenderableType

from pyics.data import Course, Task
from pyics.tests.tests import SimpleTest, Test
from pyics_jupyter import channel

ADD = "def add(a, b):\n    return a + b\n"
SUBTRACT = "def add(a, b):\n    return a - b\n"
# a markdown cell, followed by an unfinished exercise and a cell that prints
TEMPLATE: list[str | dict[str, Any]] = [
    {"cell_type": "markdown", "source": "# Adding"},
    "def add(a, b):\n    return 0\n",
    'print("Adding")',
]
SOLUTION = [TEMPLATE[0], ADD, TEMPLATE[2]]


def write_notebook(path: Path, cells: Sequence[str | dict[str, Any]]) -> Path:
    """Write a notebook (nbformat 4.5) of ``cells`` to ``path``: code cells' sources, or
    cells as dicts. Cells are given the ids ``cell-1``, ``cell-2``, ... by position."""
    built = []
    for number, cell in enumerate(cells, 1):
        cell = {"cell_type": "code", "source": cell} if isinstance(cell, str) else cell
        if cell["cell_type"] == "code":
            cell = {"outputs": [], "execution_count": None, **cell}
        built.append({"id": f"cell-{number}", "metadata": {}, **cell})
    nb = {"cells": built, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(nb, indent=1), encoding="utf-8")
    return path


def adds(notebook: Path) -> list[Test]:
    return [SimpleTest("adds", notebook, "add", 3, function_args=[1, 2])]


def write_task(
    root: Path,
    tests: Callable[[Path], list[Test]] = adds,
    cells: Sequence[str | dict[str, Any]] = TEMPLATE,
    *,
    course: bool = True,
) -> tuple[Path, Path]:
    """Write a task whose exercise file is a notebook of ``cells``, ``add.ipynb`` in
    ``root``, with the ``tests`` of that notebook, and (with ``course``) a course of it
    in ``root``. Returns the notebook's and the task file's paths."""
    notebook = write_notebook(root / "add.ipynb", cells)
    solution = write_notebook(root / ".solutions/add.ipynb", SOLUTION)
    task = Task.create_task_json(
        root / ".tasks/add",
        "Add",
        "Implement add.",
        notebook,
        solution,
        tests(notebook),
    )
    if course:
        Course.create_course_manifest(root / "course", [task], name="Adding")
    return notebook, task


def calls(notebook: Path, task: Path) -> dict[str, Any]:
    """The results of the calls of ``task``'s tests in the latest live run of the
    notebook's only kernel, by test name."""
    directory = channel.channel_dir(notebook)
    (kernel,) = channel.read_kernels(directory, now=time.time())
    results = kernel.tasks[channel.same_file(task)]
    return {r.nodeid.removeprefix("::"): r for r in results if r.when == "call"}


def text(renderable: RenderableType) -> str:
    """``renderable`` as plain text, e.g. a task's status."""
    console = Console(width=300, file=io.StringIO(), record=True)
    console.print(renderable)
    return console.export_text()


class FakeClock:
    """Time that only passes when a test says so. It starts at the real time, so that it
    agrees with the times of files it doesn't set."""

    def __init__(self) -> None:
        self.now = time.time()
        self.elapsed = 0.0

    def __call__(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.elapsed += seconds
