"""
Sections of notebooks (``pyics.notebook.sections``): finding them, the tests that are
about one (inherited from their task, or their own), tasks that store theirs, several
tasks sharing one notebook, the TUI's title showing a task's section, and choosing
sections when creating and editing tasks.
"""

import copy
import re
from pathlib import Path
from typing import Any

import pytest
from notebook_helpers import code, make_notebook, markdown, write_notebook

from pyics.notebook import loader
from pyics.notebook.sections import (
    SectionNotFound,
    check_name,
    find_sections,
    get_section,
    is_name,
)

UP, DOWN, ENTER, CTRL_C, CTRL_D = "UP", "DOWN", "\r", "\x03", "\x04"
TIMEOUT = 10  # seconds
# OSC 8 hyperlinks as rich writes them: ESC ] 8 ; id=<id> ; <uri> ESC \
OSC8 = re.compile(r"\x1b\]8;[^;]*;([^\x1b]*)\x1b\\")

LOOP = "def total(xs):\n    s = 0\n    for x in xs:\n        s += x\n    return s"

# a lecture notebook of three exercises, each a section
LECTURE = [
    "import math",  # cell 1, in no section
    markdown("# Exercise 1: Squares", tags=["pyics:ex1"], id="ex1-anchor"),  # 2
    "def square(x):\n    return x * x",  # 3
    "print(square(3))",  # 4
    markdown("<!-- pyics: ex2 -->\n## Exercise 2: Sums", id="ex2-anchor"),  # 5
    LOOP,  # 6
    "print(total([square(1), 2]))",  # 7, uses ex1's code
    code("# pyics: ex3\nprint(math.pi > 3)", id="ex3-anchor"),  # 8
]
NO_LOOPS = {"concept": "loop", "max": 0}


@pytest.fixture
def chdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The working directory, where pytest runs started by a test write their cache."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def lecture(chdir: Path) -> Path:
    """The ``LECTURE`` notebook, in the working directory."""
    return write_notebook(chdir / "lecture.ipynb", LECTURE)


def _lecture() -> dict[str, Any]:
    """The ``LECTURE`` notebook, to change."""
    return make_notebook(copy.deepcopy(LECTURE))


def _numbers(cells: list[Any]) -> list[int]:
    return [cell.number for cell in cells]


# --- finding sections ---


def test_sections_start_at_tagged_and_marked_cells() -> None:
    sections = find_sections(_lecture())
    assert list(sections) == ["ex1", "ex2", "ex3"]
    ex1, ex2, ex3 = sections.values()
    assert (ex1.title, ex1.anchor.id, _numbers(ex1.cells)) == (
        "Exercise 1: Squares",
        "ex1-anchor",
        [2, 3, 4],
    )
    assert (ex2.title, ex2.anchor.id, _numbers(ex2.cells)) == (
        "Exercise 2: Sums",
        "ex2-anchor",
        [5, 6, 7],
    )
    # a code cell has no heading
    assert (ex3.title, ex3.anchor.id, _numbers(ex3.cells)) == ("ex3", "ex3-anchor", [8])
    assert _numbers(ex2.code_cells) == [6, 7]


@pytest.mark.parametrize(
    ("source", "title"),
    [
        ("# Exercise 1", "Exercise 1"),
        (
            "<!-- pyics: s -->\nSome text.\n\n## Exercise 2: *BFS* ##",
            "Exercise 2: *BFS*",
        ),
        ("Exercise 3\n==========\n\nText", "Exercise 3"),
        ("```python\n# not a heading\n```\n### Exercise 4", "Exercise 4"),
        ("No heading, only text.", "s"),
        ("#hashtag", "s"),
    ],
)
def test_the_title_is_the_first_heading_of_the_anchor(source: str, title: str) -> None:
    sections = find_sections(make_notebook([markdown(source, tags=["pyics:s"])]))
    assert sections["s"].title == title


def test_what_doesnt_start_a_section() -> None:
    nb = make_notebook(
        [
            "x = 1\n# pyics: late",  # not on the first line
            "# pyics: two words",  # not a name
            markdown("# pyics: heading"),  # a heading in a markdown cell
            "<!-- pyics: html -->",  # a markdown marker in a code cell
            code("y = 2", tags=["pyics:", "pyics:a b", "other:x", "PYICS:upper"]),
        ]
    )
    assert find_sections(nb) == {}


def test_the_first_cell_of_a_name_starts_its_section() -> None:
    nb = make_notebook(
        [
            markdown("# Exercise 1", tags=["pyics:ex1"]),
            "a = 1",
            # e.g. the anchor copied by a student: an ordinary cell of the section
            markdown("<!-- pyics: ex1 -->\n# Exercise 1 (copy)"),
            "b = 2",
            markdown("# Exercise 2", tags=["pyics:ex2"]),
            "c = 3",
        ]
    )
    sections = find_sections(nb)
    assert (sections["ex1"].title, _numbers(sections["ex1"].cells)) == (
        "Exercise 1",
        [1, 2, 3, 4],
    )
    assert _numbers(sections["ex2"].cells) == [5, 6]


def test_a_cell_can_start_several_sections() -> None:
    nb = make_notebook(
        [
            "setup = 1",
            markdown("<!-- pyics: c -->\n# Both", tags=["pyics:a", "pyics:b"]),
            "x = 1",
        ]
    )
    sections = find_sections(nb)
    assert list(sections) == ["a", "b", "c"]
    assert all(_numbers(s.cells) == [2, 3] for s in sections.values())


def test_a_missing_section_is_explained() -> None:
    with pytest.raises(SectionNotFound) as excinfo:
        get_section(_lecture(), "ex4")
    assert excinfo.value.name == "ex4"
    assert str(excinfo.value) == (
        "Section 'ex4' not found: the cell that starts it (tagged 'pyics:ex4', or "
        "starting with the marker '# pyics: ex4') was removed or its tag changed."
    )


def test_section_names() -> None:
    assert all(map(is_name, ["ex2", "exercise_2", "2.1", "bfs-dfs", "Übung"]))
    assert not any(map(is_name, ["", "ex 2", "ex:2", "a/b", None, 2]))
    assert check_name("ex2") == "ex2"
    with pytest.raises(ValueError, match="letters, digits"):
        check_name("ex 2")


# --- the code and output of a section ---


def test_cells_added_inside_a_section_are_included(lecture: Path) -> None:
    assert _numbers(loader.python_source(lecture, section="ex2").cells) == [6, 7]

    nb = _lecture()
    # a student's cells, in ex2 and above its anchor (in ex1)
    nb["cells"][6:6] = [code("helper = 1", id="new-1"), code("x = helper", id="new-2")]
    nb["cells"][4:4] = [code("above = 1", id="new-0")]
    write_notebook(lecture, nb)

    source = loader.python_source(lecture, section="ex2")
    assert _numbers(source.cells) == [7, 8, 9, 10]
    assert source.source.splitlines()[5:7] == ["helper = 1", "x = helper"]
    assert source.locate(7) == "cell 9, line 1"
    assert _numbers(loader.python_source(lecture, section="ex1").cells) == [3, 4, 5]
