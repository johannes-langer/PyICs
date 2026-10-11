"""
Sections of notebooks (``pyics.notebook.sections``): finding them, the tests that are
about one (inherited from their task, or their own), tasks that store theirs, several
tasks sharing one notebook, the TUI's title showing a task's section, and choosing
sections when creating and editing tasks.
"""

import copy
import json as std_json
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from notebook_helpers import code, make_notebook, markdown, write_notebook

from pyics.data import Course, Task
from pyics.notebook import loader
from pyics.notebook.sections import (
    SectionNotFound,
    check_name,
    find_sections,
    get_section,
    is_name,
)
from pyics.tests.pytest_integration import LiveTestCollectionPlugin, RichFailureRepr
from pyics.tests.tests import (
    INHERIT,
    TASK_SECTIONS,
    PrintTest,
    SimpleTest,
    StructureTest,
    Test,
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


@pytest.fixture
def no_new_threads() -> Iterator[None]:
    """Waits for the threads a test started, e.g. a course's test runs."""
    threads = set(threading.enumerate())
    yield
    for thread in set(threading.enumerate()) - threads:
        thread.join(TIMEOUT)
    # the course's own pytest runs remove this when they finish, but pytest expects it
    # to be set until this test is torn down
    os.environ.setdefault("PYTEST_CURRENT_TEST", "")


def _lecture() -> dict[str, Any]:
    """The ``LECTURE`` notebook, to change."""
    return make_notebook(copy.deepcopy(LECTURE))


def _numbers(cells: list[Any]) -> list[int]:
    return [cell.number for cell in cells]


def _run(tests: list[Test], sections: dict[Path, str] | None = None) -> dict[str, str]:
    """Run ``tests`` like a ``Task`` with ``sections`` does, and return the message of
    each test by name ("" for those that pass)."""
    plugin = LiveTestCollectionPlugin(tests, sections=sections)
    pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])
    results = {}
    for report in plugin.reports:
        if report.when == "call":
            name = report.nodeid.removeprefix("::")
            if report.passed:
                results[name] = ""
            else:
                assert isinstance(report.longrepr, RichFailureRepr)
                results[name] = report.longrepr.text.plain
    return results


def _wait_for(condition: Callable[[], object]) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def _save(path: Path, nb: dict[str, Any] | list[Any]) -> None:
    """Save the notebook ``nb`` to ``path``, as Jupyter would."""
    mtime = path.stat().st_mtime + 2  # mtimes may only change every few ms
    write_notebook(path, nb)
    os.utime(path, (mtime, mtime))


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


def test_deleting_the_anchor_gives_the_missing_section_message(lecture: Path) -> None:
    nb = _lecture()
    del nb["cells"][4]  # ex2's anchor
    write_notebook(lecture, nb)
    with pytest.raises(SectionNotFound):
        loader.python_source(lecture, section="ex2")

    results = _run(
        [
            StructureTest("no loops", lecture, NO_LOOPS),
            PrintTest("prints 3", lecture, "3"),
        ],
        {lecture: "ex2"},
    )
    message = (
        "Section 'ex2' not found: the cell that starts it (tagged 'pyics:ex2', or "
        "starting with the marker '# pyics: ex2') was removed or its tag changed."
    )
    assert results == {
        "no loops": f"no loops failed: {message}",
        "prints 3": f"prints 3 failed: {message}",
    }


def test_structure_tests_check_their_section(lecture: Path) -> None:
    tests: list[Test] = [
        StructureTest("inherits", lecture, NO_LOOPS),
        StructureTest("whole notebook", lecture, NO_LOOPS, section=None),
        StructureTest("ex1", lecture, NO_LOOPS, section="ex1"),
        SimpleTest("square", lecture, "square", 4, function_args=[2]),
    ]
    # the loop is in ex2
    assert _run(tests, {lecture: "ex1"}) == {
        "inherits": "",
        "whole notebook": "whole notebook failed: Expected no loop, found 1 (cell 6, "
        "line 3).",
        "ex1": "",
        "square": "",
    }
    results = _run(tests, {lecture: "ex2"})
    assert results["inherits"] == (
        "inherits failed: Expected no loop, found 1 (cell 6, line 3)."
    )
    assert (results["ex1"], results["square"]) == ("", "")
    # without a task section, a test that inherits checks the whole notebook
    assert _run(tests[:1])["inherits"].startswith("inherits failed: Expected no loop")


