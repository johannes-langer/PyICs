"""
Exercise files import the modules next to them, as scripts and notebooks in Jupyter do.
"""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from notebook_helpers import code, write_notebook

from pyics.tests import Make, PrintTest, SimpleTest
from pyics.util.imports import prepare_imports

HELPER = """
class Graph:
    def __init__(self, edges):
        self.edges = edges

    def successors(self, node):
        return [b for a, b in self.edges if a == node]
"""

EXERCISE = """
from graph_helper import Graph

def degree(g, node):
    return len(g.successors(node))
"""


@pytest.fixture(autouse=True)
def restore_imports(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``sys.path`` and the imported modules as they were before the test."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        del sys.modules[name]


@pytest.fixture
def course(tmp_path: Path) -> Path:
    (tmp_path / "graph_helper.py").write_text(HELPER)
    return tmp_path


def test_a_file_imports_the_modules_next_to_it(course: Path) -> None:
    exercise = course / "exercise.py"
    exercise.write_text(EXERCISE)
    edges = [("A", "B"), ("A", "C")]
    SimpleTest(
        "t", exercise, "degree", 2, function_args=[Make("Graph", edges), "A"]
    ).run()


def test_a_notebook_imports_the_modules_next_to_it(course: Path) -> None:
    notebook = write_notebook(
        course / "exercise.ipynb",
        [code(EXERCISE), code("print(degree(Graph([('A', 'B')]), 'A'))")],
    )
    edges = [("A", "B"), ("A", "C")]
    SimpleTest(
        "t", notebook, "degree", 2, function_args=[Make("Graph", edges), "A"]
    ).run()
    PrintTest("t", notebook, "1").run()


def test_changed_modules_are_imported_anew(course: Path) -> None:
    exercise = course / "exercise.py"
    exercise.write_text(EXERCISE)
    test = SimpleTest(
        "t", exercise, "degree", 2, function_args=[Make("Graph", [("A", "B")]), "A"]
    )
    with pytest.raises(AssertionError):
        test.run()
    # the student fixes the module the exercise imports
    (course / "graph_helper.py").write_text(
        HELPER.replace("return [b", "return 2 * [b")
    )
    test.run()


def test_only_the_directorys_own_modules_are_forgotten(tmp_path: Path) -> None:
    (tmp_path / "src" / "own_pkg").mkdir(parents=True)
    (tmp_path / "src" / "own_pkg" / "__init__.py").write_text("")
    (tmp_path / "own_pkg").mkdir()  # the same name, but not the package
    (tmp_path / "local_mod.py").write_text("")
    sys.path[:0] = [str(tmp_path / "src"), str(tmp_path)]
    import local_mod  # noqa: F401
    import own_pkg  # noqa: F401

    prepare_imports(tmp_path / "exercise.py")
    assert sys.path[0] == str(tmp_path)
    assert sys.path.count(str(tmp_path)) == 1
    # a package of a directory below it, like pyics in its repository, is kept
    assert "own_pkg" in sys.modules
    assert "local_mod" not in sys.modules