def test_unparsable_cells_of_the_section_fail_the_test(lecture: Path) -> None:
    nb = _lecture()
    nb["cells"].insert(6, code("def broken(:\n    pass"))  # cell 7, in ex2
    write_notebook(lecture, nb)
    defines = {"concept": "function", "min": 1}
    results = _run(
        [
            StructureTest("ex2", lecture, defines, section="ex2"),
            StructureTest("ex1", lecture, defines, section="ex1"),
            StructureTest("whole notebook", lecture, defines, section=None),
        ]
    )
    assert results["ex2"].startswith("ex2 failed: Could not parse cell 7: ")
    assert results["ex2"].endswith(" (line 1).")
    # left out of the whole notebook, as long as the rules hold
    assert (results["ex1"], results["whole notebook"]) == ("", "")


def test_print_tests_compare_the_output_of_their_section(lecture: Path) -> None:
    tests: list[Test] = [
        # the whole notebook runs: ex2's code uses ex1's, and ex3's an import above
        PrintTest("inherits", lecture, "3"),
        PrintTest("ex3", lecture, "True", section="ex3"),
        PrintTest("whole notebook", lecture, "9\n3\nTrue", section=None),
    ]
    assert _run(tests, {lecture: "ex2"}) == {
        "inherits": "",
        "ex3": "",
        "whole notebook": "",
    }
    results = _run(tests, {lecture: "ex1"})
    assert results["inherits"].startswith("inherits failed: ")
    assert "- 3" in results["inherits"].splitlines()  # expected
    assert "+ 9" in results["inherits"].splitlines()  # printed by ex1


def test_the_task_section_is_only_set_while_a_test_runs(lecture: Path) -> None:
    test = StructureTest("inherits", lecture, NO_LOOPS)
    assert test.resolved_section() is None
    token = TASK_SECTIONS.set({lecture.absolute(): "ex1"})
    try:
        assert test.resolved_section() == "ex1"
        # relative paths name the same notebook
        relative = StructureTest("relative", Path("lecture.ipynb"), NO_LOOPS)
        assert relative.resolved_section() == "ex1"
        assert (
            StructureTest("own", lecture, NO_LOOPS, section=None).resolved_section()
            is None
        )
    finally:
        TASK_SECTIONS.reset(token)
    _run([test], {lecture: "ex1"})
    assert TASK_SECTIONS.get() == {}


# --- task files ---


def test_task_files_store_sections_relative_to_them(
    lecture: Path, chdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = chdir / "helper.py"
    helper.write_text("x = 1\n")
    task_file = Task.create_task_json(
        chdir / "tasks/t", "T", "", [lecture, helper], sections={lecture: "ex2"}
    )
    original = task_file.read_text()
    assert std_json.loads(original)["sections"] == {"../lecture.ipynb": "ex2"}

    fields = Task.read_task_json(task_file)
    assert fields["sections"] == {lecture: "ex2"}
    Task.create_task_json(task_file, **fields)
    assert task_file.read_text() == original

    # run from another directory: by absolute path
    monkeypatch.chdir(chdir / "tasks")
    task = Task(Path("t.pyicstask.json"), setup=False)
    assert task.sections == {lecture: "ex2"}


def test_task_files_without_sections_store_none(lecture: Path, chdir: Path) -> None:
    task_file = Task.create_task_json(chdir / "t", "T", "", lecture)
    assert "sections" not in std_json.loads(task_file.read_text())
    assert Task.read_task_json(task_file)["sections"] == {}
    assert Task(task_file, setup=False).sections == {}


@pytest.mark.parametrize(
    ("sections", "message"),
    [
        ({"helper.py": "ex2"}, "isn't one of the exercise notebooks"),
        ({"other.ipynb": "ex2"}, "isn't one of the exercise notebooks"),
        ({"lecture.ipynb": "ex 2"}, "can't be the name of a section"),
    ],
)
def test_task_sections_are_checked(
    lecture: Path, chdir: Path, sections: dict[str, str], message: str
) -> None:
    (chdir / "helper.py").write_text("x = 1\n")
    with pytest.raises(ValueError, match=message):
        Task.create_task_json(
            chdir / "t", "T", "", [lecture, chdir / "helper.py"], sections=sections
        )


def test_tests_that_inherit_store_no_section(lecture: Path, chdir: Path) -> None:
    tests: list[Test] = [
        StructureTest("inherits", lecture, NO_LOOPS),
        StructureTest("whole notebook", lecture, NO_LOOPS, section=None),
        PrintTest("ex1", lecture, "9", section="ex1"),
    ]
    task_file = Task.create_task_json(
        chdir / "t", "T", "", lecture, tests=tests, sections={lecture: "ex2"}
    )
    stored = [t["attributes"] for t in std_json.loads(task_file.read_text())["tests"]]
    assert "section" not in stored[0]
    assert (stored[1]["section"], stored[2]["section"]) == (None, "ex1")

    read = Task.read_task_json(task_file)["tests"]
    assert [t.section for t in read] == [INHERIT, None, "ex1"]
    assert "section" not in vars(read[0])


# --- several tasks in one notebook ---


def _shared_course(root: Path, notebook: list[Any]) -> tuple[Path, Path]:
    """A course of two tasks that share the notebook ``lecture.ipynb``, one per
    section, written to ``root``. Returns the manifest and the notebook's path."""
    nb = root / "lecture.ipynb"
    text = std_json.dumps(make_notebook(notebook))
    ex1 = Task.create_task_json(
        root / ".tasks/ex1",
        "Squares",
        "Exercise 1",
        nb,
        tests=[SimpleTest("squares", nb, "square", 4, function_args=[2])],
        file_contents={nb: text},
        sections={nb: "ex1"},
    )
    ex2 = Task.create_task_json(
        root / ".tasks/ex2",
        "Sums",
        "Exercise 2",
        nb,
        tests=[
            PrintTest("prints", nb, "3"),
            StructureTest("uses a loop", nb, {"concept": "loop", "min": 1}),
        ],
        file_contents={nb: text},
        sections={nb: "ex2"},
    )
    return Course.create_course_manifest(root / "lecture", [ex1, ex2]), nb


def test_tasks_sharing_a_notebook_progress_independently(
    chdir: Path, no_new_threads: None
) -> None:
    template = copy.deepcopy(LECTURE)
    template[2] = "def square(x):\n    return 0"
    template[6] = "print(total([1, 2]))"  # solves ex2, whatever square does
    manifest, nb = _shared_course(chdir, template)

    course = Course.load_course_manifest(manifest, check_progress=True)
    _wait_for(lambda: not course.loading and not course.check_status)
    # the first task's setup created the notebook, which the second task's check found
    assert nb.is_file()
    assert course.progress == Fraction(1, 2)
    assert course.instructions == "Exercise 1"  # the first unsolved task

    # ex1 solved, and ex2 broken
    other = copy.deepcopy(LECTURE)
    other[6] = "print(total([1, 2, 3]))"
    _save(nb, other)
    _wait_for(lambda: (course.tick(), course.progress == 1)[1])  # ex1, now solved

    reopened = Course.load_course_manifest(manifest, check_progress=True)
    _wait_for(lambda: not reopened.loading and not reopened.check_status)
    assert reopened.progress == Fraction(1, 2)
    assert reopened.instructions == "Exercise 2"
